"""Case record rules (awareness events, impacts, legal duties, classification, preliminary report), without a database.

The rules are checked on plain values. The HTTP mapping is checked with the domain functions replaced, as in
test_case_access.py. The database-backed flow is in test_case_records_db.py.
"""

import uuid
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from safe_kit import api, audit, auth, case_records_api
from safe_kit.case_records import (
    EVENT_KINDS,
    IMPACT_AREAS,
    TRIGGERS,
    AwarenessEvent,
    Classification,
    ImpactAssessment,
    LegalDuty,
    RecordOutcome,
    build_preliminary_report,
    classification_changes,
    duty_changes,
    impacts_in_order,
    missing_fields,
    normalize_triggers,
)
from safe_kit.case_records_api import (
    AwarenessEventRequest,
    ClassificationRequest,
    ImpactRequest,
    LegalDutyRequest,
    LegalDutyUpdateRequest,
)
from safe_kit.cases import Case, CaseScope
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import User

REPORTER_ID = "11111111-1111-1111-1111-111111111111"
TRIAGER_ID = "22222222-2222-2222-2222-222222222222"
INVESTIGATOR_ID = "33333333-3333-3333-3333-333333333333"
CASE_ID = "99999999-9999-9999-9999-999999999999"
DUTY_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
RAW_TEXT = "Operator account name and a customer's phone number"


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


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


def full_classification(version: int = 1) -> Classification:
    return Classification(
        version=version,
        triggers=("boundary_escape",),
        event_kind="near_miss",
        classification_rationale="rationale",
        responsible_operator="operator",
        affected_versions="model 1.2",
        approved_scope="scope",
        scope_permission_source="owner approval",
        containment="contained",
        evidence_gap_plan="collect logs",
        next_update_due_at=NOW + timedelta(days=7),
        created_by=TRIAGER_ID,
        created_at=NOW,
    )


def full_impacts(severity: str = "none") -> list[ImpactAssessment]:
    return [ImpactAssessment(area=area, assessed=True, severity=severity, scope="scope", updated_at=NOW) for area in IMPACT_AREAS]


def one_awareness_event() -> list[AwarenessEvent]:
    return [
        AwarenessEvent(
            id="e1",
            case_id=CASE_ID,
            awareness_kind="operator",
            event_kind="incident",
            occurred_at=NOW,
            basis=RAW_TEXT,
            recorded_by=TRIAGER_ID,
            created_at=NOW,
        )
    ]


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


# --- the vocabulary -------------------------------------------------------------------------------------------------


def test_the_five_compact_triggers_are_the_plan_values_in_order_and_none_applies_follows_them():
    assert TRIGGERS == (
        "unauthorized_access_or_change",
        "boundary_escape",
        "confidential_data_exposure",
        "out_of_scope_continuation",
        "harm_inaction_near_miss",
        "none_applies",
    )


def test_request_schemas_use_the_same_trigger_values_as_the_domain():
    annotation = ClassificationRequest.model_fields["triggers"].annotation
    literal = get_args(annotation)[0]
    assert set(get_args(literal)) == set(TRIGGERS)


def test_incident_near_miss_and_blocked_attempt_are_distinct_event_kinds():
    assert EVENT_KINDS == {"incident", "near_miss", "blocked_attempt"}
    assert len(EVENT_KINDS) == 3


def test_normalize_triggers_removes_duplicates_and_keeps_the_plan_order():
    chosen = normalize_triggers(["harm_inaction_near_miss", "boundary_escape", "boundary_escape"])
    assert chosen == ("boundary_escape", "harm_inaction_near_miss")


def test_normalize_triggers_refuses_a_value_outside_the_five():
    with pytest.raises(ValueError, match="unknown trigger"):
        normalize_triggers(["made_up"])


def test_none_applies_alone_means_no_trigger_applies():
    assert normalize_triggers(["none_applies", "none_applies"]) == ("none_applies",)


def test_none_applies_cannot_be_combined_with_another_trigger():
    with pytest.raises(ValueError, match="none_applies cannot be combined"):
        normalize_triggers(["none_applies", "boundary_escape"])


# --- impacts: an empty set is not "no impact" -----------------------------------------------------------------------


def test_every_impact_area_is_listed_even_when_nothing_is_assessed():
    rows = impacts_in_order({})
    assert [row.area for row in rows] == list(IMPACT_AREAS)
    assert all(not row.assessed and row.severity is None for row in rows)


