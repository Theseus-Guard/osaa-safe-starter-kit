"""Suggestions, verification runs, and the versioned catalog (plans/f0/api-draft.md section 2.6; R10 and R11).

A recommendation is a suggestion. A verification run is a separate record that checked one recommendation, with its own
subject, scope, assumptions, evidence path, failure behaviour, error class, uncertainty, method, result, and checker.
A recommendation has no result field, so it can never be stored as verified. An entry shows "verified" only when the
verification run it links to passed.

A catalog entry is an identity. Its content is in catalog_versions, which is insert-only. Every operation (create,
version, correction, dispute, withdrawal, supersession) appends the next version in the same transaction as its audit
row and its idempotency record. A withdrawn or superseded entry accepts no further operation, and its history stays.

A restricted reference (for example a link to a restricted report) and its digest live in catalog_restricted_references.
Reads take them only when the caller asks for the restricted view. The public export reads the public columns alone and
never joins that table, so it cannot carry a restricted reference or its digest.

The catalog belongs to the whole organisation. It has no case scope: access is by permission (catalog:read,
catalog:write, catalog:read_restricted), and every route needs a login.
"""

import base64
import binascii
import hashlib
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from safe_kit import audit
from safe_kit.db import connect
from safe_kit.idempotency import (
    KeyLookup,
    KeyScope,
    StoredResponse,
    lock_scope,
    lookup,
    store,
)

TITLE_LIMIT = 200
BODY_LIMIT = 4000
REASON_LIMIT = 1000
FIELD_LIMIT = 1000
REFERENCE_LIMIT = 2000
# A withdrawn or superseded entry is closed: no further version, correction, dispute, withdrawal, or supersession.
CLOSED_STATUSES = frozenset({"withdrawn", "superseded"})
# A replacement must still be open. A disputed entry can still be replaced.
OPEN_STATUSES = frozenset({"active", "disputed"})

AUDIT_ACTION_BY_OPERATION = {
    "version": audit.CATALOG_ENTRY_VERSION,
    "correction": audit.CATALOG_ENTRY_CORRECTION,
    "dispute": audit.CATALOG_ENTRY_DISPUTE,
    "withdrawal": audit.CATALOG_ENTRY_WITHDRAWAL,
    "supersession": audit.CATALOG_ENTRY_SUPERSESSION,
}


class CatalogOutcome(Enum):
    OK = "ok"
    REPLAY = "replay"  # the same key and fingerprint: the stored response is returned, nothing is written
    KEY_REUSED = (
        "key_reused"  # the same key with another request: refused, nothing is returned
    )
    NOT_FOUND = "not_found"
    ENTRY_CLOSED = "entry_closed"
    VERSION_CONFLICT = "version_conflict"  # value holds the current version
    UNKNOWN_VERIFICATION_RUN = "unknown_verification_run"
    INVALID_REPLACEMENT = "invalid_replacement"


@dataclass(frozen=True)
class Result:
    outcome: CatalogOutcome
    value: Any = None
    stored: StoredResponse | None = None


@dataclass(frozen=True)
class Recommendation:
    id: str
    failure_or_system: str
    required_defence_outcome: str
    minimum_control: str
    control_alternative: str | None
    responsible: str
    due_at: datetime
    evidence_to_keep: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class VerificationRun:
    id: str
    recommendation_id: str
    subject: str
    scope: str
    assumptions: str
    evidence_path: str
    failure_behavior: str
    error_class: str
    uncertainty: str
    method: str
    result: str
    verified_by: str
    verified_at: datetime


@dataclass(frozen=True)
class RunSummary:
    """The part of a verification run that a catalog version shows. The checker and the evidence path stay internal."""

    run_id: str
    subject: str
    method: str
    result: str
    verified_at: datetime


@dataclass(frozen=True)
class RestrictedReference:
    reference: str
    digest: str


@dataclass(frozen=True)
class Version:
    entry_id: str
    version: int
    operation: str
    status: str
    title: str
    body: str
    reason: str | None
    replacement_entry_id: str | None
    created_by: str
    created_at: datetime
    verification: RunSummary | None
    restricted: RestrictedReference | None = None


@dataclass(frozen=True)
class Entry:
    id: str
    created_by: str
    created_at: datetime
    # The full history (oldest first) on a detail read. One element, the current version, on a list read or a write.
    versions: tuple[Version, ...]

    @property
    def current(self) -> Version:
        return self.versions[-1]


def digest_of(reference: str) -> str:
    """SHA-256 of a restricted reference, lower-case hex. Stored with the reference; never part of a public export."""
    return hashlib.sha256(reference.encode("utf-8")).hexdigest()


