"""F1 fixture T04 (evidence) and the F1 absence check of T05.

T04: a stable evidence ID, no overwrite of a stored original, custody events for upload and access, a visible digest
mismatch that is not described as tamper-proof, and recovery of orphaned uploads after a crash.
T05 (F1 subset): an unknown evidence item is an error, never an empty success.

Most tests need no database: they use the ledger fake from test_evidence_lifecycle and a real LocalVolumeStorage in
pytest's temporary directory. The database tests are skipped unless SAFE_TEST_DATABASE=1, like tests/test_database.py.
"""

# Fixtures are imported from the sibling test modules and requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F811
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safe_kit import auth, evidence, evidence_api, evidence_crypto, worker
from safe_kit.db import connect
from safe_kit.evidence import INTEGRITY_FAILURE_REASONS, VerificationFailed
from safe_kit.evidence_storage import LocalVolumeStorage, StorageError
from safe_kit.users import User

# The fixtures and helpers below come from the evidence lifecycle tests. Each name is used by at least one test here.
from test_evidence_lifecycle import PDF
from test_evidence_lifecycle import database_case
from test_evidence_lifecycle import database_status
from test_evidence_lifecycle import ledger  # noqa: F401 - fixture: fake row operations
from test_evidence_lifecycle import provider  # noqa: F401 - fixture: local KEK provider
from test_evidence_lifecycle import read_all
from test_evidence_lifecycle import requires_database

REVIEWER = User(
    id="77777777-7777-7777-7777-777777777777",
    username="fixture-reviewer",
    role="reviewer",
    is_active=True,
    password_hash="unused",
    sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
    must_change_password=False,
    created_at=datetime(2026, 11, 1, tzinfo=UTC),
)

SOURCE_MODULES = ("evidence.py", "evidence_api.py", "evidence_crypto.py", "evidence_storage.py", "cli.py")
TAMPER_PROOF_CLAIMS = ("tamper-proof", "tamperproof", "tamper proof", "tamper-evident", "cannot be altered")


