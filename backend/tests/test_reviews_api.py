"""Review round endpoints: open a round, list and read rounds, set one layer (see reviews.py and reviews_api.py).

Without a database the service functions are replaced and the HTTP mapping is checked. Tests marked "database" run the
flow against PostgreSQL. They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings with the migrations applied (see
test_database.py). They create rows and never remove them, so use a throwaway database.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, audit, auth, reviews_api, system_timezone, users
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.idempotency import StoredResponse, fingerprint, valid_key
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit import reviews
from safe_kit.reviews import TEMPLATE_LAYERS, LayerEntry, Round, RoundOutcome
from safe_kit.users import User


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}

REPORTER_ID = "11111111-1111-1111-1111-111111111111"
REVIEWER_ID = "44444444-4444-4444-4444-444444444444"
ADMIN_ID = "55555555-5555-5555-5555-555555555555"
CASE_ID = "99999999-9999-9999-9999-999999999999"
ROUND_ID = "12121212-1212-1212-1212-121212121212"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
REASON = "needs the export"

PLAN_LAYERS = (
    "model",
    "instructions",
    "safeguards",
    "tools",
    "environment",
    "monitoring",
    "human-operations",
    "supply-chain",
    "serving-path",
    "authorization-and-commitment",
    "workflow-composition",
)

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


def fake_round(revision: int = 2, reason: str | None = REASON, outcome: str = "fail") -> Round:
    """A round whose model layer is set and whose other layers are not set."""
    layers = tuple(
        LayerEntry(
            layer=name,
            outcome=outcome if name == "model" else None,
            reason=reason if name == "model" else None,
            evidence_ids=(),
            set_at=NOW if name == "model" else None,
        )
        for name in TEMPLATE_LAYERS
    )
    return Round(
        id=ROUND_ID,
        case_id=CASE_ID,
        status="open",
        revision=revision,
        opened_at=NOW,
        closed_at=None,
        layers=layers,
    )


# --- rules and HTTP mapping without a database ----------------------------------------------------------------------


def test_the_template_has_the_eleven_plan_layers_in_order():
    assert TEMPLATE_LAYERS == PLAN_LAYERS


@pytest.mark.parametrize("role", ["admin", "auditor", "disclosure_approver"])
def test_roles_without_case_permissions_get_403(client, caller, monkeypatch, role):
    monkeypatch.setattr(reviews_api, "list_rounds", lambda *args, **kwargs: pytest.fail("must not reach the service"))
    monkeypatch.setattr(reviews_api, "open_round", lambda *args, **kwargs: pytest.fail("must not reach the service"))
    as_role(caller, ADMIN_ID, role)
    assert client.get("/api/v1/reviews", params={"case_id": CASE_ID}).status_code == 403
    assert client.get(f"/api/v1/reviews/{ROUND_ID}").status_code == 403
    assert client.post("/api/v1/reviews", headers=idem(), json={"case_id": CASE_ID}).status_code == 403
    response = client.patch(
        f"/api/v1/reviews/{ROUND_ID}/layers/model", json={"outcome": "pass", "expected_revision": 1}, headers=idem()
    )
    assert response.status_code == 403


def test_the_case_level_review_endpoints_are_removed(client):
    assert client.post(f"/api/v1/cases/{CASE_ID}/reviews", json={"outcome": "pass"}).status_code == 404
    assert client.get(f"/api/v1/cases/{CASE_ID}/reviews").status_code == 404


def test_list_needs_a_case_id(client):
    assert client.get("/api/v1/reviews").status_code == 422


def test_unknown_or_invisible_case_is_404_with_the_case_not_found_detail(client, monkeypatch):
    monkeypatch.setattr(reviews_api, "list_rounds", lambda case_id, scope: None)
    response = client.get("/api/v1/reviews", params={"case_id": CASE_ID})
    assert response.status_code == 404
    assert response.json() == {"detail": "case not found"}


def test_unknown_or_invisible_round_is_404_with_the_review_not_found_detail(client, monkeypatch):
    monkeypatch.setattr(reviews_api, "get_round", lambda round_id, scope: None)
    response = client.get(f"/api/v1/reviews/{ROUND_ID}")
    assert response.status_code == 404
    assert response.json() == {"detail": "review not found"}


def test_round_response_lists_the_layers_and_has_no_overall_result(client, monkeypatch):
    seen = {}

    def fake_list(case_id, scope):
        seen["case_id"] = case_id
        seen["scope"] = scope
        return [fake_round()]

    monkeypatch.setattr(reviews_api, "list_rounds", fake_list)
    response = client.get("/api/v1/reviews", params={"case_id": CASE_ID})
    assert response.status_code == 200
    assert seen == {"case_id": CASE_ID, "scope": CaseScope(user_id=REPORTER_ID, read_own=True)}
    (body,) = response.json()
    # A round has no single compliance value: only its state, revision, and layers.
    assert set(body) == {"id", "case_id", "status", "revision", "opened_at", "closed_at", "layers"}
    assert [layer["layer"] for layer in body["layers"]] == list(PLAN_LAYERS)
    assert set(body["layers"][0]) == {"layer", "outcome", "reason", "evidence_ids", "set_at"}
    assert body["layers"][0]["outcome"] == "fail"
    assert body["layers"][1]["outcome"] is None


@pytest.mark.parametrize(
    ("role", "expected_reason"),
    [
        ("triager", REASON),  # holds cases:read_all, the full view
        ("reporter", None),
        ("investigator", None),
        ("reviewer", None),
    ],
)
def test_only_the_full_view_sees_a_layer_reason(client, caller, monkeypatch, role, expected_reason):
    monkeypatch.setattr(reviews_api, "get_round", lambda round_id, scope: fake_round())
    as_role(caller, REVIEWER_ID, role)
    response = client.get(f"/api/v1/reviews/{ROUND_ID}")
    assert response.status_code == 200
    model = response.json()["layers"][0]
    assert model["outcome"] == "fail"
    assert model["reason"] == expected_reason


def test_open_round_maps_refusals_and_success(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(reviews_api, "open_round", lambda *args, **kwargs: (RoundOutcome.NOT_ASSIGNED, None, None))
    response = client.post("/api/v1/reviews", headers=idem(), json={"case_id": CASE_ID})
    assert response.status_code == 403
    assert response.json() == {"detail": "only an assigned reviewer can record a review"}

    monkeypatch.setattr(reviews_api, "open_round", lambda *args, **kwargs: (RoundOutcome.CLOSED, None, None))
    assert client.post("/api/v1/reviews", headers=idem(), json={"case_id": CASE_ID}).json() == {"detail": "case is closed"}

    monkeypatch.setattr(reviews_api, "open_round", lambda *args, **kwargs: (RoundOutcome.NOT_FOUND, None, None))
    assert client.post("/api/v1/reviews", headers=idem(), json={"case_id": CASE_ID}).json() == {"detail": "case not found"}

    monkeypatch.setattr(
        reviews_api, "open_round", lambda *args, **kwargs: (RoundOutcome.OK, fake_round(revision=1), None)
    )
    created = client.post("/api/v1/reviews", headers=idem(), json={"case_id": CASE_ID})
    assert created.status_code == 201
    assert created.json()["revision"] == 1


def test_open_round_requires_an_idempotency_key(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(reviews_api, "open_round", lambda *args, **kwargs: pytest.fail("must not reach the service"))
    missing = client.post("/api/v1/reviews", json={"case_id": CASE_ID})
    assert missing.status_code == 400
    assert missing.json() == {"detail": {"status": "idempotency_key_required"}}


@pytest.mark.parametrize("key", ["has space", "x" * 129, ""])
def test_idempotency_key_must_be_1_to_128_visible_ascii(client, caller, monkeypatch, key):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(reviews_api, "open_round", lambda *args, **kwargs: pytest.fail("must not reach the service"))
    response = client.post("/api/v1/reviews", headers={"Idempotency-Key": key}, json={"case_id": CASE_ID})
    assert response.status_code == 400
    assert response.json() == {"detail": {"status": "idempotency_key_invalid"}}


def test_a_permission_refusal_comes_before_the_key_check(client, caller):
    as_role(caller, ADMIN_ID, "admin")
    assert client.post("/api/v1/reviews", json={"case_id": CASE_ID}).status_code == 403


def test_a_stored_answer_is_replayed_with_its_status_and_body(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    stored = {"id": ROUND_ID, "case_id": CASE_ID, "revision": 1}
    monkeypatch.setattr(
        reviews_api,
        "open_round",
        lambda *args, **kwargs: (RoundOutcome.REPLAY, None, StoredResponse(status_code=201, body=stored)),
    )
    response = client.post("/api/v1/reviews", headers={"Idempotency-Key": "k-1"}, json={"case_id": CASE_ID})
    assert response.status_code == 201
    assert response.json() == stored


def test_a_key_used_with_another_request_is_refused_without_its_content(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(reviews_api, "open_round", lambda *args, **kwargs: (RoundOutcome.KEY_REUSED, None, None))
    response = client.post("/api/v1/reviews", headers={"Idempotency-Key": "k-1"}, json={"case_id": CASE_ID})
    assert response.status_code == 409
    assert response.json() == {"detail": {"status": "idempotency_key_reused"}}


def test_the_key_scope_uses_the_principal_route_and_key(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    seen = {}

    def fake_open_round(case_id, scope, *, idempotency, render):
        seen["scope"] = idempotency
        return RoundOutcome.NOT_FOUND, None, None

    monkeypatch.setattr(reviews_api, "open_round", fake_open_round)
    client.post("/api/v1/reviews", headers={"Idempotency-Key": "k-1"}, json={"case_id": CASE_ID})
    scope = seen["scope"]
    assert (scope.principal_id, scope.method, scope.route, scope.key) == (REVIEWER_ID, "POST", "/api/v1/reviews", "k-1")
    assert scope.fingerprint == fingerprint("POST", "/api/v1/reviews", "", {"case_id": CASE_ID})


def test_fingerprint_is_the_same_for_the_same_request_and_differs_otherwise():
    base = fingerprint("POST", "/api/v1/reviews", "", {"case_id": CASE_ID, "a": 1, "b": 2})
    assert base == fingerprint("POST", "/api/v1/reviews", "", {"b": 2, "a": 1, "case_id": CASE_ID})
    assert base != fingerprint("POST", "/api/v1/reviews", "", {"case_id": str(uuid.uuid4()), "a": 1, "b": 2})
    assert base != fingerprint("POST", "/api/v1/other", "", {"case_id": CASE_ID, "a": 1, "b": 2})
    assert base != fingerprint("POST", "/api/v1/reviews", "x=1", {"case_id": CASE_ID, "a": 1, "b": 2})


def test_key_format_bounds():
    assert valid_key("a" * 128) and valid_key("!~") and valid_key("0123-uuid-like")
    assert not valid_key("") and not valid_key("a" * 129) and not valid_key(" a") and not valid_key("é") and not valid_key(None)


def test_open_round_needs_a_case_id(client, caller):
    as_role(caller, REVIEWER_ID, "reviewer")
    assert client.post("/api/v1/reviews", headers=idem(), json={}).status_code == 422
    assert client.post("/api/v1/reviews", headers=idem(), json={"case_id": "not-a-uuid"}).status_code == 422


def test_layer_must_be_one_of_the_eleven_template_layers(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(reviews_api, "set_layer", lambda *args, **kwargs: pytest.fail("must not reach the service"))
    body = {"outcome": "pass", "expected_revision": 1}
    assert client.patch(f"/api/v1/reviews/{ROUND_ID}/layers/unknown", json=body, headers=idem()).status_code == 422
    # The URL keys are the plan names; the storage name of a migrated entry is not a layer a caller can set.
    assert client.patch(f"/api/v1/reviews/{ROUND_ID}/layers/legacy-case-review", json=body, headers=idem()).status_code == 422


@pytest.mark.parametrize("outcome", ["fail", "insufficient_evidence", "not_applicable"])
def test_reason_is_required_for_every_outcome_except_pass(client, caller, monkeypatch, outcome):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(reviews_api, "set_layer", lambda *args, **kwargs: pytest.fail("must not reach the service"))
    url = f"/api/v1/reviews/{ROUND_ID}/layers/model"
    assert client.patch(url, headers=idem(), json={"outcome": outcome, "expected_revision": 1}).status_code == 422
    assert client.patch(
        url, json={"outcome": outcome, "reason": "   ", "expected_revision": 1}, headers=idem()
    ).status_code == 422


def test_pass_needs_evidence_and_is_refused_without_it(client, caller):
    # A pass is supported only by evidence (T06). Refused before any database access, with or without a reason.
    as_role(caller, REVIEWER_ID, "reviewer")
    url = f"/api/v1/reviews/{ROUND_ID}/layers/model"
    for body in (
        {"outcome": "pass", "expected_revision": 1},
        {"outcome": "pass", "reason": "Checked", "evidence_ids": [], "expected_revision": 1},
    ):
        response = client.patch(url, json=body, headers=idem())
        assert response.status_code == 422
        assert response.json() == {"detail": "a pass needs at least one evidence item of this case"}


def test_set_layer_refuses_a_pass_without_evidence_before_the_database_is_used(monkeypatch):
    def no_database(*args, **kwargs):
        raise AssertionError("the database must not be used for a pass without evidence")

    monkeypatch.setattr(reviews, "connect", no_database)
    scope = CaseScope.from_permissions(REVIEWER_ID, frozenset({"reviews:write"}))
    outcome, review = reviews.set_layer(ROUND_ID, "model", "pass", None, [], 1, scope)
    assert outcome is RoundOutcome.EVIDENCE_REQUIRED
    assert review is None


def test_expected_revision_is_required(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(reviews_api, "set_layer", lambda *args, **kwargs: pytest.fail("must not reach the service"))
    assert client.patch(f"/api/v1/reviews/{ROUND_ID}/layers/model", json={"outcome": "pass"}, headers=idem()).status_code == 422


def test_reason_is_limited_to_1000_characters(client, caller):
    as_role(caller, REVIEWER_ID, "reviewer")
    body = {"outcome": "fail", "reason": "x" * 1001, "expected_revision": 1}
    assert client.patch(f"/api/v1/reviews/{ROUND_ID}/layers/model", json=body, headers=idem()).status_code == 422


def test_evidence_references_are_passed_through_as_identifiers(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    evidence = str(uuid.uuid4())
    seen = {}

    def fake_set_layer(round_id, layer, outcome, reason, evidence_ids, expected_revision, scope, **kwargs):
        seen.update(round_id=round_id, layer=layer, outcome=outcome, reason=reason)
        seen.update(evidence_ids=evidence_ids, expected_revision=expected_revision)
        return RoundOutcome.OK, fake_round(revision=3)

    monkeypatch.setattr(reviews_api, "set_layer", fake_set_layer)
    response = client.patch(
        f"/api/v1/reviews/{ROUND_ID}/layers/model",
        json={"outcome": "fail", "reason": f"  {REASON}  ", "evidence_ids": [evidence], "expected_revision": 2}, headers=idem(),
    )
    assert response.status_code == 200
    assert seen == {
        "round_id": ROUND_ID,
        "layer": "model",
        "outcome": "fail",
        "reason": REASON,  # trimmed
        "evidence_ids": [evidence],
        "expected_revision": 2,
    }


def test_the_setter_gets_back_its_own_reason_and_the_other_layers_stay_hidden(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(reviews_api, "set_layer", lambda *args, **kwargs: (RoundOutcome.OK, fake_round(revision=2)))
    response = client.patch(
        f"/api/v1/reviews/{ROUND_ID}/layers/model",
        json={"outcome": "fail", "reason": REASON, "expected_revision": 1}, headers=idem(),
    )
    assert response.status_code == 200
    assert response.json()["layers"][0]["reason"] == REASON


def test_stale_revision_returns_409_with_the_current_revision_only(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    monkeypatch.setattr(
        reviews_api, "set_layer", lambda *args, **kwargs: (RoundOutcome.REVISION_CONFLICT, fake_round(revision=4))
    )
    response = client.patch(
        f"/api/v1/reviews/{ROUND_ID}/layers/model",
        json={"outcome": "fail", "reason": REASON, "expected_revision": 1}, headers=idem(),
    )
    assert response.status_code == 409
    # The refused write returns the current revision and nothing else: no layers, no reasons.
    assert response.json() == {"detail": {"status": "revision_conflict", "current_revision": 4}}


def test_layer_refusals_map_to_their_status_codes(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    body = {"outcome": "pass", "expected_revision": 1}
    url = f"/api/v1/reviews/{ROUND_ID}/layers/model"

    monkeypatch.setattr(reviews_api, "set_layer", lambda *args, **kwargs: (RoundOutcome.NOT_ASSIGNED, None))
    response = client.patch(url, headers=idem(), json=body)
    assert response.status_code == 403
    assert response.json() == {"detail": "only an assigned reviewer can record a review"}

    monkeypatch.setattr(reviews_api, "set_layer", lambda *args, **kwargs: (RoundOutcome.CLOSED, None))
    assert client.patch(url, headers=idem(), json=body).json() == {"detail": "case is closed"}

    monkeypatch.setattr(reviews_api, "set_layer", lambda *args, **kwargs: (RoundOutcome.ROUND_CLOSED, None))
    assert client.patch(url, headers=idem(), json=body).json() == {"detail": "review round is closed"}

    monkeypatch.setattr(reviews_api, "set_layer", lambda *args, **kwargs: (RoundOutcome.NOT_FOUND, None))
    assert client.patch(url, headers=idem(), json=body).json() == {"detail": "review not found"}

    monkeypatch.setattr(reviews_api, "set_layer", lambda *args, **kwargs: (RoundOutcome.UNKNOWN_EVIDENCE, None))
    assert client.patch(url, headers=idem(), json=body).json() == {"detail": "evidence is not available on this case"}


# --- database --------------------------------------------------------------------------------------------------------


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

    def patch(self, url, **kwargs):
        return self._request("patch", url, **kwargs)


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
            "title": "Review rounds database case",
            "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        }, headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def assign_reviewer(db_client, people: dict, case_id: str) -> None:
    response = db_client(people["triager"]).post(
        f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]}, headers=idem()
    )
    assert response.status_code == 201, response.text


def open_round(db_client, people: dict, case_id: str) -> dict:
    response = db_client(people["reviewer"]).post("/api/v1/reviews", headers=idem(), json={"case_id": case_id})
    assert response.status_code == 201, response.text
    return response.json()


def layer(round_body: dict, name: str) -> dict:
    (found,) = [item for item in round_body["layers"] if item["layer"] == name]
    return found


@requires_database
def test_open_round_creates_eleven_unset_layers_at_revision_one(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    created = open_round(db_client, people, case_id)
    assert created["revision"] == 1
    assert created["status"] == "open"
    assert [item["layer"] for item in created["layers"]] == list(PLAN_LAYERS)
    assert all(item["outcome"] is None and item["reason"] is None for item in created["layers"])


@requires_database
def test_each_change_increments_the_revision_and_a_stale_write_changes_nothing(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    reviewer = db_client(people["reviewer"])
    review_id = open_round(db_client, people, case_id)["id"]
    url = f"/api/v1/reviews/{review_id}/layers/model"

    first = reviewer.patch(url, json={"outcome": "fail", "reason": "first", "expected_revision": 1}, headers=idem())
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 2

    stale = reviewer.patch(url, json={"outcome": "fail", "reason": "stale", "expected_revision": 1}, headers=idem())
    assert stale.status_code == 409
    assert stale.json() == {"detail": {"status": "revision_conflict", "current_revision": 2}}

    current = db_client(people["triager"]).get(f"/api/v1/reviews/{review_id}").json()
    assert current["revision"] == 2
    assert layer(current, "model")["outcome"] == "fail"
    assert layer(current, "model")["reason"] == "first"

    # A request that changes nothing stores nothing and keeps the revision.
    unchanged = reviewer.patch(url, json={"outcome": "fail", "reason": "first", "expected_revision": 2}, headers=idem())
    assert unchanged.status_code == 200
    assert unchanged.json()["revision"] == 2

    second = reviewer.patch(url, json={"outcome": "fail", "reason": "second", "expected_revision": 2}, headers=idem())
    assert second.status_code == 200
    assert second.json()["revision"] == 3
    assert layer(second.json(), "model")["reason"] == "second"


@requires_database
def test_reason_rules_on_the_stored_layer(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    reviewer = db_client(people["reviewer"])
    review_id = open_round(db_client, people, case_id)["id"]
    url = f"/api/v1/reviews/{review_id}/layers/tools"

    for outcome in ("insufficient_evidence", "not_applicable"):
        assert reviewer.patch(url, json={"outcome": outcome, "expected_revision": 1}, headers=idem()).status_code == 422
    assert reviewer.patch(
        url, json={"outcome": "not_applicable", "reason": "  ", "expected_revision": 1}, headers=idem()
    ).status_code == 422

    refused = reviewer.patch(url, json={"outcome": "pass", "expected_revision": 1}, headers=idem())
    assert refused.status_code == 422
    assert refused.json() == {"detail": "a pass needs at least one evidence item of this case"}
    assert layer(review_body(db_client, people["reviewer"], review_id), "tools")["outcome"] is None

    evidence_id = insert_evidence(case_id, people["investigator"], "available")
    passed = reviewer.patch(
        url, json={"outcome": "pass", "evidence_ids": [evidence_id], "expected_revision": 1}, headers=idem()
    )
    assert passed.status_code == 200
    assert layer(passed.json(), "tools")["outcome"] == "pass"
    assert layer(passed.json(), "tools")["reason"] is None


@requires_database
def test_a_pass_must_cite_an_available_item_of_its_own_case(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    reviewer = db_client(people["reviewer"])
    review_id = open_round(db_client, people, case_id)["id"]
    url = f"/api/v1/reviews/{review_id}/layers/model"
    other_case = create_case_via_api(db_client, people["reporter"])
    refused_for = {
        "an item of another case": insert_evidence(other_case, people["investigator"], "available"),
        "a pending item of this case": insert_evidence(case_id, people["investigator"], "pending"),
    }
    for label, evidence_id in refused_for.items():
        response = reviewer.patch(
            url, json={"outcome": "pass", "evidence_ids": [evidence_id], "expected_revision": 1}, headers=idem()
        )
        assert response.status_code == 422, label
        assert response.json() == {"detail": "evidence is not available on this case"}, label
    # Nothing was stored: the round still has its eleven layers not set, at revision 1.
    body = review_body(db_client, people["reviewer"], review_id)
    assert body["revision"] == 1
    assert layer(body, "model")["outcome"] is None


def insert_evidence(case_id: str, uploader_id: str, status: str) -> str:
    """One evidence item row, written directly. Only the status and the case matter to the review rules."""
    with connect() as conn:
        return conn.execute(
            """
            INSERT INTO evidence_items (case_id, uploaded_by, original_filename, media_type, size_bytes, storage_key,
                                        key_id, wrapped_data_key, nonce, status, sha256_plaintext, sha256_ciphertext)
            VALUES (%s, %s, 'note.txt', 'text/plain', 1, %s, 'test', '\\x00'::bytea, '\\x00'::bytea, %s, %s, %s)
            RETURNING id::text
            """,
            (
                case_id,
                uploader_id,
                str(uuid.uuid4()),
                status,
                "0" * 64 if status == "available" else None,
                "0" * 64 if status == "available" else None,
            ),
        ).fetchone()[0]


def review_body(db_client, user: str, review_id: str) -> dict:
    response = db_client(user).get(f"/api/v1/reviews/{review_id}")
    assert response.status_code == 200, response.text
    return response.json()


@requires_database
def test_evidence_reference_must_be_an_available_item_of_the_case(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    reviewer = db_client(people["reviewer"])
    review_id = open_round(db_client, people, case_id)["id"]
    body = {"outcome": "fail", "reason": REASON, "evidence_ids": [str(uuid.uuid4())], "expected_revision": 1}
    response = reviewer.patch(f"/api/v1/reviews/{review_id}/layers/model", json=body, headers=idem())
    assert response.status_code == 422
    assert response.json() == {"detail": "evidence is not available on this case"}


@requires_database
def test_reads_follow_the_case_visibility_and_the_reason_rule(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    db_client(people["triager"]).post(
        f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["investigator"]}, headers=idem()
    )
    review_id = open_round(db_client, people, case_id)["id"]
    db_client(people["reviewer"]).patch(
        f"/api/v1/reviews/{review_id}/layers/model",
        json={"outcome": "insufficient_evidence", "reason": "sensitive reason", "expected_revision": 1}, headers=idem(),
    )

    expected_reasons = {
        "triager": "sensitive reason",  # cases:read_all
        "reporter": None,
        "investigator": None,
        "reviewer": None,
    }
    for role, expected_reason in expected_reasons.items():
        listed = db_client(people[role]).get("/api/v1/reviews", params={"case_id": case_id})
        assert listed.status_code == 200, role
        (item,) = listed.json()
        assert layer(item, "model")["outcome"] == "insufficient_evidence", role
        assert layer(item, "model")["reason"] == expected_reason, role
        single = db_client(people[role]).get(f"/api/v1/reviews/{review_id}")
        assert single.status_code == 200, role
        assert layer(single.json(), "model")["reason"] == expected_reason, role

    missing_case = db_client(people["triager"]).get("/api/v1/reviews", params={"case_id": str(uuid.uuid4())})
    other_reporter = db_client(people["other_reporter"]).get("/api/v1/reviews", params={"case_id": case_id})
    unassigned = db_client(people["other_investigator"]).get("/api/v1/reviews", params={"case_id": case_id})
    assert missing_case.status_code == other_reporter.status_code == unassigned.status_code == 404
    assert missing_case.json() == other_reporter.json() == unassigned.json() == {"detail": "case not found"}

    hidden = db_client(people["other_investigator"]).get(f"/api/v1/reviews/{review_id}")
    absent = db_client(people["triager"]).get(f"/api/v1/reviews/{uuid.uuid4()}")
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json() == {"detail": "review not found"}

    assert db_client(people["admin"]).get("/api/v1/reviews", params={"case_id": case_id}).status_code == 403
    assert db_client(people["auditor"]).get(f"/api/v1/reviews/{review_id}").status_code == 403


@requires_database
def test_an_unassigned_reviewer_cannot_open_a_round_or_set_a_layer(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    hidden = db_client(people["reviewer"]).post("/api/v1/reviews", headers=idem(), json={"case_id": case_id})
    absent = db_client(people["reviewer"]).post("/api/v1/reviews", headers=idem(), json={"case_id": str(uuid.uuid4())})
    # 403 would say "only an assigned reviewer" and so reveal that the case exists.
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json()

    assign_reviewer(db_client, people, case_id)
    review_id = open_round(db_client, people, case_id)["id"]
    investigator = db_client(people["investigator"])
    assert investigator.patch(
        f"/api/v1/reviews/{review_id}/layers/model",
        json={"outcome": "fail", "reason": "Reviewed for this test", "expected_revision": 1},
        headers=idem(),
    ).status_code == 403


@requires_database
def test_soft_deleted_rounds_and_layers_are_not_listed(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    review_id = open_round(db_client, people, case_id)["id"]
    with connect() as conn:
        conn.execute(
            "UPDATE review_layers SET is_deleted = true, deleted_at = now() WHERE round_id = %s AND layer = 'tools'",
            (review_id,),
        )
    body = db_client(people["triager"]).get(f"/api/v1/reviews/{review_id}").json()
    assert "tools" not in [item["layer"] for item in body["layers"]]
    assert len(body["layers"]) == 10

    with connect() as conn:
        conn.execute(
            "UPDATE review_rounds SET is_deleted = true, deleted_at = now() WHERE id = %s",
            (review_id,),
        )
    assert db_client(people["triager"]).get(f"/api/v1/reviews/{review_id}").status_code == 404
    listed = db_client(people["triager"]).get("/api/v1/reviews", params={"case_id": case_id}).json()
    assert listed == []


# --- Idempotency-Key on opening a round ------------------------------------------------------------------------------


def _key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


@requires_database
def test_the_same_key_and_body_replays_the_first_answer_and_writes_nothing_again(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    reviewer = db_client(people["reviewer"])
    key = _key()
    first = reviewer.post("/api/v1/reviews", headers=key, json={"case_id": case_id})
    again = reviewer.post("/api/v1/reviews", headers=key, json={"case_id": case_id})
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    with connect() as conn:
        rounds = conn.execute("SELECT count(*) FROM review_rounds WHERE case_id = %s", (case_id,)).fetchone()[0]
        opened = conn.execute(
            "SELECT count(*) FROM audit_events WHERE target_id = %s AND action = %s AND NOT is_deleted",
            (case_id, audit.CASE_REVIEW_ROUND_OPENED),
        ).fetchone()[0]
    assert rounds == 1
    assert opened == 1


@requires_database
def test_the_same_key_with_another_request_is_refused_and_creates_nothing(db_client, people):
    case_a = create_case_via_api(db_client, people["reporter"])
    case_b = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_a)
    assign_reviewer(db_client, people, case_b)
    reviewer = db_client(people["reviewer"])
    key = _key()
    assert reviewer.post("/api/v1/reviews", headers=key, json={"case_id": case_a}).status_code == 201
    reused = reviewer.post("/api/v1/reviews", headers=key, json={"case_id": case_b})
    assert reused.status_code == 409
    assert reused.json() == {"detail": {"status": "idempotency_key_reused"}}
    with connect() as conn:
        rounds = conn.execute("SELECT count(*) FROM review_rounds WHERE case_id = %s", (case_b,)).fetchone()[0]
    assert rounds == 0


@requires_database
def test_keys_are_scoped_to_the_principal(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    second_reviewer = new_user("reviewer")
    response = db_client(people["triager"]).post(
        f"/api/v1/cases/{case_id}/assignments", json={"user_id": second_reviewer}, headers=idem()
    )
    assert response.status_code == 201, response.text
    key = _key()
    first = db_client(people["reviewer"]).post("/api/v1/reviews", headers=key, json={"case_id": case_id})
    other = db_client(second_reviewer).post("/api/v1/reviews", headers=key, json={"case_id": case_id})
    assert first.status_code == other.status_code == 201
    assert first.json()["id"] != other.json()["id"]


@requires_database
def test_a_refused_request_stores_no_key_so_the_retry_with_the_same_key_succeeds(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    reviewer = db_client(people["reviewer"])
    key = _key()
    refused = reviewer.post("/api/v1/reviews", headers=key, json={"case_id": case_id})
    assert refused.status_code == 404  # not assigned yet
    assign_reviewer(db_client, people, case_id)
    retried = reviewer.post("/api/v1/reviews", headers=key, json={"case_id": case_id})
    assert retried.status_code == 201
    with connect() as conn:
        records = conn.execute(
            "SELECT count(*) FROM idempotency_keys WHERE principal_id = %s AND key = %s AND NOT is_deleted",
            (people["reviewer"], key["Idempotency-Key"]),
        ).fetchone()[0]
    assert records == 1


@requires_database
def test_a_key_older_than_the_replay_window_counts_as_a_new_request(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    reviewer = db_client(people["reviewer"])
    key = _key()
    first = reviewer.post("/api/v1/reviews", headers=key, json={"case_id": case_id})
    assert first.status_code == 201
    with connect() as conn:
        conn.execute(
            """
            UPDATE idempotency_keys SET created_at = now() - interval '8 days'
            WHERE principal_id = %s AND key = %s AND NOT is_deleted
            """,
            (people["reviewer"], key["Idempotency-Key"]),
        )
    second = reviewer.post("/api/v1/reviews", headers=key, json={"case_id": case_id})
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]
    with connect() as conn:
        expired = conn.execute(
            "SELECT count(*) FROM idempotency_keys WHERE key = %s AND is_deleted",
            (key["Idempotency-Key"],),
        ).fetchone()[0]
        live = conn.execute(
            "SELECT count(*) FROM idempotency_keys WHERE key = %s AND NOT is_deleted",
            (key["Idempotency-Key"],),
        ).fetchone()[0]
    assert (expired, live) == (1, 1)


# --- migration of the F1 case reviews --------------------------------------------------------------------------------

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0030_review_rounds.sql"


def legacy_backfill_sql() -> str:
    """The idempotent copy of case_reviews rows, taken from the migration between its markers."""
    text = MIGRATION.read_text(encoding="utf-8")
    start = text.index("-- BEGIN legacy backfill")
    end = text.index("-- END legacy backfill")
    return text[start:end]


@requires_database
def test_legacy_case_reviews_are_copied_into_closed_rounds_once(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assign_reviewer(db_client, people, case_id)
    with connect() as conn:
        live_id = conn.execute(
            """
            INSERT INTO case_reviews (case_id, reviewer_id, outcome, note) VALUES (%s, %s, 'fail', 'legacy note')
            RETURNING id::text
            """,
            (case_id, people["reviewer"]),
        ).fetchone()[0]
        removed_id = conn.execute(
            """
            INSERT INTO case_reviews (case_id, reviewer_id, outcome, note, is_deleted, deleted_at)
            VALUES (%s, %s, 'pass', NULL, true, now()) RETURNING id::text
            """,
            (case_id, people["reviewer"]),
        ).fetchone()[0]
        reviews_before = conn.execute("SELECT count(*) FROM case_reviews WHERE case_id = %s", (case_id,)).fetchone()[0]

    # The migration has already run once; running its copy again must add nothing, and the rows stay as they were.
    for _ in range(2):
        with connect() as conn:
            conn.execute(legacy_backfill_sql())

    with connect() as conn:
        rounds = conn.execute(
            """
            SELECT rr.id::text, rr.status, rr.revision, rr.legacy_case_review_id::text, rr.is_deleted, rr.closed_at IS NOT NULL
            FROM review_rounds rr WHERE rr.case_id = %s AND rr.legacy_case_review_id IS NOT NULL
            """,
            (case_id,),
        ).fetchall()
        by_legacy = {row[3]: row for row in rounds}
        assert len(rounds) == 2
        assert by_legacy[live_id][1:3] == ("closed", 1)
        assert by_legacy[live_id][4] is False
        assert by_legacy[live_id][5] is True
        assert by_legacy[removed_id][4] is True  # the soft-deleted history is copied too, as deleted

        entries = conn.execute(
            """
            SELECT rl.round_id::text, rl.layer, rl.outcome, rl.reason, rl.set_by::text
            FROM review_layers rl JOIN review_rounds rr ON rr.id = rl.round_id
            WHERE rr.case_id = %s
            """,
            (case_id,),
        ).fetchall()
        assert len(entries) == 2
        live_entry = next(entry for entry in entries if entry[0] == by_legacy[live_id][0])
        assert live_entry[1:] == ("legacy-case-review", "fail", "legacy note", people["reviewer"])

        reviews_after = conn.execute("SELECT count(*) FROM case_reviews WHERE case_id = %s", (case_id,)).fetchone()[0]
        assert reviews_after == reviews_before  # nothing was removed from the F1 table

        decided = conn.execute(
            """
            SELECT count(*) FROM review_layers rl JOIN review_rounds rr ON rr.id = rl.round_id
            WHERE rr.case_id = %s AND NOT rr.is_deleted AND NOT rl.is_deleted AND rl.outcome IS NOT NULL
            """,
            (case_id,),
        ).fetchone()[0]
        assert decided == 1  # the live legacy entry is the only decided layer, so closure still counts one review
