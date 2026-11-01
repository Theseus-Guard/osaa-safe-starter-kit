"""Database-backed tests: triggers, transactions, SKIP LOCKED queue behaviour, and case persistence.

They need a PostgreSQL database with the SAFE_DB_* settings and a password file, and they run the migrations
first. They are skipped unless SAFE_TEST_DATABASE=1, so the default test run needs no database.
Use a throwaway database: the tests create rows and never remove them (rows are soft-deleted, as in the application).
"""

import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from safe_kit import audit, cases, migrate, outbox, users
from safe_kit.cases import CaseScope
from safe_kit.db import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    migrate.MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
    assert migrate.main() == 0


def new_user(role: str = "reporter") -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


def audit_rows(target_id: str, action: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT actor_id::text, details FROM audit_events "
            "WHERE target_id = %s AND action = %s AND NOT is_deleted ORDER BY created_at",
            (target_id, action),
        ).fetchall()


@pytest.fixture
def clean_outbox():
    # Soft-delete jobs left by earlier runs, so that run_once only sees this test's jobs.
    with connect() as conn:
        conn.execute(
            "UPDATE outbox_jobs SET is_deleted = true, deleted_at = now() WHERE NOT is_deleted"
        )


def enqueue_one(job_type: str = "noop") -> str:
    with connect() as conn:
        return outbox.enqueue(conn, job_type, {"check": True})


def job_row(job_id: str) -> tuple:
    with connect() as conn:
        return conn.execute(
            "SELECT status, attempts, extract(epoch FROM available_at - now()), last_error "
            "FROM outbox_jobs WHERE id = %s",
            (job_id,),
        ).fetchone()


def make_due(job_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE outbox_jobs SET available_at = now() - interval '1 second' WHERE id = %s", (job_id,))


def expire_lease(job_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE outbox_jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,))


# --- audit ---------------------------------------------------------------------------------------------------


def test_audit_content_cannot_be_changed_and_a_soft_delete_cannot_be_undone():
    target = str(uuid.uuid4())
    with connect() as conn:
        audit.record(conn, "test.event", "test", target, details={"n": 1})
    with connect() as conn:
        row_id = conn.execute(
            "SELECT id::text FROM audit_events WHERE target_id = %s", (target,)
        ).fetchone()[0]

    with pytest.raises(psycopg.errors.RaiseException):
        with connect() as conn:
            conn.execute("UPDATE audit_events SET action = 'changed' WHERE id = %s", (row_id,))
    with pytest.raises(psycopg.errors.RaiseException):
        with connect() as conn:
            conn.execute("UPDATE audit_events SET details = '{}'::jsonb WHERE id = %s", (row_id,))

    with connect() as conn:
        conn.execute("UPDATE audit_events SET is_deleted = true, deleted_at = now() WHERE id = %s", (row_id,))
    with pytest.raises(psycopg.errors.RaiseException):
        with connect() as conn:
            conn.execute("UPDATE audit_events SET is_deleted = false, deleted_at = NULL WHERE id = %s", (row_id,))


def test_role_change_and_its_audit_row_commit_together():
    actor = new_user("admin")
    target = new_user("reporter")
    assert users.update_user(target, role="auditor", actor_id=actor) == users.UpdateOutcome.OK
    rows = audit_rows(target, audit.USER_UPDATE)
    assert len(rows) == 1
    assert rows[0][0] == actor
    assert rows[0][1] == {"changes": {"role": {"from": "reporter", "to": "auditor"}}}


def test_failed_change_leaves_no_audit_row():
    target = new_user("reporter")
    before = len(audit_rows(target, audit.USER_UPDATE))
    with pytest.raises(psycopg.errors.CheckViolation):
        users.update_user(target, role="not-a-role", actor_id=None)
    assert len(audit_rows(target, audit.USER_UPDATE)) == before


def test_password_reset_is_audited_with_the_operator_source():
    target = new_user("reporter")
    assert users.reset_password(target, "temporary passphrase", source="cli") is True
    rows = audit_rows(target, audit.USER_PASSWORD_RESET)
    assert rows == [(None, {"source": "cli"})]


def test_self_password_change_is_audited_with_the_user_as_actor():
    target = new_user("reporter")
    assert users.change_own_password(target, "a new passphrase here") is True
    assert audit_rows(target, audit.USER_PASSWORD_CHANGE) == [(target, {})]


def test_rbac_change_lists_granted_and_revoked_permissions():
    from safe_kit.rbac import RoleOutcome, list_role_permissions, set_role_permissions

    actor = new_user("admin")
    before = frozenset(list_role_permissions()["auditor"])
    target = frozenset({"cases:submit"})  # independent of the current state: the diff is computed from it
    try:
        assert set_role_permissions("auditor", target, actor_id=actor) is RoleOutcome.OK
        with connect() as conn:
            row = conn.execute(
                "SELECT actor_id::text, details FROM audit_events "
                "WHERE action = %s AND target_id = 'auditor' ORDER BY created_at DESC LIMIT 1",
                (audit.ROLE_PERMISSIONS_CHANGE,),
            ).fetchone()
        assert row[0] == actor
        assert row[1] == {
            "granted": sorted(target - before),
            "revoked": sorted(before - target),
        }
    finally:
        set_role_permissions("auditor", before, actor_id=actor)


