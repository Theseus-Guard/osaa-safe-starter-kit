"""User records, lookups, and administrative changes."""

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

import psycopg
from email_validator import EmailNotValidError, validate_email
from psycopg import sql

from safe_kit import audit
from safe_kit.db import connect
from safe_kit.security import (
    PASSWORD_HISTORY_DEPTH,
    hash_password,
    is_recent_password,
    password_expired,
)

ROLES = frozenset(
    {
        "reporter",
        "triager",
        "investigator",
        "reviewer",
        "disclosure_approver",
        "auditor",
        "admin",
        "reporting_owner",
        "institution_owner",
    }
)

# Generic names that are easy to guess and would make the admin account a target.
RESERVED_USERNAMES = frozenset(
    {"admin", "administrator", "root", "superuser", "sysadmin", "system", "operator"}
)
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


class InvalidUsername(Exception):
    pass


def validate_username(username: str) -> None:
    # Checked first and case-insensitively, so "Admin" and "ROOT" get the reserved-name message too.
    if username.lower() in RESERVED_USERNAMES:
        raise InvalidUsername("username is reserved")
    if not USERNAME_PATTERN.fullmatch(username):
        raise InvalidUsername(
            "username must be 3-64 characters: lowercase letters, digits, '.', '_' or '-'"
        )


# Profile rules. The database checks the same limits (migration 0016) as a backstop.
TEXT_MAX_LENGTH = 100
EMAIL_MAX_LENGTH = 254
PROFILE_TEXT_FIELDS = ("first_name", "last_name", "title")
PROFILE_FIELDS = (*PROFILE_TEXT_FIELDS, "email")
# Name of the partial unique index on lower(email). Migration 0016 creates it.
EMAIL_INDEX = "users_email_unique"


class InvalidEmail(Exception):
    pass


class InvalidProfile(Exception):
    pass


class EmailAlreadyInUse(Exception):
    pass


class PasswordReused(Exception):
    """The new password equals one of the account's last PASSWORD_HISTORY_DEPTH passwords,
    or a temporary password that was ever issued to the account."""


class ReporterAccountResetRefused(Exception):
    """An administrator may not reset the password of a reporter account. The reporter keeps the account they chose,
    so no administrator can sign in as the person who opened a case."""


def normalize_email(value: str | None) -> str:
    """The one email rule, used by the API, the profile endpoints, and the CLI.

    Surrounding whitespace is removed and the rest is stored as entered. The address must pass email-validator
    (syntax only, no deliverability check) and fit in EMAIL_MAX_LENGTH. Empty input is reported as required.
    """
    candidate = (value or "").strip()
    if not candidate:
        raise InvalidEmail("email is required")
    if len(candidate) > EMAIL_MAX_LENGTH:
        raise InvalidEmail("invalid email address")
    try:
        validate_email(candidate, check_deliverability=False)
    except EmailNotValidError:
        raise InvalidEmail("invalid email address") from None
    return candidate


def normalize_profile_text(field: str, value: str | None) -> str | None:
    """Trim a name or title. Empty input becomes None (NULL), so a caller can clear the field."""
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > TEXT_MAX_LENGTH:
        raise InvalidProfile(f"{field} must be at most {TEXT_MAX_LENGTH} characters")
    return trimmed


def normalize_profile(values: Mapping[str, str | None]) -> dict[str, str | None]:
    """Normalize the profile fields a caller sent. Only the keys present in values are returned."""
    normalized: dict[str, str | None] = {}
    for field, value in values.items():
        if field == "email":
            normalized[field] = normalize_email(value)
        elif field in PROFILE_TEXT_FIELDS:
            normalized[field] = normalize_profile_text(field, value)
        else:
            raise ValueError(f"unknown profile field: {field}")
    return normalized


@dataclass(frozen=True)
class User:
    id: str
    username: str
    role: str
    is_active: bool
    password_hash: str
    sessions_valid_after: datetime
    # True when the account must change its password: a forced change, or a password older than PASSWORD_MAX_AGE.
    must_change_password: bool
    created_at: datetime
    password_changed_at: datetime = dataclasses.field(
        default_factory=lambda: datetime.now(UTC)
    )
    # The first admin created by the bootstrap. It can never be disabled, deleted, or demoted.
    is_protected: bool = False
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    # NULL only for accounts created before the email requirement (migration 0016).
    email: str | None = None
    # Starts at 1 and rises by one with each change to the account (see update_user). Sent back as If-Match.
    revision: int = 1


_COLUMNS = (
    "id::text, username, role, is_active, password_hash, sessions_valid_after, must_change_password, created_at, "
    "is_protected, first_name, last_name, title, email, password_changed_at, revision"
)


