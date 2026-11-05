"""The 30-day analysis obligation against PostgreSQL (F3 A5; plans/01, the t0 + 30 days row).

Every test here is marked "database". They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings, with the migrations
applied (see test_database.py). They create rows and never remove them, so use a throwaway database. A task is
soft-deleted in one test with a direct update, the way the application marks a row deleted.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from if_match import task_if_match
from safe_kit import api, auth, cases, users
from safe_kit.case_records import RecordOutcome, record_awareness_event
from safe_kit.cases import CaseScope, Outcome
from safe_kit.db import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

KIND = "preliminary_control_failure_analysis"
PUBLIC_KIND = "preliminary_public_report"
# A Friday at 10:00 UTC (13:00 in Istanbul). Thirty elapsed days later is a Sunday: the due time stays on the weekend.
T0 = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
DUE = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations" / "0051_control_failure_analysis_task.sql"
)


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


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


def new_user(role: str) -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


def create_case_via_api(reporter_id: str) -> dict:
    response = ActingClient(reporter_id).post(
        "/api/v1/cases",
        json={"title": "Analysis deadline", "awareness_time": T0.isoformat()},
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def analysis_task(case_json: dict) -> dict:
    tasks = [task for task in case_json["deadlines"] if task["kind"] == KIND]
    assert len(tasks) == 1, case_json["deadlines"]
    return tasks[0]


def stored_rows(case_id: str, kind: str = KIND) -> list[tuple]:
    """(id, due_at, status, is_deleted) of every stored row of one kind, deleted rows included."""
    with connect() as conn:
        return conn.execute(
            "SELECT id::text, due_at, status, is_deleted FROM deadline_tasks WHERE case_id = %s AND kind = %s",
            (case_id, kind),
        ).fetchall()


def assign(case_id: str, investigator_id: str, triager_id: str) -> None:
    outcome, _ = cases.assign_member(
        case_id,
        investigator_id,
        CaseScope(user_id=triager_id, read_all=True, can_assign=True),
    )
    assert outcome is Outcome.OK, outcome


def closure_blockers(case_id: str, triager_id: str) -> list[str]:
    """The reasons a close would be refused now. The case has no review in these tests, so it is always blocked."""
    outcome, blockers = cases.close_case(
        case_id,
        "test: closure check",
        CaseScope(user_id=triager_id, read_all=True, can_assign=True),
    )
    assert outcome is Outcome.BLOCKED, outcome
    return blockers


def backfill_statement() -> str:
    """The INSERT of migration 0051, limited to one case so that the shared test database is not touched."""
    text = MIGRATION.read_text(encoding="utf-8")
    statement = text[text.index("INSERT INTO deadline_tasks") :].strip().rstrip(";")
    scoped = statement.replace("WHERE NOT c.is_deleted", "WHERE NOT c.is_deleted AND c.id = %s")
    assert scoped != statement
    return scoped


def test_the_task_is_created_once_with_the_case_and_due_thirty_days_after_t0():
    reporter_id = new_user("reporter")
    case = create_case_via_api(reporter_id)

    task = analysis_task(case)
    assert task["status"] == "open"
    assert datetime.fromisoformat(task["due_at"]) == DUE
    assert DUE.weekday() == 6  # a Sunday: the 30 elapsed days are not moved to a working day

    rows = stored_rows(case["id"])
    assert len(rows) == 1
    assert rows[0][1] == DUE
    assert rows[0][3] is False


def test_an_earlier_operator_event_moves_the_task_with_the_public_report_and_adds_no_row():
    reporter_id = new_user("reporter")
    case = create_case_via_api(reporter_id)
    earlier = T0 - timedelta(days=2)
    outcome, _ = record_awareness_event(
        case["id"],
        "operator",
        "incident",
        earlier,
        "test: operator event before the awareness time",
        CaseScope(user_id=reporter_id, read_own=True),
    )
    assert outcome is RecordOutcome.OK

    assert stored_rows(case["id"])[0][1] == earlier + timedelta(days=30)
    assert stored_rows(case["id"], PUBLIC_KIND)[0][1] == earlier + timedelta(days=30)

    # A second resync finds nothing to move and creates nothing.
    with connect() as conn:
        _, moved = cases.resync_deadlines(conn, case["id"])
    assert moved == {}
    assert len(stored_rows(case["id"])) == 1


def test_the_database_refuses_a_second_task_of_the_same_kind_and_an_unknown_kind():
    case = create_case_via_api(new_user("reporter"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with connect() as conn:
            conn.execute(
                "INSERT INTO deadline_tasks (case_id, kind, due_at, status) VALUES (%s, %s, %s, 'open')",
                (case["id"], KIND, DUE),
            )
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect() as conn:
            conn.execute(
                "INSERT INTO deadline_tasks (case_id, kind, due_at, status) VALUES (%s, 'not_a_kind', %s, 'open')",
                (case["id"], DUE),
            )
    assert len(stored_rows(case["id"])) == 1


def test_the_migration_backfill_adds_the_task_once_to_a_case_written_before_it():
    # A case written before migration 0051 has no analysis task. t0 is the earliest of the awareness time and the
    # operator events, so a later operator event does not move the due time. The case is recent, so its 30 days
    # have not run out when the backfill runs.
    recent = datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)
    reporter_id = new_user("reporter")
    with connect() as conn:
        seq = conn.execute("SELECT nextval('case_reference_seq')").fetchone()[0]
        case_id = conn.execute(
            """
            INSERT INTO cases (reference, title, awareness_time, created_by, status, calendar_version, calendar_timezone)
            VALUES (%s, 'Written before 0051', %s, %s, 'open', 'provisional-1', 'Europe/Istanbul')
            RETURNING id::text
            """,
            (f"SAFE-2026-{seq:06d}", recent, reporter_id),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO awareness_events (case_id, awareness_kind, event_kind, occurred_at, basis, recorded_by) "
            "VALUES (%s, 'operator', 'incident', %s, 'test: later operator event', %s)",
            (case_id, recent + timedelta(days=1), reporter_id),
        )
    statement = backfill_statement()
    for _ in range(2):
        with connect() as conn:
            conn.execute(statement, (case_id,))

    rows = stored_rows(case_id)
    assert len(rows) == 1
    assert rows[0][1] == recent + timedelta(days=30)
    assert rows[0][2] == "open"


def test_the_backfill_skips_a_closed_case_and_an_open_case_already_past_its_due_time():
    # Owner decision (2026-11-03): no automatic task for a closed case, nor for an open case whose 30 days have run out.
    reporter_id = new_user("reporter")
    old = datetime.now(UTC).replace(microsecond=0) - timedelta(days=40)
    statement = backfill_statement()
    case_ids = []
    with connect() as conn:
        for title, closed in (("Closed before 0051", True), ("Overdue before 0051", False)):
            seq = conn.execute("SELECT nextval('case_reference_seq')").fetchone()[0]
            case_ids.append(
                conn.execute(
                    """
                    INSERT INTO cases (reference, title, awareness_time, created_by, status, calendar_version,
                                       calendar_timezone, closed_at, closed_by, close_reason)
                    VALUES (%s, %s, %s, %s, %s, 'provisional-1', 'Europe/Istanbul', %s, %s, %s)
                    RETURNING id::text
                    """,
                    (
                        f"SAFE-2026-{seq:06d}",
                        title,
                        old,
                        reporter_id,
                        "closed" if closed else "open",
                        old if closed else None,
                        reporter_id if closed else None,
                        "Closed before the migration" if closed else None,
                    ),
                ).fetchone()[0]
            )
    for case_id in case_ids:
        with connect() as conn:
            conn.execute(statement, (case_id,))

    for case_id in case_ids:
        assert stored_rows(case_id) == []


def test_the_assigned_investigator_completes_the_task_and_only_then_the_closure_blocker_clears():
    reporter_id = new_user("reporter")
    case = create_case_via_api(reporter_id)
    triager_id = new_user("triager")
    investigator_id = new_user("investigator")
    assign(case["id"], investigator_id, triager_id)
    task_id = analysis_task(case)["id"]
    url = f"/api/v1/cases/{case['id']}/deadlines/{task_id}"
    investigator = ActingClient(investigator_id)

    before = closure_blockers(case["id"], triager_id)
    assert f"deadline task {KIND} is open" in before

    response = investigator.patch(
        url,
        json={"status": "done"},
        headers={**idem(), **task_if_match(investigator, url)},
    )
    assert response.status_code == 200, response.text
    assert analysis_task(response.json())["status"] == "done"

    after = closure_blockers(case["id"], triager_id)
    assert not any(KIND in blocker for blocker in after)
    # The other deadline tasks still block closure: only this one was completed.
    assert f"deadline task {PUBLIC_KIND} is open" in after


def test_an_investigator_cannot_mark_the_task_not_applicable():
    case = create_case_via_api(new_user("reporter"))
    triager_id = new_user("triager")
    investigator_id = new_user("investigator")
    assign(case["id"], investigator_id, triager_id)
    url = f"/api/v1/cases/{case['id']}/deadlines/{analysis_task(case)['id']}"
    investigator = ActingClient(investigator_id)

    response = investigator.patch(
        url,
        json={"status": "not_applicable", "reason": "test: not for this case"},
        headers={**idem(), **task_if_match(investigator, url)},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "only a triager may mark a deadline not applicable"
    assert stored_rows(case["id"])[0][2] == "open"


def test_a_user_who_cannot_see_the_case_neither_sees_nor_completes_the_task():
    # The route checks the permission before the case visibility, so a caller with no deadline permission is refused
    # with 403 and an unassigned investigator with 404. Both refusals must leave the task open.
    case = create_case_via_api(new_user("reporter"))
    url = f"/api/v1/cases/{case['id']}/deadlines/{analysis_task(case)['id']}"
    stranger = ActingClient(new_user("reporter"))
    unassigned = ActingClient(new_user("investigator"))

    for client in (stranger, unassigned):
        assert client.get(f"/api/v1/cases/{case['id']}").status_code == 404
        response = client.patch(
            url, json={"status": "done"}, headers={**idem(), "If-Match": '"1"'}
        )
        assert response.status_code in (403, 404), response.text
    assert stored_rows(case["id"])[0][2] == "open"


def test_a_soft_deleted_task_is_hidden_refused_and_kept_in_the_table():
    reporter_id = new_user("reporter")
    case = create_case_via_api(reporter_id)
    triager_id = new_user("triager")
    investigator_id = new_user("investigator")
    assign(case["id"], investigator_id, triager_id)
    task_id = analysis_task(case)["id"]
    url = f"/api/v1/cases/{case['id']}/deadlines/{task_id}"
    investigator = ActingClient(investigator_id)

    with connect() as conn:
        conn.execute(
            "UPDATE deadline_tasks SET is_deleted = true, deleted_at = now() WHERE case_id = %s AND kind = %s",
            (case["id"], KIND),
        )
        cases.resync_deadlines(conn, case["id"])

    listed = investigator.get(f"/api/v1/cases/{case['id']}").json()
    assert KIND not in {task["kind"] for task in listed["deadlines"]}
    response = investigator.patch(
        url, json={"status": "done"}, headers={**idem(), "If-Match": '"1"'}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "deadline task not found"

    rows = stored_rows(case["id"])
    assert len(rows) == 1
    assert rows[0][3] is True
    assert not any(KIND in blocker for blocker in closure_blockers(case["id"], triager_id))
