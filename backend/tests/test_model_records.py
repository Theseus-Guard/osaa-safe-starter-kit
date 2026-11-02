"""Model records without a database: the request rules, the replay form, the reason gating and the HTTP mapping.
The service functions are replaced. The database tests (test_model_records_db.py) cover the versions, the insert-only
trigger, the audit rows and the whole flow against PostgreSQL.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from safe_kit import api, auth, model_records_api
from safe_kit.cases import CaseScope
from safe_kit.model_records import ModelRecord, ModelRecordOutcome
from safe_kit.model_records_api import ModelRecordRequest, RevocationRequest
from safe_kit.users import User

CASE_ID = "99999999-9999-4999-8999-999999999999"
RECORD_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def valid_body(**overrides) -> dict:
    body = {
        "model_name": "model-a",
        "model_version": "2026-10",
        "runtime": "runtime/1.4",
        "provider_alias": "provider-main",
        "reasoning": {"mode": "enabled", "effort": "high", "budget_tokens": 8000},
    }
    body.update(overrides)
    return body


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


def make_record(**overrides) -> ModelRecord:
    values = {
        "record_id": RECORD_ID,
        "case_id": CASE_ID,
        "version": 1,
        "configuration": model_records_api._configuration(ModelRecordRequest(**valid_body())),
        "evidence_item_id": None,
        "review_round_id": None,
        "recorded_at": NOW,
        "written_by": USER_ID,
        "written_at": NOW,
        "revoked": False,
        "revoked_at": None,
        "revocation_reason": None,
    }
    values.update(overrides)
    return ModelRecord(**values)


# --- request rules ---------------------------------------------------------------------------------------------------


def test_identifiers_and_reasoning_within_the_rules_are_accepted():
    body = ModelRecordRequest(**valid_body(reasoning={"mode": "unknown"}))
    assert body.reasoning.effort is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_name", "model a"),
        ("model_version", "please answer in French"),
        ("runtime", "x" * 201),
        ("provider_alias", "provider\nalias"),
        ("provider_alias", ""),
        ("runtime", "-leading-dash"),
    ],
)
def test_identifier_fields_refuse_spaces_prompt_text_and_long_values(field, value):
    with pytest.raises(ValidationError):
        ModelRecordRequest(**valid_body(**{field: value}))


@pytest.mark.parametrize(
    "reasoning",
    [
        {"mode": "disabled", "effort": "low"},
        {"mode": "unknown", "budget_tokens": 100},
        {"mode": "disabled", "budget_tokens": 100},
    ],
)
def test_effort_and_budget_are_set_only_when_reasoning_is_enabled(reasoning):
    with pytest.raises(ValidationError):
        ModelRecordRequest(**valid_body(reasoning=reasoning))


@pytest.mark.parametrize("budget", [0, 10_000_001])
def test_budget_tokens_stay_within_the_limit(budget):
    with pytest.raises(ValidationError):
        ModelRecordRequest(**valid_body(reasoning={"mode": "enabled", "budget_tokens": budget}))


def test_a_record_links_to_one_evidence_item_or_one_review_round_not_both():
    ids = {"evidence_item_id": str(uuid.uuid4()), "review_round_id": str(uuid.uuid4())}
    with pytest.raises(ValidationError):
        ModelRecordRequest(**valid_body(**ids))
    assert ModelRecordRequest(**valid_body(evidence_item_id=ids["evidence_item_id"])).review_round_id is None


def test_a_blank_revocation_reason_is_refused_and_the_reason_is_trimmed():
    with pytest.raises(ValidationError):
        RevocationRequest(reason="   ")
    assert RevocationRequest(reason="  wrong runtime  ").reason == "wrong runtime"


# --- replay form and reason gating -----------------------------------------------------------------------------------


def test_the_configuration_read_back_is_the_request_body_so_it_can_be_replayed():
    record = make_record()
    response = model_records_api._response(record, CaseScope.from_permissions(USER_ID, frozenset({"cases:read_all"})))
    configuration = response.configuration.model_dump(mode="json")

    assert configuration == valid_body()
    assert ModelRecordRequest(**configuration).model_dump(mode="json")["reasoning"] == valid_body()["reasoning"]


def test_the_revocation_reason_is_returned_only_to_callers_with_the_full_view():
    revoked = make_record(version=2, revoked=True, revoked_at=NOW, revocation_reason="wrong provider alias")

    full = CaseScope.from_permissions(USER_ID, frozenset({"cases:read_all"}))
    partial = CaseScope.from_permissions(USER_ID, frozenset({"cases:read_own"}))

    assert model_records_api._response(revoked, full).revocation_reason == "wrong provider alias"
    assert model_records_api._response(revoked, partial).revocation_reason is None
    assert model_records_api._response(revoked, partial).revoked is True


def test_a_revoked_record_is_never_usable_as_current():
    full = CaseScope.from_permissions(USER_ID, frozenset({"cases:read_all"}))
    assert model_records_api._response(make_record(), full).usable_as_current is True
    revoked = make_record(version=2, revoked=True, revoked_at=NOW, revocation_reason="superseded")
    assert model_records_api._response(revoked, full).usable_as_current is False


# --- HTTP mapping with the service replaced --------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    permissions = {
        "investigator": frozenset({"evidence:read", "evidence:write"}),
        "triager": frozenset({"evidence:read"}),
    }
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: permissions.get(role, frozenset()))
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def as_role(role: str) -> None:
    api.app.dependency_overrides[auth.current_user] = lambda: make_user(role)


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_a_write_without_an_idempotency_key_is_refused(client):
    as_role("investigator")
    response = client.post(f"/api/v1/cases/{CASE_ID}/model-records", json=valid_body())
    assert response.status_code == 400
    assert response.json()["detail"] == {"status": "idempotency_key_required"}


def test_a_reader_without_evidence_write_cannot_record_or_revoke(client, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("the service must not run without the permission")

    monkeypatch.setattr(model_records_api, "add_model_record", must_not_run)
    monkeypatch.setattr(model_records_api, "revoke_model_record", must_not_run)
    as_role("triager")

    recorded = client.post(f"/api/v1/cases/{CASE_ID}/model-records", json=valid_body(), headers=idem())
    revoked = client.post(
        f"/api/v1/cases/{CASE_ID}/model-records/{RECORD_ID}/revoke", json={"reason": "x"}, headers=idem()
    )
    assert recorded.status_code == 403
    assert revoked.status_code == 403


def test_the_outcomes_map_to_the_documented_statuses(client, monkeypatch):
    as_role("investigator")
    cases = {
        ModelRecordOutcome.NOT_FOUND: (404, {"detail": "case not found"}),
        ModelRecordOutcome.CLOSED: (409, {"detail": "case is closed"}),
        ModelRecordOutcome.REFERENCE_NOT_FOUND: (404, {"detail": "evidence item or review round not found"}),
    }
    for outcome, (status, detail) in cases.items():
        monkeypatch.setattr(model_records_api, "add_model_record", lambda *a, _o=outcome, **k: (_o, None))
        response = client.post(f"/api/v1/cases/{CASE_ID}/model-records", json=valid_body(), headers=idem())
        assert response.status_code == status
        assert response.json()["detail"] == detail["detail"]

    monkeypatch.setattr(
        model_records_api, "revoke_model_record", lambda *a, **k: (ModelRecordOutcome.ALREADY_REVOKED, None)
    )
    response = client.post(
        f"/api/v1/cases/{CASE_ID}/model-records/{RECORD_ID}/revoke", json={"reason": "x"}, headers=idem()
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "model record is already revoked"


def test_the_list_passes_the_current_only_flag_to_the_service(client, monkeypatch):
    seen = {}

    def fake_list(case_id, scope, *, current_only=False):
        seen["current_only"] = current_only
        return []

    monkeypatch.setattr(model_records_api, "list_model_records", fake_list)
    as_role("investigator")

    assert client.get(f"/api/v1/cases/{CASE_ID}/model-records").json() == []
    assert seen["current_only"] is False
    client.get(f"/api/v1/cases/{CASE_ID}/model-records?current_only=true")
    assert seen["current_only"] is True


def test_a_record_the_caller_cannot_see_is_not_found(client, monkeypatch):
    monkeypatch.setattr(model_records_api, "read_model_record", lambda *a, **k: None)
    as_role("investigator")
    response = client.get(f"/api/v1/cases/{CASE_ID}/model-records/{RECORD_ID}")
    assert response.status_code == 404
    assert response.json()["detail"] == "model record not found"