def _to_user(row: tuple) -> User:
    return User(
        id=row[0],
        username=row[1],
        role=row[2],
        is_active=row[3],
        password_hash=row[4],
        sessions_valid_after=row[5],
        # An expired password uses the same forced-change state as a temporary one.
        must_change_password=row[6] or password_expired(row[13]),
        created_at=row[7],
        password_changed_at=row[13],
        is_protected=row[8],
        first_name=row[9],
        last_name=row[10],
        title=row[11],
        email=row[12],
        revision=row[14],
    )


def _record_password(conn, user_id: str, password_hash: str, temporary: bool) -> None:
    """Keep the hash of a password that was just set. Called in the same transaction as the write that sets it.

    temporary is True for a password issued by an admin or the operator. Such a password can never be chosen again.
    """
    conn.execute(
        "INSERT INTO password_history (user_id, password_hash, is_temporary) VALUES (%s, %s, %s)",
        (user_id, password_hash, temporary),
    )


def create_user(
    username: str, password: str, role: str, actor_id: str | None = None
) -> str:
    """Create an account with a temporary password. The user must change it at first login."""
    validate_username(username)
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    password_hash = hash_password(password)
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role, password_changed_at) "
            "VALUES (%s, %s, %s, now()) RETURNING id::text",
            (username, password_hash, role),
        ).fetchone()
        _record_password(conn, row[0], password_hash, temporary=True)
        audit.record(
            conn,
            audit.USER_CREATE,
            "user",
            row[0],
            actor_id=actor_id,
            details={"role": role},
        )
    return row[0]


def create_user_with_profile(
    username: str,
    password: str,
    role: str,
    email: str | None,
    first_name: str | None = None,
    last_name: str | None = None,
    title: str | None = None,
    actor_id: str | None = None,
) -> str:
    """Create an account with a temporary password and its profile. The email is required.

    Raises InvalidUsername, InvalidEmail, InvalidProfile, or EmailAlreadyInUse before anything is written.
    A duplicate username still raises psycopg.errors.UniqueViolation, as create_user does.
    """
    validate_username(username)
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    email_value = normalize_email(email)
    texts = normalize_profile(
        {"first_name": first_name, "last_name": last_name, "title": title}
    )
    password_hash = hash_password(password)
    with connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO users (username, password_hash, role, email, first_name, last_name, title, "
                "password_changed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now()) RETURNING id::text",
                (
                    username,
                    password_hash,
                    role,
                    email_value,
                    texts["first_name"],
                    texts["last_name"],
                    texts["title"],
                ),
            ).fetchone()
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name == EMAIL_INDEX:
                raise EmailAlreadyInUse from error
            raise
        _record_password(conn, row[0], password_hash, temporary=True)
        audit.record(
            conn,
            audit.USER_CREATE,
            "user",
            row[0],
            actor_id=actor_id,
            details={"role": role},
        )
    return row[0]


class AdminAlreadyExists(Exception):
    pass


BOOTSTRAP_LOCK_KEY = 0x5AFE0002
# Serializes changes to who is an active admin, so two concurrent changes cannot both remove the last admin.
ADMIN_SET_LOCK_KEY = 0x5AFE0003


def admin_exists() -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM users WHERE role = 'admin' AND NOT is_deleted)"
        ).fetchone()
    return row[0]


def bootstrap_admin(username: str, password: str, email: str | None) -> str:
    """Create the first admin with its email. The check and the insert run under one lock, so two concurrent
    bootstraps cannot both succeed. Raises InvalidEmail before anything is written if the email is not valid."""
    validate_username(username)
    email_value = normalize_email(email)
    password_hash = hash_password(password)
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (BOOTSTRAP_LOCK_KEY,))
        if conn.execute(
            "SELECT EXISTS (SELECT 1 FROM users WHERE role = 'admin' AND NOT is_deleted)"
        ).fetchone()[0]:
            raise AdminAlreadyExists
        try:
            row = conn.execute(
                "INSERT INTO users (username, password_hash, role, is_protected, email, password_changed_at) "
                "VALUES (%s, %s, 'admin', true, %s, now()) RETURNING id::text",
                (username, password_hash, email_value),
            ).fetchone()
        except psycopg.errors.UniqueViolation as error:
            if error.diag.constraint_name == EMAIL_INDEX:
                raise EmailAlreadyInUse from error
            raise
        _record_password(conn, row[0], password_hash, temporary=True)
        # No actor: the first admin is created by the operator at the command line.
        audit.record(
            conn,
            audit.USER_CREATE,
            "user",
            row[0],
            details={"role": "admin", "bootstrap": True},
        )
    return row[0]