def test_an_assessed_none_is_different_from_an_unassessed_area():
    rows = impacts_in_order({"scale": ImpactAssessment(area="scale", assessed=True, severity="none", scope="s")})
    by_area = {row.area: row for row in rows}
    assert by_area["scale"].assessed and by_area["scale"].severity == "none"
    assert not by_area["duration"].assessed


# --- missing fields are marked, never refused ----------------------------------------------------------------------


def test_an_empty_record_marks_every_gap_as_missing():
    missing = missing_fields(Classification(version=0), [], impacts_in_order({}), contact_recorded=False)
    fields = {(item["field"], item["state"]) for item in missing}
    assert ("contact", "missing") in fields
    assert ("classification.triggers", "missing") in fields
    assert ("classification.containment", "missing") in fields
    assert ("awareness_events", "missing") in fields
    for area in IMPACT_AREAS:
        assert (f"impacts.{area}", "missing") in fields
    assert all(item["state"] == "missing" for item in missing)


def test_a_complete_record_has_nothing_missing():
    assert missing_fields(full_classification(), one_awareness_event(), full_impacts(), contact_recorded=True) == []


def test_an_explicit_unknown_impact_is_listed_as_unknown():
    impacts = full_impacts()
    impacts[2] = ImpactAssessment(area=impacts[2].area, assessed=True, severity="unknown", scope="to research")
    missing = missing_fields(full_classification(), one_awareness_event(), impacts, contact_recorded=True)
    assert missing == [{"field": f"impacts.{IMPACT_AREAS[2]}", "state": "unknown"}]


def test_an_empty_trigger_set_is_missing_not_no_trigger():
    classification = replace(full_classification(), triggers=())
    missing = missing_fields(classification, one_awareness_event(), full_impacts(), contact_recorded=True)
    assert missing == [{"field": "classification.triggers", "state": "missing"}]


def test_preliminary_report_is_built_even_when_fields_are_missing():
    report = build_preliminary_report(
        sample_case(),
        Classification(version=0),
        [],
        impacts_in_order({}),
        [],
        contact_recorded=False,
        generated_at=NOW,
    )
    assert report["complete"] is False
    assert report["contact"] == {"status": "missing"}
    assert report["legal_duties"] == []
    assert report["safe_deadlines"] == []
    assert len(report["impacts"]) == len(IMPACT_AREAS)


def test_preliminary_report_keeps_legal_duties_apart_from_safe_deadlines():
    duty = LegalDuty(
        id=DUTY_ID,
        case_id=CASE_ID,
        kind="statutory",
        description="notify the regulator",
        source_reference="section 4",
        jurisdiction=None,
        due_at=NOW,
        status="open",
        created_by=TRIAGER_ID,
        created_at=NOW,
    )
    report = build_preliminary_report(
        sample_case(), full_classification(), one_awareness_event(), full_impacts(), [duty], True, NOW
    )
    assert [item["id"] for item in report["legal_duties"]] == [DUTY_ID]
    assert report["safe_deadlines"] == []
    assert report["complete"] is True


# --- change diffs and the audit rule --------------------------------------------------------------------------------


def test_classification_changes_lists_only_the_fields_that_differ():
    old = full_classification()
    values = old.editable_values()
    assert classification_changes(old, values) == {}
    values["event_kind"] = "incident"
    changes = classification_changes(old, values)
    assert set(changes) == {"event_kind"}
    assert changes["event_kind"] == {"old": "near_miss", "new": "incident"}


def test_classification_text_is_reduced_to_its_length_in_the_diff():
    old = full_classification()
    values = old.editable_values()
    values["containment"] = RAW_TEXT
    changes = classification_changes(old, values)
    assert changes["containment"] == {"old": {"length": len("contained")}, "new": {"length": len(RAW_TEXT)}}
    assert RAW_TEXT not in json.dumps(changes)


def test_duty_changes_lists_only_changed_fields_and_hides_text():
    old = {
        "description": "one",
        "source_reference": "s",
        "jurisdiction": None,
        "due_at": NOW,
        "status": "open",
    }
    new = {**old, "status": "done", "description": RAW_TEXT}
    changes = duty_changes(old, new)
    assert set(changes) == {"status", "description"}
    assert changes["description"]["new"] == {"length": len(RAW_TEXT)}
    assert RAW_TEXT not in json.dumps(changes)


