"""Reporter channel: the reporter's account and contact never reach a caller, and the intake stores the contact apart.

Two layers, as in test_case_access.py:
- Tests without a database replace the service functions and check the rules and the HTTP mapping.
- Tests marked "database" run the flow through the API against PostgreSQL. They need SAFE_TEST_DATABASE=1 and the
  SAFE_DB_* settings with the migrations applied (see test_database.py). They create rows and never remove them, so
  use a throwaway database.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, audit, auth, system_timezone, users
from safe_kit.cases import Case, HistoryEntry
from safe_kit.db import connect
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.reporter_channel import hide_reporter
from safe_kit.users import User

REPORTER_ID = "11111111-1111-1111-1111-111111111111"
TRIAGER_ID = "22222222-2222-2222-2222-222222222222"
REVIEWER_ID = "44444444-4444-4444-4444-444444444444"
CASE_ID = "99999999-9999-9999-9999-999999999999"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
PAST_AWARENESS = "2026-10-31T09:00:00+00:00"

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def make_user(user_id, role):
    return User(
        id=user_id,
        username=f"user-{role}",
        role=role,
        is_active=True,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=NOW,
    )


def sample_case() -> Case:
    return Case(
        id=CASE_ID,
        reference="SAFE-2026-000001",
        title="Sample",
        awareness_time=NOW,
        status="open",
        calendar_version="provisional-1",
        calendar_timezone="Europe/Istanbul",
        created_at=NOW,
        created_by=REPORTER_ID,
        deadlines=[],
    )


# --- rules without a database ----------------------------------------------------------------------------------


def test_hide_reporter_blanks_only_the_case_creation_actor():
    entries = [
        HistoryEntry(id="e1", action=audit.CASE_CREATE, created_at=NOW, actor_id=REPORTER_ID, details={"a": 1}),
        HistoryEntry(id="e2", action=audit.CASE_REVIEW, created_at=NOW, actor_id=REVIEWER_ID, details={"b": 2}),
    ]
    hidden = hide_reporter(entries)
    assert [entry.actor_id for entry in hidden] == [None, REVIEWER_ID]
    # The action, time, and details of the creation entry stay.
    assert hidden[0].action == audit.CASE_CREATE and hidden[0].details == {"a": 1}


# --- HTTP mapping without a database ---------------------------------------------------------------------------


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user(REPORTER_ID, "reporter")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(caller):
    return TestClient(api.app)


def as_role(caller, user_id, role):
    caller["user"] = make_user(user_id, role)


def test_report_passes_the_stripped_contact_and_returns_no_reporter_fields(client, caller, monkeypatch):
    seen = {}

    def fake_submit(title, awareness_time, contact, reporter_id):
        seen.update(title=title, contact=contact, reporter_id=reporter_id)
        return sample_case()

    monkeypatch.setattr(api, "submit_report", fake_submit)
    response = client.post(
        "/api/v1/reports",
        json={"title": "Case", "awareness_time": PAST_AWARENESS, "contact": "  reach me at the desk  "},
    )
    assert response.status_code == 201, response.text
    assert seen == {"title": "Case", "contact": "reach me at the desk", "reporter_id": REPORTER_ID}
    body = response.json()
    assert "contact" not in body and "created_by" not in body
    assert REPORTER_ID not in response.text


def test_report_without_contact_or_with_blank_contact_stores_none(client, caller, monkeypatch):
    seen = []

    def fake_submit(title, awareness_time, contact, reporter_id):
        seen.append(contact)
        return sample_case()

    monkeypatch.setattr(api, "submit_report", fake_submit)
    assert client.post("/api/v1/reports", json={"title": "Case", "awareness_time": PAST_AWARENESS}).status_code == 201
    assert client.post(
        "/api/v1/reports", json={"title": "Case", "awareness_time": PAST_AWARENESS, "contact": "   "}
    ).status_code == 201
    assert seen == [None, None]


def test_contact_longer_than_200_characters_is_refused(client, caller, monkeypatch):
    monkeypatch.setattr(api, "submit_report", lambda *args: pytest.fail("must not reach the service"))
    response = client.post(
        "/api/v1/reports", json={"title": "Case", "awareness_time": PAST_AWARENESS, "contact": "x" * 201}
    )
    assert response.status_code == 422


def test_report_needs_the_submit_permission(client, caller, monkeypatch):
    monkeypatch.setattr(api, "submit_report", lambda *args: pytest.fail("must not reach the service"))
    as_role(caller, TRIAGER_ID, "triager")
    response = client.post("/api/v1/reports", json={"title": "Case", "awareness_time": PAST_AWARENESS})
    assert response.status_code == 403


def test_history_hides_the_reporter_account_from_triagers_too(client, caller, monkeypatch):
    entries = [
        HistoryEntry(
            id="e1",
            action=audit.CASE_CREATE,
            created_at=NOW,
            actor_id=REPORTER_ID,
            details={"calendar_version": "provisional-1"},
        ),
        HistoryEntry(id="e2", action=audit.CASE_REVIEW, created_at=NOW, actor_id=REVIEWER_ID, details={"outcome": "pass"}),
    ]
    monkeypatch.setattr(api, "case_history", lambda case_id, scope, limit, offset, **kwargs: (entries, 2))

    as_role(caller, TRIAGER_ID, "triager")
    response = client.get(f"/api/v1/cases/{CASE_ID}/history")
    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["actor_id"] is None
    assert items[0]["details"] == {"calendar_version": "provisional-1"}
    assert items[1]["actor_id"] == REVIEWER_ID
    assert REPORTER_ID not in response.text


# --- database --------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
    if os.environ.get("SAFE_TEST_DATABASE") != "1":
        yield
        return
    from safe_kit.migrate import main as migrate

    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute("SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)").fetchone()[0]
        if not exists:
            conn.execute("INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')")
    yield


def new_user(role: str) -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


class ActingClient:
    """Makes requests as one real user. The caller is set before every request, so clients never share a caller."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.client = TestClient(api.app)

    def _request(self, method: str, url: str, **kwargs):
        user = users.find_user_by_id(self.user_id)
        assert user is not None
        api.app.dependency_overrides[auth.current_user] = lambda: user
        return getattr(self.client, method)(url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("post", url, **kwargs)


@pytest.fixture
def db_client():
    yield ActingClient
    api.app.dependency_overrides.clear()


@pytest.fixture
def people():
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))
    return {
        "reporter": new_user("reporter"),
        "triager": new_user("triager"),
    }


