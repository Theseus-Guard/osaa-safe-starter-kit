"""F1 fixture T02 (timelines): the F1 part of the T02 acceptance scenario.

Covered without a database:
- the SAFE deadlines run from the awareness time t0 that the case was opened with, not from the moment of creation;
- an external report is an awareness kind of its own;
- a legal duty is never one of the five SAFE deadline tasks, and it does not change the report's SAFE deadlines.

Covered with a database:
- a case opened after its awareness time keeps that time, and its deadlines are computed from it;
- an external report is recorded as an awareness event and leaves the case clock and the deadlines unchanged;
- a legal duty due before a SAFE deadline does not move that deadline;
- each change of the timeline writes exactly one audit row, and a repeated request writes none.

Not testable yet, so not asserted here:
- Whether an earlier awareness event should move t0. The rule is still open (see the F1 notes on t0). The test that
  records an external report pins the current rule only, and must change when that decision is made.
- The 72-hour, 4-business-day, DST and holiday cases, the 30-day publication limit, the 14-condition list, and the
  unresolved weekly update. They are tested in tests/test_deadlines.py, or they are F2 scope.
- Legal due dates that the organization enters from a legal source. F1 only stores them with their source reference.

The database tests use the helpers of tests/test_case_records_db.py and are skipped unless SAFE_TEST_DATABASE=1.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from safe_kit import api, audit, system_timezone
from safe_kit.case_records import LegalDuty, build_preliminary_report
from safe_kit.case_records_api import AwarenessEventRequest
from safe_kit.cases import Case, calendar_for, deadline_tasks_for
from safe_kit.db import connect
from safe_kit.migrate import main as migrate
from test_case_records_db import ActingClient, new_user, past
from test_evidence_lifecycle import requires_database
from test_case_records import full_classification, full_impacts

SAFE_KINDS = (
    "affected_customers",
    "preliminary_safe_report",
    "broader_advisory",
    "preliminary_public_report",
    "remediation_status",
)
CASE_ID = "99999999-9999-9999-9999-999999999999"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def case_opened_at(t0: datetime) -> Case:
    return Case(
        id=CASE_ID,
        reference="SAFE-2026-000001",
        title="Sample",
        awareness_time=t0,
        status="open",
        calendar_version="provisional-1",
        calendar_timezone="Europe/Istanbul",
        created_at=NOW,
        created_by="11111111-1111-1111-1111-111111111111",
        deadlines=deadline_tasks_for(t0, calendar_for("Europe/Istanbul")),
    )


def legal_duty(due_at: datetime) -> LegalDuty:
    return LegalDuty(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        case_id=CASE_ID,
        kind="statutory",
        description="notify the regulator",
        source_reference="section 4",
        jurisdiction=None,
        due_at=due_at,
        status="open",
        created_by="22222222-2222-2222-2222-222222222222",
        created_at=NOW,
    )


def test_deadlines_run_from_the_awareness_time_and_not_from_the_creation_time():
    t0 = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)  # Wednesday 09:00 in Istanbul, a weekday with no holiday
    due = {task.kind: task.due_at for task in deadline_tasks_for(t0, calendar_for("Europe/Istanbul"))}

    assert tuple(due) == SAFE_KINDS
    assert due["affected_customers"] == t0 + timedelta(hours=72)
    # Four business days after Wednesday are Tuesday of the following week, at the same local time.
    assert due["preliminary_safe_report"] == datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
    assert due["broader_advisory"] == t0 + timedelta(days=14)
    assert due["preliminary_public_report"] == t0 + timedelta(days=30)
    assert due["remediation_status"] == t0 + timedelta(days=90)


def test_an_external_report_is_an_awareness_kind_of_its_own():
    occurred = datetime.now(UTC) - timedelta(days=3)
    event = AwarenessEventRequest(
        awareness_kind="external_report", event_kind="incident", occurred_at=occurred, basis="report from a partner"
    )
    assert event.awareness_kind == "external_report"
    with pytest.raises(ValidationError):
        AwarenessEventRequest(awareness_kind="press", event_kind="incident", occurred_at=occurred, basis="x")


def test_a_legal_duty_is_not_a_safe_deadline_and_does_not_change_the_report():
    t0 = NOW - timedelta(hours=1)
    case = case_opened_at(t0)
    without_duty = build_preliminary_report(case, full_classification(), [], full_impacts(), [], True, NOW)
    with_duty = build_preliminary_report(
        case, full_classification(), [], full_impacts(), [legal_duty(t0 + timedelta(hours=24))], True, NOW
    )

    assert [task["kind"] for task in with_duty["safe_deadlines"]] == list(SAFE_KINDS)
    assert with_duty["safe_deadlines"] == without_duty["safe_deadlines"]
    assert [item["due_at"] for item in with_duty["legal_duties"]] == [(t0 + timedelta(hours=24)).isoformat()]


# --- with a database ------------------------------------------------------------------------------------------------


@pytest.fixture
def timeline_people():
    migrate()
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))
    people = {"reporter": ActingClient(new_user("reporter")), "triager": ActingClient(new_user("triager"))}
    yield people
    api.app.dependency_overrides.clear()


def open_case(reporter: ActingClient, awareness: datetime) -> str:
    response = reporter.post("/api/v1/cases", json={"title": "Timeline case", "awareness_time": awareness.isoformat()})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def due_times(case: dict) -> dict[str, datetime]:
    return {task["kind"]: datetime.fromisoformat(task["due_at"]) for task in case["deadlines"]}


def audit_count(action: str, case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND target_id = %s AND NOT is_deleted",
            (action, case_id),
        ).fetchone()[0]


@requires_database
def test_a_case_opened_after_the_awareness_time_keeps_that_time_for_its_deadlines(timeline_people):
    t0 = datetime.now(UTC) - timedelta(hours=50)
    case_id = open_case(timeline_people["reporter"], t0)

    body = timeline_people["triager"].get(f"/api/v1/cases/{case_id}").json()

    assert datetime.fromisoformat(body["awareness_time"]) == t0
    assert due_times(body)["affected_customers"] == t0 + timedelta(hours=72)


@requires_database
def test_an_external_report_is_recorded_and_leaves_the_clock_and_the_deadlines_alone(timeline_people):
    reporter, triager = timeline_people["reporter"], timeline_people["triager"]
    case_id = open_case(reporter, datetime.now(UTC) - timedelta(hours=2))
    before = triager.get(f"/api/v1/cases/{case_id}").json()

    recorded = triager.post(
        f"/api/v1/cases/{case_id}/awareness-events",
        json={
            "awareness_kind": "external_report",
            "event_kind": "incident",
            "occurred_at": (datetime.now(UTC) - timedelta(days=5)).isoformat(),
            "basis": "report from a partner",
        },
    )

    assert recorded.status_code == 201, recorded.text
    events = triager.get(f"/api/v1/cases/{case_id}/awareness-events").json()
    assert [event["awareness_kind"] for event in events] == ["external_report"]
    after = triager.get(f"/api/v1/cases/{case_id}").json()
    assert after["awareness_time"] == before["awareness_time"]
    assert after["deadlines"] == before["deadlines"]


@requires_database
def test_a_legal_duty_due_before_a_safe_deadline_does_not_move_it(timeline_people):
    reporter, triager = timeline_people["reporter"], timeline_people["triager"]
    case_id = open_case(reporter, datetime.now(UTC) - timedelta(hours=1))
    deadlines_before = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]
    duty = {
        "kind": "statutory",
        "description": "notify the regulator",
        "source_reference": "section 4",
        "due_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
    }

    assert triager.post(f"/api/v1/cases/{case_id}/legal-duties", json=duty).status_code == 201

    body = triager.get(f"/api/v1/cases/{case_id}").json()
    assert body["deadlines"] == deadlines_before
    report = triager.get(f"/api/v1/cases/{case_id}/preliminary-report").json()
    legal_due = datetime.fromisoformat(report["legal_duties"][0]["due_at"])
    safe_due = min(datetime.fromisoformat(task["due_at"]) for task in report["safe_deadlines"])
    assert legal_due < safe_due


@requires_database
def test_each_timeline_change_writes_one_audit_row_and_a_repeat_writes_none(timeline_people):
    reporter, triager = timeline_people["reporter"], timeline_people["triager"]
    case_id = open_case(reporter, datetime.now(UTC) - timedelta(hours=1))

    triager.post(
        f"/api/v1/cases/{case_id}/awareness-events",
        json={
            "awareness_kind": "operator",
            "event_kind": "near_miss",
            "occurred_at": past(hours=1),
            "basis": "monitoring alert",
        },
    )
    assert audit_count(audit.CASE_AWARENESS_RECORDED, case_id) == 1

    duty = {
        "kind": "contractual",
        "description": "tell the customer",
        "source_reference": "clause 9",
        "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    }
    assert triager.post(f"/api/v1/cases/{case_id}/legal-duties", json=duty).status_code == 201
    assert audit_count(audit.CASE_LEGAL_DUTY_ADD, case_id) == 1

    task_id = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"][0]["id"]
    assert triager.patch(f"/api/v1/cases/{case_id}/deadlines/{task_id}", json={"status": "done"}).status_code == 200
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 1
    triager.patch(f"/api/v1/cases/{case_id}/deadlines/{task_id}", json={"status": "done"})
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 1
