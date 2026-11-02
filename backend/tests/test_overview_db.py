"""The overview dashboard against PostgreSQL (F2): the caller's own series, the approval and weekly panels, and the ranges.

Every test here is marked "database". They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, with the migrations
applied (see test_database.py). They insert rows and never remove them, so use a throwaway database. Counts are read
before and after each step where the database is shared with other tests.
"""

import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from safe_kit import api, overview
from safe_kit.db import connect
from safe_kit.migrate import main as migrate
from safe_kit.weekly_updates import generate_due_updates
from test_case_records_db import ActingClient, idem, new_case, new_user

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

DIGEST = "a" * 64


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Day grouping needs the system timezone. Set one on a freshly migrated test database."""
    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute("SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)").fetchone()[0]
        if not exists:
            conn.execute("INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')")
    yield


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    api.app.dependency_overrides.clear()


def today() -> str:
    """Today's date in the system timezone, the last point of every series."""
    return datetime.now(ZoneInfo(overview.current_timezone_name())).date().isoformat()


def series(client: ActingClient, metric: str, days: int = 30) -> dict[str, int]:
    response = client.get(f"/api/v1/metrics/timeseries?metric={metric}&days={days}")
    assert response.status_code == 200, response.text
    return {point["date"]: point["count"] for point in response.json()["points"]}


def summary(client: ActingClient, days: int = 30) -> dict:
    response = client.get(f"/api/v1/overview/summary?days={days}")
    assert response.status_code == 200, response.text
    return response.json()


def set_impact(triager: ActingClient, case_id: str, severity: str) -> None:
    response = triager.put(
        f"/api/v1/cases/{case_id}/impacts/severity",
        json={"severity": severity, "scope": "Test scope, not shown on the overview"},
        headers=idem(),
    )
    assert response.status_code == 200, response.text


def assign(triager: ActingClient, case_id: str, user_id: str) -> None:
    response = triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": user_id}, headers=idem())
    assert response.status_code == 201, response.text


