"""Evidence lifecycle: pending, available, and failed, with the custody event for each transition.

Most tests need no database. A ledger fake replaces the four row operations that create_evidence and the download
path use, so the order of the transitions can be asserted. A recording connection checks the SQL of record_failure.
The database tests at the end run the same transitions against PostgreSQL. They are skipped unless
SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings are set, like tests/test_database.py.
"""

import contextlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safe_kit import auth, evidence, evidence_api, evidence_crypto, migrate, users
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.evidence import INTEGRITY_FAILURE_REASONS, StoreVerificationFailed, UploadExpired, VerificationFailed
from safe_kit.evidence_keys import KeyProviderError, LocalFileKeyProvider
from safe_kit.evidence_storage import ObjectNotFound, StorageError
from safe_kit.users import User

PDF = b"%PDF-1.7\n" + b"incident detail " * 60 + b"\n%%EOF\n"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

# Every reason that read_original can return, and how each one is classified.
AVAILABILITY_FAILURE_REASONS = frozenset({"storage_read_failed", "key_unavailable"})
ALL_READ_FAILURE_REASONS = frozenset(
    {
        "object_missing",
        "decryption_failed",
        "ciphertext_checksum_mismatch",
        "plaintext_checksum_mismatch",
    }
) | AVAILABILITY_FAILURE_REASONS

READER = User(
    id="66666666-6666-6666-6666-666666666666",
    username="rafael",
    role="reviewer",
    is_active=True,
    password_hash="unused",
    sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
    must_change_password=False,
    created_at=datetime(2026, 11, 1, tzinfo=UTC),
)


# --- helpers and fakes -------------------------------------------------------------------------------------------


def write_kek(path: Path) -> Path:
    path.write_bytes(os.urandom(32))
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def provider(tmp_path) -> LocalFileKeyProvider:
    return LocalFileKeyProvider(write_kek(tmp_path / "kek.bin"))


class UnreadableKey:
    """Wraps a key provider whose wrapped keys can be written but not read back."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.key_id = inner.key_id

    def wrap(self, data_key: bytes, context: bytes) -> bytes:
        return self._inner.wrap(data_key, context)

    def unwrap(self, wrapped: bytes, context: bytes) -> bytes:
        raise KeyProviderError("the key cannot be read back")


class ScriptedStorage:
    """An EvidenceStorage whose behaviour a test chooses: ok, fail_write, drop, corrupt, or unreadable.

    after_put runs once the write has returned, so a test can simulate reconciliation racing the upload.
    """

    def __init__(self, events: list, mode: str = "ok", after_put=None) -> None:
        self.events = events
        self.mode = mode
        self.after_put = after_put
        self.objects: dict[str, bytes] = {}

    def put(self, object_key: str, data: bytes) -> None:
        self.events.append("put")
        if self.mode == "fail_write":
            raise StorageError("object could not be written")
        if self.mode == "drop":
            pass
        elif self.mode == "corrupt":
            damaged = bytearray(data)
            damaged[10] ^= 0x01
            self.objects[object_key] = bytes(damaged)
        else:
            self.objects[object_key] = data
        if self.after_put is not None:
            self.after_put()

    def get(self, object_key: str) -> bytes:
        if self.mode == "unreadable":
            raise StorageError("object could not be read")
        if object_key not in self.objects:
            raise ObjectNotFound("object is missing")
        return self.objects[object_key]

    def exists(self, object_key: str) -> bool:
        return object_key in self.objects


class Ledger:
    """Fake rows and custody log for evidence_items. The four row operations are replaced with these methods."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.custody: list[tuple[str, str, dict]] = []
        self.events: list[str] = []


