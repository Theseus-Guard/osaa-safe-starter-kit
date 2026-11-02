"""F1 fixture T02 (timelines): the F1 part of the T02 acceptance scenario.

Covered without a database:
- the SAFE deadlines run from the awareness time t0 that the case was opened with, not from the moment of creation;
- an external report is an awareness kind of its own;
- a legal duty is never one of the five SAFE deadline tasks, and it does not change the report's SAFE deadlines.

Covered with a database:
- a case opened after its awareness time keeps that time; with no operator event its deadlines run from it;
- t0 is the earlier of the case awareness time and the operator awareness events. An operator event earlier than the
  awareness time moves the SAFE due dates earlier, and the audit row of that event records the move. A later operator
  event never moves t0. An affected party event or an external report never moves them;
- an external report is recorded as an awareness event and leaves the case clock and the deadlines unchanged;
- a legal duty due before a SAFE deadline does not move that deadline;
- each change of the timeline writes exactly one audit row, and a repeated request writes none.

Not testable yet, so not asserted here:
- The 72-hour, 4-business-day, DST and holiday cases, the 30-day publication limit, the 14-condition list, and the
  unresolved weekly update. They are tested in tests/test_deadlines.py, or they are F2 scope.
- Legal due dates that the organization enters from a legal source. F1 only stores them with their source reference.

The database tests use the helpers of tests/test_case_records_db.py and are skipped unless SAFE_TEST_DATABASE=1.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from safe_kit import api, audit, cases, system_timezone
from safe_kit.case_records import LegalDuty, build_preliminary_report
from safe_kit.case_records_api import AwarenessEventRequest
from safe_kit.cases import (
    WEEKLY_NOT_SIGNIFICANT_REASON,
    Case,
    calendar_for,
    deadline_tasks_for,
)
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


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


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
    t0 = datetime(
        2026, 10, 28, 6, 0, tzinfo=UTC
    )  # Wednesday 09:00 in Istanbul, a weekday with no holiday
    due = {
        task.kind: task.due_at
        for task in deadline_tasks_for(t0, calendar_for("Europe/Istanbul"))
    }

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
        awareness_kind="external_report",
        event_kind="incident",
        occurred_at=occurred,
        basis="report from a partner",
    )
    assert event.awareness_kind == "external_report"
    with pytest.raises(ValidationError):
        AwarenessEventRequest(
            awareness_kind="press",
            event_kind="incident",
            occurred_at=occurred,
            basis="x",
        )


def test_a_legal_duty_is_not_a_safe_deadline_and_does_not_change_the_report():
    t0 = NOW - timedelta(hours=1)
    case = case_opened_at(t0)
    without_duty = build_preliminary_report(
        case, full_classification(), [], full_impacts(), [], True, NOW
    )
    with_duty = build_preliminary_report(
        case,
        full_classification(),
        [],
        full_impacts(),
        [legal_duty(t0 + timedelta(hours=24))],
        True,
        NOW,
    )

    assert [task["kind"] for task in with_duty["safe_deadlines"]] == list(SAFE_KINDS)
    assert with_duty["safe_deadlines"] == without_duty["safe_deadlines"]
    assert [item["due_at"] for item in with_duty["legal_duties"]] == [
        (t0 + timedelta(hours=24)).isoformat()
    ]


def test_the_holidays_of_the_calendar_in_use_skip_business_days(monkeypatch):
    # The case calendar is calendar_for(tz), which reads CALENDAR_HOLIDAYS. A holiday there must move the 4-day deadline.
    monkeypatch.setattr(cases, "CALENDAR_HOLIDAYS", frozenset({date(2026, 11, 3)}))
    t0 = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)  # Monday 09:00 in Istanbul
    due = {
        task.kind: task.due_at
        for task in deadline_tasks_for(t0, calendar_for("Europe/Istanbul"))
    }
    # Tuesday 3 is a holiday: Wed(1), Thu(2), Fri(3), Mon 9(4) -> Monday 09:00 Istanbul, 06:00 UTC
    assert due["preliminary_safe_report"] == datetime(2026, 11, 9, 6, 0, tzinfo=UTC)


# --- with a database ------------------------------------------------------------------------------------------------


@pytest.fixture
def timeline_people():
    migrate()
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))
    people = {
        "reporter": ActingClient(new_user("reporter")),
        "triager": ActingClient(new_user("triager")),
    }
    yield people
    api.app.dependency_overrides.clear()


def open_case(reporter: ActingClient, awareness: datetime) -> str:
    response = reporter.post(
        "/api/v1/cases",
        json={"title": "Timeline case", "awareness_time": awareness.isoformat()},
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def due_times(case: dict) -> dict[str, datetime]:
    return {
        task["kind"]: datetime.fromisoformat(task["due_at"])
        for task in case["deadlines"]
    }


def audit_count(action: str, case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND target_id = %s AND NOT is_deleted",
            (action, case_id),
        ).fetchone()[0]


@requires_database
def test_a_case_opened_after_the_awareness_time_keeps_that_time_for_its_deadlines(
    timeline_people,
):
    t0 = datetime.now(UTC) - timedelta(hours=50)
    case_id = open_case(timeline_people["reporter"], t0)

    body = timeline_people["triager"].get(f"/api/v1/cases/{case_id}").json()

    assert datetime.fromisoformat(body["awareness_time"]) == t0
    assert due_times(body)["affected_customers"] == t0 + timedelta(hours=72)


@requires_database
def test_an_earlier_operator_event_moves_t0_and_the_deadlines(timeline_people):
    reporter, triager = timeline_people["reporter"], timeline_people["triager"]
    case_id = open_case(reporter, datetime.now(UTC) - timedelta(hours=2))
    before = due_times(triager.get(f"/api/v1/cases/{case_id}").json())
    earlier = datetime.now(UTC) - timedelta(hours=50)

    recorded = triager.post(
        f"/api/v1/cases/{case_id}/awareness-events",
        json={
            "awareness_kind": "operator",
            "event_kind": "incident",
            "occurred_at": earlier.isoformat(),
            "basis": "monitoring alert",
        },
        headers=idem(),
    )

    assert recorded.status_code == 201, recorded.text
    after = due_times(triager.get(f"/api/v1/cases/{case_id}").json())
    assert after["affected_customers"] == earlier + timedelta(hours=72)
    assert after["broader_advisory"] == earlier + timedelta(days=14)
    assert after["preliminary_public_report"] == earlier + timedelta(days=30)
    assert after["remediation_status"] == earlier + timedelta(days=90)
    assert after["affected_customers"] < before["affected_customers"]
    with connect() as conn:
        details = conn.execute(
            "SELECT details FROM audit_events WHERE action = %s AND target_id = %s AND NOT is_deleted "
            "ORDER BY created_at DESC LIMIT 1",
            (audit.CASE_AWARENESS_RECORDED, case_id),
        ).fetchone()[0]
    assert details["deadline_start"] == earlier.isoformat()
    assert set(details["deadlines"]) == set(SAFE_KINDS)


@requires_database
def test_a_later_operator_event_never_moves_t0_or_the_deadlines(timeline_people):
    reporter, triager = timeline_people["reporter"], timeline_people["triager"]
    awareness = datetime.now(UTC) - timedelta(hours=3)
    case_id = open_case(reporter, awareness)
    before = triager.get(f"/api/v1/cases/{case_id}").json()

    recorded = triager.post(
        f"/api/v1/cases/{case_id}/awareness-events",
        json={
            "awareness_kind": "operator",
            "event_kind": "incident",
            "occurred_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "basis": "monitoring alert",
        },
        headers=idem(),
    )

    assert recorded.status_code == 201, recorded.text
    after = triager.get(f"/api/v1/cases/{case_id}").json()
    assert after["awareness_time"] == before["awareness_time"]
    assert after["deadlines"] == before["deadlines"]
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 0
    with connect() as conn:
        details = conn.execute(
            "SELECT details FROM audit_events WHERE action = %s AND target_id = %s AND NOT is_deleted "
            "ORDER BY created_at DESC LIMIT 1",
            (audit.CASE_AWARENESS_RECORDED, case_id),
        ).fetchone()[0]
    assert "deadline_start" not in details
    assert "deadlines" not in details


@requires_database
def test_a_case_stores_the_calendar_version_its_deadlines_were_computed_with(
    timeline_people,
):
    case_id = open_case(
        timeline_people["reporter"], datetime.now(UTC) - timedelta(hours=1)
    )

    body = timeline_people["triager"].get(f"/api/v1/cases/{case_id}").json()

    assert body["calendar_version"] == cases.CALENDAR_VERSION


@requires_database
def test_an_affected_party_event_does_not_move_t0_or_the_deadlines(timeline_people):
    reporter, triager = timeline_people["reporter"], timeline_people["triager"]
    case_id = open_case(reporter, datetime.now(UTC) - timedelta(hours=2))
    before = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]

    triager.post(
        f"/api/v1/cases/{case_id}/awareness-events",
        json={
            "awareness_kind": "affected_party",
            "event_kind": "incident",
            "occurred_at": (datetime.now(UTC) - timedelta(days=5)).isoformat(),
            "basis": "customer told us",
        },
        headers=idem(),
    )

    assert triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"] == before


@requires_database
def test_an_external_report_is_recorded_and_leaves_the_clock_and_the_deadlines_alone(
    timeline_people,
):
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
        headers=idem(),
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

    assert (
        triager.post(
            f"/api/v1/cases/{case_id}/legal-duties", json=duty, headers=idem()
        ).status_code
        == 201
    )

    body = triager.get(f"/api/v1/cases/{case_id}").json()
    assert body["deadlines"] == deadlines_before
    report = triager.get(f"/api/v1/cases/{case_id}/preliminary-report").json()
    legal_due = datetime.fromisoformat(report["legal_duties"][0]["due_at"])
    safe_due = min(
        datetime.fromisoformat(task["due_at"]) for task in report["safe_deadlines"]
    )
    assert legal_due < safe_due


@requires_database
def test_each_timeline_change_writes_one_audit_row_and_a_repeat_writes_none(
    timeline_people,
):
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
        headers=idem(),
    )
    assert audit_count(audit.CASE_AWARENESS_RECORDED, case_id) == 1

    duty = {
        "kind": "contractual",
        "description": "tell the customer",
        "source_reference": "clause 9",
        "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    }
    assert (
        triager.post(
            f"/api/v1/cases/{case_id}/legal-duties", json=duty, headers=idem()
        ).status_code
        == 201
    )
    assert audit_count(audit.CASE_LEGAL_DUTY_ADD, case_id) == 1

    task_id = triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"][0]["id"]
    assert (
        triager.patch(
            f"/api/v1/cases/{case_id}/deadlines/{task_id}",
            json={"status": "done"},
            headers=idem(),
        ).status_code
        == 200
    )
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 1
    triager.patch(
        f"/api/v1/cases/{case_id}/deadlines/{task_id}",
        json={"status": "done"},
        headers=idem(),
    )
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 1


def tasks_by_kind(client: ActingClient, case_id: str) -> dict[str, dict]:
    return {
        task["kind"]: task
        for task in client.get(f"/api/v1/cases/{case_id}").json()["deadlines"]
    }


@requires_database
def test_the_broader_advisory_starts_not_applicable_and_a_triager_may_open_it(
    timeline_people,
):
    reporter, triager = timeline_people["reporter"], timeline_people["triager"]
    case_id = open_case(reporter, datetime.now(UTC) - timedelta(hours=1))
    tasks = tasks_by_kind(triager, case_id)

    assert tasks["broader_advisory"]["status"] == "not_applicable"
    assert tasks["broader_advisory"]["not_applicable_reason"]
    assert "weekly_update" not in tasks
    url = f"/api/v1/cases/{case_id}/deadlines/{tasks['broader_advisory']['id']}"
    assert (
        triager.patch(url, json={"status": "open"}, headers=idem()).status_code == 200
    )
    assert tasks_by_kind(triager, case_id)["broader_advisory"]["status"] == "open"


@requires_database
def test_an_exception_applies_to_one_task_only_needs_a_reason_and_is_audited_once(
    timeline_people,
):
    reporter, triager = timeline_people["reporter"], timeline_people["triager"]
    case_id = open_case(reporter, datetime.now(UTC) - timedelta(hours=1))
    before = tasks_by_kind(triager, case_id)
    url = f"/api/v1/cases/{case_id}/deadlines/{before['affected_customers']['id']}"

    assert (
        triager.patch(
            url, json={"status": "not_applicable"}, headers=idem()
        ).status_code
        == 400
    )
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 0

    reason = "the partner informed the customers"
    assert (
        triager.patch(
            url, json={"status": "not_applicable", "reason": reason}, headers=idem()
        ).status_code
        == 200
    )
    after = tasks_by_kind(triager, case_id)
    assert after["affected_customers"]["status"] == "not_applicable"
    assert after["affected_customers"]["not_applicable_reason"] == reason
    assert (
        after["affected_customers"]["due_at"] == before["affected_customers"]["due_at"]
    )
    for kind, task in before.items():
        if kind != "affected_customers":
            assert after[kind]["status"] == task["status"]
            assert after[kind]["not_applicable_reason"] == task["not_applicable_reason"]
            assert after[kind]["due_at"] == task["due_at"]
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 1

    # The same request changes nothing and writes no audit row.
    assert (
        triager.patch(
            url, json={"status": "not_applicable", "reason": reason}, headers=idem()
        ).status_code
        == 200
    )
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 1


@requires_database
def test_a_significant_risk_opens_the_weekly_update_and_each_change_is_audited(
    timeline_people,
):
    reporter, triager = timeline_people["reporter"], timeline_people["triager"]
    case_id = open_case(reporter, datetime.now(UTC) - timedelta(hours=1))
    url = f"/api/v1/cases/{case_id}/impacts/scale"

    assert (
        triager.put(
            url, json={"severity": "low", "scope": "three users"}, headers=idem()
        ).status_code
        == 200
    )
    assert "weekly_update" not in tasks_by_kind(triager, case_id)
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 0

    # A high impact is a significant risk: the weekly update opens, due 7 elapsed days after the change.
    recorded_at = datetime.now(UTC)
    assert (
        triager.put(
            url, json={"severity": "high", "scope": "three users"}, headers=idem()
        ).status_code
        == 200
    )
    weekly = tasks_by_kind(triager, case_id)["weekly_update"]
    assert weekly["status"] == "open"
    due = datetime.fromisoformat(weekly["due_at"])
    assert (
        recorded_at + timedelta(days=7) <= due <= datetime.now(UTC) + timedelta(days=7)
    )
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 1

    # The risk falls below significant: the task the rule opened becomes not applicable with a reason.
    assert (
        triager.put(
            url, json={"severity": "low", "scope": "three users"}, headers=idem()
        ).status_code
        == 200
    )
    closed = tasks_by_kind(triager, case_id)["weekly_update"]
    assert closed["status"] == "not_applicable"
    assert closed["not_applicable_reason"] == WEEKLY_NOT_SIGNIFICANT_REASON
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 2

    # Raised again: the same task row is reopened, not a second one is added.
    assert (
        triager.put(
            url, json={"severity": "critical", "scope": "three users"}, headers=idem()
        ).status_code
        == 200
    )
    reopened = tasks_by_kind(triager, case_id)["weekly_update"]
    assert reopened["id"] == weekly["id"]
    assert reopened["status"] == "open"
    assert reopened["not_applicable_reason"] is None
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 3

    # A repeated assessment with no change writes no deadline audit row.
    assert (
        triager.put(
            url, json={"severity": "critical", "scope": "three users"}, headers=idem()
        ).status_code
        == 200
    )
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 3
