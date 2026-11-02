"""Idempotency-Key handling for side-effect POST requests (plans/f0/api-draft.md section 3.4).

A key is scoped to (principal, method, route template, key). Its record holds the request fingerprint and the stored
response. The record is written in the same transaction as the domain change, so a refused or failed request stores
nothing and can be retried safely.

Concurrent requests with the same scope are serialised by a transaction-level advisory lock taken before the record
is read. The second request then sees the first one's committed record and replays it.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

KEY_PATTERN = re.compile(r"^[!-~]{1,128}$")
REPLAY_WINDOW = timedelta(days=7)


class KeyLookup(Enum):
    NEW = "new"  # no live record: the request runs and its response is stored
    REPLAY = "replay"  # the same key and the same fingerprint: the stored response is returned
    REUSED = "reused"  # the same key with a different fingerprint: refused, nothing is returned


@dataclass(frozen=True)
class KeyScope:
    principal_id: str
    method: str
    route: str
    key: str
    fingerprint: str


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: Any


def valid_key(value: str | None) -> bool:
    return value is not None and KEY_PATTERN.fullmatch(value) is not None


def fingerprint(method: str, path: str, query: str, body: Any) -> str:
    """SHA-256 of the normalised request: method, path, query string, and the JSON body with sorted keys."""
    canonical = json.dumps(
        {"method": method, "path": path, "query": query, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lock_scope(conn, scope: KeyScope) -> None:
    """Serialise requests with the same scope until the caller's transaction ends."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"idempotency|{scope.principal_id}|{scope.method}|{scope.route}|{scope.key}",),
    )


def lookup(conn, scope: KeyScope) -> tuple[KeyLookup, StoredResponse | None]:
    """Find the live record of the scope. A record older than the replay window is soft-deleted, and the request is new."""
    row = conn.execute(
        """
        SELECT id::text, fingerprint, response_status, response_body, created_at < %s
        FROM idempotency_keys
        WHERE principal_id = %s AND method = %s AND route = %s AND key = %s AND NOT is_deleted
        """,
        (
            datetime.now(UTC) - REPLAY_WINDOW,
            scope.principal_id,
            scope.method,
            scope.route,
            scope.key,
        ),
    ).fetchone()
    if row is None:
        return KeyLookup.NEW, None
    record_id, stored_fingerprint, status_code, body, expired = row
    if expired:
        conn.execute(
            "UPDATE idempotency_keys SET is_deleted = true, deleted_at = now() WHERE id = %s",
            (record_id,),
        )
        return KeyLookup.NEW, None
    if stored_fingerprint != scope.fingerprint:
        return KeyLookup.REUSED, None
    return KeyLookup.REPLAY, StoredResponse(status_code=status_code, body=body)


def store(conn, scope: KeyScope, response: StoredResponse) -> None:
    """Keep the response of a successful request under its key, in the caller's transaction."""
    conn.execute(
        """
        INSERT INTO idempotency_keys (principal_id, method, route, key, fingerprint, response_status, response_body)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            scope.principal_id,
            scope.method,
            scope.route,
            scope.key,
            scope.fingerprint,
            response.status_code,
            json.dumps(response.body),
        ),
    )