@pytest.mark.parametrize(
    "action, details",
    [
        (audit.CASE_AWARENESS_RECORDED, {"awareness_event_id": "e", "basis": {"length": 4}, "occurred_at": NOW.isoformat()}),
        (audit.CASE_IMPACT_CHANGE, {"area": "scale", "severity": {"old": None, "new": "low"}, "scope": {"old": None, "new": {"length": 3}}}),
        (audit.CASE_LEGAL_DUTY_ADD, {"legal_duty_id": "d", "kind": "statutory", "description": {"length": 3}}),
        (audit.CASE_LEGAL_DUTY_CHANGE, {"legal_duty_id": "d", "status": {"old": "open", "new": "done"}}),
        (audit.CASE_CLASSIFICATION_CHANGE, {"version": {"old": 0, "new": 1}, "triggers": {"old": [], "new": ["boundary_escape"]}}),
    ],
)
def test_case_record_audit_details_pass_the_audit_rules(action, details):
    audit.check_details(details, action)


def test_audit_refuses_a_raw_ip_address_in_case_record_details():
    with pytest.raises(ValueError):
        audit.check_details({"basis": "10.0.0.1"}, audit.CASE_AWARENESS_RECORDED)


# --- request validation ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_the_request_refuses_the_case_wide_internal_option(value):
    # The option is per task now. A request that sends it is refused, with the way to mark the tasks.
    with pytest.raises(ValidationError, match="PATCH /api/v1/cases/"):
        ClassificationRequest(expected_version=0, internal_incident_option=value, classification_rationale="why")


def test_classification_request_removes_duplicate_triggers():
    request = ClassificationRequest(
        expected_version=0, triggers=["boundary_escape", "boundary_escape", "unauthorized_access_or_change"]
    )
    assert request.triggers == ["unauthorized_access_or_change", "boundary_escape"]


def test_classification_request_refuses_an_unknown_trigger():
    with pytest.raises(ValidationError):
        ClassificationRequest(expected_version=0, triggers=["invented"])


def test_classification_text_is_stripped_and_blank_becomes_none():
    request = ClassificationRequest(expected_version=0, containment="   ", responsible_operator="  team  ")
    assert request.containment is None
    assert request.responsible_operator == "team"


def test_classification_request_refuses_a_negative_expected_version():
    with pytest.raises(ValidationError):
        ClassificationRequest(expected_version=-1)


def test_awareness_event_refuses_a_future_time_a_blank_basis_and_a_long_basis():
    future = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(ValidationError):
        AwarenessEventRequest(awareness_kind="operator", event_kind="incident", occurred_at=future, basis="x")
    with pytest.raises(ValidationError):
        AwarenessEventRequest(awareness_kind="operator", event_kind="incident", occurred_at=NOW, basis="   ")
    with pytest.raises(ValidationError):
        AwarenessEventRequest(awareness_kind="operator", event_kind="incident", occurred_at=NOW, basis="x" * 1001)


def test_awareness_event_accepts_an_earlier_affected_party_time():
    event = AwarenessEventRequest(
        awareness_kind="affected_party", event_kind="blocked_attempt", occurred_at=NOW - timedelta(days=2), basis="log"
    )
    assert event.event_kind == "blocked_attempt"


def test_impact_refuses_a_blank_scope_and_an_unknown_severity():
    with pytest.raises(ValidationError):
        ImpactRequest(severity="low", scope="  ")
    with pytest.raises(ValidationError):
        ImpactRequest(severity="maybe", scope="scope")


def test_impact_accepts_the_explicit_unknown_value():
    assert ImpactRequest(severity="unknown", scope="to research").severity == "unknown"


def test_legal_duty_request_needs_a_source_and_a_due_time():
    with pytest.raises(ValidationError):
        LegalDutyRequest(kind="statutory", description="d", source_reference="", due_at=NOW)
    with pytest.raises(ValidationError):
        LegalDutyRequest(kind="statutory", description="d", source_reference="s", due_at=datetime(2026, 11, 2))


def test_legal_duty_update_refuses_an_empty_body_and_null_required_fields():
    with pytest.raises(ValidationError):
        LegalDutyUpdateRequest()
    with pytest.raises(ValidationError):
        LegalDutyUpdateRequest(description=None)
    with pytest.raises(ValidationError):
        LegalDutyUpdateRequest(due_at=None)


def test_legal_duty_update_may_clear_the_jurisdiction():
    body = LegalDutyUpdateRequest(jurisdiction=None)
    assert body.model_fields_set == {"jurisdiction"}


def test_legal_duty_update_reports_only_the_fields_that_were_sent():
    assert LegalDutyUpdateRequest(status="done").model_fields_set == {"status"}


# --- the HTTP mapping -------------------------------------------------------------------------------------------------


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


