from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import UpdateOutcome, User

ADMIN_ID = "33333333-3333-3333-3333-333333333333"
TARGET_ID = "11111111-1111-1111-1111-111111111111"


def make_user(user_id, username, role, *, active=True):
    return User(
        id=user_id,
        username=username,
        role=role,
        is_active=active,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


@pytest.fixture
def actor():
    return {"user": make_user(ADMIN_ID, "osaa-safe-admin", "admin")}


@pytest.fixture
def client(actor, monkeypatch):
    # Authentication is covered by test_auth_api. Here the caller is injected directly.
    api.app.dependency_overrides[auth.current_user] = lambda: actor["user"]
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


@pytest.fixture
def users(monkeypatch):
    store = {
        TARGET_ID: make_user(TARGET_ID, "alice", "triager"),
        ADMIN_ID: make_user(ADMIN_ID, "osaa-safe-admin", "admin"),
    }
    monkeypatch.setattr(api, "list_users", lambda limit, offset: list(store.values())[offset : offset + limit])
    monkeypatch.setattr(api, "count_users", lambda: len(store))
    monkeypatch.setattr(api, "find_user_by_id", lambda user_id: store.get(user_id))
    return store


def test_admin_lists_users_with_pagination(client, users):
    response = client.get("/api/v1/users?limit=1&offset=0")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert set(body["items"][0]) == {"id", "username", "role", "is_active", "password_change_required", "created_at", "is_protected", "email", "first_name", "last_name", "title"}


def test_list_rejects_out_of_range_paging(client, users):
    assert client.get("/api/v1/users?limit=0").status_code == 422
    assert client.get("/api/v1/users?limit=201").status_code == 422
    assert client.get("/api/v1/users?offset=-1").status_code == 422


def test_non_admin_cannot_list_users(client, actor, users):
    actor["user"] = make_user("44444444-4444-4444-4444-444444444444", "tina", "triager")
    assert client.get("/api/v1/users").status_code == 403


def test_admin_changes_role(client, users, monkeypatch):
    monkeypatch.setattr(api, "update_user", lambda user_id, role=None, is_active=None, **kwargs: UpdateOutcome.OK)
    users[TARGET_ID] = make_user(TARGET_ID, "alice", "reviewer")
    response = client.patch(f"/api/v1/users/{TARGET_ID}", json={"role": "reviewer"})
    assert response.status_code == 200
    assert response.json()["role"] == "reviewer"


def test_admin_disables_user(client, users, monkeypatch):
    calls = []

    def fake_update(user_id, role=None, is_active=None, **kwargs):
        calls.append((user_id, role, is_active))
        return UpdateOutcome.OK

    monkeypatch.setattr(api, "update_user", fake_update)
    users[TARGET_ID] = make_user(TARGET_ID, "alice", "triager", active=False)
    response = client.patch(f"/api/v1/users/{TARGET_ID}", json={"is_active": False})
    assert response.status_code == 200
    assert calls == [(TARGET_ID, None, False)]


def test_admin_cannot_change_own_role_or_status(client, users):
    response = client.patch(f"/api/v1/users/{ADMIN_ID}", json={"is_active": False})
    assert response.status_code == 409


def test_empty_update_is_rejected(client, users):
    assert client.patch(f"/api/v1/users/{TARGET_ID}", json={}).status_code == 422


def test_unknown_role_in_update_is_rejected(client, users):
    assert client.patch(f"/api/v1/users/{TARGET_ID}", json={"role": "root"}).status_code == 422


def test_last_active_admin_cannot_be_removed(client, users, monkeypatch):
    monkeypatch.setattr(api, "update_user", lambda user_id, role=None, is_active=None, **kwargs: UpdateOutcome.LAST_ADMIN)
    response = client.patch(f"/api/v1/users/{TARGET_ID}", json={"role": "auditor"})
    assert response.status_code == 409
    assert "last active admin" in response.json()["detail"]


def test_update_of_unknown_user_returns_404(client, users, monkeypatch):
    monkeypatch.setattr(api, "update_user", lambda user_id, role=None, is_active=None, **kwargs: UpdateOutcome.NOT_FOUND)
    response = client.patch("/api/v1/users/55555555-5555-5555-5555-555555555555", json={"is_active": False})
    assert response.status_code == 404


def test_create_user_rejects_reserved_username(client):
    response = client.post(
        "/api/v1/users",
        json={"username": "root", "role": "auditor", "temporary_password": "temporary passphrase"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "username is reserved"


def test_admin_soft_deletes_user(client, users, monkeypatch):
    calls = []

    def fake_delete(user_id, **kwargs):
        calls.append(user_id)
        return UpdateOutcome.OK

    monkeypatch.setattr(api, "soft_delete_user", fake_delete)
    response = client.delete(f"/api/v1/users/{TARGET_ID}")
    assert response.status_code == 204
    assert calls == [TARGET_ID]


def test_admin_cannot_delete_own_account(client, users):
    assert client.delete(f"/api/v1/users/{ADMIN_ID}").status_code == 409


def test_delete_of_last_active_admin_is_refused(client, users, monkeypatch):
    monkeypatch.setattr(api, "soft_delete_user", lambda user_id, **kwargs: UpdateOutcome.LAST_ADMIN)
    assert client.delete(f"/api/v1/users/{TARGET_ID}").status_code == 409


def test_delete_of_unknown_user_returns_404(client, users, monkeypatch):
    monkeypatch.setattr(api, "soft_delete_user", lambda user_id, **kwargs: UpdateOutcome.NOT_FOUND)
    assert client.delete("/api/v1/users/55555555-5555-5555-5555-555555555555").status_code == 404


def test_non_admin_cannot_delete_users(client, actor, users):
    actor["user"] = make_user("44444444-4444-4444-4444-444444444444", "tina", "triager")
    assert client.delete(f"/api/v1/users/{TARGET_ID}").status_code == 403


def test_protected_admin_cannot_be_demoted_or_disabled(client, users, monkeypatch):
    monkeypatch.setattr(api, "update_user", lambda user_id, role=None, is_active=None, **kwargs: UpdateOutcome.PROTECTED)
    response = client.patch(f"/api/v1/users/{TARGET_ID}", json={"is_active": False})
    assert response.status_code == 409
    assert "protected admin" in response.json()["detail"]


def test_protected_admin_cannot_be_deleted(client, users, monkeypatch):
    monkeypatch.setattr(api, "soft_delete_user", lambda user_id, **kwargs: UpdateOutcome.PROTECTED)
    response = client.delete(f"/api/v1/users/{TARGET_ID}")
    assert response.status_code == 409
    assert "protected admin" in response.json()["detail"]


def test_update_is_attributed_to_the_acting_admin(client, users, monkeypatch):
    seen = {}

    def fake_update(user_id, role=None, is_active=None, actor_id=None):
        seen["actor_id"] = actor_id
        return UpdateOutcome.OK

    monkeypatch.setattr(api, "update_user", fake_update)
    users[TARGET_ID] = make_user(TARGET_ID, "alice", "reviewer")
    assert client.patch(f"/api/v1/users/{TARGET_ID}", json={"role": "reviewer"}).status_code == 200
    assert seen["actor_id"] == ADMIN_ID


def test_soft_delete_is_attributed_to_the_acting_admin(client, users, monkeypatch):
    seen = {}

    def fake_delete(user_id, actor_id=None):
        seen["actor_id"] = actor_id
        return UpdateOutcome.OK

    monkeypatch.setattr(api, "soft_delete_user", fake_delete)
    assert client.delete(f"/api/v1/users/{TARGET_ID}").status_code == 204
    assert seen["actor_id"] == ADMIN_ID
