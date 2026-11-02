"""GET /api/v1/cases/{case_id}/assignable-users: the picker list for a cases:assign holder.

Tests without a database check the permission gate, the response shape and the 404 mapping with the service
function replaced. Tests marked "database" check which users are listed, against the role grants in PostgreSQL.
They need SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (see test_database.py). They create rows and never
remove them, so use a throwaway database.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, system_timezone, users
from safe_kit.cases import AssignableUser, CaseScope
from safe_kit.rbac import (
    DEFAULT_ROLE_PERMISSIONS,
    list_role_permissions,
    set_role_permissions,
)
from safe_kit.users import User

TRIAGER_ID = "22222222-2222-2222-2222-222222222222"
CASE_ID = "99999999-9999-9999-9999-999999999999"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


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


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user(TRIAGER_ID, "triager")}
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


def url() -> str:
    return f"/api/v1/cases/{CASE_ID}/assignable-users"


# --- HTTP mapping without a database -----------------------------------------------------------------------------


def test_holder_of_cases_assign_gets_the_listed_users_with_name_and_role_only(
    client, monkeypatch
):
    seen = {}

    def fake_list(case_id, scope):
        seen["case_id"] = case_id
        seen["scope"] = scope
        return [
            AssignableUser(
                id="77777777-7777-7777-7777-777777777777",
                username="investigator-one",
                first_name="Ada",
                last_name="Lovelace",
                role="investigator",
            )
        ]

    monkeypatch.setattr(api, "list_assignable_users", fake_list)
    response = client.get(url())
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "77777777-7777-7777-7777-777777777777",
            "username": "investigator-one",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "role": "investigator",
        }
    ]
    assert seen["case_id"] == CASE_ID
    assert seen["scope"] == CaseScope(
        user_id=TRIAGER_ID, read_all=True, read_assigned=True
    )


@pytest.mark.parametrize(
    "role",
    ["reporter", "investigator", "reviewer", "admin", "auditor", "disclosure_approver"],
)
def test_caller_without_cases_assign_gets_403(client, caller, monkeypatch, role):
    def must_not_run(case_id, scope):
        raise AssertionError(
            "the list must not be built for a caller without cases:assign"
        )

    monkeypatch.setattr(api, "list_assignable_users", must_not_run)
    caller["user"] = make_user(TRIAGER_ID, role)
    assert client.get(url()).status_code == 403


def test_case_the_caller_cannot_see_is_404(client, monkeypatch):
    monkeypatch.setattr(api, "list_assignable_users", lambda case_id, scope: None)
    response = client.get(url())
    assert response.status_code == 404
    assert response.json()["detail"] == "case not found"


# --- database: which users are listed -----------------------------------------------------------------------------


def new_user(role: str) -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


class ActingClient:
    """Makes requests as one real user. The caller is set before every request, so clients never share a caller."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.client = TestClient(api.app)

    def _request(self, method: str, url_path: str, **kwargs):
        user = users.find_user_by_id(self.user_id)
        assert user is not None
        api.app.dependency_overrides[auth.current_user] = lambda: user
        return getattr(self.client, method)(url_path, **kwargs)

    def get(self, url_path, **kwargs):
        return self._request("get", url_path, **kwargs)

    def post(self, url_path, **kwargs):
        return self._request("post", url_path, **kwargs)


@pytest.fixture
def db_client():
    yield ActingClient
    api.app.dependency_overrides.clear()


@pytest.fixture
def timezone_set():
    """Case creation needs a system timezone. Migrate a fresh test database, then set one."""
    from safe_kit.migrate import main as migrate

    migrate()  # idempotent: applies pending migrations on a fresh test database
    if system_timezone.current_timezone_name() is None:
        system_timezone.set_timezone("Europe/Istanbul", None, False, new_user("admin"))


@pytest.fixture
def granted(timezone_set):
    """Grant or revoke a permission on a role for one test (cases:assignable by default), then restore the roles."""
    admin = new_user("admin")
    original = {}

    def set_permission(
        role: str, granted_flag: bool, permission: str = "cases:assignable"
    ) -> None:
        if role not in original:
            original[role] = list_role_permissions()[role]
        wanted = set(original[role])
        if granted_flag:
            wanted.add(permission)
        else:
            wanted.discard(permission)
        set_role_permissions(role, frozenset(wanted), actor_id=admin)

    yield set_permission
    for role, permissions in original.items():
        set_role_permissions(role, frozenset(permissions), actor_id=admin)