class RecordingStorage(LocalVolumeStorage):
    """A real local volume that remembers every object key it was asked to write."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.written: list[str] = []

    def put(self, object_key: str, data: bytes) -> None:
        super().put(object_key, data)
        self.written.append(object_key)


# --- T04: stable identity and no mutation of the stored original ----------------------------------------------------


def test_stored_original_is_never_overwritten(tmp_path):
    storage = LocalVolumeStorage(tmp_path)
    key = str(uuid.uuid4())
    storage.put(key, b"original sealed bytes")
    with pytest.raises(StorageError):
        storage.put(key, b"replacement bytes")
    assert storage.get(key) == b"original sealed bytes"


def test_every_upload_gets_its_own_evidence_id_and_object_key(ledger, provider, tmp_path):
    storage = RecordingStorage(tmp_path)
    scope = read_all(str(uuid.uuid4()))
    first = evidence.create_evidence(
        str(uuid.uuid4()), REVIEWER.id, "a.pdf", "application/pdf", PDF, storage, provider, scope,
    )
    first_object = storage.get(storage.written[0])

    second = evidence.create_evidence(
        str(uuid.uuid4()), REVIEWER.id, "b.pdf", "application/pdf", PDF + b"2", storage, provider, scope,
    )

    assert first.id != second.id
    assert len(set(storage.written)) == 2
    # The second upload does not change the object written by the first one.
    assert storage.get(storage.written[0]) == first_object


@requires_database
def test_evidence_id_is_the_same_in_the_case_list_the_lookup_and_the_custody_log(provider, tmp_path):
    user_id, case_id = database_case()
    scope = read_all(user_id)
    item = evidence.create_evidence(
        case_id, user_id, "scan.pdf", "application/pdf", PDF, LocalVolumeStorage(tmp_path), provider, scope,
    )

    listed, total = evidence.list_evidence(case_id, 50, 0)
    stored = evidence.find_stored(item.id, scope)

    assert total == 1
    assert [row.id for row in listed] == [item.id]
    assert stored is not None and stored.id == item.id
    assert {event.evidence_id for event in evidence.custody_events(item.id)} == {item.id}


@requires_database
def test_custody_log_records_the_upload_and_each_access_in_order(provider, tmp_path):
    user_id, case_id = database_case()
    storage = LocalVolumeStorage(tmp_path)
    scope = read_all(user_id)
    item = evidence.create_evidence(case_id, user_id, "scan.pdf", "application/pdf", PDF, storage, provider, scope)

    stored = evidence.find_stored(item.id, scope)
    content = evidence.read_original(stored, storage, provider)
    evidence.record_download(item.id, user_id, case_id)

    assert content == PDF
    events = evidence.custody_events(item.id)
    assert [(event.action, event.actor_id) for event in events] == [("uploaded", user_id), ("downloaded", user_id)]
    assert database_status(item.id) == "available"


# --- T04: visible digest mismatch, with no tamper-proof claim -------------------------------------------------------


def test_digest_mismatch_is_reported_by_its_integrity_reason(provider, tmp_path):
    storage = LocalVolumeStorage(tmp_path)
    key = str(uuid.uuid4())
    sealed = evidence_crypto.seal(PDF, key, provider)
    storage.put(key, sealed.ciphertext)
    stored = evidence.StoredEvidence(
        id=str(uuid.uuid4()),
        case_id=str(uuid.uuid4()),
        original_filename="scan.pdf",
        media_type="application/pdf",
        sha256_plaintext=evidence_crypto.sha256_hex(PDF),
        sha256_ciphertext=evidence_crypto.sha256_hex(sealed.ciphertext),
        storage_key=key,
        key_id=sealed.key_id,
        wrapped_data_key=sealed.wrapped_data_key,
        nonce=sealed.nonce,
        status="available",
    )
    assert evidence.read_original(stored, storage, provider) == PDF

    damaged = bytearray(sealed.ciphertext)
    damaged[10] ^= 0x01
    (tmp_path / key).write_bytes(bytes(damaged))  # the stored object changes after the upload was verified

    with pytest.raises(VerificationFailed) as failure:
        evidence.read_original(stored, storage, provider)
    assert failure.value.reason == "ciphertext_checksum_mismatch"
    assert failure.value.reason in INTEGRITY_FAILURE_REASONS


def test_no_evidence_source_text_claims_tamper_proofing():
    source_dir = Path(evidence.__file__).parent
    for name in SOURCE_MODULES:
        text = (source_dir / name).read_text(encoding="utf-8").lower()
        for claim in TAMPER_PROOF_CLAIMS:
            assert claim not in text, f"{name} claims {claim!r}; a SHA-256 digest only shows a mismatch"


# --- T04: crash recovery of orphaned uploads -----------------------------------------------------------------------


@requires_database
def test_object_left_by_a_crash_is_failed_by_the_reconciliation_job_and_kept_on_disk(provider, tmp_path):
    user_id, case_id = database_case()
    storage = LocalVolumeStorage(tmp_path)
    scope = read_all(user_id)
    key = str(uuid.uuid4())
    sealed = evidence_crypto.seal(PDF, key, provider)
    storage.put(key, sealed.ciphertext)  # the process stopped after the object was written and before it was marked
    with connect() as conn:
        evidence_id = conn.execute(
            """
            INSERT INTO evidence_items (case_id, uploaded_by, original_filename, media_type, size_bytes,
                                        storage_key, key_id, wrapped_data_key, nonce, status, created_at)
            VALUES (%s, %s, 'scan.pdf', 'application/pdf', %s, %s, %s, %s, %s, 'pending',
                    now() - interval '2 hours')
            RETURNING id::text
            """,
            (case_id, user_id, len(PDF), key, sealed.key_id, sealed.wrapped_data_key, sealed.nonce),
        ).fetchone()[0]

    failed_count = worker.reconcile_pending_uploads()

    assert failed_count >= 1
    assert database_status(evidence_id) == "failed"
    last = evidence.custody_events(evidence_id)[-1]
    assert (last.action, last.details) == ("failed", {"reason": "stale_pending"})
    # Nothing is deleted: the orphaned object stays on disk, and it is no longer reachable through the row.
    assert storage.exists(key)
    assert evidence.find_stored(evidence_id, scope).status == "failed"


# --- T05 (F1 subset): absence of a record is never reported as success -----------------------------------------------


@pytest.fixture
def evidence_app(monkeypatch):
    app = FastAPI()
    app.include_router(evidence_api.evidence_router)
    app.dependency_overrides[auth.current_user] = lambda: REVIEWER
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:read", "evidence:write"}))
    monkeypatch.setattr(evidence_api, "is_visible", lambda case_id, scope: True)
    return TestClient(app)


def test_unknown_evidence_custody_and_download_are_404_not_an_empty_success(evidence_app, monkeypatch):
    monkeypatch.setattr(evidence, "find_stored", lambda evidence_id, scope: None)
    unknown = str(uuid.uuid4())

    custody = evidence_app.get(f"/api/v1/evidence/{unknown}/custody")
    download = evidence_app.get(f"/api/v1/evidence/{unknown}/download")

    assert custody.status_code == 404
    assert download.status_code == 404
    assert custody.json()["detail"] == "evidence not found"


# --- T05: the model, runtime and reasoning record has a place in the schema ---------------------------------------------

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def test_model_runtime_and_reasoning_have_a_place_in_the_schema():
    schema = "\n".join(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.sql"))).lower()
    assert all(word in schema for word in ("model", "runtime", "reasoning"))
