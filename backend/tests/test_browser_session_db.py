"""Browser session against PostgreSQL: a login that sets the cookies, a cookie-authenticated write with and without the
CSRF header, and a logout that revokes the session in the database.

Every test here is marked "database". They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, with the migrations
applied; scripts/test-backend.sh --db sets them up on a throwaway PostgreSQL container.
"""

import os
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, users
from safe_kit.db import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
    from safe_kit.migrate import main as migrate

    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute("SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)").fetchone()[0]
        if not exists:
            conn.execute("INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')")
    yield


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    api.app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def signing_key(tmp_path, monkeypatch):
    """Access tokens are signed with a key read from SAFE_JWT_KEY_FILE. A random key is written for each test run."""
    key_file = tmp_path / "jwt_signing_key.txt"
    key_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    monkeypatch.setenv("SAFE_JWT_KEY_FILE", str(key_file))


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def past(hours: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def browser_login(username: str, password: str) -> TestClient:
    """A client that behaves like the browser: it keeps the cookies and talks over https, so Secure cookies are sent."""
    browser = TestClient(api.app, base_url="https://testserver")
    response = browser.post("/api/v1/auth/token", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return browser


def test_cookie_login_write_and_logout_against_postgresql():
    username = "t-" + uuid.uuid4().hex[:12]
    user_id = users.create_user(username, "temporary passphrase", "reporter")
    # The forced first-login change is done directly here, so the test can log in with a password that is not temporary.
    users.change_own_password(user_id, "my own passphrase")
    # Tokens are rejected if issued in the same second as a password change (iat has whole seconds). Wait it out.
    time.sleep(1.1)

    browser = browser_login(username, "my own passphrase")
    access_cookie = browser.cookies.get("safe_access")
    csrf_value = browser.cookies.get("safe_csrf")
    assert access_cookie and browser.cookies.get("safe_refresh") and csrf_value

    # The login wrote a refresh-token row for this user.
    with connect() as conn:
        open_sessions = conn.execute(
            "SELECT count(*) FROM refresh_tokens WHERE user_id = %s AND revoked_at IS NULL", (user_id,)
        ).fetchone()[0]
    assert open_sessions >= 1

    # A cookie-authenticated write is refused without the CSRF header and goes through with it.
    body = {"title": "Cookie session case", "awareness_time": past()}
    refused = browser.post("/api/v1/cases", json=body, headers=idem())
    assert refused.status_code == 403
    assert refused.json()["detail"] == "csrf_token_invalid"

    created = browser.post("/api/v1/cases", json=body, headers={**idem(), "X-CSRF-Token": csrf_value})
    assert created.status_code == 201, created.text
    assert created.json()["title"] == "Cookie session case"

    # Logout with the CSRF header clears the cookies and revokes the session in the database.
    logout = browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_value})
    assert logout.status_code == 204
    assert browser.cookies.get("safe_access") is None
    assert browser.cookies.get("safe_refresh") is None

    with connect() as conn:
        still_open = conn.execute(
            "SELECT count(*) FROM refresh_tokens WHERE user_id = %s AND revoked_at IS NULL", (user_id,)
        ).fetchone()[0]
    assert still_open == 0

    # The access token of the ended session is refused, even when it is presented again.
    replay = TestClient(api.app, base_url="https://testserver")
    replay.cookies.set("safe_access", access_cookie)
    assert replay.get("/api/v1/auth/me").status_code == 401
