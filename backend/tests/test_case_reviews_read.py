"""Reviewer read endpoint: GET /api/v1/cases/{case_id}/reviews.

Without a database the service function is replaced and the HTTP mapping is checked. Tests marked "database" run the
flow against PostgreSQL. They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings with the migrations applied (see
test_database.py). They create rows and never remove them, so use a throwaway database.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, system_timezone, users
from safe_kit.cases import CaseScope, Outcome, Review
from safe_kit.db import connect
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import User

REPORTER_ID = "11111111-1111-1111-1111-111111111111"
REVIEWER_ID = "44444444-4444-4444-4444-444444444444"
CASE_ID = "99999999-9999-9999-9999-999999999999"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
NOTE = "needs the export"

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


# --- HTTP mapping without a database ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "auditor", "disclosure_approver"])
def test_roles_without_case_permissions_get_403(client, caller, monkeypatch, role):
    monkeypatch.setattr(api, "list_reviews", lambda *args: pytest.fail("must not reach the service"))
    as_role(caller, "55555555-5555-5555-5555-555555555555", role)
    assert client.get(f"/api/v1/cases/{CASE_ID}/reviews").status_code == 403


def test_unknown_or_invisible_case_is_404_with_the_case_not_found_detail(client, monkeypatch):
    monkeypatch.setattr(api, "list_reviews", lambda case_id, scope: None)
    response = client.get(f"/api/v1/cases/{CASE_ID}/reviews")
    assert response.status_code == 404
    assert response.json()["detail"] == "case not found"


def review(review_id: str, outcome: str, note: str | None) -> Review:
    return Review(id=review_id, case_id=CASE_ID, outcome=outcome, note=note, created_at=NOW)


def test_reviews_use_the_callers_scope_and_keep_the_review_shape(client, monkeypatch):
    seen = {}

    def fake_list(case_id, scope):
        seen["case_id"] = case_id
        seen["scope"] = scope
        return [review("r2", "pass", None), review("r1", "insufficient_evidence", NOTE)]

    monkeypatch.setattr(api, "list_reviews", fake_list)
    response = client.get(f"/api/v1/cases/{CASE_ID}/reviews")
    assert response.status_code == 200
    assert seen == {"case_id": CASE_ID, "scope": CaseScope(user_id=REPORTER_ID, read_own=True)}
    # A reporter sees both reviews, with outcome and time, but no note.
    assert response.json() == [
        {"id": "r2", "case_id": CASE_ID, "outcome": "pass", "note": None, "created_at": "2026-11-01T09:00:00Z"},
        {
            "id": "r1",
            "case_id": CASE_ID,
            "outcome": "insufficient_evidence",
            "note": None,
            "created_at": "2026-11-01T09:00:00Z",
        },
    ]


@pytest.mark.parametrize(
    ("role", "expected_note"),
    [
        ("triager", NOTE),  # holds cases:read_all, the full view
        ("reporter", None),
        ("investigator", None),
        ("reviewer", None),
    ],
)
def test_only_the_full_view_sees_the_note(client, caller, monkeypatch, role, expected_note):
    monkeypatch.setattr(api, "list_reviews", lambda case_id, scope: [review("r1", "fail", NOTE)])
    as_role(caller, REVIEWER_ID, role)
    response = client.get(f"/api/v1/cases/{CASE_ID}/reviews")
    assert response.status_code == 200
    item = response.json()[0]
    assert item["outcome"] == "fail"
    assert item["created_at"] == "2026-11-01T09:00:00Z"
    assert item["note"] == expected_note


def test_the_submitter_gets_their_own_note_back(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(
        api,
        "record_review",
        lambda case_id, outcome, note, scope: (Outcome.OK, review("r1", outcome, note)),
    )
    response = client.post(f"/api/v1/cases/{CASE_ID}/reviews", json={"outcome": "pass", "note": f"  {NOTE}  "})
    assert response.status_code == 201
    # The reviewer has no cases:read_all, but the note on their own submission is their input and stays.
    assert response.json() == {
        "id": "r1",
        "case_id": CASE_ID,
        "outcome": "pass",
        "note": NOTE,
        "created_at": "2026-11-01T09:00:00Z",
    }


def test_empty_review_list_is_an_empty_array(client, monkeypatch):
    monkeypatch.setattr(api, "list_reviews", lambda case_id, scope: [])
    response = client.get(f"/api/v1/cases/{CASE_ID}/reviews")
    assert response.status_code == 200
    assert response.json() == []


# --- database --------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def system_timezone_configured():
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
        "other_reporter": new_user("reporter"),
        "triager": new_user("triager"),
        "reviewer": new_user("reviewer"),
        "investigator": new_user("investigator"),
        "other_investigator": new_user("investigator"),
        "admin": new_user("admin"),
        "auditor": new_user("auditor"),
    }


def create_case_via_api(db_client, reporter: str) -> str:
    response = db_client(reporter).post(
        "/api/v1/cases",
        json={
            "title": "Reviews database case",
            "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@requires_database
def test_reviews_are_listed_newest_first_and_skip_soft_deleted_rows(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    response = db_client(people["triager"]).post(
        f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]}
    )
    assert response.status_code == 201, response.text
    reviewer = db_client(people["reviewer"])
    first = reviewer.post(f"/api/v1/cases/{case_id}/reviews", json={"outcome": "fail", "note": "first"}).json()["id"]
    second = reviewer.post(f"/api/v1/cases/{case_id}/reviews", json={"outcome": "pass", "note": "second"}).json()["id"]

    listed = db_client(people["triager"]).get(f"/api/v1/cases/{case_id}/reviews").json()
    assert [item["id"] for item in listed] == [second, first]
    assert listed[0]["case_id"] == case_id
    assert listed[0]["outcome"] == "pass"
    assert listed[0]["note"] == "second"
    assert listed[0]["created_at"] is not None

    # Soft delete one review directly, as an administrator would. The listing must leave it out.
    with connect() as conn:
        conn.execute("UPDATE case_reviews SET is_deleted = true, deleted_at = now() WHERE id = %s", (second,))
    listed = db_client(people["triager"]).get(f"/api/v1/cases/{case_id}/reviews").json()
    assert [item["id"] for item in listed] == [first]


@requires_database
def test_note_is_shown_only_to_the_full_view(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    db_client(people["triager"]).post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]})
    db_client(people["triager"]).post(
        f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["investigator"]}
    )
    submitted = db_client(people["reviewer"]).post(
        f"/api/v1/cases/{case_id}/reviews", json={"outcome": "insufficient_evidence", "note": "sensitive note"}
    )
    assert submitted.status_code == 201, submitted.text
    # The reviewer's own submission keeps its note; the reviewer has no full view, so the listing hides it.
    assert submitted.json()["note"] == "sensitive note"
    assert submitted.json()["created_at"] is not None

    expected_notes = {
        "triager": "sensitive note",  # cases:read_all
        "reporter": None,
        "investigator": None,
        "reviewer": None,
    }
    for role, expected_note in expected_notes.items():
        response = db_client(people[role]).get(f"/api/v1/cases/{case_id}/reviews")
        assert response.status_code == 200, role
        (item,) = response.json()
        assert item["outcome"] == "insufficient_evidence", role
        assert item["created_at"] is not None, role
        assert item["note"] == expected_note, role


@requires_database
def test_reviews_follow_the_case_visibility_rule(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    db_client(people["triager"]).post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]})
    db_client(people["triager"]).post(
        f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["investigator"]}
    )
    db_client(people["reviewer"]).post(f"/api/v1/cases/{case_id}/reviews", json={"outcome": "pass"})

    for allowed in ("reporter", "triager", "reviewer", "investigator"):
        response = db_client(people[allowed]).get(f"/api/v1/cases/{case_id}/reviews")
        assert response.status_code == 200, allowed
        assert len(response.json()) == 1, allowed

    missing = db_client(people["triager"]).get(f"/api/v1/cases/{uuid.uuid4()}/reviews")
    other_reporter = db_client(people["other_reporter"]).get(f"/api/v1/cases/{case_id}/reviews")
    unassigned = db_client(people["other_investigator"]).get(f"/api/v1/cases/{case_id}/reviews")
    assert missing.status_code == other_reporter.status_code == unassigned.status_code == 404
    assert missing.json() == other_reporter.json() == unassigned.json()

    # Administrators and auditors hold no case permission, so they are refused before the case is looked up.
    assert db_client(people["admin"]).get(f"/api/v1/cases/{case_id}/reviews").status_code == 403
    assert db_client(people["auditor"]).get(f"/api/v1/cases/{case_id}/reviews").status_code == 403
