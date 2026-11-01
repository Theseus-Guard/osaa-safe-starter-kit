"""User records, lookups, and administrative changes."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from safe_kit.db import connect
from safe_kit.security import hash_password

ROLES = frozenset(
    {
        "reporter",
        "triager",
        "investigator",
        "reviewer",
        "disclosure_approver",
        "auditor",
        "admin",
    }
)

# Generic names that are easy to guess and would make the admin account a target.
RESERVED_USERNAMES = frozenset({"admin", "administrator", "root", "superuser", "sysadmin", "system", "operator"})
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


class InvalidUsername(Exception):
    pass


def validate_username(username: str) -> None:
    # Checked first and case-insensitively, so "Admin" and "ROOT" get the reserved-name message too.
    if username.lower() in RESERVED_USERNAMES:
        raise InvalidUsername("username is reserved")
    if not USERNAME_PATTERN.fullmatch(username):
        raise InvalidUsername("username must be 3-64 characters: lowercase letters, digits, '.', '_' or '-'")


@dataclass(frozen=True)
class User:
    id: str
    username: str
    role: str
    is_active: bool
    password_hash: str
    sessions_valid_after: datetime
    must_change_password: bool
    created_at: datetime
    # The first admin created by the bootstrap. It can never be disabled, deleted, or demoted.
    is_protected: bool = False


_COLUMNS = (
    "id::text, username, role, is_active, password_hash, sessions_valid_after, must_change_password, created_at, "
    "is_protected"
)


def _to_user(row: tuple) -> User:
    return User(
        id=row[0],
        username=row[1],
        role=row[2],
        is_active=row[3],
        password_hash=row[4],
        sessions_valid_after=row[5],
        must_change_password=row[6],
        created_at=row[7],
        is_protected=row[8],
    )


def create_user(username: str, password: str, role: str) -> str:
    """Create an account with a temporary password. The user must change it at first login."""
    validate_username(username)
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id::text",
            (username, hash_password(password), role),
        ).fetchone()
    return row[0]


class AdminAlreadyExists(Exception):
    pass


BOOTSTRAP_LOCK_KEY = 0x5AFE0002
# Serializes changes to who is an active admin, so two concurrent changes cannot both remove the last admin.
ADMIN_SET_LOCK_KEY = 0x5AFE0003


def admin_exists() -> bool:
    with connect() as conn:
        row = conn.execute("SELECT EXISTS (SELECT 1 FROM users WHERE role = 'admin' AND NOT is_deleted)").fetchone()
    return row[0]


def bootstrap_admin(username: str, password: str) -> str:
    """Create the first admin. The check and the insert run under one lock, so two concurrent
    bootstraps cannot both succeed."""
    validate_username(username)
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (BOOTSTRAP_LOCK_KEY,))
        if conn.execute("SELECT EXISTS (SELECT 1 FROM users WHERE role = 'admin' AND NOT is_deleted)").fetchone()[0]:
            raise AdminAlreadyExists
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role, is_protected) "
            "VALUES (%s, %s, 'admin', true) RETURNING id::text",
            (username, hash_password(password)),
        ).fetchone()
    return row[0]


def _end_sessions(conn, user_id: str) -> None:
    conn.execute(
        "UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )


def _set_password(user_id: str, new_password: str, must_change: bool) -> bool:
    """Replace the password and end every session of the user. Returns False if the user does not exist.

    Access tokens issued before this moment are rejected by the sessions_valid_after check.
    """
    with connect() as conn:
        result = conn.execute(
            """
            UPDATE users
            SET password_hash = %s, must_change_password = %s, sessions_valid_after = now()
            WHERE id = %s AND NOT is_deleted
            """,
            (hash_password(new_password), must_change, user_id),
        )
        if result.rowcount == 0:
            return False
        _end_sessions(conn, user_id)
    return True


def reset_password(user_id: str, temporary_password: str) -> bool:
    """Admin or operator reset. The user must choose a new password at next login."""
    return _set_password(user_id, temporary_password, must_change=True)


def change_own_password(user_id: str, new_password: str) -> bool:
    """User-initiated change, after the current password has been verified by the caller."""
    return _set_password(user_id, new_password, must_change=False)


class UpdateOutcome(Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    LAST_ADMIN = "last_admin"
    PROTECTED = "protected"


def update_user(user_id: str, role: str | None = None, is_active: bool | None = None) -> UpdateOutcome:
    """Change a user's role and/or active state. Refuses if the change would leave no active admin."""
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADMIN_SET_LOCK_KEY,))
        current = conn.execute(
            "SELECT role, is_active, is_protected FROM users WHERE id = %s AND NOT is_deleted",
            (user_id,),
        ).fetchone()
        if current is None:
            return UpdateOutcome.NOT_FOUND
        current_role, current_active, protected = current
        new_role = role if role is not None else current_role
        new_active = is_active if is_active is not None else current_active
        if protected and (new_role != "admin" or not new_active):
            return UpdateOutcome.PROTECTED

        was_active_admin = current_role == "admin" and current_active
        stays_active_admin = new_role == "admin" and new_active
        if was_active_admin and not stays_active_admin:
            others = conn.execute(
                "SELECT count(*) FROM users WHERE role = 'admin' AND is_active AND NOT is_deleted AND id <> %s",
                (user_id,),
            ).fetchone()[0]
            if others == 0:
                return UpdateOutcome.LAST_ADMIN

        conn.execute("UPDATE users SET role = %s, is_active = %s WHERE id = %s", (new_role, new_active, user_id))
        if not new_active:
            _end_sessions(conn, user_id)
    return UpdateOutcome.OK


def soft_delete_user(user_id: str) -> UpdateOutcome:
    """Mark a user as deleted. The row stays in the database. A deleted user is disabled and loses its sessions."""
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADMIN_SET_LOCK_KEY,))
        current = conn.execute(
            "SELECT role, is_active, is_protected FROM users WHERE id = %s AND NOT is_deleted",
            (user_id,),
        ).fetchone()
        if current is None:
            return UpdateOutcome.NOT_FOUND
        current_role, current_active, protected = current
        if protected:
            return UpdateOutcome.PROTECTED
        if current_role == "admin" and current_active:
            others = conn.execute(
                "SELECT count(*) FROM users WHERE role = 'admin' AND is_active AND NOT is_deleted AND id <> %s",
                (user_id,),
            ).fetchone()[0]
            if others == 0:
                return UpdateOutcome.LAST_ADMIN
        conn.execute(
            "UPDATE users SET is_deleted = true, deleted_at = now(), is_active = false WHERE id = %s",
            (user_id,),
        )
        _end_sessions(conn, user_id)
    return UpdateOutcome.OK


def list_users(limit: int, offset: int) -> list[User]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM users WHERE NOT is_deleted ORDER BY username LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
    return [_to_user(row) for row in rows]


def count_users() -> int:
    with connect() as conn:
        row = conn.execute("SELECT count(*) FROM users WHERE NOT is_deleted").fetchone()
    return row[0]


def find_user(username: str) -> User | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM users WHERE username = %s AND NOT is_deleted",
            (username,),
        ).fetchone()
    return _to_user(row) if row else None


def find_user_by_id(user_id: str) -> User | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM users WHERE id = %s AND NOT is_deleted",
            (user_id,),
        ).fetchone()
    return _to_user(row) if row else None
