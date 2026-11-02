"""Evidence storage tests: envelope encryption, checksums, key providers, storage, uploads, and custody.

Most tests need no database. They use a dictionary-backed storage, a local KEK file in pytest's temporary directory,
and a small FastAPI app that mounts the evidence router with the authentication dependency overridden.
The custody append-only test needs PostgreSQL. It is skipped unless SAFE_TEST_DATABASE=1, like tests/test_database.py.
"""

import base64
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safe_kit import (
    api,
    auth,
    cases,
    evidence,
    evidence_api,
    evidence_crypto,
    evidence_keys,
    evidence_storage,
    migrate,
    system_timezone,
    users,
)
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.evidence_keys import KeyProviderError, LocalFileKeyProvider, VaultTransitKeyProvider
from safe_kit.evidence_storage import InvalidObjectKey, LocalVolumeStorage, ObjectNotFound, StorageError
from safe_kit.users import User

PDF = b"%PDF-1.7\n" + b"incident detail " * 60 + b"\n%%EOF\n"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


# --- helpers -----------------------------------------------------------------------------------------------------


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


def write_kek(path: Path, raw: bytes | None = None, mode: int = 0o600) -> Path:
    path.write_bytes(raw if raw is not None else os.urandom(32))
    os.chmod(path, mode)
    return path


@pytest.fixture
def provider(tmp_path) -> LocalFileKeyProvider:
    return LocalFileKeyProvider(write_kek(tmp_path / "kek.bin"))