@pytest.fixture
def ledger(monkeypatch) -> Ledger:
    book = Ledger()

    def insert_pending(case_id, uploader_id, name, media_type, size_bytes, storage_key, key_id, wrapped, nonce, scope, **kwargs):
        evidence_id = str(uuid.uuid4())
        book.rows[evidence_id] = "pending"
        book.events.append("pending")
        return evidence_id

    def mark_available(evidence_id, uploader_id, sha_plaintext, sha_ciphertext, **kwargs):
        if book.rows.get(evidence_id) != "pending":
            raise UploadExpired()
        book.rows[evidence_id] = "available"
        book.custody.append((evidence_id, "uploaded", {}))
        book.events.append("available")
        return evidence.EvidenceItem(
            id=evidence_id,
            case_id="",
            uploaded_by=uploader_id,
            original_filename="",
            media_type="",
            size_bytes=0,
            sha256_plaintext=sha_plaintext,
            status="available",
            created_at=datetime.now(UTC),
        )

    def mark_failed(evidence_id, actor_id, reason):
        if book.rows.get(evidence_id) == "pending":
            book.rows[evidence_id] = "failed"
            book.custody.append((evidence_id, "failed", {"reason": reason}))
            book.events.append(f"failed:{reason}")

    monkeypatch.setattr(evidence, "_insert_pending", insert_pending)
    monkeypatch.setattr(evidence, "_mark_available", mark_available)
    monkeypatch.setattr(evidence, "_mark_failed", mark_failed)
    return book


def read_all(user_id: str) -> CaseScope:
    return CaseScope.from_permissions(user_id, frozenset({"cases:read_all"}))


def upload(storage, provider, data: bytes = PDF):
    uploader = str(uuid.uuid4())
    return evidence.create_evidence(
        str(uuid.uuid4()), uploader, "scan.pdf", "application/pdf", data, storage, provider, read_all(uploader),
    )


class FakeConnection:
    def __init__(self, log: list) -> None:
        self.log = log

    def execute(self, sql, params=()):
        self.log.append(" ".join(sql.split()))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


@pytest.fixture
def sql_log(monkeypatch) -> list:
    log: list = []

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConnection(log)

    monkeypatch.setattr(evidence, "connect", fake_connect)
    return log


def stored_for(status: str, storage: ScriptedStorage, provider, data: bytes = PDF) -> evidence.StoredEvidence:
    """Seal and store one object, and return the record the database would hold for it with the given status."""
    key = str(uuid.uuid4())
    sealed = evidence_crypto.seal(data, key, provider)
    storage.objects[key] = sealed.ciphertext
    return evidence.StoredEvidence(
        id=str(uuid.uuid4()),
        case_id=str(uuid.uuid4()),
        original_filename="scan.pdf",
        media_type="application/pdf",
        sha256_plaintext=evidence_crypto.sha256_hex(data),
        sha256_ciphertext=evidence_crypto.sha256_hex(sealed.ciphertext),
        storage_key=key,
        key_id=sealed.key_id,
        wrapped_data_key=sealed.wrapped_data_key,
        nonce=sealed.nonce,
        status=status,
    )


# --- upload: pending, then available or failed --------------------------------------------------------------------


def test_successful_upload_is_pending_then_available_after_the_read_back(ledger, provider):
    storage = ScriptedStorage(ledger.events)
    item = upload(storage, provider)
    assert item.status == "available"
    assert list(ledger.rows.values()) == ["available"]
    # The pending row exists before the object is written, and the row turns available only after the write.
    assert ledger.events == ["pending", "put", "available"]
    assert [action for _, action, _ in ledger.custody] == ["uploaded"]


def test_failed_write_marks_the_row_failed_with_a_custody_event_and_re_raises(ledger, provider):
    storage = ScriptedStorage(ledger.events, mode="fail_write")
    with pytest.raises(StorageError):
        upload(storage, provider)
    assert list(ledger.rows.values()) == ["failed"]
    assert ledger.custody == [(next(iter(ledger.rows)), "failed", {"reason": "storage_write_failed"})]
    assert storage.objects == {}


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("drop", "object_missing"),
        ("corrupt", "ciphertext_checksum_mismatch"),
        ("unreadable", "storage_read_failed"),
    ],
)
def test_failed_read_back_marks_the_row_failed_and_is_never_available(ledger, provider, mode, reason):
    storage = ScriptedStorage(ledger.events, mode=mode)
    with pytest.raises(StoreVerificationFailed) as failure:
        upload(storage, provider)
    assert failure.value.reason == reason
    assert isinstance(failure.value, StorageError)  # the API answers 500 for any StorageError
    assert list(ledger.rows.values()) == ["failed"]
    assert "available" not in ledger.events
    assert ledger.custody[-1][2] == {"reason": reason}


