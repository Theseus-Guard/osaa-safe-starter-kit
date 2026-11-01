"""Case visibility is checked again inside the transaction that inserts the pending evidence row.

The upload route checks visibility before it reads the body. Between that check and the insert, the case can stop
being visible. These tests cover the insert-time check: a refusal writes no row and stores no object.

Most tests need no database. The fake connection answers the visibility query with a flag the test sets.
The database test at the end runs against PostgreSQL and is skipped unless SAFE_TEST_DATABASE=1 and the SAFE_DB_*
settings are set, like tests/test_database.py.
"""

import contextlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safe_kit import auth, evidence, evidence_api, migrate, users
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.evidence import CaseNotVisible
from safe_kit.evidence_keys import LocalFileKeyProvider
from safe_kit.users import User

PDF = b"%PDF-1.7\n" + b"incident detail " * 60 + b"\n%%EOF\n"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

INVESTIGATOR = User(
    id="77777777-7777-7777-7777-777777777777",
    username="ines",
    role="investigator",
    is_active=True,
    password_hash="unused",
    sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
    must_change_password=False,
    created_at=datetime(2026, 11, 1, tzinfo=UTC),
)


class StorageSpy:
    """Records every object written. A refused upload must leave this empty."""

    def __init__(self) -> None:
        self.puts: list[str] = []

    def put(self, object_key: str, data: bytes) -> None:
        self.puts.append(object_key)

    def get(self, object_key: str) -> bytes:
        raise AssertionError("no read is expected in these tests")

    def exists(self, object_key: str) -> bool:
        return False


class LoggingConnection:
    """Logs the SQL it is given. The visibility query answers with the flag of the fake database."""

    def __init__(self, db: "FakeDatabase") -> None:
        self.db = db
        self.last = ""

    def execute(self, sql, params=()):
        self.last = " ".join(sql.split())
        self.db.statements.append(self.last)
        self.db.connection_of_statement.append(id(self))
        return self

    def fetchone(self):
        if self.last.startswith("SELECT 1 FROM cases"):
            return (1,) if self.db.visible else None
        if self.last.startswith("INSERT INTO evidence_items"):
            return (str(uuid.uuid4()),)
        return None

    def fetchall(self):
        return []


class FakeDatabase:
    def __init__(self) -> None:
        self.visible = True
        self.statements: list[str] = []
        self.connection_of_statement: list[int] = []

    def inserts(self) -> list[str]:
        return [sql for sql in self.statements if sql.startswith("INSERT INTO evidence_items")]


@pytest.fixture
def db(monkeypatch) -> FakeDatabase:
    fake = FakeDatabase()

    @contextlib.contextmanager
    def fake_connect():
        yield LoggingConnection(fake)

    monkeypatch.setattr(evidence, "connect", fake_connect)
    return fake


@pytest.fixture
def provider(tmp_path) -> LocalFileKeyProvider:
    path = tmp_path / "kek.bin"
    path.write_bytes(os.urandom(32))
    os.chmod(path, 0o600)
    return LocalFileKeyProvider(path)


# --- create_evidence and _insert_pending ----------------------------------------------------------------------------


def test_case_no_longer_visible_at_insert_is_refused_with_no_row_and_no_object(db, provider):
    storage = StorageSpy()
    db.visible = False  # removed after the API check, before the insert
    scope = CaseScope(user_id=INVESTIGATOR.id, read_own=True)
    with pytest.raises(CaseNotVisible):
        evidence.create_evidence(
            str(uuid.uuid4()), INVESTIGATOR.id, "scan.pdf", "application/pdf", PDF, storage, provider, scope,
        )
    assert db.inserts() == []
    assert storage.puts == []


def test_visibility_is_checked_on_the_same_connection_as_the_insert(db):
    scope = CaseScope(user_id=INVESTIGATOR.id, read_all=True)
    evidence._insert_pending(
        str(uuid.uuid4()), INVESTIGATOR.id, "scan.pdf", "text/plain", 3, str(uuid.uuid4()), "test",
        b"\x00", b"\x00", scope,
    )
    assert len(db.inserts()) == 1
    visibility_checks = [i for i, sql in enumerate(db.statements) if sql.startswith("SELECT 1 FROM cases")]
    insert_at = db.statements.index(db.inserts()[0])
    assert visibility_checks and visibility_checks[-1] < insert_at
    # One connection served the check and the insert, so they share one transaction.
    assert len(set(db.connection_of_statement)) == 1


def test_visible_case_still_gets_its_pending_row(db):
    db.visible = True
    scope = CaseScope(user_id=INVESTIGATOR.id, read_all=True)
    evidence._insert_pending(
        str(uuid.uuid4()), INVESTIGATOR.id, "scan.pdf", "text/plain", 3, str(uuid.uuid4()), "test",
        b"\x00", b"\x00", scope,
    )
    assert len(db.inserts()) == 1


# --- the upload route: the API check passes, the insert-time check refuses -----------------------------------------


@pytest.fixture
def upload_client(monkeypatch, provider, db):
    app = FastAPI()
    app.include_router(evidence_api.evidence_router)
    app.dependency_overrides[auth.current_user] = lambda: INVESTIGATOR
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"evidence:write"}))
    monkeypatch.setattr(evidence_api, "_PROVIDER", provider)
    storage = StorageSpy()
    monkeypatch.setattr(evidence_api, "storage_from_env", lambda: storage)
    # The API check (before the body is read) says visible; the insert-time check in the database says not.
    monkeypatch.setattr(evidence_api, "is_visible", lambda case_id, scope: True)
    db.visible = False
    client = TestClient(app, raise_server_exceptions=False)
    yield client, storage
    app.dependency_overrides.clear()


def test_upload_refused_at_insert_time_is_404_like_a_missing_case_and_stores_nothing(upload_client, db):
    client, storage = upload_client
    case_id = str(uuid.uuid4())
    response = client.post(
        f"/api/v1/cases/{case_id}/evidence",
        files={"file": ("scan.pdf", PDF, "application/pdf")},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "case not found"}
    assert db.inserts() == []
    assert storage.puts == []


# --- database: the same refusal against PostgreSQL ------------------------------------------------------------------

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@requires_database
def test_database_refuses_an_insert_for_a_case_the_scope_cannot_see(provider):
    migrate.MIGRATIONS_DIR = MIGRATIONS
    assert migrate.main() == 0
    suffix = uuid.uuid4().hex[:12]
    creator = users.create_user("v-a-" + suffix, "temporary passphrase", "investigator")
    other = users.create_user("v-b-" + suffix, "temporary passphrase", "investigator")
    with connect() as conn:
        case_id = conn.execute(
            """
            INSERT INTO cases (reference, title, awareness_time, created_by, status, calendar_version, calendar_timezone)
            VALUES (%s, 'visibility case', now(), %s, 'open', 'test', 'UTC') RETURNING id::text
            """,
            ("SAFE-TEST-" + suffix, creator),
        ).fetchone()[0]
    stranger = CaseScope.from_permissions(other, frozenset({"cases:read_own"}))
    storage = StorageSpy()
    with pytest.raises(CaseNotVisible):
        evidence.create_evidence(case_id, other, "scan.pdf", "application/pdf", PDF, storage, provider, stranger)
    assert storage.puts == []
    with connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM evidence_items WHERE case_id = %s", (case_id,)
        ).fetchone()[0]
    assert count == 0
