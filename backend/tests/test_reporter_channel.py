"""Reporter channel: the reporter's account and contact never reach a caller, and the intake stores the contact apart.

Two layers, as in test_case_access.py:
- Tests without a database replace the service functions and check the rules and the HTTP mapping.
- Tests marked "database" run the flow through the API against PostgreSQL. They need SAFE_TEST_DATABASE=1 and the
  SAFE_DB_* settings with the migrations applied (see test_database.py). They create rows and never remove them, so
  use a throwaway database.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from contextlib import contextmanager

from safe_kit import api, audit, auth, system_timezone, users
from safe_kit.cases import Case, HistoryEntry
from safe_kit.db import connect
from test_case_records_db import reporter_contact_granted_directly
from safe_kit.rbac import (
    DEFAULT_ROLE_PERMISSIONS,
    list_role_permissions,
    set_role_permissions,
)
from safe_kit.reporter_channel import (
    REPORTER_CONTACT_READ,
    ContactOutcome,
    hide_reporter,
)
from safe_kit.users import User
from error_body import without_request_id


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


REPORTER_ID = "11111111-1111-1111-1111-111111111111"
TRIAGER_ID = "22222222-2222-2222-2222-222222222222"
REVIEWER_ID = "44444444-4444-4444-4444-444444444444"
CASE_ID = "99999999-9999-9999-9999-999999999999"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
PAST_AWARENESS = "2026-10-31T09:00:00+00:00"

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def make_user(user_id, role):
    return User(
        id=user_id,
        username=f"user-{role}",
        role=role,
        is_active=True,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=NOW,
    )


def sample_case() -> Case:
    return Case(
        id=CASE_ID,
        reference="SAFE-2026-000001",
        title="Sample",
        awareness_time=NOW,
        status="open",
        calendar_version="provisional-1",
        calendar_timezone="Europe/Istanbul",
        created_at=NOW,
        created_by=REPORTER_ID,
        deadlines=[],
    )


# --- rules without a database ----------------------------------------------------------------------------------


def test_hide_reporter_blanks_only_the_case_creation_actor():
    entries = [
        HistoryEntry(
            id="e1",
            action=audit.CASE_CREATE,
            created_at=NOW,
            actor_id=REPORTER_ID,
            details={"a": 1},
        ),
        HistoryEntry(
            id="e2",
            action=audit.CASE_REVIEW,
            created_at=NOW,
            actor_id=REVIEWER_ID,
            details={"b": 2},
        ),
    ]
    hidden = hide_reporter(entries)
    assert [entry.actor_id for entry in hidden] == [None, REVIEWER_ID]
    # The action, time, and details of the creation entry stay.
    assert hidden[0].action == audit.CASE_CREATE and hidden[0].details == {"a": 1}


# --- HTTP mapping without a database ---------------------------------------------------------------------------


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user(REPORTER_ID, "reporter")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(
        auth,
        "permissions_for_role",
        lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()),
    )
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(caller):
    return TestClient(api.app)


def as_role(caller, user_id, role):
    caller["user"] = make_user(user_id, role)


def test_report_passes_the_stripped_contact_and_returns_no_reporter_fields(
    client, caller, monkeypatch
):
    seen = {}

    def fake_submit(title, awareness_time, contact, reporter_id, **kwargs):
        seen.update(title=title, contact=contact, reporter_id=reporter_id)
        return sample_case()

    monkeypatch.setattr(api, "submit_report", fake_submit)
    response = client.post(
        "/api/v1/reports",
        json={
            "title": "Case",
            "awareness_time": PAST_AWARENESS,
            "contact": "  reach me at the desk  ",
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    assert seen == {
        "title": "Case",
        "contact": "reach me at the desk",
        "reporter_id": REPORTER_ID,
    }
    body = response.json()
    assert "contact" not in body and "created_by" not in body
    assert REPORTER_ID not in response.text


def test_report_without_contact_or_with_blank_contact_stores_none(
    client, caller, monkeypatch
):
    seen = []

    def fake_submit(title, awareness_time, contact, reporter_id, **kwargs):
        seen.append(contact)
        return sample_case()

    monkeypatch.setattr(api, "submit_report", fake_submit)
    assert (
        client.post(
            "/api/v1/reports",
            json={"title": "Case", "awareness_time": PAST_AWARENESS},
            headers=idem(),
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/reports",
            json={"title": "Case", "awareness_time": PAST_AWARENESS, "contact": "   "},
            headers=idem(),
        ).status_code
        == 201
    )
    assert seen == [None, None]


def test_contact_longer_than_200_characters_is_refused(client, caller, monkeypatch):
    monkeypatch.setattr(
        api,
        "submit_report",
        lambda *args, **kwargs: pytest.fail("must not reach the service"),
    )
    response = client.post(
        "/api/v1/reports",
        json={"title": "Case", "awareness_time": PAST_AWARENESS, "contact": "x" * 201},
        headers=idem(),
    )
    assert response.status_code == 400


def test_report_needs_the_submit_permission(client, caller, monkeypatch):
    monkeypatch.setattr(
        api,
        "submit_report",
        lambda *args, **kwargs: pytest.fail("must not reach the service"),
    )
    as_role(caller, TRIAGER_ID, "triager")
    response = client.post(
        "/api/v1/reports",
        json={"title": "Case", "awareness_time": PAST_AWARENESS},
        headers=idem(),
    )
    assert response.status_code == 403


def test_history_hides_the_reporter_account_from_triagers_too(
    client, caller, monkeypatch
):
    entries = [
        HistoryEntry(
            id="e1",
            action=audit.CASE_CREATE,
            created_at=NOW,
            actor_id=REPORTER_ID,
            details={"calendar_version": "provisional-1"},
        ),
        HistoryEntry(
            id="e2",
            action=audit.CASE_REVIEW,
            created_at=NOW,
            actor_id=REVIEWER_ID,
            details={"outcome": "pass"},
        ),
    ]
    monkeypatch.setattr(
        api,
        "case_history",
        lambda case_id, scope, limit, offset, **kwargs: (entries, 2),
    )

    as_role(caller, TRIAGER_ID, "triager")
    response = client.get(f"/api/v1/cases/{CASE_ID}/history")
    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["actor_id"] is None
    assert items[0]["details"] == {"calendar_version": "provisional-1"}
    assert items[1]["actor_id"] == REVIEWER_ID
    assert REPORTER_ID not in response.text


def test_contact_needs_the_reporter_contact_permission(client, caller, monkeypatch):
    monkeypatch.setattr(
        api,
        "read_reporter_contact",
        lambda *args, **kwargs: pytest.fail("must not reach the service"),
    )
    # The reporter's own role has no reporter_contact:read, and neither does the triager.
    assert client.get(f"/api/v1/reports/{CASE_ID}/contact").status_code == 403
    as_role(caller, TRIAGER_ID, "triager")
    assert client.get(f"/api/v1/reports/{CASE_ID}/contact").status_code == 403


def test_contact_is_returned_to_a_holder(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(
        auth, "permissions_for_role", lambda role: frozenset({"reporter_contact:read"})
    )
    seen = {}

    def fake_read(case_id, scope):
        seen["scope"] = scope
        return ContactOutcome.OK, "reach me at the desk"

    monkeypatch.setattr(api, "read_reporter_contact", fake_read)
    response = client.get(f"/api/v1/reports/{CASE_ID}/contact")
    assert response.status_code == 200, response.text
    assert response.json() == {"case_id": CASE_ID, "contact": "reach me at the desk"}
    assert (
        seen["scope"].reporter_contact is True and seen["scope"].user_id == TRIAGER_ID
    )


@pytest.mark.parametrize(
    "outcome, detail",
    [
        (ContactOutcome.NOT_FOUND, "case not found"),
        (ContactOutcome.NO_REPORTER, "reporter contact not found"),
    ],
)
def test_contact_refusals_are_404(client, caller, monkeypatch, outcome, detail):
    monkeypatch.setattr(
        auth, "permissions_for_role", lambda role: frozenset({"reporter_contact:read"})
    )
    monkeypatch.setattr(
        api, "read_reporter_contact", lambda case_id, scope: (outcome, None)
    )
    response = client.get(f"/api/v1/reports/{CASE_ID}/contact")
    assert response.status_code == 404
    assert response.json()["detail"] == detail


def test_contact_refused_after_the_window_is_409_with_a_stable_code(
    client, caller, monkeypatch
):
    monkeypatch.setattr(
        auth, "permissions_for_role", lambda role: frozenset({"reporter_contact:read"})
    )
    monkeypatch.setattr(
        api,
        "read_reporter_contact",
        lambda case_id, scope: (ContactOutcome.WINDOW_CLOSED, None),
    )
    response = client.get(f"/api/v1/reports/{CASE_ID}/contact")
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "reporter_contact_window_closed"}
    assert "contact" not in response.json()


# --- database --------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
    if os.environ.get("SAFE_TEST_DATABASE") != "1":
        yield
        return
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


@pytest.fixture
def db_client():
    yield ActingClient
    api.app.dependency_overrides.clear()


@pytest.fixture
def people():
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))
    return {
        "reporter": new_user("reporter"),
        "triager": new_user("triager"),
        "investigator": new_user("investigator"),
        "other_investigator": new_user("investigator"),
        "reviewer": new_user("reviewer"),
    }


@contextmanager
def role_holds(role: str, permission: str):
    """Grant one permission to a role for the length of the block. The role matrix is restored afterwards."""
    if permission == "reporter_contact:read":
        # The matrix refuses this permission, so the grant is written directly (see the helper's docstring).
        with reporter_contact_granted_directly(role):
            yield
        return
    before = frozenset(list_role_permissions()[role])
    set_role_permissions(role, before | {permission})
    try:
        yield
    finally:
        set_role_permissions(role, before)


def contact_audit_rows(case_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT actor_id::text, target_type, target_id FROM audit_events "
            "WHERE action = %s AND target_id = %s AND NOT is_deleted",
            (REPORTER_CONTACT_READ, case_id),
        ).fetchall()


CONTACT = "reporter contact for database test"


def open_report_via_api(db_client, reporter: str, contact: str | None = CONTACT) -> str:
    response = db_client(reporter).post(
        "/api/v1/reports",
        json={
            "title": "Reporter channel case",
            "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "contact": contact,
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@requires_database
def test_report_stores_the_reporter_and_contact_apart_from_the_case(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    with connect() as conn:
        row = conn.execute(
            "SELECT reporter_id::text, contact FROM reporter_identities WHERE case_id = %s AND NOT is_deleted",
            (case_id,),
        ).fetchone()
    assert row == (people["reporter"], CONTACT)


@requires_database
def test_no_caller_gets_the_reporter_account_or_contact(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    # A triager sees a reporter-channel case only once it is assigned (see test_case_access.py). The reporting owner,
    # who holds reporter_contact:read, makes the assignment.
    with role_holds("triager", "reporter_contact:read"):
        assigned = db_client(people["triager"]).post(
            f"/api/v1/cases/{case_id}/assignments",
            json={"user_id": people["investigator"]},
            headers=idem(),
        )
    assert assigned.status_code == 201, assigned.text
    triager = db_client(people["triager"])
    responses = [
        db_client(people["reporter"]).get(f"/api/v1/cases/{case_id}"),
        db_client(people["reporter"]).get(f"/api/v1/cases/{case_id}/history"),
        triager.get(f"/api/v1/cases/{case_id}"),
        triager.get(f"/api/v1/cases/{case_id}/history?limit=200"),
        triager.get("/api/v1/cases?limit=200"),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        assert people["reporter"] not in response.text
        assert CONTACT not in response.text

    # The creation entry stays in the history, without the account that made it.
    history = triager.get(f"/api/v1/cases/{case_id}/history").json()["items"]
    created = [item for item in history if item["action"] == audit.CASE_CREATE]
    assert len(created) == 1 and created[0]["actor_id"] is None


@requires_database
def test_contact_holder_reads_the_contact_and_the_read_is_audited(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    with role_holds("triager", "reporter_contact:read"):
        response = db_client(people["triager"]).get(
            f"/api/v1/reports/{case_id}/contact"
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"case_id": case_id, "contact": CONTACT}
    assert contact_audit_rows(case_id) == [(people["triager"], "case", case_id)]


@requires_database
def test_contact_read_without_the_permission_is_refused_and_not_audited(
    db_client, people
):
    case_id = open_report_via_api(db_client, people["reporter"])
    for actor in (people["reporter"], people["triager"]):
        response = db_client(actor).get(f"/api/v1/reports/{case_id}/contact")
        assert response.status_code == 403
        assert CONTACT not in response.text
    assert contact_audit_rows(case_id) == []


@requires_database
def test_contact_of_a_case_the_holder_cannot_see_is_404_and_not_audited(
    db_client, people
):
    case_id = open_report_via_api(db_client, people["reporter"])
    # Investigators have read_assigned only. With the permission, an unassigned investigator still cannot see the case.
    with role_holds("investigator", "reporter_contact:read"):
        hidden = db_client(people["investigator"]).get(
            f"/api/v1/reports/{case_id}/contact"
        )
        absent = db_client(people["investigator"]).get(
            f"/api/v1/reports/{uuid.uuid4()}/contact"
        )
    assert hidden.status_code == absent.status_code == 404
    assert without_request_id(hidden) == without_request_id(absent)
    assert CONTACT not in hidden.text
    assert contact_audit_rows(case_id) == []


@requires_database
def test_contact_of_a_case_without_reporter_identity_is_404_and_not_audited(
    db_client, people
):
    # A case opened through the ordinary intake, which has no reporter identity.
    created = db_client(people["reporter"]).post(
        "/api/v1/cases",
        json={
            "title": "Not from the reporter channel",
            "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        },
        headers=idem(),
    )
    assert created.status_code == 201, created.text
    case_id = created.json()["id"]
    with role_holds("triager", "reporter_contact:read"):
        response = db_client(people["triager"]).get(
            f"/api/v1/reports/{case_id}/contact"
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "reporter contact not found"
    assert contact_audit_rows(case_id) == []


@requires_database
def test_contact_holder_sees_the_contact_of_a_reporter_case_before_assignment(
    db_client, people
):
    case_id = open_report_via_api(db_client, people["reporter"])
    with role_holds("triager", "reporter_contact:read"):
        response = db_client(people["triager"]).get(
            f"/api/v1/reports/{case_id}/contact"
        )
    assert response.status_code == 200 and response.json()["contact"] == CONTACT


@requires_database
def test_contact_of_a_report_without_contact_is_null_and_still_audited(
    db_client, people
):
    case_id = open_report_via_api(db_client, people["reporter"], contact=None)
    with role_holds("triager", "reporter_contact:read"):
        response = db_client(people["triager"]).get(
            f"/api/v1/reports/{case_id}/contact"
        )
    assert response.status_code == 200
    assert response.json()["contact"] is None
    assert len(contact_audit_rows(case_id)) == 1


def close_reporter_case(db_client, people, case_id: str) -> None:
    """Close a reporter-channel case through the API: assign a reviewer, review it, finish every task, then close.

    The first assignment needs reporter_contact:read, because the case has no assignee yet.
    """
    triager = db_client(people["triager"])
    with role_holds("triager", "reporter_contact:read"):
        assigned = triager.post(
            f"/api/v1/cases/{case_id}/assignments",
            json={"user_id": people["reviewer"]},
            headers=idem(),
        )
    assert assigned.status_code == 201, assigned.text
    opened = db_client(people["reviewer"]).post(
        "/api/v1/reviews", headers=idem(), json={"case_id": case_id}
    )
    assert opened.status_code == 201, opened.text
    reviewed = db_client(people["reviewer"]).patch(
        f"/api/v1/reviews/{opened.json()['id']}/layers/model",
        json={"outcome": "fail", "reason": "Reviewed for this test"},
        headers={**idem(), "If-Match": f'"{opened.json()["revision"]}"'},
    )
    assert reviewed.status_code == 200, reviewed.text
    body = triager.get(f"/api/v1/cases/{case_id}").json()
    for task in body["deadlines"]:
        patched = triager.patch(
            f"/api/v1/cases/{case_id}/deadlines/{task['id']}",
            json={"status": "done"},
            headers=idem(),
        )
        assert patched.status_code == 200, patched.text
    closed = triager.post(
        f"/api/v1/cases/{case_id}/close",
        json={"reason": "Closed for the contact window test"},
        headers=idem(),
    )
    assert closed.status_code == 200, closed.text


def backdate_closure(case_id: str, hours: int, closure: int = -1) -> None:
    """Move one closure of the case back by the given hours in the database. closure indexes the closures in
    time order: -1 is the latest, 0 the first. Only the timestamp logic of the read window needs this."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id::text FROM case_status_changes WHERE case_id = %s AND action = 'closed' AND NOT is_deleted "
            "ORDER BY changed_at",
            (case_id,),
        ).fetchall()
        conn.execute(
            "UPDATE case_status_changes SET changed_at = now() - make_interval(hours => %s) WHERE id = %s",
            (hours, rows[closure][0]),
        )


