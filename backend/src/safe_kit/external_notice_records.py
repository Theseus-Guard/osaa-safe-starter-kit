"""The T12-A records of an external notice (F3, work package A8; plans/03 T12-A; plans/02 disclosure flow; plans/01 roles 5).

Three records, each about one disclosure version of a case:
- Escalation: the named approver cannot send an urgent notice (unreachable, or in a conflict of interest). An independent
  backup who holds disclosures:approve is named, with the authority basis, the missing facts and the next update. The
  escalation changes no deadline task: the deadline keeps running (plans/02: "deadline durmaz").
- Recipient review: a review of the selected recipient, bound to the version digest.
- Package review: a review of the package, bound to the version digest.

Rules:
- The case must be visible to the caller and open. A closed case refuses new records (the same rule as notices).
- The version must belong to the case.
- The person who prepared the version may not name themselves as the backup (independence, plans/02: the approver and the
  preparer differ) and may not review their own version.
- The backup must be an active user who holds disclosures:approve. That is the only approval authority the system defines.
- Free text (authority basis, missing facts, next update) is kept in the rows and is never written to the audit log: the
  audit log keeps lengths only. Every record writes one audit row in the same transaction.
- Records are insert-only (migration 0053). No record is ever deleted or edited; a corrected fact is a new record.

Nothing here sends a notice or approves a version. The notice itself is still recorded through safe_kit.external_notices.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from safe_kit import audit, auth, cases, disclosure
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.idempotency import KeyReused, KeyScope, Replayed, check, remember

ESCALATION_REASONS = ("unreachable", "conflict_of_interest")
REVIEW_KINDS = ("recipient", "package")
TEXT_LIMIT = 500
BACKUP_PERMISSION = "disclosures:approve"


class Outcome(Enum):
    OK = "ok"
    REPLAY = "replay"
    KEY_REUSED = "key_reused"
    # The case is missing, deleted, or not visible to the caller.
    NOT_FOUND = "not_found"
    VERSION_NOT_FOUND = "version_not_found"
    CLOSED = "closed"
    # The backup named for an escalation prepared the version.
    PREPARER_IS_BACKUP = "preparer_cannot_be_backup"
    BACKUP_NOT_FOUND = "backup_not_found"
    # The backup does not hold disclosures:approve.
    BACKUP_NOT_APPROVER = "backup_not_approver"
    # The reviewer prepared the version.
    PREPARER_IS_REVIEWER = "preparer_cannot_review"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    value: Any = None
    stored: Any = None


@dataclass(frozen=True)
class NewEscalation:
    reason_code: str
    authority_basis: str
    missing_facts: str
    next_update: str
    backup_user_id: str


@dataclass(frozen=True)
class Escalation:
    id: str
    case_id: str
    package_id: str
    version_id: str
    reason_code: str
    authority_basis: str
    missing_facts: str
    next_update: str
    backup_user_id: str
    escalated_by: str
    escalated_at: datetime


@dataclass(frozen=True)
class Review:
    id: str
    review_kind: str
    case_id: str
    package_id: str
    version_id: str
    version: int
    digest: str
    recipient_ref: str | None
    reviewed_by: str
    reviewed_at: datetime


_ESCALATION_COLUMNS = (
    "id::text, case_id::text, package_id::text, version_id::text, reason_code, authority_basis, missing_facts, "
    "next_update, backup_user_id::text, escalated_by::text, escalated_at"
)
_REVIEW_COLUMNS = (
    "id::text, review_kind, case_id::text, package_id::text, version_id::text, version, digest, recipient_ref, "
    "reviewed_by::text, reviewed_at"
)


def _escalation_from_row(row: tuple) -> Escalation:
    return Escalation(*row)


def _review_from_row(row: tuple) -> Review:
    return Review(*row)


def _lock_open_case(conn, case_id: str, scope: CaseScope) -> Outcome | None:
    """Lock the case, then check visibility and status. Returns the refusal, or None when the caller may write."""
    locked = cases.lock_visible_case(conn, case_id, scope)
    if locked is None:
        return Outcome.NOT_FOUND
    if locked[0] == "closed":
        return Outcome.CLOSED
    return None


def record_escalation(
    case_id: str,
    version_id: str,
    new: NewEscalation,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Escalation], Any] | None = None,
) -> Result:
    """Record that the named approver cannot send an urgent notice, and name the independent backup. Changes no deadline."""
    try:
        with connect() as conn:
            check(conn, idempotency)
            refusal = _lock_open_case(conn, case_id, scope)
            if refusal is not None:
                return Result(refusal)
            version = disclosure.load_version_on(conn, case_id, version_id)
            if version is None:
                return Result(Outcome.VERSION_NOT_FOUND)
            if new.backup_user_id == version.created_by:
                return Result(Outcome.PREPARER_IS_BACKUP)
            role_row = conn.execute(
                "SELECT role FROM users WHERE id = %s AND NOT is_deleted",
                (new.backup_user_id,),
            ).fetchone()
            if role_row is None:
                return Result(Outcome.BACKUP_NOT_FOUND)
            if BACKUP_PERMISSION not in auth.permissions_for_role(role_row[0]):
                return Result(Outcome.BACKUP_NOT_APPROVER)
            escalation_id = str(uuid.uuid4())
            row = conn.execute(
                f"""
                INSERT INTO external_notice_escalations (
                    id, case_id, package_id, version_id, reason_code, authority_basis, missing_facts, next_update,
                    backup_user_id, escalated_by, escalated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                RETURNING {_ESCALATION_COLUMNS}
                """,
                (
                    escalation_id,
                    case_id,
                    version.package_id,
                    version_id,
                    new.reason_code,
                    new.authority_basis,
                    new.missing_facts,
                    new.next_update,
                    new.backup_user_id,
                    scope.user_id,
                ),
            ).fetchone()
            created = _escalation_from_row(row)
            audit.record(
                conn,
                audit.DISCLOSURE_ESCALATION,
                "disclosure_escalation",
                escalation_id,
                actor_id=scope.user_id,
                details={
                    "case_id": case_id,
                    "package_id": version.package_id,
                    "version_id": version_id,
                    "version": version.version,
                    "reason_code": new.reason_code,
                    "backup_user_id": new.backup_user_id,
                    "authority_basis": {"length": len(new.authority_basis)},
                    "missing_facts": {"length": len(new.missing_facts)},
                    "next_update": {"length": len(new.next_update)},
                },
            )
            if idempotency is not None:
                assert render is not None
                remember(conn, idempotency, 201, render(created))
        return Result(Outcome.OK, value=created)
    except Replayed as replay:
        return Result(Outcome.REPLAY, stored=replay.stored)
    except KeyReused:
        return Result(Outcome.KEY_REUSED)


def record_review(
    case_id: str,
    version_id: str,
    review_kind: str,
    recipient_ref: str | None,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Review], Any] | None = None,
) -> Result:
    """Record a review of the recipient or of the package of one version, bound to the version digest."""
    assert review_kind in REVIEW_KINDS, review_kind
    assert (review_kind == "recipient") == (recipient_ref is not None)
    try:
        with connect() as conn:
            check(conn, idempotency)
            refusal = _lock_open_case(conn, case_id, scope)
            if refusal is not None:
                return Result(refusal)
            version = disclosure.load_version_on(conn, case_id, version_id)
            if version is None:
                return Result(Outcome.VERSION_NOT_FOUND)
            if version.created_by == scope.user_id:
                return Result(Outcome.PREPARER_IS_REVIEWER)
            review_id = str(uuid.uuid4())
            row = conn.execute(
                f"""
                INSERT INTO external_notice_reviews (
                    id, review_kind, case_id, package_id, version_id, version, digest, recipient_ref, reviewed_by,
                    reviewed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                RETURNING {_REVIEW_COLUMNS}
                """,
                (
                    review_id,
                    review_kind,
                    case_id,
                    version.package_id,
                    version_id,
                    version.version,
                    version.digest,
                    recipient_ref,
                    scope.user_id,
                ),
            ).fetchone()
            created = _review_from_row(row)
            audit.record(
                conn,
                audit.DISCLOSURE_REVIEW,
                "disclosure_review",
                review_id,
                actor_id=scope.user_id,
                details={
                    "case_id": case_id,
                    "package_id": version.package_id,
                    "version_id": version_id,
                    "version": version.version,
                    "review_kind": review_kind,
                    "digest": version.digest,
                    "recipient_ref": recipient_ref,
                },
            )
            if idempotency is not None:
                assert render is not None
                remember(conn, idempotency, 201, render(created))
        return Result(Outcome.OK, value=created)
    except Replayed as replay:
        return Result(Outcome.REPLAY, stored=replay.stored)
    except KeyReused:
        return Result(Outcome.KEY_REUSED)


# --- read -----------------------------------------------------------------------------------------------------------
# The API checks that the case is visible and that the version belongs to it before it calls these; they only read.


def list_escalations(case_id: str, version_id: str, limit: int) -> list[Escalation]:
    """The escalations of one version, newest first."""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_ESCALATION_COLUMNS}
            FROM external_notice_escalations
            WHERE case_id = %s AND version_id = %s AND NOT is_deleted
            ORDER BY escalated_at DESC, id DESC
            LIMIT %s
            """,
            (case_id, version_id, limit),
        ).fetchall()
    return [_escalation_from_row(row) for row in rows]


def list_reviews(
    case_id: str, version_id: str, review_kind: str | None, limit: int
) -> list[Review]:
    """The reviews of one version, newest first. review_kind None lists both kinds."""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_REVIEW_COLUMNS}
            FROM external_notice_reviews
            WHERE case_id = %s AND version_id = %s AND NOT is_deleted
              AND (%s::text IS NULL OR review_kind = %s::text)
            ORDER BY reviewed_at DESC, id DESC
            LIMIT %s
            """,
            (case_id, version_id, review_kind, review_kind, limit),
        ).fetchall()
    return [_review_from_row(row) for row in rows]
