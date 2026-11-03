"""F2 fixture T02-A: the legal notice and the SAFE customer notice run on their own clocks.

- The legal notice is a legal duty with its own due time, 24 hours after t0. It is open from the start and is not one
  of the five SAFE deadline tasks.
- The SAFE customer notice (affected_customers) is due 72 hours after t0.
- A restriction on the public package (the preliminary_public_report task, marked not applicable with a reason)
  applies to that task only. The other hours keep running: their due dates and statuses do not change.
- A SAFE exception, and its restore, never changes a legal duty.
- The 14-day broader advisory keeps its due date when it is opened.

The change proposal PR #8 cannot be tested here. The hours are fixed in deadlines.py and asserted by the unit tests in
tests/test_deadlines.py.

Database tests: skipped unless SAFE_TEST_DATABASE=1, like the other fixtures.
"""

from datetime import UTC, datetime, timedelta

import pytest

from safe_kit import api, audit, system_timezone
from safe_kit.migrate import main as migrate
from if_match import task_if_match
from test_case_records_db import ActingClient, new_user
from test_evidence_lifecycle import requires_database
from test_fixture_t02_timelines import (
    SAFE_KINDS,
    audit_count,
    due_times,
    idem,
    open_case,
)


@pytest.fixture
def notice_people():
    migrate()
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))
    people = {
        "reporter": ActingClient(new_user("reporter")),
        "triager": ActingClient(new_user("triager")),
    }
    yield people
    api.app.dependency_overrides.clear()


def add_legal_notice(triager: ActingClient, case_id: str, t0: datetime) -> None:
    response = triager.post(
        f"/api/v1/cases/{case_id}/legal-duties",
        json={
            "kind": "statutory",
            "description": "notify the regulator",
            "source_reference": "section 4",
            "due_at": (t0 + timedelta(hours=24)).isoformat(),
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text


def legal_duties(triager: ActingClient, case_id: str) -> list[dict]:
    return triager.get(f"/api/v1/cases/{case_id}/preliminary-report").json()[
        "legal_duties"
    ]


def tasks_by_kind(triager: ActingClient, case_id: str) -> dict[str, dict]:
    return {
        task["kind"]: task
        for task in triager.get(f"/api/v1/cases/{case_id}").json()["deadlines"]
    }


@requires_database
def test_the_legal_notice_is_open_within_24_hours_and_the_customer_notice_runs_to_72_hours(
    notice_people,
):
    reporter, triager = notice_people["reporter"], notice_people["triager"]
    t0 = datetime.now(UTC) - timedelta(hours=1)
    case_id = open_case(reporter, t0)
    add_legal_notice(triager, case_id, t0)

    [duty] = legal_duties(triager, case_id)
    assert duty["status"] == "open"
    assert datetime.fromisoformat(duty["due_at"]) == t0 + timedelta(hours=24)

    tasks = tasks_by_kind(triager, case_id)
    assert tasks["affected_customers"]["status"] == "open"
    assert due_times(triager.get(f"/api/v1/cases/{case_id}").json())[
        "affected_customers"
    ] == t0 + timedelta(hours=72)
    assert datetime.fromisoformat(duty["due_at"]) < t0 + timedelta(hours=72)


@requires_database
def test_a_restriction_on_the_public_package_applies_to_that_task_and_the_other_hours_keep_running(
    notice_people,
):
    reporter, triager = notice_people["reporter"], notice_people["triager"]
    t0 = datetime.now(UTC) - timedelta(hours=1)
    case_id = open_case(reporter, t0)
    add_legal_notice(triager, case_id, t0)
    before = tasks_by_kind(triager, case_id)
    legal_before = legal_duties(triager, case_id)
    url = (
        f"/api/v1/cases/{case_id}/deadlines/{before['preliminary_public_report']['id']}"
    )
    reason = "investigation hold on public release"

    response = triager.patch(
        url,
        json={"status": "not_applicable", "reason": reason},
        headers={**idem(), **task_if_match(triager, url)},
    )

    assert response.status_code == 200, response.text
    after = tasks_by_kind(triager, case_id)
    assert after["preliminary_public_report"]["status"] == "not_applicable"
    assert after["preliminary_public_report"]["not_applicable_reason"] == reason
    assert (
        after["preliminary_public_report"]["due_at"]
        == before["preliminary_public_report"]["due_at"]
    )
    for kind in SAFE_KINDS:
        if kind == "preliminary_public_report":
            continue
        assert after[kind]["status"] == before[kind]["status"], kind
        assert after[kind]["due_at"] == before[kind]["due_at"], kind
    assert after["affected_customers"]["status"] == "open"
    assert datetime.fromisoformat(
        after["affected_customers"]["due_at"]
    ) == t0 + timedelta(hours=72)
    assert legal_duties(triager, case_id) == legal_before
    assert audit_count(audit.CASE_DEADLINE_UPDATE, case_id) == 1


@requires_database
def test_a_safe_exception_and_its_restore_never_change_the_legal_duty(notice_people):
    reporter, triager = notice_people["reporter"], notice_people["triager"]
    t0 = datetime.now(UTC) - timedelta(hours=1)
    case_id = open_case(reporter, t0)
    add_legal_notice(triager, case_id, t0)
    legal_before = legal_duties(triager, case_id)
    task_id = tasks_by_kind(triager, case_id)["affected_customers"]["id"]

    task_url = f"/api/v1/cases/{case_id}/deadlines/{task_id}"
    marked = triager.patch(
        task_url,
        json={
            "status": "not_applicable",
            "reason": "the partner informed the customers",
        },
        headers={**idem(), **task_if_match(triager, task_url)},
    )
    assert marked.status_code == 200, marked.text
    assert legal_duties(triager, case_id) == legal_before

    restored = triager.post(
        f"/api/v1/cases/{case_id}/deadlines/{task_id}/restore", headers=idem()
    )
    assert restored.status_code == 200, restored.text
    assert legal_duties(triager, case_id) == legal_before
    assert tasks_by_kind(triager, case_id)["affected_customers"]["status"] == "open"


@requires_database
def test_the_conditional_14_day_advisory_keeps_its_due_date_when_it_is_opened(
    notice_people,
):
    reporter, triager = notice_people["reporter"], notice_people["triager"]
    t0 = datetime.now(UTC) - timedelta(hours=1)
    case_id = open_case(reporter, t0)
    advisory = tasks_by_kind(triager, case_id)["broader_advisory"]
    assert advisory["status"] == "not_applicable"
    assert advisory["not_applicable_reason"]
    assert datetime.fromisoformat(advisory["due_at"]) == t0 + timedelta(days=14)

    advisory_url = f"/api/v1/cases/{case_id}/deadlines/{advisory['id']}"
    opened = triager.patch(
        advisory_url,
        json={"status": "open"},
        headers={**idem(), **task_if_match(triager, advisory_url)},
    )

    assert opened.status_code == 200, opened.text
    now_open = tasks_by_kind(triager, case_id)["broader_advisory"]
    assert now_open["status"] == "open"
    assert datetime.fromisoformat(now_open["due_at"]) == t0 + timedelta(days=14)
