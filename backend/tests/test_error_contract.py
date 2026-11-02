"""The error contract (plans/f0/api-draft.md sections 3.3 and 3.6): one body shape for every error, request IDs, the body
size limit, and the readiness body. The existing "detail" field is kept next to the new "error" object.
"""

import re
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import User

SOME_UUID = "11111111-1111-4111-8111-111111111111"
REQUEST_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
LIMIT = 256 * 1024
SECRET = "probe-secret-value-4c1e"


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app)


def _sign_in_as(monkeypatch, role: str) -> None:
    """A signed-in user with the default permissions of the role, so that body validation is reached."""
    actor = User(
        id="22222222-2222-4222-8222-222222222222", username=f"user-{role}", role=role, is_active=True,
        password_hash="unused", sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False, created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    api.app.dependency_overrides[auth.current_user] = lambda: actor
    monkeypatch.setattr(auth, "permissions_for_role", lambda name: DEFAULT_ROLE_PERMISSIONS.get(name, frozenset()))


@pytest.fixture
def signed_in(monkeypatch):
    """A reviewer, for the review routes."""
    _sign_in_as(monkeypatch, "reviewer")
    yield
    api.app.dependency_overrides.clear()


def assert_error_body(response, status: int, code: str, *, retryable: bool = False) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert "detail" in body, "the existing detail field must stay"
    error = body["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert error["retryable"] is retryable
    assert error["request_id"] == response.headers["X-Request-ID"]
    return body


# --- one body shape across routes -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("method", "path", "status", "code", "detail"),
    [
        ("GET", f"/api/v1/cases/{SOME_UUID}", 401, "unauthenticated", "invalid credentials"),
        ("GET", f"/api/v1/evidence/{SOME_UUID}", 401, "unauthenticated", "invalid credentials"),
        ("PATCH", f"/api/v1/reviews/{SOME_UUID}/layers/model", 401, "unauthenticated", "invalid credentials"),
    ],
)
def test_auth_cases_evidence_and_reviews_keep_detail_and_add_error(client, method, path, status, code, detail):
    response = client.request(method, path)

    body = assert_error_body(response, status, code)
    assert body["detail"] == detail


def test_validation_error_on_auth_keeps_the_error_list_and_names_the_fields(client):
    response = client.post("/api/v1/auth/token", json={"username": "alice", "password": SECRET + "x" * 1100})

    body = assert_error_body(response, 400, "validation_failed")
    assert isinstance(body["detail"], list)
    assert [field["path"] for field in body["error"]["fields"]] == ["password"]
    assert body["error"]["fields"][0]["problem"] == "string_too_long"
    assert SECRET not in response.text, "a rejected value is never returned"


def test_a_validator_message_that_quotes_the_value_is_not_returned(client, monkeypatch):
    # The classification trigger check names the unknown trigger in its message. The response must not repeat it.
    _sign_in_as(monkeypatch, "triager")
    response = client.put(
        f"/api/v1/cases/{SOME_UUID}/classification",
        headers={"Idempotency-Key": "key-4"},
        json={"expected_version": 0, "triggers": [SECRET]},
    )

    assert_error_body(response, 400, "validation_failed")
    assert SECRET not in response.text


def test_missing_field_is_a_validation_error_with_its_path(client):
    response = client.post("/api/v1/auth/token", json={"username": "alice"})

    body = assert_error_body(response, 400, "validation_failed")
    assert {"path": "password", "problem": "missing"} in body["error"]["fields"]


def test_unknown_field_is_400_unknown_field_not_422(client, signed_in):
    # AppealRequest forbids extra fields. The body names the field, and the code says it is unknown.
    response = client.post(
        f"/api/v1/reviews/{SOME_UUID}/appeals",
        headers={"Idempotency-Key": "key-1"},
        json={"grounds": "The layer outcome is wrong.", "reviewer_id": SECRET},
    )

    body = assert_error_body(response, 400, "unknown_field")
    assert {"path": "reviewer_id", "problem": "unknown_field"} in body["error"]["fields"]
    assert SECRET not in response.text


def test_a_domain_refusal_stays_422_with_its_detail_and_gains_the_domain_code(client, signed_in):
    # A pass needs evidence of the case. The domain refuses it before any database access (reviews.set_layer).
    response = client.patch(
        f"/api/v1/reviews/{SOME_UUID}/layers/model",
        headers={"Idempotency-Key": "key-3", "If-Match": '"1"'},
        json={"outcome": "pass", "evidence_ids": []},
    )

    body = assert_error_body(response, 422, "domain_rule_violated")
    assert body["detail"] == "a pass needs at least one evidence item of this case"


def test_error_without_fields_has_no_fields_key(client):
    response = client.get(f"/api/v1/cases/{SOME_UUID}")

    assert "fields" not in assert_error_body(response, 401, "unauthenticated")["error"]


# --- request IDs ------------------------------------------------------------------------------------------------

def test_a_request_id_the_client_sends_is_echoed_in_the_header_and_the_body(client):
    response = client.post("/api/v1/auth/token", json={}, headers={"X-Request-ID": "client-trace.42"})

    body = assert_error_body(response, 400, "validation_failed")
    assert response.headers["X-Request-ID"] == "client-trace.42"
    assert body["error"]["request_id"] == "client-trace.42"


def test_a_request_id_is_generated_when_the_client_sends_none(client):
    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert REQUEST_ID.fullmatch(response.headers["X-Request-ID"])


def test_each_request_gets_its_own_request_id(client):
    first = client.get("/api/v1/health/live").headers["X-Request-ID"]
    second = client.get("/api/v1/health/live").headers["X-Request-ID"]

    assert first != second


@pytest.mark.parametrize("sent", ["has spaces", "x" * 129, "bad\x7fchar", ""])
def test_a_malformed_request_id_is_replaced(client, sent):
    response = client.get("/api/v1/health/live", headers={"X-Request-ID": sent})

    assert REQUEST_ID.fullmatch(response.headers["X-Request-ID"])
    assert response.headers["X-Request-ID"] != sent


def test_success_responses_keep_their_shape_and_gain_only_the_header(client):
    response = client.get("/api/v1/health/live")

    assert response.json() == {"status": "ok"}
    assert "X-Request-ID" in response.headers


# --- body size limit --------------------------------------------------------------------------------------------

def test_a_declared_body_over_the_limit_is_413_with_the_error_shape(client):
    response = client.post(
        "/api/v1/auth/token",
        content=b"x" * (LIMIT + 1),
        headers={"Content-Type": "application/json"},
    )

    body = assert_error_body(response, 413, "payload_too_large")
    assert body["detail"] == {"status": "payload_too_large"}


def test_a_body_without_a_declared_length_is_counted_and_refused_at_the_limit(client):
    def chunks() -> Iterator[bytes]:
        for _ in range(LIMIT // 1024 + 1):
            yield b"y" * 1024

    response = client.post("/api/v1/auth/token", content=chunks(), headers={"Content-Type": "application/json"})

    assert_error_body(response, 413, "payload_too_large")


def test_a_body_exactly_at_the_limit_is_not_refused_for_its_size(client):
    response = client.post(
        "/api/v1/auth/token",
        content=b"z" * LIMIT,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code != 413


def test_the_import_route_keeps_its_own_limit_and_is_exempt_from_the_json_limit(client):
    # The import reads its own limit (1 MiB). Without authentication it refuses with 401, not 413.
    response = client.post(
        "/api/v1/cases/import",
        content=b"[" + b"0," * (LIMIT // 2) + b"0]",
        headers={"Content-Type": "application/json", "Idempotency-Key": "key-2"},
    )

    assert response.status_code == 401


# --- readiness --------------------------------------------------------------------------------------------------

class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, *args, **kwargs):
        return None


def test_readiness_is_200_with_the_same_body_when_the_database_answers(client, monkeypatch):
    monkeypatch.setattr(api, "connect", lambda: _Connection())
    monkeypatch.setattr(api, "admin_exists", lambda: True)

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_is_503_in_the_error_shape_when_the_database_is_unreachable(monkeypatch):
    def unreachable():
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(api, "connect", unreachable)
    client = TestClient(api.app)

    body = assert_error_body(client.get("/api/v1/health/ready"), 503, "temporarily_unavailable", retryable=True)
    assert body["detail"] == {"status": "unavailable"}
    assert body["error"]["message"]


def test_readiness_names_the_missing_first_administrator_without_retrying(monkeypatch):
    monkeypatch.setattr(api, "connect", lambda: _Connection())
    monkeypatch.setattr(api, "admin_exists", lambda: False)
    client = TestClient(api.app)

    body = assert_error_body(client.get("/api/v1/health/ready"), 503, "setup_required")
    assert body["detail"] == {"status": "setup_required"}


# --- unexpected failures ----------------------------------------------------------------------------------------

def test_an_unexpected_failure_is_500_with_no_detail_of_the_cause(monkeypatch):
    def broken():
        raise RuntimeError("internal detail that must not leave the server")

    monkeypatch.setattr(api, "connect", broken)
    client = TestClient(api.app, raise_server_exceptions=False)

    response = client.get("/api/v1/health/ready")

    body = assert_error_body(response, 500, "internal_error")
    assert "internal detail" not in response.text
    assert body["detail"] == "Internal Server Error"
