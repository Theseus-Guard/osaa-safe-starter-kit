"""Basic import of case records in the project-defined, versioned JSON format.

A file is an envelope, {"import_format_version": 1, "records": [...]}. Each record must follow the project schema
(record_schema_version 0.1.0). The schema is project-defined and is not an official SAFE schema. The copy in
schemas/ is the one the service reads, so the container image carries it.

Each record is handled on its own, in its own transaction, so a record is either stored in full or not at all:
- imported: the case, its deadline tasks, the import record, and the audit rows are written together.
- duplicate: the same source case_id was imported before with identical content. Nothing is written.
- conflict: the same source case_id was imported before with different content. Nothing is written, and the
  existing case is not changed.
- rejected: the record fails the schema or a mapping rule. Nothing is written.
- failed: the database failed while the record was being stored. Nothing is written for that record.

Only some fields map onto the case row (see prepare_record). The whole record is kept in case_import_records.payload,
so no field is dropped silently. Evidence items and the other sections wait for the domain tables planned for F1/F2.

preview_records is the dry run: it applies the same validation and the same duplicate and conflict rules as
import_records, reads only, and writes nothing (no case, registry row, audit row or idempotency record).
"""

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

from safe_kit import audit, cases, system_timezone
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.idempotency import KeyScope, check, remember
from safe_kit.system_timezone import TimezoneNotConfigured

IMPORT_FORMAT_VERSION = 1
RECORD_SCHEMA_VERSION = "0.1.0"
MAX_IMPORT_RECORDS = 100
MAX_IMPORT_BYTES = 1024 * 1024
# Limits on what the response carries, so a bad file cannot produce an unbounded reply.
MAX_ERRORS_PER_RECORD = 20
MAX_MESSAGE_LENGTH = 300
TITLE_LIMIT = 200
ENVELOPE_FIELDS = frozenset({"import_format_version", "records"})

SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schemas" / "case-minimum-record.v0.json"
)
_VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


class ImportFileError(Exception):
    """The file as a whole is not an import this version accepts. No record is processed."""


