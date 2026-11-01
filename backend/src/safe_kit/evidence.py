"""Evidence items: validation, upload, verified download, chain of custody, and reconciliation.

Upload is two steps so that a crash never leaves an unexplained object or a row that claims to be stored:
  1. insert the row as pending, with the wrapped data key, nonce, and key id;
  2. write the encrypted object, then set the row to available with both checksums, and record the custody event
     and the audit row in the same transaction.
If the write fails, the row is marked failed. A pending row left behind by a crash is marked failed by
fail_stale_pending. Nothing is deleted.

Download decrypts only after the ciphertext checksum matches. The plaintext checksum is checked before any byte is
returned. A failed check records a "failed" custody event and the caller gets no content.

File contents, keys, and file names never go to logs or to audit details.
"""

import json
import os
import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from safe_kit import audit
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.evidence_crypto import IntegrityError, open_sealed, seal, sha256_hex
from safe_kit.evidence_keys import KeyProvider, KeyProviderError
from safe_kit.evidence_storage import EvidenceStorage, ObjectNotFound, StorageError

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
MAX_FILENAME_LENGTH = 255
MAX_SUFFIX_LENGTH = 16
DEFAULT_PENDING_MINUTES = 30

PENDING = "pending"
AVAILABLE = "available"
FAILED = "failed"

UPLOADED = "uploaded"
DOWNLOADED = "downloaded"
VERIFIED = "verified"
FAILED_EVENT = "failed"

EVIDENCE_UPLOADED = audit.EVIDENCE_UPLOADED
EVIDENCE_DOWNLOADED = audit.EVIDENCE_DOWNLOADED
EVIDENCE_TARGET = "evidence"

_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f"<>:|?*\\/]')


class UnsupportedMediaType(Exception):
    """The declared media type is not on the allowlist, or the content does not match it."""


class EmptyEvidence(Exception):
    """The uploaded file has no content."""


class UploadExpired(Exception):
    """The pending row was marked failed (by reconciliation) before the upload finished."""


class VerificationFailed(Exception):
    """The stored object does not match its record. reason is recorded in the custody log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Declared media type -> check that the bytes look like that type. The declared type alone is not trusted.
def _is_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def _is_png(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n")


def _is_jpeg(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8\xff")


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _is_json(data: bytes) -> bool:
    if not _is_text(data):
        return False
    try:
        json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return False
    return True


ALLOWED_MEDIA_TYPES: dict[str, Callable[[bytes], bool]] = {
    "application/pdf": _is_pdf,
    "image/png": _is_png,
    "image/jpeg": _is_jpeg,
    "text/plain": _is_text,
    "application/json": _is_json,
}


def max_bytes_from_env() -> int:
    raw = os.environ.get("SAFE_EVIDENCE_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    value = int(raw)
    if value <= 0:
        raise ValueError("SAFE_EVIDENCE_MAX_BYTES must be positive")
    return value


def sanitize_filename(raw: str | None) -> str:
    """A display name that is safe to store and to put in a header. It is never used as a path."""
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", name)
    name = _UNSAFE_NAME_CHARS.sub("_", name)
    name = " ".join(name.split()).strip(" .")
    if not name:
        name = "evidence"
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, suffix = name.rpartition(".")
        if dot and 0 < len(suffix) <= MAX_SUFFIX_LENGTH:
            name = name[: MAX_FILENAME_LENGTH - len(suffix) - 1] + "." + suffix
        else:
            name = name[:MAX_FILENAME_LENGTH]
    return name


def check_media_type(declared: str | None, data: bytes) -> str:
    """Return the normalized media type, or raise UnsupportedMediaType. Both the declared type and the bytes must match."""
    if not declared:
        raise UnsupportedMediaType("media type is missing")
    media_type = declared.split(";", 1)[0].strip().lower()
    checker = ALLOWED_MEDIA_TYPES.get(media_type)
    if checker is None or not checker(data):
        raise UnsupportedMediaType("media type is not allowed or does not match the content")
    return media_type


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    case_id: str
    uploaded_by: str
    original_filename: str
    media_type: str
    size_bytes: int
    sha256_plaintext: str | None
    status: str
    created_at: datetime


@dataclass(frozen=True)
class StoredEvidence:
    id: str
    case_id: str
    original_filename: str
    media_type: str
    sha256_plaintext: str | None
    sha256_ciphertext: str | None
    storage_key: str
    key_id: str
    wrapped_data_key: bytes
    nonce: bytes
    status: str


@dataclass(frozen=True)
class CustodyEvent:
    id: str
    evidence_id: str
    actor_id: str | None
    action: str
    details: dict[str, Any]
    created_at: datetime


_ITEM_COLUMNS = (
    "id::text, case_id::text, uploaded_by::text, original_filename, media_type, size_bytes, "
    "sha256_plaintext, status, created_at"
)


def _to_item(row: tuple) -> EvidenceItem:
    return EvidenceItem(
        id=row[0],
        case_id=row[1],
        uploaded_by=row[2],
        original_filename=row[3],
        media_type=row[4],
        size_bytes=row[5],
        sha256_plaintext=row[6],
        status=row[7],
        created_at=row[8],
    )


def _custody(conn, evidence_id: str, actor_id: str | None, action: str, details: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO custody_events (evidence_id, actor_id, action, details) VALUES (%s, %s, %s, %s)",
        (evidence_id, actor_id, action, Jsonb(details)),
    )


def case_visible(case_id: str, scope: CaseScope) -> bool:
    """True when the case exists, is not deleted, and the caller's case scope includes it (case ACL in cases.py)."""
    visibility, params = scope.visibility_sql()
    with connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM cases WHERE id = %s AND NOT is_deleted AND {visibility}",
            (case_id, *params),
        ).fetchone()
    return row is not None


