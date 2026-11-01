"""Case domain: reporting, access control, assignment, reviews, closure, deadline status, and change history.

Who may see a case is decided in SQL (CaseScope.visibility_sql), in the same transaction as every read or change.
A case the caller may not see is reported as not found, so its existence is not revealed.

Every change to a case writes its audit row in the same transaction. Each change first locks the case row,
so concurrent assignment, review, deadline, and closure changes run one after another.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from safe_kit import audit
from safe_kit import system_timezone
from safe_kit.db import connect
from safe_kit.deadlines import BusinessCalendar, deadlines_for
from safe_kit.system_timezone import TimezoneNotConfigured

# The working calendar is provisional until the owning organization confirms its working days and holidays.
# The version string is stored on every case, so a later change of calendar does not silently move deadlines
# that were already computed. The local timezone is not part of the version: each case stores its own snapshot
# of the system timezone that was in effect when it was created.
CALENDAR_VERSION = "provisional-1"
CALENDAR_HOLIDAYS: frozenset = frozenset()

# Roles that may hold an assignment on a case. Reporters and triagers are never assignees.
ASSIGNABLE_ROLES = frozenset({"investigator", "reviewer"})
REVIEW_OUTCOMES = frozenset({"pass", "fail", "insufficient_evidence", "not_applicable"})
DEADLINE_STATUSES = frozenset({"open", "done", "not_applicable"})
# A deadline task blocks closure unless it is done or explicitly not applicable.
CLOSABLE_TASK_STATUSES = frozenset({"done", "not_applicable"})


def calendar_for(timezone_name: str) -> BusinessCalendar:
    return BusinessCalendar(
        timezone=ZoneInfo(timezone_name),
        holidays=CALENDAR_HOLIDAYS,
        version=CALENDAR_VERSION,
    )


class Outcome(Enum):
    OK = "ok"
    # The case does not exist, is deleted, or is not visible to the caller. Also an unknown deadline task.
    NOT_FOUND = "not_found"
    CLOSED = "closed"
    NOT_CLOSED = "not_closed"
    BLOCKED = "blocked"
    ROLE_NOT_ASSIGNABLE = "role_not_assignable"
    ASSIGNEE_INACTIVE = "assignee_inactive"
    DUPLICATE = "duplicate"
    NOT_ASSIGNED = "not_assigned"
    ASSIGNMENT_NOT_FOUND = "assignment_not_found"
    TASK_NOT_FOUND = "task_not_found"
    NOT_APPLICABLE_FORBIDDEN = "not_applicable_forbidden"


@dataclass(frozen=True)
class CaseScope:
    """What the caller may see, taken from the caller's permissions.

    read_own: the caller created the case. read_all: every case. read_assigned: the caller has an active assignment.
    """

    user_id: str
    read_all: bool = False
    read_own: bool = False
    read_assigned: bool = False

    @classmethod
    def from_permissions(cls, user_id: str, permissions: frozenset[str]) -> "CaseScope":
        return cls(
            user_id=user_id,
            read_all="cases:read_all" in permissions,
            read_own="cases:read_own" in permissions,
            read_assigned="cases:read_assigned" in permissions,
        )

    def visibility_sql(self) -> tuple[str, tuple[Any, ...]]:
        """A condition on the cases table that holds exactly for the cases this scope may see."""
        if self.read_all:
            return "TRUE", ()
        clauses: list[str] = []
        params: list[Any] = []
        if self.read_own:
            clauses.append("cases.created_by = %s")
            params.append(self.user_id)
        if self.read_assigned:
            clauses.append(
                "EXISTS (SELECT 1 FROM case_assignments ca WHERE ca.case_id = cases.id "
                "AND ca.user_id = %s AND NOT ca.is_deleted)"
            )
            params.append(self.user_id)
        if not clauses:
            return "FALSE", ()
        return "(" + " OR ".join(clauses) + ")", tuple(params)


@dataclass(frozen=True)
class DeadlineTask:
    kind: str
    due_at: datetime
    status: str
    id: str | None = None
    not_applicable_reason: str | None = None


@dataclass(frozen=True)
class Case:
    id: str
    reference: str
    title: str
    awareness_time: datetime
    status: str
    calendar_version: str
    calendar_timezone: str
    created_at: datetime
    created_by: str
    deadlines: list[DeadlineTask] = field(default_factory=list)
    closed_at: datetime | None = None
    closed_by: str | None = None
    close_reason: str | None = None


@dataclass(frozen=True)
class Assignment:
    id: str
    case_id: str
    user_id: str
    username: str
    role: str
    assigned_by: str
    assigned_at: datetime


@dataclass(frozen=True)
class HistoryEntry:
    id: str
    action: str
    created_at: datetime
    actor_id: str | None
    details: dict[str, Any] | None


def deadline_tasks_for(t0: datetime, calendar: BusinessCalendar) -> list[DeadlineTask]:
    """The tasks created with a case. weekly_update is not created here: it depends on risks still being open."""
    due = deadlines_for(t0, calendar)
    return [
        DeadlineTask("affected_customers", due.affected_customers, "open"),
        DeadlineTask("preliminary_safe_report", due.preliminary_safe_report, "open"),
        DeadlineTask("broader_advisory", due.broader_advisory, "open"),
        DeadlineTask("preliminary_public_report", due.preliminary_public_report, "open"),
        DeadlineTask("remediation_status", due.remediation_status, "open"),
    ]


def closure_blockers(review_count: int, tasks: Iterable[tuple[str, str]]) -> list[str]:
    """Why a case cannot be closed yet. An empty list means it may be closed.

    A case needs at least one review, and every deadline task must be done or not applicable.
    An open weekly_update task is covered by the same rule, so it never closes silently.
    """
    blockers = []
    if review_count < 1:
        blockers.append("no review is recorded")
    for kind, status in tasks:
        if status not in CLOSABLE_TASK_STATUSES:
            blockers.append(f"deadline task {kind} is {status}")
    return blockers


def redact_history(entries: Sequence[HistoryEntry]) -> list[HistoryEntry]:
    """For callers without full case access: keep the action and the timestamp, drop actor and details."""
    return [replace(entry, actor_id=None, details=None) for entry in entries]


def _text_marker(text: str | None) -> dict[str, int] | None:
    # Free text (reasons and notes) is never written to the audit log. Only its length is recorded.
    return None if text is None else {"length": len(text)}


def _change(old: Any, new: Any) -> dict[str, Any]:
    return {"old": old, "new": new}


_CASE_COLUMNS = (
    "id::text, reference, title, awareness_time, status, calendar_version, created_at, created_by::text, "
    "calendar_timezone, closed_at, closed_by::text, close_reason"
)


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
        calendar_timezone=row[8],
        closed_at=row[9],
        closed_by=row[10],
        close_reason=row[11],
    )


def _load_tasks(conn, case_id: str) -> list[DeadlineTask]:
    rows = conn.execute(
        """
        SELECT id::text, kind, due_at, status, not_applicable_reason FROM deadline_tasks
        WHERE case_id = %s AND NOT is_deleted
        ORDER BY due_at, kind
        """,
        (case_id,),
    ).fetchall()
    return [DeadlineTask(id=row[0], kind=row[1], due_at=row[2], status=row[3], not_applicable_reason=row[4]) for row in rows]


def _lock_visible_case(conn, case_id: str, scope: CaseScope) -> tuple | None:
    """Lock the case row until the transaction ends. Returns (status, closed_at, closed_by) or None if not visible."""
    visibility, params = scope.visibility_sql()
    return conn.execute(
        f"SELECT status, closed_at, closed_by::text FROM cases WHERE id = %s AND NOT is_deleted AND {visibility} FOR UPDATE",
        (case_id, *params),
    ).fetchone()


def _is_visible(conn, case_id: str, scope: CaseScope) -> bool:
    visibility, params = scope.visibility_sql()
    row = conn.execute(
        f"SELECT 1 FROM cases WHERE id = %s AND NOT is_deleted AND {visibility}",
        (case_id, *params),
    ).fetchone()
    return row is not None


def _is_assigned(conn, case_id: str, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM case_assignments WHERE case_id = %s AND user_id = %s AND NOT is_deleted",
        (case_id, user_id),
    ).fetchone()
    return row is not None


def create_case(title: str, awareness_time: datetime, created_by: str) -> Case:
    """Create the case, its deadline tasks, and the audit row in one transaction.

    The calendar timezone is the system timezone in effect inside this transaction. It is stored on the case,
    so a later change of the system timezone does not move deadlines that were already computed.
    """
    with connect() as conn:
        setting = system_timezone.read_current(conn)
        if setting is None:
            raise TimezoneNotConfigured()
        calendar_timezone = setting.timezone
        tasks = deadline_tasks_for(awareness_time, calendar_for(calendar_timezone))
        sequence = conn.execute("SELECT nextval('case_reference_seq')").fetchone()[0]
        reference = f"SAFE-{datetime.now(UTC).year}-{sequence:06d}"
        row = conn.execute(
            f"""
            INSERT INTO cases (reference, title, awareness_time, created_by, status, calendar_version, calendar_timezone)
            VALUES (%s, %s, %s, %s, 'open', %s, %s)
            RETURNING {_CASE_COLUMNS}
            """,
            (reference, title, awareness_time, created_by, CALENDAR_VERSION, calendar_timezone),
        ).fetchone()
        case = _to_case(row)
        stored_tasks = []
        for task in tasks:
            task_id = conn.execute(
                "INSERT INTO deadline_tasks (case_id, kind, due_at, status) VALUES (%s, %s, %s, %s) RETURNING id::text",
                (case.id, task.kind, task.due_at, task.status),
            ).fetchone()[0]
            stored_tasks.append(replace(task, id=task_id))
        audit.record(
            conn,
            audit.CASE_CREATE,
            "case",
            case.id,
            actor_id=created_by,
            details={"calendar_version": CALENDAR_VERSION, "calendar_timezone": calendar_timezone},
        )
    return replace(case, deadlines=stored_tasks)


def find_visible_case(case_id: str, scope: CaseScope) -> Case | None:
    """Return the case with its deadline tasks if the scope may see it. Otherwise None, the same as a missing case."""
    visibility, params = scope.visibility_sql()
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_CASE_COLUMNS} FROM cases WHERE id = %s AND NOT is_deleted AND {visibility}",
            (case_id, *params),
        ).fetchone()
        if row is None:
            return None
        tasks = _load_tasks(conn, case_id)
    return replace(_to_case(row), deadlines=tasks)


def list_visible_cases(scope: CaseScope, limit: int, offset: int) -> list[Case]:
    visibility, params = scope.visibility_sql()
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_CASE_COLUMNS} FROM cases
            WHERE NOT is_deleted AND {visibility}
            ORDER BY created_at DESC, id
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        ).fetchall()
    return [_to_case(row) for row in rows]


def count_visible_cases(scope: CaseScope) -> int:
    visibility, params = scope.visibility_sql()
    with connect() as conn:
        row = conn.execute(
            f"SELECT count(*) FROM cases WHERE NOT is_deleted AND {visibility}",
            params,
        ).fetchone()
    return row[0]


def list_assignments(case_id: str, scope: CaseScope) -> list[Assignment] | None:
    """Active assignments of a case, or None when the scope may not see the case."""
    with connect() as conn:
        if not _is_visible(conn, case_id, scope):
            return None
        rows = conn.execute(
            """
            SELECT ca.id::text, ca.case_id::text, ca.user_id::text, u.username, u.role, ca.assigned_by::text, ca.assigned_at
            FROM case_assignments ca
            JOIN users u ON u.id = ca.user_id
            WHERE ca.case_id = %s AND NOT ca.is_deleted
            ORDER BY ca.assigned_at, u.username
            """,
            (case_id,),
        ).fetchall()
    return [Assignment(*row) for row in rows]


def assign_member(case_id: str, user_id: str, scope: CaseScope) -> tuple[Outcome, Assignment | None]:
    """Give an investigator or reviewer an active assignment on the case."""
    with connect() as conn:
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Outcome.NOT_FOUND, None
        if locked[0] == "closed":
            return Outcome.CLOSED, None
        user = conn.execute(
            "SELECT username, role, is_active FROM users WHERE id = %s AND NOT is_deleted",
            (user_id,),
        ).fetchone()
        if user is None or not user[2]:
            return Outcome.ASSIGNEE_INACTIVE, None
        if user[1] not in ASSIGNABLE_ROLES:
            return Outcome.ROLE_NOT_ASSIGNABLE, None
        if _is_assigned(conn, case_id, user_id):
            return Outcome.DUPLICATE, None
        row = conn.execute(
            """
            INSERT INTO case_assignments (case_id, user_id, assigned_by) VALUES (%s, %s, %s)
            RETURNING id::text, assigned_at
            """,
            (case_id, user_id, scope.user_id),
        ).fetchone()
        audit.record(
            conn,
            audit.CASE_ASSIGNMENT_ADD,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={"assignee_id": _change(None, user_id)},
        )
    assignment = Assignment(
        id=row[0],
        case_id=case_id,
        user_id=user_id,
        username=user[0],
        role=user[1],
        assigned_by=scope.user_id,
        assigned_at=row[1],
    )
    return Outcome.OK, assignment


def unassign_member(case_id: str, user_id: str, scope: CaseScope) -> Outcome:
    """Soft delete the active assignment. The row is kept, so the history stays complete."""
    with connect() as conn:
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Outcome.NOT_FOUND
        if locked[0] == "closed":
            return Outcome.CLOSED
        row = conn.execute(
            """
            UPDATE case_assignments SET is_deleted = true, deleted_at = now()
            WHERE case_id = %s AND user_id = %s AND NOT is_deleted
            RETURNING id
            """,
            (case_id, user_id),
        ).fetchone()
        if row is None:
            return Outcome.ASSIGNMENT_NOT_FOUND
        audit.record(
            conn,
            audit.CASE_ASSIGNMENT_REMOVE,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={"assignee_id": _change(user_id, None)},
        )
    return Outcome.OK


def record_review(case_id: str, outcome: str, note: str | None, scope: CaseScope) -> tuple[Outcome, str | None]:
    """Record a review. Only an active assignee may review, and only while the case is open."""
    with connect() as conn:
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Outcome.NOT_FOUND, None
        if locked[0] == "closed":
            return Outcome.CLOSED, None
        if not _is_assigned(conn, case_id, scope.user_id):
            return Outcome.NOT_ASSIGNED, None
        review_id = conn.execute(
            """
            INSERT INTO case_reviews (case_id, reviewer_id, outcome, note) VALUES (%s, %s, %s, %s)
            RETURNING id::text
            """,
            (case_id, scope.user_id, outcome, note),
        ).fetchone()[0]
        audit.record(
            conn,
            audit.CASE_REVIEW,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "review_id": _change(None, review_id),
                "outcome": _change(None, outcome),
                "note": _change(None, _text_marker(note)),
            },
        )
    return Outcome.OK, review_id


def update_deadline(
    case_id: str,
    task_id: str,
    status: str,
    reason: str | None,
    scope: CaseScope,
    may_mark_not_applicable: bool,
) -> Outcome:
    """Set a deadline task to done, open, or not applicable.

    Changing a task to or from not applicable needs the triager permission (may_mark_not_applicable).
    A request that changes nothing writes no audit row.
    """
    new_reason = reason if status == "not_applicable" else None
    with connect() as conn:
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Outcome.NOT_FOUND
        if locked[0] == "closed":
            return Outcome.CLOSED
        task = conn.execute(
            """
            SELECT kind, status, not_applicable_reason FROM deadline_tasks
            WHERE id = %s AND case_id = %s AND NOT is_deleted
            FOR UPDATE
            """,
            (task_id, case_id),
        ).fetchone()
        if task is None:
            return Outcome.TASK_NOT_FOUND
        kind, old_status, old_reason = task
        if "not_applicable" in (status, old_status) and not may_mark_not_applicable:
            return Outcome.NOT_APPLICABLE_FORBIDDEN
        changes: dict[str, Any] = {}
        if status != old_status:
            changes["status"] = _change(old_status, status)
        if new_reason != old_reason:
            changes["not_applicable_reason"] = _change(_text_marker(old_reason), _text_marker(new_reason))
        if not changes:
            return Outcome.OK
        conn.execute(
            "UPDATE deadline_tasks SET status = %s, not_applicable_reason = %s WHERE id = %s",
            (status, new_reason, task_id),
        )
        audit.record(
            conn,
            audit.CASE_DEADLINE_UPDATE,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={"task_id": task_id, "kind": kind, **changes},
        )
    return Outcome.OK


def close_case(case_id: str, reason: str, scope: CaseScope) -> tuple[Outcome, list[str]]:
    """Close the case if the closure rules hold. Otherwise return the blockers and change nothing."""
    with connect() as conn:
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Outcome.NOT_FOUND, []
        if locked[0] == "closed":
            return Outcome.CLOSED, []
        review_count = conn.execute(
            "SELECT count(*) FROM case_reviews WHERE case_id = %s AND NOT is_deleted",
            (case_id,),
        ).fetchone()[0]
        tasks = conn.execute(
            "SELECT kind, status FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted ORDER BY due_at, kind",
            (case_id,),
        ).fetchall()
        blockers = closure_blockers(review_count, tasks)
        if blockers:
            return Outcome.BLOCKED, blockers
        closed_at = conn.execute(
            """
            UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = %s
            WHERE id = %s
            RETURNING closed_at
            """,
            (scope.user_id, reason, case_id),
        ).fetchone()[0]
        audit.record(
            conn,
            audit.CASE_CLOSE,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "status": _change("open", "closed"),
                "closed_at": _change(None, closed_at.isoformat()),
                "closed_by": _change(None, scope.user_id),
                "close_reason": _change(None, _text_marker(reason)),
            },
        )
    return Outcome.OK, []


def reopen_case(case_id: str, reason: str, scope: CaseScope) -> Outcome:
    """Reopen a closed case. The reason and the previous closure stay in the audit log."""
    with connect() as conn:
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Outcome.NOT_FOUND
        status, closed_at, closed_by = locked
        if status != "closed":
            return Outcome.NOT_CLOSED
        conn.execute(
            "UPDATE cases SET status = 'open', closed_at = NULL, closed_by = NULL WHERE id = %s",
            (case_id,),
        )
        audit.record(
            conn,
            audit.CASE_REOPEN,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "status": _change("closed", "open"),
                "closed_at": _change(closed_at.isoformat() if closed_at else None, None),
                "closed_by": _change(closed_by, None),
                "reopen_reason": _change(None, _text_marker(reason)),
            },
        )
    return Outcome.OK


def case_history(case_id: str, scope: CaseScope, limit: int, offset: int) -> tuple[list[HistoryEntry], int] | None:
    """Audit rows of the case, newest first, or None when the scope may not see the case. Details are not redacted here."""
    with connect() as conn:
        if not _is_visible(conn, case_id, scope):
            return None
        rows = conn.execute(
            """
            SELECT id::text, action, created_at, actor_id::text, details FROM audit_events
            WHERE target_type = 'case' AND target_id = %s AND NOT is_deleted
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (case_id, limit, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT count(*) FROM audit_events WHERE target_type = 'case' AND target_id = %s AND NOT is_deleted",
            (case_id,),
        ).fetchone()[0]
    return [HistoryEntry(id=r[0], action=r[1], created_at=r[2], actor_id=r[3], details=r[4]) for r in rows], total