# --- idempotency -------------------------------------------------------------------------------------------------------


def _replay_or_refuse(conn, idempotency: KeyScope | None) -> Result | None:
    if idempotency is None:
        return None
    lock_scope(conn, idempotency)
    found, stored = lookup(conn, idempotency)
    if found is KeyLookup.REPLAY:
        return Result(CatalogOutcome.REPLAY, stored=stored)
    if found is KeyLookup.REUSED:
        return Result(CatalogOutcome.KEY_REUSED)
    return None


def _remember(
    conn, idempotency: KeyScope | None, render: Callable[[Any], Any] | None, value: Any
) -> None:
    """Store the answer of a successful request under its key, in the same transaction."""
    if idempotency is not None and render is not None:
        store(conn, idempotency, StoredResponse(status_code=201, body=render(value)))


# --- recommendations and verification runs ----------------------------------------------------------------------------


def add_recommendation(
    values: Mapping[str, Any],
    actor_id: str,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Recommendation], Any] | None = None,
) -> Result:
    with connect() as conn:
        replay = _replay_or_refuse(conn, idempotency)
        if replay is not None:
            return replay
        row = conn.execute(
            """
            INSERT INTO catalog_recommendations (
                failure_or_system, required_defence_outcome, minimum_control, control_alternative,
                responsible, due_at, evidence_to_keep, created_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id::text, created_at
            """,
            (
                values["failure_or_system"],
                values["required_defence_outcome"],
                values["minimum_control"],
                values["control_alternative"],
                values["responsible"],
                values["due_at"],
                values["evidence_to_keep"],
                actor_id,
            ),
        ).fetchone()
        recommendation = Recommendation(
            id=row[0],
            failure_or_system=values["failure_or_system"],
            required_defence_outcome=values["required_defence_outcome"],
            minimum_control=values["minimum_control"],
            control_alternative=values["control_alternative"],
            responsible=values["responsible"],
            due_at=values["due_at"],
            evidence_to_keep=values["evidence_to_keep"],
            created_by=actor_id,
            created_at=row[1],
        )
        audit.record(
            conn,
            audit.CATALOG_RECOMMENDATION_ADD,
            "catalog_recommendation",
            recommendation.id,
            actor_id=actor_id,
        )
        _remember(conn, idempotency, render, recommendation)
    return Result(CatalogOutcome.OK, recommendation)


def record_verification_run(
    recommendation_id: str,
    values: Mapping[str, Any],
    actor_id: str,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[VerificationRun], Any] | None = None,
) -> Result:
    """Record a verification run of a recommendation. The run is a record of its own; the recommendation is unchanged."""
    with connect() as conn:
        replay = _replay_or_refuse(conn, idempotency)
        if replay is not None:
            return replay
        if (
            conn.execute(
                "SELECT 1 FROM catalog_recommendations WHERE id = %s AND NOT is_deleted",
                (recommendation_id,),
            ).fetchone()
            is None
        ):
            return Result(CatalogOutcome.NOT_FOUND)
        row = conn.execute(
            """
            INSERT INTO catalog_verification_runs (
                recommendation_id, subject, scope, assumptions, evidence_path, failure_behavior, error_class,
                uncertainty, method, result, verified_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id::text, verified_at
            """,
            (
                recommendation_id,
                values["subject"],
                values["scope"],
                values["assumptions"],
                values["evidence_path"],
                values["failure_behavior"],
                values["error_class"],
                values["uncertainty"],
                values["method"],
                values["result"],
                actor_id,
            ),
        ).fetchone()
        run = VerificationRun(
            id=row[0],
            recommendation_id=recommendation_id,
            subject=values["subject"],
            scope=values["scope"],
            assumptions=values["assumptions"],
            evidence_path=values["evidence_path"],
            failure_behavior=values["failure_behavior"],
            error_class=values["error_class"],
            uncertainty=values["uncertainty"],
            method=values["method"],
            result=values["result"],
            verified_by=actor_id,
            verified_at=row[1],
        )
        audit.record(
            conn,
            audit.CATALOG_VERIFICATION_RECORD,
            "catalog_recommendation",
            recommendation_id,
            actor_id=actor_id,
            details={"run_id": run.id, "result": run.result},
        )
        _remember(conn, idempotency, render, run)
    return Result(CatalogOutcome.OK, run)


def _run_exists(conn, run_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM catalog_verification_runs WHERE id = %s AND NOT is_deleted",
            (run_id,),
        ).fetchone()
        is not None
    )