def test_the_case_record_routes_are_mounted_under_the_case():
    paths = set(api.app.openapi()["paths"])
    for suffix in (
        "awareness-events",
        "impacts",
        "impacts/{area}",
        "legal-duties",
        "legal-duties/{duty_id}",
        "classification",
        "preliminary-report",
    ):
        assert f"/api/v1/cases/{{case_id}}/{suffix}" in paths


def test_awareness_list_answers_404_for_a_case_the_caller_may_not_see(client, caller, monkeypatch):
    monkeypatch.setattr(case_records_api, "list_awareness_events", lambda case_id, scope: None)
    assert client.get(f"/api/v1/cases/{CASE_ID}/awareness-events").status_code == 404


def test_reporter_cannot_record_an_awareness_event(client, caller):
    body = {"awareness_kind": "operator", "event_kind": "incident", "occurred_at": NOW.isoformat(), "basis": "b"}
    assert client.post(f"/api/v1/cases/{CASE_ID}/awareness-events", json=body, headers=idem()).status_code == 403


def test_triager_records_an_event_and_only_read_all_callers_see_the_basis(client, caller, monkeypatch):
    event = one_awareness_event()[0]
    monkeypatch.setattr(case_records_api, "record_awareness_event", lambda *args, **kwargs: (RecordOutcome.OK, event))
    monkeypatch.setattr(case_records_api, "list_awareness_events", lambda case_id, scope: [event])

    as_role(caller, TRIAGER_ID, "triager")
    body = {"awareness_kind": "operator", "event_kind": "incident", "occurred_at": NOW.isoformat(), "basis": RAW_TEXT}
    created = client.post(f"/api/v1/cases/{CASE_ID}/awareness-events", json=body, headers=idem())
    assert created.status_code == 201
    assert created.json()["basis"] == RAW_TEXT

    as_role(caller, REPORTER_ID, "reporter")
    listed = client.get(f"/api/v1/cases/{CASE_ID}/awareness-events")
    assert listed.status_code == 200
    assert listed.json()[0]["basis"] is None
    assert listed.json()[0]["event_kind"] == "incident"


def test_awareness_write_on_a_closed_case_answers_409(client, caller, monkeypatch):
    monkeypatch.setattr(case_records_api, "record_awareness_event", lambda *args, **kwargs: (RecordOutcome.CLOSED, None))
    as_role(caller, TRIAGER_ID, "triager")
    body = {"awareness_kind": "operator", "event_kind": "incident", "occurred_at": NOW.isoformat(), "basis": "b"}
    assert client.post(f"/api/v1/cases/{CASE_ID}/awareness-events", json=body, headers=idem()).status_code == 409


def test_impacts_are_listed_in_full_even_when_nothing_is_assessed(client, caller, monkeypatch):
    monkeypatch.setattr(case_records_api, "list_impacts", lambda case_id, scope: impacts_in_order({}))
    as_role(caller, INVESTIGATOR_ID, "investigator")
    response = client.get(f"/api/v1/cases/{CASE_ID}/impacts")
    assert response.status_code == 200
    assert [row["area"] for row in response.json()] == list(IMPACT_AREAS)
    assert all(row["assessed"] is False and row["severity"] is None for row in response.json())


def test_impact_put_refuses_an_unknown_area(client, caller):
    as_role(caller, TRIAGER_ID, "triager")
    body = {"severity": "low", "scope": "s"}
    assert client.put(f"/api/v1/cases/{CASE_ID}/impacts/made_up", json=body, headers=idem()).status_code == 400


def test_impact_put_answers_404_for_an_invisible_case(client, caller, monkeypatch):
    monkeypatch.setattr(case_records_api, "set_impact", lambda *args, **kwargs: (RecordOutcome.NOT_FOUND, None))
    as_role(caller, TRIAGER_ID, "triager")
    body = {"severity": "low", "scope": "s"}
    assert client.put(f"/api/v1/cases/{CASE_ID}/impacts/scale", json=body, headers=idem()).status_code == 404


def test_investigator_cannot_change_a_legal_duty(client, caller):
    as_role(caller, INVESTIGATOR_ID, "investigator")
    assert client.patch(f"/api/v1/cases/{CASE_ID}/legal-duties/{DUTY_ID}", json={"status": "done"}).status_code == 403


def test_legal_duty_patch_answers_404_for_an_unknown_duty(client, caller, monkeypatch):
    monkeypatch.setattr(case_records_api, "update_legal_duty", lambda *args, **kwargs: (RecordOutcome.DUTY_NOT_FOUND, None))
    as_role(caller, TRIAGER_ID, "triager")
    assert client.patch(f"/api/v1/cases/{CASE_ID}/legal-duties/{DUTY_ID}", json={"status": "done"}).status_code == 404


