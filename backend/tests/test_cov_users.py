"""Coverage for user deletion, administrative updates, listing, and lookups.

They need a PostgreSQL database with the SAFE_DB_* settings and a password file, and they run the migrations
first. They are skipped unless SAFE_TEST_DATABASE=1. Users are never removed: deletion is a soft delete.

The last-admin rule can only be reached when no protected admin is active. The protected admin cannot be
disabled (a database trigger refuses it), so that test is skipped when one exists in the test database.
"""

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from safe_kit import audit, migrate, users
from safe_kit.db import connect
from safe_kit.sessions import open_session
from safe_kit.security import hash_refresh_token
from safe_kit.users import UpdateOutcome

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    migrate.MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
    assert migrate.main() == 0


def new_user(role: str = "reporter") -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


def user_state(user_id: str) -> tuple:
    with connect() as conn:
        return conn.execute(
            "SELECT role, is_active, is_deleted, is_protected FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()


def audit_rows(target_id: str, action: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT actor_id::text, details FROM audit_events "
            "WHERE target_id = %s AND action = %s AND NOT is_deleted ORDER BY created_at, id",
            (target_id, action),
        ).fetchall()


def session_revoked(token: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT revoked_at IS NOT NULL FROM refresh_tokens WHERE token_hash = %s",
            (hash_refresh_token(token),),
        ).fetchone()[0]


@contextmanager
def only_active_admin(user_id: str):
    """Make user_id the only active admin while the block runs, then restore the other admins.

    A protected admin cannot be disabled, so the test is skipped when one is active.
    """
    with connect() as conn:
        others = conn.execute(
            "SELECT id::text, is_protected FROM users "
            "WHERE role = 'admin' AND is_active AND NOT is_deleted AND id <> %s",
            (user_id,),
        ).fetchall()
    if any(protected for _, protected in others):
        pytest.skip("a protected admin is active: the last-admin rule is not reachable in this database")
    other_ids = [row[0] for row in others]
    with connect() as conn:
        conn.execute("UPDATE users SET is_active = false WHERE id = ANY(%s::uuid[])", (other_ids,))
    try:
        yield
    finally:
        with connect() as conn:
            conn.execute("UPDATE users SET is_active = true WHERE id = ANY(%s::uuid[])", (other_ids,))


def protected_admin() -> str:
    """The protected admin of this database. If there is none, protect a new admin (the unique index allows one)."""
    with connect() as conn:
        row = conn.execute("SELECT id::text FROM users WHERE is_protected AND NOT is_deleted").fetchone()
    if row:
        return row[0]
    admin = new_user("admin")
    with connect() as conn:
        conn.execute("UPDATE users SET is_protected = true WHERE id = %s", (admin,))
    return admin


# --- last admin ----------------------------------------------------------------------------------------------


def test_the_only_active_admin_cannot_be_deleted_demoted_or_disabled():
    admin = new_user("admin")
    before = user_state(admin)

    with only_active_admin(admin):
        assert users.soft_delete_user(admin) is UpdateOutcome.LAST_ADMIN
        assert users.update_user(admin, role="reporter") is UpdateOutcome.LAST_ADMIN
        assert users.update_user(admin, is_active=False) is UpdateOutcome.LAST_ADMIN
        assert user_state(admin) == before


def test_an_admin_with_another_active_admin_can_be_deleted():
    first = new_user("admin")
    second = new_user("admin")

    assert users.soft_delete_user(first) is UpdateOutcome.OK
    assert user_state(first)[2] is True
    assert user_state(second)[2] is False


# --- protected admin -----------------------------------------------------------------------------------------


def test_protected_admin_cannot_be_deleted_demoted_or_disabled():
    admin = protected_admin()
    before = user_state(admin)
    assert before[3] is True

    assert users.soft_delete_user(admin) is UpdateOutcome.PROTECTED
    assert users.update_user(admin, role="reporter") is UpdateOutcome.PROTECTED
    assert users.update_user(admin, is_active=False) is UpdateOutcome.PROTECTED
    assert user_state(admin) == before

    # Keeping the protected admin as it is remains allowed.
    assert users.update_user(admin, role="admin", is_active=True) is UpdateOutcome.OK


def test_database_trigger_also_refuses_to_disable_the_protected_admin():
    admin = protected_admin()
    with pytest.raises(psycopg.errors.RaiseException):
        with connect() as conn:
            conn.execute("UPDATE users SET is_active = false WHERE id = %s", (admin,))
    assert user_state(admin)[1] is True


# --- soft delete ----------------------------------------------------------------------------------------------


def test_soft_delete_keeps_the_row_disables_the_account_and_ends_its_sessions():
    actor = new_user("triager")
    user = new_user("reporter")
    username = users.find_user_by_id(user).username
    token = open_session(user)

    assert users.soft_delete_user(user, actor_id=actor) is UpdateOutcome.OK

    with connect() as conn:
        row = conn.execute(
            "SELECT is_deleted, deleted_at IS NOT NULL, is_active FROM users WHERE id = %s", (user,)
        ).fetchone()
    assert row == (True, True, False)
    assert session_revoked(token) is True
    assert audit_rows(user, audit.USER_DELETE) == [(actor, {"role": "reporter"})]

    assert users.soft_delete_user(user) is UpdateOutcome.NOT_FOUND
    assert users.find_user(username) is None
    assert users.find_user_by_id(user) is None
    # The username stays reserved, so it cannot be reused for a new account.
    with pytest.raises(psycopg.errors.UniqueViolation):
        users.create_user(username, "temporary passphrase", "reporter")


def test_unknown_user_is_not_found_for_delete_and_update():
    unknown = str(uuid.uuid4())

    assert users.soft_delete_user(unknown) is UpdateOutcome.NOT_FOUND
    assert users.update_user(unknown, role="triager") is UpdateOutcome.NOT_FOUND


# --- update ---------------------------------------------------------------------------------------------------


def test_role_change_is_audited_and_an_unchanged_request_writes_no_audit_row():
    actor = new_user("triager")
    user = new_user("reporter")

    assert users.update_user(user, role="triager", actor_id=actor) is UpdateOutcome.OK
    assert user_state(user)[0] == "triager"
    assert audit_rows(user, audit.USER_UPDATE) == [
        (actor, {"changes": {"role": {"from": "reporter", "to": "triager"}}})
    ]

    assert users.update_user(user, role="triager", is_active=True, actor_id=actor) is UpdateOutcome.OK
    assert len(audit_rows(user, audit.USER_UPDATE)) == 1


def test_deactivating_a_user_ends_their_sessions_and_keeps_the_row():
    user = new_user("reporter")
    token = open_session(user)

    assert users.update_user(user, is_active=False) is UpdateOutcome.OK

    assert user_state(user)[1] is False
    assert session_revoked(token) is True
    assert users.find_user_by_id(user).is_active is False


# --- listing and lookups --------------------------------------------------------------------------------------


def test_list_pages_follow_the_username_order_and_skip_deleted_users():
    doomed = new_user()
    users.soft_delete_user(doomed)
    total = users.count_users()

    everyone = users.list_users(total, 0)
    with connect() as conn:
        expected_order = [
            row[0] for row in conn.execute("SELECT id::text FROM users WHERE NOT is_deleted ORDER BY username").fetchall()
        ]

    assert [user.id for user in everyone] == expected_order
    assert len(everyone) == total
    assert doomed not in [user.id for user in everyone]

    paged = []
    for offset in range(0, total, 2):
        paged.extend(users.list_users(2, offset))
    assert [user.id for user in paged] == expected_order
    assert users.list_users(2, total) == []


def test_find_user_ignores_deleted_users_and_count_drops_by_one():
    user = new_user()
    username = users.find_user_by_id(user).username
    assert users.find_user(username).id == user
    before = users.count_users()

    users.soft_delete_user(user)

    assert users.find_user(username) is None
    assert users.find_user_by_id(user) is None
    assert users.count_users() == before - 1
