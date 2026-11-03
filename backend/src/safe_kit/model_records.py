"""Model, runtime and reasoning records (F2, package T05; plans/01 R05, plans/02 evidence categories).

A record states the observed configuration of the model that produced an agent run: the model name and version, the
runtime, the provider alias, and the reasoning configuration. It also says when it was recorded. A record concerns the
case as a whole, or one evidence item, or one review round. It holds no prompt, no reasoning text, no secret and no
personal data. The identifier fields accept only letters, digits and . _ : / @ + -
(see migration 0041).

Rules:
- Records are versioned and insert-only (migration 0041, the pattern of case_classifications). A revocation inserts the
  next version of the same record, with the reason. Earlier versions are never changed.
- A revoked record stays readable for audit. It is never the current configuration: the current listing leaves it out.
- The case is locked before a change, and the caller must see the case (cases.is_visible_on). A case the caller may not
  see is reported as not found. Changes to a closed case are refused.
- Replay: a read returns the stored configuration unchanged, so the same configuration can be recorded again.
- Free text: the revocation reason is the only free text. The audit log keeps its length only.
  The API returns it only to callers with the full view (cases:read_all).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from safe_kit import audit, cases
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.idempotency import KeyScope, check, remember

REASONING_MODES = ("disabled", "enabled", "unknown")
REASONING_EFFORTS = ("low", "medium", "high", "max")


class ModelRecordOutcome(Enum):
    OK = "ok"
    # The case is missing, deleted or not visible to the caller.
    NOT_FOUND = "not_found"
    # The record is missing from this case (or not visible through it).
    RECORD_NOT_FOUND = "record_not_found"
    CLOSED = "closed"
    # The linked evidence item or review round is missing, or belongs to another case.
    REFERENCE_NOT_FOUND = "reference_not_found"
    ALREADY_REVOKED = "already_revoked"


@dataclass(frozen=True)
class Reasoning:
    mode: str
    effort: str | None
    budget_tokens: int | None


@dataclass(frozen=True)
class Configuration:
    """What the replay needs: the same values, sent again, give the same configuration."""

    model_name: str
    model_version: str
    runtime: str
    provider_alias: str
    reasoning: Reasoning


@dataclass(frozen=True)
class ModelRecord:
    """One version of a record. The version shown is the latest one: a revoked record shows its revocation."""

    record_id: str
    case_id: str
    version: int
    configuration: Configuration
    evidence_item_id: str | None
    review_round_id: str | None
    # When the configuration was recorded. Copied to every version of the record.
    recorded_at: datetime
    # Who wrote this version, and when: the recorder for version 1, the revoker for a revocation.
    written_by: str
    written_at: datetime
    revoked: bool
    revoked_at: datetime | None
    revocation_reason: str | None


_COLUMNS = (
    "record_id::text, case_id::text, version, model_name, model_version, runtime, provider_alias, "
    "reasoning_mode, reasoning_effort, reasoning_budget_tokens, evidence_item_id::text, review_round_id::text, "
    "recorded_at, created_by::text, created_at, revoked, revoked_at, revocation_reason"
)


def _from_row(row: tuple) -> ModelRecord:
    return ModelRecord(
        record_id=row[0],
        case_id=row[1],
        version=row[2],
        configuration=Configuration(
            model_name=row[3],
            model_version=row[4],
            runtime=row[5],
            provider_alias=row[6],
            reasoning=Reasoning(mode=row[7], effort=row[8], budget_tokens=row[9]),
        ),
        evidence_item_id=row[10],
        review_round_id=row[11],
        recorded_at=row[12],
        written_by=row[13],
        written_at=row[14],
        revoked=row[15],
        revoked_at=row[16],
        revocation_reason=row[17],
    )


def _latest(conn, case_id: str, record_id: str | None = None) -> list[ModelRecord]:
    """The latest version of each record of the case, or of one record when record_id is given. Oldest first."""
    rows = conn.execute(
        f"""
        SELECT {_COLUMNS}
        FROM (
            SELECT DISTINCT ON (record_id) *
            FROM model_records
            WHERE case_id = %s AND NOT is_deleted AND (%s::uuid IS NULL OR record_id = %s::uuid)
            ORDER BY record_id, version DESC
        ) latest
        ORDER BY recorded_at, record_id
        """,
        (case_id, record_id, record_id),
    ).fetchall()
    return [_from_row(row) for row in rows]


def _lock_case(conn, case_id: str, scope: CaseScope) -> str | None:
    """Lock the case row, then check the caller's visibility. Returns the case status, or None when not visible."""
    row = conn.execute(
        "SELECT status FROM cases WHERE id = %s AND NOT is_deleted FOR UPDATE",
        (case_id,),
    ).fetchone()
    if row is None or not cases.is_visible_on(conn, case_id, scope, writing=True):
        return None
    return row[0]


def _reference_in_case(
    conn, case_id: str, evidence_item_id: str | None, review_round_id: str | None
) -> bool:
    """The linked evidence item or review round must belong to this case.

    A missing item and an item of another case give the same answer.
    """
    if evidence_item_id is not None:
        return (
            conn.execute(
                "SELECT 1 FROM evidence_items WHERE id = %s AND case_id = %s AND NOT is_deleted",
                (evidence_item_id, case_id),
            ).fetchone()
            is not None
        )
    if review_round_id is not None:
        return (
            conn.execute(
                "SELECT 1 FROM review_rounds WHERE id = %s AND case_id = %s AND NOT is_deleted",
                (review_round_id, case_id),
            ).fetchone()
            is not None
        )
    return True


