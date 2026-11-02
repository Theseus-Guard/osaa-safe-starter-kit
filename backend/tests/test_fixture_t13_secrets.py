"""F1 fixture T13 (secrets and uploads): no reusable secret in API responses or logs, and evidence uploads that
refuse a disallowed media type or an archive-like name that could leave the store.

The login fixtures come from tests/test_auth_api.py: an in-memory account store and a real JWT signing key file.
The upload tests use the ledger fake from test_evidence_lifecycle and a real LocalVolumeStorage in a temporary
directory, so nothing reaches PostgreSQL. Test values are generated or already used in the suite.
"""

# Fixtures are imported from the sibling test modules and requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F811
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safe_kit import auth, evidence, evidence_api
from safe_kit.evidence_storage import InvalidObjectKey, LocalVolumeStorage
from safe_kit.users import User
from test_auth_api import ALICE, PASSWORD, bearer_header, login_tokens  # noqa: F401 - helpers from the auth tests
from test_auth_api import client, store  # noqa: F401 - fixtures: FakeStore-backed API client
from test_evidence_lifecycle import PDF, read_all  # noqa: F401 - sample PDF and scope helper
from test_evidence_lifecycle import ledger, provider  # noqa: F401 - fixtures from the evidence lifecycle tests


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


def signing_key_text() -> str:
    return Path(os.environ["SAFE_JWT_KEY_FILE"]).read_text(encoding="utf-8").strip()


def wrong_password() -> str:
    return "wrong-" + uuid.uuid4().hex


def multipart(filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    boundary = "fixtureboundary" + uuid.uuid4().hex
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + data + tail, f"multipart/form-data; boundary={boundary}"


# --- T13: no reusable secret in responses or logs -----------------------------------------------------------------


def test_login_response_carries_no_password_no_signing_key_and_no_password_hash(
    client, store
):
    response = client.post(
        "/api/v1/auth/token", json={"username": "alice", "password": PASSWORD}
    )

    assert response.status_code == 200
    assert PASSWORD not in response.text
    assert signing_key_text() not in response.text
    assert store.users[ALICE].password_hash not in response.text


def test_failed_login_response_does_not_echo_the_submitted_password(client, store):
    attempt = wrong_password()

    response = client.post(
        "/api/v1/auth/token", json={"username": "alice", "password": attempt}
    )

    assert response.status_code == 401
    assert attempt not in response.text
    assert PASSWORD not in response.text


def test_refresh_response_has_no_password_key_or_previous_refresh_token(client, store):
    first = login_tokens(client, "alice")

    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )

    assert response.status_code == 200
    body = response.text
    assert first["refresh_token"] not in body
    assert PASSWORD not in body
    assert signing_key_text() not in body


def test_admin_reset_returns_no_body_and_the_new_password_is_not_in_the_response(
    client, store
):
    admin = login_tokens(client, "carol")["access_token"]
    temporary = "temporary passphrase"

    response = client.post(
        f"/api/v1/users/{ALICE}/password",
        headers=bearer_header(admin),
        json={"temporary_password": temporary},
    )

    assert response.status_code == 204
    assert response.content == b""
    assert temporary not in response.text
    assert PASSWORD not in response.text


def test_login_and_refresh_write_no_password_or_token_to_the_logs(
    client, store, caplog
):
    caplog.set_level(logging.DEBUG)
    attempt = wrong_password()
    client.post("/api/v1/auth/token", json={"username": "alice", "password": attempt})
    tokens = login_tokens(client, "alice")
    refreshed = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    ).json()

    logged = caplog.text
    for secret in (
        PASSWORD,
        attempt,
        signing_key_text(),
        tokens["refresh_token"],
        tokens["access_token"],
        refreshed["refresh_token"],
    ):
        assert secret not in logged


def test_validation_error_does_not_echo_the_submitted_password(client, store):
    too_long = "probe-" + uuid.uuid4().hex + "x" * 1024

    response = client.post(
        "/api/v1/auth/token", json={"username": "alice", "password": too_long}
    )

    assert response.status_code == 400
    assert too_long not in response.text