CONTACT = "reporter contact for database test"


def open_report_via_api(db_client, reporter: str, contact: str | None = CONTACT) -> str:
    response = db_client(reporter).post(
        "/api/v1/reports",
        json={
            "title": "Reporter channel case",
            "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "contact": contact,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@requires_database
def test_report_stores_the_reporter_and_contact_apart_from_the_case(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    with connect() as conn:
        row = conn.execute(
            "SELECT reporter_id::text, contact FROM reporter_identities WHERE case_id = %s AND NOT is_deleted",
            (case_id,),
        ).fetchone()
    assert row == (people["reporter"], CONTACT)


@requires_database
def test_no_caller_gets_the_reporter_account_or_contact(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    responses = [
        db_client(people["reporter"]).get(f"/api/v1/cases/{case_id}"),
        db_client(people["reporter"]).get(f"/api/v1/cases/{case_id}/history"),
        triager.get(f"/api/v1/cases/{case_id}"),
        triager.get(f"/api/v1/cases/{case_id}/history?limit=200"),
        triager.get("/api/v1/cases?limit=200"),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        assert people["reporter"] not in response.text
        assert CONTACT not in response.text

    # The creation entry stays in the history, without the account that made it.
    history = triager.get(f"/api/v1/cases/{case_id}/history").json()["items"]
    created = [item for item in history if item["action"] == audit.CASE_CREATE]
    assert len(created) == 1 and created[0]["actor_id"] is None
