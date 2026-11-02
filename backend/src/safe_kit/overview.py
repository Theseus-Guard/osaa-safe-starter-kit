"""Overview dashboard queries (F2). The screen and the role matrix are in plans/f1/overview-dashboard.md.

Every function returns counts and dates only. No query selects a username, an IP address, a case title or any free
text, so no response built from these functions can carry them. Days are grouped on the server in the system timezone
(safe_kit.system_timezone). Every read filters NOT is_deleted. Range reads use the date indexes of migration 0031.

The gates below are the single place where the role matrix becomes code. The HTTP layer (overview_api.py) checks
them before it calls a query.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from safe_kit.db import connect
from safe_kit.system_timezone import current_timezone_name
from safe_kit.users import ROLES

RANGE_DAYS = (7, 30, 90)
DEFAULT_RANGE_DAYS = 30
TIMESERIES_METRICS = (
    "cases_created",
    "audit_events",
    "logins_success",
    "logins_failed",
    "outbox_completed",
    "outbox_failed",
)
AUDIT_TOP_ACTIONS = 5
# The bucket for every audit action beyond the top five. Charts show it as "Other".
OTHER_BUCKET = "other"
DEADLINE_KINDS = (
    "affected_customers",
    "preliminary_safe_report",
    "broader_advisory",
    "preliminary_public_report",
    "remediation_status",
    "weekly_update",
)
DEADLINE_HORIZON = timedelta(hours=72)
OUTBOX_STATUSES = ("pending", "leased", "done", "failed")
# The worker writes a heartbeat every 10 seconds (safe_kit.worker.HEARTBEAT_SECONDS). A heartbeat older than this is stale.
WORKER_STALE_AFTER = timedelta(seconds=60)
# Permissions that make a case "own" for the deadline and case counts: cases the caller created or is assigned to.
OWN_CASE_PERMISSIONS = frozenset({"cases:read_own", "cases:read_assigned"})
# The plan's matrix gives admin the audit and ops sections too, and the default admin role does not hold audit:read,
# so the admin role name is accepted there as well. The admin role holds no case permission (safe_kit.rbac), so these
# counts never reach case content.
ADMIN_ROLE = "admin"


class SystemTimezoneNotSet(Exception):
    """Day grouping needs the system timezone, which an administrator sets once."""


# Gates. Each one mirrors a row of the plan's role matrix.


def sees_own_cases(permissions: frozenset[str]) -> bool:
    """Reporter, triager, investigator and reviewer: the caller's own case and deadline counts."""
    return not OWN_CASE_PERMISSIONS.isdisjoint(permissions)


def sees_case_activity(role: str, permissions: frozenset[str]) -> bool:
    """Auditor and admin: anonymous case counts, audit actions, outbox and worker health."""
    return "audit:read" in permissions or role == ADMIN_ROLE


def sees_users(permissions: frozenset[str]) -> bool:
    """Admin only: the user distribution and the pending password changes."""
    return "users:manage" in permissions


def sees_logins(permissions: frozenset[str]) -> bool:
    """Admin only: login successes and failures."""
    return "users:manage" in permissions


def system_timezone() -> str:
    name = current_timezone_name()
    if name is None:
        raise SystemTimezoneNotSet
    return name


@dataclass(frozen=True)
class Window:
    """The days shown: the last `days` local days, the first one starting at local midnight."""

    timezone: str
    days: int
    start: datetime
    dates: tuple[date, ...]


def make_window(timezone: str, days: int, now: datetime) -> Window:
    zone = ZoneInfo(timezone)
    first = now.astimezone(zone).date() - timedelta(days=days - 1)
    return Window(
        timezone=timezone,
        days=days,
        start=datetime.combine(first, time.min, tzinfo=zone),
        dates=tuple(first + timedelta(days=offset) for offset in range(days)),
    )


