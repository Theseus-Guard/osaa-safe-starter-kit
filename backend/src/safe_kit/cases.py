"""Case domain: reporting, access control, assignment, reviews, closure, deadline status, and change history.

Who may see a case is decided in SQL (CaseScope.visibility_sql), in the same transaction as every read or change.
A case the caller may not see is reported as not found, so its existence is not revealed.

Every change to a case writes its audit row in the same transaction. Each change first locks the case row,
so concurrent assignment, review, deadline, and closure changes run one after another.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo

from safe_kit import audit
from safe_kit import system_timezone
from safe_kit.db import connect
from safe_kit.idempotency import KeyScope, check, remember
from safe_kit.pagination import encode_cursor
from safe_kit.deadlines import (
    BusinessCalendar,
    deadline_start,
    deadlines_for,
    next_weekly_update,
)
from safe_kit.system_timezone import TimezoneNotConfigured

# The working calendar is provisional until the owning organization confirms its working days and holidays.
# The version string is stored on every case, so a later change of calendar does not silently move deadlines
# that were already computed. The local timezone is not part of the version: each case stores its own snapshot
# of the system timezone that was in effect when it was created.
CALENDAR_VERSION = "provisional-1"
CALENDAR_HOLIDAYS: frozenset = frozenset()

# A user may be assigned to a case only when its role holds the cases:assignable permission (role_permissions).
# The reporter role is refused in code as well, so the rule does not depend on how the matrix is edited.
ASSIGNABLE_PERMISSION = "cases:assignable"
REVIEW_OUTCOMES = frozenset({"pass", "fail", "insufficient_evidence", "not_applicable"})
DEADLINE_STATUSES = frozenset({"open", "done", "not_applicable"})
# The case status values the database stores (cases.status). The case list filters on them.
CaseStatus = Literal["open", "closed"]
CASE_STATUSES: tuple[str, ...] = get_args(CaseStatus)
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
    # The If-Match revision is not the case's current revision. Nothing changed.
    REVISION_MISMATCH = "revision_mismatch"
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
    NOT_MARKED_NOT_APPLICABLE = "not_marked_not_applicable"


REPORTER_CASE_SQL = "EXISTS (SELECT 1 FROM reporter_identities ri WHERE ri.case_id = cases.id AND NOT ri.is_deleted)"
ASSIGNED_CASE_SQL = "EXISTS (SELECT 1 FROM case_assignments ca WHERE ca.case_id = cases.id AND NOT ca.is_deleted)"


@dataclass(frozen=True)
class CaseScope:
    """What the caller may see, taken from the caller's permissions.

    read_own: the caller created the case. read_all: every case. read_assigned: the caller has an active assignment.
    can_assign: the caller holds cases:assign. With read_all, such a caller sees every case, including reporter-channel
    cases that have no assignment yet, so it can assign them (the triager). The reporter identity and the contact stay
    hidden from it: the contact needs reporter_contact:read.
    reporter_contact: the caller holds reporter_contact:read. With read_all, such a caller sees every case in the same
    way, so the reporting owner can assign reporter-channel cases too.
    """

    user_id: str
    read_all: bool = False
    read_own: bool = False
    read_assigned: bool = False
    can_assign: bool = False
    reporter_contact: bool = False

    @classmethod
    def from_permissions(cls, user_id: str, permissions: frozenset[str]) -> "CaseScope":
        return cls(
            user_id=user_id,
            read_all="cases:read_all" in permissions,
            read_own="cases:read_own" in permissions,
            read_assigned="cases:read_assigned" in permissions,
            can_assign="cases:assign" in permissions,
            reporter_contact="reporter_contact:read" in permissions,
        )

    def visibility_sql(self) -> tuple[str, tuple[Any, ...]]:
        """A condition on the cases table that holds exactly for the cases this scope may see.

        A case opened through the reporter channel (it has an active reporter identity) is hidden from a read_all
        caller until it has an active assignment, unless the caller holds cases:assign or reporter_contact:read. Those
        two see it before assignment. A read_all caller with neither (for example the disclosure approver) keeps the
        old rule.
        """
        if self.read_all:
            if self.reporter_contact or self.can_assign:
                return "TRUE", ()
            return f"(NOT {REPORTER_CASE_SQL} OR {ASSIGNED_CASE_SQL})", ()
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
    # Starts at 1 and rises by one with each change to the task. Only stored tasks have one (None for a preview).
    revision: int | None = None


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
    # The reason of the latest reopening, from case_status_changes. Only find_visible_case fills it in.
    reopen_reason: str | None = None
    # Starts at 1 and rises by one with each title change. Clients send it back in If-Match (see update_case_title).
    revision: int = 1
    # Only list_visible_cases fills these in. next_deadline is the earliest due_at of the open deadline tasks, or None
    # when none is open. updated_at is the time of the latest change recorded in the audit log (see _UPDATED_AT_SQL).
    next_deadline: datetime | None = None
    updated_at: datetime | None = None


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


# The five SAFE deadline tasks. Their due dates follow t0. weekly_update is not one of them.
SAFE_DEADLINE_KINDS = (
    "affected_customers",
    "preliminary_safe_report",
    "broader_advisory",
    "preliminary_public_report",
    "remediation_status",
)
# The 30-day analysis obligation (plans/01, t0 + 30 days): its own task, due at t0 + 30 days like the public report.
# It is not in SAFE_DEADLINE_KINDS, so the weekly update document keeps its five kinds.
CONTROL_FAILURE_ANALYSIS_KIND = "preliminary_control_failure_analysis"
# Every task whose due date follows t0. resync_deadlines moves these.
T0_DEADLINE_KINDS = (*SAFE_DEADLINE_KINDS, CONTROL_FAILURE_ANALYSIS_KIND)


# The 14-day broader advisory is conditional, and the case data has no field that records the condition. Until it
# does, the task starts not applicable with this reason. A triager sets it open once the condition holds.
BROADER_ADVISORY_CONDITION_REASON = "conditional advisory: no condition is recorded in the case data, so the advisory is not applicable yet"
# The reason a task holds only by its default, so that no person has decided it. A person who marks the task not
# applicable replaces it, and restoring the task puts it back.
DEFAULT_NOT_APPLICABLE_REASONS = {"broader_advisory": BROADER_ADVISORY_CONDITION_REASON}
# The weekly update is due while a significant risk is open: an impact area is recorded as high or critical.
WEEKLY_UPDATE_KIND = "weekly_update"
SIGNIFICANT_SEVERITIES = ("high", "critical")
WEEKLY_SIGNIFICANT_SOURCE = "significant_risk"
WEEKLY_NOT_SIGNIFICANT_REASON = (
    "no significant risk is open, so the weekly update is not due"
)


def deadline_tasks_for(t0: datetime, calendar: BusinessCalendar) -> list[DeadlineTask]:
    """The tasks created with a case. weekly_update is not created here: it depends on risks still being open."""
    due = deadlines_for(t0, calendar)
    return [
        DeadlineTask("affected_customers", due.affected_customers, "open"),
        DeadlineTask("preliminary_safe_report", due.preliminary_safe_report, "open"),
        DeadlineTask(
            "broader_advisory",
            due.broader_advisory,
            "not_applicable",
            not_applicable_reason=BROADER_ADVISORY_CONDITION_REASON,
        ),
        DeadlineTask(
            "preliminary_public_report", due.preliminary_public_report, "open"
        ),
        DeadlineTask(
            CONTROL_FAILURE_ANALYSIS_KIND,
            due.preliminary_control_failure_analysis,
            "open",
        ),
        DeadlineTask("remediation_status", due.remediation_status, "open"),
    ]


def resync_deadlines(conn, case_id: str) -> tuple[datetime, dict[str, dict[str, str]]]:
    """Recompute the due dates of the SAFE deadline tasks from the current t0. Statuses are not changed.

    Runs on the caller's transaction and returns t0 with the moved due dates, as {kind: {"old": iso, "new": iso}}.
    The caller writes the audit row, so the change and its record commit together.
    """
    awareness_time, calendar_timezone = conn.execute(
        "SELECT awareness_time, calendar_timezone FROM cases WHERE id = %s", (case_id,)
    ).fetchone()
    operator_times = [
        row[0]
        for row in conn.execute(
            "SELECT occurred_at FROM awareness_events "
            "WHERE case_id = %s AND awareness_kind = 'operator' AND NOT is_deleted",
            (case_id,),
        ).fetchall()
    ]
    t0 = deadline_start(awareness_time, operator_times)
    expected = {
        task.kind: task.due_at
        for task in deadline_tasks_for(t0, calendar_for(calendar_timezone))
    }
    moved: dict[str, dict[str, str]] = {}
    stored = conn.execute(
        "SELECT id::text, kind, due_at FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted AND kind = ANY(%s)",
        (case_id, list(T0_DEADLINE_KINDS)),
    ).fetchall()
    for task_id, kind, due_at in stored:
        if due_at != expected[kind]:
            conn.execute(
                "UPDATE deadline_tasks SET due_at = %s, revision = revision + 1 WHERE id = %s",
                (expected[kind], task_id),
            )
            moved[kind] = {"old": due_at.isoformat(), "new": expected[kind].isoformat()}
    return t0, moved


def closure_blockers(
    review_count: int, tasks: Iterable[tuple[str, str]], unknown_notices: int = 0
) -> list[str]:
    """Why a case cannot be closed yet. An empty list means it may be closed.

    A case needs at least one review, and every deadline task must be done or not applicable.
    An open weekly_update task is covered by the same rule, so it never closes silently.
    unknown_notices counts the notices whose latest state is receipt_unknown. A new unknown answer always comes with a
    follow-up (safe_kit.external_notices), so only a notice recorded before that rule can be counted here: it must get
    a follow-up before the case closes.
    """
    blockers = []
    if review_count < 1:
        blockers.append("no review is recorded")
    for kind, status in tasks:
        if status not in CLOSABLE_TASK_STATUSES:
            blockers.append(f"deadline task {kind} is {status}")
    if unknown_notices:
        blockers.append(
            f"{unknown_notices} external notice(s) have no receipt and no follow-up recorded"
        )
    return blockers


def redact_history(entries: Sequence[HistoryEntry]) -> list[HistoryEntry]:
    """For callers without full case access: keep the action and the timestamp, drop actor and details."""
    return [replace(entry, actor_id=None, details=None) for entry in entries]


def _text_marker(text: str | None) -> dict[str, int] | None:
    # Free text (reasons and notes) is never written to the audit log. Only its length is recorded.
    # Closure and reopening reasons are kept as text in case_status_changes instead.
    return None if text is None else {"length": len(text)}


def _record_status_change(
    conn, case_id: str, action: str, reason: str, actor_id: str
) -> None:
    """Store the reason of a closure or reopening. changed_at is the transaction time, as is the audit row's."""
    conn.execute(
        "INSERT INTO case_status_changes (case_id, action, reason, actor_id) VALUES (%s, %s, %s, %s)",
        (case_id, action, reason, actor_id),
    )