# --- outbox --------------------------------------------------------------------------------------------------


def test_job_is_claimed_and_completed(clean_outbox):
    job_id = enqueue_one()
    assert outbox.run_once() is True
    assert job_row(job_id)[0] == "done"
    assert outbox.run_once() is False


def test_claimed_job_is_not_offered_again_until_its_lease_expires(clean_outbox):
    job_id = enqueue_one()
    first = outbox.claim_next()
    assert first is not None and first.id == job_id and first.attempts == 1
    assert outbox.claim_next() is None
    expire_lease(job_id)
    second = outbox.claim_next()
    assert second is not None and second.id == job_id and second.attempts == 2
    # The first worker's late result is refused, because the attempt number has moved on.
    assert outbox._complete(first) is False
    assert outbox._complete(second) is True
    assert job_row(job_id)[0] == "done"


def test_failed_attempt_waits_for_backoff_then_moves_to_failed_at_the_limit(clean_outbox):
    job_id = enqueue_one()

    def refuse(payload):
        raise RuntimeError("refused")

    handlers = {"noop": refuse}
    assert outbox.run_once(handlers) is True
    status, attempts, seconds_until_retry, last_error = job_row(job_id)
    assert (status, attempts) == ("pending", 1)
    assert 20 <= seconds_until_retry <= 40
    assert last_error == "RuntimeError: refused"
    assert outbox.run_once(handlers) is False  # backoff not yet elapsed

    for expected in range(2, outbox.MAX_ATTEMPTS + 1):
        make_due(job_id)
        assert outbox.run_once(handlers) is True
        status, attempts, _, _ = job_row(job_id)
        assert attempts == expected
    assert status == "failed"

    make_due(job_id)
    assert outbox.run_once(handlers) is False  # failed jobs are never claimed again


def test_job_whose_last_attempt_lost_its_lease_is_failed_not_retried(clean_outbox):
    job_id = enqueue_one()
    for _ in range(outbox.MAX_ATTEMPTS):
        assert outbox.claim_next() is not None
        expire_lease(job_id)
    assert outbox.claim_next() is None
    status, attempts, _, last_error = job_row(job_id)
    assert (status, attempts) == ("failed", outbox.MAX_ATTEMPTS)
    assert "lease expired" in last_error


def test_unknown_job_type_is_failed_without_retry(clean_outbox):
    job_id = enqueue_one(job_type="deliver_to_nowhere")
    assert outbox.run_once({"noop": lambda payload: None}) is True
    status, attempts, _, last_error = job_row(job_id)
    assert status == "failed"
    assert last_error == "unknown job type: deliver_to_nowhere"


# --- cases --------------------------------------------------------------------------------------------------


def test_case_is_stored_with_its_deadline_tasks_and_audit_row():
    owner = new_user("reporter")
    t0 = datetime.now(UTC) - timedelta(hours=1)
    case = cases.create_case("Integration case", t0, created_by=owner)

    assert re.fullmatch(r"SAFE-\d{4}-\d{6}", case.reference)
    fetched = cases.find_visible_case(case.id, CaseScope(owner, read_own=True))
    assert fetched is not None
    assert fetched.calendar_version == "provisional-1"
    assert len(fetched.deadlines) == 5
    assert [task.status for task in fetched.deadlines] == ["open"] * 5
    assert any(item.id == case.id for item in cases.list_visible_cases(CaseScope(owner, read_own=True), 50, 0))
    assert cases.count_visible_cases(CaseScope(owner, read_own=True)) >= 1
    assert audit_rows(case.id, audit.CASE_CREATE) == [
        (owner, {"calendar_version": "provisional-1", "calendar_timezone": fetched.calendar_timezone})
    ]


def test_other_users_cannot_find_a_case(clean_outbox):
    owner = new_user("reporter")
    stranger = new_user("reporter")
    case = cases.create_case("Owner only", datetime.now(UTC) - timedelta(hours=1), created_by=owner)
    assert cases.find_visible_case(case.id, CaseScope(stranger, read_own=True)) is None
    assert all(item.id != case.id for item in cases.list_visible_cases(CaseScope(stranger, read_own=True), 50, 0))


def test_soft_deleted_case_is_not_found():
    owner = new_user("reporter")
    case = cases.create_case("To be soft deleted", datetime.now(UTC) - timedelta(hours=1), created_by=owner)
    with connect() as conn:
        conn.execute("UPDATE cases SET is_deleted = true, deleted_at = now() WHERE id = %s", (case.id,))
    assert cases.find_visible_case(case.id, CaseScope(owner, read_own=True)) is None
