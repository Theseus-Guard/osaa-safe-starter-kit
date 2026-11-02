"""Coverage for case persistence and deadline status changes at the service level.

They need a PostgreSQL database with the SAFE_DB_* settings and a password file, and they run the migrations
first. They are skipped unless SAFE_TEST_DATABASE=1. A system timezone is set on a fresh database if none exists.
Cases are never removed: the tests only add rows and read them.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from safe_kit import audit, cases, migrate, reviews, system_timezone, users
from safe_kit.cases import CaseScope, Outcome, calendar_for, deadline_tasks_for
from safe_kit.db import connect
from safe_kit.reviews import RoundOutcome

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@pytest.fixture(scope="module", autouse=True)
def case_database():
    migrate.MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
    assert migrate.main() == 0
    with connect() as conn:
        exists = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)"
        ).fetchone()[0]
        if not exists:
            conn.execute(
                "INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')"
            )


def new_user(role: str = "reporter") -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


def a_while_ago() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


def audit_rows(target_id: str, action: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT actor_id::text, details FROM audit_events "
            "WHERE target_id = %s AND action = %s AND NOT is_deleted ORDER BY created_at, id",
            (target_id, action),
        ).fetchall()


def task_state(task_id: str) -> tuple:
    with connect() as conn:
        return conn.execute(
            "SELECT status, not_applicable_reason FROM deadline_tasks WHERE id = %s",
            (task_id,),
        ).fetchone()


def finish_all_tasks(case_id: str, scope: CaseScope) -> None:
    for task in cases.find_visible_case(case_id, scope).deadlines:
        # A task that starts not applicable (the conditional advisory) is left as it is: closure accepts it.
        if task.status == "not_applicable":
            continue
        assert (
            cases.update_deadline(case_id, task.id, "done", None, scope, False)
            is Outcome.OK
        )


# --- creation -------------------------------------------------------------------------------------------------


def test_case_stores_the_timezone_snapshot_and_its_deadline_tasks():
    owner = new_user()
    tz = system_timezone.current_timezone_name()
    t0 = a_while_ago()

    case = cases.create_case("Snapshot case", t0, created_by=owner)

    assert case.calendar_timezone == tz
    expected = deadline_tasks_for(t0, calendar_for(tz))
    assert sorted((t.kind, t.due_at) for t in case.deadlines) == sorted(
        (t.kind, t.due_at) for t in expected
    )
    assert all(task.id for task in case.deadlines)
    assert len({task.id for task in case.deadlines}) == len(expected)
    assert {task.kind: task.status for task in case.deadlines} == {
        "affected_customers": "open",
        "preliminary_safe_report": "open",
        "broader_advisory": "not_applicable",
        "preliminary_public_report": "open",
        "remediation_status": "open",
    }

    with connect() as conn:
        stored_tz = conn.execute(
            "SELECT calendar_timezone FROM cases WHERE id = %s", (case.id,)
        ).fetchone()[0]
        stored_tasks = conn.execute(
            "SELECT kind, due_at, status FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted ORDER BY kind",
            (case.id,),
        ).fetchall()
    assert stored_tz == tz
    assert stored_tasks == sorted(
        ((t.kind, t.due_at, t.status) for t in expected), key=lambda row: row[0]
    )
    assert audit_rows(case.id, audit.CASE_CREATE) == [
        (
            owner,
            {
                "calendar_version": "provisional-1",
                "calendar_timezone": tz,
                "not_applicable": ["broader_advisory"],
            },
        )
    ]


# --- visibility -----------------------------------------------------------------------------------------------


def test_find_visible_case_returns_none_for_a_case_the_caller_cannot_see():
    owner = new_user()
    stranger = new_user()
    case = cases.create_case("Owner only", a_while_ago(), created_by=owner)

    assert cases.find_visible_case(case.id, CaseScope(owner, read_own=True)) is not None
    assert cases.find_visible_case(case.id, CaseScope(stranger, read_own=True)) is None
    # A caller with no case permission at all sees nothing, not even their own case.
    assert cases.find_visible_case(case.id, CaseScope(owner)) is None
    # An assignee-only scope without an active assignment sees nothing.
    assert (
        cases.find_visible_case(case.id, CaseScope(stranger, read_assigned=True))
        is None
    )
    assert (
        cases.find_visible_case(str(uuid.uuid4()), CaseScope(owner, read_own=True))
        is None
    )
    assert (
        cases.find_visible_case(case.id, CaseScope(stranger, read_all=True)) is not None
    )


def test_history_and_assignments_are_hidden_from_a_caller_who_cannot_see_the_case():
    owner = new_user()
    stranger = new_user()
    case = cases.create_case("Hidden history", a_while_ago(), created_by=owner)

    assert (
        cases.case_history(case.id, CaseScope(stranger, read_own=True), 10, 0) is None
    )
    assert cases.list_assignments(case.id, CaseScope(stranger, read_own=True)) is None
    assert (
        cases.case_history(case.id, CaseScope(owner, read_own=True), 10, 0) is not None
    )
    assert cases.list_assignments(case.id, CaseScope(owner, read_own=True)) == []


# --- paging ---------------------------------------------------------------------------------------------------


def test_list_pages_are_consecutive_and_newest_first():
    owner = new_user()
    scope = CaseScope(owner, read_own=True)
    created = [
        cases.create_case(f"Page case {n}", a_while_ago(), created_by=owner)
        for n in range(3)
    ]

    first = cases.list_visible_cases(scope, 2)
    second = cases.list_visible_cases(scope, 2, (first[-1].created_at, first[-1].id))
    third = cases.list_visible_cases(scope, 2, (second[-1].created_at, second[-1].id))

    assert [case.id for case in first + second] == [
        case.id for case in reversed(created)
    ]
    assert third == []


# --- deadline status transitions ------------------------------------------------------------------------------


def test_deadline_task_moves_between_open_done_and_not_applicable():
    owner = new_user()
    scope = CaseScope(owner, read_own=True)
    case = cases.create_case("Transitions", a_while_ago(), created_by=owner)
    task, other = case.deadlines[0], case.deadlines[1]

    # A reporter may set done, and a reason given for a status other than not_applicable is dropped.
    assert (
        cases.update_deadline(case.id, task.id, "done", "ignored", scope, False)
        is Outcome.OK
    )
    assert task_state(task.id) == ("done", None)
    assert audit_rows(case.id, audit.CASE_DEADLINE_UPDATE) == [
        (
            owner,
            {
                "task_id": task.id,
                "kind": task.kind,
                "status": {"old": "open", "new": "done"},
            },
        )
    ]

    # Only a triager may mark a task not applicable.
    assert cases.update_deadline(
        case.id, task.id, "not_applicable", "no reason", scope, False
    ) is (Outcome.NOT_APPLICABLE_FORBIDDEN)
    assert task_state(task.id) == ("done", None)

    assert (
        cases.update_deadline(
            case.id, task.id, "not_applicable", "reason text 123", scope, True
        )
        is Outcome.OK
    )
    assert task_state(task.id) == ("not_applicable", "reason text 123")
    rows = audit_rows(case.id, audit.CASE_DEADLINE_UPDATE)
    assert rows[-1][1] == {
        "task_id": task.id,
        "kind": task.kind,
        "status": {"old": "done", "new": "not_applicable"},
        "not_applicable_reason": {
            "old": None,
            "new": {"length": len("reason text 123")},
        },
    }

    # Repeating the same request changes nothing and writes no audit row.
    count = len(audit_rows(case.id, audit.CASE_DEADLINE_UPDATE))
    assert (
        cases.update_deadline(
            case.id, task.id, "not_applicable", "reason text 123", scope, True
        )
        is Outcome.OK
    )
    assert len(audit_rows(case.id, audit.CASE_DEADLINE_UPDATE)) == count

    # Leaving not_applicable needs the same permission, and clears the reason.
    assert (
        cases.update_deadline(case.id, task.id, "open", None, scope, False)
        is Outcome.NOT_APPLICABLE_FORBIDDEN
    )
    assert task_state(task.id) == ("not_applicable", "reason text 123")
    assert (
        cases.update_deadline(case.id, task.id, "open", None, scope, True) is Outcome.OK
    )
    assert task_state(task.id) == ("open", None)
    assert audit_rows(case.id, audit.CASE_DEADLINE_UPDATE)[-1][1] == {
        "task_id": task.id,
        "kind": task.kind,
        "status": {"old": "not_applicable", "new": "open"},
        "not_applicable_reason": {
            "old": {"length": len("reason text 123")},
            "new": None,
        },
    }

    assert task_state(other.id) == ("open", None), (
        "a change to one task leaves the others alone"
    )


def test_deadline_update_refuses_unknown_tasks_other_cases_tasks_and_invisible_cases():
    owner = new_user()
    stranger = new_user()
    scope = CaseScope(owner, read_own=True)
    case = cases.create_case("Refusals", a_while_ago(), created_by=owner)
    other_case = cases.create_case("Other case", a_while_ago(), created_by=owner)
    foreign_task = other_case.deadlines[0]

    assert (
        cases.update_deadline(case.id, str(uuid.uuid4()), "done", None, scope, False)
        is Outcome.TASK_NOT_FOUND
    )
    assert (
        cases.update_deadline(case.id, foreign_task.id, "done", None, scope, False)
        is Outcome.TASK_NOT_FOUND
    )
    assert task_state(foreign_task.id) == ("open", None)

    stranger_scope = CaseScope(stranger, read_own=True)
    assert cases.update_deadline(
        case.id, case.deadlines[0].id, "done", None, stranger_scope, False
    ) is (Outcome.NOT_FOUND)
    assert task_state(case.deadlines[0].id) == ("open", None)


def test_closed_case_refuses_deadline_changes_until_it_is_reopened():
    owner = new_user()
    triager = new_user("triager")
    reviewer = new_user("reviewer")
    owner_scope = CaseScope(owner, read_own=True)
    triage = CaseScope(triager, read_all=True)
    case = cases.create_case("Closed for changes", a_while_ago(), created_by=owner)

    finish_all_tasks(case.id, owner_scope)
    assert cases.assign_member(case.id, reviewer, triage)[0] is Outcome.OK
    reviewer_scope = CaseScope(reviewer, read_assigned=True)
    round_outcome, review_round, _ = reviews.open_round(case.id, reviewer_scope)
    assert round_outcome is RoundOutcome.OK
    assert (
        reviews.set_layer(
            review_round.id, "model", "fail", "Gap", [], 1, reviewer_scope
        )[0]
        is RoundOutcome.OK
    )
    assert cases.close_case(case.id, "Closed for the test", triage) == (Outcome.OK, [])

    task = case.deadlines[0]
    assert (
        cases.update_deadline(case.id, task.id, "open", None, triage, True)
        is Outcome.CLOSED
    )
    assert task_state(task.id) == ("done", None)

    assert cases.reopen_case(case.id, "Reopened for the test", triage) is Outcome.OK
    assert (
        cases.update_deadline(case.id, task.id, "open", None, triage, True)
        is Outcome.OK
    )
    assert task_state(task.id) == ("open", None)