def _change(old: Any, new: Any) -> dict[str, Any]:
    return {"old": old, "new": new}


_CASE_COLUMNS = (
    "id::text, reference, title, awareness_time, status, calendar_version, created_at, created_by::text, "
    "calendar_timezone, closed_at, closed_by::text, close_reason, revision"
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
        revision=row[12],
    )


def _load_tasks(conn, case_id: str) -> list[DeadlineTask]:
    rows = conn.execute(
        """
        SELECT id::text, kind, due_at, status, not_applicable_reason, revision FROM deadline_tasks
        WHERE case_id = %s AND NOT is_deleted
        ORDER BY due_at, kind
        """,
        (case_id,),
    ).fetchall()
    return [
        DeadlineTask(
            id=row[0],
            kind=row[1],
            due_at=row[2],
            status=row[3],
            not_applicable_reason=row[4],
            revision=row[5],
        )
        for row in rows
    ]


def _lock_visible_case(conn, case_id: str, scope: CaseScope) -> tuple | None:
    """Lock the case row until the transaction ends. Returns (status, closed_at, closed_by, revision) or None if not visible."""
    visibility, params = scope.visibility_sql()
    return conn.execute(
        f"SELECT status, closed_at, closed_by::text, revision FROM cases "
        f"WHERE id = %s AND NOT is_deleted AND {visibility} FOR UPDATE",
        (case_id, *params),
    ).fetchone()