def test_read_back_with_a_key_that_cannot_be_unwrapped_fails_the_upload(ledger, provider):
    storage = ScriptedStorage(ledger.events)
    with pytest.raises(StoreVerificationFailed) as failure:
        upload(storage, UnreadableKey(provider))
    assert failure.value.reason == "key_unavailable"
    assert list(ledger.rows.values()) == ["failed"]


def test_upload_that_reconciliation_failed_first_is_expired_and_stays_failed(ledger, provider):
    def reconcile():
        # What fail_stale_pending does when the upload is slower than its window.
        for evidence_id, status in list(ledger.rows.items()):
            if status == "pending":
                ledger.rows[evidence_id] = "failed"
                ledger.custody.append((evidence_id, "failed", {"reason": "stale_pending"}))

    storage = ScriptedStorage(ledger.events, after_put=reconcile)
    with pytest.raises(UploadExpired):
        upload(storage, provider)
    assert list(ledger.rows.values()) == ["failed"]
    assert "available" not in ledger.events


def test_key_failure_before_the_row_is_created_leaves_nothing_behind(ledger, provider):
    class NoWrap(UnreadableKey):
        def wrap(self, data_key, context):
            raise KeyProviderError("no key material")

    storage = ScriptedStorage(ledger.events)
    with pytest.raises(KeyProviderError):
        upload(storage, NoWrap(provider))
    assert ledger.rows == {}
    assert storage.objects == {}


# --- record_failure: the custody event always, the status only for integrity failures ----------------------------


def test_every_read_failure_reason_is_classified_as_integrity_or_availability():
    assert INTEGRITY_FAILURE_REASONS | AVAILABILITY_FAILURE_REASONS == ALL_READ_FAILURE_REASONS
    assert not INTEGRITY_FAILURE_REASONS & AVAILABILITY_FAILURE_REASONS


@pytest.mark.parametrize("reason", sorted(INTEGRITY_FAILURE_REASONS))
def test_integrity_failure_moves_an_available_item_to_failed(sql_log, reason):
    evidence.record_failure("evid-1", "actor-1", reason)
    assert len(sql_log) == 2
    assert sql_log[0] == (
        "UPDATE evidence_items SET status = 'failed' WHERE id = %s AND status = 'available' AND NOT is_deleted"
    )
    assert sql_log[1].startswith("INSERT INTO custody_events")


@pytest.mark.parametrize("reason", sorted(AVAILABILITY_FAILURE_REASONS))
def test_availability_failure_is_logged_and_leaves_the_status_alone(sql_log, reason):
    evidence.record_failure("evid-1", "actor-1", reason)
    assert len(sql_log) == 1
    assert sql_log[0].startswith("INSERT INTO custody_events")


# --- download endpoint: 409 for a non-available item, 500 for an integrity failure -----------------------------


class Recorder:
    def __init__(self) -> None:
        self.downloads: list = []
        self.failures: list = []

    def download(self, evidence_id, actor_id, case_id):
        self.downloads.append((evidence_id, actor_id, case_id))

    def failure(self, evidence_id, actor_id, reason):
        self.failures.append((evidence_id, actor_id, reason))


