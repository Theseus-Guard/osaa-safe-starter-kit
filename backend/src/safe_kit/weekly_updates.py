"""Weekly machine-readable updates: internal status records for cases with an open significant risk.

The document of each update follows schemas/weekly-update.v0.json, a project-defined format (not an official SAFE
schema). An update is internal: it needs no approval and goes to no external recipient.

A significant risk is an impact area of the case whose severity is high or critical (case_records.impacts). F1 has no
separate risk record, so the impact severity is the proxy used here. A risk counts as open while the case is open.

Cadence: the first update is created when the case first has a significant risk, then one update every 7 elapsed days
for as long as the risk stays open (deadlines.next_weekly_update). The worker runs generate_due_updates. For each case
it takes a transaction-level advisory lock, then re-reads the case, the risks and the latest update under that lock.
So runs that overlap (several workers, or a rerun in the same week) never create a second update in the same week.

Closure is unchanged: cases.closure_blockers decides it. Creating an update never changes a deadline task.
No free text is copied into a document, so every reader who can see the case can read its updates.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from safe_kit import audit, cases
from safe_kit.cases import SAFE_DEADLINE_KINDS, CaseScope
from safe_kit.db import connect
from safe_kit.deadlines import next_weekly_update

WEEKLY_UPDATE_SCHEMA_VERSION = "0.1.0"
# Impact severities that make a risk significant. none, low, medium and unknown do not.
SIGNIFICANT_SEVERITIES = ("high", "critical")


def is_due(latest_generated_at: datetime | None, now: datetime) -> bool:
    """A new update is due when the case has none yet, or 7 elapsed days have passed since the latest one."""
    if latest_generated_at is None:
        return True
    return next_weekly_update(latest_generated_at) <= now


def _iso(moment: datetime) -> str:
    """RFC 3339 in UTC with a Z suffix, to whole seconds, as the schema requires."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_document(
    *,
    update_id: str,
    case_id: str,
    case_reference: str,
    sequence: int,
    generated_at: datetime,
    risks: Iterable[tuple[str, str]],
    tasks: Iterable[tuple[str, datetime, str]],
) -> dict[str, Any]:
    """The update as the project-defined document. risks holds (area, severity); tasks holds (kind, due_at, status)."""
    return {
        "weekly_update_schema_version": WEEKLY_UPDATE_SCHEMA_VERSION,
        "weekly_update_id": update_id,
        "case_id": case_id,
        "case_reference": case_reference,
        "sequence": sequence,
        "generated_at": _iso(generated_at),
        "next_update_due_at": _iso(next_weekly_update(generated_at)),
        "significant_risks": [{"area": area, "severity": severity} for area, severity in risks],
        "deadline_tasks": [{"kind": kind, "due_at": _iso(due_at), "status": status} for kind, due_at, status in tasks],
    }


# --- creation (worker) ----------------------------------------------------------------------------------------------

_CANDIDATES_SQL = """
SELECT c.id::text FROM cases c
WHERE NOT c.is_deleted AND c.status = 'open'
  AND EXISTS (
      SELECT 1 FROM impacts i
      WHERE i.case_id = c.id AND NOT i.is_deleted AND i.severity = ANY(%s)
  )
ORDER BY c.created_at, c.id
"""


def generate_due_updates(now: datetime | None = None) -> int:
    """Create every weekly update that is due at `now` (UTC now by default). Returns how many were created.

    Each case is handled in its own transaction, so one case never holds back another. Running it again at the same
    moment creates nothing new.
    """
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    with connect() as conn:
        case_ids = [row[0] for row in conn.execute(_CANDIDATES_SQL, (list(SIGNIFICANT_SEVERITIES),)).fetchall()]
    created = 0
    for case_id in case_ids:
        with connect() as conn:
            if _create_if_due(conn, case_id, moment):
                created += 1
    return created


def _create_if_due(conn, case_id: str, now: datetime) -> bool:
    # The lock is released at the end of the transaction. Everything below runs under it, so a second run that waits
    # for the lock sees the update that the first run committed.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"weekly_update:{case_id}",))
    case_row = conn.execute(
        "SELECT reference, status FROM cases WHERE id = %s AND NOT is_deleted", (case_id,)
    ).fetchone()
    if case_row is None or case_row[1] != "open":
        return False
    risks = conn.execute(
        "SELECT area, severity FROM impacts WHERE case_id = %s AND NOT is_deleted AND severity = ANY(%s) ORDER BY area",
        (case_id, list(SIGNIFICANT_SEVERITIES)),
    ).fetchall()
    if not risks:
        return False
    latest = conn.execute(
        "SELECT sequence, generated_at FROM weekly_updates WHERE case_id = %s AND NOT is_deleted "
        "ORDER BY sequence DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if not is_due(latest[1] if latest else None, now):
        return False
    sequence = 1 if latest is None else latest[0] + 1
    tasks = conn.execute(
        "SELECT kind, due_at, status FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted AND kind = ANY(%s) "
        "ORDER BY due_at, kind",
        (case_id, list(SAFE_DEADLINE_KINDS)),
    ).fetchall()
    update_id = str(uuid.uuid4())
    document = build_document(
        update_id=update_id,
        case_id=case_id,
        case_reference=case_row[0],
        sequence=sequence,
        generated_at=now,
        risks=risks,
        tasks=tasks,
    )
    conn.execute(
        "INSERT INTO weekly_updates (id, case_id, sequence, generated_at, document) VALUES (%s, %s, %s, %s, %s)",
        (update_id, case_id, sequence, now, Jsonb(document)),
    )
    audit.record(
        conn,
        audit.CASE_WEEKLY_UPDATE_CREATED,
        "case",
        target_id=case_id,
        details={"weekly_update_id": update_id, "sequence": sequence},
    )
    return True


# --- reads (API) ----------------------------------------------------------------------------------------------------


def list_weekly_updates(case_id: str, scope: CaseScope) -> list[dict[str, Any]] | None:
    """The documents of the case, oldest first. None when the scope may not see the case."""
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return None
        rows = conn.execute(
            "SELECT document FROM weekly_updates WHERE case_id = %s AND NOT is_deleted ORDER BY sequence",
            (case_id,),
        ).fetchall()
    return [row[0] for row in rows]


def get_weekly_update(case_id: str, update_id: str, scope: CaseScope) -> tuple[bool, dict[str, Any] | None]:
    """(case_visible, document). The document is None when the update does not belong to this case."""
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return False, None
        row = conn.execute(
            "SELECT document FROM weekly_updates WHERE id = %s AND case_id = %s AND NOT is_deleted",
            (update_id, case_id),
        ).fetchone()
    return True, (row[0] if row else None)