def create_case_via_api(reporter: str) -> str:
    response = ActingClient(reporter).post(
        "/api/v1/cases",
        json={
            "title": "Assignable users case",
            "awareness_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        },
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def listed_ids(db_client, triager: str, case_id: str) -> set[str]:
    response = db_client(triager).get(f"/api/v1/cases/{case_id}/assignable-users")
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()}


@requires_database
def test_only_active_undeleted_assignable_roles_holding_cases_assign_are_listed(
    db_client, granted
):
    reporter = new_user("reporter")
    triager = new_user("triager")
    case_id = create_case_via_api(reporter)
    granted("investigator", True)
    granted("reporter", True)
    active = new_user("investigator")
    inactive = new_user("investigator")
    users.update_user(inactive, is_active=False, actor_id=triager)
    deleted = new_user("investigator")
    users.soft_delete_user(deleted, actor_id=triager)
    reporter_account = new_user("reporter")
    ids = listed_ids(db_client, triager, case_id)
    assert active in ids
    assert inactive not in ids
    assert deleted not in ids
    assert reporter_account not in ids
    assert reporter not in ids


@requires_database
def test_listing_follows_the_role_grant_and_triagers_are_never_listed(
    db_client, granted
):
    reporter = new_user("reporter")
    triager = new_user("triager")
    case_id = create_case_via_api(reporter)
    investigator = new_user("investigator")
    reviewer = new_user("reviewer")
    # Investigators and reviewers hold cases:assignable by default. The triager holds cases:assign (it makes the
    # assignments) but not cases:assignable, so the picker never offers it.
    assert investigator in listed_ids(db_client, triager, case_id)
    assert reviewer in listed_ids(db_client, triager, case_id)
    assert triager not in listed_ids(db_client, triager, case_id)
    granted("investigator", False)
    assert investigator not in listed_ids(db_client, triager, case_id)
    assert reviewer in listed_ids(db_client, triager, case_id)
    granted("investigator", True)
    assert investigator in listed_ids(db_client, triager, case_id)


@requires_database
def test_triager_with_cases_assign_is_not_listed_and_cannot_be_assigned(
    db_client, granted
):
    reporter = new_user("reporter")
    triager = new_user("triager")
    case_id = create_case_via_api(reporter)
    # Holds cases:assign (checked by the endpoint) and must not be an assignee.
    assert "cases:assign" in DEFAULT_ROLE_PERMISSIONS["triager"]
    assert "cases:assignable" not in DEFAULT_ROLE_PERMISSIONS["triager"]
    assert triager not in listed_ids(db_client, triager, case_id)
    response = db_client(triager).post(
        f"/api/v1/cases/{case_id}/assignments",
        json={"user_id": triager},
        headers=idem(),
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "assignee must be an investigator or reviewer"


@requires_database
def test_assignability_follows_the_grant_not_the_role_name(db_client, granted):
    reporter = new_user("reporter")
    triager = new_user("triager")
    case_id = create_case_via_api(reporter)
    auditor = new_user("auditor")
    url = f"/api/v1/cases/{case_id}/assignments"
    assert auditor not in listed_ids(db_client, triager, case_id)
    assert (
        db_client(triager)
        .post(url, json={"user_id": auditor}, headers=idem())
        .status_code
        == 422
    )
    granted("auditor", True)
    assert auditor in listed_ids(db_client, triager, case_id)
    assert (
        db_client(triager)
        .post(url, json={"user_id": auditor}, headers=idem())
        .status_code
        == 201
    )
    granted("auditor", False)
    assert auditor not in listed_ids(db_client, triager, case_id)
    assert (
        db_client(triager)
        .post(url, json={"user_id": new_user("auditor")}, headers=idem())
        .status_code
        == 422
    )


@requires_database
def test_unassigned_holder_of_cases_assign_gets_404_for_a_case_it_cannot_see(
    db_client, granted
):
    reporter = new_user("reporter")
    case_id = create_case_via_api(reporter)
    # The endpoint needs cases:assign; the investigator holds it only for this test, so the 404 is about visibility.
    granted("investigator", True, "cases:assign")
    unassigned = new_user("investigator")
    response = db_client(unassigned).get(f"/api/v1/cases/{case_id}/assignable-users")
    assert response.status_code == 404
    assert response.json()["detail"] == "case not found"