# --- entries and versions ---------------------------------------------------------------------------------------------


def _append_version(
    conn,
    entry_id: str,
    *,
    operation: str,
    status: str,
    title: str,
    body: str,
    actor_id: str,
    reason: str | None = None,
    replacement_entry_id: str | None = None,
    verification_run_id: str | None = None,
    previous_version_id: str | None = None,
    reference: str | None = None,
) -> tuple[int, str]:
    """Insert the next version of an entry. A new reference is stored; without one, the previous version's is carried."""
    number = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM catalog_versions WHERE entry_id = %s",
        (entry_id,),
    ).fetchone()[0]
    version_id = conn.execute(
        """
        INSERT INTO catalog_versions (
            entry_id, version, operation, status, title, body, reason, replacement_entry_id,
            verification_run_id, created_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id::text
        """,
        (
            entry_id,
            number,
            operation,
            status,
            title,
            body,
            reason,
            replacement_entry_id,
            verification_run_id,
            actor_id,
        ),
    ).fetchone()[0]
    if reference is not None:
        conn.execute(
            """
            INSERT INTO catalog_restricted_references (version_id, reference, digest, created_by)
            VALUES (%s, %s, %s, %s)
            """,
            (version_id, reference, digest_of(reference), actor_id),
        )
    elif previous_version_id is not None:
        conn.execute(
            """
            INSERT INTO catalog_restricted_references (version_id, reference, digest, created_by)
            SELECT %s, reference, digest, %s
            FROM catalog_restricted_references
            WHERE version_id = %s AND NOT is_deleted
            """,
            (version_id, actor_id, previous_version_id),
        )
    return number, version_id


def create_entry(
    values: Mapping[str, Any],
    actor_id: str,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Entry], Any] | None = None,
) -> Result:
    with connect() as conn:
        replay = _replay_or_refuse(conn, idempotency)
        if replay is not None:
            return replay
        run_id = values.get("verification_run_id")
        if run_id is not None and not _run_exists(conn, run_id):
            return Result(CatalogOutcome.UNKNOWN_VERIFICATION_RUN)
        entry_id = conn.execute(
            "INSERT INTO catalog_entries (created_by) VALUES (%s) RETURNING id::text",
            (actor_id,),
        ).fetchone()[0]
        _append_version(
            conn,
            entry_id,
            operation="create",
            status="active",
            title=values["title"],
            body=values["body"],
            actor_id=actor_id,
            verification_run_id=run_id,
            reference=values.get("reference"),
        )
        audit.record(
            conn,
            audit.CATALOG_ENTRY_CREATE,
            "catalog_entry",
            entry_id,
            actor_id=actor_id,
            details={"version": 1},
        )
        entry = _load_entry(conn, entry_id, with_restricted=False, history=False)
        _remember(conn, idempotency, render, entry)
    return Result(CatalogOutcome.OK, entry)


