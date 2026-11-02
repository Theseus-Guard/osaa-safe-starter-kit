"""Risk impact values of a case: Confidentiality (C), Integrity (B) and Availability (A), each from 1 to 5, and the risk score.

Each value is optional. The risk score is the mean of the three values, rounded half up to one decimal place, and it is
None until all three are set. Nothing derived is stored: the score is computed from the stored values.

The values are kept as numbered versions (migration 0038). A change inserts the next version and writes one audit row
with the actor, the changed values, and the new version. A write names the version it read (expected_version); a
different current version is a conflict, and nothing is stored. A request that changes nothing stores nothing.

Access: reads need a case-read permission and a visible case. Writes need cases:triage, the permission that changes the
classification, and the case must be visible and open. A case the caller may not see is reported as not found.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

from safe_kit import audit, cases
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.idempotency import KeyLookup, KeyScope, StoredResponse, lock_scope, lookup, store

MIN_VALUE = 1
MAX_VALUE = 5
# The three values in the order C, B, A, under the column each one is stored in.
IMPACT_FIELDS = ("confidentiality", "integrity", "availability")


class ImpactOutcome(Enum):
    OK = "ok"
    # The case does not exist, is deleted, or is not visible to the caller.
    NOT_FOUND = "not_found"
    CLOSED = "closed"
    # The caller read an older version than the current one. The result carries the current version.
    VERSION_CONFLICT = "version_conflict"
    # The same idempotency key and request came again. The result carries the stored response.
    REPLAY = "replay"
    # The same idempotency key came with a different request. Nothing was stored.
    KEY_REUSED = "key_reused"


@dataclass(frozen=True)
class ImpactScores:
    """One version of the impact values. Version 0 means none is recorded, so every value is not set."""

    case_id: str
    version: int
    confidentiality: int | None = None
    integrity: int | None = None
    availability: int | None = None
    created_by: str | None = None
    created_at: datetime | None = None

    def values(self) -> tuple[int | None, int | None, int | None]:
        return (self.confidentiality, self.integrity, self.availability)

    @property
    def risk_score(self) -> float | None:
        return risk_score(self.values())


@dataclass(frozen=True)
class ImpactResult:
    outcome: ImpactOutcome
    value: ImpactScores | None = None
    stored: StoredResponse | None = None


def risk_score(values: Iterable[int | None]) -> float | None:
    """The mean of the three values, rounded half up to one decimal place. None when any value is not set.

    With three integers the mean is never exactly halfway between two tenths, so the rounding mode does not change a result.
    """
    chosen = tuple(values)
    if not chosen or any(value is None for value in chosen):
        return None
    mean = Decimal(sum(chosen)) / Decimal(len(chosen))
    return float(mean.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def impact_changes(old: ImpactScores, new: Mapping[str, int | None]) -> dict[str, dict[str, Any]]:
    """The values that differ, as {field: {"old": ..., "new": ...}}."""
    return {
        name: {"old": getattr(old, name), "new": new[name]}
        for name in IMPACT_FIELDS
        if getattr(old, name) != new[name]
    }


_COLUMNS = (
    "case_id::text, version, confidentiality, integrity, availability, created_by::text, created_at"
)


def _lock_case(conn, case_id: str, scope: CaseScope) -> str | None:
    """Lock the case row, then check the caller's visibility. Returns the case status, or None when not visible."""
    row = conn.execute("SELECT status FROM cases WHERE id = %s AND NOT is_deleted FOR UPDATE", (case_id,)).fetchone()
    if row is None or not cases.is_visible_on(conn, case_id, scope):
        return None
    return row[0]


def _current(conn, case_id: str) -> ImpactScores:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM case_impact_scores WHERE case_id = %s AND NOT is_deleted ORDER BY version DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if row is None:
        return ImpactScores(case_id=case_id, version=0)
    return ImpactScores(*row)


def _current_for(conn, case_ids: list[str]) -> dict[str, ImpactScores]:
    # The qualified column names keep DISTINCT ON and ORDER BY on the table column, not on the text output column.
    rows = conn.execute(
        """
        SELECT DISTINCT ON (s.case_id) s.case_id::text, s.version, s.confidentiality, s.integrity, s.availability,
               s.created_by::text, s.created_at
        FROM case_impact_scores s
        WHERE s.case_id = ANY(%s::uuid[]) AND NOT s.is_deleted
        ORDER BY s.case_id, s.version DESC
        """,
        (case_ids,),
    ).fetchall()
    return {row[0]: ImpactScores(*row) for row in rows}


def read_impact_scores(case_id: str, scope: CaseScope) -> ImpactScores | None:
    """The current values (version 0 when none is recorded), or None when the scope may not see the case."""
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return None
        return _current(conn, case_id)


def current_scores_for_cases(case_ids: Iterable[str]) -> dict[str, ImpactScores]:
    """The current values of many cases in one query. A case with no version is absent from the mapping.

    The caller passes only cases it has already checked for visibility.
    """
    ids = list(case_ids)
    if not ids:
        return {}
    with connect() as conn:
        return _current_for(conn, ids)


def update_impact_scores(
    case_id: str,
    expected_version: int,
    values: Mapping[str, int | None],
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[ImpactScores], Any] | None = None,
) -> ImpactResult:
    """Store the given values as the next version. values holds every field; a field set to None is not set.

    The idempotency record, when given, is written in the same transaction as the version and its audit row.
    """
    with connect() as conn:
        if idempotency is not None:
            lock_scope(conn, idempotency)
            found, stored = lookup(conn, idempotency)
            if found is KeyLookup.REPLAY:
                return ImpactResult(ImpactOutcome.REPLAY, stored=stored)
            if found is KeyLookup.REUSED:
                return ImpactResult(ImpactOutcome.KEY_REUSED)
        status = _lock_case(conn, case_id, scope)
        if status is None:
            return ImpactResult(ImpactOutcome.NOT_FOUND)
        if status == "closed":
            return ImpactResult(ImpactOutcome.CLOSED)
        current = _current(conn, case_id)
        if current.version != expected_version:
            return ImpactResult(ImpactOutcome.VERSION_CONFLICT, value=current)
        changes = impact_changes(current, values)
        if changes:
            new_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM case_impact_scores WHERE case_id = %s",
                (case_id,),
            ).fetchone()[0]
            created_at = conn.execute(
                """
                INSERT INTO case_impact_scores (case_id, version, confidentiality, integrity, availability, created_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING created_at
                """,
                (
                    case_id,
                    new_version,
                    values["confidentiality"],
                    values["integrity"],
                    values["availability"],
                    scope.user_id,
                ),
            ).fetchone()[0]
            stored_version = ImpactScores(
                case_id=case_id,
                version=new_version,
                confidentiality=values["confidentiality"],
                integrity=values["integrity"],
                availability=values["availability"],
                created_by=scope.user_id,
                created_at=created_at,
            )
            audit.record(
                conn,
                audit.CASE_IMPACT_SCORES_CHANGE,
                "case",
                case_id,
                actor_id=scope.user_id,
                details={"version": {"old": current.version, "new": new_version}, **changes},
            )
        else:
            # Nothing changed: no version and no audit row. The current version is the answer.
            stored_version = current
        if idempotency is not None and render is not None:
            store(conn, idempotency, StoredResponse(status_code=200, body=render(stored_version)))
    return ImpactResult(ImpactOutcome.OK, value=stored_version)
