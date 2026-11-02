"""Case access control, assignment, reviews, closure, reopening, and change history.

Two layers:
- Tests without a database check the pure rules and the HTTP status mapping. The service functions are replaced.
- Tests marked "database" run the whole flow through the API against PostgreSQL. They need SAFE_TEST_DATABASE=1 and
  the SAFE_DB_* settings, with the migrations already applied (see test_database.py). They create rows and never
  remove them, so use a throwaway database.
"""

import json
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, audit, auth, case_import, reviews_api, system_timezone, users
from safe_kit.cases import (
    Assignment,
    Case,
    CaseScope,
    HistoryEntry,
    Outcome,
    closure_blockers,
    redact_history,
)
from safe_kit.db import connect
from safe_kit.reviews import RoundOutcome
from test_case_records_db import reporter_contact_granted_directly
from safe_kit.rbac import ADMIN_REQUIRED_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS, list_role_permissions, set_role_permissions
from safe_kit.users import User


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}

REPORTER_ID = "11111111-1111-1111-1111-111111111111"
TRIAGER_ID = "22222222-2222-2222-2222-222222222222"
INVESTIGATOR_ID = "33333333-3333-3333-3333-333333333333"
REVIEWER_ID = "44444444-4444-4444-4444-444444444444"
ADMIN_ID = "55555555-5555-5555-5555-555555555555"
AUDITOR_ID = "66666666-6666-6666-6666-666666666666"
ASSIGNEE_ID = "77777777-7777-7777-7777-777777777777"
CASE_ID = "99999999-9999-9999-9999-999999999999"
TASK_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Case creation needs a system timezone. Set one on a freshly migrated test database."""
    if os.environ.get("SAFE_TEST_DATABASE") != "1":
        yield
        return
    from safe_kit.migrate import main as migrate

    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute("SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)").fetchone()[0]
        if not exists:
            conn.execute("INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')")
    yield


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


# --- rules without a database ----------------------------------------------------------------------------------


def test_closure_needs_at_least_one_review():
    assert closure_blockers(0, [("affected_customers", "done")]) == ["no review is recorded"]


def test_closure_is_blocked_by_open_deadline_and_open_weekly_update():
    blockers = closure_blockers(1, [("affected_customers", "open"), ("weekly_update", "open")])
    assert blockers == ["deadline task affected_customers is open", "deadline task weekly_update is open"]


def test_done_and_not_applicable_tasks_do_not_block_closure():
    assert closure_blockers(1, [("affected_customers", "done"), ("broader_advisory", "not_applicable")]) == []


def test_reporter_scope_sees_only_own_cases():
    sql, params = CaseScope.from_permissions(REPORTER_ID, frozenset({"cases:read_own"})).visibility_sql()
    assert sql == "(cases.created_by = %s)"
    assert params == (REPORTER_ID,)


def test_triager_scope_sees_every_case_except_unassigned_reporter_cases():
    sql, params = CaseScope.from_permissions(TRIAGER_ID, DEFAULT_ROLE_PERMISSIONS["triager"]).visibility_sql()
    assert sql == (
        "(NOT EXISTS (SELECT 1 FROM reporter_identities ri WHERE ri.case_id = cases.id AND NOT ri.is_deleted) "
        "OR EXISTS (SELECT 1 FROM case_assignments ca WHERE ca.case_id = cases.id AND NOT ca.is_deleted))"
    )
    assert params == ()


def test_reporter_contact_holder_sees_every_case_including_unassigned_reporter_cases():
    scope = CaseScope.from_permissions(TRIAGER_ID, DEFAULT_ROLE_PERMISSIONS["triager"] | {"reporter_contact:read"})
    assert scope.reporter_contact is True
    assert scope.visibility_sql() == ("TRUE", ())


def test_reporter_contact_permission_is_granted_by_default_to_reporting_owner_only():
    holders = {role for role, permissions in DEFAULT_ROLE_PERMISSIONS.items() if "reporter_contact:read" in permissions}
    assert holders == {"reporting_owner"}


def test_assignee_scope_needs_an_active_assignment():
    sql, params = CaseScope.from_permissions(INVESTIGATOR_ID, DEFAULT_ROLE_PERMISSIONS["investigator"]).visibility_sql()
    assert "case_assignments" in sql and "NOT ca.is_deleted" in sql
    assert params == (INVESTIGATOR_ID,)


@pytest.mark.parametrize("role", ["admin", "auditor", "disclosure_approver"])
def test_roles_without_case_permissions_see_no_case(role):
    scope = CaseScope.from_permissions(ADMIN_ID, DEFAULT_ROLE_PERMISSIONS[role])
    assert scope.visibility_sql() == ("FALSE", ())


def test_history_redaction_keeps_only_action_and_timestamp():
    entry = HistoryEntry(
        id="e1",
        action=audit.CASE_CLOSE,
        created_at=NOW,
        actor_id=TRIAGER_ID,
        details={"status": {"old": "open", "new": "closed"}},
    )
    [redacted] = redact_history([entry])
    assert (redacted.id, redacted.action, redacted.created_at) == ("e1", audit.CASE_CLOSE, NOW)
    assert redacted.actor_id is None
    assert redacted.details is None


def test_case_roles_in_the_default_seed():
    triager = DEFAULT_ROLE_PERMISSIONS["triager"]
    assert {"cases:read_all", "cases:assign", "cases:close", "cases:triage"} <= triager
    assert "cases:read_all" not in DEFAULT_ROLE_PERMISSIONS["reporter"]
    assert "cases:read_all" not in DEFAULT_ROLE_PERMISSIONS["investigator"]
    assert not {"cases:assign", "cases:close"} & DEFAULT_ROLE_PERMISSIONS["investigator"]
    assert not {"cases:assign", "cases:close"} & DEFAULT_ROLE_PERMISSIONS["reviewer"]
    # Assignability is its own permission: investigator and reviewer hold it, the triager (who assigns) does not.
    assert "cases:assignable" in DEFAULT_ROLE_PERMISSIONS["investigator"]
    assert "cases:assignable" in DEFAULT_ROLE_PERMISSIONS["reviewer"]
    assert "cases:assignable" not in triager
    assert "cases:assignable" not in DEFAULT_ROLE_PERMISSIONS["reporter"]


def test_admin_still_has_no_case_permissions_and_keeps_its_required_ones():
    admin = DEFAULT_ROLE_PERMISSIONS["admin"]
    assert not any(permission.startswith("cases:") for permission in admin)
    assert ADMIN_REQUIRED_PERMISSIONS <= admin


def test_auditor_keeps_only_audit_read_among_its_permissions():
    assert DEFAULT_ROLE_PERMISSIONS["auditor"] == frozenset({"audit:read"})


# --- HTTP mapping without a database -----------------------------------------------------------------------------


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user(REPORTER_ID, "reporter")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(caller):
    return TestClient(api.app)


def as_role(caller, user_id, role):
    caller["user"] = make_user(user_id, role)


def sample_case(**overrides) -> Case:
    values = {
        "id": CASE_ID,
        "reference": "SAFE-2026-000001",
        "title": "Sample",
        "awareness_time": NOW,
        "status": "open",
        "calendar_version": "provisional-1",
        "calendar_timezone": "Europe/Istanbul",
        "created_at": NOW,
        "created_by": REPORTER_ID,
        "deadlines": [],
    }
    values.update(overrides)
    return Case(**values)


def sample_assignment() -> Assignment:
    return Assignment(
        id="a1",
        case_id=CASE_ID,
        user_id=ASSIGNEE_ID,
        username="investigator-one",
        role="investigator",
        assigned_by=TRIAGER_ID,
        assigned_at=NOW,
    )


def test_unknown_or_invisible_case_is_404_on_every_read(client, caller, monkeypatch):
    monkeypatch.setattr(api, "find_visible_case", lambda case_id, scope: None)
    monkeypatch.setattr(api, "list_assignments", lambda case_id, scope: None)
    monkeypatch.setattr(api, "case_history", lambda case_id, scope, limit, offset, **kwargs: None)
    assert client.get(f"/api/v1/cases/{CASE_ID}").status_code == 404
    assert client.get(f"/api/v1/cases/{CASE_ID}/assignments").status_code == 404
    assert client.get(f"/api/v1/cases/{CASE_ID}/history").status_code == 404


def test_list_is_built_from_the_callers_scope(client, caller, monkeypatch):
    seen = {}

    def fake_list(scope, limit, offset, q=None, **kwargs):
        seen["scope"] = scope
        return []

    monkeypatch.setattr(api, "list_visible_cases", fake_list)
    monkeypatch.setattr(api, "count_visible_cases", lambda scope, q=None, **kwargs: 0)
    as_role(caller, TRIAGER_ID, "triager")
    assert client.get("/api/v1/cases").status_code == 200
    assert seen["scope"] == CaseScope(user_id=TRIAGER_ID, read_all=True, read_assigned=True)


@pytest.mark.parametrize("role", ["admin", "auditor", "disclosure_approver"])
def test_roles_without_case_permissions_get_403_on_every_case_endpoint(client, caller, role):
    as_role(caller, ADMIN_ID, role)
    assert client.get("/api/v1/cases").status_code == 403
    assert client.get(f"/api/v1/cases/{CASE_ID}").status_code == 403
    assert client.get(f"/api/v1/cases/{CASE_ID}/assignments").status_code == 403
    assert client.get(f"/api/v1/cases/{CASE_ID}/history").status_code == 403
    assert client.post(f"/api/v1/cases/{CASE_ID}/assignments", json={"user_id": ASSIGNEE_ID}).status_code == 403
    assert client.post(f"/api/v1/cases/{CASE_ID}/close", json={"reason": "done"}).status_code == 403
    assert client.post("/api/v1/reviews", headers=idem(), json={"case_id": CASE_ID}).status_code == 403


def test_reporter_cannot_assign_review_close_or_change_deadlines(client, caller):
    assert client.post(f"/api/v1/cases/{CASE_ID}/assignments", json={"user_id": ASSIGNEE_ID}).status_code == 403
    assert client.post("/api/v1/reviews", headers=idem(), json={"case_id": CASE_ID}).status_code == 403
    assert client.post(f"/api/v1/cases/{CASE_ID}/close", json={"reason": "done"}).status_code == 403
    response = client.patch(
        f"/api/v1/cases/{CASE_ID}/deadlines/{TASK_ID}", json={"status": "done"}
    )
    assert response.status_code == 403


def test_triager_cannot_open_a_review_round_or_set_a_layer(client, caller):
    as_role(caller, TRIAGER_ID, "triager")
    assert client.post("/api/v1/reviews", headers=idem(), json={"case_id": CASE_ID}).status_code == 403
    round_id = str(uuid.uuid4())
    body = {"outcome": "pass", "expected_revision": 1}
    assert client.patch(f"/api/v1/reviews/{round_id}/layers/model", json=body).status_code == 403


@pytest.mark.parametrize(
    ("outcome", "status", "detail"),
    [
        (Outcome.NOT_FOUND, 404, "case not found"),
        (Outcome.CLOSED, 409, "case is closed"),
        (Outcome.ASSIGNEE_INACTIVE, 422, "assignee is not an active user"),
        (Outcome.ROLE_NOT_ASSIGNABLE, 422, "assignee must be an investigator or reviewer"),
        (Outcome.DUPLICATE, 409, "user is already assigned to this case"),
    ],
)
def test_assignment_refusals(client, caller, monkeypatch, outcome, status, detail):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(api, "assign_member", lambda case_id, user_id, scope: (outcome, None))
    response = client.post(f"/api/v1/cases/{CASE_ID}/assignments", json={"user_id": ASSIGNEE_ID})
    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_assignment_created_returns_201_with_the_assignment(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(api, "assign_member", lambda case_id, user_id, scope: (Outcome.OK, sample_assignment()))
    response = client.post(f"/api/v1/cases/{CASE_ID}/assignments", json={"user_id": ASSIGNEE_ID})
    assert response.status_code == 201
    assert response.json()["user_id"] == ASSIGNEE_ID
    assert response.json()["role"] == "investigator"


def test_assignee_must_be_a_user_id(client, caller):
    as_role(caller, TRIAGER_ID, "triager")
    assert client.post(f"/api/v1/cases/{CASE_ID}/assignments", json={"user_id": "not-a-uuid"}).status_code == 422


@pytest.mark.parametrize(
    ("outcome", "status", "detail"),
    [
        (Outcome.NOT_FOUND, 404, "case not found"),
        (Outcome.CLOSED, 409, "case is closed"),
        (Outcome.ASSIGNMENT_NOT_FOUND, 404, "assignment not found"),
    ],
)
def test_unassign_refusals(client, caller, monkeypatch, outcome, status, detail):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(api, "unassign_member", lambda case_id, user_id, scope: outcome)
    response = client.delete(f"/api/v1/cases/{CASE_ID}/assignments/{ASSIGNEE_ID}")
    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_unassign_success_is_204(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(api, "unassign_member", lambda case_id, user_id, scope: Outcome.OK)
    assert client.delete(f"/api/v1/cases/{CASE_ID}/assignments/{ASSIGNEE_ID}").status_code == 204


def test_layer_outcome_must_be_a_known_value(client, caller):
    as_role(caller, REVIEWER_ID, "reviewer")
    round_id = str(uuid.uuid4())
    body = {"outcome": "maybe", "expected_revision": 1}
    assert client.patch(f"/api/v1/reviews/{round_id}/layers/model", json=body).status_code == 422


def test_layer_reason_is_limited_to_1000_characters(client, caller):
    as_role(caller, REVIEWER_ID, "reviewer")
    round_id = str(uuid.uuid4())
    body = {"outcome": "pass", "reason": "x" * 1001, "expected_revision": 1}
    assert client.patch(f"/api/v1/reviews/{round_id}/layers/model", json=body).status_code == 422


def test_layer_refusals_map_to_their_status_codes(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    round_id = str(uuid.uuid4())
    body = {"outcome": "pass", "expected_revision": 1}
    url = f"/api/v1/reviews/{round_id}/layers/model"

    monkeypatch.setattr(reviews_api, "set_layer", lambda *args: (RoundOutcome.NOT_ASSIGNED, None))
    response = client.patch(url, json=body)
    assert response.status_code == 403
    assert response.json() == {"detail": "only an assigned reviewer can record a review"}

    monkeypatch.setattr(reviews_api, "set_layer", lambda *args: (RoundOutcome.CLOSED, None))
    assert client.patch(url, json=body).json() == {"detail": "case is closed"}

    monkeypatch.setattr(reviews_api, "set_layer", lambda *args: (RoundOutcome.NOT_FOUND, None))
    assert client.patch(url, json=body).json() == {"detail": "review not found"}


def test_layer_blank_reason_is_stored_as_no_reason(client, caller, monkeypatch):
    as_role(caller, REVIEWER_ID, "reviewer")
    seen = {}

    def fake_set_layer(round_id, layer, outcome, reason, evidence_ids, expected_revision, scope):
        seen["reason"] = reason
        return RoundOutcome.NOT_FOUND, None

    monkeypatch.setattr(reviews_api, "set_layer", fake_set_layer)
    client.patch(
        f"/api/v1/reviews/{uuid.uuid4()}/layers/model",
        json={"outcome": "pass", "reason": "   ", "expected_revision": 1},
    )
    assert seen["reason"] is None  # a pass needs no reason, and a blank one is stored as none


def test_closure_refusal_lists_the_blockers(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    blockers = ["no review is recorded", "deadline task weekly_update is open"]
    monkeypatch.setattr(api, "close_case", lambda case_id, reason, scope: (Outcome.BLOCKED, blockers))
    response = client.post(f"/api/v1/cases/{CASE_ID}/close", json={"reason": "done"})
    assert response.status_code == 409
    assert response.json() == {
        "detail": "case cannot be closed: no review is recorded; deadline task weekly_update is open"
    }


def test_closing_an_already_closed_case_is_409_case_is_closed(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(api, "close_case", lambda case_id, reason, scope: (Outcome.CLOSED, []))
    assert client.post(f"/api/v1/cases/{CASE_ID}/close", json={"reason": "again"}).json() == {"detail": "case is closed"}


@pytest.mark.parametrize("reason", ["", "   ", "x" * 1001])
def test_close_reason_must_be_1_to_1000_characters(client, caller, reason):
    as_role(caller, TRIAGER_ID, "triager")
    assert client.post(f"/api/v1/cases/{CASE_ID}/close", json={"reason": reason}).status_code == 422


def test_close_success_returns_the_closed_case_and_strips_the_reason(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    seen = {}

    def fake_close(case_id, reason, scope):
        seen["reason"] = reason
        return Outcome.OK, []

    closed = sample_case(status="closed", closed_at=NOW, closed_by=TRIAGER_ID, close_reason="Confirmed")
    monkeypatch.setattr(api, "close_case", fake_close)
    monkeypatch.setattr(api, "find_visible_case", lambda case_id, scope: closed)
    response = client.post(f"/api/v1/cases/{CASE_ID}/close", json={"reason": "  Confirmed  "})
    assert response.status_code == 200
    assert seen["reason"] == "Confirmed"
    assert response.json()["status"] == "closed"


def test_reopen_refusal_when_the_case_is_open(client, caller, monkeypatch):
    as_role(caller, TRIAGER_ID, "triager")
    monkeypatch.setattr(api, "reopen_case", lambda case_id, reason, scope: Outcome.NOT_CLOSED)
    response = client.post(f"/api/v1/cases/{CASE_ID}/reopen", json={"reason": "new facts"})
    assert response.status_code == 409
    assert response.json() == {"detail": "case is not closed"}


def test_deadline_update_bodies_are_validated(client, caller):
    as_role(caller, TRIAGER_ID, "triager")
    url = f"/api/v1/cases/{CASE_ID}/deadlines/{TASK_ID}"
    assert client.patch(url, json={"status": "not_applicable"}).status_code == 422
    assert client.patch(url, json={"status": "not_applicable", "reason": "  "}).status_code == 422
    assert client.patch(url, json={"status": "done", "reason": "not needed"}).status_code == 422
    assert client.patch(url, json={"status": "archived"}).status_code == 422


def test_deadline_update_passes_the_triage_permission_to_the_service(client, caller, monkeypatch):
    seen = {}

    def fake_update(case_id, task_id, status, reason, scope, may_mark_not_applicable):
        seen.update(status=status, reason=reason, may=may_mark_not_applicable)
        return Outcome.OK

    monkeypatch.setattr(api, "update_deadline", fake_update)
    monkeypatch.setattr(api, "find_visible_case", lambda case_id, scope: sample_case())

    as_role(caller, TRIAGER_ID, "triager")
    response = client.patch(
        f"/api/v1/cases/{CASE_ID}/deadlines/{TASK_ID}", json={"status": "not_applicable", "reason": " out of scope "}
    )
    assert response.status_code == 200
    assert seen == {"status": "not_applicable", "reason": "out of scope", "may": True}

    as_role(caller, INVESTIGATOR_ID, "investigator")
    client.patch(f"/api/v1/cases/{CASE_ID}/deadlines/{TASK_ID}", json={"status": "done"})
    assert seen["may"] is False


@pytest.mark.parametrize(
    ("outcome", "status", "detail"),
    [
        (Outcome.NOT_FOUND, 404, "case not found"),
        (Outcome.CLOSED, 409, "case is closed"),
        (Outcome.TASK_NOT_FOUND, 404, "deadline task not found"),
        (Outcome.NOT_APPLICABLE_FORBIDDEN, 403, "only a triager may mark a deadline not applicable"),
    ],
)
def test_deadline_update_refusals(client, caller, monkeypatch, outcome, status, detail):
    as_role(caller, INVESTIGATOR_ID, "investigator")
    monkeypatch.setattr(api, "update_deadline", lambda *args, **kwargs: outcome)
    response = client.patch(f"/api/v1/cases/{CASE_ID}/deadlines/{TASK_ID}", json={"status": "done"})
    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_history_gives_triager_the_details_and_reporter_only_actions(client, caller, monkeypatch):
    entry = HistoryEntry(
        id="e1",
        action=audit.CASE_CLOSE,
        created_at=NOW,
        actor_id=TRIAGER_ID,
        details={"status": {"old": "open", "new": "closed"}},
    )

    def fake_history(case_id, scope, limit, offset, **kwargs):
        return [entry], 1

    monkeypatch.setattr(api, "case_history", fake_history)

    as_role(caller, TRIAGER_ID, "triager")
    full = client.get(f"/api/v1/cases/{CASE_ID}/history").json()
    assert full["items"][0]["details"] == {"status": {"old": "open", "new": "closed"}}
    assert full["items"][0]["actor_id"] == TRIAGER_ID

    as_role(caller, REPORTER_ID, "reporter")
    stripped = client.get(f"/api/v1/cases/{CASE_ID}/history").json()
    assert stripped["total"] == 1
    assert stripped["items"][0]["action"] == audit.CASE_CLOSE
    assert stripped["items"][0]["details"] is None
    assert stripped["items"][0]["actor_id"] is None


def test_history_leaves_out_evidence_entries_unless_the_caller_holds_evidence_read(client, caller, monkeypatch):
    seen = []

    def fake_history(case_id, scope, limit, offset, **kwargs):
        seen.append(kwargs["include_evidence"])
        return [], 0

    monkeypatch.setattr(api, "case_history", fake_history)
    as_role(caller, REPORTER_ID, "reporter")
    assert client.get(f"/api/v1/cases/{CASE_ID}/history").status_code == 200
    as_role(caller, INVESTIGATOR_ID, "investigator")
    assert client.get(f"/api/v1/cases/{CASE_ID}/history").status_code == 200
    assert seen == [False, True]


def test_history_is_paged_with_the_standard_bounds(client, caller):
    assert client.get(f"/api/v1/cases/{CASE_ID}/history?limit=0").status_code == 422
    assert client.get(f"/api/v1/cases/{CASE_ID}/history?offset=-1").status_code == 422


# --- database: the same flow against PostgreSQL ------------------------------------------------------------------

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


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

    def delete(self, url, **kwargs):
        return self._request("delete", url, **kwargs)


@pytest.fixture
def db_client():
    yield ActingClient
    api.app.dependency_overrides.clear()


@pytest.fixture
def timezone_set():
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))


def create_case_via_api(db_client, reporter: str) -> str:
    response = db_client(reporter).post(
        "/api/v1/cases",
        json={
            "title": "Database case title",
            "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def get_case(db_client, actor: str, case_id: str):
    return db_client(actor).get(f"/api/v1/cases/{case_id}")


def scope_of(user_id: str, role: str) -> CaseScope:
    return CaseScope.from_permissions(user_id, DEFAULT_ROLE_PERMISSIONS[role])


def task_ids(db_client, triager: str, case_id: str) -> dict[str, str]:
    body = db_client(triager).get(f"/api/v1/cases/{case_id}").json()
    return {task["kind"]: task["id"] for task in body["deadlines"]}


@pytest.fixture
def people(timezone_set):
    return {
        "reporter": new_user("reporter"),
        "other_reporter": new_user("reporter"),
        "triager": new_user("triager"),
        "investigator": new_user("investigator"),
        "other_investigator": new_user("investigator"),
        "reviewer": new_user("reviewer"),
        "admin": new_user("admin"),
        "auditor": new_user("auditor"),
    }


@requires_database
def test_reporter_sees_own_case_and_not_another_reporters(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assert get_case(db_client, people["reporter"], case_id).status_code == 200
    other = get_case(db_client, people["other_reporter"], case_id)
    missing = get_case(db_client, people["other_reporter"], str(uuid.uuid4()))
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()
    listed = db_client(people["reporter"]).get("/api/v1/cases?limit=200").json()
    assert case_id in {item["id"] for item in listed["items"]}
    other_list = db_client(people["other_reporter"]).get("/api/v1/cases?limit=200").json()
    assert case_id not in {item["id"] for item in other_list["items"]}


@requires_database
def test_triager_sees_every_case(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    assert get_case(db_client, people["triager"], case_id).status_code == 200
    listed = db_client(people["triager"]).get("/api/v1/cases?limit=200").json()
    assert case_id in {item["id"] for item in listed["items"]}


@requires_database
def test_assigned_investigator_sees_only_assigned_cases(db_client, people):
    first = create_case_via_api(db_client, people["reporter"])
    second = create_case_via_api(db_client, people["reporter"])
    response = db_client(people["triager"]).post(
        f"/api/v1/cases/{first}/assignments", json={"user_id": people["investigator"]}
    )
    assert response.status_code == 201

    assert get_case(db_client, people["investigator"], first).status_code == 200
    assert get_case(db_client, people["investigator"], second).status_code == 404
    assert get_case(db_client, people["other_investigator"], first).status_code == 404
    listed = {item["id"] for item in db_client(people["investigator"]).get("/api/v1/cases?limit=200").json()["items"]}
    assert first in listed and second not in listed


@requires_database
def test_assignment_rules_roles_duplicates_and_soft_delete(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    url = f"/api/v1/cases/{case_id}/assignments"

    # The triager holds cases:assign (it makes assignments) but not cases:assignable, so it is refused as an assignee.
    for wrong in ("reporter", "auditor", "admin", "triager"):
        response = triager.post(url, json={"user_id": people[wrong]})
        assert response.status_code == 422
        assert response.json() == {"detail": "assignee must be an investigator or reviewer"}

    assert triager.post(url, json={"user_id": people["investigator"]}).status_code == 201
    assert triager.post(url, json={"user_id": people["reviewer"]}).status_code == 201
    assert triager.post(url, json={"user_id": people["other_investigator"]}).status_code == 201
    duplicate = triager.post(url, json={"user_id": people["investigator"]})
    assert duplicate.status_code == 409
    assert len(triager.get(url).json()) == 3

    removed = triager.delete(f"{url}/{people['investigator']}")
    assert removed.status_code == 204
    assert triager.delete(f"{url}/{people['investigator']}").status_code == 404
    with connect() as conn:
        rows = conn.execute(
            "SELECT is_deleted, deleted_at IS NOT NULL FROM case_assignments WHERE case_id = %s AND user_id = %s",
            (case_id, people["investigator"]),
        ).fetchall()
    assert rows == [(True, True)]  # the row is kept, soft deleted
    assert len(triager.get(url).json()) == 2
    assert triager.post(url, json={"user_id": people["investigator"]}).status_code == 201  # assignable again

    with connect() as conn:
        actions = [
            row[0]
            for row in conn.execute(
                "SELECT action FROM audit_events WHERE target_type = 'case' AND target_id = %s ORDER BY created_at, id",
                (case_id,),
            ).fetchall()
        ]
    assert actions.count(audit.CASE_ASSIGNMENT_ADD) == 4
    assert actions.count(audit.CASE_ASSIGNMENT_REMOVE) == 1


@requires_database
def test_inactive_and_deleted_users_cannot_be_assigned(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    url = f"/api/v1/cases/{case_id}/assignments"
    disabled = new_user("investigator")
    users.update_user(disabled, is_active=False, actor_id=people["admin"])
    deleted = new_user("investigator")
    users.soft_delete_user(deleted, actor_id=people["admin"])
    for user_id in (disabled, deleted):
        response = triager.post(url, json={"user_id": user_id})
        assert response.status_code == 422
        assert response.json() == {"detail": "assignee is not an active user"}


def open_and_set_review(client, case_id: str, outcome: str = "pass", reason: str | None = None):
    """Open a review round and set its model layer. Closing a case needs at least one layer outcome."""
    opened = client.post("/api/v1/reviews", headers=idem(), json={"case_id": case_id})
    assert opened.status_code == 201, opened.text
    review = opened.json()
    body = {"outcome": outcome, "expected_revision": review["revision"]}
    if reason is not None:
        body["reason"] = reason
    return client.patch(f"/api/v1/reviews/{review['id']}/layers/model", json=body)


@requires_database
def test_review_needs_an_assignment_and_the_reviewer_role(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    body = {"case_id": case_id}
    # Not assigned: the reviewer cannot see the case, so the answer is not found.
    assert db_client(people["reviewer"]).post("/api/v1/reviews", headers=idem(), json=body).status_code == 404
    # The triager has no reviews:write permission.
    assert db_client(people["triager"]).post("/api/v1/reviews", headers=idem(), json=body).status_code == 403

    db_client(people["triager"]).post(
        f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]}
    )
    assert db_client(people["reviewer"]).post("/api/v1/reviews", headers=idem(), json=body).status_code == 201


@requires_database
def test_close_is_refused_without_review_and_with_open_tasks(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]})
    triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["investigator"]})

    refused = triager.post(f"/api/v1/cases/{case_id}/close", json={"reason": "all done"})
    assert refused.status_code == 409
    assert "no review is recorded" in refused.json()["detail"]
    assert "deadline task affected_customers is open" in refused.json()["detail"]

    open_and_set_review(db_client(people["reviewer"]), case_id, "pass")
    refused = triager.post(f"/api/v1/cases/{case_id}/close", json={"reason": "all done"})
    assert refused.status_code == 409
    assert "no review" not in refused.json()["detail"]
    assert "deadline task affected_customers is open" in refused.json()["detail"]


@requires_database
def test_close_is_refused_with_an_open_weekly_update(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]})
    open_and_set_review(db_client(people["reviewer"]), case_id, "pass")
    tasks = task_ids(db_client, people["triager"], case_id)
    for kind, task_id in tasks.items():
        response = triager.patch(f"/api/v1/cases/{case_id}/deadlines/{task_id}", json={"status": "done"})
        assert response.status_code == 200, (kind, response.text)
    with connect() as conn:
        conn.execute(
            "INSERT INTO deadline_tasks (case_id, kind, due_at, status) VALUES (%s, 'weekly_update', now(), 'open')",
            (case_id,),
        )
    refused = triager.post(f"/api/v1/cases/{case_id}/close", json={"reason": "all done"})
    assert refused.status_code == 409
    assert refused.json() == {"detail": "case cannot be closed: deadline task weekly_update is open"}


@requires_database
def test_close_succeeds_after_review_and_tasks_done_or_not_applicable(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    investigator = db_client(people["investigator"])
    triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]})
    triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["investigator"]})
    open_and_set_review(db_client(people["reviewer"]), case_id, "not_applicable", reason="Not applicable to this case")

    tasks = task_ids(db_client, people["triager"], case_id)
    # An assigned investigator may mark tasks done, but not not_applicable. The conditional advisory starts not
    # applicable, and only a triager may change that, so it is left as it is and closure accepts it.
    for kind in ("affected_customers", "preliminary_safe_report", "preliminary_public_report"):
        assert investigator.patch(
            f"/api/v1/cases/{case_id}/deadlines/{tasks[kind]}", json={"status": "done"}
        ).status_code == 200
    forbidden = investigator.patch(
        f"/api/v1/cases/{case_id}/deadlines/{tasks['remediation_status']}",
        json={"status": "not_applicable", "reason": "not in scope"},
    )
    assert forbidden.status_code == 403
    applied = triager.patch(
        f"/api/v1/cases/{case_id}/deadlines/{tasks['remediation_status']}",
        json={"status": "not_applicable", "reason": "  Remediation tracked elsewhere  "},
    )
    assert applied.status_code == 200
    statuses = {t["kind"]: (t["status"], t["not_applicable_reason"]) for t in applied.json()["deadlines"]}
    assert statuses["remediation_status"] == ("not_applicable", "Remediation tracked elsewhere")

    closed = triager.post(f"/api/v1/cases/{case_id}/close", json={"reason": "Closed after review"})
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["status"] == "closed"
    assert body["closed_by"] == people["triager"]
    assert body["closed_at"] is not None


@requires_database
def test_closed_case_refuses_changes_and_reopen_is_audited(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]})
    open_and_set_review(db_client(people["reviewer"]), case_id, "pass")
    tasks = task_ids(db_client, people["triager"], case_id)
    for kind, task_id in tasks.items():
        triager.patch(
            f"/api/v1/cases/{case_id}/deadlines/{task_id}",
            json={"status": "not_applicable", "reason": f"{kind} is not applicable here"},
        )
    assert triager.post(f"/api/v1/cases/{case_id}/close", json={"reason": "Closed"}).status_code == 200

    new_investigator = new_user("investigator")
    url = f"/api/v1/cases/{case_id}/assignments"
    assert triager.post(url, json={"user_id": new_investigator}).json() == {"detail": "case is closed"}
    assert triager.delete(f"{url}/{people['reviewer']}").json() == {"detail": "case is closed"}
    assert db_client(people["reviewer"]).post("/api/v1/reviews", headers=idem(), json={"case_id": case_id}).json() == {
        "detail": "case is closed"
    }
    first_task = next(iter(tasks.values()))
    assert triager.patch(
        f"/api/v1/cases/{case_id}/deadlines/{first_task}", json={"status": "open"}
    ).json() == {"detail": "case is closed"}
    assert triager.post(f"/api/v1/cases/{case_id}/close", json={"reason": "again"}).json() == {"detail": "case is closed"}

    assert db_client(people["reporter"]).post(
        f"/api/v1/cases/{case_id}/reopen", json={"reason": "new facts"}
    ).status_code == 403
    reopened = triager.post(f"/api/v1/cases/{case_id}/reopen", json={"reason": "New facts found"})
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"
    assert reopened.json()["closed_at"] is None
    assert reopened.json()["closed_by"] is None
    assert triager.post(f"/api/v1/cases/{case_id}/reopen", json={"reason": "again"}).json() == {
        "detail": "case is not closed"
    }
    assert triager.post(url, json={"user_id": new_investigator}).status_code == 201

    history = triager.get(f"/api/v1/cases/{case_id}/history").json()["items"]
    reopen = next(item for item in history if item["action"] == audit.CASE_REOPEN)
    assert reopen["details"]["status"] == {"old": "closed", "new": "open"}
    assert reopen["details"]["closed_by"]["old"] == people["triager"]


@requires_database
def test_history_shows_details_only_to_the_triager(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]})
    open_and_set_review(db_client(people["reviewer"]), case_id, "fail", reason="Confidential reviewer note")

    full = triager.get(f"/api/v1/cases/{case_id}/history").json()
    stripped = db_client(people["reporter"]).get(f"/api/v1/cases/{case_id}/history").json()

    actions = {item["action"] for item in full["items"]}
    assert {audit.CASE_CREATE, audit.CASE_ASSIGNMENT_ADD, audit.CASE_REVIEW_ROUND_OPENED, audit.CASE_REVIEW_LAYER_SET} <= actions
    assert full["items"][0]["action"] == audit.CASE_REVIEW_LAYER_SET  # newest first
    assert all(item["details"] is not None for item in full["items"])
    assert stripped["total"] == full["total"]
    assert all(item["details"] is None and item["actor_id"] is None for item in stripped["items"])
    assert {item["action"] for item in stripped["items"]} == actions
    # Free text and titles never reach the audit details.
    dumped = json.dumps(full)
    assert "Database case title" not in dumped
    assert "Confidential reviewer note" not in dumped


@requires_database
def test_close_and_reopen_reasons_are_stored_as_text(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]})
    open_and_set_review(db_client(people["reviewer"]), case_id, "pass")
    for kind, task_id in task_ids(db_client, people["triager"], case_id).items():
        triager.patch(
            f"/api/v1/cases/{case_id}/deadlines/{task_id}",
            json={"status": "not_applicable", "reason": f"{kind} is not applicable here"},
        )
    close_reason = "Confirmed root cause in the vendor patch"
    reopen_reason = "New facts found"
    assert triager.get(f"/api/v1/cases/{case_id}").json()["reopen_reason"] is None
    assert triager.post(f"/api/v1/cases/{case_id}/close", json={"reason": close_reason}).status_code == 200
    assert triager.post(f"/api/v1/cases/{case_id}/reopen", json={"reason": reopen_reason}).status_code == 200
    assert triager.get(f"/api/v1/cases/{case_id}").json()["reopen_reason"] == reopen_reason
    # The assigned reviewer can see the case but not the reason texts.
    reviewer_view = db_client(people["reviewer"]).get(f"/api/v1/cases/{case_id}").json()
    assert reviewer_view["close_reason"] is None
    assert reviewer_view["reopen_reason"] is None

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT action, reason, actor_id::text FROM case_status_changes
            WHERE case_id = %s AND NOT is_deleted ORDER BY changed_at
            """,
            (case_id,),
        ).fetchall()
    assert rows == [
        ("closed", close_reason, people["triager"]),
        ("reopened", reopen_reason, people["triager"]),
    ]

    history = triager.get(f"/api/v1/cases/{case_id}/history").json()["items"]
    closed = next(item for item in history if item["action"] == audit.CASE_CLOSE)
    reopened = next(item for item in history if item["action"] == audit.CASE_REOPEN)
    assert closed["details"]["reason"] == close_reason
    assert reopened["details"]["reason"] == reopen_reason
    # The audit row itself still keeps only the length of each reason.
    assert closed["details"]["close_reason"]["new"] == {"length": len(close_reason)}
    assert reopened["details"]["reopen_reason"]["new"] == {"length": len(reopen_reason)}

    stripped = db_client(people["reporter"]).get(f"/api/v1/cases/{case_id}/history").json()["items"]
    assert all(item["details"] is None and item["actor_id"] is None for item in stripped)