def change_entry(
    entry_id: str,
    expected_version: int,
    operation: str,
    values: Mapping[str, Any],
    actor_id: str,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Entry], Any] | None = None,
) -> Result:
    """Append one version to an entry: version, correction, dispute, withdrawal, or supersession.

    expected_version is the version the caller read. A different current version is a conflict and nothing is stored.
    A version or correction carries a verification run only when the caller names one: a new content version does not
    inherit the verification of the old content. A dispute, withdrawal, or supersession keeps the content and its run.
    """
    with connect() as conn:
        replay = _replay_or_refuse(conn, idempotency)
        if replay is not None:
            return replay
        replacement_id = values.get("replacement_entry_id")
        # Lock the entry and the replacement together, in identifier order, so two crossing supersessions cannot deadlock.
        lock_ids = sorted({entry_id, replacement_id} - {None})
        locked = {
            row[0]
            for row in conn.execute(
                "SELECT id::text FROM catalog_entries WHERE id = ANY(%s::uuid[]) AND NOT is_deleted ORDER BY id FOR UPDATE",
                (lock_ids,),
            ).fetchall()
        }
        if entry_id not in locked:
            return Result(CatalogOutcome.NOT_FOUND)
        current = conn.execute(
            """
            SELECT version, status, title, body, verification_run_id::text, id::text
            FROM catalog_versions
            WHERE entry_id = %s AND NOT is_deleted
            ORDER BY version DESC
            LIMIT 1
            """,
            (entry_id,),
        ).fetchone()
        (
            current_version,
            current_status,
            current_title,
            current_body,
            current_run,
            current_version_id,
        ) = current
        if current_status in CLOSED_STATUSES:
            return Result(CatalogOutcome.ENTRY_CLOSED)
        if current_version != expected_version:
            return Result(CatalogOutcome.VERSION_CONFLICT, value=current_version)
        run_id = values.get("verification_run_id")
        if run_id is not None and not _run_exists(conn, run_id):
            return Result(CatalogOutcome.UNKNOWN_VERIFICATION_RUN)
        if operation == "supersession":
            if replacement_id == entry_id or replacement_id not in locked:
                return Result(CatalogOutcome.INVALID_REPLACEMENT)
            replacement_status = conn.execute(
                """
                SELECT status FROM catalog_versions
                WHERE entry_id = %s AND NOT is_deleted
                ORDER BY version DESC
                LIMIT 1
                """,
                (replacement_id,),
            ).fetchone()[0]
            if replacement_status not in OPEN_STATUSES:
                return Result(CatalogOutcome.INVALID_REPLACEMENT)

        if operation in ("version", "correction"):
            status, title, body, run = "active", values["title"], values["body"], run_id
        elif operation == "dispute":
            status, title, body, run = (
                "disputed",
                current_title,
                current_body,
                current_run,
            )
        elif operation == "withdrawal":
            status, title, body, run = (
                "withdrawn",
                current_title,
                current_body,
                current_run,
            )
        else:  # supersession
            status, title, body, run = (
                "superseded",
                current_title,
                current_body,
                current_run,
            )

        new_version, _ = _append_version(
            conn,
            entry_id,
            operation=operation,
            status=status,
            title=title,
            body=body,
            actor_id=actor_id,
            reason=values.get("reason"),
            replacement_entry_id=replacement_id
            if operation == "supersession"
            else None,
            verification_run_id=run,
            previous_version_id=current_version_id,
            reference=values.get("reference"),
        )
        audit.record(
            conn,
            AUDIT_ACTION_BY_OPERATION[operation],
            "catalog_entry",
            entry_id,
            actor_id=actor_id,
            details={"version": {"old": current_version, "new": new_version}},
        )
        entry = _load_entry(conn, entry_id, with_restricted=False, history=False)
        _remember(conn, idempotency, render, entry)
    return Result(CatalogOutcome.OK, entry)


# --- reads ------------------------------------------------------------------------------------------------------------

_VERSION_COLUMNS = (
    "v.entry_id::text, v.version, v.operation, v.status, v.title, v.body, v.reason, v.replacement_entry_id::text, "
    "v.created_by::text, v.created_at, vr.id::text, vr.subject, vr.method, vr.result, vr.verified_at"
)
_VERSION_FROM = "FROM catalog_versions v LEFT JOIN catalog_verification_runs vr ON vr.id = v.verification_run_id AND NOT vr.is_deleted"
_RESTRICTED_COLUMNS = ", rr.reference, rr.digest"
_RESTRICTED_JOIN = " LEFT JOIN catalog_restricted_references rr ON rr.version_id = v.id AND NOT rr.is_deleted"
# The current version of each entry, used by the list and the write responses.
_CURRENT_ONLY = "v.version = (SELECT MAX(v2.version) FROM catalog_versions v2 WHERE v2.entry_id = v.entry_id AND NOT v2.is_deleted)"


def _load_versions(
    conn, where_sql: str, params: tuple, *, with_restricted: bool
) -> list[Version]:
    """Load versions. Without with_restricted the query does not touch catalog_restricted_references at all.

    where_sql holds only constant SQL built in this module; values are passed as parameters.
    """
    columns = _VERSION_COLUMNS + (_RESTRICTED_COLUMNS if with_restricted else "")
    join = _RESTRICTED_JOIN if with_restricted else ""
    rows = conn.execute(
        f"SELECT {columns} {_VERSION_FROM}{join} WHERE NOT v.is_deleted AND {where_sql} ORDER BY v.entry_id, v.version",
        params,
    ).fetchall()
    return [_version_from_row(row, with_restricted) for row in rows]


def _version_from_row(row: tuple, with_restricted: bool) -> Version:
    verification = None
    if row[10] is not None:
        verification = RunSummary(
            run_id=row[10],
            subject=row[11],
            method=row[12],
            result=row[13],
            verified_at=row[14],
        )
    restricted = None
    if with_restricted and row[15] is not None:
        restricted = RestrictedReference(reference=row[15], digest=row[16])
    return Version(
        entry_id=row[0],
        version=row[1],
        operation=row[2],
        status=row[3],
        title=row[4],
        body=row[5],
        reason=row[6],
        replacement_entry_id=row[7],
        created_by=row[8],
        created_at=row[9],
        verification=verification,
        restricted=restricted,
    )


