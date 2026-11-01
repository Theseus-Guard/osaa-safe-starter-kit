"""F1 fixture T01 (reporting classification): the F1 part of the T01 acceptance scenario.

Covered without a database:
- the five compact trigger values are the only accepted values, and a repeated trigger is removed in plan order;
- incident, near miss and blocked attempt are three separate event kinds, and the preliminary report keeps each one;
- an internal incident option needs a written rationale and leaves the SAFE deadlines of the report unchanged.

Covered with a database (the scenario is run through the API, so the stored record is read back):
- the triggers are stored without duplicates in plan order;
- the internal incident option is stored as a version and the SAFE deadlines stay as they were.

Not testable yet, so not asserted here:
- Linked reports do not delete tasks. The model that links a report to a case or to a deadline task does not exist in
  F1. When it is added, a test should link a second report to a case, then check that every deadline task of the case
  is still present with its status.
- Intent and simulation (T01 "niyet/simülasyon"). F1 has no field for intent or for a simulated event. The only
  distinctions F1 keeps are incident, near miss and blocked attempt.
- What the internal incident option changes. That decision is still open. F1 only asserts that the option does not move
  a SAFE deadline.

The database tests use the helpers of tests/test_case_records_db.py and are skipped unless SAFE_TEST_DATABASE=1.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from safe_kit import api, system_timezone
from safe_kit.migrate import main as migrate
from safe_kit.case_records import EVENT_KINDS, TRIGGERS, AwarenessEvent, Classification, build_preliminary_report
from safe_kit.case_records_api import ClassificationRequest
from safe_kit.cases import Case, calendar_for, deadline_tasks_for
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


def test_internal_incident_option_leaves_the_safe_deadlines_in_the_report_unchanged():
    case = case_with_deadlines()
    plain = build_preliminary_report(case, classified(), [awareness("incident", 0)], [], [], True, NOW)
    internal = build_preliminary_report(
        case,
        classified(internal_incident_option=True, classification_rationale="handled inside the team"),
        [awareness("incident", 0)],
        [],
        [],
        True,
        NOW,
    )
    assert internal["classification"]["internal_incident_option"] is True
    assert len(plain["safe_deadlines"]) == 5
    assert internal["safe_deadlines"] == plain["safe_deadlines"]


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


@requires_database
def test_internal_incident_option_is_stored_and_moves_no_safe_deadline(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    deadlines_before = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]
    body = classification_body(
        0, internal_incident_option=True, classification_rationale="handled inside the team"
    )

    saved = triager.put(f"/api/v1/cases/{case_id}/classification", json=body)

    assert saved.status_code == 200, saved.text
    assert saved.json()["internal_incident_option"] is True
    assert triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"] == deadlines_before
    with connect() as conn:
        tasks = conn.execute(
            "SELECT count(*) FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted", (case_id,)
        ).fetchone()[0]
    assert tasks == 5