def test_classification_reads_version_zero_when_nothing_is_recorded(client, caller, monkeypatch):
    monkeypatch.setattr(case_records_api, "read_classification", lambda case_id, scope: Classification(version=0))
    as_role(caller, REPORTER_ID, "reporter")
    body = client.get(f"/api/v1/cases/{CASE_ID}/classification").json()
    assert body["version"] == 0
    assert body["triggers"] == []


def test_classification_free_text_is_hidden_from_callers_without_read_all(client, caller, monkeypatch):
    monkeypatch.setattr(case_records_api, "read_classification", lambda case_id, scope: full_classification())
    as_role(caller, REPORTER_ID, "reporter")
    hidden = client.get(f"/api/v1/cases/{CASE_ID}/classification").json()
    assert hidden["classification_rationale"] is None
    assert hidden["triggers"] == ["boundary_escape"]
    as_role(caller, TRIAGER_ID, "triager")
    shown = client.get(f"/api/v1/cases/{CASE_ID}/classification").json()
    assert shown["classification_rationale"] == "rationale"


def test_classification_put_answers_409_with_the_current_version(client, caller, monkeypatch):
    monkeypatch.setattr(
        case_records_api,
        "update_classification",
        lambda *args, **kwargs: (RecordOutcome.VERSION_CONFLICT, full_classification(version=3)),
    )
    as_role(caller, TRIAGER_ID, "triager")
    response = client.put(f"/api/v1/cases/{CASE_ID}/classification", json={"expected_version": 1}, headers=idem())
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "classification_version_conflict", "current_version": 3}


def test_investigator_cannot_store_a_classification(client, caller):
    as_role(caller, INVESTIGATOR_ID, "investigator")
    assert client.put(f"/api/v1/cases/{CASE_ID}/classification", json={"expected_version": 0}, headers=idem()).status_code == 403


def test_classification_put_is_refused_without_read_all_so_the_hidden_text_is_not_cleared(client, caller, monkeypatch):
    def must_not_save(*args):
        raise AssertionError("a caller without cases:read_all must not replace the classification")

    monkeypatch.setattr(case_records_api, "update_classification", must_not_save)
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: frozenset({"cases:triage", "cases:read_own"}))
    as_role(caller, TRIAGER_ID, "triager")
    response = client.put(f"/api/v1/cases/{CASE_ID}/classification", json={"expected_version": 0}, headers=idem())
    assert response.status_code == 403
    assert response.json()["detail"] == "classification_requires_full_view"


def test_classification_put_refuses_the_case_wide_internal_option(client, caller, monkeypatch):
    monkeypatch.setattr(
        case_records_api, "update_classification", lambda *args, **kwargs: pytest.fail("the save must not run")
    )
    as_role(caller, TRIAGER_ID, "triager")
    body = {"expected_version": 0, "internal_incident_option": True}
    assert client.put(f"/api/v1/cases/{CASE_ID}/classification", json=body, headers=idem()).status_code == 400


def test_preliminary_report_is_for_read_all_callers_only(client, caller, monkeypatch):
    monkeypatch.setattr(case_records_api, "preliminary_report", lambda case_id, scope: {"case_id": case_id, "complete": False})
    as_role(caller, INVESTIGATOR_ID, "investigator")
    assert client.get(f"/api/v1/cases/{CASE_ID}/preliminary-report").status_code == 403
    as_role(caller, TRIAGER_ID, "triager")
    response = client.get(f"/api/v1/cases/{CASE_ID}/preliminary-report")
    assert response.status_code == 200
    assert response.json()["complete"] is False


def test_preliminary_report_answers_404_for_an_invisible_case(client, caller, monkeypatch):
    monkeypatch.setattr(case_records_api, "preliminary_report", lambda case_id, scope: None)
    as_role(caller, TRIAGER_ID, "triager")
    assert client.get(f"/api/v1/cases/{CASE_ID}/preliminary-report").status_code == 404


def test_the_scope_passed_to_the_domain_comes_from_the_caller(client, caller, monkeypatch):
    seen: list[CaseScope] = []

    def fake_list(case_id, scope):
        seen.append(scope)
        return []

    monkeypatch.setattr(case_records_api, "list_awareness_events", fake_list)
    as_role(caller, INVESTIGATOR_ID, "investigator")
    client.get(f"/api/v1/cases/{CASE_ID}/awareness-events")
    assert seen[0].user_id == INVESTIGATOR_ID
    assert seen[0].read_assigned and not seen[0].read_all