# Daily series. Each query returns (local day, count) rows; days without rows are filled with zero in Python.
# Outbox rows have no completion time: updated_at is the last change, which for done and failed jobs is the time they
# reached that state. The series is therefore approximate for those two metrics.
_DAILY_SQL: dict[str, str] = {
    "cases_created": """
        SELECT (created_at AT TIME ZONE %(tz)s)::date AS day, count(*)
        FROM cases WHERE NOT is_deleted AND created_at >= %(start)s
        GROUP BY 1
    """,
    "audit_events": """
        SELECT (created_at AT TIME ZONE %(tz)s)::date AS day, count(*)
        FROM audit_events WHERE NOT is_deleted AND created_at >= %(start)s
        GROUP BY 1
    """,
    "logins_failed": """
        SELECT (created_at AT TIME ZONE %(tz)s)::date AS day, count(*)
        FROM audit_events
        WHERE NOT is_deleted AND action = 'auth.login_failed' AND created_at >= %(start)s
        GROUP BY 1
    """,
    # Login creates a session family (open_session); every token rotation inserts a row into the same family. Only the
    # first row of a family is a login, so rotations are not counted.
    "logins_success": """
        SELECT (r.created_at AT TIME ZONE %(tz)s)::date AS day, count(*)
        FROM refresh_tokens r
        WHERE NOT r.is_deleted AND r.created_at >= %(start)s
          AND NOT EXISTS (
            SELECT 1 FROM refresh_tokens p
            WHERE p.family_id = r.family_id AND p.created_at < r.created_at
          )
        GROUP BY 1
    """,
    "outbox_completed": """
        SELECT (updated_at AT TIME ZONE %(tz)s)::date AS day, count(*)
        FROM outbox_jobs WHERE NOT is_deleted AND status = 'done' AND updated_at >= %(start)s
        GROUP BY 1
    """,
    "outbox_failed": """
        SELECT (updated_at AT TIME ZONE %(tz)s)::date AS day, count(*)
        FROM outbox_jobs WHERE NOT is_deleted AND status = 'failed' AND updated_at >= %(start)s
        GROUP BY 1
    """,
}

# Own cases: created by the caller, or with an active assignment to the caller. Expects the cases table as alias c.
_OWN_CASE = """
    (c.created_by = %(user_id)s OR EXISTS (
        SELECT 1 FROM case_assignments a
        WHERE a.case_id = c.id AND a.user_id = %(user_id)s AND NOT a.is_deleted
    ))
"""


def timeseries(metric: str, window: Window) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(_DAILY_SQL[metric], {"tz": window.timezone, "start": window.start}).fetchall()
    counts = {day: int(total) for day, total in rows}
    return [{"date": day.isoformat(), "count": counts.get(day, 0)} for day in window.dates]


