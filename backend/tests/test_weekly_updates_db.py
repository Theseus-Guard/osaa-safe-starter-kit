"""Weekly updates against PostgreSQL (T02, weekly part): creation, the cadence, visibility, the API and the worker run.

Every test here is marked "database". They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, with the migrations
applied (see test_database.py). They create rows and never remove them, so use a throwaway database.
"""

import uuid
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from safe_kit import api, worker
from safe_kit.db import connect
from safe_kit.migrate import main as migrate
from safe_kit.weekly_updates import generate_due_updates
from test_case_records_db import ActingClient, new_case, new_user

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = Draft202012Validator(
    json.loads(
        (ROOT / "schemas" / "weekly-update.v0.json").read_text(encoding="utf-8")
    ),
    format_checker=FormatChecker(),
)
START = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
WEEK = timedelta(days=7)
SCOPE_SECRET = "Customer records SECRET-SCOPE-TEXT"


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)"
        ).fetchone()[0]
        if not exists:
            conn.execute(
                "INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')"
            )
    yield


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    api.app.dependency_overrides.clear()


@pytest.fixture
def people():
    return {
        "reporter": ActingClient(new_user("reporter")),
        "other_reporter": ActingClient(new_user("reporter")),
        "triager": ActingClient(new_user("triager")),
        "investigator": ActingClient(new_user("investigator")),
    }


def set_impact(
    triager: ActingClient, case_id: str, area: str = "severity", severity: str = "high"
) -> None:
    response = triager.put(
        f"/api/v1/cases/{case_id}/impacts/{area}",
        json={"severity": severity, "scope": SCOPE_SECRET},
        headers=idem(),
    )
    assert response.status_code == 200, response.text


def weekly_count(case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM weekly_updates WHERE case_id = %s AND NOT is_deleted",
            (case_id,),
        ).fetchone()[0]


def audit_count(case_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'case.weekly_update_created' "
            "AND target_id = %s AND NOT is_deleted",
            (case_id,),
        ).fetchone()[0]


def deadline_rows(case_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT kind, due_at, status FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted ORDER BY kind",
            (case_id,),
        ).fetchall()


def close_in_database(case_id: str, user_id: str) -> None:
    # The closure flow needs a reviewed round; these tests only need the case to be closed.
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = 'test' WHERE id = %s",
            (user_id, case_id),
        )


def first_update_id(triager: ActingClient, case_id: str) -> str:
    listed = triager.get(f"/api/v1/cases/{case_id}/weekly-updates").json()
    return listed[0]["weekly_update_id"]


def test_open_significant_risk_gets_one_weekly_update(people):
    case_id = new_case(people["reporter"])
    set_impact(people["triager"], case_id, severity="high")
    tasks_before = deadline_rows(case_id)

    generate_due_updates(START)

    assert weekly_count(case_id) == 1
    listed = people["triager"].get(f"/api/v1/cases/{case_id}/weekly-updates")
    assert listed.status_code == 200
    assert [item["sequence"] for item in listed.json()] == [1]
    assert listed.json()[0]["significant_risks"] == [
        {"area": "severity", "severity": "high"}
    ]
    # The update changes no deadline task, and the closure rule is not touched.
    assert deadline_rows(case_id) == tasks_before


def test_critical_severity_is_also_a_significant_risk(people):
    case_id = new_case(people["reporter"])
    set_impact(people["triager"], case_id, area="reversibility", severity="critical")

    generate_due_updates(START)

    assert weekly_count(case_id) == 1


def test_running_again_in_the_same_week_creates_no_duplicate(people):
    case_id = new_case(people["reporter"])
    set_impact(people["triager"], case_id)

    generate_due_updates(START)
    generate_due_updates(START)
    generate_due_updates(START + timedelta(hours=1))
    generate_due_updates(START + WEEK - timedelta(minutes=1))

    assert weekly_count(case_id) == 1
    assert audit_count(case_id) == 1


def test_a_new_update_is_created_seven_days_later_and_numbered_on(people):
    case_id = new_case(people["reporter"])
    set_impact(people["triager"], case_id)

    generate_due_updates(START)
    generate_due_updates(START + WEEK)

    listed = people["triager"].get(f"/api/v1/cases/{case_id}/weekly-updates").json()
    assert [item["sequence"] for item in listed] == [1, 2]
    assert listed[1]["next_update_due_at"] == "2026-11-16T09:00:00Z"


