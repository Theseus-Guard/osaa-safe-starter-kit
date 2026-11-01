"""Minimal case domain: a reporter opens a case with its awareness time t0, and SAFE deadline tasks are computed from it.

Only "own cases" access exists for now. Assignment, evidence, reviews, and disclosures are separate work.
A case that does not belong to the caller is reported as not found, so its existence is not revealed.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from safe_kit import audit
from safe_kit.db import connect
from safe_kit.deadlines import BusinessCalendar, deadlines_for

# The single configuration point for the working calendar. It is provisional until the owning organization
# confirms its working days and holidays. The version string is stored on every case, so a later change
# of calendar does not silently move deadlines that were already computed.
CASE_CALENDAR = BusinessCalendar(
    timezone=ZoneInfo("Europe/Istanbul"),
    holidays=frozenset(),
    version="provisional-1",
)


@dataclass(frozen=True)
class DeadlineTask:
    kind: str
    due_at: datetime
    status: str


@dataclass(frozen=True)
class Case:
    id: str
    reference: str
    title: str
    awareness_time: datetime
    status: str
    calendar_version: str
    created_at: datetime
    created_by: str
    deadlines: list[DeadlineTask] = field(default_factory=list)


def deadline_tasks_for(t0: datetime, calendar: BusinessCalendar = CASE_CALENDAR) -> list[DeadlineTask]:
    """The tasks created with a case. weekly_update is not created here: it depends on risks still being open."""
    due = deadlines_for(t0, calendar)
    return [
        DeadlineTask("affected_customers", due.affected_customers, "open"),
        DeadlineTask("preliminary_safe_report", due.preliminary_safe_report, "open"),
        DeadlineTask("broader_advisory", due.broader_advisory, "open"),
        DeadlineTask("preliminary_public_report", due.preliminary_public_report, "open"),
        DeadlineTask("remediation_status", due.remediation_status, "open"),
    ]


_CASE_COLUMNS = "id::text, reference, title, awareness_time, status, calendar_version, created_at, created_by::text"


def _to_case(row: tuple) -> Case:
    return Case(
        id=row[0],
        reference=row[1],
        title=row[2],
        awareness_time=row[3],
        status=row[4],
        calendar_version=row[5],
        created_at=row[6],
        created_by=row[7],
    )


def create_case(title: str, awareness_time: datetime, created_by: str) -> Case:
    """Create the case, its deadline tasks, and the audit row in one transaction."""
    tasks = deadline_tasks_for(awareness_time)
    with connect() as conn:
        sequence = conn.execute("SELECT nextval('case_reference_seq')").fetchone()[0]
        reference = f"SAFE-{datetime.now(UTC).year}-{sequence:06d}"
        row = conn.execute(
            f"""
            INSERT INTO cases (reference, title, awareness_time, created_by, status, calendar_version)
            VALUES (%s, %s, %s, %s, 'open', %s)
            RETURNING {_CASE_COLUMNS}
            """,
            (reference, title, awareness_time, created_by, CASE_CALENDAR.version),
        ).fetchone()
        case = _to_case(row)
        for task in tasks:
            conn.execute(
                "INSERT INTO deadline_tasks (case_id, kind, due_at, status) VALUES (%s, %s, %s, %s)",
                (case.id, task.kind, task.due_at, task.status),
            )
        audit.record(
            conn,
            audit.CASE_CREATE,
            "case",
            case.id,
            actor_id=created_by,
            details={"calendar_version": CASE_CALENDAR.version},
        )
    return replace(case, deadlines=tasks)


def find_own_case(case_id: str, owner_id: str) -> Case | None:
    """Return the case only if the owner created it. Any other case, or a deleted one, is None."""
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_CASE_COLUMNS} FROM cases WHERE id = %s AND created_by = %s AND NOT is_deleted",
            (case_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        tasks = conn.execute(
            """
            SELECT kind, due_at, status FROM deadline_tasks
            WHERE case_id = %s AND NOT is_deleted
            ORDER BY due_at, kind
            """,
            (case_id,),
        ).fetchall()
    return replace(_to_case(row), deadlines=[DeadlineTask(*task) for task in tasks])


def list_own_cases(owner_id: str, limit: int, offset: int) -> list[Case]:
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_CASE_COLUMNS} FROM cases
            WHERE created_by = %s AND NOT is_deleted
            ORDER BY created_at DESC, id
            LIMIT %s OFFSET %s
            """,
            (owner_id, limit, offset),
        ).fetchall()
    return [_to_case(row) for row in rows]


def count_own_cases(owner_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT count(*) FROM cases WHERE created_by = %s AND NOT is_deleted",
            (owner_id,),
        ).fetchone()
    return row[0]