def _case_counts(conn, start: datetime) -> dict[str, int]:
    open_cases, opened = conn.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'open'), count(*) FILTER (WHERE created_at >= %(start)s)
        FROM cases WHERE NOT is_deleted
        """,
        {"start": start},
    ).fetchone()
    return {"open_cases": int(open_cases), "opened_in_period": int(opened)}


def _own_case_counts(conn, user_id: str, start: datetime, now: datetime) -> dict[str, int]:
    open_cases, opened = conn.execute(
        f"""
        SELECT count(*) FILTER (WHERE c.status = 'open'), count(*) FILTER (WHERE c.created_at >= %(start)s)
        FROM cases c WHERE NOT c.is_deleted AND {_OWN_CASE}
        """,
        {"user_id": user_id, "start": start},
    ).fetchone()
    due_soon, overdue = conn.execute(
        f"""
        SELECT count(*) FILTER (WHERE d.due_at >= %(now)s AND d.due_at < %(horizon)s),
               count(*) FILTER (WHERE d.due_at < %(now)s)
        FROM deadline_tasks d JOIN cases c ON c.id = d.case_id
        WHERE NOT d.is_deleted AND NOT c.is_deleted AND c.status = 'open' AND d.status = 'open' AND {_OWN_CASE}
        """,
        {"user_id": user_id, "now": now, "horizon": now + DEADLINE_HORIZON},
    ).fetchone()
    return {
        "open_cases": int(open_cases),
        "opened_in_period": int(opened),
        "deadlines_due_72h": int(due_soon),
        "deadlines_overdue": int(overdue),
    }


def _worker_status(conn, now: datetime) -> dict[str, Any]:
    beat_at = conn.execute("SELECT max(beat_at) FROM worker_heartbeat WHERE NOT is_deleted").fetchone()[0]
    if beat_at is None:
        return {"status": "no_data", "heartbeat_age_seconds": None}
    age = max(now - beat_at, timedelta(0))
    status = "stale" if age > WORKER_STALE_AFTER else "ok"
    return {"status": status, "heartbeat_age_seconds": int(age.total_seconds())}


def _failed_outbox_jobs(conn) -> int:
    return int(
        conn.execute("SELECT count(*) FROM outbox_jobs WHERE NOT is_deleted AND status = 'failed'").fetchone()[0]
    )


def _pending_password_changes(conn) -> int:
    return int(
        conn.execute(
            "SELECT count(*) FROM users WHERE NOT is_deleted AND is_active AND must_change_password"
        ).fetchone()[0]
    )


def summary(user_id: str, role: str, permissions: frozenset[str], window: Window, now: datetime) -> dict[str, Any]:
    """Stat cards. A section the caller may not see is None, so the response never carries a hidden count."""
    result: dict[str, Any] = {"own_cases": None, "case_activity": None, "users": None, "ops": None}
    with connect() as conn:
        if sees_own_cases(permissions):
            result["own_cases"] = _own_case_counts(conn, user_id, window.start, now)
        if sees_case_activity(role, permissions):
            result["case_activity"] = _case_counts(conn, window.start)
            result["ops"] = {
                "failed_outbox_jobs": _failed_outbox_jobs(conn),
                **_worker_status(conn, now),
            }
        if sees_users(permissions):
            result["users"] = {"pending_password_change": _pending_password_changes(conn)}
    return result


def deadline_summary(user_id: str, now: datetime) -> list[dict[str, Any]]:
    """Per deadline kind, for the caller's own cases. Open and overdue count only open cases."""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT d.kind,
                   count(*) FILTER (WHERE c.status = 'open' AND d.status = 'open' AND d.due_at >= %(now)s),
                   count(*) FILTER (WHERE c.status = 'open' AND d.status = 'open' AND d.due_at < %(now)s),
                   count(*) FILTER (WHERE d.status = 'done'),
                   count(*) FILTER (WHERE d.status = 'not_applicable')
            FROM deadline_tasks d JOIN cases c ON c.id = d.case_id
            WHERE NOT d.is_deleted AND NOT c.is_deleted AND {_OWN_CASE}
            GROUP BY d.kind
            """,
            {"user_id": user_id, "now": now},
        ).fetchall()
    by_kind = {row[0]: tuple(int(value) for value in row[1:]) for row in rows}
    items = []
    for kind in DEADLINE_KINDS:
        open_, overdue, done, not_applicable = by_kind.get(kind, (0, 0, 0, 0))
        items.append(
            {"kind": kind, "open": open_, "overdue": overdue, "done": done, "not_applicable": not_applicable}
        )
    return items


def audit_summary(window: Window) -> dict[str, Any]:
    """Per day and action. Only the top five actions by total are named; the rest are summed into the "other" bucket."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT (created_at AT TIME ZONE %(tz)s)::date AS day, action, count(*)
            FROM audit_events WHERE NOT is_deleted AND created_at >= %(start)s
            GROUP BY 1, 2
            """,
            {"tz": window.timezone, "start": window.start},
        ).fetchall()
    return bucket_audit_rows(rows, window)


def bucket_audit_rows(rows: Iterable[tuple[date, str, int]], window: Window) -> dict[str, Any]:
    """Pure part of audit_summary: (day, action, count) rows into the top actions and the "other" bucket."""
    days = set(window.dates)
    inside = [(day, action, int(total)) for day, action, total in rows if day in days]
    totals: Counter[str] = Counter()
    for _, action, total in inside:
        totals[action] += total
    top = sorted(totals, key=lambda action: (-totals[action], action))[:AUDIT_TOP_ACTIONS]
    actions = [*top, OTHER_BUCKET] if len(totals) > len(top) else list(top)
    per_day: dict[date, dict[str, int]] = {day: dict.fromkeys(actions, 0) for day in window.dates}
    for day, action, total in inside:
        per_day[day][action if action in top else OTHER_BUCKET] += total
    return {
        "actions": actions,
        "points": [{"date": day.isoformat(), "counts": per_day[day]} for day in window.dates],
    }


def users_summary() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, is_active, count(*) FROM users WHERE NOT is_deleted GROUP BY role, is_active"
        ).fetchall()
        pending = _pending_password_changes(conn)
    counts = {role: {"active": 0, "inactive": 0} for role in sorted(ROLES)}
    for role, is_active, total in rows:
        counts[role]["active" if is_active else "inactive"] += int(total)
    roles = [{"role": role, **counts[role]} for role in sorted(counts)]
    return {
        "total": sum(item["active"] + item["inactive"] for item in roles),
        "pending_password_change": pending,
        "roles": roles,
    }


def outbox_summary(now: datetime) -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, count(*), min(created_at) FROM outbox_jobs WHERE NOT is_deleted GROUP BY status"
        ).fetchall()
    counts = dict.fromkeys(OUTBOX_STATUSES, 0)
    oldest_pending: datetime | None = None
    for status, total, first_created in rows:
        counts[status] = int(total)
        if status == "pending":
            oldest_pending = first_created
    age = None
    if oldest_pending is not None:
        age = int(max(now - oldest_pending, timedelta(0)).total_seconds())
    return {"counts": counts, "oldest_pending_age_seconds": age}


def worker_status(now: datetime) -> dict[str, Any]:
    with connect() as conn:
        return _worker_status(conn, now)

