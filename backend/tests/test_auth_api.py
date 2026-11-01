import time
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.security import hash_password
from safe_kit.sessions import InvalidRefreshToken
from safe_kit.users import User

PASSWORD = "correct horse battery"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
CAROL = "33333333-3333-3333-3333-333333333333"


class FakeStore:
    """In-memory stand-in for the database functions used by the API."""

    def __init__(self):
        self.users = {}
        self.sessions = {}  # raw refresh token -> {"user_id", "family", "revoked"}
        self.revoked_jtis = set()
        self.buckets = {}
        self.login_failures = []
        self._counter = 0

    def add_user(self, user_id, username, role, *, password=PASSWORD, active=True, must_change=False):
        self.users[user_id] = User(
            id=user_id,
            username=username,
            role=role,
            is_active=active,
            password_hash=hash_password(password),
            sessions_valid_after=EPOCH,
            must_change_password=must_change,
            created_at=EPOCH,
        )

    def _replace(self, user_id, **changes):
        self.users[user_id] = User(**{**self.users[user_id].__dict__, **changes})

    # --- users ---
    def find_user(self, username):
        return next((u for u in self.users.values() if u.username == username), None)

    def find_user_by_id(self, user_id):
        return self.users.get(user_id)

    def create_user(self, username, password, role, actor_id=None):
        if self.find_user(username):
            raise psycopg.errors.UniqueViolation("duplicate username")
        user_id = f"00000000-0000-0000-0000-{len(self.users) + 1:012d}"
        self.add_user(user_id, username, role, password=password, must_change=True)
        return user_id

    def _set_password(self, user_id, new_password, must_change):
        if user_id not in self.users:
            return False
        self._replace(
            user_id,
            password_hash=hash_password(new_password),
            must_change_password=must_change,
            sessions_valid_after=datetime.now(UTC),
        )
        for session in self.sessions.values():
            if session["user_id"] == user_id:
                session["revoked"] = True
        return True

    def reset_password(self, user_id, temporary_password, actor_id=None, source="api"):
        return self._set_password(user_id, temporary_password, must_change=True)

    # --- audit ---
    def record_login_failure(self, user_id, reason):
        self.login_failures.append((user_id, reason))

    def change_own_password(self, user_id, new_password):
        return self._set_password(user_id, new_password, must_change=False)

    # --- rate limiting ---
    def hit(self, bucket, window):
        self.buckets[bucket] = self.buckets.get(bucket, 0) + 1
        return self.buckets[bucket]

    def clear(self, bucket):
        self.buckets.pop(bucket, None)

    # --- sessions ---
    def open_session(self, user_id):
        self._counter += 1
        token = f"refresh-{self._counter}"
        self.sessions[token] = {"user_id": user_id, "family": self._counter, "revoked": False}
        return token

    def rotate_session(self, raw_token):
        session = self.sessions.get(raw_token)
        if session is None:
            raise InvalidRefreshToken
        if session["revoked"]:
            for other in self.sessions.values():
                if other["family"] == session["family"]:
                    other["revoked"] = True
            raise InvalidRefreshToken
        session["revoked"] = True
        new_token = self.open_session(session["user_id"])
        self.sessions[new_token]["family"] = session["family"]
        return session["user_id"], new_token

    def revoke_refresh_token(self, raw_token, user_id):
        session = self.sessions.get(raw_token)
        if session and session["user_id"] == user_id:
            session["revoked"] = True

    def revoke_access_token(self, jti, expires_at):
        self.revoked_jtis.add(jti)

    def is_access_token_revoked(self, jti):
        return jti in self.revoked_jtis


@pytest.fixture
def store(tmp_path, monkeypatch):
    key_file = tmp_path / "jwt_signing_key.txt"
    key_file.write_text("test-signing-key-with-enough-length-1234567890", encoding="utf-8")
    monkeypatch.setenv("SAFE_JWT_KEY_FILE", str(key_file))

    fake = FakeStore()
    fake.add_user(ALICE, "alice", "triager")
    fake.add_user(BOB, "bob", "reviewer", active=False)
    fake.add_user(CAROL, "carol", "admin")

    monkeypatch.setattr(api, "admin_exists", lambda: True)
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    monkeypatch.setattr(auth, "find_user_by_id", fake.find_user_by_id)
    monkeypatch.setattr(auth, "is_access_token_revoked", fake.is_access_token_revoked)
    monkeypatch.setattr(api, "find_user", fake.find_user)
    monkeypatch.setattr(api, "find_user_by_id", fake.find_user_by_id)
    monkeypatch.setattr(api, "create_user", fake.create_user)
    monkeypatch.setattr(api, "reset_password", fake.reset_password)
    monkeypatch.setattr(api, "record_login_failure", fake.record_login_failure)
    monkeypatch.setattr(api, "change_own_password", fake.change_own_password)
    monkeypatch.setattr(api, "hit_rate_limit", fake.hit)
    monkeypatch.setattr(api, "clear_rate_limit", fake.clear)
    monkeypatch.setattr(api, "open_session", fake.open_session)
    monkeypatch.setattr(api, "rotate_session", fake.rotate_session)
    monkeypatch.setattr(api, "revoke_refresh_token", fake.revoke_refresh_token)
    monkeypatch.setattr(api, "revoke_access_token", fake.revoke_access_token)
    return fake