def add_model_record(
    case_id: str,
    configuration: Configuration,
    scope: CaseScope,
    *,
    evidence_item_id: str | None = None,
    review_round_id: str | None = None,
    idempotency: KeyScope | None = None,
    render: Callable[[ModelRecord], Any] | None = None,
) -> tuple[ModelRecordOutcome, ModelRecord | None]:
    """Record a configuration as version 1 of a new record.

    With an idempotency scope the key is checked first, and the stored response is render(record).
    """
    with connect() as conn:
        check(conn, idempotency)
        status = _lock_case(conn, case_id, scope)
        if status is None:
            return ModelRecordOutcome.NOT_FOUND, None
        if status == "closed":
            return ModelRecordOutcome.CLOSED, None
        if not _reference_in_case(conn, case_id, evidence_item_id, review_round_id):
            return ModelRecordOutcome.REFERENCE_NOT_FOUND, None
        record_id = str(uuid.uuid4())
        reasoning = configuration.reasoning
        row = conn.execute(
            """
            INSERT INTO model_records (
                record_id, case_id, evidence_item_id, review_round_id, version,
                model_name, model_version, runtime, provider_alias,
                reasoning_mode, reasoning_effort, reasoning_budget_tokens, recorded_at, created_by
            )
            VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, now(), %s)
            RETURNING recorded_at, created_at
            """,
            (
                record_id,
                case_id,
                evidence_item_id,
                review_round_id,
                configuration.model_name,
                configuration.model_version,
                configuration.runtime,
                configuration.provider_alias,
                reasoning.mode,
                reasoning.effort,
                reasoning.budget_tokens,
                scope.user_id,
            ),
        ).fetchone()
        audit.record(
            conn,
            audit.CASE_MODEL_RECORD_ADD,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "model_record_id": record_id,
                "version": 1,
                "evidence_item_id": evidence_item_id,
                "review_round_id": review_round_id,
            },
        )
        record = ModelRecord(
            record_id=record_id,
            case_id=case_id,
            version=1,
            configuration=configuration,
            evidence_item_id=evidence_item_id,
            review_round_id=review_round_id,
            recorded_at=row[0],
            written_by=scope.user_id,
            written_at=row[1],
            revoked=False,
            revoked_at=None,
            revocation_reason=None,
        )
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 201, render(record))
    return ModelRecordOutcome.OK, record


def revoke_model_record(
    case_id: str,
    record_id: str,
    reason: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[ModelRecord], Any] | None = None,
) -> tuple[ModelRecordOutcome, ModelRecord | None]:
    """Insert the next version of the record, marked revoked with the reason. Nothing else is changed."""
    with connect() as conn:
        check(conn, idempotency)
        status = _lock_case(conn, case_id, scope)
        if status is None:
            return ModelRecordOutcome.NOT_FOUND, None
        if status == "closed":
            return ModelRecordOutcome.CLOSED, None
        found = _latest(conn, case_id, record_id)
        if not found:
            return ModelRecordOutcome.RECORD_NOT_FOUND, None
        current = found[0]
        if current.revoked:
            return ModelRecordOutcome.ALREADY_REVOKED, current
        new_version = current.version + 1
        config = current.configuration
        row = conn.execute(
            """
            INSERT INTO model_records (
                record_id, case_id, evidence_item_id, review_round_id, version,
                model_name, model_version, runtime, provider_alias,
                reasoning_mode, reasoning_effort, reasoning_budget_tokens, recorded_at,
                revoked, revoked_at, revocation_reason, created_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, now(), %s, %s)
            RETURNING revoked_at, created_at
            """,
            (
                record_id,
                case_id,
                current.evidence_item_id,
                current.review_round_id,
                new_version,
                config.model_name,
                config.model_version,
                config.runtime,
                config.provider_alias,
                config.reasoning.mode,
                config.reasoning.effort,
                config.reasoning.budget_tokens,
                current.recorded_at,
                reason,
                scope.user_id,
            ),
        ).fetchone()
        audit.record(
            conn,
            audit.CASE_MODEL_RECORD_REVOKE,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "model_record_id": record_id,
                "version": {"old": current.version, "new": new_version},
                "reason": {"length": len(reason)},
            },
        )
        revoked = ModelRecord(
            record_id=record_id,
            case_id=case_id,
            version=new_version,
            configuration=config,
            evidence_item_id=current.evidence_item_id,
            review_round_id=current.review_round_id,
            recorded_at=current.recorded_at,
            written_by=scope.user_id,
            written_at=row[1],
            revoked=True,
            revoked_at=row[0],
            revocation_reason=reason,
        )
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 200, render(revoked))
    return ModelRecordOutcome.OK, revoked


def list_model_records(
    case_id: str, scope: CaseScope, *, current_only: bool = False
) -> list[ModelRecord] | None:
    """The latest version of every record of the case, or None when the caller may not see the case.

    current_only leaves out the revoked records: they are kept for audit, but are not the current configuration.
    """
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return None
        records = _latest(conn, case_id)
    if current_only:
        return [record for record in records if not record.revoked]
    return records


def read_model_record(
    case_id: str, record_id: str, scope: CaseScope
) -> ModelRecord | None:
    """The latest version of one record, or None when the case is not visible or the record is not in it."""
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return None
        found = _latest(conn, case_id, record_id)
    return found[0] if found else None