def _end_sessions(conn, user_id: str) -> None:
    conn.execute(
        "UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    )


def _set_password(
    user_id: str,
    new_password: str,
    must_change: bool,
    action: str,
    actor_id: str | None,
    details: dict,
    refuse_reporter: bool = False,
) -> bool:
    """Replace the password and end every session of the user. Returns False if the user does not exist.

    Raises PasswordReused, before anything is written, if the new password is one of the account's last
    PASSWORD_HISTORY_DEPTH passwords or a temporary password issued to the account, however long ago.
    Raises ReporterAccountResetRefused, before anything is written, when refuse_reporter is set and the account
    has the reporter role. Access tokens issued before this moment are rejected by the sessions_valid_after check.
    """
    with connect() as conn:
        # Locks the account row, so two password changes for the same user run one after the other.
        account = conn.execute(
            "SELECT role FROM users WHERE id = %s AND NOT is_deleted FOR UPDATE",
            (user_id,),
        ).fetchone()
        if account is None:
            return False
        if refuse_reporter and account[0] == "reporter":
            raise ReporterAccountResetRefused
        # The last PASSWORD_HISTORY_DEPTH entries, plus every temporary entry, however old.
        candidates = conn.execute(
            "SELECT password_hash FROM ("
            "SELECT password_hash, is_temporary, row_number() OVER (ORDER BY created_at DESC) AS position "
            "FROM password_history WHERE user_id = %s AND NOT is_deleted"
            ") AS history WHERE position <= %s OR is_temporary",
            (user_id, PASSWORD_HISTORY_DEPTH),
        ).fetchall()
        if is_recent_password(new_password, [row[0] for row in candidates]):
            raise PasswordReused
        password_hash = hash_password(new_password)
        conn.execute(
            """
            UPDATE users
            SET password_hash = %s, must_change_password = %s, password_changed_at = now(),
                sessions_valid_after = now(), revision = revision + 1
            WHERE id = %s
            """,
            (password_hash, must_change, user_id),
        )
        # A password the account must replace at its next login is a temporary one.
        _record_password(conn, user_id, password_hash, temporary=must_change)
        _end_sessions(conn, user_id)
        audit.record(conn, action, "user", user_id, actor_id=actor_id, details=details)
    return True


def reset_password(
    user_id: str,
    temporary_password: str,
    actor_id: str | None = None,
    source: str = "api",
) -> bool:
    """Admin or operator reset. The user must choose a new password at next login.

    actor_id is None and source is "cli" when the operator resets a password from the container.
    Raises ReporterAccountResetRefused for an account with the reporter role.
    """
    return _set_password(
        user_id,
        temporary_password,
        must_change=True,
        action=audit.USER_PASSWORD_RESET,
        actor_id=actor_id,
        details={"source": source},
        refuse_reporter=True,
    )


def change_own_password(user_id: str, new_password: str) -> bool:
    """User-initiated change, after the current password has been verified by the caller."""
    return _set_password(
        user_id,
        new_password,
        must_change=False,
        action=audit.USER_PASSWORD_CHANGE,
        actor_id=user_id,
        details={},
    )


class UpdateOutcome(Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    LAST_ADMIN = "last_admin"
    PROTECTED = "protected"
    # The If-Match revision is not the account's current revision. Nothing changed.
    REVISION_MISMATCH = "revision_mismatch"


def update_user(
    user_id: str,
    role: str | None = None,
    is_active: bool | None = None,
    actor_id: str | None = None,
    *,
    expected_revision: int | None = None,
) -> UpdateOutcome:
    """Change a user's role and/or active state. Refuses if the change would leave no active admin.

    expected_revision is the account revision the caller read. The API always passes it; None skips the check and is
    only for server-side callers that hold no client revision. A change that changes nothing keeps the revision;
    otherwise the revision rises by one in the same transaction as the change.

    The audit row records only the fields whose value actually changed, and commits with the change.
    """
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADMIN_SET_LOCK_KEY,))
        current = conn.execute(
            "SELECT role, is_active, is_protected, revision FROM users WHERE id = %s AND NOT is_deleted FOR UPDATE",
            (user_id,),
        ).fetchone()
        if current is None:
            return UpdateOutcome.NOT_FOUND
        current_role, current_active, protected, revision = current
        if expected_revision is not None and revision != expected_revision:
            return UpdateOutcome.REVISION_MISMATCH
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

        changes = {}
        if new_role != current_role:
            changes["role"] = {"from": current_role, "to": new_role}
        if new_active != current_active:
            changes["is_active"] = {"from": current_active, "to": new_active}
        conn.execute(
            "UPDATE users SET role = %s, is_active = %s, revision = revision + %s WHERE id = %s",
            (new_role, new_active, int(bool(changes)), user_id),
        )
        if not new_active:
            _end_sessions(conn, user_id)
        if changes:
            audit.record(
                conn,
                audit.USER_UPDATE,
                "user",
                user_id,
                actor_id=actor_id,
                details={"changes": changes},
            )
    return UpdateOutcome.OK