def is_visible(case_id: str, scope: CaseScope) -> bool:
    """True when the case exists, is not deleted, and the scope includes it.

    This is the one check that answers "may this caller see this case" for a single case. Routes outside this
    module (evidence_api) call it too, so the rule is not copied anywhere else.
    """
    with connect() as conn:
        return is_visible_on(conn, case_id, scope)


def is_visible_on(conn, case_id: str, scope: CaseScope) -> bool:
    """The same check as is_visible, on the caller's connection, so it shares the caller's transaction."""
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


def create_case(
    title: str,
    awareness_time: datetime,
    created_by: str,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Case], Any] | None = None,
) -> Case:
    """Create the case, its deadline tasks, and the audit row in one transaction.

    With an idempotency scope, a replay raises Replayed and a reused key raises KeyReused, and nothing is created.
    """
    with connect() as conn:
        check(conn, idempotency)
        case = insert_case(conn, title, awareness_time, created_by)
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 201, render(case))
        return case


def insert_case(conn, title: str, awareness_time: datetime, created_by: str) -> Case:
    """Write the case, its deadline tasks, and its audit row on the caller's connection.

    The caller owns the transaction, so it can add its own rows (for example an import record) to the same commit.
    The calendar timezone is the system timezone in effect inside this transaction. It is stored on the case,
    so a later change of the system timezone does not move deadlines that were already computed.
    """
    setting = system_timezone.read_current(conn)
    if setting is None:
        raise TimezoneNotConfigured()
    calendar_timezone = setting.timezone
    tasks = deadline_tasks_for(
        deadline_start(awareness_time, ()), calendar_for(calendar_timezone)
    )
    sequence = conn.execute("SELECT nextval('case_reference_seq')").fetchone()[0]
    reference = f"SAFE-{datetime.now(UTC).year}-{sequence:06d}"
    row = conn.execute(
        f"""
        INSERT INTO cases (reference, title, awareness_time, created_by, status, calendar_version, calendar_timezone)
        VALUES (%s, %s, %s, %s, 'open', %s, %s)
        RETURNING {_CASE_COLUMNS}
        """,
        (
            reference,
            title,
            awareness_time,
            created_by,
            CALENDAR_VERSION,
            calendar_timezone,
        ),
    ).fetchone()
    case = _to_case(row)
    stored_tasks = []
    for task in tasks:
        # A new task starts at revision 1 (the column default).
        task_id = conn.execute(
            "INSERT INTO deadline_tasks (case_id, kind, due_at, status, not_applicable_reason) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
            (case.id, task.kind, task.due_at, task.status, task.not_applicable_reason),
        ).fetchone()[0]
        stored_tasks.append(replace(task, id=task_id, revision=1))
    audit.record(
        conn,
        audit.CASE_CREATE,
        "case",
        case.id,
        actor_id=created_by,
        details={
            "calendar_version": CALENDAR_VERSION,
            "calendar_timezone": calendar_timezone,
            "not_applicable": [
                task.kind for task in tasks if task.status == "not_applicable"
            ],
        },
    )
    return replace(case, deadlines=stored_tasks)