# --- T13: evidence uploads that must be refused or kept inside the store -------------------------------------------


INVESTIGATOR = User(
    id="88888888-8888-8888-8888-888888888888",
    username="fixture-investigator",
    role="investigator",
    is_active=True,
    password_hash="unused",
    sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
    must_change_password=False,
    created_at=datetime(2026, 11, 1, tzinfo=UTC),
)


@pytest.fixture
def upload_client(monkeypatch, provider):
    app = FastAPI()
    app.include_router(evidence_api.evidence_router)
    app.dependency_overrides[auth.current_user] = lambda: INVESTIGATOR
    monkeypatch.setattr(
        auth,
        "permissions_for_role",
        lambda role: frozenset({"evidence:read", "evidence:write"}),
    )
    monkeypatch.setattr(evidence_api, "is_visible", lambda case_id, scope: True)
    monkeypatch.setattr(evidence_api, "_PROVIDER", provider)
    monkeypatch.setenv("SAFE_EVIDENCE_MAX_BYTES", "100000")
    return TestClient(app)


ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 64


def test_archive_bytes_declared_as_pdf_are_refused_and_nothing_is_stored(
    upload_client, monkeypatch
):
    def must_not_run(*args, **kwargs):
        raise AssertionError(
            "create_evidence must not run when the bytes do not match the declared type"
        )

    monkeypatch.setattr(evidence, "create_evidence", must_not_run)
    body, content_type = multipart("report.pdf", "application/pdf", ZIP_BYTES)

    response = upload_client.post(
        f"/api/v1/cases/{uuid.uuid4()}/evidence",
        content=body,
        headers={**idem(), "Content-Type": content_type},
    )

    assert response.status_code == 415


def test_archive_declared_as_an_archive_is_refused_before_storage(
    upload_client, monkeypatch
):
    def must_not_run(*args, **kwargs):
        raise AssertionError("create_evidence must not run for an archive")

    monkeypatch.setattr(evidence, "create_evidence", must_not_run)
    body, content_type = multipart("bundle.zip", "application/zip", ZIP_BYTES)

    response = upload_client.post(
        f"/api/v1/cases/{uuid.uuid4()}/evidence",
        content=body,
        headers={**idem(), "Content-Type": content_type},
    )

    assert response.status_code == 415


@pytest.mark.parametrize(
    "raw_name",
    [
        "../../../etc/escape.pdf",
        "..\\..\\..\\escape.pdf",
        "/var/lib/safe/evidence/escape.pdf",
        "nested/../../escape.pdf",
    ],
)
def test_archive_like_or_traversal_name_is_kept_as_a_plain_display_name(
    raw_name, ledger, provider, tmp_path, monkeypatch
):
    stored_names: list[str] = []
    real_insert = evidence._insert_pending

    def record_name(case_id, uploader_id, name, *rest, **kwargs):
        stored_names.append(name)
        return real_insert(case_id, uploader_id, name, *rest)

    monkeypatch.setattr(evidence, "_insert_pending", record_name)
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    storage = LocalVolumeStorage(store_dir)
    scope = read_all(str(uuid.uuid4()))

    evidence.create_evidence(
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        raw_name,
        "application/pdf",
        PDF,
        storage,
        provider,
        scope,
    )

    assert stored_names == ["escape.pdf"]
    # The object key is a generated UUID, so the object lands directly in the store and nowhere else. The only other
    # file under tmp_path is the key file written by the provider fixture.
    outside = [
        entry.name
        for entry in tmp_path.rglob("*")
        if entry.is_file() and store_dir not in entry.parents
    ]
    assert outside == ["kek.bin"]
    stored_files = list(store_dir.iterdir())
    assert len(stored_files) == 1
    assert str(uuid.UUID(stored_files[0].name)) == stored_files[0].name


def test_object_key_that_would_leave_the_store_is_refused_and_writes_nothing(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    storage = LocalVolumeStorage(store_dir)
    with pytest.raises(InvalidObjectKey):
        storage.put("../escape", b"data")
    assert list(tmp_path.iterdir()) == [store_dir]
