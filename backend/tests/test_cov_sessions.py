"""Coverage for login sessions: refresh-token rotation, reuse detection, revocation, and access-token revocation.

They need a PostgreSQL database with the SAFE_DB_* settings and a password file, and they run the migrations
first. They are skipped unless SAFE_TEST_DATABASE=1. Expiry is set with SQL, never by sleeping.
Rows are never removed: the tests only read them or set timestamps.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from safe_kit import migrate, sessions, users
from safe_kit.db import connect
from safe_kit.security import hash_refresh_token, new_refresh_token
from safe_kit.sessions import InvalidRefreshToken

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    migrate.MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
    assert migrate.main() == 0


def new_user() -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", "reporter")


def family_rows(token: str) -> list[dict]:
    """Every refresh-token row of the family that the token belongs to, oldest first."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT token_hash, revoked_at IS NOT NULL, is_deleted FROM refresh_tokens
            WHERE family_id = (SELECT family_id FROM refresh_tokens WHERE token_hash = %s)
            ORDER BY created_at, id
            """,
            (hash_refresh_token(token),),
        ).fetchall()
    return [{"token_hash": h, "revoked": revoked, "deleted": deleted} for h, revoked, deleted in rows]


def expire_refresh_token(token: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE refresh_tokens SET expires_at = now() - interval '1 second' WHERE token_hash = %s",
            (hash_refresh_token(token),),
        )


def revoked_access_token_rows(jti: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT is_deleted, deleted_at IS NOT NULL FROM revoked_access_tokens WHERE jti = %s",
            (jti,),
        ).fetchall()


# --- refresh-token rotation ------------------------------------------------------------------------------------


def test_rotation_returns_a_new_token_in_the_same_family_and_revokes_the_old_one():
    user = new_user()
    first = sessions.open_session(user)

    user_id, second = sessions.rotate_session(first)

    assert user_id == user
    assert second != first
    rows = family_rows(first)
    assert [row["token_hash"] for row in rows] == [hash_refresh_token(first), hash_refresh_token(second)]
    assert [row["revoked"] for row in rows] == [True, False]


def test_each_rotation_extends_the_same_family():
    user = new_user()
    first = sessions.open_session(user)
    _, second = sessions.rotate_session(first)
    _, third = sessions.rotate_session(second)

    rows = family_rows(third)
    assert [row["token_hash"] for row in rows] == [
        hash_refresh_token(first),
        hash_refresh_token(second),
        hash_refresh_token(third),
    ]
    assert [row["revoked"] for row in rows] == [True, True, False]


def test_presenting_a_rotated_token_revokes_the_whole_family_and_fails():
    user = new_user()
    first = sessions.open_session(user)
    _, second = sessions.rotate_session(first)

    with pytest.raises(InvalidRefreshToken):
        sessions.rotate_session(first)

    rows = family_rows(first)
    assert len(rows) == 2, "reuse must not issue a new token"
    assert all(row["revoked"] for row in rows)
    # The holder of the newest token is also refused, because the whole family is now revoked.
    with pytest.raises(InvalidRefreshToken):
        sessions.rotate_session(second)


def test_expired_refresh_token_is_refused_and_issues_nothing():
    token = sessions.open_session(new_user())
    expire_refresh_token(token)

    with pytest.raises(InvalidRefreshToken):
        sessions.rotate_session(token)

    assert len(family_rows(token)) == 1


def test_unknown_refresh_token_is_refused():
    with pytest.raises(InvalidRefreshToken):
        sessions.rotate_session(new_refresh_token())


# --- refresh-token revocation ----------------------------------------------------------------------------------


def test_revoke_refresh_token_applies_only_for_the_matching_user():
    owner = new_user()
    other = new_user()
    token = sessions.open_session(owner)

    sessions.revoke_refresh_token(token, other)
    assert family_rows(token)[0]["revoked"] is False

    sessions.revoke_refresh_token(token, owner)
    assert family_rows(token)[0]["revoked"] is True


def test_revoke_refresh_token_for_an_unknown_token_changes_nothing():
    token = sessions.open_session(new_user())
    sessions.revoke_refresh_token(new_refresh_token(), "00000000-0000-0000-0000-000000000000")
    assert family_rows(token)[0]["revoked"] is False


# --- access-token revocation -----------------------------------------------------------------------------------


def test_revoked_access_token_is_reported_as_revoked():
    jti = str(uuid.uuid4())
    expires_at = datetime.now(UTC) + timedelta(minutes=10)

    assert sessions.is_access_token_revoked(jti) is False
    sessions.revoke_access_token(jti, expires_at)
    assert sessions.is_access_token_revoked(jti) is True

    # Revoking again is harmless: one row, still revoked.
    sessions.revoke_access_token(jti, expires_at)
    assert sessions.is_access_token_revoked(jti) is True
    assert revoked_access_token_rows(jti) == [(False, False)]


def test_other_access_tokens_are_not_affected_by_a_revocation():
    revoked = str(uuid.uuid4())
    untouched = str(uuid.uuid4())
    sessions.revoke_access_token(revoked, datetime.now(UTC) + timedelta(minutes=10))
    assert sessions.is_access_token_revoked(untouched) is False


def test_expired_revoked_token_rows_are_soft_deleted_and_kept():
    expired_jti = str(uuid.uuid4())
    live_jti = str(uuid.uuid4())
    sessions.revoke_access_token(live_jti, datetime.now(UTC) + timedelta(minutes=10))

    # Recording an already expired token runs the cleanup, which soft-deletes every expired row.
    sessions.revoke_access_token(expired_jti, datetime.now(UTC) - timedelta(minutes=1))

    rows = revoked_access_token_rows(expired_jti)
    assert rows == [(True, True)], "the expired row must remain, soft-deleted"
    assert sessions.is_access_token_revoked(expired_jti) is False
    assert revoked_access_token_rows(live_jti) == [(False, False)]
    assert sessions.is_access_token_revoked(live_jti) is True