# The SAFE deadline tasks that are external notices: to the affected customers, to SAFE, to customers in general, and
# to the public. Remediation status is tracked inside the organization and is not one of them. Each one is marked not
# applicable on its own, with a reason (update_deadline). No case-wide switch applies to all of them.
EXTERNAL_NOTICE_KINDS = (
    "affected_customers",
    "preliminary_safe_report",
    "broader_advisory",
    "preliminary_public_report",
)
# The source written in the audit details of a deadline change that restores a task from not applicable.
RESTORED_SOURCE = "restore"


def _previous_status(conn, case_id: str, task_id: str) -> str:
    """The status a not applicable task held before it became not applicable.

    The latest status change of the task is the one that made it not applicable, so its old status is the answer. A
    task with no status change was created not applicable (the conditional advisory), so it returns to that. Audit rows
    are never removed, so this reads the record of the task's changes.
    """
    row = conn.execute(
        """
        SELECT details->'status'->>'old' FROM audit_events
        WHERE target_type = 'case' AND target_id = %s AND action = %s AND details->>'task_id' = %s
          AND details->'status' IS NOT NULL AND NOT is_deleted
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (case_id, audit.CASE_DEADLINE_UPDATE, task_id),
    ).fetchone()
    if row is None or row[0] is None:
        return "not_applicable"
    return row[0]


def restore_deadline(
    case_id: str,
    task_id: str,
    scope: CaseScope,
    may_mark_not_applicable: bool,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Case], Any] | None = None,
) -> Outcome:
    """Turn off the not applicable exception of one deadline task: it goes back to the status it had before.

    The task's due date does not change. The reason goes too, except for a task that was created not applicable by its
    default reason. Only a triager may change a task that is not applicable. Writes one deadline audit row.
    With an idempotency scope the key is checked first and the response is stored in the same transaction.
    """
    with connect() as conn:
        check(conn, idempotency)
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
        kind, status, reason = task
        if status != "not_applicable":
            return Outcome.NOT_MARKED_NOT_APPLICABLE
        if not may_mark_not_applicable:
            return Outcome.NOT_APPLICABLE_FORBIDDEN
        previous = _previous_status(conn, case_id, task_id)
        previous_reason = (
            DEFAULT_NOT_APPLICABLE_REASONS.get(kind)
            if previous == "not_applicable"
            else None
        )
        _write_task_change(
            conn,
            case_id,
            task_id,
            kind,
            status,
            previous,
            reason,
            previous_reason,
            RESTORED_SOURCE,
            scope.user_id,
        )
        _remember_case(conn, idempotency, render, case_id)
    return Outcome.OK


def _write_task_change(
    conn,
    case_id: str,
    task_id: str,
    kind: str,
    old_status: str,
    new_status: str,
    old_reason: str | None,
    new_reason: str | None,
    source: str,
    actor_id: str,
    *,
    old_due: datetime | None = None,
    new_due: datetime | None = None,
) -> None:
    """Write one deadline change and its audit row. A new_due that is given also moves the due date.

    The task revision rises by one only when a field really changes.
    """
    changes: dict[str, Any] = {}
    if old_status != new_status:
        changes["status"] = _change(old_status, new_status)
    if old_reason != new_reason:
        changes["not_applicable_reason"] = _change(
            _text_marker(old_reason), _text_marker(new_reason)
        )
    if new_due is not None and new_due != old_due:
        changes["due_at"] = _change(
            old_due.isoformat() if old_due else None, new_due.isoformat()
        )
    conn.execute(
        "UPDATE deadline_tasks SET status = %s, not_applicable_reason = %s, due_at = COALESCE(%s, due_at), "
        "revision = revision + %s WHERE id = %s",
        (new_status, new_reason, new_due, int(bool(changes)), task_id),
    )
    audit.record(
        conn,
        audit.CASE_DEADLINE_UPDATE,
        "case",
        case_id,
        actor_id=actor_id,
        details={"task_id": task_id, "kind": kind, "source": source, **changes},
    )


def sync_weekly_update(conn, case_id: str, actor_id: str) -> None:
    """Keep the weekly_update task in step with the significant risks of the case. Runs on the caller's transaction.

    A significant risk is an impact area recorded as high or critical. While one is open, the weekly update task
    is open and due 7 elapsed days after the moment the risk was recorded. The machine-readable update record that
    moves the due date later belongs to another package. When no significant risk is open, the task that this rule
    opened becomes not applicable with a reason. A task that a person has marked done, or not applicable with a reason
    of their own, is left as it is.
    Each change writes one deadline audit row. The row is never deleted and the task is never removed.
    """
    significant = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM impacts WHERE case_id = %s AND NOT is_deleted AND severity = ANY(%s))",
        (case_id, list(SIGNIFICANT_SEVERITIES)),
    ).fetchone()[0]
    row = conn.execute(
        """
        SELECT id::text, status, not_applicable_reason, due_at FROM deadline_tasks
        WHERE case_id = %s AND kind = %s AND NOT is_deleted
        FOR UPDATE
        """,
        (case_id, WEEKLY_UPDATE_KIND),
    ).fetchone()
    now = datetime.now(UTC)
    if significant:
        if row is None:
            due = next_weekly_update(now)
            task_id = conn.execute(
                "INSERT INTO deadline_tasks (case_id, kind, due_at, status) VALUES (%s, %s, %s, 'open') RETURNING id::text",
                (case_id, WEEKLY_UPDATE_KIND, due),
            ).fetchone()[0]
            audit.record(
                conn,
                audit.CASE_DEADLINE_UPDATE,
                "case",
                case_id,
                actor_id=actor_id,
                details={
                    "task_id": task_id,
                    "kind": WEEKLY_UPDATE_KIND,
                    "source": WEEKLY_SIGNIFICANT_SOURCE,
                    "status": _change(None, "open"),
                    "due_at": _change(None, due.isoformat()),
                },
            )
        elif row[1] == "not_applicable" and row[2] == WEEKLY_NOT_SIGNIFICANT_REASON:
            task_id, _, _, old_due = row
            due = next_weekly_update(now)
            _write_task_change(
                conn,
                case_id,
                task_id,
                WEEKLY_UPDATE_KIND,
                "not_applicable",
                "open",
                row[2],
                None,
                WEEKLY_SIGNIFICANT_SOURCE,
                actor_id,
                old_due=old_due,
                new_due=due,
            )
    elif row is not None and row[1] == "open":
        task_id = row[0]
        _write_task_change(
            conn,
            case_id,
            task_id,
            WEEKLY_UPDATE_KIND,
            "open",
            "not_applicable",
            None,
            WEEKLY_NOT_SIGNIFICANT_REASON,
            WEEKLY_SIGNIFICANT_SOURCE,
            actor_id,
        )


_LATEST_REOPEN_REASON = (
    "(SELECT csc.reason FROM case_status_changes csc WHERE csc.case_id = cases.id "
    "AND csc.action = 'reopened' AND NOT csc.is_deleted ORDER BY csc.changed_at DESC, csc.id DESC LIMIT 1)"
)


def _load_case(conn, case_id: str) -> Case:
    """The case with its deadline tasks, read on the caller's connection so a response can be stored in its transaction.

    Callers hold the case visible already (the row is locked), so no visibility filter is applied here.
    """
    row = conn.execute(
        f"SELECT {_CASE_COLUMNS}, {_LATEST_REOPEN_REASON} FROM cases WHERE id = %s AND NOT is_deleted",
        (case_id,),
    ).fetchone()
    return replace(
        _to_case(row[:-1]), deadlines=_load_tasks(conn, case_id), reopen_reason=row[-1]
    )


def _remember_case(
    conn,
    idempotency: KeyScope | None,
    render: Callable[[Case], Any] | None,
    case_id: str,
) -> None:
    """Store the case response of a successful request under its key, in the same transaction as the change."""
    if idempotency is not None:
        assert render is not None
        remember(conn, idempotency, 200, render(_load_case(conn, case_id)))


def find_visible_case(case_id: str, scope: CaseScope) -> Case | None:
    """Return the case with its deadline tasks if the scope may see it. Otherwise None, the same as a missing case."""
    visibility, params = scope.visibility_sql()
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_CASE_COLUMNS}, {_LATEST_REOPEN_REASON} FROM cases "
            f"WHERE id = %s AND NOT is_deleted AND {visibility}",
            (case_id, *params),
        ).fetchone()
        if row is None:
            return None
        tasks = _load_tasks(conn, case_id)
    return replace(_to_case(row[:-1]), deadlines=tasks, reopen_reason=row[-1])


def _search_sql(q: str | None) -> tuple[str, tuple[Any, ...]]:
    """A case matches q when its reference contains q, or its id starts with q (both case-insensitive).

    The LIKE wildcards in q are escaped, so q is always matched as plain text.
    """
    if not q:
        return "TRUE", ()
    escaped = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return (
        "(cases.reference ILIKE %s ESCAPE '!' OR cases.id::text ILIKE %s ESCAPE '!')",
        (f"%{escaped}%", f"{escaped}%"),
    )


def _status_sql(status: str | None) -> tuple[str, tuple[Any, ...]]:
    if status is None:
        return "TRUE", ()
    return "cases.status = %s", (status,)


# The earliest due date of the case's open deadline tasks. Deleted tasks, done tasks and not-applicable tasks are
# left out. The value is computed on every read and never stored.
_NEXT_DEADLINE_SQL = (
    "(SELECT min(dt.due_at) FROM deadline_tasks dt "
    "WHERE dt.case_id = cases.id AND dt.status = 'open' AND NOT dt.is_deleted)"
)

# The latest audit row that changed the case: a row on the case itself, and when include_evidence is set (the caller
# holds evidence:read), a row on one of its evidence items. Read-only rows are left out (audit.READ_ONLY_AUDIT_ACTIONS).
# The case row's own updated_at is not used, because only closure and reopening write to the cases table. A case with
# no such row falls back to created_at. The three placeholders are the read-only actions, include_evidence, and the
# read-only actions again. See _updated_at_params.
_UPDATED_AT_SQL = """
    COALESCE(GREATEST(
        (SELECT max(ae.created_at) FROM audit_events ae
         WHERE ae.target_type = 'case' AND ae.target_id = cases.id::text AND NOT ae.is_deleted
           AND ae.action <> ALL(%s::text[])),
        CASE WHEN %s THEN (
            SELECT max(ae.created_at) FROM audit_events ae
            WHERE ae.target_type = 'evidence' AND ae.details->>'case_id' = cases.id::text AND NOT ae.is_deleted
              AND ae.action <> ALL(%s::text[])
        ) END
    ), cases.created_at)
"""


def _updated_at_params(include_evidence: bool) -> tuple[Any, ...]:
    read_only = sorted(audit.READ_ONLY_AUDIT_ACTIONS)
    return (read_only, include_evidence, read_only)


def list_visible_cases(
    scope: CaseScope,
    limit: int,
    after: tuple[datetime, str] | None = None,
    q: str | None = None,
    status: str | None = None,
    include_evidence: bool = False,
) -> list[Case]:
    # after is the (created_at, id) position of the last row of the previous page. Rows come newest first.
    visibility, params = scope.visibility_sql()
    search, search_params = _search_sql(q)
    status_clause, status_params = _status_sql(status)
    keyset, keyset_params = _keyset_sql(after)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_CASE_COLUMNS}, {_NEXT_DEADLINE_SQL}, {_UPDATED_AT_SQL} FROM cases
            WHERE NOT is_deleted AND {visibility} AND {search} AND {status_clause} AND {keyset}
            ORDER BY created_at DESC, id
            LIMIT %s
            """,
            (
                *_updated_at_params(include_evidence),
                *params,
                *search_params,
                *status_params,
                *keyset_params,
                limit,
            ),
        ).fetchall()
    return [
        replace(_to_case(row[:13]), next_deadline=row[13], updated_at=row[14])
        for row in rows
    ]