@pytest.fixture
def api(monkeypatch, provider):
    app = FastAPI()
    app.include_router(evidence_api.evidence_router)
    app.dependency_overrides[auth.current_user] = lambda: READER
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:read"}))
    monkeypatch.setattr(evidence_api, "_PROVIDER", provider)
    storage = ScriptedStorage([])
    monkeypatch.setattr(evidence_api, "storage_from_env", lambda: storage)
    recorder = Recorder()
    monkeypatch.setattr(evidence, "record_download", recorder.download)
    monkeypatch.setattr(evidence, "record_failure", recorder.failure)
    return SimpleNamespace(
        client=TestClient(app, raise_server_exceptions=False),
        storage=storage,
        recorder=recorder,
        monkeypatch=monkeypatch,
    )


def download_url(stored: evidence.StoredEvidence) -> str:
    return f"/api/v1/evidence/{stored.id}/download"


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_download_of_a_non_available_item_is_409_and_reads_nothing(api, provider, status):
    stored = stored_for(status, api.storage, provider)
    api.monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: stored)

    def must_not_read(*args, **kwargs):
        raise AssertionError("a non-available item must not be read")

    api.monkeypatch.setattr(evidence, "read_original", must_not_read)
    response = api.client.get(download_url(stored))
    assert response.status_code == 409
    assert response.json()["detail"] == "evidence is not available"
    assert api.recorder.downloads == [] and api.recorder.failures == []


def test_download_of_an_available_item_returns_the_content_and_records_the_read(api, provider):
    stored = stored_for("available", api.storage, provider)
    api.monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: stored)
    response = api.client.get(download_url(stored))
    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["Cache-Control"] == "no-store"
    assert api.recorder.downloads == [(stored.id, READER.id, stored.case_id)]
    assert api.recorder.failures == []


@pytest.mark.parametrize("reason", ["ciphertext_checksum_mismatch", "object_missing"])
def test_integrity_failure_on_download_is_500_with_custody_and_no_content(api, provider, reason):
    stored = stored_for("available", api.storage, provider)
    if reason == "object_missing":
        del api.storage.objects[stored.storage_key]
    else:
        api.storage.objects[stored.storage_key] = b"damaged" + api.storage.objects[stored.storage_key][7:]
    api.monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: stored)
    response = api.client.get(download_url(stored))
    assert response.status_code == 500
    assert response.json()["detail"] == "evidence failed its integrity check"
    assert PDF[:20] not in response.content
    assert api.recorder.failures == [(stored.id, READER.id, reason)]
    assert api.recorder.downloads == []


def test_unavailable_key_on_download_is_500_and_logged_without_content(api, provider):
    stored = stored_for("available", api.storage, provider)
    api.monkeypatch.setattr(evidence_api, "_PROVIDER", UnreadableKey(provider))
    api.monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: stored)
    response = api.client.get(download_url(stored))
    assert response.status_code == 500
    assert api.recorder.failures == [(stored.id, READER.id, "key_unavailable")]
    assert api.recorder.downloads == []


def test_read_marker_that_cannot_be_recorded_returns_no_content(api, provider):
    stored = stored_for("available", api.storage, provider)
    api.monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: stored)

    def cannot_record(*args, **kwargs):
        raise RuntimeError("audit table unavailable")

    api.monkeypatch.setattr(evidence, "record_download", cannot_record)
    response = api.client.get(download_url(stored))
    assert response.status_code == 500
    assert PDF[:20] not in response.content


# --- database: the same transitions against PostgreSQL --------------------------------------------------------------

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def database_case() -> tuple[str, str]:
    migrate.MIGRATIONS_DIR = MIGRATIONS
    assert migrate.main() == 0
    suffix = uuid.uuid4().hex[:12]
    user_id = users.create_user("t-" + suffix, "temporary passphrase", "investigator")
    with connect() as conn:
        case_id = conn.execute(
            """
            INSERT INTO cases (reference, title, awareness_time, created_by, status, calendar_version, calendar_timezone)
            VALUES (%s, 'lifecycle case', now(), %s, 'open', 'test', 'UTC') RETURNING id::text
            """,
            ("SAFE-TEST-" + suffix, user_id),
        ).fetchone()[0]
    return user_id, case_id


def database_status(evidence_id: str) -> str:
    with connect() as conn:
        return conn.execute("SELECT status FROM evidence_items WHERE id = %s", (evidence_id,)).fetchone()[0]


