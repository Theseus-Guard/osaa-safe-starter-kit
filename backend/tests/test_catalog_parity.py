"""Recommendation list (GET /api/v1/catalog/recommendations), added for CLI and UI parity (plans/03, catalog row).

Unit and API tests, no database: the domain function is replaced, so the access rules, the cursor check, and the response
shape are checked here. The keyset walk over real rows is in tests/test_catalog_parity_db.py.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, catalog_api
from safe_kit.catalog import Recommendation
from safe_kit.users import User

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
USER_ID = "44444444-4444-4444-4444-444444444444"
RECOMMENDATION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
RECOMMENDATIONS = "/api/v1/catalog/recommendations"
ROLE_PERMISSIONS = {
    "reviewer": frozenset({"catalog:read", "catalog:write"}),
    "auditor": frozenset({"catalog:read"}),
    "reporter": frozenset({"cases:submit", "cases:read_own"}),
}


def make_user(role: str) -> User:
    return User(
        id=USER_ID,
        username=f"user-{role}",
        role=role,
        is_active=True,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=NOW,
    )


def recommendation(**changes) -> Recommendation:
    values = {
        "id": RECOMMENDATION_ID,
        "failure_or_system": "tool gateway accepts unscoped calls",
        "required_defence_outcome": "every call is checked against the approved scope",
        "minimum_control": "deny by default at the gateway",
        "control_alternative": None,
        "responsible": "platform owner",
        "due_at": NOW,
        "evidence_to_keep": "gateway policy diff and the test log",
        "created_by": USER_ID,
        "created_at": NOW,
    }
    values.update(changes)
    return Recommendation(**values)


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user("auditor")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(
        auth, "permissions_for_role", lambda role: ROLE_PERMISSIONS.get(role, frozenset())
    )
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(caller):
    return TestClient(api.app)


def test_anonymous_and_unpermitted_callers_are_refused_before_the_read(monkeypatch, caller):
    def fail(*args, **kwargs):
        raise AssertionError("the recommendation list must not run for this request")

    monkeypatch.setattr(catalog_api, "list_recommendations", fail)
    api.app.dependency_overrides.clear()
    assert TestClient(api.app).get(RECOMMENDATIONS).status_code == 401
    api.app.dependency_overrides[auth.current_user] = lambda: make_user("reporter")
    response = TestClient(api.app).get(RECOMMENDATIONS)
    assert response.status_code == 403
    assert response.json()["detail"] == "forbidden"


def test_a_read_only_role_lists_recommendations(monkeypatch, client, caller):
    caller["user"] = make_user("auditor")
    monkeypatch.setattr(
        catalog_api, "list_recommendations", lambda limit, cursor: ([recommendation()], "next-token")
    )
    response = client.get(RECOMMENDATIONS)
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] == "next-token"
    assert [item["id"] for item in body["items"]] == [RECOMMENDATION_ID]
    assert body["items"][0]["responsible"] == "platform owner"


def test_a_recommendation_in_the_list_carries_no_result_or_verified_flag(monkeypatch, client):
    monkeypatch.setattr(
        catalog_api, "list_recommendations", lambda limit, cursor: ([recommendation()], None)
    )
    item = client.get(RECOMMENDATIONS).json()["items"][0]
    assert "result" not in item
    assert "verified" not in item
    assert "verification" not in item


def test_the_list_passes_the_limit_and_the_cursor_to_the_domain(monkeypatch, client):
    seen = {}

    def fake(limit, cursor):
        seen["limit"] = limit
        seen["cursor"] = cursor
        return [], None

    monkeypatch.setattr(catalog_api, "list_recommendations", fake)
    assert client.get(f"{RECOMMENDATIONS}?limit=7").status_code == 200
    assert seen == {"limit": 7, "cursor": None}


def test_an_invalid_cursor_is_refused_before_any_read(client):
    # The cursor is decoded before the database is opened, so this runs without a database.
    response = client.get(f"{RECOMMENDATIONS}?cursor=not-a-cursor")
    assert response.status_code == 400
    assert response.json()["detail"] == {"status": "invalid_cursor"}


def test_a_limit_outside_the_page_bounds_is_refused(client):
    assert client.get(f"{RECOMMENDATIONS}?limit=0").status_code == 422
    assert client.get(f"{RECOMMENDATIONS}?limit=201").status_code == 422