def test_worker_run_twice_creates_one_update(people):
    case_id = new_case(people["reporter"])
    set_impact(people["triager"], case_id)

    worker.create_weekly_updates()
    worker.create_weekly_updates()

    assert weekly_count(case_id) == 1


def test_no_update_without_a_significant_risk(people):
    medium = new_case(people["reporter"])
    set_impact(people["triager"], medium, severity="medium")
    unknown = new_case(people["reporter"])
    set_impact(people["triager"], unknown, severity="unknown")
    no_impacts = new_case(people["reporter"])

    generate_due_updates(START)

    assert [weekly_count(case_id) for case_id in (medium, unknown, no_impacts)] == [
        0,
        0,
        0,
    ]


def test_a_closed_case_gets_no_new_update(people):
    case_id = new_case(people["reporter"])
    set_impact(people["triager"], case_id)
    generate_due_updates(START)

    close_in_database(case_id, people["triager"].user_id)
    generate_due_updates(START + WEEK)

    assert weekly_count(case_id) == 1


def test_only_visible_cases_are_listed_and_hidden_cases_answer_404(people):
    case_id = new_case(people["reporter"])
    set_impact(people["triager"], case_id)
    generate_due_updates(START)
    update_id = first_update_id(people["triager"], case_id)

    for hidden in ("other_reporter", "investigator"):
        assert (
            people[hidden].get(f"/api/v1/cases/{case_id}/weekly-updates").status_code
            == 404
        )
        assert (
            people[hidden]
            .get(f"/api/v1/cases/{case_id}/weekly-updates/{update_id}")
            .status_code
            == 404
        )

    # The creator (read_own) and a triager (read_all) see the case, so they see its updates.
    assert (
        people["reporter"].get(f"/api/v1/cases/{case_id}/weekly-updates").status_code
        == 200
    )
    assert (
        people["triager"]
        .get(f"/api/v1/cases/{case_id}/weekly-updates/{update_id}")
        .status_code
        == 200
    )


def test_an_update_of_another_case_is_not_found_under_this_case(people):
    case_a = new_case(people["reporter"])
    case_b = new_case(people["reporter"])
    set_impact(people["triager"], case_a)
    set_impact(people["triager"], case_b)
    generate_due_updates(START)
    update_of_a = first_update_id(people["triager"], case_a)

    response = people["triager"].get(
        f"/api/v1/cases/{case_b}/weekly-updates/{update_of_a}"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "weekly update not found"


def test_unknown_case_answers_404_for_the_same_reason_as_a_hidden_one(people):
    unknown = "11111111-2222-4333-8444-555555555555"

    listed = people["triager"].get(f"/api/v1/cases/{unknown}/weekly-updates")

    assert listed.status_code == 404
    assert listed.json()["detail"] == "case not found"


def test_document_follows_the_schema_and_holds_no_free_text(people):
    case_id = new_case(people["reporter"])
    set_impact(people["triager"], case_id)
    generate_due_updates(START)
    update_id = first_update_id(people["triager"], case_id)

    response = people["reporter"].get(
        f"/api/v1/cases/{case_id}/weekly-updates/{update_id}"
    )

    assert response.status_code == 200
    document = response.json()
    VALIDATOR.validate(document)
    assert document["case_id"] == case_id
    assert document["weekly_update_id"] == update_id
    assert "SECRET-SCOPE-TEXT" not in response.text


def test_weekly_updates_are_read_only_over_the_api(people):
    case_id = new_case(people["reporter"])
    set_impact(people["triager"], case_id)
    generate_due_updates(START)

    response = people["triager"].post(
        f"/api/v1/cases/{case_id}/weekly-updates", json={}
    )

    assert response.status_code == 405
    assert weekly_count(case_id) == 1


def test_each_created_update_writes_one_audit_row_and_a_repeat_writes_none(people):
    case_id = new_case(people["reporter"])
    set_impact(people["triager"], case_id)

    generate_due_updates(START)
    generate_due_updates(START)

    assert audit_count(case_id) == 1
