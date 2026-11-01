"""F1 fixture T01 (reporting classification): the F1 part of the T01 acceptance scenario.

Covered without a database:
- the five compact trigger values are the only accepted values, and a repeated trigger is removed in plan order;
- incident, near miss and blocked attempt are three separate event kinds, and the preliminary report keeps each one;
- none_applies is a value of its own: it stands alone, and an empty trigger list is reported as not assessed;
- an internal incident option needs a written rationale, and it keeps the SAFE due dates. The external notice tasks
  are the ones it marks not applicable (EXTERNAL_NOTICE_KINDS).

Covered with a database (the scenario is run through the API, so the stored record is read back):
- the triggers are stored without duplicates in plan order;
- none_applies is stored alone and refused with 422 together with another trigger;
- the internal incident option marks the four external notice tasks not applicable with the rationale as reason, and
  turning it off restores their previous status; a task changed by hand while the option is on is not restored;
- the SAFE due dates never move with the option, and remediation_status is never changed by it.

Not testable yet, so not asserted here:
- Linked reports do not delete tasks. The model that links a report to a case or to a deadline task does not exist in
  F1. When it is added, a test should link a second report to a case, then check that every deadline task of the case
  is still present with its status.
- Intent and simulation (T01 "niyet/simülasyon"). F1 has no field for intent or for a simulated event. The only
  distinctions F1 keeps are incident, near miss and blocked attempt.

The database tests use the helpers of tests/test_case_records_db.py and are skipped unless SAFE_TEST_DATABASE=1.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from safe_kit import api, audit, system_timezone
from safe_kit.migrate import main as migrate
from safe_kit.case_records import EVENT_KINDS, TRIGGERS, AwarenessEvent, Classification, build_preliminary_report
from safe_kit.case_records_api import ClassificationRequest
from safe_kit.cases import EXTERNAL_NOTICE_KINDS, Case, calendar_for, deadline_tasks_for
from safe_kit.db import connect
from test_case_records_db import ActingClient, classification_body, new_case, new_user
from test_evidence_lifecycle import requires_database

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
FIVE_TRIGGERS = (
    "unauthorized_access_or_change",
    "boundary_escape",
    "confidential_data_exposure",
    "out_of_scope_continuation",
    "harm_inaction_near_miss",
)
# none_applies is an explicit value of its own: it records that no trigger applies, and it stands alone.
TRIGGER_VALUES = (*FIVE_TRIGGERS, "none_applies")


def classified(**changes) -> Classification:
    values = {
        "version": 1,
        "triggers": ("boundary_escape",),
        "event_kind": "near_miss",
        "classification_rationale": "rationale",
        "responsible_operator": "operator",
        "affected_versions": "model 1.2",
        "approved_scope": "scope",
        "scope_permission_source": "owner approval",
        "containment": "contained",
        "evidence_gap_plan": "collect logs",
        "next_update_due_at": NOW + timedelta(days=7),
        "created_by": "22222222-2222-2222-2222-222222222222",
        "created_at": NOW,
    }
    values.update(changes)
    return Classification(**values)


def awareness(event_kind: str, index: int) -> AwarenessEvent:
    return AwarenessEvent(
        id=f"event-{index}",
        case_id="99999999-9999-9999-9999-999999999999",
        awareness_kind="operator",
        event_kind=event_kind,
        occurred_at=NOW,
        basis="monitoring alert",
        recorded_by="22222222-2222-2222-2222-222222222222",
        created_at=NOW,
    )


def test_the_five_compact_triggers_and_none_applies_are_the_only_accepted_values():
    assert TRIGGERS == TRIGGER_VALUES
    assert ClassificationRequest(expected_version=0, triggers=list(FIVE_TRIGGERS)).triggers == list(FIVE_TRIGGERS)
    for value in ("other_trigger", "", "BOUNDARY_ESCAPE"):
        with pytest.raises(ValidationError):
            ClassificationRequest(expected_version=0, triggers=[value])


def test_none_applies_is_accepted_alone_and_refused_with_another_trigger():
    alone = ClassificationRequest(expected_version=0, triggers=["none_applies", "none_applies"])
    assert alone.triggers == ["none_applies"]
    with pytest.raises(ValidationError):
        ClassificationRequest(expected_version=0, triggers=["none_applies", "boundary_escape"])
    with pytest.raises(ValidationError):
        ClassificationRequest(expected_version=0, triggers=["harm_inaction_near_miss", "none_applies"])


def test_an_empty_trigger_list_is_not_assessed_and_is_reported_as_missing():
    report = build_preliminary_report(case_with_deadlines(), classified(triggers=()), [], [], [], True, NOW)
    assert {"field": "classification.triggers", "state": "missing"} in report["missing_fields"]
    assert report["complete"] is False


def test_none_applies_is_a_recorded_answer_and_not_a_missing_field():
    report = build_preliminary_report(
        case_with_deadlines(), classified(triggers=("none_applies",)), [awareness("incident", 0)], [], [], True, NOW
    )
    assert report["classification"]["triggers"] == ["none_applies"]
    assert {"field": "classification.triggers", "state": "missing"} not in report["missing_fields"]


def test_a_repeated_trigger_is_removed_and_the_plan_order_is_kept():
    request = ClassificationRequest(
        expected_version=0,
        triggers=["harm_inaction_near_miss", "boundary_escape", "harm_inaction_near_miss"],
    )
    assert request.triggers == ["boundary_escape", "harm_inaction_near_miss"]


def test_incident_near_miss_and_blocked_attempt_are_three_separate_event_kinds():
    assert EVENT_KINDS == frozenset({"incident", "near_miss", "blocked_attempt"})
    assert ClassificationRequest(expected_version=0, event_kind="blocked_attempt").event_kind == "blocked_attempt"
    with pytest.raises(ValidationError):
        ClassificationRequest(expected_version=0, event_kind="attempted")


def test_preliminary_report_keeps_each_awareness_kind_apart():
    events = [awareness(kind, index) for index, kind in enumerate(sorted(EVENT_KINDS))]
    case = case_with_deadlines()
    report = build_preliminary_report(case, classified(), events, [], [], True, NOW)
    assert sorted(item["event_kind"] for item in report["awareness_events"]) == [
        "blocked_attempt",
        "incident",
        "near_miss",
    ]


def test_internal_incident_option_needs_a_written_rationale():
    with pytest.raises(ValidationError):
        ClassificationRequest(expected_version=0, internal_incident_option=True, classification_rationale=None)
    # A blank rationale is cleaned to None before the rule runs, so it is refused as well.
    with pytest.raises(ValidationError):
        ClassificationRequest(expected_version=0, internal_incident_option=True, classification_rationale="   ")


def test_the_external_notice_tasks_are_the_four_the_internal_option_changes():
    assert EXTERNAL_NOTICE_KINDS == (
        "affected_customers",
        "preliminary_safe_report",
        "broader_advisory",
        "preliminary_public_report",
    )
    assert "remediation_status" not in EXTERNAL_NOTICE_KINDS


def test_the_report_keeps_the_due_dates_and_shows_the_not_applicable_external_tasks():
    case = case_with_deadlines()
    plain = build_preliminary_report(case, classified(), [awareness("incident", 0)], [], [], True, NOW)
    marked = [
        replace(task, status="not_applicable", not_applicable_reason="handled inside the team")
        if task.kind in EXTERNAL_NOTICE_KINDS
        else task
        for task in case.deadlines
    ]
    internal = build_preliminary_report(
        replace(case, deadlines=marked),
        classified(internal_incident_option=True, classification_rationale="handled inside the team"),
        [awareness("incident", 0)],
        [],
        [],
        True,
        NOW,
    )
    assert internal["classification"]["internal_incident_option"] is True
    assert [item["due_at"] for item in internal["safe_deadlines"]] == [
        item["due_at"] for item in plain["safe_deadlines"]
    ]
    statuses = {item["kind"]: item["status"] for item in internal["safe_deadlines"]}
    assert statuses == {
        "affected_customers": "not_applicable",
        "preliminary_safe_report": "not_applicable",
        "broader_advisory": "not_applicable",
        "preliminary_public_report": "not_applicable",
        "remediation_status": "open",
    }


def case_with_deadlines() -> Case:
    return Case(
        id="99999999-9999-9999-9999-999999999999",
        reference="SAFE-2026-000001",
        title="Sample",
        awareness_time=NOW,
        status="open",
        calendar_version="provisional-1",
        calendar_timezone="Europe/Istanbul",
        created_at=NOW,
        created_by="11111111-1111-1111-1111-111111111111",
        deadlines=deadline_tasks_for(NOW, calendar_for("Europe/Istanbul")),
    )


# --- with a database ------------------------------------------------------------------------------------------------


@pytest.fixture
def triage_people():
    migrate()
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))
    people = {"reporter": ActingClient(new_user("reporter")), "triager": ActingClient(new_user("triager"))}
    yield people
    api.app.dependency_overrides.clear()


@requires_database
def test_triggers_are_stored_without_duplicates_in_plan_order(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    body = classification_body(0)
    body["triggers"] = ["harm_inaction_near_miss", "unauthorized_access_or_change", "harm_inaction_near_miss"]

    saved = triager.put(f"/api/v1/cases/{case_id}/classification", json=body)

    assert saved.status_code == 200, saved.text
    expected = ["unauthorized_access_or_change", "harm_inaction_near_miss"]
    assert saved.json()["triggers"] == expected
    assert triager.get(f"/api/v1/cases/{case_id}/classification").json()["triggers"] == expected


@requires_database
def test_none_applies_is_stored_alone_and_refused_with_another_trigger(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    url = f"/api/v1/cases/{case_id}/classification"

    refused = triager.put(url, json={**classification_body(0), "triggers": ["none_applies", "boundary_escape"]})
    assert refused.status_code == 422, refused.text
    assert triager.get(url).json()["version"] == 0

    saved = triager.put(url, json={**classification_body(0), "triggers": ["none_applies", "none_applies"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["triggers"] == ["none_applies"]
    assert triager.get(url).json()["triggers"] == ["none_applies"]


def internal_option_body(expected_version: int, rationale: str | None = "handled inside the team") -> dict:
    return classification_body(
        expected_version, internal_incident_option=rationale is not None, classification_rationale=rationale
    )


def task_states(triager: ActingClient, case_id: str) -> dict[str, tuple[str, str | None, str]]:
    deadlines = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]
    return {task["kind"]: (task["status"], task["not_applicable_reason"], task["due_at"]) for task in deadlines}


def deadline_audit_rows(case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND target_id = %s AND NOT is_deleted",
            (audit.CASE_DEADLINE_UPDATE, case_id),
        ).fetchone()[0]


@requires_database
def test_internal_incident_option_marks_the_external_notice_tasks_not_applicable(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    url = f"/api/v1/cases/{case_id}/classification"
    before = task_states(triager, case_id)

    saved = triager.put(url, json=internal_option_body(0))

    assert saved.status_code == 200, saved.text
    assert saved.json()["internal_incident_option"] is True
    after = task_states(triager, case_id)
    for kind in EXTERNAL_NOTICE_KINDS:
        assert after[kind][0] == "not_applicable"
        assert after[kind][1] == "handled inside the team"
    assert after["remediation_status"] == before["remediation_status"]
    assert {kind: value[2] for kind, value in after.items()} == {kind: value[2] for kind, value in before.items()}
    assert len(after) == 5
    assert deadline_audit_rows(case_id) == 4


@requires_database
def test_turning_the_internal_option_off_restores_the_previous_states(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    url = f"/api/v1/cases/{case_id}/classification"
    before = task_states(triager, case_id)
    assert triager.put(url, json=internal_option_body(0)).status_code == 200

    restored = triager.put(url, json=internal_option_body(1, rationale=None))

    assert restored.status_code == 200, restored.text
    after = task_states(triager, case_id)
    assert after == before
    assert deadline_audit_rows(case_id) == 8


@requires_database
def test_a_task_changed_by_hand_while_the_option_is_on_is_not_restored(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    url = f"/api/v1/cases/{case_id}/classification"
    assert triager.put(url, json=internal_option_body(0)).status_code == 200
    deadlines = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]
    task_id = next(task["id"] for task in deadlines if task["kind"] == "broader_advisory")
    assert triager.patch(f"/api/v1/cases/{case_id}/deadlines/{task_id}", json={"status": "done"}).status_code == 200

    assert triager.put(url, json=internal_option_body(1, rationale=None)).status_code == 200

    states = task_states(triager, case_id)
    assert states["broader_advisory"][0] == "done"
    assert states["affected_customers"][0] == "open"


@requires_database
def test_a_changed_rationale_is_copied_to_the_tasks_the_option_marked(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    url = f"/api/v1/cases/{case_id}/classification"
    assert triager.put(url, json=internal_option_body(0)).status_code == 200

    assert triager.put(url, json=internal_option_body(1, rationale="team agreed a new rationale")).status_code == 200

    states = task_states(triager, case_id)
    assert states["affected_customers"][1] == "team agreed a new rationale"
    assert states["remediation_status"][0] == "open"


@requires_database
def test_none_of_the_internal_option_changes_moves_a_due_date(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    url = f"/api/v1/cases/{case_id}/classification"
    due_before = {kind: value[2] for kind, value in task_states(triager, case_id).items()}

    assert triager.put(url, json=internal_option_body(0)).status_code == 200
    assert triager.put(url, json=internal_option_body(1, rationale=None)).status_code == 200

    assert {kind: value[2] for kind, value in task_states(triager, case_id).items()} == due_before
