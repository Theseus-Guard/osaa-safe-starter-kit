"""Append-only audit trail. Administrative changes and login failures each write one row.

Rows are never edited or removed: a database trigger rejects every update except the soft delete.
Details must never hold passwords, tokens, or IP addresses. record() rejects such keys and values,
so a careless caller fails in tests instead of leaking secrets into the log.
The one exception is login_failed, which records the submitted username and the client address.

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
SYSTEM_TIMEZONE_SET = "system.timezone_set"
SYSTEM_TIMEZONE_CHANGED = "system.timezone_changed"

# A key is rejected when any word in it starts with one of these stems, or equals one of the network words.
_SECRET_STEMS = ("password", "passwd", "pwd", "secret", "token", "credential", "cookie", "authorization", "hash")
_NETWORK_WORDS = frozenset({"ip", "ips", "addr", "address"})
# Identity words are rejected like network words, unless an action lists the key explicitly.
_RESTRICTED_WORDS = _NETWORK_WORDS | {"username"}
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")

# Top-level keys that one action may carry in addition to the generic rules. Secret stems are never allowed.
_ACTION_EXTRA_KEYS: dict[str, frozenset[str]] = {LOGIN_FAILED: frozenset({"username", "client_ip"})}
# Of those, the keys whose values may be an IP address. Only the client address of a login failure qualifies.
_ACTION_ADDRESS_KEYS: dict[str, frozenset[str]] = {LOGIN_FAILED: frozenset({"client_ip"})}
_NO_KEYS: frozenset[str] = frozenset()


def _forbidden_key(key: str, extra_allowed: frozenset[str]) -> bool:
    words = [word for word in _WORD_SPLIT.split(key.lower()) if word]
    if any(word.startswith(_SECRET_STEMS) for word in words):
        return True
    return key not in extra_allowed and any(word in _RESTRICTED_WORDS for word in words)


def _looks_like_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True


def _check_key(key: Any, where: str, extra_allowed: frozenset[str] = _NO_KEYS) -> None:
    if not isinstance(key, str):
        raise TypeError(f"{where}: audit detail keys must be strings")
    if _forbidden_key(key, extra_allowed):
        raise ValueError(f"{where}: audit detail key not allowed: {key}")


def _check_value(value: Any, where: str, address_ok: bool = False) -> None:
    if value is None or isinstance(value, bool | int | float):
        return
    if isinstance(value, str):
        if not address_ok and _looks_like_ip(value):
            raise ValueError(f"{where}: IP addresses are allowed only as client_ip of a login failure")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _check_key(key, where)
            _check_value(item, f"{where}.{key}")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _check_value(item, where)
        return
    raise TypeError(f"{where}: unsupported audit value type {type(value).__name__}")


def check_details(details: Mapping[str, Any], action: str | None = None) -> None:
    """Raise if the details contain a secret-looking key, an IP address, or a value that is not plain JSON.

    Without an action, every rule applies in full. With an action, only that action's listed keys are relaxed.
    """
    extra_keys = _ACTION_EXTRA_KEYS.get(action or "", _NO_KEYS)
    address_keys = _ACTION_ADDRESS_KEYS.get(action or "", _NO_KEYS)
    for key, value in details.items():
        _check_key(key, "details", extra_keys)
        _check_value(value, f"details.{key}", address_ok=key in address_keys)


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
    check_details(payload, action)
    conn.execute(
        "INSERT INTO audit_events (actor_id, action, target_type, target_id, details) VALUES (%s, %s, %s, %s, %s)",
        (actor_id, action, target_type, target_id, Jsonb(payload)),
    )


def record_login_failure(user_id: str | None, reason: str, username: str, client_ip: str) -> None:
    """Log a failed login with the account, the submitted username, and the client address.

    user_id is None when no account matches the submitted name. The reason is one of
    unknown_user, account_inactive, or wrong_password. A submitted name that is an IP address is stored as null,
    because the audit rules forbid IP addresses outside client_ip.
    """
    details = {
        "reason": reason,
        "username": None if _looks_like_ip(username) else username,
        "client_ip": client_ip,
    }
    with connect() as conn:
        record(conn, LOGIN_FAILED, "user", target_id=user_id, details=details)