class MemoryStorage:
    """Dictionary-backed EvidenceStorage for tests that need no filesystem."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, object_key: str, data: bytes) -> None:
        self.objects[object_key] = data

    def get(self, object_key: str) -> bytes:
        if object_key not in self.objects:
            raise ObjectNotFound("object is missing")
        return self.objects[object_key]

    def exists(self, object_key: str) -> bool:
        return object_key in self.objects


def stored_record(data: bytes, storage_key: str, provider, storage: MemoryStorage) -> evidence.StoredEvidence:
    """Seal the data as create_evidence does, store it, and return the record that the database would hold."""
    sealed = evidence_crypto.seal(data, storage_key, provider)
    storage.put(storage_key, sealed.ciphertext)
    return evidence.StoredEvidence(
        id=str(uuid.uuid4()),
        case_id=str(uuid.uuid4()),
        original_filename="report.pdf",
        media_type="application/pdf",
        sha256_plaintext=evidence_crypto.sha256_hex(data),
        sha256_ciphertext=evidence_crypto.sha256_hex(sealed.ciphertext),
        storage_key=storage_key,
        key_id=sealed.key_id,
        wrapped_data_key=sealed.wrapped_data_key,
        nonce=sealed.nonce,
        status="available",
    )


def multipart(filename: str, content_type: str, data: bytes, boundary: str = "SAFEBOUNDARY") -> tuple[bytes, str]:
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def chunked(body: bytes, size: int = 1000):
    """A request body sent as a generator: httpx sends it with chunked encoding and no Content-Length."""
    for start in range(0, len(body), size):
        yield body[start:start + size]


INVESTIGATOR = User(
    id="55555555-5555-5555-5555-555555555555",
    username="ines",
    role="investigator",
    is_active=True,
    password_hash="unused",
    sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
    must_change_password=False,
    created_at=datetime(2026, 11, 1, tzinfo=UTC),
)


@pytest.fixture
def client(monkeypatch, provider):
    """The evidence router in a small app. Database-facing calls are replaced; the request handling is real."""
    app = FastAPI()
    app.include_router(evidence_api.evidence_router)
    app.dependency_overrides[auth.current_user] = lambda: INVESTIGATOR
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:read", "evidence:write"}))
    monkeypatch.setattr(evidence_api, "is_visible", lambda case_id, scope: True)
    monkeypatch.setattr(evidence_api, "_PROVIDER", provider)
    monkeypatch.setenv("SAFE_EVIDENCE_MAX_BYTES", "1000")
    return TestClient(app)


def fake_create(calls: list, **kwargs):
    def create(case_id, uploader_id, filename, media_type, data, storage, provider, scope, **kwargs):
        calls.append((case_id, uploader_id, filename, media_type, len(data)))
        return evidence.EvidenceItem(
            id=str(uuid.uuid4()),
            case_id=case_id,
            uploaded_by=uploader_id,
            original_filename=filename or "evidence",
            media_type=media_type,
            size_bytes=len(data),
            sha256_plaintext=evidence_crypto.sha256_hex(data),
            status="available",
            created_at=datetime.now(UTC),
        )

    return create


# --- envelope encryption -----------------------------------------------------------------------------------------


def test_round_trip_encrypts_and_decrypts(provider):
    key = str(uuid.uuid4())
    sealed = evidence_crypto.seal(PDF, key, provider)
    assert sealed.ciphertext != PDF and PDF[:20] not in sealed.ciphertext
    assert len(sealed.nonce) == 12
    assert len(sealed.wrapped_data_key) == 12 + 32 + 16  # wrap nonce, data key, GCM tag
    assert sealed.key_id == provider.key_id
    opened = evidence_crypto.open_sealed(
        sealed.ciphertext, sealed.nonce, sealed.wrapped_data_key, sealed.key_id, key, provider,
    )
    assert opened == PDF


def test_every_object_gets_a_fresh_data_key_and_nonce(provider):
    key_a, key_b = str(uuid.uuid4()), str(uuid.uuid4())
    first = evidence_crypto.seal(PDF, key_a, provider)
    second = evidence_crypto.seal(PDF, key_a, provider)
    assert first.ciphertext != second.ciphertext
    assert first.nonce != second.nonce
    assert first.wrapped_data_key != second.wrapped_data_key
    other = evidence_crypto.seal(PDF, key_b, provider)
    with pytest.raises(KeyProviderError):
        # The storage key is bound as associated data of the wrap: a wrapped key cannot be used under another key.
        evidence_crypto.open_sealed(
            other.ciphertext, other.nonce, other.wrapped_data_key, other.key_id, key_a, provider,
        )


def test_wrong_kek_fails(tmp_path, provider):
    other = LocalFileKeyProvider(write_kek(tmp_path / "other.bin"))
    key = str(uuid.uuid4())
    sealed = evidence_crypto.seal(PDF, key, provider)
    with pytest.raises(KeyProviderError, match="cannot be unwrapped"):
        evidence_crypto.open_sealed(
            sealed.ciphertext, sealed.nonce, sealed.wrapped_data_key, sealed.key_id, key, other,
        )


def test_key_id_mismatch_is_refused(tmp_path, provider):
    renamed = LocalFileKeyProvider(write_kek(tmp_path / "kek2.bin"), key_id="local-file-2")
    key = str(uuid.uuid4())
    sealed = evidence_crypto.seal(PDF, key, provider)
    with pytest.raises(KeyProviderError, match="not the configured key"):
        evidence_crypto.open_sealed(
            sealed.ciphertext, sealed.nonce, sealed.wrapped_data_key, sealed.key_id, key, renamed,
        )


# --- checksums and verified reads --------------------------------------------------------------------------------


def test_verified_read_returns_the_original(provider):
    storage = MemoryStorage()
    record = stored_record(PDF, str(uuid.uuid4()), provider, storage)
    assert evidence.read_original(record, storage, provider) == PDF


def test_tampered_ciphertext_is_detected_by_the_sha256_check(provider):
    storage = MemoryStorage()
    record = stored_record(PDF, str(uuid.uuid4()), provider, storage)
    damaged = bytearray(storage.objects[record.storage_key])
    damaged[10] ^= 0x01
    storage.objects[record.storage_key] = bytes(damaged)
    with pytest.raises(evidence.VerificationFailed) as failure:
        evidence.read_original(record, storage, provider)
    assert failure.value.reason == "ciphertext_checksum_mismatch"


def test_tampered_ciphertext_with_a_matching_checksum_fails_authentication(provider):
    # An attacker who can also rewrite the database checksum still cannot pass the GCM tag.
    storage = MemoryStorage()
    record = stored_record(PDF, str(uuid.uuid4()), provider, storage)
    damaged = bytearray(storage.objects[record.storage_key])
    damaged[10] ^= 0x01
    storage.objects[record.storage_key] = bytes(damaged)
    forged = evidence.StoredEvidence(
        **{**record.__dict__, "sha256_ciphertext": evidence_crypto.sha256_hex(bytes(damaged))}
    )
    with pytest.raises(evidence.VerificationFailed) as failure:
        evidence.read_original(forged, storage, provider)
    assert failure.value.reason == "decryption_failed"


def test_plaintext_checksum_mismatch_is_detected(provider):
    storage = MemoryStorage()
    record = stored_record(PDF, str(uuid.uuid4()), provider, storage)
    wrong = evidence.StoredEvidence(**{**record.__dict__, "sha256_plaintext": "0" * 64})
    with pytest.raises(evidence.VerificationFailed) as failure:
        evidence.read_original(wrong, storage, provider)
    assert failure.value.reason == "plaintext_checksum_mismatch"


def test_missing_object_and_unavailable_key_are_reported(tmp_path, provider):
    storage = MemoryStorage()
    record = stored_record(PDF, str(uuid.uuid4()), provider, storage)
    original = storage.objects.pop(record.storage_key)
    with pytest.raises(evidence.VerificationFailed) as missing:
        evidence.read_original(record, storage, provider)
    assert missing.value.reason == "object_missing"

    storage.put(record.storage_key, original)
    other = LocalFileKeyProvider(write_kek(tmp_path / "other.bin"))
    with pytest.raises(evidence.VerificationFailed) as wrong_key:
        evidence.read_original(record, storage, other)
    assert wrong_key.value.reason == "key_unavailable"


# --- key providers -----------------------------------------------------------------------------------------------


def test_kek_file_with_group_or_world_permissions_is_refused(tmp_path):
    for mode in (0o644, 0o640, 0o604, 0o666):
        path = write_kek(tmp_path / f"kek-{mode:o}.bin", mode=mode)
        with pytest.raises(KeyProviderError, match="group or others"):
            LocalFileKeyProvider(path)


def test_kek_file_owner_only_modes_are_accepted(tmp_path):
    for mode in (0o600, 0o400):
        assert LocalFileKeyProvider(write_kek(tmp_path / f"kek-{mode:o}.bin", mode=mode)).key_id == "local-file-1"


def test_missing_empty_and_malformed_kek_files_are_refused(tmp_path):
    with pytest.raises(KeyProviderError, match="missing"):
        LocalFileKeyProvider(tmp_path / "absent.bin")
    empty = write_kek(tmp_path / "empty.bin", raw=b"")
    with pytest.raises(KeyProviderError, match="empty"):
        LocalFileKeyProvider(empty)
    short = write_kek(tmp_path / "short.bin", raw=os.urandom(16))
    with pytest.raises(KeyProviderError, match="32"):
        LocalFileKeyProvider(short)
    short_b64 = write_kek(tmp_path / "short-b64.txt", raw=base64.b64encode(os.urandom(16)))
    with pytest.raises(KeyProviderError, match="32"):
        LocalFileKeyProvider(short_b64)
    not_base64 = write_kek(tmp_path / "text.bin", raw=b"not a key!!")
    with pytest.raises(KeyProviderError):
        LocalFileKeyProvider(not_base64)


def test_base64_kek_file_is_accepted(tmp_path):
    raw = os.urandom(32)
    base64_provider = LocalFileKeyProvider(write_kek(tmp_path / "b64.txt", raw=base64.b64encode(raw) + b"\n"))
    raw_provider = LocalFileKeyProvider(write_kek(tmp_path / "raw.bin", raw=raw))
    wrapped = raw_provider.wrap(os.urandom(32), b"ctx")
    assert base64_provider.unwrap(wrapped, b"ctx") == raw_provider.unwrap(wrapped, b"ctx")


def test_provider_repr_does_not_show_key_material(provider):
    assert "local-file-1" in repr(provider)
    assert "_kek" not in repr(provider)


def test_key_provider_from_env_requires_the_key_file(monkeypatch):
    monkeypatch.delenv("SAFE_EVIDENCE_KEK_FILE", raising=False)
    monkeypatch.delenv("SAFE_EVIDENCE_KEY_PROVIDER", raising=False)
    with pytest.raises(KeyProviderError, match="SAFE_EVIDENCE_KEK_FILE is not set"):
        evidence_keys.key_provider_from_env()


def test_vault_transit_provider_is_a_stub(monkeypatch, tmp_path):
    with pytest.raises(NotImplementedError, match="not implemented"):
        VaultTransitKeyProvider()
    monkeypatch.setenv("SAFE_EVIDENCE_KEY_PROVIDER", "vault-transit")
    with pytest.raises(NotImplementedError):
        evidence_keys.key_provider_from_env()


# --- storage -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "../secret",
        "..",
        "/etc/passwd",
        "a/b",
        "",
        "../../" + "0" * 8,
        str(uuid.uuid4()).upper(),
        "report.pdf",
        str(uuid.uuid4()) + "/../x",
    ],
)
def test_path_traversal_and_non_uuid_keys_are_refused(tmp_path, key):
    root = tmp_path / "evidence"
    root.mkdir()
    storage = LocalVolumeStorage(root)
    with pytest.raises(InvalidObjectKey):
        storage.put(key, b"x")
    with pytest.raises(InvalidObjectKey):
        storage.get(key)
    with pytest.raises(InvalidObjectKey):
        storage.exists(key)
    assert list(tmp_path.iterdir()) == [root]


def test_local_volume_round_trip_and_permissions(tmp_path):
    storage = LocalVolumeStorage(tmp_path)
    key = str(uuid.uuid4())
    assert storage.exists(key) is False
    storage.put(key, b"ciphertext bytes")
    assert storage.exists(key) is True
    assert storage.get(key) == b"ciphertext bytes"
    assert (tmp_path / key).stat().st_mode & 0o777 == 0o600
    with pytest.raises(StorageError):
        storage.put(key, b"overwrite attempt")
    with pytest.raises(ObjectNotFound):
        storage.get(str(uuid.uuid4()))


def test_atomic_write_leaves_no_partial_file_when_fsync_fails(tmp_path, monkeypatch):
    storage = LocalVolumeStorage(tmp_path)
    key = str(uuid.uuid4())

    def failing_fsync(fd):
        raise OSError("disk full")

    monkeypatch.setattr(evidence_storage.os, "fsync", failing_fsync)
    with pytest.raises(StorageError):
        storage.put(key, b"x" * 4096)
    monkeypatch.undo()
    assert not (tmp_path / key).exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_leaves_no_partial_file_when_rename_fails(tmp_path, monkeypatch):
    storage = LocalVolumeStorage(tmp_path)
    key = str(uuid.uuid4())

    def failing_replace(src, dst):
        raise OSError("rename refused")

    monkeypatch.setattr(evidence_storage.os, "replace", failing_replace)
    with pytest.raises(StorageError):
        storage.put(key, b"x" * 4096)
    monkeypatch.undo()
    assert not (tmp_path / key).exists()
    assert list(tmp_path.iterdir()) == []


def test_storage_error_when_directory_is_missing(tmp_path):
    storage = LocalVolumeStorage(tmp_path / "absent")
    with pytest.raises(StorageError):
        storage.put(str(uuid.uuid4()), b"x")


# --- filenames and media types -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\scan.pdf", "scan.pdf"),
        ('evil"name<>.png', "evil_name__.png"),
        ("line\nbreak\x00.txt", "line_break_.txt"),
        ("   ", "evidence"),
        (None, "evidence"),
        ("..", "evidence"),
    ],
)
def test_filenames_are_sanitized(raw, expected):
    assert evidence.sanitize_filename(raw) == expected


def test_long_filenames_are_capped_and_keep_a_short_extension():
    name = evidence.sanitize_filename("a" * 400 + ".pdf")
    assert len(name) == 255 and name.endswith(".pdf")
    assert len(evidence.sanitize_filename("b" * 400)) == 255


def test_media_type_allowlist_checks_declared_type_and_content():
    assert evidence.check_media_type("application/pdf; charset=binary", PDF) == "application/pdf"
    assert evidence.check_media_type("text/plain", b"plain text") == "text/plain"
    assert evidence.check_media_type("application/json", b'{"a": 1}') == "application/json"
    for declared, data in [
        ("application/x-msdownload", PDF),
        ("application/zip", b"PK\x03\x04"),
        ("text/html", b"<html>"),
        ("image/png", PDF),
        ("text/plain", b"binary\x00data"),
        ("application/json", b"{not json"),
        (None, PDF),
    ]:
        with pytest.raises(evidence.UnsupportedMediaType):
            evidence.check_media_type(declared, data)


def test_max_bytes_defaults_to_fifty_mebibytes(monkeypatch):
    monkeypatch.delenv("SAFE_EVIDENCE_MAX_BYTES", raising=False)
    assert evidence.max_bytes_from_env() == 50 * 1024 * 1024
    monkeypatch.setenv("SAFE_EVIDENCE_MAX_BYTES", "0")
    with pytest.raises(ValueError):
        evidence.max_bytes_from_env()


def test_content_disposition_is_attachment_with_safe_fallback():
    value = evidence_api._content_disposition('résumé "v2".pdf')
    assert value.startswith('attachment; filename="r_sum_ _v2_.pdf"')
    assert "filename*=UTF-8''r%C3%A9sum%C3%A9%20%22v2%22.pdf" in value


def test_response_model_never_exposes_keys_or_storage_details():
    forbidden = {"storage_key", "wrapped_data_key", "nonce", "key_id", "sha256_ciphertext"}
    assert not forbidden & set(evidence_api.EvidenceResponse.model_fields)


# --- upload endpoint ---------------------------------------------------------------------------------------------


def test_size_limit_is_enforced_on_a_stream_without_content_length(client, monkeypatch):
    calls: list = []
    monkeypatch.setattr(evidence, "create_evidence", fake_create(calls))
    body, content_type = multipart("big.pdf", "application/pdf", PDF + b"x" * 5000)
    response = client.post(
        f"/api/v1/cases/{uuid.uuid4()}/evidence",
        content=chunked(body),
        headers={**idem(), "Content-Type": content_type},
    )
    assert "content-length" not in {name.lower() for name in response.request.headers}
    assert response.status_code == 413
    assert calls == []


def test_body_far_over_the_limit_is_cut_off_while_reading(client, monkeypatch):
    calls: list = []
    monkeypatch.setattr(evidence, "create_evidence", fake_create(calls))
    body, content_type = multipart("huge.txt", "text/plain", b"a" * 500_000)
    response = client.post(
        f"/api/v1/cases/{uuid.uuid4()}/evidence",
        content=chunked(body, size=4096),
        headers={**idem(), "Content-Type": content_type},
    )
    assert response.status_code == 413
    assert calls == []


def test_declared_content_length_over_the_limit_is_refused_early(client, monkeypatch):
    calls: list = []
    monkeypatch.setattr(evidence, "create_evidence", fake_create(calls))
    body, content_type = multipart("huge.txt", "text/plain", b"a" * 500_000)
    response = client.post(
        f"/api/v1/cases/{uuid.uuid4()}/evidence", content=body, headers={**idem(), "Content-Type": content_type},
    )
    assert response.status_code == 413
    assert calls == []


def test_file_within_the_limit_is_accepted_and_returns_metadata_only(client, monkeypatch):
    calls: list = []
    monkeypatch.setattr(evidence, "create_evidence", fake_create(calls))
    data = PDF[:900]
    body, content_type = multipart("scan.pdf", "application/pdf", data)
    case_id = str(uuid.uuid4())
    response = client.post(f"/api/v1/cases/{case_id}/evidence", content=chunked(body), headers={**idem(), "Content-Type": content_type})
    assert response.status_code == 201
    payload = response.json()
    assert payload["case_id"] == case_id and payload["status"] == "available"
    assert payload["size_bytes"] == len(data) and payload["media_type"] == "application/pdf"
    assert not {"storage_key", "wrapped_data_key", "nonce", "key_id"} & set(payload)
    assert calls == [(case_id, INVESTIGATOR.id, "scan.pdf", "application/pdf", len(data))]


def test_disallowed_media_type_is_415_and_nothing_is_stored(client, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("create_evidence must not run for a disallowed type")

    monkeypatch.setattr(evidence, "create_evidence", must_not_run)
    body, content_type = multipart("tool.zip", "application/zip", b"PK\x03\x04 data")
    response = client.post(f"/api/v1/cases/{uuid.uuid4()}/evidence", content=body, headers={**idem(), "Content-Type": content_type})
    assert response.status_code == 415


def test_missing_file_field_and_empty_file_are_rejected(client, monkeypatch):
    monkeypatch.setattr(evidence, "create_evidence", fake_create([]))
    body, content_type = multipart("empty.txt", "text/plain", b"")
    assert client.post(f"/api/v1/cases/{uuid.uuid4()}/evidence", content=body,
                       headers={**idem(), "Content-Type": content_type}).status_code == 422
    plain = b"--X\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nhi\r\n--X--\r\n"
    assert client.post(f"/api/v1/cases/{uuid.uuid4()}/evidence", content=plain,
                       headers={**idem(), "Content-Type": "multipart/form-data; boundary=X"}).status_code == 422


def test_upload_is_refused_when_no_key_is_configured(client, monkeypatch):
    monkeypatch.setattr(evidence_api, "_PROVIDER", None)
    body, content_type = multipart("scan.pdf", "application/pdf", PDF[:100])
    response = client.post(f"/api/v1/cases/{uuid.uuid4()}/evidence", content=body, headers={**idem(), "Content-Type": content_type})
    assert response.status_code == 503


def test_upload_to_a_case_the_caller_cannot_see_is_404(client, monkeypatch):
    monkeypatch.setattr(evidence_api, "is_visible", lambda case_id, scope: False)
    body, content_type = multipart("scan.pdf", "application/pdf", PDF[:100])
    response = client.post(f"/api/v1/cases/{uuid.uuid4()}/evidence", content=body, headers={**idem(), "Content-Type": content_type})
    assert response.status_code == 404


def test_upload_needs_evidence_write(client, monkeypatch):
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"cases:read_own"}))
    body, content_type = multipart("scan.pdf", "application/pdf", PDF[:100])
    response = client.post(f"/api/v1/cases/{uuid.uuid4()}/evidence", content=body, headers={**idem(), "Content-Type": content_type})
    assert response.status_code == 403


# --- reading evidence: list, download, and custody need evidence:read ---------------------------------------------

READ_ENDPOINTS = [
    "/api/v1/cases/{}/evidence",
    "/api/v1/evidence/{}/download",
    "/api/v1/evidence/{}/custody",
]


def refuse(*args, **kwargs):
    raise AssertionError("must not run without evidence:read")


@pytest.mark.parametrize("url_template", READ_ENDPOINTS)
def test_reading_evidence_needs_evidence_read(client, monkeypatch, url_template):
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:write", "cases:read_assigned"}))
    monkeypatch.setattr(evidence_api, "is_visible", refuse)
    monkeypatch.setattr(evidence, "find_stored", refuse)
    monkeypatch.setattr(evidence, "list_evidence", refuse)
    response = client.get(url_template.format(uuid.uuid4()))
    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}


@pytest.mark.parametrize("url_template", READ_ENDPOINTS)
def test_reading_evidence_is_404_when_the_case_is_not_visible(client, monkeypatch, url_template):
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:read"}))
    monkeypatch.setattr(evidence_api, "is_visible", lambda case_id, scope: False)
    monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: None)
    assert client.get(url_template.format(uuid.uuid4())).status_code == 404


def test_listing_with_evidence_read_returns_the_page(client, monkeypatch):
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:read"}))
    monkeypatch.setattr(evidence, "list_evidence", lambda case_id, limit, offset, recipient=None: ([], 0))
    response = client.get(f"/api/v1/cases/{uuid.uuid4()}/evidence")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


# --- custody and reconciliation: database-backed -----------------------------------------------------------------


def test_fail_stale_pending_rejects_a_non_positive_window():
    with pytest.raises(ValueError):
        evidence.fail_stale_pending(0)


@pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)
def test_custody_events_are_append_only():
    migrate.MIGRATIONS_DIR = MIGRATIONS
    assert migrate.main() == 0
    suffix = uuid.uuid4().hex[:12]
    user_id = users.create_user("t-" + suffix, "temporary passphrase", "investigator")
    with connect() as conn:
        case_id = conn.execute(
            """
            INSERT INTO cases (reference, title, awareness_time, created_by, status, calendar_version, calendar_timezone)
            VALUES (%s, 'test case', now(), %s, 'open', 'test', 'UTC') RETURNING id::text
            """,
            ("SAFE-TEST-" + suffix, user_id),
        ).fetchone()[0]
        evidence_id = conn.execute(
            """
            INSERT INTO evidence_items (case_id, uploaded_by, original_filename, media_type, size_bytes,
                                        storage_key, key_id, wrapped_data_key, nonce, status)
            VALUES (%s, %s, 'note.txt', 'text/plain', 1, %s, 'test', '\\x00'::bytea, '\\x00'::bytea, 'pending')
            RETURNING id::text
            """,
            (case_id, user_id, str(uuid.uuid4())),
        ).fetchone()[0]
        event_id = conn.execute(
            "INSERT INTO custody_events (evidence_id, actor_id, action) VALUES (%s, %s, 'uploaded') RETURNING id::text",
            (evidence_id, user_id),
        ).fetchone()[0]

    with pytest.raises(psycopg.errors.RaiseException):
        with connect() as conn:
            conn.execute("UPDATE custody_events SET action = 'downloaded' WHERE id = %s", (event_id,))
    with pytest.raises(psycopg.errors.RaiseException):
        with connect() as conn:
            conn.execute("UPDATE custody_events SET details = '{\"x\": 1}'::jsonb WHERE id = %s", (event_id,))
    with connect() as conn:
        conn.execute("UPDATE custody_events SET is_deleted = true, deleted_at = now() WHERE id = %s", (event_id,))
    with pytest.raises(psycopg.errors.RaiseException):
        with connect() as conn:
            conn.execute("UPDATE custody_events SET is_deleted = false, deleted_at = NULL WHERE id = %s", (event_id,))


# --- case visibility applied to evidence: database-backed --------------------------------------------------------

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def db_user(role: str) -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


def request_as(user_id: str, method: str, url: str, **kwargs):
    """One request through the real app, made as the given user. The caller is set before every request."""
    user = users.find_user_by_id(user_id)
    assert user is not None
    api.app.dependency_overrides[auth.current_user] = lambda: user
    return getattr(TestClient(api.app), method)(url, **kwargs)


def evidence_count(case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM evidence_items WHERE case_id = %s AND NOT is_deleted", (case_id,)
        ).fetchone()[0]


def downloaded_events(evidence_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM custody_events WHERE evidence_id = %s AND action = %s AND NOT is_deleted",
            (evidence_id, evidence.DOWNLOADED),
        ).fetchone()[0]


@pytest.fixture
def evidence_world(monkeypatch, provider):
    """A migrated database, one case with one stored evidence item, and the users around it.

    The investigator is assigned to the case; the other investigator and the reviewer are not.
    """
    migrate.MIGRATIONS_DIR = MIGRATIONS
    assert migrate.main() == 0
    people = {
        "reporter": db_user("reporter"),
        "triager": db_user("triager"),
        "investigator": db_user("investigator"),
        "other_investigator": db_user("investigator"),
        "reviewer": db_user("reviewer"),
    }
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, people["triager"])
    storage = MemoryStorage()
    monkeypatch.setattr(evidence_api, "_PROVIDER", provider)
    monkeypatch.setattr(evidence_api, "storage_from_env", lambda: storage)
    case = cases.create_case("Evidence access case", datetime(2026, 10, 30, 9, tzinfo=UTC), people["reporter"])
    outcome, _ = cases.assign_member(case.id, people["investigator"], CaseScope(people["triager"], read_all=True))
    assert outcome is cases.Outcome.OK
    item = evidence.create_evidence(
        case.id, people["investigator"], "scan.pdf", "application/pdf", PDF, storage, provider,
        CaseScope(people["triager"], read_all=True),
    )
    yield {"people": people, "case_id": case.id, "evidence_id": item.id}
    api.app.dependency_overrides.clear()


@requires_database
def test_unassigned_investigator_gets_the_same_404_as_a_missing_item_or_case(evidence_world):
    other = evidence_world["people"]["other_investigator"]
    case_id, evidence_id = evidence_world["case_id"], evidence_world["evidence_id"]
    missing_case, missing_item = str(uuid.uuid4()), str(uuid.uuid4())
    for method, hidden_url, absent_url in [
        ("get", f"/api/v1/cases/{case_id}/evidence", f"/api/v1/cases/{missing_case}/evidence"),
        ("get", f"/api/v1/evidence/{evidence_id}/download", f"/api/v1/evidence/{missing_item}/download"),
        ("get", f"/api/v1/evidence/{evidence_id}/custody", f"/api/v1/evidence/{missing_item}/custody"),
    ]:
        hidden = request_as(other, method, hidden_url)
        absent = request_as(other, method, absent_url)
        assert hidden.status_code == absent.status_code == 404
        assert hidden.json() == absent.json()


@requires_database
def test_refused_download_returns_no_bytes_and_records_no_download(evidence_world):
    other = evidence_world["people"]["other_investigator"]
    evidence_id = evidence_world["evidence_id"]
    response = request_as(other, "get", f"/api/v1/evidence/{evidence_id}/download")
    assert response.status_code == 404
    assert response.content != PDF
    assert downloaded_events(evidence_id) == 0


@requires_database
def test_unassigned_investigator_cannot_upload_to_a_case_it_cannot_see(evidence_world):
    other = evidence_world["people"]["other_investigator"]
    case_id = evidence_world["case_id"]
    body, content_type = multipart("scan.pdf", "application/pdf", PDF[:100])
    headers = {"Content-Type": content_type}
    hidden = request_as(other, "post", f"/api/v1/cases/{case_id}/evidence", content=body, headers=headers)
    absent = request_as(other, "post", f"/api/v1/cases/{uuid.uuid4()}/evidence", content=body, headers=headers)
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json()
    assert evidence_count(case_id) == 1  # the refused upload wrote nothing


@requires_database
def test_unassigned_reviewer_cannot_list_evidence_of_the_case(evidence_world):
    reviewer = evidence_world["people"]["reviewer"]
    response = request_as(reviewer, "get", f"/api/v1/cases/{evidence_world['case_id']}/evidence")
    assert response.status_code == 404


@requires_database
def test_missing_evidence_permission_is_403_before_visibility_is_checked(evidence_world):
    people = evidence_world["people"]
    case_id, evidence_id = evidence_world["case_id"], evidence_world["evidence_id"]
    # The reporter created the case, so the case is visible to it, and it still holds no evidence permission.
    for method, url in [
        ("get", f"/api/v1/cases/{case_id}/evidence"),
        ("get", f"/api/v1/evidence/{evidence_id}/download"),
        ("get", f"/api/v1/evidence/{evidence_id}/custody"),
    ]:
        assert request_as(people["reporter"], method, url).status_code == 403
    body, content_type = multipart("scan.pdf", "application/pdf", PDF[:100])
    upload = request_as(
        people["reporter"], "post", f"/api/v1/cases/{case_id}/evidence", content=body,
        headers={"Content-Type": content_type},
    )
    assert upload.status_code == 403
    # The reviewer holds evidence:read but not evidence:write, and the case is hidden from it: 403 wins.
    upload = request_as(
        people["reviewer"], "post", f"/api/v1/cases/{case_id}/evidence", content=body,
        headers={"Content-Type": content_type},
    )
    assert upload.status_code == 403


@requires_database
def test_assigned_investigator_reads_and_uploads_on_its_case(evidence_world):
    investigator = evidence_world["people"]["investigator"]
    case_id, evidence_id = evidence_world["case_id"], evidence_world["evidence_id"]
    listing = request_as(investigator, "get", f"/api/v1/cases/{case_id}/evidence")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    download = request_as(investigator, "get", f"/api/v1/evidence/{evidence_id}/download")
    assert download.status_code == 200
    assert download.content == PDF
    custody = request_as(investigator, "get", f"/api/v1/evidence/{evidence_id}/custody")
    assert custody.status_code == 200
    assert evidence.DOWNLOADED in [event["action"] for event in custody.json()]
    body, content_type = multipart("notes.pdf", "application/pdf", PDF[:100])
    upload = request_as(investigator, "post", f"/api/v1/cases/{case_id}/evidence", content=body,
                        headers={**idem(), "Content-Type": content_type})
    assert upload.status_code == 201
    assert evidence_count(case_id) == 2