@requires_database
def test_evidence_history_entries_need_evidence_read(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    # Evidence rows use target_type "evidence" and name the case in details, as evidence.py writes them.
    with connect() as conn:
        audit.record(
            conn,
            audit.EVIDENCE_UPLOADED,
            "evidence",
            str(uuid.uuid4()),
            actor_id=people["investigator"],
            details={"case_id": case_id},
        )

    with_read = db_client(people["triager"]).get(f"/api/v1/cases/{case_id}/history").json()
    # The reporter can see the case but holds no evidence:read.
    without_read = db_client(people["reporter"]).get(f"/api/v1/cases/{case_id}/history").json()
    assert audit.EVIDENCE_UPLOADED in {item["action"] for item in with_read["items"]}
    assert audit.EVIDENCE_UPLOADED not in {item["action"] for item in without_read["items"]}
    assert without_read["total"] == with_read["total"] - 1


@requires_database
def test_only_triagers_may_mark_a_deadline_not_applicable(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    triager.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["investigator"]})
    task_id = task_ids(db_client, people["triager"], case_id)["broader_advisory"]
    url = f"/api/v1/cases/{case_id}/deadlines/{task_id}"
    assert db_client(people["investigator"]).patch(url, json={"status": "not_applicable", "reason": "x"}).status_code == 403
    assert triager.patch(url, json={"status": "not_applicable"}).status_code == 422
    assert triager.patch(url, json={"status": "not_applicable", "reason": "Not needed"}).status_code == 200
    assert db_client(people["investigator"]).patch(url, json={"status": "open"}).status_code == 403