def database_storage_key(evidence_id: str) -> str:
    with connect() as conn:
        return conn.execute(
            "SELECT storage_key FROM evidence_items WHERE id = %s", (evidence_id,)
        ).fetchone()[0]


@requires_database
def test_upload_is_available_in_the_database_with_an_uploaded_custody_event(provider):
    user_id, case_id = database_case()
    storage = ScriptedStorage([])
    item = evidence.create_evidence(
        case_id, user_id, "scan.pdf", "application/pdf", PDF, storage, provider, read_all(user_id),
    )
    assert database_status(item.id) == "available"
    assert [event.action for event in evidence.custody_events(item.id)] == ["uploaded"]


@requires_database
def test_failed_write_is_failed_in_the_database_with_a_reason(provider):
    user_id, case_id = database_case()
    with pytest.raises(StorageError):
        evidence.create_evidence(
            case_id, user_id, "scan.pdf", "application/pdf", PDF, ScriptedStorage([], mode="fail_write"), provider,
            read_all(user_id),
        )
    with connect() as conn:
        rows = conn.execute(
            "SELECT id::text, status FROM evidence_items WHERE case_id = %s", (case_id,)
        ).fetchall()
    assert [status for _, status in rows] == ["failed"]
    events = evidence.custody_events(rows[0][0])
    assert [(event.action, event.details) for event in events] == [("failed", {"reason": "storage_write_failed"})]


@requires_database
def test_integrity_failure_on_download_moves_the_row_to_failed(provider):
    user_id, case_id = database_case()
    storage = ScriptedStorage([])
    item = evidence.create_evidence(
        case_id, user_id, "scan.pdf", "application/pdf", PDF, storage, provider, read_all(user_id),
    )
    key = database_storage_key(item.id)
    storage.objects[key] = b"damaged" + storage.objects[key][7:]
    scope = CaseScope.from_permissions(user_id, frozenset({"cases:read_all"}))
    stored = evidence.find_stored(item.id, scope)
    assert stored is not None and stored.status == "available"
    with pytest.raises(VerificationFailed) as failure:
        evidence.read_original(stored, storage, provider)
    evidence.record_failure(item.id, user_id, failure.value.reason)
    assert database_status(item.id) == "failed"
    last = evidence.custody_events(item.id)[-1]
    assert (last.action, last.details) == ("failed", {"reason": failure.value.reason})
    # A later download is refused as not available, not retried.
    assert evidence.find_stored(item.id, scope).status == "failed"


@requires_database
def test_availability_failure_on_download_keeps_the_row_available(provider):
    user_id, case_id = database_case()
    item = evidence.create_evidence(
        case_id, user_id, "scan.pdf", "application/pdf", PDF, ScriptedStorage([]), provider, read_all(user_id),
    )
    evidence.record_failure(item.id, user_id, "key_unavailable")
    assert database_status(item.id) == "available"
    assert evidence.custody_events(item.id)[-1].details == {"reason": "key_unavailable"}


@requires_database
def test_pending_row_left_by_a_crash_is_failed_by_reconciliation():
    user_id, case_id = database_case()
    with connect() as conn:
        evidence_id = conn.execute(
            """
            INSERT INTO evidence_items (case_id, uploaded_by, original_filename, media_type, size_bytes,
                                        storage_key, key_id, wrapped_data_key, nonce, status, created_at)
            VALUES (%s, %s, 'note.txt', 'text/plain', 1, %s, 'test', '\\x00'::bytea, '\\x00'::bytea,
                    'pending', now() - interval '2 hours')
            RETURNING id::text
            """,
            (case_id, user_id, str(uuid.uuid4())),
        ).fetchone()[0]
    assert evidence_id in evidence.fail_stale_pending(30)
    assert database_status(evidence_id) == "failed"
    last = evidence.custody_events(evidence_id)[-1]
    assert (last.action, last.details) == ("failed", {"reason": "stale_pending"})
