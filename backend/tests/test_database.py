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

from safe_kit import audit, cases, migrate, outbox, security, users
from safe_kit.cases import CaseScope
from safe_kit.db import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
    if os.environ.get("SAFE_TEST_DATABASE") != "1":
        yield
        return
    from safe_kit.migrate import main as migrate

    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute("SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)").fetchone()[0]
        if not exists:
            conn.execute("INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')")
    yield


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
    target = new_user("triager")
    assert users.reset_password(target, "another temporary passphrase", source="cli") is True
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
    assert {task.kind: task.status for task in fetched.deadlines} == {
        "affected_customers": "open",
        "preliminary_safe_report": "open",
        "broader_advisory": "not_applicable",
        "preliminary_public_report": "open",
        "remediation_status": "open",
    }
    assert any(item.id == case.id for item in cases.list_visible_cases(CaseScope(owner, read_own=True), 50, 0))
    assert cases.count_visible_cases(CaseScope(owner, read_own=True)) >= 1
    assert audit_rows(case.id, audit.CASE_CREATE) == [
        (
            owner,
            {
                "calendar_version": "provisional-1",
                "calendar_timezone": fetched.calendar_timezone,
                "not_applicable": ["broader_advisory"],
            },
        )
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


def password_history_hashes(user_id: str) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT password_hash FROM password_history WHERE user_id = %s AND NOT is_deleted ORDER BY created_at",
            (user_id,),
        ).fetchall()
    return [row[0] for row in rows]


def password_history_temporary_flags(user_id: str) -> list[bool]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT is_temporary FROM password_history WHERE user_id = %s AND NOT is_deleted ORDER BY created_at",
            (user_id,),
        ).fetchall()
    return [row[0] for row in rows]


def password_changed_at(user_id: str) -> datetime:
    with connect() as conn:
        return conn.execute("SELECT password_changed_at FROM users WHERE id = %s", (user_id,)).fetchone()[0]


def test_every_password_write_is_recorded_as_an_argon2_hash():
    target = new_user("triager")
    first = password_history_hashes(target)
    assert len(first) == 1
    assert security.verify_password(first[0], "temporary passphrase")

    changed_before = password_changed_at(target)
    assert users.change_own_password(target, "a new passphrase here") is True
    assert password_changed_at(target) > changed_before
    assert users.reset_password(target, "another temporary passphrase") is True

    hashes = password_history_hashes(target)
    assert len(hashes) == 3
    assert all(stored.startswith("$argon2id$") for stored in hashes)
    assert "a new passphrase here" not in hashes
    # Account creation and the admin reset are temporary; the self-service change is not.
    assert password_history_temporary_flags(target) == [True, False, True]


def test_bootstrap_admin_records_its_password_as_temporary():
    # The bootstrap needs a database with no admin. Other tests in the session create admins, so this runs only
    # when it is the first to do so (for example, run alone on a fresh database).
    if users.admin_exists():
        pytest.skip("the bootstrap needs a database with no admin")
    username = "t-" + uuid.uuid4().hex[:12]
    user_id = users.bootstrap_admin(username, "bootstrap passphrase", f"{username}@example.com")
    assert password_history_temporary_flags(user_id) == [True]


def test_profile_account_creation_records_the_temporary_password():
    username = "t-" + uuid.uuid4().hex[:12]
    user_id = users.create_user_with_profile(username, "temporary passphrase", "reporter", f"{username}@example.com")
    assert len(password_history_hashes(user_id)) == 1
    assert password_history_temporary_flags(user_id) == [True]


def test_temporary_password_cannot_be_chosen_as_the_next_password():
    target = new_user("reporter")
    with pytest.raises(users.PasswordReused):
        users.change_own_password(target, "temporary passphrase")
    assert len(password_history_hashes(target)) == 1


def test_temporary_password_stays_refused_after_later_changes():
    target = new_user("triager")
    for new in ["first passphrase here", "second passphrase here", "third passphrase here", "fourth passphrase here"]:
        assert users.change_own_password(target, new) is True
    # Four later changes: the temporary password is outside the last three, and it is still refused.
    before = len(password_history_hashes(target))
    with pytest.raises(users.PasswordReused):
        users.change_own_password(target, "temporary passphrase")
    with pytest.raises(users.PasswordReused):
        users.reset_password(target, "temporary passphrase")
    assert len(password_history_hashes(target)) == before


def test_last_three_passwords_are_refused_and_the_fourth_most_recent_is_allowed():
    target = new_user("triager")
    passwords = ["first passphrase here", "second passphrase here", "third passphrase here", "fourth passphrase here"]
    for new in passwords:
        assert users.change_own_password(target, new) is True
    before = len(password_history_hashes(target))
    assert before == 5

    # The last three are second, third and fourth.
    for recent in passwords[1:]:
        with pytest.raises(users.PasswordReused):
            users.change_own_password(target, recent)
    with pytest.raises(users.PasswordReused):
        users.reset_password(target, "third passphrase here")
    # A refused change writes nothing: no history row, no new timestamp.
    assert len(password_history_hashes(target)) == before

    # The first password is now the fourth most recent non-temporary entry, outside the window.
    assert users.change_own_password(target, "first passphrase here") is True


def test_expired_password_forces_a_change_until_the_user_changes_it():
    target = new_user("reporter")
    # A new account starts in the forced-change state. A self-service change clears it.
    assert users.find_user_by_id(target).must_change_password is True
    assert users.change_own_password(target, "a new passphrase here") is True
    with connect() as conn:
        conn.execute("UPDATE users SET password_changed_at = now() - interval '179 days' WHERE id = %s", (target,))
    assert users.find_user_by_id(target).must_change_password is False

    with connect() as conn:
        conn.execute("UPDATE users SET password_changed_at = now() - interval '181 days' WHERE id = %s", (target,))
    assert users.find_user_by_id(target).must_change_password is True

    assert users.change_own_password(target, "yet another passphrase") is True
    user = users.find_user_by_id(target)
    assert user.must_change_password is False
    assert user.password_changed_at > datetime.now(UTC) - timedelta(days=1)


def test_reporter_account_password_cannot_be_reset_by_an_administrator():
    target = new_user("reporter")
    hashes_before = password_history_hashes(target)
    changed_before = password_changed_at(target)
    with pytest.raises(users.ReporterAccountResetRefused):
        users.reset_password(target, "another temporary passphrase", source="cli")
    # Refused before anything is written: the password, its history, and the audit trail are unchanged.
    assert password_history_hashes(target) == hashes_before
    assert password_changed_at(target) == changed_before
    assert audit_rows(target, audit.USER_PASSWORD_RESET) == []


def test_reporter_can_still_change_its_own_password():
    target = new_user("reporter")
    assert users.change_own_password(target, "a new passphrase here") is True
    assert audit_rows(target, audit.USER_PASSWORD_CHANGE) == [(target, {})]


def test_reporter_contact_permission_is_in_the_catalog_and_held_only_by_reporting_owner():
    from safe_kit import rbac

    assert "reporter_contact:read" in dict(rbac.list_catalog())
    holders = {role for role, permissions in rbac.list_role_permissions().items() if "reporter_contact:read" in permissions}
    assert holders == {"reporting_owner"}