@requires_database
def test_admin_and_auditor_have_no_case_access(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    for actor in (people["admin"], people["auditor"]):
        client = db_client(actor)
        assert client.get("/api/v1/cases").status_code == 403
        assert client.get(f"/api/v1/cases/{case_id}").status_code == 403
        assert client.get(f"/api/v1/cases/{case_id}/history").status_code == 403
        assert client.post(f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["reviewer"]}).status_code == 403


# --- negative access: a hidden case looks exactly like a missing one -----------------------------------------------

EXAMPLES = Path(__file__).resolve().parents[2] / "schemas" / "examples"


@requires_database
def test_unassigned_investigator_and_reviewer_get_the_same_404_as_a_missing_case(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    missing_id = str(uuid.uuid4())
    for actor in (people["investigator"], people["reviewer"]):
        client = db_client(actor)
        for path in ("", "/assignments", "/history"):
            hidden = client.get(f"/api/v1/cases/{case_id}{path}")
            absent = client.get(f"/api/v1/cases/{missing_id}{path}")
            assert hidden.status_code == absent.status_code == 404
            assert hidden.json() == absent.json()
        listed = {item["id"] for item in client.get("/api/v1/cases?limit=200").json()["items"]}
        assert case_id not in listed


@requires_database
def test_unassigned_reviewer_gets_404_not_403_when_recording_a_review(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    reviewer = db_client(people["reviewer"])
    hidden = reviewer.post("/api/v1/reviews", headers=idem(), json={"case_id": case_id})
    absent = reviewer.post("/api/v1/reviews", headers=idem(), json={"case_id": str(uuid.uuid4())})
    # 403 would say "only an assigned reviewer" and so reveal that the case exists.
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json()


@requires_database
def test_unassigned_investigator_gets_404_on_deadline_change_and_403_without_permission(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    task_id = task_ids(db_client, people["triager"], case_id)["broader_advisory"]
    investigator = db_client(people["investigator"])
    hidden = investigator.patch(f"/api/v1/cases/{case_id}/deadlines/{task_id}", json={"status": "done"})
    absent = investigator.patch(f"/api/v1/cases/{case_id}/deadlines/{uuid.uuid4()}", json={"status": "done"})
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json()
    # Closing needs cases:close, which the investigator does not hold, so the answer is 403 whatever the case.
    assert investigator.post(f"/api/v1/cases/{case_id}/close", json={"reason": "done"}).status_code == 403


@requires_database
def test_reporter_cannot_read_another_reporters_assignments_or_history(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    missing_id = str(uuid.uuid4())
    other = db_client(people["other_reporter"])
    for path in ("/assignments", "/history"):
        hidden = other.get(f"/api/v1/cases/{case_id}{path}")
        absent = other.get(f"/api/v1/cases/{missing_id}{path}")
        assert hidden.status_code == absent.status_code == 404
        assert hidden.json() == absent.json()
    assert db_client(people["reporter"]).get(f"/api/v1/cases/{case_id}/history").status_code == 200


@requires_database
def test_list_filters_cannot_be_used_to_find_another_reporters_title(db_client, people):
    marker = "Confidential " + uuid.uuid4().hex
    created = db_client(people["other_reporter"]).post(
        "/api/v1/cases",
        json={"title": marker, "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
    )
    assert created.status_code == 201
    # The list filters by q only (reference or id prefix; see the q tests below). Other parameters are ignored, so
    # they must not narrow the list either.
    plain = db_client(people["reporter"]).get("/api/v1/cases?limit=200")
    assert plain.status_code == 200
    assert marker not in plain.text
    for query in (f"title={marker}", f"search={marker}", f"status=open&title={marker}"):
        filtered = db_client(people["reporter"]).get(f"/api/v1/cases?limit=200&{query}")
        assert filtered.status_code == 200
        assert marker not in filtered.text
        assert filtered.json() == plain.json()
    # The creator still sees its own title, so the checks above would catch a title shown to the wrong caller.
    assert marker in db_client(people["other_reporter"]).get("/api/v1/cases?limit=200").text


@requires_database
def test_import_of_another_reporters_source_case_does_not_disclose_it(people):
    record = json.loads((EXAMPLES / "valid-minimal.json").read_text(encoding="utf-8"))
    record["case_id"] = str(uuid.uuid4())
    record["timeline"]["awareness_time"]["value"] = "2026-10-30T09:00:00Z"
    [created] = case_import.import_records([record], scope_of(people["other_reporter"], "reporter"))
    assert created.status == "imported"
    [result] = case_import.import_records([record], scope_of(people["reporter"], "reporter"))
    assert result.status == "duplicate"
    assert result.case_id is None
    assert result.reference is None
    # The creator of the case still gets its ids on a repeat, so the check above is not an empty answer.
    [own] = case_import.import_records([record], scope_of(people["other_reporter"], "reporter"))
    assert own.status == "duplicate"
    assert own.case_id == created.case_id and own.reference == created.reference


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


def open_report_via_api(db_client, reporter: str, title: str = "Reporter channel case") -> str:
    response = db_client(reporter).post(
        "/api/v1/reports",
        json={"title": title, "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def listed_ids(db_client, actor: str, q: str | None = None) -> set[str]:
    params = {"limit": 200} | ({"q": q} if q is not None else {})
    response = db_client(actor).get("/api/v1/cases", params=params)
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


# --- reporter-channel cases are hidden from read_all callers until assigned (D2) -----------------------------------


@requires_database
def test_unassigned_reporter_case_is_hidden_from_a_triager(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    triager = db_client(people["triager"])
    # In the list, on the case read, and in the history, the triager gets the same answer as for a missing case.
    assert case_id not in listed_ids(db_client, people["triager"])
    assert triager.get(f"/api/v1/cases/{case_id}").status_code == 404
    assert triager.get(f"/api/v1/cases/{case_id}/history").status_code == 404
    assert triager.get(f"/api/v1/cases/{case_id}/assignments").status_code == 404
    # The reporter still sees its own case, with no assignment.
    assert case_id in listed_ids(db_client, people["reporter"])
    assert get_case(db_client, people["reporter"], case_id).status_code == 200


@requires_database
def test_assigned_reporter_case_is_visible_to_a_triager(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    # The reporting owner assigns it. Assignment needs the case to be visible, so the owner holds reporter_contact:read.
    with role_holds("triager", "reporter_contact:read"):
        response = db_client(people["triager"]).post(
            f"/api/v1/cases/{case_id}/assignments", json={"user_id": people["investigator"]}
        )
    assert response.status_code == 201, response.text
    # The case now has an active assignment, so the triager without the permission sees it too.
    assert case_id in listed_ids(db_client, people["triager"])
    assert get_case(db_client, people["triager"], case_id).status_code == 200
    assert db_client(people["triager"]).get(f"/api/v1/cases/{case_id}/history").status_code == 200


@requires_database
def test_reporter_contact_holder_sees_unassigned_reporter_case(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    assert case_id not in listed_ids(db_client, people["triager"])
    # Only the triager role has read_all, so the holder is a triager that has been granted the permission.
    with role_holds("triager", "reporter_contact:read"):
        assert case_id in listed_ids(db_client, people["triager"])
        assert get_case(db_client, people["triager"], case_id).status_code == 200
        assert db_client(people["triager"]).get(f"/api/v1/cases/{case_id}/history").status_code == 200
        contact = db_client(people["triager"]).get(f"/api/v1/reports/{case_id}/contact")
        assert contact.status_code == 200, contact.text
    # Without the permission again, the case is hidden again.
    assert case_id not in listed_ids(db_client, people["triager"])
    assert get_case(db_client, people["triager"], case_id).status_code == 404


@requires_database
def test_hidden_reporter_case_is_not_counted_for_a_triager(db_client, people):
    before = db_client(people["triager"]).get("/api/v1/cases?limit=1").json()["total"]
    open_report_via_api(db_client, people["reporter"], title="Hidden from triagers")
    after = db_client(people["triager"]).get("/api/v1/cases?limit=1").json()["total"]
    assert after == before


# --- q: case-insensitive match on the reference or the id prefix (F1) ----------------------------------------------


@requires_database
def test_q_matches_reference_partially_and_id_prefix_in_any_case(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    reference = db_client(people["reporter"]).get(f"/api/v1/cases/{case_id}").json()["reference"]
    for query in (reference, reference.lower(), reference[5:12], case_id[:8], case_id[:8].upper()):
        ids = listed_ids(db_client, people["reporter"], query)
        assert case_id in ids, query
    # Every returned case matches q, so nothing unrelated comes through.
    response = db_client(people["reporter"]).get("/api/v1/cases", params={"limit": 200, "q": reference[5:12]})
    for item in response.json()["items"]:
        assert reference[5:12].lower() in item["reference"].lower()


@requires_database
def test_q_is_combined_with_visibility(db_client, people):
    own = create_case_via_api(db_client, people["reporter"])
    own_reference = db_client(people["reporter"]).get(f"/api/v1/cases/{own}").json()["reference"]
    # Another reporter searches for the exact reference and the id of a case it may not see.
    assert own not in listed_ids(db_client, people["other_reporter"], own_reference)
    assert own not in listed_ids(db_client, people["other_reporter"], own[:8])
    other = db_client(people["other_reporter"]).get("/api/v1/cases", params={"limit": 200, "q": own_reference})
    assert other.status_code == 200 and other.json()["total"] == 0 and other.json()["items"] == []
    # The owner finds it.
    assert own in listed_ids(db_client, people["reporter"], own_reference)


@requires_database
def test_q_total_counts_only_the_matches(db_client, people):
    case_id = create_case_via_api(db_client, people["reporter"])
    reference = db_client(people["reporter"]).get(f"/api/v1/cases/{case_id}").json()["reference"]
    body = db_client(people["triager"]).get("/api/v1/cases", params={"limit": 200, "q": reference}).json()
    assert body["total"] == len(body["items"]) == 1
    assert body["items"][0]["id"] == case_id


@requires_database
def test_q_treats_like_wildcards_as_plain_text_and_ignores_titles(db_client, people):
    marker = "Title marker " + uuid.uuid4().hex
    created = db_client(people["reporter"]).post(
        "/api/v1/cases",
        json={"title": marker, "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
    )
    assert created.status_code == 201
    for query in ("%", "_", "SAFE-%", marker):
        response = db_client(people["triager"]).get("/api/v1/cases", params={"limit": 200, "q": query})
        assert response.status_code == 200
        assert response.json()["total"] == 0, query
    # A blank q is no filter at all.
    plain = db_client(people["triager"]).get("/api/v1/cases?limit=200").json()
    blank = db_client(people["triager"]).get("/api/v1/cases", params={"limit": 200, "q": "  "}).json()
    assert blank["total"] == plain["total"]