def _load_entry(
    conn, entry_id: str, *, with_restricted: bool, history: bool
) -> Entry | None:
    row = conn.execute(
        "SELECT id::text, created_by::text, created_at FROM catalog_entries WHERE id = %s AND NOT is_deleted",
        (entry_id,),
    ).fetchone()
    if row is None:
        return None
    where_sql = "v.entry_id = %s" if history else f"v.entry_id = %s AND {_CURRENT_ONLY}"
    versions = _load_versions(
        conn, where_sql, (entry_id,), with_restricted=with_restricted
    )
    return Entry(
        id=row[0], created_by=row[1], created_at=row[2], versions=tuple(versions)
    )


def get_entry(entry_id: str, *, with_restricted: bool) -> Entry | None:
    """The full history of an entry, or None when it does not exist."""
    with connect() as conn:
        return _load_entry(
            conn, entry_id, with_restricted=with_restricted, history=True
        )


def encode_cursor(created_at: datetime, entry_id: str) -> str:
    raw = f"{created_at.isoformat()}|{entry_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str] | None:
    """The keyset position of a cursor, or None when the cursor is not one this module issued."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        created_text, entry_text = text.split("|", 1)
        created_at = datetime.fromisoformat(created_text)
        uuid.UUID(entry_text)
    except (binascii.Error, UnicodeError, ValueError):
        return None
    return created_at, entry_text


def list_entries(
    limit: int, cursor: str | None, *, with_restricted: bool
) -> tuple[list[Entry], str | None] | None:
    """One page of entries, each with its current version, in (created_at, id) order. None for an invalid cursor."""
    position = None
    if cursor is not None:
        position = decode_cursor(cursor)
        if position is None:
            return None
    with connect() as conn:
        if position is None:
            rows = conn.execute(
                "SELECT id::text, created_by::text, created_at FROM catalog_entries WHERE NOT is_deleted "
                "ORDER BY created_at, id LIMIT %s",
                (limit + 1,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id::text, created_by::text, created_at FROM catalog_entries "
                "WHERE NOT is_deleted AND (created_at, id) > (%s, %s) ORDER BY created_at, id LIMIT %s",
                (position[0], position[1], limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        ids = [row[0] for row in rows]
        versions_by_entry: dict[str, list[Version]] = defaultdict(list)
        if ids:
            for version in _load_versions(
                conn,
                f"v.entry_id = ANY(%s::uuid[]) AND {_CURRENT_ONLY}",
                (ids,),
                with_restricted=with_restricted,
            ):
                versions_by_entry[version.entry_id].append(version)
    entries = [
        Entry(
            id=row[0],
            created_by=row[1],
            created_at=row[2],
            versions=tuple(versions_by_entry[row[0]]),
        )
        for row in rows
    ]
    next_cursor = encode_cursor(rows[-1][2], rows[-1][0]) if has_more and rows else None
    return entries, next_cursor


def public_export() -> dict[str, Any]:
    """A public derivative of the catalog: every entry with its public history. Never a restricted reference or digest.

    It reads the public columns only (see _load_versions with with_restricted=False), and it omits the internal fields:
    the creator and checker identifiers, the evidence path, the recommendation, and the run identifier.
    """
    with connect() as conn:
        entry_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id::text FROM catalog_entries WHERE NOT is_deleted ORDER BY created_at, id"
            ).fetchall()
        ]
        versions = _load_versions(conn, "TRUE", (), with_restricted=False)
    history: dict[str, list[Version]] = defaultdict(list)
    for version in versions:
        history[version.entry_id].append(version)
    entries = []
    for entry_id in entry_ids:
        entry_versions = history.get(entry_id)
        if not entry_versions:
            continue
        current = entry_versions[-1]
        entries.append(
            {
                "id": entry_id,
                "status": current.status,
                "version": current.version,
                "versions": [_public_version(version) for version in entry_versions],
            }
        )
    return {"catalog_export": {"entries": entries}}


def _public_version(version: Version) -> dict[str, Any]:
    verification = None
    if version.verification is not None:
        verification = {
            "subject": version.verification.subject,
            "method": version.verification.method,
            "result": version.verification.result,
            "verified_at": version.verification.verified_at.isoformat(),
        }
    return {
        "version": version.version,
        "operation": version.operation,
        "status": version.status,
        "title": version.title,
        "body": version.body,
        "reason": version.reason,
        "replacement_entry_id": version.replacement_entry_id,
        "created_at": version.created_at.isoformat(),
        "verification": verification,
    }
