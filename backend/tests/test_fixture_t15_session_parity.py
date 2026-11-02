"""F2 fixture T15 (session parity): the same account and the same token state give the same refusal through the API
Bearer header, through the browser cookie, and through the CLI.

- Login: a wrong password and a forced password change are refused the same way by the API and the CLI.
- Token states (garbage, expired, revoked, disabled account): the Bearer path and the cookie path both answer 401 with
  the same body, and the CLI reports the same refusal as an authentication error (exit 1, one line, no traceback).
- CSRF on the case routes: a state-changing browser request without the X-CSRF-Token header, or with a different one,
  is refused with 403. A refused session is answered 401 before the CSRF check.

The accounts come from the FakeStore of tests/test_auth_api.py. No database is needed: each refusal here happens
before a case handler runs.
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid
from dataclasses import replace

import pytest

from cli_bridge import install, run
from error_body import without_request_id
from safe_kit import security
from test_auth_api import (  # noqa: F401 - fixtures and helpers of the auth tests
    ALICE,
    PASSWORD,
    bearer_header,
    browser,
    client,
    login,
    login_tokens,
    presenting,
    store,
)

CASES_URL = "/api/v1/cases"
DAVE = "44444444-4444-4444-4444-444444444444"
TOKEN_STATES = ["garbage", "expired", "revoked", "disabled"]


def token_in_state(state: str, client, store, monkeypatch) -> str:
    """An access token in one refused state. Each one would be accepted before the state changed."""
    if state == "garbage":
        return "not-a-token-" + uuid.uuid4().hex
    if state == "expired":
        monkeypatch.setenv("SAFE_ACCESS_TTL_MINUTES", "-1")
        token, _ = security.issue_access_token(ALICE, "alice", "triager")
        return token
    if state == "revoked":
        token = login_tokens(client, "alice")["access_token"]
        assert client.post("/api/v1/auth/logout", headers=bearer_header(token)).status_code == 204
        return token
    if state == "disabled":
        token = login_tokens(client, "alice")["access_token"]
        store.users[ALICE] = replace(store.users[ALICE], is_active=False)
        return token
    raise AssertionError(state)


# --- login ------------------------------------------------------------------------------------------------------------


def test_a_wrong_password_is_refused_the_same_way_by_the_api_and_the_cli(client, store, monkeypatch, capsys):
    wrong = "wrong-" + uuid.uuid4().hex
    api_answer = login(client, "alice", wrong)
    assert api_answer.status_code == 401
    assert api_answer.json()["detail"] == "invalid credentials"
    assert "access_token" not in api_answer.json()

    install(monkeypatch, client)
    code, out, err = run(capsys, monkeypatch, wrong, "case", "list", "--username", "alice")

    assert code == 1
    assert out == ""
    assert err == "error: invalid credentials\n"


def test_a_forced_password_change_is_refused_the_same_way_by_the_api_and_the_cli(client, store, monkeypatch, capsys):
    store.add_user(DAVE, "dave", "triager", must_change=True)
    tokens = login(client, "dave").json()
    assert tokens["password_change_required"] is True
    # The API refuses every business request with this token until the password is changed.
    assert client.get(CASES_URL, headers=bearer_header(tokens["access_token"])).status_code == 403

    install(monkeypatch, client)
    code, out, err = run(capsys, monkeypatch, PASSWORD, "case", "list", "--username", "dave")

    assert code == 1
    assert out == ""
    assert err.startswith("error: you must change your password before continuing")


# --- token states: the Bearer path, the cookie path and the CLI -------------------------------------------------------


@pytest.mark.parametrize("state", TOKEN_STATES)
def test_a_refused_token_gets_the_same_401_on_the_bearer_path_and_on_the_cookie_path(
    client, store, monkeypatch, state
):
    token = token_in_state(state, client, store, monkeypatch)

    bearer = client.get(CASES_URL, headers=bearer_header(token))
    cookie = presenting(("safe_access", token)).get(CASES_URL)

    assert bearer.status_code == cookie.status_code == 401
    assert bearer.json()["detail"] == cookie.json()["detail"] == "invalid credentials"
    assert without_request_id(bearer) == without_request_id(cookie)


def test_a_request_with_no_token_is_401_on_both_paths(client, store):
    assert client.get(CASES_URL).status_code == 401
    assert presenting().get(CASES_URL).status_code == 401


@pytest.mark.parametrize("state", ["garbage", "revoked"])
def test_a_refused_cookie_session_is_401_on_a_case_write_even_when_the_csrf_header_matches(client, store, monkeypatch, state):
    token = token_in_state(state, client, store, monkeypatch)
    browser_client = presenting(("safe_access", token), ("safe_csrf", "value"))

    response = browser_client.post(f"/api/v1/cases/{uuid.uuid4()}/close", json={"reason": "x"}, headers={"X-CSRF-Token": "value"})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


@pytest.mark.parametrize("state", TOKEN_STATES)
def test_the_cli_reports_a_refused_token_as_an_authentication_error(client, store, monkeypatch, capsys, state):
    token = token_in_state(state, client, store, monkeypatch)
    issued = {
        "access_token": token,
        "refresh_token": "refresh-" + uuid.uuid4().hex,
        "token_type": "bearer",
        "expires_in": 900,
        "password_change_required": False,
    }

    install(monkeypatch, client, issued=issued)
    code, out, err = run(capsys, monkeypatch, PASSWORD, "case", "list", "--username", "alice")

    # The CLI's own message for a 401, one line on stderr, exit 1, and no traceback or stack text.
    assert code == 1
    assert out == ""
    assert err == "error: invalid credentials\n"
    assert "Traceback" not in err


# --- CSRF on the case routes ------------------------------------------------------------------------------------------

CASE_WRITES = [
    ("post", "/api/v1/cases/{case_id}/close", {"reason": "closed"}),
    ("post", "/api/v1/cases/{case_id}/reopen", {"reason": "again"}),
    ("patch", "/api/v1/cases/{case_id}", {"title": "Changed title"}),
]


@pytest.mark.parametrize("method,path,body", CASE_WRITES)
def test_a_cookie_case_write_without_the_csrf_header_is_refused(browser, store, method, path, body):
    login(browser, "alice")

    response = getattr(browser, method)(path.format(case_id=uuid.uuid4()), json=body)

    assert response.status_code == 403
    assert response.json()["detail"] == "csrf_token_invalid"


@pytest.mark.parametrize("method,path,body", CASE_WRITES)
def test_a_cookie_case_write_with_a_different_csrf_header_is_refused(browser, store, method, path, body):
    login(browser, "alice")

    response = getattr(browser, method)(
        path.format(case_id=uuid.uuid4()), json=body, headers={"X-CSRF-Token": "not-the-token"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "csrf_token_invalid"
