"""F1 fixture T15 (client errors): invalid, expired and revoked access tokens are refused on a case endpoint, the same
way the other authenticated endpoints refuse them.

The account store and the signing key come from tests/test_auth_api.py. The tokens are issued by the real login
endpoint or by security.issue_access_token, so the refusal happens in the real authentication dependency.
"""

# Fixtures are imported from the sibling test modules and requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F811
import uuid
from dataclasses import replace

from safe_kit import security
from test_auth_api import ALICE, PASSWORD, bearer_header, login_tokens  # noqa: F401 - helpers from the auth tests
from test_auth_api import client, store  # noqa: F401 - fixtures: FakeStore-backed API client

CASES_URL = "/api/v1/cases"


def test_garbage_bearer_token_is_refused_on_a_case_endpoint(client, store):
    response = client.get(CASES_URL, headers=bearer_header("not-a-token"))

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}


def test_missing_token_is_refused_on_a_case_endpoint(client, store):
    assert client.get(CASES_URL).status_code == 401


def test_expired_token_is_refused_on_a_case_endpoint(client, store, monkeypatch):
    monkeypatch.setenv("SAFE_ACCESS_TTL_MINUTES", "-1")
    token, _ = security.issue_access_token(ALICE, "alice", "triager")

    response = client.get(CASES_URL, headers=bearer_header(token))

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}


def test_revoked_token_is_refused_on_a_case_endpoint_after_logout(client, store):
    tokens = login_tokens(client, "alice")
    assert client.get("/api/v1/auth/me", headers=bearer_header(tokens["access_token"])).status_code == 200

    logout = client.post("/api/v1/auth/logout", headers=bearer_header(tokens["access_token"]))
    assert logout.status_code == 204

    response = client.get(CASES_URL, headers=bearer_header(tokens["access_token"]))
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}


def test_token_for_a_disabled_account_is_refused_on_a_case_endpoint(client, store):
    tokens = login_tokens(client, "alice")
    store.users[ALICE] = replace(store.users[ALICE], is_active=False)

    assert client.get(CASES_URL, headers=bearer_header(tokens["access_token"])).status_code == 401


def test_wrong_password_never_yields_a_token_for_a_case_endpoint(client, store):
    response = client.post("/api/v1/auth/token", json={"username": "alice", "password": "wrong-" + uuid.uuid4().hex})

    assert response.status_code == 401
    assert "access_token" not in response.json()
    assert PASSWORD not in response.text
