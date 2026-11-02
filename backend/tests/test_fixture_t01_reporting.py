"""F1 fixture T01 (reporting classification): the F1 part of the T01 acceptance scenario.

Covered without a database:
- the five compact trigger values are the only accepted values, and a repeated trigger is removed in plan order;
- incident, near miss and blocked attempt are three separate event kinds, and the preliminary report keeps each one;
- none_applies is a value of its own: it stands alone, and an empty trigger list is reported as not assessed;
- the classification no longer takes a case-wide internal incident option. The external notice tasks
  (EXTERNAL_NOTICE_KINDS) are marked not applicable one by one, through the deadline update, with a reason.

Covered with a database (the scenario is run through the API, so the stored record is read back):
- the triggers are stored without duplicates in plan order;
- none_applies is stored alone and refused with 422 together with another trigger;
- each external notice task is marked not applicable on its own, with a required reason, and no other task changes;
- restoring one task puts it back to the status it had before, and its due date never moves;
- migration 0037 gives a case whose current classification had the old flag the same per-task exceptions;
- the SAFE due dates never move with an exception, and remediation_status is never changed by one.

Not testable yet, so not asserted here:
- Linked reports do not delete tasks. The model that links a report to a case or to a deadline task does not exist in
  F1. When it is added, a test should link a second report to a case, then check that every deadline task of the case
  is still present with its status.
- Intent and simulation (T01 "niyet/simülasyon"). F1 has no field for intent or for a simulated event. The only
  distinctions F1 keeps are incident, near miss and blocked attempt.

The database tests use the helpers of tests/test_case_records_db.py and are skipped unless SAFE_TEST_DATABASE=1.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from safe_kit import api, audit, system_timezone
from safe_kit.migrate import main as migrate
from safe_kit.case_records import EVENT_KINDS, TRIGGERS, AwarenessEvent, Classification, build_preliminary_report
from safe_kit.case_records_api import ClassificationRequest
from safe_kit.cases import (
    BROADER_ADVISORY_CONDITION_REASON,
    EXTERNAL_NOTICE_KINDS,
    Case,
    calendar_for,
    deadline_tasks_for,
)
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
MIGRATED_REASON = "migrated: the case-wide internal incident option was set on this case"
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations" / "0037_internal_option_per_task.sql"


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


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


@pytest.mark.parametrize("value", [True, False])
def test_the_request_refuses_the_case_wide_internal_option(value):
    # The option is per task now. A request that sends it is refused, and the message says where the choice is.
    with pytest.raises(ValidationError, match="PATCH /api/v1/cases/"):
        ClassificationRequest(expected_version=0, internal_incident_option=value, classification_rationale="why")


def test_the_external_notice_tasks_are_the_four_that_can_be_marked_per_task():
    assert EXTERNAL_NOTICE_KINDS == (
        "affected_customers",
        "preliminary_safe_report",
        "broader_advisory",
        "preliminary_public_report",
    )
    assert "remediation_status" not in EXTERNAL_NOTICE_KINDS


def test_the_report_keeps_the_due_dates_when_external_tasks_are_not_applicable():
    case = case_with_deadlines()
    plain = build_preliminary_report(case, classified(), [awareness("incident", 0)], [], [], True, NOW)
    marked = [
        replace(task, status="not_applicable", not_applicable_reason="handled inside the team")
        if task.kind in EXTERNAL_NOTICE_KINDS
        else task
        for task in case.deadlines
    ]
    marked_report = build_preliminary_report(
        replace(case, deadlines=marked),
        classified(classification_rationale="handled inside the team"),
        [awareness("incident", 0)],
        [],
        [],
        True,
        NOW,
    )
    assert "internal_incident_option" not in marked_report["classification"]
    assert [item["due_at"] for item in marked_report["safe_deadlines"]] == [
        item["due_at"] for item in plain["safe_deadlines"]
    ]
    statuses = {item["kind"]: item["status"] for item in marked_report["safe_deadlines"]}
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

    saved = triager.put(f"/api/v1/cases/{case_id}/classification", json=body, headers=idem())

    assert saved.status_code == 200, saved.text
    expected = ["unauthorized_access_or_change", "harm_inaction_near_miss"]
    assert saved.json()["triggers"] == expected
    assert triager.get(f"/api/v1/cases/{case_id}/classification").json()["triggers"] == expected


@requires_database
def test_none_applies_is_stored_alone_and_refused_with_another_trigger(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    url = f"/api/v1/cases/{case_id}/classification"

    refused = triager.put(url, json={**classification_body(0), "triggers": ["none_applies", "boundary_escape"]}, headers=idem())
    assert refused.status_code == 400, refused.text
    assert triager.get(url).json()["version"] == 0

    saved = triager.put(url, json={**classification_body(0), "triggers": ["none_applies", "none_applies"]}, headers=idem())
    assert saved.status_code == 200, saved.text
    assert saved.json()["triggers"] == ["none_applies"]
    assert triager.get(url).json()["triggers"] == ["none_applies"]


def task_states(triager: ActingClient, case_id: str) -> dict[str, tuple[str, str | None, str]]:
    deadlines = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]
    return {task["kind"]: (task["status"], task["not_applicable_reason"], task["due_at"]) for task in deadlines}


def task_id_of(triager: ActingClient, case_id: str, kind: str) -> str:
    deadlines = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]
    return next(task["id"] for task in deadlines if task["kind"] == kind)


def mark_not_applicable(triager: ActingClient, case_id: str, kind: str, reason: str):
    return triager.patch(
        f"/api/v1/cases/{case_id}/deadlines/{task_id_of(triager, case_id, kind)}",
        json={"status": "not_applicable", "reason": reason}, headers=idem(),
    )


def deadline_audit_rows(case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND target_id = %s AND NOT is_deleted",
            (audit.CASE_DEADLINE_UPDATE, case_id),
        ).fetchone()[0]


@requires_database
def test_each_external_notice_task_is_marked_not_applicable_on_its_own(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    before = task_states(triager, case_id)

    marked = mark_not_applicable(triager, case_id, "affected_customers", "no customer data was involved")

    assert marked.status_code == 200, marked.text
    after = task_states(triager, case_id)
    assert after["affected_customers"] == ("not_applicable", "no customer data was involved", before["affected_customers"][2])
    # Only the task that was marked changes: the other external notice tasks and remediation_status keep their state.
    for kind in set(before) - {"affected_customers"}:
        assert after[kind] == before[kind], kind
    assert {kind: value[2] for kind, value in after.items()} == {kind: value[2] for kind, value in before.items()}
    assert len(after) == 5
    assert deadline_audit_rows(case_id) == 1


@requires_database
def test_a_per_task_exception_needs_a_reason(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    task_id = task_id_of(triager, case_id, "preliminary_safe_report")

    refused = triager.patch(f"/api/v1/cases/{case_id}/deadlines/{task_id}", json={"status": "not_applicable"}, headers=idem())

    assert refused.status_code == 400, refused.text
    assert task_states(triager, case_id)["preliminary_safe_report"][0] == "open"
    assert deadline_audit_rows(case_id) == 0


@requires_database
def test_each_task_keeps_its_own_reason(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    assert mark_not_applicable(triager, case_id, "affected_customers", "reason one").status_code == 200
    assert mark_not_applicable(triager, case_id, "preliminary_public_report", "reason two").status_code == 200

    states = task_states(triager, case_id)
    assert states["affected_customers"][1] == "reason one"
    assert states["preliminary_public_report"][1] == "reason two"
    assert states["preliminary_safe_report"][0] == "open"


@requires_database
def test_restoring_a_task_puts_it_back_to_the_status_it_had_before(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    task_id = task_id_of(triager, case_id, "preliminary_public_report")
    before = task_states(triager, case_id)
    # The task is done before it becomes not applicable, so restoring it must give done back, not open.
    assert triager.patch(f"/api/v1/cases/{case_id}/deadlines/{task_id}", json={"status": "done"}, headers=idem()).status_code == 200
    assert mark_not_applicable(triager, case_id, "preliminary_public_report", "not for the public").status_code == 200

    restored = triager.post(f"/api/v1/cases/{case_id}/deadlines/{task_id}/restore", headers=idem())

    assert restored.status_code == 200, restored.text
    after = task_states(triager, case_id)
    assert after["preliminary_public_report"] == ("done", None, before["preliminary_public_report"][2])
    assert after == {**before, "preliminary_public_report": after["preliminary_public_report"]}


@requires_database
def test_restoring_the_default_not_applicable_advisory_returns_it_to_the_default_reason(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    assert task_states(triager, case_id)["broader_advisory"][1] == BROADER_ADVISORY_CONDITION_REASON
    assert mark_not_applicable(triager, case_id, "broader_advisory", "decided by the team").status_code == 200
    task_id = task_id_of(triager, case_id, "broader_advisory")

    restored = triager.post(f"/api/v1/cases/{case_id}/deadlines/{task_id}/restore", headers=idem())

    assert restored.status_code == 200, restored.text
    assert task_states(triager, case_id)["broader_advisory"][:2] == ("not_applicable", BROADER_ADVISORY_CONDITION_REASON)


@requires_database
def test_restore_refuses_a_task_that_is_not_marked_not_applicable(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    before = task_states(triager, case_id)
    task_id = task_id_of(triager, case_id, "affected_customers")

    refused = triager.post(f"/api/v1/cases/{case_id}/deadlines/{task_id}/restore", headers=idem())

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == {"status": "deadline_not_marked_not_applicable"}
    assert task_states(triager, case_id) == before


@requires_database
def test_only_a_triager_may_restore_a_task_that_is_not_applicable(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    case_id = new_case(reporter)
    investigator = ActingClient(new_user("investigator"))
    assigned = triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": investigator.user_id}, headers=idem())
    assert assigned.status_code == 201, assigned.text
    assert mark_not_applicable(triager, case_id, "affected_customers", "no customer data").status_code == 200
    task_id = task_id_of(triager, case_id, "affected_customers")

    refused = investigator.post(f"/api/v1/cases/{case_id}/deadlines/{task_id}/restore", headers=idem())

    assert refused.status_code == 403, refused.text
    assert task_states(triager, case_id)["affected_customers"][0] == "not_applicable"


def run_migration_0037() -> None:
    # The migration is idempotent, so running it again in a test is the same as the first run changing nothing new.
    with connect() as conn:
        conn.execute(MIGRATION_PATH.read_text(encoding="utf-8"))


def migrated_audit_rows(case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE target_id = %s AND action = %s "
            "AND details->>'source' = 'migration_0037' AND NOT is_deleted",
            (case_id, audit.CASE_DEADLINE_UPDATE),
        ).fetchone()[0]


@requires_database
def test_migration_0037_gives_a_case_with_the_old_flag_the_per_task_exceptions(triage_people):
    reporter, triager = triage_people["reporter"], triage_people["triager"]
    flagged = new_case(reporter)
    unflagged = new_case(reporter)
    flagged_before = task_states(triager, flagged)
    unflagged_before = task_states(triager, unflagged)
    # The old switch was stored on each classification version. The current version of "flagged" has it set. The
    # current version of "unflagged" has it cleared again after an earlier version set it.
    with connect() as conn:
        conn.execute(
            "INSERT INTO case_classifications (case_id, version, internal_incident_option, classification_rationale, "
            "created_by) VALUES (%s, 1, true, 'handled inside the team', %s)",
            (flagged, triager.user_id),
        )
        conn.execute(
            "INSERT INTO case_classifications (case_id, version, internal_incident_option, classification_rationale, "
            "created_by) VALUES (%s, 1, true, 'handled inside the team', %s)",
            (unflagged, triager.user_id),
        )
        conn.execute(
            "INSERT INTO case_classifications (case_id, version, internal_incident_option, created_by) "
            "VALUES (%s, 2, false, %s)",
            (unflagged, triager.user_id),
        )

    run_migration_0037()

    flagged_after = task_states(triager, flagged)
    for kind in EXTERNAL_NOTICE_KINDS:
        assert flagged_after[kind][:2] == ("not_applicable", MIGRATED_REASON), kind
    assert flagged_after["remediation_status"] == flagged_before["remediation_status"]
    assert {kind: value[2] for kind, value in flagged_after.items()} == {
        kind: value[2] for kind, value in flagged_before.items()
    }
    assert migrated_audit_rows(flagged) == 4
    assert task_states(triager, unflagged) == unflagged_before
    assert migrated_audit_rows(unflagged) == 0

    run_migration_0037()
    assert migrated_audit_rows(flagged) == 4
