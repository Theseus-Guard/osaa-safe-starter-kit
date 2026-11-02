"""Risk impact values (safe_kit.impact and impact_api.py). Unit and API tests, no database.

The domain functions are replaced, so the risk score, the request checks, the access rules, the HTTP mapping, the
idempotency answers, and the risk fields of the case responses are checked here. The versions, the audit rows, and the
database constraints are checked in tests/test_impact_db.py.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, impact, impact_api
from safe_kit.cases import Case
from safe_kit.idempotency import StoredResponse
from safe_kit.impact import ImpactOutcome, ImpactResult, ImpactScores, impact_changes, risk_score
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import User

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
USER_ID = "44444444-4444-4444-4444-444444444444"
CASE_ID = "11111111-2222-4333-8444-555555555555"
SCORES_PATH = f"/api/v1/cases/{CASE_ID}/impact-scores"


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


def scores(**changes) -> ImpactScores:
    values = {
        "case_id": CASE_ID,
        "version": 2,
        "confidentiality": 4,
        "integrity": 3,
        "availability": 4,
        "created_by": USER_ID,
        "created_at": NOW,
    }
    values.update(changes)
    return ImpactScores(**values)


def case(**changes) -> Case:
    values = {
        "id": CASE_ID,
        "reference": "SAFE-2026-000001",
        "title": "Phishing campaign",
        "awareness_time": NOW,
        "status": "open",
        "calendar_version": "2026.1",
        "calendar_timezone": "UTC",
        "created_at": NOW,
        "created_by": USER_ID,
    }
    values.update(changes)
    return Case(**values)


@pytest.fixture
def actor(monkeypatch):
    state = {"role": "triager"}
    monkeypatch.setattr(
        auth,
        "permissions_for_role",
        lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()),
    )
    api.app.dependency_overrides[auth.current_user] = lambda: make_user(state["role"])
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(actor):
    return TestClient(api.app)


@pytest.fixture
def no_domain(monkeypatch):
    """A refused request must never reach the domain layer."""

    def fail(*args, **kwargs):
        raise AssertionError("the impact service must not run for this request")

    monkeypatch.setattr(impact_api, "update_impact_scores", fail)
    monkeypatch.setattr(impact_api, "read_impact_scores", fail)


def body(**changes) -> dict:
    values = {"expected_version": 1, "c": 4, "b": 3, "a": 4}
    values.update(changes)
    return values


def idem(key: str | None = None) -> dict:
    return {"Idempotency-Key": key or str(uuid.uuid4())}


# --- the risk score ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((2, 3, 4), 3.0),
        ((1, 2, 2), 1.7),
        ((3, 4, 4), 3.7),
        ((1, 1, 2), 1.3),
        ((4, 4, 5), 4.3),
        ((5, 5, 5), 5.0),
        ((1, 1, 1), 1.0),
    ],
)
def test_risk_score_is_the_mean_rounded_to_one_decimal(values, expected):
    assert risk_score(values) == expected


@pytest.mark.parametrize("values", [(None, 3, 4), (2, None, 4), (2, 3, None), (None, None, None)])
def test_risk_score_is_none_until_all_three_values_are_set(values):
    assert risk_score(values) is None


def test_a_case_without_a_version_has_no_risk_score():
    assert ImpactScores(case_id=CASE_ID, version=0).risk_score is None


def test_the_score_of_a_stored_version_is_computed_from_its_values():
    assert scores(confidentiality=1, integrity=2, availability=2).risk_score == 1.7


def test_impact_changes_lists_only_the_values_that_differ():
    old = scores(confidentiality=4, integrity=3, availability=None)
    new = {"confidentiality": 4, "integrity": 5, "availability": 2}
    assert impact_changes(old, new) == {
        "integrity": {"old": 3, "new": 5},
        "availability": {"old": None, "new": 2},
    }


# --- request checks ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 6, -1, 2.5, 3.0, "3", True])
def test_a_value_outside_one_to_five_or_not_a_whole_number_is_refused(actor, client, no_domain, value):
    response = client.post(SCORES_PATH, json=body(c=value), headers=idem())
    assert response.status_code == 422


@pytest.mark.parametrize("field", ["c", "b", "a"])
def test_each_of_the_three_values_is_checked(actor, client, no_domain, field):
    response = client.post(SCORES_PATH, json=body(**{field: 6}), headers=idem())
    assert response.status_code == 422


def test_expected_version_is_required_and_not_negative(actor, client, no_domain):
    without = {"c": 4, "b": 3, "a": 4}
    assert client.post(SCORES_PATH, json=without, headers=idem()).status_code == 422
    assert client.post(SCORES_PATH, json=body(expected_version=-1), headers=idem()).status_code == 422


def test_an_unknown_field_is_refused(actor, client, no_domain):
    response = client.post(SCORES_PATH, json=body(d=2), headers=idem())
    assert response.status_code == 422


def test_a_value_left_out_or_null_is_not_set(actor, client, monkeypatch):
    seen = {}

    def fake(case_id, expected_version, values, scope, **kwargs):
        seen["values"] = values
        return ImpactResult(ImpactOutcome.OK, value=scores(version=2, integrity=None, availability=None))

    monkeypatch.setattr(impact_api, "update_impact_scores", fake)
    response = client.post(SCORES_PATH, json={"expected_version": 1, "c": 4, "b": None}, headers=idem())
    assert response.status_code == 200
    assert seen["values"] == {"confidentiality": 4, "integrity": None, "availability": None}
    assert response.json()["risk_score"] is None


# --- idempotency -------------------------------------------------------------------------------------------------------


def test_a_put_without_an_idempotency_key_is_refused_before_the_domain_runs(actor, client, no_domain):
    response = client.post(SCORES_PATH, json=body())
    assert response.status_code == 400
    assert response.json()["detail"] == {"status": "idempotency_key_required"}


@pytest.mark.parametrize("key", ["has a space", "x" * 129])
def test_an_invalid_idempotency_key_is_refused(actor, client, no_domain, key):
    response = client.post(SCORES_PATH, json=body(), headers={"Idempotency-Key": key})
    assert response.status_code == 400
    assert response.json()["detail"] == {"status": "idempotency_key_invalid"}


def test_the_key_and_the_request_reach_the_domain_as_one_scope(actor, client, monkeypatch):
    seen = {}

    def fake(case_id, expected_version, values, scope, *, idempotency=None, render=None):
        seen["idempotency"] = idempotency
        seen["render"] = render
        return ImpactResult(ImpactOutcome.OK, value=scores())

    monkeypatch.setattr(impact_api, "update_impact_scores", fake)
    response = client.post(SCORES_PATH, json=body(), headers={"Idempotency-Key": "key-1"})
    assert response.status_code == 200
    scope = seen["idempotency"]
    assert scope.principal_id == USER_ID
    assert scope.method == "POST"
    assert scope.route == impact_api.IMPACT_SCORES_ROUTE
    assert scope.key == "key-1"
    assert scope.fingerprint
    # The stored answer is the same body as a fresh answer.
    assert seen["render"](scores())["risk_score"] == 3.7


def test_a_replay_returns_the_first_answer_with_its_status_and_body(actor, client, monkeypatch):
    first = impact_api.impact_response(CASE_ID, scores()).model_dump(mode="json")
    monkeypatch.setattr(
        impact_api,
        "update_impact_scores",
        lambda *args, **kwargs: ImpactResult(ImpactOutcome.REPLAY, stored=StoredResponse(200, first)),
    )
    response = client.post(SCORES_PATH, json=body(), headers=idem("again"))
    assert response.status_code == 200
    assert response.json() == first


def test_a_reused_key_with_another_request_is_a_409(actor, client, monkeypatch):
    monkeypatch.setattr(
        impact_api,
        "update_impact_scores",
        lambda *args, **kwargs: ImpactResult(ImpactOutcome.KEY_REUSED),
    )
    response = client.post(SCORES_PATH, json=body(), headers=idem("again"))
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "idempotency_key_reused"}


# --- access ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["reviewer", "investigator", "reporter", "auditor", "reporting_owner"])
def test_only_a_triager_can_set_the_values(actor, client, no_domain, role):
    actor["role"] = role
    response = client.post(SCORES_PATH, json=body(), headers=idem())
    assert response.status_code == 403


def test_a_triager_sets_the_values_and_gets_the_stored_version(actor, client, monkeypatch):
    monkeypatch.setattr(
        impact_api,
        "update_impact_scores",
        lambda *args, **kwargs: ImpactResult(ImpactOutcome.OK, value=scores()),
    )
    response = client.post(SCORES_PATH, json=body(), headers=idem())
    assert response.status_code == 200
    assert response.json() == {
        "case_id": CASE_ID,
        "version": 2,
        "c": 4,
        "b": 3,
        "a": 4,
        "risk_score": 3.7,
        "created_by": USER_ID,
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
    }


@pytest.mark.parametrize("role", ["triager", "reviewer", "investigator", "reporter"])
def test_every_caller_who_can_read_the_case_can_read_the_values(actor, client, monkeypatch, role):
    actor["role"] = role
    monkeypatch.setattr(impact_api, "read_impact_scores", lambda case_id, scope: scores())
    response = client.get(SCORES_PATH)
    assert response.status_code == 200
    assert response.json()["risk_score"] == 3.7


def test_a_role_without_a_case_read_permission_cannot_read(actor, client, no_domain):
    actor["role"] = "auditor"
    assert client.get(SCORES_PATH).status_code == 403


def test_a_case_the_caller_cannot_see_is_not_found_on_read(actor, client, monkeypatch):
    monkeypatch.setattr(impact_api, "read_impact_scores", lambda case_id, scope: None)
    response = client.get(SCORES_PATH)
    assert response.status_code == 404
    assert response.json()["detail"] == "case not found"


def test_a_case_the_caller_cannot_see_is_not_found_on_write(actor, client, monkeypatch):
    monkeypatch.setattr(
        impact_api,
        "update_impact_scores",
        lambda *args, **kwargs: ImpactResult(ImpactOutcome.NOT_FOUND),
    )
    response = client.post(SCORES_PATH, json=body(), headers=idem())
    assert response.status_code == 404
    assert response.json()["detail"] == "case not found"


def test_a_closed_case_refuses_the_write_with_409(actor, client, monkeypatch):
    monkeypatch.setattr(impact_api, "update_impact_scores", lambda *args, **kwargs: ImpactResult(ImpactOutcome.CLOSED))
    response = client.post(SCORES_PATH, json=body(), headers=idem())
    assert response.status_code == 409
    assert response.json()["detail"] == "case is closed"


def test_a_stale_version_is_a_409_with_the_current_version(actor, client, monkeypatch):
    monkeypatch.setattr(
        impact_api,
        "update_impact_scores",
        lambda *args, **kwargs: ImpactResult(ImpactOutcome.VERSION_CONFLICT, value=scores(version=3)),
    )
    response = client.post(SCORES_PATH, json=body(expected_version=1), headers=idem())
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "impact_version_conflict", "current_version": 3}


# --- the risk fields of the case responses ------------------------------------------------------------------------------


def test_the_case_list_carries_the_risk_score_and_the_three_values(actor, client, monkeypatch):
    monkeypatch.setattr(api, "list_visible_cases", lambda scope, limit, offset, q=None: [case()])
    monkeypatch.setattr(api, "count_visible_cases", lambda scope, q=None: 1)
    monkeypatch.setattr(api, "current_scores_for_cases", lambda case_ids: {CASE_ID: scores()})
    item = client.get("/api/v1/cases").json()["items"][0]
    assert item["risk_score"] == 3.7
    assert (item["impact_c"], item["impact_b"], item["impact_a"]) == (4, 3, 4)
    # The existing fields are still there.
    assert item["reference"] == "SAFE-2026-000001"
    assert item["status"] == "open"


def test_a_case_without_values_has_null_risk_fields_in_the_list(actor, client, monkeypatch):
    monkeypatch.setattr(api, "list_visible_cases", lambda scope, limit, offset, q=None: [case()])
    monkeypatch.setattr(api, "count_visible_cases", lambda scope, q=None: 1)
    monkeypatch.setattr(api, "current_scores_for_cases", lambda case_ids: {})
    item = client.get("/api/v1/cases").json()["items"][0]
    assert item["risk_score"] is None
    assert item["impact_c"] is None and item["impact_b"] is None and item["impact_a"] is None


def test_the_case_detail_carries_the_risk_score_and_the_three_values(actor, client, monkeypatch):
    monkeypatch.setattr(api, "find_visible_case", lambda case_id, scope: case())
    monkeypatch.setattr(api, "current_scores_for_cases", lambda case_ids: {CASE_ID: scores(availability=5)})
    response = client.get(f"/api/v1/cases/{CASE_ID}").json()
    assert response["risk_score"] == 4.0
    assert (response["impact_c"], response["impact_b"], response["impact_a"]) == (4, 3, 5)
    assert response["deadlines"] == []


def test_risk_fields_of_a_case_without_a_version_are_null():
    assert impact.ImpactScores(case_id=CASE_ID, version=0).risk_score is None
    assert impact_api.risk_fields(None) == {"risk_score": None, "impact_c": None, "impact_b": None, "impact_a": None}
    assert impact_api.risk_fields(scores())["impact_b"] == 3
