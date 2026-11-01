"""Append-only audit trail. Administrative changes and login failures each write one row.

Rows are never edited or removed: a database trigger rejects every update except the soft delete.
Details must never hold passwords, tokens, or IP addresses. record() rejects such keys and values,
so a careless caller fails in tests instead of leaking secrets into the log.

record() takes the caller's connection so the audit row commits or rolls back with the change it describes.
"""

import ipaddress
import re
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from safe_kit.db import connect

USER_CREATE = "user.create"
USER_UPDATE = "user.update"
USER_DELETE = "user.delete"
USER_PASSWORD_RESET = "user.password_reset"
USER_PASSWORD_CHANGE = "user.password_change"
ROLE_PERMISSIONS_CHANGE = "role.permissions_change"
LOGIN_FAILED = "auth.login_failed"
CASE_CREATE = "case.create"

# A key is rejected when any word in it starts with one of these stems, or equals one of the network words.
_SECRET_STEMS = ("password", "passwd", "pwd", "secret", "token", "credential", "cookie", "authorization", "hash")
_NETWORK_WORDS = frozenset({"ip", "ips", "addr", "address"})
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def _forbidden_key(key: str) -> bool:
    words = [word for word in _WORD_SPLIT.split(key.lower()) if word]
    return any(word.startswith(_SECRET_STEMS) or word in _NETWORK_WORDS for word in words)


def _looks_like_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True


def _check_value(value: Any, where: str) -> None:
    if value is None or isinstance(value, bool | int | float):
        return
    if isinstance(value, str):
        if _looks_like_ip(value):
            raise ValueError(f"{where}: IP addresses must not be written to the audit trail")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{where}: audit detail keys must be strings")
            if _forbidden_key(key):
                raise ValueError(f"{where}: audit detail key not allowed: {key}")
            _check_value(item, f"{where}.{key}")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _check_value(item, where)
        return
    raise TypeError(f"{where}: unsupported audit value type {type(value).__name__}")


def check_details(details: Mapping[str, Any]) -> None:
    """Raise if the details contain a secret-looking key, an IP address, or a value that is not plain JSON."""
    _check_value(details, "details")


def record(
    conn: psycopg.Connection,
    action: str,
    target_type: str,
    target_id: str | None = None,
    actor_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Insert one audit row using the caller's connection, so it shares the caller's transaction."""
    payload = dict(details or {})
    check_details(payload)
    conn.execute(
        "INSERT INTO audit_events (actor_id, action, target_type, target_id, details) VALUES (%s, %s, %s, %s, %s)",
        (actor_id, action, target_type, target_id, Jsonb(payload)),
    )


def record_login_failure(user_id: str | None, reason: str) -> None:
    """Log a failed login. The username and the client address are deliberately not stored.

    user_id is None when no account matches the submitted name. The reason is one of
    unknown_user, account_inactive, or wrong_password.
    """
    with connect() as conn:
        record(conn, LOGIN_FAILED, "user", target_id=user_id, details={"reason": reason})
