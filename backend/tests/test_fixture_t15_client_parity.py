"""F1 fixture T15 (client parity): the same role and the same case data give the same create, list and show through
the API and through the container CLI, a hidden case looks the same as a missing one in both clients, and a closed
case is not served as open afterwards.

The API and its case store come from tests/test_cases.py (the role is taken from the dependency override, as in
those tests). The CLI runs unchanged: its urllib.request.urlopen is routed into the FastAPI test client. Login and
logout are answered by the bridge, because token issuing is covered by the authentication tests.
The close test needs PostgreSQL and is skipped unless SAFE_TEST_DATABASE=1, like tests/test_database.py.
"""

# Fixtures are imported from the sibling test modules and requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F811
import getpass
import io
import json
import os
import urllib.error
import urllib.parse
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, cases, cli, migrate, reviews, users
from safe_kit.cases import CaseScope, Outcome
from safe_kit.db import connect
from safe_kit.reviews import RoundOutcome
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import User
from test_cases import OTHER_ID, REPORTER_ID, caller, make_user, valid_body  # noqa: F401 - shared case fixtures
from test_cases import store  # noqa: F401 - fixture: in-memory case store
from test_cases import client  # noqa: F401 - API client bound to the case fixtures
from error_body import without_request_id

BASE_URL = "http://api.test:8000"
PASSWORD = "pw-" + "parity-" + uuid.uuid4().hex
ACCESS = "access-parity-" + uuid.uuid4().hex
REFRESH = "refresh-parity-" + uuid.uuid4().hex
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def cli_bridge(monkeypatch, client):
    """Run the real CLI against the FastAPI test client. Every API request the CLI makes goes through the bridge."""
    monkeypatch.setenv("SAFE_API_URL", BASE_URL)
    monkeypatch.delenv("SAFE_USERNAME", raising=False)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(PASSWORD + "\n"))

    def no_prompt(prompt=""):
        raise AssertionError("getpass must not be used when stdin is not a terminal")

    monkeypatch.setattr(getpass, "getpass", no_prompt)
    token_body = {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "token_type": "bearer",
        "expires_in": 900,
        "password_change_required": False,
    }

    def urlopen(request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        method = request.get_method()
        if (method, parts.path) == ("POST", "/api/v1/auth/token"):
            return _Response(200, json.dumps(token_body).encode("utf-8"))
        if (method, parts.path) == ("POST", "/api/v1/auth/logout"):
            return _Response(204, b"")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        body = json.loads(request.data) if request.data else None
        headers = {"Authorization": request.get_header("Authorization")} if request.has_header("Authorization") else {}
        if request.has_header("Idempotency-key"):
            headers["Idempotency-Key"] = request.get_header("Idempotency-key")
        response = client.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, response.status_code, "error", {}, io.BytesIO(response.content),
            )
        return _Response(response.status_code, response.content)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)
    return client


def run(capsys, *argv: str) -> tuple[int, str, str]:
    # The CLI reads the password from stdin once per run, so each run gets a new stream. The fixture's monkeypatch
    # restores the original stdin at teardown.
    cli.sys.stdin = io.StringIO(PASSWORD + "\n")
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def cli_json(capsys, *argv: str) -> tuple[int, object]:
    code, out, err = run(capsys, *argv, "--username", "alice", "--json")
    assert err == "", err
    return code, json.loads(out) if out.strip() else None


# --- same role, same data: create, list and show agree between the API and the CLI ----------------------------------


def test_case_created_through_the_cli_reads_back_identically_through_the_api(cli_bridge, store, capsys):
    awareness = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    code, created = cli_json(capsys, "case", "create", "--title", "Suspected token leak", "--awareness-time", awareness)
    assert code == 0
    case_id = created["id"]

    api_show = cli_bridge.get(f"/api/v1/cases/{case_id}")
    assert api_show.status_code == 200
    assert api_show.json() == created
    code, shown = cli_json(capsys, "case", "show", case_id)
    assert code == 0 and shown == api_show.json()