@pytest.fixture
def client(store):
    return TestClient(api.app)


def login(client, username, password=PASSWORD):
    return client.post("/api/v1/auth/token", json={"username": username, "password": password})


def bearer_header(token):
    return {"Authorization": f"Bearer {token}"}


def login_tokens(client, username, password=PASSWORD):
    return login(client, username, password).json()


def test_login_returns_tokens_and_no_password_change_flag(client):
    response = login(client, "alice")
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] == 60 * 60
    assert body["password_change_required"] is False


def test_wrong_password_is_rejected_with_generic_message(client):
    response = login(client, "alice", "wrong password")
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}


def test_unknown_user_gets_same_response_as_wrong_password(client):
    unknown = login(client, "nobody", "whatever")
    wrong = login(client, "alice", "wrong password")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_nobody_can_log_in_before_the_first_admin_exists(client, monkeypatch):
    monkeypatch.setattr(api, "admin_exists", lambda: False)
    response = login(client, "alice")
    assert response.status_code == 503
    assert response.json() == {"detail": {"status": "setup_required"}}


def test_disabled_user_cannot_log_in(client):
    assert login(client, "bob").status_code == 401


def test_brute_force_locks_username_even_for_correct_password(client):
    for _ in range(5):
        assert login(client, "alice", "wrong password").status_code == 401
    locked = login(client, "alice")
    assert locked.status_code == 429
    assert locked.headers["Retry-After"] == str(15 * 60)


def test_successful_login_resets_username_counter(client):
    for _ in range(3):
        login(client, "alice", "wrong password")
    assert login(client, "alice").status_code == 200
    for _ in range(5):
        assert login(client, "alice", "wrong password").status_code == 401


def test_refresh_rotates_the_refresh_token(client):
    tokens = login_tokens(client, "alice")
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 200
    rotated = response.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert client.get("/api/v1/auth/me", headers=bearer_header(rotated["access_token"])).status_code == 200


def test_reused_refresh_token_revokes_the_whole_family(client):
    first = login_tokens(client, "alice")["refresh_token"]
    second = client.post("/api/v1/auth/refresh", json={"refresh_token": first}).json()["refresh_token"]

    assert client.post("/api/v1/auth/refresh", json={"refresh_token": first}).status_code == 401
    # The newest token in the same family was revoked as well.
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": second}).status_code == 401


def test_logout_revokes_access_token_immediately(client):
    tokens = login_tokens(client, "alice")
    assert client.get("/api/v1/auth/me", headers=bearer_header(tokens["access_token"])).status_code == 200

    assert client.post("/api/v1/auth/logout", headers=bearer_header(tokens["access_token"])).status_code == 204
    assert client.get("/api/v1/auth/me", headers=bearer_header(tokens["access_token"])).status_code == 401


def test_logout_with_refresh_token_revokes_refresh_token(client):
    tokens = login_tokens(client, "alice")
    client.post(
        "/api/v1/auth/logout",
        headers=bearer_header(tokens["access_token"]),
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_logout_requires_a_valid_access_token(client):
    assert client.post("/api/v1/auth/logout").status_code == 401


def test_me_with_valid_token(client):
    token = login_tokens(client, "alice")["access_token"]
    response = client.get("/api/v1/auth/me", headers=bearer_header(token))
    assert response.status_code == 200
    assert response.json() == {
        "id": ALICE,
        "username": "alice",
        "role": "triager",
        "password_change_required": False,
    }


def test_me_without_or_with_bad_token_is_rejected(client):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers=bearer_header("not-a-jwt")).status_code == 401


def test_token_stops_working_when_account_is_disabled(client, store):
    token = login_tokens(client, "alice")["access_token"]
    store._replace(ALICE, is_active=False)
    assert client.get("/api/v1/auth/me", headers=bearer_header(token)).status_code == 401


def test_new_account_must_change_password_before_anything_else(client, store):
    admin_token = login_tokens(client, "carol")["access_token"]
    created = client.post(
        "/api/v1/users",
        headers=bearer_header(admin_token),
        json={"username": "dave", "role": "reviewer", "temporary_password": "temporary passphrase"},
    )
    assert created.status_code == 201
    assert created.json()["password_change_required"] is True

    dave = login_tokens(client, "dave", "temporary passphrase")
    assert dave["password_change_required"] is True
    assert client.post(
        "/api/v1/users",
        headers=bearer_header(dave["access_token"]),
        json={"username": "erin", "role": "auditor", "temporary_password": "temporary passphrase"},
    ).status_code == 403
    # Identity and logout stay available so the client can show the change screen.
    assert client.get("/api/v1/auth/me", headers=bearer_header(dave["access_token"])).json()[
        "password_change_required"
    ] is True