def soft_delete_user(user_id: str, actor_id: str | None = None) -> UpdateOutcome:
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
            "UPDATE users SET is_deleted = true, deleted_at = now(), is_active = false, revision = revision + 1 "
            "WHERE id = %s",
            (user_id,),
        )
        _end_sessions(conn, user_id)
        audit.record(
            conn,
            audit.USER_DELETE,
            "user",
            user_id,
            actor_id=actor_id,
            details={"role": current_role},
        )
    return UpdateOutcome.OK


class OwnerOutcome(Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    INACTIVE = "inactive"
    PROTECTED = "protected"
    ALREADY_OWNER = "already_owner"
    NOT_OWNER = "not_owner"
    INVALID_ROLE = "invalid_role"


# The institution_owner role name. users.py cannot import safe_kit.rbac, because rbac imports this module.
_INSTITUTION_OWNER = "institution_owner"


def change_institution_owner(
    user_id: str, *, grant: bool, role: str | None, reason: str
) -> OwnerOutcome:
    """Grant the institution_owner role, or revoke it by giving the account `role`. Only the operator in the container
    calls this (safe_kit.admin). The change, its audit row, and the end of the account's sessions commit together.

    The change is refused when the account is protected (the first admin), inactive, or already in the state asked for.
    Revoking gives the account a role other than institution_owner. Its sessions end, so the next sign-in carries the
    new role.
    """
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADMIN_SET_LOCK_KEY,))
        current = conn.execute(
            "SELECT role, is_active, is_protected FROM users WHERE id = %s AND NOT is_deleted FOR UPDATE",
            (user_id,),
        ).fetchone()
        if current is None:
            return OwnerOutcome.NOT_FOUND
        current_role, active, protected = current
        if protected:
            return OwnerOutcome.PROTECTED
        if not active:
            return OwnerOutcome.INACTIVE
        if grant:
            if current_role == _INSTITUTION_OWNER:
                return OwnerOutcome.ALREADY_OWNER
            new_role = _INSTITUTION_OWNER
            action = audit.USER_OWNER_GRANT
        else:
            if current_role != _INSTITUTION_OWNER:
                return OwnerOutcome.NOT_OWNER
            if role is None or role not in ROLES or role == _INSTITUTION_OWNER:
                return OwnerOutcome.INVALID_ROLE
            new_role = role
            action = audit.USER_OWNER_REVOKE
        conn.execute(
            "UPDATE users SET role = %s, revision = revision + 1 WHERE id = %s",
            (new_role, user_id),
        )
        _end_sessions(conn, user_id)
        audit.record(
            conn,
            action,
            "user",
            user_id,
            details={
                "from": current_role,
                "to": new_role,
                "reason": reason,
                "source": "cli",
            },
        )
    return OwnerOutcome.OK


def list_users(limit: int, after: tuple[str, str] | None = None) -> list[User]:
    """One page of users in (username, id) order. after is the (username, id) position of the last row of the
    previous page; None starts at the first user."""
    if after is None:
        keyset, keyset_params = "TRUE", ()
    else:
        username, row_id = after
        keyset = "(username > %s OR (username = %s AND id > %s::uuid))"
        keyset_params = (username, username, row_id)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM users WHERE NOT is_deleted AND {keyset} ORDER BY username, id LIMIT %s",
            (*keyset_params, limit),
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


def update_profile(
    user_id: str, changes: Mapping[str, str | None], actor_id: str | None = None
) -> User | None:
    """Apply profile changes that were already normalized by normalize_profile.

    Only the fields whose value actually changes are written, and the audit row lists their names. It never
    holds a value, so the new email and the names stay out of the log. Returns None if the user does not exist.
    Raises EmailAlreadyInUse if another active account holds the email.
    """
    with connect() as conn:
        current = conn.execute(
            "SELECT first_name, last_name, title, email FROM users WHERE id = %s AND NOT is_deleted FOR UPDATE",
            (user_id,),
        ).fetchone()
        if current is None:
            return None
        current_values = dict(zip(PROFILE_FIELDS, current, strict=True))
        changed = [
            field
            for field in PROFILE_FIELDS
            if field in changes and changes[field] != current_values[field]
        ]
        if changed:
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(field)) for field in changed
            )
            try:
                conn.execute(
                    sql.SQL(
                        "UPDATE users SET {}, revision = revision + 1 WHERE id = %s"
                    ).format(assignments),
                    [changes[field] for field in changed] + [user_id],
                )
            except psycopg.errors.UniqueViolation as error:
                if error.diag.constraint_name == EMAIL_INDEX:
                    raise EmailAlreadyInUse from error
                raise
            audit.record(
                conn,
                audit.USER_PROFILE_UPDATE,
                "user",
                user_id,
                actor_id=actor_id,
                details={"fields": changed},
            )
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM users WHERE id = %s", (user_id,)
        ).fetchone()
    return _to_user(row)