def test_list_of_the_callers_cases_is_the_same_through_the_api_and_the_cli(cli_bridge, caller, store, capsys):
    cli_bridge.post("/api/v1/cases", json=valid_body(), headers=idem())
    caller["user"] = make_user(OTHER_ID, "reporter")
    cli_bridge.post("/api/v1/cases", json=valid_body(title="Somebody else's case"), headers=idem())
    caller["user"] = make_user(REPORTER_ID, "reporter")

    api_list = cli_bridge.get("/api/v1/cases?limit=50&offset=0")
    code, listed = cli_json(capsys, "case", "list", "--limit", "50", "--offset", "0")

    assert api_list.status_code == 200
    assert code == 0
    assert listed == api_list.json()
    assert listed["total"] == 1
    assert [item["title"] for item in listed["items"]] == ["Suspected token leak"]


def test_hidden_case_and_missing_case_look_the_same_in_the_api_and_the_cli(cli_bridge, caller, store, capsys):
    caller["user"] = make_user(OTHER_ID, "reporter")
    hidden_id = cli_bridge.post("/api/v1/cases", json=valid_body(), headers=idem()).json()["id"]
    caller["user"] = make_user(REPORTER_ID, "reporter")
    missing_id = str(uuid.uuid4())

    api_hidden = cli_bridge.get(f"/api/v1/cases/{hidden_id}")
    api_missing = cli_bridge.get(f"/api/v1/cases/{missing_id}")
    assert api_hidden.status_code == api_missing.status_code == 404
    # The request ID is different on every request. Everything else must be the same.
    assert without_request_id(api_hidden) == without_request_id(api_missing)

    cli_hidden = run(capsys, "case", "show", hidden_id, "--username", "alice")
    cli_missing = run(capsys, "case", "show", missing_id, "--username", "alice")
    assert cli_hidden == cli_missing
    assert cli_hidden[0] == 1
    assert "case not found" in cli_hidden[2]


# --- stale data after a close (database) -----------------------------------------------------------------------------


def ensure_migrated_with_a_timezone() -> None:
    migrate.MIGRATIONS_DIR = MIGRATIONS
    assert migrate.main() == 0
    with connect() as conn:
        exists = conn.execute("SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)").fetchone()[0]
        if not exists:
            conn.execute("INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')")


@pytest.fixture
def triager_api(monkeypatch):
    user = User(
        id="", username="fixture-triager", role="triager", is_active=True, password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC), must_change_password=False,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    holder = {"user": user}
    api.app.dependency_overrides[auth.current_user] = lambda: holder["user"]
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    yield holder
    api.app.dependency_overrides.clear()


@requires_database
def test_closed_case_is_not_served_as_open_after_the_close(triager_api):
    ensure_migrated_with_a_timezone()
    suffix = uuid.uuid4().hex[:12]
    triager_id = users.create_user("t-" + suffix, "temporary passphrase", "triager")
    triager_api["user"] = User(
        id=triager_id, username="t-" + suffix, role="triager", is_active=True, password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC), must_change_password=False,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    scope = CaseScope.from_permissions(triager_id, frozenset({"cases:read_all", "cases:close"}))
    case = cases.create_case("Close check " + suffix, datetime.now(UTC) - timedelta(hours=1), triager_id)
    with connect() as conn:
        # Test setup: close the deadline tasks and record the one review that closure requires.
        conn.execute(
            "UPDATE deadline_tasks SET status = 'done', not_applicable_reason = NULL WHERE case_id = %s", (case.id,)
        )
        round_id = conn.execute(
            "INSERT INTO review_rounds (case_id, opened_by) VALUES (%s, %s) RETURNING id",
            (case.id, triager_id),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO review_layers (round_id, layer, outcome, set_by, set_at)
            VALUES (%s, 'model', 'pass', %s, now())
            """,
            (round_id, triager_id),
        )
    assert cases.close_case(case.id, "closed for the fixture", scope) == (Outcome.OK, [])

    client = TestClient(api.app)
    shown = client.get(f"/api/v1/cases/{case.id}")
    listed = client.get("/api/v1/cases?limit=200&offset=0")

    assert shown.status_code == 200
    assert shown.json()["status"] == "closed"
    assert shown.json()["closed_at"] is not None
    assert [item["status"] for item in listed.json()["items"] if item["id"] == case.id] == ["closed"]
    # A late review or a second close is refused with the closed state, not accepted against a stale open case.
    assert reviews.open_round(case.id, scope)[0] is RoundOutcome.CLOSED
    assert client.post(f"/api/v1/cases/{case.id}/close", json={"reason": "again"}, headers=idem()).status_code == 409