def _keyset_sql(after: tuple[datetime, str] | None) -> tuple[str, tuple[object, ...]]:
    # Matches the ORDER BY: created_at descending, then id ascending.
    if after is None:
        return "TRUE", ()
    created_at, row_id = after
    return "(created_at < %s OR (created_at = %s AND id > %s))", (
        created_at,
        created_at,
        row_id,
    )


def list_assignments(case_id: str, scope: CaseScope) -> list[Assignment] | None:
    """Active assignments of a case, or None when the scope may not see the case."""
    with connect() as conn:
        if not is_visible_on(conn, case_id, scope):
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


@dataclass(frozen=True)
class AssignableUser:
    id: str
    username: str
    first_name: str | None
    last_name: str | None
    role: str


def list_assignable_users(
    case_id: str, scope: CaseScope
) -> list[AssignableUser] | None:
    """Users the caller may pick for an assignment on the case, or None when the scope may not see the case.

    A user is listed only when the account is active and not deleted and its role currently holds cases:assignable
    in role_permissions. That is the rule assign_member enforces, so every listed user can be assigned. The reporter
    role is excluded explicitly: reporters are never assignees, even if the matrix grants them cases:assignable.
    Read-only: it writes no audit row.
    """
    with connect() as conn:
        if not is_visible_on(conn, case_id, scope):
            return None
        rows = conn.execute(
            """
            SELECT u.id::text, u.username, u.first_name, u.last_name, u.role
            FROM users u
            WHERE NOT u.is_deleted AND u.is_active
              -- Explicit guard: reporters are never assignees, whatever the permission matrix says.
              AND u.role <> 'reporter'
              AND EXISTS (
                  SELECT 1 FROM role_permissions rp
                  JOIN permissions p ON p.name = rp.permission AND NOT p.is_deleted
                  WHERE rp.role = u.role AND rp.permission = %s AND NOT rp.is_deleted
              )
            ORDER BY u.username
            """,
            (ASSIGNABLE_PERMISSION,),
        ).fetchall()
    return [AssignableUser(*row) for row in rows]


