import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, rbac
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS, RoleOutcome
from safe_kit.rbac import set_role_permissions as real_set_role_permissions
from safe_kit.users import User
from datetime import UTC, datetime

ADMIN_ID = "33333333-3333-3333-3333-333333333333"


def make_user(role):
    return User(
        id=ADMIN_ID if role == "admin" else "44444444-4444-4444-4444-444444444444",
        username="someone",
        role=role,
        is_active=True,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user("admin")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(caller):
    return TestClient(api.app)


@pytest.fixture
def matrix(monkeypatch):
    state = {"roles": {role: sorted(perms) for role, perms in DEFAULT_ROLE_PERMISSIONS.items()}, "calls": []}
    monkeypatch.setattr(api, "list_role_permissions", lambda: state["roles"])
    monkeypatch.setattr(api, "list_catalog", lambda: [("cases:submit", "Submit a case")])

    def fake_set(role, permissions, **kwargs):
        state["calls"].append((role, permissions))
        return state.get("outcome", RoleOutcome.OK)

    monkeypatch.setattr(api, "set_role_permissions", fake_set)
    return state


def test_admin_reads_the_matrix(client, matrix):
    response = client.get("/api/v1/rbac/roles")
    assert response.status_code == 200
    by_role = {entry["role"]: entry["permissions"] for entry in response.json()}
    assert "roles:manage" in by_role["admin"]


def test_admin_reads_the_permission_catalog(client, matrix):
    response = client.get("/api/v1/rbac/permissions")
    assert response.json() == [{"name": "cases:submit", "description": "Submit a case"}]


def test_admin_updates_a_role(client, matrix):
    response = client.put("/api/v1/rbac/roles/auditor/permissions", json={"permissions": ["audit:read", "cases:submit"]})
    assert response.status_code == 200
    assert matrix["calls"] == [("auditor", frozenset({"audit:read", "cases:submit"}))]


def test_admin_role_cannot_lose_required_permissions(client, matrix):
    matrix["outcome"] = RoleOutcome.ADMIN_GUARD
    response = client.put("/api/v1/rbac/roles/admin/permissions", json={"permissions": ["users:manage"]})
    assert response.status_code == 409
    assert "roles:manage" in response.json()["detail"]


def test_unknown_permission_is_rejected(client, matrix):
    matrix["outcome"] = RoleOutcome.UNKNOWN_PERMISSION
    response = client.put("/api/v1/rbac/roles/auditor/permissions", json={"permissions": ["root:everything"]})
    assert response.status_code == 422


def test_unknown_role_returns_404(client, matrix):
    response = client.put("/api/v1/rbac/roles/root/permissions", json={"permissions": []})
    assert response.status_code == 404
    assert matrix["calls"] == []


def test_caller_without_roles_manage_cannot_change_matrix(client, caller, matrix):
    caller["user"] = make_user("triager")
    response = client.put("/api/v1/rbac/roles/auditor/permissions", json={"permissions": []})
    assert response.status_code == 403
    assert matrix["calls"] == []


@pytest.mark.parametrize(
    "permissions",
    [
        ["cases:read_all", "reporter_contact:read"],  # the current set, unchanged
        ["cases:read_all"],  # drops the contact grant
        ["cases:read_all", "cases:triage", "reporter_contact:read"],  # adds a permission
    ],
)
def test_reporting_owner_is_refused_with_409_and_the_fixed_code(client, matrix, monkeypatch, permissions):
    # The real rbac function runs here: it refuses before it touches the database, so no connection is opened.
    monkeypatch.setattr(api, "set_role_permissions", real_set_role_permissions)
    monkeypatch.setattr(rbac, "connect", lambda: pytest.fail("the refusal must happen before any database access"))

    response = client.put("/api/v1/rbac/roles/reporting_owner/permissions", json={"permissions": permissions})

    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "reporting_owner_fixed"}


def test_permission_change_is_attributed_to_the_acting_admin(client, matrix, monkeypatch):
    seen = {}

    def fake_set(role, permissions, actor_id=None):
        seen["actor_id"] = actor_id
        return RoleOutcome.OK

    monkeypatch.setattr(api, "set_role_permissions", fake_set)
    response = client.put(
        "/api/v1/rbac/roles/auditor/permissions",
        json={"permissions": ["audit:read"]},
    )
    assert response.status_code == 200
    assert seen["actor_id"] == ADMIN_ID