def test_password_change_clears_the_flag_and_ends_sessions(client, store):
    admin_token = login_tokens(client, "carol")["access_token"]
    client.post(
        "/api/v1/users",
        headers=bearer_header(admin_token),
        json={"username": "dave", "role": "reviewer", "temporary_password": "temporary passphrase"},
    )
    dave = login_tokens(client, "dave", "temporary passphrase")

    changed = client.post(
        "/api/v1/auth/password",
        headers=bearer_header(dave["access_token"]),
        json={"current_password": "temporary passphrase", "new_password": "my own passphrase"},
    )
    assert changed.status_code == 204
    assert client.get("/api/v1/auth/me", headers=bearer_header(dave["access_token"])).status_code == 401
    assert login(client, "dave", "temporary passphrase").status_code == 401

    # Tokens are rejected if issued in the same second as the password change (iat has whole seconds).
    time.sleep(1.1)
    relogin = login_tokens(client, "dave", "my own passphrase")
    assert relogin["password_change_required"] is False
    assert client.get("/api/v1/auth/me", headers=bearer_header(relogin["access_token"])).status_code == 200


def test_password_change_requires_current_password(client):
    token = login_tokens(client, "alice")["access_token"]
    response = client.post(
        "/api/v1/auth/password",
        headers=bearer_header(token),
        json={"current_password": "wrong", "new_password": "my own passphrase"},
    )
    assert response.status_code == 401
    assert login(client, "alice", PASSWORD).status_code == 200


def test_admin_can_create_user_with_temporary_password(client):
    admin_token = login_tokens(client, "carol")["access_token"]
    response = client.post(
        "/api/v1/users",
        headers=bearer_header(admin_token),
        json={"username": "frank", "role": "auditor", "temporary_password": "temporary passphrase"},
    )
    assert response.status_code == 201
    assert response.json()["role"] == "auditor"


def test_duplicate_username_returns_409(client):
    admin_token = login_tokens(client, "carol")["access_token"]
    response = client.post(
        "/api/v1/users",
        headers=bearer_header(admin_token),
        json={"username": "alice", "role": "auditor", "temporary_password": "temporary passphrase"},
    )
    assert response.status_code == 409


def test_non_admin_cannot_create_users(client):
    token = login_tokens(client, "alice")["access_token"]
    response = client.post(
        "/api/v1/users",
        headers=bearer_header(token),
        json={"username": "frank", "role": "auditor", "temporary_password": "temporary passphrase"},
    )
    assert response.status_code == 403


def test_unknown_role_is_rejected(client):
    admin_token = login_tokens(client, "carol")["access_token"]
    response = client.post(
        "/api/v1/users",
        headers=bearer_header(admin_token),
        json={"username": "frank", "role": "root", "temporary_password": "temporary passphrase"},
    )
    assert response.status_code == 422


def test_admin_reset_sets_temporary_password_and_ends_old_sessions(client):
    alice_tokens = login_tokens(client, "alice")
    admin_token = login_tokens(client, "carol")["access_token"]

    response = client.post(
        f"/api/v1/users/{ALICE}/password",
        headers=bearer_header(admin_token),
        json={"temporary_password": "temporary passphrase"},
    )
    assert response.status_code == 204

    assert client.get("/api/v1/auth/me", headers=bearer_header(alice_tokens["access_token"])).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": alice_tokens["refresh_token"]}).status_code == 401
    assert login(client, "alice", PASSWORD).status_code == 401
    temporary = login_tokens(client, "alice", "temporary passphrase")
    assert temporary["password_change_required"] is True


def test_non_admin_cannot_reset_passwords(client):
    triager_token = login_tokens(client, "alice")["access_token"]
    response = client.post(
        f"/api/v1/users/{CAROL}/password",
        headers=bearer_header(triager_token),
        json={"temporary_password": "temporary passphrase"},
    )
    assert response.status_code == 403


def test_reset_for_unknown_user_returns_404(client):
    admin_token = login_tokens(client, "carol")["access_token"]
    response = client.post(
        "/api/v1/users/44444444-4444-4444-4444-444444444444/password",
        headers=bearer_header(admin_token),
        json={"temporary_password": "temporary passphrase"},
    )
    assert response.status_code == 404


def test_short_passwords_are_rejected(client):
    admin_token = login_tokens(client, "carol")["access_token"]
    response = client.post(
        f"/api/v1/users/{ALICE}/password",
        headers=bearer_header(admin_token),
        json={"temporary_password": "short"},
    )
    assert response.status_code == 422


def test_failed_login_is_audited_by_account_not_by_username_or_address(client, store):
    response = client.post(
        "/api/v1/auth/token",
        headers={"X-Real-IP": "203.0.113.9"},
        json={"username": "alice", "password": "wrong password here"},
    )
    assert response.status_code == 401
    assert store.login_failures == [(ALICE, "wrong_password")]


def test_failed_login_for_unknown_name_is_audited_without_the_name(client, store):
    login(client, "nobody-by-this-name")
    assert store.login_failures == [(None, "unknown_user")]


def test_login_to_disabled_account_is_audited_as_inactive(client, store):
    login(client, "bob")
    assert store.login_failures == [(BOB, "account_inactive")]


def test_successful_login_is_not_audited_as_a_failure(client, store):
    login(client, "alice")
    assert store.login_failures == []