def create_evidence(
    case_id: str,
    uploader_id: str,
    filename: str | None,
    media_type: str,
    data: bytes,
    storage: EvidenceStorage,
    provider: KeyProvider,
) -> EvidenceItem:
    """Encrypt, store, and record one evidence object. The caller has already checked size and media type.

    Raises StorageError or KeyProviderError when the object cannot be stored. The row is then marked failed.
    """
    name = sanitize_filename(filename)
    storage_key = str(uuid.uuid4())
    sealed = seal(data, storage_key, provider)
    evidence_id = _insert_pending(
        case_id, uploader_id, name, media_type, len(data), storage_key, sealed.key_id,
        sealed.wrapped_data_key, sealed.nonce,
    )
    try:
        storage.put(storage_key, sealed.ciphertext)
    except Exception:
        _mark_failed(evidence_id, uploader_id, "storage_write_failed")
        raise
    return _mark_available(
        evidence_id, uploader_id, sha256_hex(data), sha256_hex(sealed.ciphertext),
    )


def _insert_pending(
    case_id: str,
    uploader_id: str,
    name: str,
    media_type: str,
    size_bytes: int,
    storage_key: str,
    key_id: str,
    wrapped_data_key: bytes,
    nonce: bytes,
) -> str:
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO evidence_items
                (case_id, uploaded_by, original_filename, media_type, size_bytes, storage_key,
                 key_id, wrapped_data_key, nonce, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id::text
            """,
            (case_id, uploader_id, name, media_type, size_bytes, storage_key, key_id, wrapped_data_key, nonce),
        ).fetchone()
    return row[0]


def _mark_available(evidence_id: str, uploader_id: str, sha_plaintext: str, sha_ciphertext: str) -> EvidenceItem:
    with connect() as conn:
        row = conn.execute(
            f"""
            UPDATE evidence_items
            SET status = 'available', sha256_plaintext = %s, sha256_ciphertext = %s
            WHERE id = %s AND status = 'pending' AND NOT is_deleted
            RETURNING {_ITEM_COLUMNS}
            """,
            (sha_plaintext, sha_ciphertext, evidence_id),
        ).fetchone()
        if row is None:
            raise UploadExpired()
        item = _to_item(row)
        _custody(conn, evidence_id, uploader_id, UPLOADED, {})
        audit.record(
            conn,
            EVIDENCE_UPLOADED,
            EVIDENCE_TARGET,
            evidence_id,
            actor_id=uploader_id,
            details={"case_id": item.case_id},
        )
    return item


def _mark_failed(evidence_id: str, actor_id: str | None, reason: str) -> None:
    with connect() as conn:
        row = conn.execute(
            "UPDATE evidence_items SET status = 'failed' WHERE id = %s AND status = 'pending' RETURNING id",
            (evidence_id,),
        ).fetchone()
        if row is not None:
            _custody(conn, evidence_id, actor_id, FAILED_EVENT, {"reason": reason})


def find_stored(evidence_id: str, scope: CaseScope) -> StoredEvidence | None:
    """The record needed to read an object, if its case is visible to the scope. None when missing, deleted, or hidden."""
    visibility, params = scope.visibility_sql()
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT e.id::text, e.case_id::text, e.original_filename, e.media_type,
                   e.sha256_plaintext, e.sha256_ciphertext, e.storage_key, e.key_id, e.wrapped_data_key,
                   e.nonce, e.status
            FROM evidence_items e
            JOIN cases ON cases.id = e.case_id AND NOT cases.is_deleted
            WHERE e.id = %s AND NOT e.is_deleted AND {visibility}
            """,
            (evidence_id, *params),
        ).fetchone()
    if row is None:
        return None
    return StoredEvidence(
        id=row[0],
        case_id=row[1],
        original_filename=row[2],
        media_type=row[3],
        sha256_plaintext=row[4],
        sha256_ciphertext=row[5],
        storage_key=row[6],
        key_id=row[7],
        wrapped_data_key=bytes(row[8]),
        nonce=bytes(row[9]),
        status=row[10],
    )