class RecordRejected(Exception):
    """The record fails validation. The errors are shown to the submitter."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class PreparedRecord:
    source_case_id: str
    fingerprint: str
    title: str
    awareness_time: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class RecordResult:
    index: int
    status: str
    source_case_id: str | None = None
    case_id: str | None = None
    reference: str | None = None
    errors: tuple[str, ...] = ()


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not allowed in JSON")


def parse_import_file(raw: bytes) -> list[Any]:
    """Check the envelope and return the records. Each record is checked later, one at a time."""
    try:
        document = json.loads(raw, parse_constant=_reject_constant)
    except ValueError:
        raise ImportFileError("the request body is not valid JSON") from None
    if not isinstance(document, dict):
        raise ImportFileError("the import file must be a JSON object")
    if not set(document) <= ENVELOPE_FIELDS:
        raise ImportFileError(
            "the import file has a field that this version does not accept"
        )
    version = document.get("import_format_version")
    if type(version) is not int or version != IMPORT_FORMAT_VERSION:
        raise ImportFileError(
            f"unsupported import_format_version; this version reads {IMPORT_FORMAT_VERSION}"
        )
    records = document.get("records")
    if not isinstance(records, list):
        raise ImportFileError("records must be a JSON array")
    if len(records) > MAX_IMPORT_RECORDS:
        raise ImportFileError(
            f"an import file may hold at most {MAX_IMPORT_RECORDS} records"
        )
    return records


def _clip(text: str) -> str:
    return (
        text
        if len(text) <= MAX_MESSAGE_LENGTH
        else text[: MAX_MESSAGE_LENGTH - 3] + "..."
    )


def _location(path: list[Any]) -> str:
    return "/" + "/".join(str(part) for part in path) if path else "/"


def validate_record(record: Any) -> list[str]:
    """The schema errors of one record, as 'location: message' lines. An empty list means the schema holds."""
    errors = sorted(
        _VALIDATOR.iter_errors(record),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    return [
        _clip(f"{_location(list(error.absolute_path))}: {error.message}")
        for error in errors[:MAX_ERRORS_PER_RECORD]
    ]


def _parse_utc(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value.astimezone(UTC) if value.tzinfo is not None else None


def fingerprint(record: dict[str, Any]) -> str:
    """SHA-256 of the record in canonical JSON. The same content always gives the same fingerprint."""
    canonical = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prepare_record(record: Any, now: datetime) -> PreparedRecord:
    """Validate one record and map its fields onto the case.

    Mapping: the case title is "<first affected system name>: <incident class>", cut to 200 characters, because the
    record has no title field. The awareness time is timeline.awareness_time.value, and the deadlines are computed from
    it, as for a case submitted through the API. Every other field is kept in the import record payload only.
    """
    errors = validate_record(record)
    if errors:
        raise RecordRejected(errors)
    problems = []
    awareness_time = _parse_utc(record["timeline"]["awareness_time"]["value"])
    if awareness_time is None:
        problems.append("/timeline/awareness_time/value: not a real UTC date and time")
    elif awareness_time > now:
        problems.append("/timeline/awareness_time/value: cannot be in the future")
    system_name = record["affected_systems"][0]["system_name"].strip()
    if not system_name:
        problems.append("/affected_systems/0/system_name: must not be blank")
    if problems:
        raise RecordRejected(problems)
    title = f"{system_name}: {record['incident_class']}"[:TITLE_LIMIT]
    return PreparedRecord(
        source_case_id=str(uuid.UUID(record["case_id"])),
        fingerprint=fingerprint(record),
        title=title,
        awareness_time=awareness_time,
        payload=record,
    )


def import_records(
    records: list[Any],
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[list[RecordResult]], Any] | None = None,
) -> list[RecordResult]:
    """Import the records one at a time and report each result. The caller becomes the creator of every new case.

    A duplicate or conflict names the existing case only when the caller may see it. Otherwise the result carries the
    outcome alone, so an import cannot be used to learn the ids of another reporter's case.

    With an idempotency scope, the key lock and the stored response belong to one transaction that stays open for the
    whole batch. Each record keeps its own transaction, as before, and a record that is already stored is reported as a
    duplicate by source case id. So a batch cut short by a crash is safe to send again, although its response is not
    stored until the batch ends.
    """
    with connect() as conn:
        check(conn, idempotency)
        if system_timezone.read_current(conn) is None:
            raise TimezoneNotConfigured()
        now = datetime.now(UTC)
        results = [
            _import_one(index, record, scope, now)
            for index, record in enumerate(records)
        ]
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 200, render(results))
    return results


@dataclass(frozen=True)
class DryRunResult:
    index: int
    # would_import: the record would be stored. duplicate and conflict: the source case ID is already stored, or an
    # earlier record of the same file holds it. rejected: the record fails the schema or a mapping rule.
    status: str
    source_case_id: str | None = None
    case_id: str | None = None
    reference: str | None = None
    title: str | None = None
    awareness_time: datetime | None = None
    errors: tuple[str, ...] = ()


def received_format_version(raw: bytes) -> int | str | None:
    """The import_format_version that the file declares, for the dry-run report.

    An integer or a string is returned (a string clipped to the message limit). Anything else, and a file that is not a
    JSON object with that field, gives None, so a bad file cannot make the report carry a large value.
    """
    try:
        document = json.loads(raw, parse_constant=_reject_constant)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    value = document.get("import_format_version")
    if type(value) is int:
        return value
    if isinstance(value, str):
        return _clip(value)
    return None


def preview_records(records: list[Any], scope: CaseScope) -> list[DryRunResult]:
    """Report what import_records would do with these records, without storing anything.

    Records that would be stored count for the duplicate and conflict rules of the records after them, as they would
    in a real import, so a file that holds one source case twice is reported the same way the commit would store it.
    """
    with connect() as conn:
        if system_timezone.read_current(conn) is None:
            raise TimezoneNotConfigured()
        now = datetime.now(UTC)
        would_store: dict[str, str] = {}
        return [
            _preview_one(conn, index, record, scope, now, would_store)
            for index, record in enumerate(records)
        ]


def _preview_one(
    conn,
    index: int,
    record: Any,
    scope: CaseScope,
    now: datetime,
    would_store: dict[str, str],
) -> DryRunResult:
    try:
        prepared = prepare_record(record, now)
    except RecordRejected as rejected:
        return DryRunResult(
            index=index, status="rejected", errors=tuple(rejected.errors)
        )
    source = prepared.source_case_id
    if source in would_store:
        status = (
            "duplicate" if would_store[source] == prepared.fingerprint else "conflict"
        )
        return DryRunResult(index=index, status=status, source_case_id=source)
    existing = conn.execute(
        """
        SELECT r.case_id::text, c.reference, r.content_sha256 FROM case_import_records r
        JOIN cases c ON c.id = r.case_id
        WHERE r.source_case_id = %s AND NOT r.is_deleted
        """,
        (source,),
    ).fetchone()
    if existing is not None:
        status = "duplicate" if existing[2] == prepared.fingerprint else "conflict"
        if not cases.is_visible_on(conn, existing[0], scope):
            return DryRunResult(index=index, status=status, source_case_id=source)
        return DryRunResult(
            index=index,
            status=status,
            source_case_id=source,
            case_id=existing[0],
            reference=existing[1],
        )
    would_store[source] = prepared.fingerprint
    return DryRunResult(
        index=index,
        status="would_import",
        source_case_id=source,
        title=prepared.title,
        awareness_time=prepared.awareness_time,
    )


def _import_one(
    index: int, record: Any, scope: CaseScope, now: datetime
) -> RecordResult:
    try:
        prepared = prepare_record(record, now)
    except RecordRejected as rejected:
        return RecordResult(
            index=index, status="rejected", errors=tuple(rejected.errors)
        )
    try:
        return _store(index, prepared, scope)
    except psycopg.Error:
        # The transaction was rolled back, so this record left nothing behind. The batch goes on.
        return RecordResult(
            index=index, status="failed", source_case_id=prepared.source_case_id
        )


def _store(index: int, prepared: PreparedRecord, scope: CaseScope) -> RecordResult:
    with connect() as conn:
        # Serializes imports of the same source case, so two concurrent files cannot both create it.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (prepared.source_case_id,),
        )
        existing = conn.execute(
            """
            SELECT r.case_id::text, c.reference, r.content_sha256 FROM case_import_records r
            JOIN cases c ON c.id = r.case_id
            WHERE r.source_case_id = %s AND NOT r.is_deleted
            """,
            (prepared.source_case_id,),
        ).fetchone()
        if existing is not None:
            status = "duplicate" if existing[2] == prepared.fingerprint else "conflict"
            if not cases.is_visible_on(conn, existing[0], scope):
                return RecordResult(
                    index=index, status=status, source_case_id=prepared.source_case_id
                )
            return RecordResult(
                index=index,
                status=status,
                source_case_id=prepared.source_case_id,
                case_id=existing[0],
                reference=existing[1],
            )
        case = cases.insert_case(
            conn, prepared.title, prepared.awareness_time, scope.user_id
        )
        registry_id = conn.execute(
            """
            INSERT INTO case_import_records (source_case_id, case_id, record_schema_version, content_sha256, payload, imported_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id::text
            """,
            (
                prepared.source_case_id,
                case.id,
                RECORD_SCHEMA_VERSION,
                prepared.fingerprint,
                Jsonb(prepared.payload),
                scope.user_id,
            ),
        ).fetchone()[0]
        audit.record(
            conn,
            audit.CASE_IMPORT,
            "case_import",
            registry_id,
            actor_id=scope.user_id,
            details={
                "source_case_id": prepared.source_case_id,
                "case_id": case.id,
                "record_schema_version": RECORD_SCHEMA_VERSION,
                "content_sha256": prepared.fingerprint,
            },
        )
    return RecordResult(
        index=index,
        status="imported",
        source_case_id=prepared.source_case_id,
        case_id=case.id,
        reference=case.reference,
    )
