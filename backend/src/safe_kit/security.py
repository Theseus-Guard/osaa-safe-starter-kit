"""Password hashing, access tokens, and refresh-token material."""

import hashlib
import os
import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from safe_kit.config_guard import required_int

ISSUER = "safe-starter-kit"
AUDIENCE = "safe-starter-kit-api"
ALGORITHM = "HS256"
MIN_PASSWORD_LENGTH = 12
# Every password expires this long after it was set, temporary passwords included.
PASSWORD_MAX_AGE = timedelta(days=180)
# A new password may not equal any of the account's last this many passwords.
# Temporary passwords are refused for good, whatever their position in the history.
PASSWORD_HISTORY_DEPTH = 3

_hasher = PasswordHasher()
# Checked when the username is unknown, so that response time does not reveal which usernames exist.
_DUMMY_HASH = _hasher.hash("unused-password-placeholder")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_expired(changed_at: datetime, now: datetime | None = None) -> bool:
    """True when the password was set more than PASSWORD_MAX_AGE ago."""
    return (now or datetime.now(UTC)) - changed_at > PASSWORD_MAX_AGE


def is_recent_password(candidate: str, recent_hashes: Iterable[str]) -> bool:
    """True when the candidate matches one of the stored hashes. Hashes are salted, so each one is verified."""
    return any(verify_password(stored, candidate) for stored in recent_hashes)


def access_token_ttl() -> timedelta:
    # Required, no default: the startup guard refuses a process without it (config_guard.load_settings).
    return timedelta(minutes=required_int(os.environ, "SAFE_ACCESS_TTL_MINUTES", minimum=None))


def refresh_token_ttl() -> timedelta:
    # Each rotation restarts this window, so it is an idle limit: a session ends after this long without a refresh.
    return timedelta(minutes=required_int(os.environ, "SAFE_REFRESH_TTL_MINUTES", minimum=None))


def session_max_age() -> timedelta:
    # Counted from the login that started the session family. Renewals do not extend it.
    return timedelta(hours=required_int(os.environ, "SAFE_SESSION_MAX_HOURS", minimum=None))


def _signing_key() -> str:
    return Path(os.environ["SAFE_JWT_KEY_FILE"]).read_text(encoding="utf-8").strip()


def issue_access_token(user_id: str, username: str, role: str) -> tuple[str, datetime]:
    """Return the signed token and its expiry time."""
    now = datetime.now(UTC)
    expires_at = now + access_token_ttl()
    claims = {
        "sub": user_id,
        "username": username,
        "role": role,
        "jti": str(uuid.uuid4()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": expires_at,
    }
    return jwt.encode(claims, _signing_key(), algorithm=ALGORITHM), expires_at


def decode_access_token(token: str) -> dict:
    """Return the claims of a valid token. Raises jwt.InvalidTokenError otherwise."""
    return jwt.decode(
        token,
        _signing_key(),
        algorithms=[ALGORITHM],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": ["sub", "jti", "iss", "aud", "iat", "exp"]},
    )


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    """Random value of the CSRF cookie. The page reads it and sends it back in the X-CSRF-Token header."""
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