def read_original(stored: StoredEvidence, storage: EvidenceStorage, provider: KeyProvider) -> bytes:
    """Return the verified plaintext. Raises VerificationFailed with a reason; no partial content is returned."""
    try:
        ciphertext = storage.get(stored.storage_key)
    except ObjectNotFound:
        raise VerificationFailed("object_missing") from None
    except StorageError:
        raise VerificationFailed("storage_read_failed") from None
    if sha256_hex(ciphertext) != stored.sha256_ciphertext:
        raise VerificationFailed("ciphertext_checksum_mismatch")
    try:
        plaintext = open_sealed(
            ciphertext, stored.nonce, stored.wrapped_data_key, stored.key_id, stored.storage_key, provider,
        )
    except IntegrityError as error:
        raise VerificationFailed(error.reason) from None
    except KeyProviderError:
        raise VerificationFailed("key_unavailable") from None
    if sha256_hex(plaintext) != stored.sha256_plaintext:
        raise VerificationFailed("plaintext_checksum_mismatch")
    return plaintext


def record_download(evidence_id: str, actor_id: str, case_id: str) -> None:
    """The read marker: the custody event and the audit row commit together. The caller returns content only after this."""
    with connect() as conn:
        _custody(conn, evidence_id, actor_id, DOWNLOADED, {})
        audit.record(
            conn,
            EVIDENCE_DOWNLOADED,
            EVIDENCE_TARGET,
            evidence_id,
            actor_id=actor_id,
            details={"case_id": case_id},
        )


def record_failure(evidence_id: str, actor_id: str | None, reason: str) -> None:
    with connect() as conn:
        _custody(conn, evidence_id, actor_id, FAILED_EVENT, {"reason": reason})


def list_evidence(case_id: str, limit: int, offset: int) -> tuple[list[EvidenceItem], int]:
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_ITEM_COLUMNS} FROM evidence_items
            WHERE case_id = %s AND NOT is_deleted
            ORDER BY created_at, id
            LIMIT %s OFFSET %s
            """,
            (case_id, limit, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT count(*) FROM evidence_items WHERE case_id = %s AND NOT is_deleted",
            (case_id,),
        ).fetchone()[0]
    return [_to_item(row) for row in rows], total


def custody_events(evidence_id: str) -> list[CustodyEvent]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id::text, evidence_id::text, actor_id::text, action, details, created_at
            FROM custody_events
            WHERE evidence_id = %s AND NOT is_deleted
            ORDER BY created_at, id
            """,
            (evidence_id,),
        ).fetchall()
    return [
        CustodyEvent(
            id=row[0], evidence_id=row[1], actor_id=row[2], action=row[3], details=row[4], created_at=row[5],
        )
        for row in rows
    ]


def fail_stale_pending(older_than_minutes: int = DEFAULT_PENDING_MINUTES) -> list[str]:
    """Mark pending rows older than the limit as failed, with a custody event. Returns their ids. Deletes nothing."""
    if older_than_minutes <= 0:
        raise ValueError("older_than_minutes must be positive")
    with connect() as conn:
        rows = conn.execute(
            """
            UPDATE evidence_items SET status = 'failed'
            WHERE status = 'pending' AND NOT is_deleted AND created_at < now() - make_interval(mins => %s::int)
            RETURNING id::text
            """,
            (older_than_minutes,),
        ).fetchall()
        for (evidence_id,) in rows:
            _custody(conn, evidence_id, None, FAILED_EVENT, {"reason": "stale_pending"})
    return [row[0] for row in rows]