def _role_holds_assignable(conn, role: str) -> bool:
    """Whether the role currently holds cases:assignable. Runs on the caller's connection, so it is in the same transaction."""
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM role_permissions rp
            JOIN permissions p ON p.name = rp.permission AND NOT p.is_deleted
            WHERE rp.role = %s AND rp.permission = %s AND NOT rp.is_deleted
        )
        """,
        (role, ASSIGNABLE_PERMISSION),
    ).fetchone()
    return bool(row[0])


def assign_member(
    case_id: str,
    user_id: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Assignment], Any] | None = None,
) -> tuple[Outcome, Assignment | None]:
    """Give an active user whose role holds cases:assignable an assignment on the case.

    With an idempotency scope the key is checked first, and the stored response is render(assignment).
    """
    with connect() as conn:
        check(conn, idempotency)
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
        # Reporters are never assignees, whatever the permission matrix says (same guard as list_assignable_users).
        if user[1] == "reporter" or not _role_holds_assignable(conn, user[1]):
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
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 201, render(assignment))
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


def update_deadline(
    case_id: str,
    task_id: str,
    status: str,
    reason: str | None,
    scope: CaseScope,
    may_mark_not_applicable: bool,
    expected_revision: int,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Case], Any] | None = None,
) -> tuple[Outcome, int | None]:
    """Set a deadline task to done, open, or not applicable. Returns the outcome and the task revision.

    expected_revision is the task revision the caller read. A different revision is refused with REVISION_MISMATCH
    and nothing changes. Changing a task to or from not applicable needs the triager permission
    (may_mark_not_applicable). A request that changes nothing writes no audit row and keeps the revision. The revision
    rises by one with a change, in the same transaction. With an idempotency scope, a replay raises Replayed and a
    reused key raises KeyReused; a successful request stores render(case) in the same transaction.
    """
    new_reason = reason if status == "not_applicable" else None
    with connect() as conn:
        check(conn, idempotency)
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Outcome.NOT_FOUND, None
        if locked[0] == "closed":
            return Outcome.CLOSED, None
        task = conn.execute(
            """
            SELECT kind, status, not_applicable_reason, revision FROM deadline_tasks
            WHERE id = %s AND case_id = %s AND NOT is_deleted
            FOR UPDATE
            """,
            (task_id, case_id),
        ).fetchone()
        if task is None:
            return Outcome.TASK_NOT_FOUND, None
        kind, old_status, old_reason, revision = task
        if "not_applicable" in (status, old_status) and not may_mark_not_applicable:
            return Outcome.NOT_APPLICABLE_FORBIDDEN, revision
        if revision != expected_revision:
            return Outcome.REVISION_MISMATCH, revision
        changes: dict[str, Any] = {}
        if status != old_status:
            changes["status"] = _change(old_status, status)
        if new_reason != old_reason:
            changes["not_applicable_reason"] = _change(
                _text_marker(old_reason), _text_marker(new_reason)
            )
        if changes:
            conn.execute(
                "UPDATE deadline_tasks SET status = %s, not_applicable_reason = %s, revision = revision + 1 "
                "WHERE id = %s",
                (status, new_reason, task_id),
            )
            revision += 1
            audit.record(
                conn,
                audit.CASE_DEADLINE_UPDATE,
                "case",
                case_id,
                actor_id=scope.user_id,
                details={"task_id": task_id, "kind": kind, **changes},
            )
        _remember_case(conn, idempotency, render, case_id)
    return Outcome.OK, revision


def update_case_title(
    case_id: str,
    title: str,
    expected_revision: int,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Case], Any] | None = None,
) -> tuple[Outcome, int]:
    """Change the title when expected_revision is the case's current revision. Returns the outcome and the revision.

    A title that is already set writes nothing and keeps the revision. A change raises the revision by one and writes an
    audit row that holds the new revision but not the title text, because the audit log is append-only. The row lock
    makes the revision check and the change one step, so two writers with the same revision cannot both succeed.
    """
    with connect() as conn:
        check(conn, idempotency)
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Outcome.NOT_FOUND, 0
        status, _, _, revision = locked
        if status == "closed":
            return Outcome.CLOSED, revision
        if revision != expected_revision:
            return Outcome.REVISION_MISMATCH, revision
        (old_title,) = conn.execute(
            "SELECT title FROM cases WHERE id = %s", (case_id,)
        ).fetchone()
        if title != old_title:
            revision += 1
            conn.execute(
                "UPDATE cases SET title = %s, revision = %s WHERE id = %s",
                (title, revision, case_id),
            )
            audit.record(
                conn,
                audit.CASE_TITLE_UPDATE,
                "case",
                case_id,
                actor_id=scope.user_id,
                details={"revision": revision},
            )
        _remember_case(conn, idempotency, render, case_id)
    return Outcome.OK, revision


def close_case(
    case_id: str,
    reason: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Case], Any] | None = None,
) -> tuple[Outcome, list[str]]:
    """Close the case if the closure rules hold. Otherwise return the blockers and change nothing.

    With an idempotency scope the key is checked first, and a successful close stores render(case) in its transaction.
    """
    with connect() as conn:
        check(conn, idempotency)
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Outcome.NOT_FOUND, []
        if locked[0] == "closed":
            return Outcome.CLOSED, []
        # A recorded layer outcome of any review round of the case (migrated F1 reviews included).
        review_count = conn.execute(
            """
            SELECT count(*) FROM review_layers rl
            JOIN review_rounds rr ON rr.id = rl.round_id
            WHERE rr.case_id = %s AND NOT rr.is_deleted AND NOT rl.is_deleted AND rl.outcome IS NOT NULL
            """,
            (case_id,),
        ).fetchone()[0]
        tasks = conn.execute(
            "SELECT kind, status FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted ORDER BY due_at, kind",
            (case_id,),
        ).fetchall()
        # A notice whose latest revision is receipt_unknown has no follow-up (only older notices can be in this state).
        unknown_notices = conn.execute(
            """
            SELECT count(*) FROM (
                SELECT DISTINCT ON (notice_id) state
                FROM external_notices
                WHERE case_id = %s AND NOT is_deleted
                ORDER BY notice_id, revision DESC
            ) latest
            WHERE latest.state = 'receipt_unknown'
            """,
            (case_id,),
        ).fetchone()[0]
        blockers = closure_blockers(review_count, tasks, unknown_notices)
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
        _record_status_change(conn, case_id, "closed", reason, scope.user_id)
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
        _remember_case(conn, idempotency, render, case_id)
    return Outcome.OK, []


def reopen_case(
    case_id: str,
    reason: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Case], Any] | None = None,
) -> Outcome:
    """Reopen a closed case. The reason is stored as text in case_status_changes; the audit row keeps only its length.

    The previous closure stays in the audit log. With an idempotency scope the key is checked first.
    """
    with connect() as conn:
        check(conn, idempotency)
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Outcome.NOT_FOUND
        status, closed_at, closed_by, _ = locked
        if status != "closed":
            return Outcome.NOT_CLOSED
        conn.execute(
            "UPDATE cases SET status = 'open', closed_at = NULL, closed_by = NULL WHERE id = %s",
            (case_id,),
        )
        _record_status_change(conn, case_id, "reopened", reason, scope.user_id)
        audit.record(
            conn,
            audit.CASE_REOPEN,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "status": _change("closed", "open"),
                "closed_at": _change(
                    closed_at.isoformat() if closed_at else None, None
                ),
                "closed_by": _change(closed_by, None),
                "reopen_reason": _change(None, _text_marker(reason)),
            },
        )
        _remember_case(conn, idempotency, render, case_id)
    return Outcome.OK


def case_history(
    case_id: str,
    scope: CaseScope,
    limit: int,
    after: tuple[datetime, str] | None = None,
    *,
    include_evidence: bool = False,
) -> tuple[list[HistoryEntry], str | None] | None:
    """One page of audit rows of the case, newest first, or None when the scope may not see the case.

    after is the (created_at, id) position of the last row of the previous page. The second item of the result is
    the cursor of the next page, or None on the last page.
    A closure or reopening gets its reason from case_status_changes, under the key "reason" in details.
    Evidence actions are left out unless include_evidence is set (the caller holds evidence:read).
    Details are not redacted here: the endpoint decides who sees them.
    """
    # The reason row has the same case, actor, and transaction time as its audit row, because both are written
    # in one transaction and take now() as their timestamp.
    history_join = """
        FROM audit_events ae
        LEFT JOIN case_status_changes csc
            ON csc.case_id::text = ae.target_id
            AND csc.changed_at = ae.created_at
            AND csc.actor_id = ae.actor_id
            AND csc.action = CASE ae.action WHEN %s THEN 'closed' WHEN %s THEN 'reopened' END
            AND NOT csc.is_deleted
        WHERE ((ae.target_type = 'case' AND ae.target_id = %s)
            OR (ae.target_type = 'evidence' AND ae.details->>'case_id' = %s))
            AND NOT ae.is_deleted
            AND (%s OR ae.action <> ALL(%s::text[]))
    """
    evidence_filter = (include_evidence, sorted(audit.EVIDENCE_AUDIT_ACTIONS))
    # Matches the ORDER BY: created_at descending, then id descending.
    if after is None:
        keyset, keyset_params = "TRUE", ()
    else:
        created_at, row_id = after
        keyset = "(ae.created_at < %s OR (ae.created_at = %s AND ae.id < %s::uuid))"
        keyset_params = (created_at, created_at, row_id)
    with connect() as conn:
        if not is_visible_on(conn, case_id, scope):
            return None
        # One extra row tells whether another page exists; it is not returned.
        rows = conn.execute(
            f"SELECT ae.id::text, ae.action, ae.created_at, ae.actor_id::text, ae.details, csc.reason {history_join}"
            f" AND {keyset} ORDER BY ae.created_at DESC, ae.id DESC LIMIT %s",
            (
                audit.CASE_CLOSE,
                audit.CASE_REOPEN,
                case_id,
                case_id,
                *evidence_filter,
                *keyset_params,
                limit + 1,
            ),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1][2], rows[-1][0]) if has_more else None
    entries = []
    for row in rows:
        details = row[4]
        if row[5] is not None:
            details = {**(details or {}), "reason": row[5]}
        entries.append(
            HistoryEntry(
                id=row[0],
                action=row[1],
                created_at=row[2],
                actor_id=row[3],
                details=details,
            )
        )
    return entries, next_cursor