def insert_audit(actor_id: str, action: str, case_id: str, created_at: datetime | None = None) -> None:
    """An audit row written directly, for the cases where the test needs an action the API would not produce."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_events (actor_id, action, target_type, target_id, details, created_at) "
            "VALUES (%s, %s, 'case', %s, '{}'::jsonb, %s)",
            (actor_id, action, case_id, created_at or datetime.now(UTC)),
        )


def close_directly(case_id: str, actor_id: str) -> None:
    """Closes a case as the close route does, minus the closure checks (review and deadlines), which are not under test.

    The status history row and the case.closed audit row are written as the route writes them.
    """
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = 'Closed in a test' "
            "WHERE id = %s",
            (actor_id, case_id),
        )
        conn.execute(
            "INSERT INTO case_status_changes (case_id, action, reason, actor_id) "
            "VALUES (%s, 'closed', 'Closed in a test', %s)",
            (case_id, actor_id),
        )
    insert_audit(actor_id, "case.closed", case_id)


def soft_delete_case(case_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET is_deleted = true, deleted_at = now() WHERE id = %s AND NOT is_deleted", (case_id,)
        )


def user_id_of(client: ActingClient) -> str:
    return client.user_id


# --- the caller's own cases and actions --------------------------------------------------------------------------


def test_my_cases_by_status_count_only_the_callers_cases_and_follow_closure():
    reporter = ActingClient(new_user("reporter"))
    other = ActingClient(new_user("reporter"))
    triager = ActingClient(new_user("triager"))
    open_before = series(reporter, "my_cases_open")[today()]
    closed_before = series(reporter, "my_cases_closed")[today()]

    mine = new_case(reporter)
    new_case(reporter)
    new_case(other)

    assert series(reporter, "my_cases_open")[today()] == open_before + 2
    assert series(other, "my_cases_open")[today()] == 1

    close_directly(mine, user_id_of(triager))

    assert series(reporter, "my_cases_open")[today()] == open_before + 1
    assert series(reporter, "my_cases_closed")[today()] == closed_before + 1


def test_my_cases_include_cases_the_caller_is_assigned_to():
    reporter = ActingClient(new_user("reporter"))
    triager = ActingClient(new_user("triager"))
    investigator = ActingClient(new_user("investigator"))
    case_id = new_case(reporter)
    assert series(investigator, "my_cases_open")[today()] == 0

    assign(triager, case_id, user_id_of(investigator))

    assert series(investigator, "my_cases_open")[today()] == 1


def test_my_contributions_count_only_the_callers_own_actions():
    reporter = ActingClient(new_user("reporter"))
    other = ActingClient(new_user("reporter"))
    triager = ActingClient(new_user("triager"))
    investigator = ActingClient(new_user("investigator"))
    first = new_case(reporter)
    new_case(reporter)
    new_case(other)

    assert series(reporter, "my_actions_cases_opened")[today()] == 2
    assert series(other, "my_actions_cases_opened")[today()] == 1
    assert series(reporter, "my_actions_assignments")[today()] == 0

    assign(triager, first, user_id_of(investigator))

    assert series(triager, "my_actions_assignments")[today()] == 1
    assert series(investigator, "my_actions_assignments")[today()] == 0
    assert series(reporter, "my_actions_assignments")[today()] == 0

    close_directly(first, user_id_of(triager))

    assert series(triager, "my_actions_cases_closed")[today()] == 1
    assert series(reporter, "my_actions_cases_closed")[today()] == 0


def test_my_review_actions_count_the_review_layer_and_the_legacy_review_action():
    reviewer = ActingClient(new_user("reviewer"))
    other_reviewer = ActingClient(new_user("reviewer"))
    triager = ActingClient(new_user("triager"))
    case_id = new_case(ActingClient(new_user("reporter")))
    # The reviewers must be able to see the case, so each one is assigned to it first.
    assign(triager, case_id, user_id_of(reviewer))
    assign(triager, case_id, user_id_of(other_reviewer))
    insert_audit(user_id_of(reviewer), "case.review_layer_set", case_id)
    insert_audit(user_id_of(reviewer), "case.review_recorded", case_id)
    insert_audit(user_id_of(other_reviewer), "case.review_layer_set", case_id)

    assert series(reviewer, "my_actions_reviews")[today()] == 2
    assert series(other_reviewer, "my_actions_reviews")[today()] == 1


def test_contributions_skip_cases_the_caller_cannot_see_and_deleted_cases():
    reporter = ActingClient(new_user("reporter"))
    triager = ActingClient(new_user("triager"))
    investigator = ActingClient(new_user("investigator"))
    hidden = new_case(reporter)
    visible = new_case(reporter)
    assign(triager, visible, user_id_of(investigator))

    # The investigator is not assigned to the hidden case, so the case is not visible to them and the action is not
    # counted. The visible case is the one the investigator was assigned to.
    insert_audit(user_id_of(investigator), "case.assignment_added", hidden)
    assert series(investigator, "my_actions_assignments")[today()] == 0

    insert_audit(user_id_of(investigator), "case.assignment_added", visible)
    assert series(investigator, "my_actions_assignments")[today()] == 1

    # A deleted case is not counted, although its audit row stays.
    doomed = new_case(reporter)
    before = series(reporter, "my_actions_cases_opened")[today()]
    soft_delete_case(doomed)
    assert series(reporter, "my_actions_cases_opened")[today()] == before - 1


@pytest.mark.parametrize("days", [7, 30, 90])
def test_contribution_range_is_respected(days):
    reporter = ActingClient(new_user("reporter"))
    case_id = new_case(reporter)
    ten_days_ago = datetime.now(UTC) - timedelta(days=10)
    insert_audit(user_id_of(reporter), "case.create", case_id, created_at=ten_days_ago)

    points = series(reporter, "my_actions_cases_opened", days)

    assert len(points) == days
    # Today's case creation always counts. The row from 10 days ago counts only when the range reaches back that far.
    expected = 2 if days >= 30 else 1
    assert sum(points.values()) == expected


def test_own_series_have_one_point_per_day_of_the_range():
    reporter = ActingClient(new_user("reporter"))

    for days in (7, 30, 90):
        assert len(series(reporter, "my_cases_open", days)) == days


# --- approvals -----------------------------------------------------------------------------------------------------


def _insert_version(package_id, case_id, created_by, version, template, created_at):
    with connect() as conn:
        return conn.execute(
            "INSERT INTO disclosure_versions (package_id, case_id, version, template, tlp_audience, recipient, purpose, "
            "manifest, digest, created_by, created_at) "
            "VALUES (%s, %s, %s, %s, 'AMBER', 'Test recipient', 'Test purpose', '{}'::jsonb, %s, %s, %s) RETURNING id",
            (package_id, case_id, version, template, DIGEST, created_by, created_at),
        ).fetchone()[0]


def _insert_package(case_id, created_by) -> str:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO disclosure_packages (case_id, created_by) VALUES (%s, %s) RETURNING id", (case_id, created_by)
        ).fetchone()[0]


def test_pending_approvals_count_the_latest_unapproved_version_and_only_for_the_approver():
    reporter = ActingClient(new_user("reporter"))
    triager = ActingClient(new_user("triager"))
    approver = ActingClient(new_user("disclosure_approver"))
    case_id = new_case(reporter)
    triager_id = user_id_of(triager)
    before = summary(approver)["approvals"]
    before_rapid = next(i for i in before["by_template"] if i["template"] == "confidential_rapid_alert")["pending"]
    before_advisory = next(i for i in before["by_template"] if i["template"] == "de_identified_member_advisory")

    package = _insert_package(case_id, triager_id)
    old_time = datetime.now(UTC) - timedelta(days=10)
    _insert_version(package, case_id, triager_id, 1, "confidential_rapid_alert", old_time)  # superseded by version 2
    _insert_version(package, case_id, triager_id, 2, "confidential_rapid_alert", datetime.now(UTC) - timedelta(days=3))

    # An approved version: the approval names the recipient, purpose and digest of the version, by another user.
    approved_package = _insert_package(case_id, triager_id)
    approved_version = _insert_version(
        approved_package, case_id, triager_id, 1, "de_identified_member_advisory", datetime.now(UTC)
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO disclosure_approvals (version_id, package_id, case_id, recipient, purpose, digest, preparer_id, "
            "approved_by, separation_rule) VALUES (%s, %s, %s, 'Test recipient', 'Test purpose', %s, %s, %s, "
            "'preparer_differs_from_approver')",
            (approved_version, approved_package, case_id, DIGEST, triager_id, user_id_of(approver)),
        )

    after = summary(approver)["approvals"]
    after_rapid = next(i for i in after["by_template"] if i["template"] == "confidential_rapid_alert")
    after_advisory = next(i for i in after["by_template"] if i["template"] == "de_identified_member_advisory")

    assert after["pending"] == before["pending"] + 1
    assert after_rapid["pending"] == before_rapid + 1
    assert after_advisory["pending"] == before_advisory["pending"]
    # The version is three days old, so the oldest pending age is at least three days.
    assert after["oldest_pending_age_days"] >= 3

    # Nobody else gets the approval panel.
    assert summary(reporter)["approvals"] is None
    assert summary(triager)["approvals"] is None
    assert summary(ActingClient(new_user("investigator")))["approvals"] is None


# --- weekly updates ------------------------------------------------------------------------------------------------


def test_weekly_panel_counts_open_significant_risks_for_assigned_investigators_and_reviewers():
    reporter = ActingClient(new_user("reporter"))
    triager = ActingClient(new_user("triager"))
    investigator = ActingClient(new_user("investigator"))
    reviewer = ActingClient(new_user("reviewer"))
    risky = new_case(reporter)
    minor = new_case(reporter)
    set_impact(triager, risky, "critical")
    set_impact(triager, minor, "medium")
    assign(triager, risky, user_id_of(investigator))
    assign(triager, risky, user_id_of(reviewer))
    assign(triager, minor, user_id_of(investigator))

    generate_due_updates(datetime.now(UTC))

    for client in (investigator, reviewer):
        weekly = summary(client)["weekly_updates"]
        assert weekly["open_significant_risk_cases"] == 1
        assert weekly["latest_update_date"] == today()
        open_row = next(row for row in weekly["by_status"] if row["status"] == "open")
        assert open_row == {
            "status": "open",
            "cases_with_significant_risk": 1,
            "cases_with_weekly_update": 1,
            "latest_update_date": today(),
        }
    assert summary(triager)["weekly_updates"] is None
    assert summary(reporter)["weekly_updates"] is None


def test_weekly_panel_without_a_significant_risk_shows_no_update_date():
    investigator = ActingClient(new_user("investigator"))

    weekly = summary(investigator)["weekly_updates"]

    assert weekly["open_significant_risk_cases"] == 0
    assert weekly["latest_update_date"] is None
    assert all(row["cases_with_significant_risk"] == 0 for row in weekly["by_status"])


def test_an_unlisted_range_is_refused_for_the_own_series():
    reporter = ActingClient(new_user("reporter"))

    response = reporter.get("/api/v1/metrics/timeseries?metric=my_cases_open&days=14")

    assert response.status_code == 422
