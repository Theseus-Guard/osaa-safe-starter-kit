"""Case list filters and the list fields against PostgreSQL: status, next deadline, last update, and visibility.

Every test here is marked "database". They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, with the migrations
applied (see test_database.py). They create rows and never remove them, so use a throwaway database.

The tests set due dates, statuses and closure directly in the database, because the API cannot set a due date.
The application itself never changes these columns that way.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, users
from safe_kit.db import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

BASE = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
    from safe_kit.migrate import main as migrate

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


def new_user(role: str) -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


class ActingClient:
    """Makes requests as one real user. The caller is set before every request, so clients never share a caller."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.client = TestClient(api.app)

    def _request(self, method: str, url: str, **kwargs):
        user = users.find_user_by_id(self.user_id)
        assert user is not None
        api.app.dependency_overrides[auth.current_user] = lambda: user
        return getattr(self.client, method)(url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("post", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._request("patch", url, **kwargs)


def past(hours: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def new_case(reporter: ActingClient, title: str = "Case") -> dict:
    response = reporter.post(
        "/api/v1/cases", json={"title": title, "awareness_time": past()}, headers=idem()
    )
    assert response.status_code == 201, response.text
    return response.json()


def set_deadline(
    case_id: str,
    kind: str,
    *,
    due_at: datetime | None = None,
    status: str | None = None,
) -> None:
    """Sets a due date and or a status directly. A not_applicable task gets the reason the constraint requires."""
    with connect() as conn:
        if due_at is not None:
            conn.execute(
                "UPDATE deadline_tasks SET due_at = %s WHERE case_id = %s AND kind = %s",
                (due_at, case_id, kind),
            )
        if status is not None:
            reason = "test: not applicable" if status == "not_applicable" else None
            conn.execute(
                "UPDATE deadline_tasks SET status = %s, not_applicable_reason = %s WHERE case_id = %s AND kind = %s",
                (status, reason, case_id, kind),
            )


def soft_delete_deadline(case_id: str, kind: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE deadline_tasks SET is_deleted = true, deleted_at = now() WHERE case_id = %s AND kind = %s",
            (case_id, kind),
        )


def close_in_database(case_id: str, actor_id: str) -> None:
    # Closing through the API needs a review and settled deadlines. The closed state is set here for the list tests.
    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', closed_at = now(), closed_by = %s, close_reason = 'test' WHERE id = %s",
            (actor_id, case_id),
        )


def audit_time(case_id: str, action: str) -> datetime:
    with connect() as conn:
        row = conn.execute(
            "SELECT max(created_at) FROM audit_events WHERE target_type = 'case' AND target_id = %s AND action = %s "
            "AND NOT is_deleted",
            (case_id, action),
        ).fetchone()
    assert row is not None and row[0] is not None
    return row[0]


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def item_for(page: dict, case_id: str) -> dict:
    matches = [item for item in page["items"] if item["id"] == case_id]
    assert len(matches) == 1, page
    return matches[0]


def fresh_case_with_known_deadlines(reporter: ActingClient) -> str:
    """A case whose five SAFE tasks have known due dates: affected customers done, broader advisory not applicable."""
    case_id = new_case(reporter)["id"]
    set_deadline(
        case_id, "affected_customers", due_at=BASE + timedelta(days=1), status="done"
    )
    set_deadline(case_id, "preliminary_safe_report", due_at=BASE + timedelta(days=2))
    set_deadline(case_id, "broader_advisory", due_at=BASE + timedelta(hours=1))
    set_deadline(case_id, "preliminary_public_report", due_at=BASE + timedelta(days=3))
    set_deadline(case_id, "remediation_status", due_at=BASE + timedelta(days=4))
    return case_id


# --- next deadline ---------------------------------------------------------------------------------------------------


def test_next_deadline_is_the_earliest_open_task():
    reporter = ActingClient(new_user("reporter"))
    case_id = fresh_case_with_known_deadlines(reporter)

    item = item_for(reporter.get("/api/v1/cases").json(), case_id)

    # Done (affected_customers at +1 day) and not applicable (broader_advisory at +1 hour) are ignored.
    assert parse(item["next_deadline"]) == BASE + timedelta(days=2)


def test_next_deadline_is_null_when_no_task_is_open():
    reporter = ActingClient(new_user("reporter"))
    case_id = fresh_case_with_known_deadlines(reporter)
    for kind in (
        "preliminary_safe_report",
        "preliminary_public_report",
        "remediation_status",
    ):
        set_deadline(case_id, kind, status="done")

    item = item_for(reporter.get("/api/v1/cases").json(), case_id)

    assert item["next_deadline"] is None


def test_next_deadline_ignores_a_soft_deleted_task():
    reporter = ActingClient(new_user("reporter"))
    case_id = fresh_case_with_known_deadlines(reporter)
    soft_delete_deadline(case_id, "preliminary_safe_report")

    item = item_for(reporter.get("/api/v1/cases").json(), case_id)

    assert parse(item["next_deadline"]) == BASE + timedelta(days=3)


def test_next_deadline_counts_an_open_weekly_update():
    reporter = ActingClient(new_user("reporter"))
    case_id = fresh_case_with_known_deadlines(reporter)
    with connect() as conn:
        conn.execute(
            "INSERT INTO deadline_tasks (case_id, kind, due_at, status) VALUES (%s, 'weekly_update', %s, 'open')",
            (case_id, BASE + timedelta(hours=12)),
        )

    item = item_for(reporter.get("/api/v1/cases").json(), case_id)

    assert parse(item["next_deadline"]) == BASE + timedelta(hours=12)


def test_next_deadline_is_computed_for_each_row_of_a_page():
    reporter = ActingClient(new_user("reporter"))
    first = fresh_case_with_known_deadlines(reporter)
    second = new_case(reporter, "Second")[
        "id"
    ]  # its own default deadlines, all later than the known ones
    page = reporter.get("/api/v1/cases").json()

    assert parse(item_for(page, first)["next_deadline"]) == BASE + timedelta(days=2)
    assert item_for(page, second)["next_deadline"] is not None
    assert (
        item_for(page, second)["next_deadline"]
        != item_for(page, first)["next_deadline"]
    )


# --- visibility ------------------------------------------------------------------------------------------------------


def test_a_case_the_caller_cannot_see_is_not_listed_and_its_deadline_does_not_leak():
    owner = ActingClient(new_user("reporter"))
    stranger = ActingClient(new_user("reporter"))
    case_id = fresh_case_with_known_deadlines(owner)
    owner_page = owner.get("/api/v1/cases").json()
    deadline_text = item_for(owner_page, case_id)["next_deadline"]
    assert deadline_text is not None

    response = stranger.get("/api/v1/cases?status=open")

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert case_id not in response.text
    assert deadline_text not in response.text


# --- status filter ---------------------------------------------------------------------------------------------------


def test_status_filter_combines_with_search_and_paging():
    reporter = ActingClient(new_user("reporter"))
    closed = new_case(reporter, "Leak A")
    wanted = new_case(reporter, "Leak B")
    new_case(reporter, "Other C")
    close_in_database(closed["id"], reporter.user_id)

    assert len(reporter.get("/api/v1/cases?status=closed").json()["items"]) == 1
    open_page = reporter.get("/api/v1/cases?status=open").json()
    assert len(open_page["items"]) == 2
    assert len(reporter.get("/api/v1/cases").json()["items"]) == 3

    # The search narrows inside the status: the reference of the open case matches, the reference of the closed one does not.
    by_reference = reporter.get(
        f"/api/v1/cases?status=open&q={wanted['reference']}"
    ).json()
    assert [item["id"] for item in by_reference["items"]] == [wanted["id"]]
    assert (
        reporter.get(f"/api/v1/cases?status=closed&q={wanted['reference']}").json()[
            "items"
        ]
        == []
    )

    first_page = reporter.get("/api/v1/cases?status=open&limit=1").json()
    assert len(first_page["items"]) == 1
    assert first_page["items"][0]["status"] == "open"
    assert first_page["next_cursor"] is not None
    second_page = reporter.get(
        f"/api/v1/cases?status=open&limit=1&cursor={first_page['next_cursor']}"
    ).json()
    assert len(second_page["items"]) == 1
    assert second_page["items"][0]["id"] != first_page["items"][0]["id"]
    assert second_page["next_cursor"] is None


def test_status_filter_refuses_an_unknown_value():
    reporter = ActingClient(new_user("reporter"))
    assert reporter.get("/api/v1/cases?status=archived").status_code == 400


# --- last update ----------------------------------------------------------------------------------------------------


def test_updated_at_starts_at_the_creation_audit_row():
    reporter = ActingClient(new_user("reporter"))
    case = new_case(reporter)

    item = item_for(reporter.get("/api/v1/cases").json(), case["id"])

    assert parse(item["updated_at"]) == audit_time(case["id"], "case.create")


def test_updated_at_moves_to_the_latest_change():
    reporter = ActingClient(new_user("reporter"))
    triager = ActingClient(new_user("triager"))
    case_id = new_case(reporter)["id"]
    created = parse(
        item_for(reporter.get("/api/v1/cases").json(), case_id)["updated_at"]
    )

    task_id = task_id_of(triager, case_id, "affected_customers")
    response = triager.patch(
        f"/api/v1/cases/{case_id}/deadlines/{task_id}",
        json={"status": "done"},
        headers=idem(),
    )
    assert response.status_code == 200, response.text

    item = item_for(reporter.get("/api/v1/cases").json(), case_id)
    changed = audit_time(case_id, "case.deadline_updated")
    assert parse(item["updated_at"]) == changed
    assert changed > created


def test_reading_a_case_does_not_change_updated_at():
    reporter = ActingClient(new_user("reporter"))
    case_id = new_case(reporter)["id"]
    before = item_for(reporter.get("/api/v1/cases").json(), case_id)["updated_at"]
    # A read-only audit row, written later than the creation, must not count as a change.
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_events (action, target_type, target_id, details, created_at) "
            "VALUES ('reporter_contact.read', 'case', %s, '{}'::jsonb, now() + interval '1 hour')",
            (case_id,),
        )

    after = item_for(reporter.get("/api/v1/cases").json(), case_id)["updated_at"]

    assert after == before


def test_evidence_changes_count_only_for_a_caller_with_evidence_read():
    owner = ActingClient(new_user("reporter"))
    triager = ActingClient(
        new_user("triager")
    )  # holds cases:read_all and evidence:read
    case_id = new_case(owner)["id"]
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_events (action, target_type, target_id, details, created_at) "
            "VALUES ('evidence.uploaded', 'evidence', %s, jsonb_build_object('case_id', %s::text), "
            "now() + interval '2 hours')",
            (str(uuid.uuid4()), case_id),
        )
        uploaded = conn.execute(
            "SELECT created_at FROM audit_events WHERE action = 'evidence.uploaded' AND details->>'case_id' = %s",
            (case_id,),
        ).fetchone()[0]
    created = audit_time(case_id, "case.create")

    with_evidence = item_for(triager.get("/api/v1/cases").json(), case_id)
    without_evidence = item_for(owner.get("/api/v1/cases").json(), case_id)

    assert parse(with_evidence["updated_at"]) == uploaded
    assert parse(without_evidence["updated_at"]) == created


def task_id_of(client: ActingClient, case_id: str, kind: str) -> str:
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    return next(task["id"] for task in detail["deadlines"] if task["kind"] == kind)