def read_contact_as_holder(db_client, people, case_id: str):
    with role_holds("triager", "reporter_contact:read"):
        return db_client(people["triager"]).get(f"/api/v1/reports/{case_id}/contact")


@requires_database
def test_contact_is_readable_while_the_case_is_open(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    response = read_contact_as_holder(db_client, people, case_id)
    assert response.status_code == 200, response.text
    assert response.json()["contact"] == CONTACT
    assert len(contact_audit_rows(case_id)) == 1


@requires_database
def test_contact_is_readable_71_hours_after_closure(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    close_reporter_case(db_client, people, case_id)
    backdate_closure(case_id, hours=71)
    response = read_contact_as_holder(db_client, people, case_id)
    assert response.status_code == 200, response.text
    assert response.json()["contact"] == CONTACT
    assert len(contact_audit_rows(case_id)) == 1


@requires_database
def test_contact_is_refused_73_hours_after_closure_and_writes_no_audit_row(
    db_client, people
):
    case_id = open_report_via_api(db_client, people["reporter"])
    close_reporter_case(db_client, people, case_id)
    backdate_closure(case_id, hours=73)
    response = read_contact_as_holder(db_client, people, case_id)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"status": "reporter_contact_window_closed"}
    assert CONTACT not in response.text
    assert contact_audit_rows(case_id) == []


@requires_database
def test_reopened_case_is_readable_again_and_the_latest_closure_counts(
    db_client, people
):
    case_id = open_report_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    close_reporter_case(db_client, people, case_id)
    backdate_closure(case_id, hours=73)
    assert read_contact_as_holder(db_client, people, case_id).status_code == 409

    reopened = triager.post(
        f"/api/v1/cases/{case_id}/reopen",
        json={"reason": "New facts found"},
        headers=idem(),
    )
    assert reopened.status_code == 200, reopened.text
    response = read_contact_as_holder(db_client, people, case_id)
    assert response.status_code == 200, response.text
    assert response.json()["contact"] == CONTACT
    assert len(contact_audit_rows(case_id)) == 1

    # A second closure starts a new window. The first closure is 73 hours old, but the latest one is new.
    closed_again = triager.post(
        f"/api/v1/cases/{case_id}/close",
        json={"reason": "Closed again"},
        headers=idem(),
    )
    assert closed_again.status_code == 200, closed_again.text
    backdate_closure(case_id, hours=73, closure=0)
    response = read_contact_as_holder(db_client, people, case_id)
    assert response.status_code == 200, response.text
    assert len(contact_audit_rows(case_id)) == 2
