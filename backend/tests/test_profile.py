"""Profile fields, the required email, the profile endpoints, and the CLI bootstrap that asks for an email.

Most tests need no database: the endpoints run on their own FastAPI app with the router, and the database
functions are replaced by fakes or checked through a fake connection. The tests marked "database" run the
rules against PostgreSQL. They are skipped unless SAFE_TEST_DATABASE=1, like tests/test_database.py.
Use a throwaway database: the tests create rows and never remove them (rows are soft-deleted, as in the application).
"""

import dataclasses
import io
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safe_kit import admin, audit, auth, migrate, profile_api, users
from safe_kit.db import connect
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import (
    EmailAlreadyInUse,
    InvalidEmail,
    InvalidProfile,
    User,
    normalize_email,
    normalize_profile_text,
)

PASSWORD = "bootstrap passphrase"
EMAIL = "first.admin@example.com"
ADMIN_ID = "33333333-3333-3333-3333-333333333333"
ALICE_ID = "11111111-1111-1111-1111-111111111111"
BOB_ID = "22222222-2222-2222-2222-222222222222"
LONG_LOCAL = "a" * 250

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def make_user(user_id, username, role, email=None, **profile):
    return User(
        id=user_id,
        username=username,
        role=role,
        is_active=True,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        email=email,
        **profile,
    )


# Email rules


@pytest.mark.parametrize(
    "value",
    [
        "alice@example.com",
        "  bob@example.org  ",
        "Carol.Smith@Example.COM",
        "user+tag@sub.example.co.uk",
    ],
)
def test_valid_emails_are_accepted(value):
    assert normalize_email(value) == value.strip()


def test_email_is_stored_as_entered_apart_from_surrounding_whitespace():
    assert normalize_email("  Carol.Smith@Example.COM ") == "Carol.Smith@Example.COM"


@pytest.mark.parametrize(
    "value",
    ["not-an-email", "a b@example.com", "missing-at.example.com", "x@localhost"],
)
def test_invalid_emails_are_rejected(value):
    with pytest.raises(InvalidEmail, match="^invalid email address$"):
        normalize_email(value)


def test_email_longer_than_254_characters_is_rejected():
    too_long = LONG_LOCAL + "@example.com"
    assert len(too_long) > 254
    with pytest.raises(InvalidEmail, match="^invalid email address$"):
        normalize_email(too_long)


def test_email_of_exactly_254_characters_passes_the_length_rule():
    local = "a" * 64
    domain = "b" * 63 + "." + "c" * 63 + "." + "d" * 53 + ".example"
    value = f"{local}@{domain}"
    assert len(value) == 254
    assert normalize_email(value) == value


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_email_is_reported_as_required(value):
    with pytest.raises(InvalidEmail, match="^email is required$"):
        normalize_email(value)


def test_profile_text_is_trimmed_and_empty_becomes_none():
    assert normalize_profile_text("first_name", "  Ayse  ") == "Ayse"
    assert normalize_profile_text("title", "   ") is None
    assert normalize_profile_text("last_name", "") is None
    assert normalize_profile_text("last_name", None) is None


def test_profile_text_longer_than_100_characters_is_rejected():
    assert normalize_profile_text("title", "t" * 100) == "t" * 100
    with pytest.raises(InvalidProfile, match="title must be at most 100 characters"):
        normalize_profile_text("title", "t" * 101)


# Profile endpoints, without a database


class FakeProfiles:
    """Stands in for users.update_profile: applies the change to an in-memory store and enforces the email rule."""

    def __init__(self, users_by_id):
        # Shared with the fixture store, so the caller in signed_in sees the changes.
        self.users = users_by_id
        self.calls = []

    def update(self, user_id, changes, actor_id=None):
        self.calls.append((user_id, dict(changes), actor_id))
        user = self.users.get(user_id)
        if user is None:
            return None
        email = changes.get("email")
        if email and any(
            other.id != user_id and other.email and other.email.lower() == email.lower()
            for other in self.users.values()
        ):
            raise EmailAlreadyInUse
        self.users[user_id] = dataclasses.replace(user, **changes)
        return self.users[user_id]


@pytest.fixture
def store():
    return {
        ADMIN_ID: make_user(
            ADMIN_ID, "osaa-safe-admin", "admin", email="admin@example.com"
        ),
        ALICE_ID: make_user(
            ALICE_ID,
            "alice",
            "triager",
            email="alice@example.com",
            first_name="Alice",
            title="Analyst",
        ),
        BOB_ID: make_user(BOB_ID, "bob", "reporter", email=None),
    }


@pytest.fixture
def profiles(store, monkeypatch):
    fake = FakeProfiles(store)
    monkeypatch.setattr(profile_api, "update_profile", fake.update)
    return fake


@pytest.fixture
def signed_in(store, monkeypatch):
    """The caller is injected directly, as in test_user_management_api. Authentication has its own tests."""
    holder = {"id": ADMIN_ID}
    monkeypatch.setattr(
        auth,
        "permissions_for_role",
        lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()),
    )

    def current():
        return store[holder["id"]]

    app = FastAPI()
    app.include_router(profile_api.router)
    app.dependency_overrides[auth.current_user] = current
    app.dependency_overrides[auth.current_user_pending_change] = current
    client = TestClient(app)
    return client, holder


def test_profile_is_readable_before_the_password_change(store, monkeypatch):
    # The change-password screen shows the profile, so the pending-change dependency is used here.
    app = FastAPI()
    app.include_router(profile_api.router)
    app.dependency_overrides[auth.current_user_pending_change] = lambda: store[ALICE_ID]
    response = TestClient(app).get("/api/v1/auth/profile")
    assert response.status_code == 200
    assert response.json() == {
        "first_name": "Alice",
        "last_name": None,
        "title": "Analyst",
        "email": "alice@example.com",
    }


def test_user_updates_own_profile(signed_in, profiles):
    client, holder = signed_in
    holder["id"] = ALICE_ID
    response = client.patch(
        "/api/v1/auth/profile", json={"last_name": "  Yilmaz ", "title": "Lead analyst"}
    )
    assert response.status_code == 200
    assert response.json()["last_name"] == "Yilmaz"
    assert response.json()["title"] == "Lead analyst"
    assert response.json()["first_name"] == "Alice"  # not sent, so unchanged
    assert profiles.calls == [
        (ALICE_ID, {"last_name": "Yilmaz", "title": "Lead analyst"}, ALICE_ID)
    ]


def test_null_or_blank_name_clears_it(signed_in, profiles):
    client, holder = signed_in
    holder["id"] = ALICE_ID
    response = client.patch(
        "/api/v1/auth/profile", json={"first_name": None, "title": "  "}
    )
    assert response.status_code == 200
    assert response.json()["first_name"] is None
    assert response.json()["title"] is None


@pytest.mark.parametrize("email", [None, "", "   "])
def test_own_email_cannot_be_cleared(signed_in, profiles, email):
    client, holder = signed_in
    holder["id"] = ALICE_ID
    response = client.patch("/api/v1/auth/profile", json={"email": email})
    assert response.status_code == 422
    assert response.json()["detail"] == "email is required"
    assert profiles.calls == []


def test_own_invalid_email_is_refused(signed_in, profiles):
    client, holder = signed_in
    holder["id"] = ALICE_ID
    response = client.patch("/api/v1/auth/profile", json={"email": "alice at example"})
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid email address"
    assert profiles.calls == []


def test_empty_profile_update_is_refused(signed_in, profiles):
    client, holder = signed_in
    holder["id"] = ALICE_ID
    response = client.patch("/api/v1/auth/profile", json={})
    assert response.status_code == 422
    assert response.json()["detail"] == "nothing to change"


def test_unknown_profile_field_is_refused(signed_in, profiles):
    client, holder = signed_in
    holder["id"] = ALICE_ID
    response = client.patch("/api/v1/auth/profile", json={"firstName": "Alice"})
    assert response.status_code == 422


def test_duplicate_email_on_own_profile_is_a_conflict(signed_in, profiles, store):
    client, holder = signed_in
    holder["id"] = BOB_ID
    response = client.patch("/api/v1/auth/profile", json={"email": "ALICE@example.com"})
    assert response.status_code == 409
    assert response.json()["detail"] == "email already in use"


def test_admin_updates_another_users_profile_with_actor_recorded(signed_in, profiles):
    client, _ = signed_in
    response = client.patch(
        f"/api/v1/users/{BOB_ID}/profile",
        json={"email": "bob@example.org", "first_name": "Bob"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "first_name": "Bob",
        "last_name": None,
        "title": None,
        "email": "bob@example.org",
    }
    assert profiles.calls == [
        (BOB_ID, {"email": "bob@example.org", "first_name": "Bob"}, ADMIN_ID)
    ]


def test_admin_cannot_edit_own_profile_through_the_admin_path(signed_in, profiles):
    client, _ = signed_in
    response = client.patch(
        f"/api/v1/users/{ADMIN_ID}/profile", json={"title": "Chief"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "use your own profile endpoint"
    assert profiles.calls == []


def test_admin_profile_edit_of_unknown_user_is_404(signed_in, profiles):
    client, _ = signed_in
    missing = "44444444-4444-4444-4444-444444444444"
    response = client.patch(
        f"/api/v1/users/{missing}/profile", json={"title": "Analyst"}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "user not found"


def test_admin_profile_edit_cannot_clear_email(signed_in, profiles):
    client, _ = signed_in
    response = client.patch(f"/api/v1/users/{ALICE_ID}/profile", json={"email": None})
    assert response.status_code == 422
    assert response.json()["detail"] == "email is required"


def test_non_admin_cannot_edit_another_users_profile(signed_in, profiles, store):
    client, holder = signed_in
    holder["id"] = BOB_ID
    response = client.patch(
        f"/api/v1/users/{ALICE_ID}/profile", json={"title": "Chief"}
    )
    assert response.status_code == 403
    assert profiles.calls == []


def test_user_summary_helper_returns_the_profile_fields(store):
    fields = profile_api.profile_fields(store[ALICE_ID])
    assert fields == {
        "first_name": "Alice",
        "last_name": None,
        "title": "Analyst",
        "email": "alice@example.com",
    }


# Audit: field names only, never values


class _Rows:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    """Answers the three statements users.update_profile issues, and records the UPDATE it would run."""

    def __init__(self, before, after):
        self.before = before
        self.after = after
        self.update_sql = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        if not isinstance(statement, str):
            self.update_sql.append((statement.as_string(None), list(params)))
            return _Rows(None)
        if statement.startswith("SELECT first_name"):
            return _Rows(self.before)
        return _Rows(self.after)


ROW_BEFORE = ("Alice", None, "Analyst", "alice@example.com")


def full_row(email: str) -> tuple:
    """A users row as users._COLUMNS selects it, for the connection fake."""
    return (
        ALICE_ID,
        "alice",
        "triager",
        True,
        "unused",
        datetime(1970, 1, 1, tzinfo=UTC),
        False,
        datetime(2026, 11, 1, tzinfo=UTC),
        False,
        "Alice",
        None,
        "Analyst",
        email,
        datetime.now(UTC),
        1,
    )


@pytest.fixture
def audited(monkeypatch):
    recorded = []

    def fake_record(
        conn, action, target_type, target_id=None, actor_id=None, details=None
    ):
        audit.check_details(details or {}, action)
        recorded.append(
            {
                "action": action,
                "target_id": target_id,
                "actor_id": actor_id,
                "details": details,
            }
        )

    monkeypatch.setattr(audit, "record", fake_record)
    return recorded


def test_profile_audit_row_carries_field_names_only(audited, monkeypatch):
    connection = _FakeConnection(ROW_BEFORE, full_row("new.address@example.org"))
    monkeypatch.setattr(users, "connect", lambda: connection)
    updated = users.update_profile(
        ALICE_ID, {"email": "new.address@example.org"}, actor_id=ALICE_ID
    )

    assert updated.email == "new.address@example.org"
    assert len(audited) == 1
    row = audited[0]
    assert row["action"] == "user.profile_updated"
    assert row["target_id"] == ALICE_ID
    assert row["actor_id"] == ALICE_ID
    assert row["details"] == {"fields": ["email"]}
    assert "new.address@example.org" not in repr(row["details"])
    assert "Alice" not in repr(row["details"])


def test_only_changed_fields_are_written_and_named(audited, monkeypatch):
    connection = _FakeConnection(ROW_BEFORE, full_row("new.address@example.org"))
    monkeypatch.setattr(users, "connect", lambda: connection)
    users.update_profile(
        ALICE_ID,
        {"first_name": "Alice", "email": "new.address@example.org"},
        actor_id=ADMIN_ID,
    )

    # first_name is unchanged, so it is neither written nor audited.
    assert audited[0]["details"] == {"fields": ["email"]}
    statement, params = connection.update_sql[0]
    assert '"first_name"' not in statement
    # The account revision rises with the change (api-draft 3.5).
    assert (
        statement
        == 'UPDATE users SET "email" = %s, revision = revision + 1 WHERE id = %s'
    )
    assert params == ["new.address@example.org", ALICE_ID]


def test_unchanged_profile_writes_nothing(audited, monkeypatch):
    connection = _FakeConnection(ROW_BEFORE, full_row("alice@example.com"))
    monkeypatch.setattr(users, "connect", lambda: connection)
    users.update_profile(ALICE_ID, {"title": "Analyst"}, actor_id=ALICE_ID)
    assert connection.update_sql == []
    assert audited == []


def test_update_profile_of_missing_user_returns_none(audited, monkeypatch):
    connection = _FakeConnection(None, None)
    monkeypatch.setattr(users, "connect", lambda: connection)
    assert (
        users.update_profile(ALICE_ID, {"title": "Analyst"}, actor_id=ADMIN_ID) is None
    )
    assert audited == []


# CLI bootstrap


@pytest.fixture
def bootstrap(monkeypatch):
    recorded = {"bootstrap": []}
    state = {"admin_exists": False, "error": None}
    monkeypatch.setattr(admin, "admin_exists", lambda: state["admin_exists"])

    def fake_bootstrap(username, password, email):
        recorded["bootstrap"].append((username, password, email))
        if state["error"]:
            raise state["error"]
        return "00000000-0000-0000-0000-000000000001"

    monkeypatch.setattr(admin, "bootstrap_admin", fake_bootstrap)
    return recorded, state


def feed_stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def test_bootstrap_prompts_for_the_email_on_the_second_stdin_line(
    bootstrap, monkeypatch
):
    recorded, _ = bootstrap
    feed_stdin(monkeypatch, PASSWORD + "\n" + "  " + EMAIL + "  \n")
    assert admin.main(["create-user", "--username", "root-op", "--role", "admin"]) == 0
    assert recorded["bootstrap"] == [("root-op", PASSWORD, EMAIL)]


def test_bootstrap_takes_the_email_from_the_option_without_reading_it(
    bootstrap, monkeypatch
):
    recorded, _ = bootstrap
    feed_stdin(monkeypatch, PASSWORD + "\n")
    code = admin.main(
        ["create-user", "--username", "root-op", "--role", "admin", "--email", EMAIL]
    )
    assert code == 0
    assert recorded["bootstrap"] == [("root-op", PASSWORD, EMAIL)]


def test_bootstrap_prompts_on_a_terminal(bootstrap, monkeypatch):
    recorded, _ = bootstrap

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(admin.getpass, "getpass", lambda prompt: PASSWORD)
    monkeypatch.setattr("builtins.input", lambda prompt="": EMAIL)
    assert admin.main(["create-user", "--username", "root-op", "--role", "admin"]) == 0
    assert recorded["bootstrap"] == [("root-op", PASSWORD, EMAIL)]


@pytest.mark.parametrize(
    "email", ["not-an-email", "x@localhost", LONG_LOCAL + "@example.com"]
)
def test_invalid_email_option_is_refused_before_anything_is_created(
    bootstrap, monkeypatch, capsys, email
):
    recorded, _ = bootstrap
    feed_stdin(monkeypatch, PASSWORD + "\n")
    code = admin.main(
        ["create-user", "--username", "root-op", "--role", "admin", "--email", email]
    )
    assert code == 2
    assert recorded["bootstrap"] == []
    assert "invalid email address" in capsys.readouterr().err


def test_invalid_email_on_stdin_is_refused_before_anything_is_created(
    bootstrap, monkeypatch, capsys
):
    recorded, _ = bootstrap
    feed_stdin(monkeypatch, PASSWORD + "\n" + "not an address\n")
    assert admin.main(["create-user", "--username", "root-op", "--role", "admin"]) == 2
    assert recorded["bootstrap"] == []
    assert "invalid email address" in capsys.readouterr().err


def test_missing_email_on_stdin_is_refused(bootstrap, monkeypatch, capsys):
    recorded, _ = bootstrap
    feed_stdin(monkeypatch, PASSWORD + "\n")
    assert admin.main(["create-user", "--username", "root-op", "--role", "admin"]) == 2
    assert recorded["bootstrap"] == []
    assert "email is required" in capsys.readouterr().err


def test_bootstrap_email_already_in_use_is_reported(bootstrap, monkeypatch, capsys):
    recorded, state = bootstrap
    state["error"] = EmailAlreadyInUse()
    feed_stdin(monkeypatch, PASSWORD + "\n" + EMAIL + "\n")
    assert admin.main(["create-user", "--username", "root-op", "--role", "admin"]) == 1
    assert "email already in use" in capsys.readouterr().err


# Database


@pytest.fixture(scope="module")
def migrated_database():
    if os.environ.get("SAFE_TEST_DATABASE") != "1":
        pytest.skip("database tests are disabled")
    migrate.MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
    assert migrate.main() == 0


def _unique_email(prefix: str = "p") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


def _new_user(role: str = "reporter", email: str | None = None) -> str:
    return users.create_user_with_profile(
        "t-" + uuid.uuid4().hex[:12],
        PASSWORD,
        role,
        email if email is not None else _unique_email(),
    )


def _audit_rows(target_id: str, action: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT actor_id::text, details FROM audit_events "
            "WHERE target_id = %s AND action = %s AND NOT is_deleted ORDER BY created_at",
            (target_id, action),
        ).fetchall()


@requires_database
@pytest.mark.usefixtures("migrated_database")
def test_database_duplicate_email_differing_only_in_case_is_rejected():
    email = _unique_email("dup")
    _new_user(email=email)
    with pytest.raises(EmailAlreadyInUse):
        users.create_user_with_profile(
            "t-" + uuid.uuid4().hex[:12], PASSWORD, "reporter", email.upper()
        )


@requires_database
@pytest.mark.usefixtures("migrated_database")
def test_database_email_is_released_by_soft_delete():
    email = _unique_email("gone")
    first = _new_user(email=email)
    assert users.soft_delete_user(first, actor_id=None) == users.UpdateOutcome.OK
    second = users.create_user_with_profile(
        "t-" + uuid.uuid4().hex[:12], PASSWORD, "reporter", email.upper()
    )
    assert users.find_user_by_id(second).email == email.upper()


@requires_database
@pytest.mark.usefixtures("migrated_database")
def test_database_create_requires_an_email_and_writes_nothing_without_one():
    username = "t-" + uuid.uuid4().hex[:12]
    with pytest.raises(InvalidEmail, match="^email is required$"):
        users.create_user_with_profile(username, PASSWORD, "reporter", "  ")
    assert users.find_user(username) is None


@requires_database
@pytest.mark.usefixtures("migrated_database")
def test_database_self_update_through_the_api_and_audit_has_no_values():
    user_id = _new_user()
    new_email = _unique_email("self")
    app = FastAPI()
    app.include_router(profile_api.router)
    app.dependency_overrides[auth.current_user] = lambda: users.find_user_by_id(user_id)
    client = TestClient(app)

    response = client.patch(
        "/api/v1/auth/profile",
        json={"first_name": "Ayse", "email": new_email, "title": "Analyst"},
    )
    assert response.status_code == 200
    stored = users.find_user_by_id(user_id)
    assert (stored.first_name, stored.email, stored.title) == (
        "Ayse",
        new_email,
        "Analyst",
    )

    rows = _audit_rows(user_id, "user.profile_updated")
    assert len(rows) == 1
    actor, details = rows[0]
    assert actor == user_id
    assert sorted(details["fields"]) == ["email", "first_name", "title"]
    assert new_email not in repr(details) and "Ayse" not in repr(details)


@requires_database
@pytest.mark.usefixtures("migrated_database")
def test_database_email_cannot_be_cleared_or_duplicated_through_the_admin_path(
    monkeypatch,
):
    admin_id = _new_user(role="admin")
    target_email = _unique_email("target")
    target_id = _new_user(email=target_email)
    other_id = _new_user()
    monkeypatch.setattr(
        auth,
        "permissions_for_role",
        lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()),
    )
    app = FastAPI()
    app.include_router(profile_api.router)
    app.dependency_overrides[auth.current_user] = lambda: users.find_user_by_id(
        admin_id
    )
    client = TestClient(app)

    assert (
        client.patch(
            f"/api/v1/users/{target_id}/profile", json={"email": None}
        ).status_code
        == 422
    )
    duplicate = client.patch(
        f"/api/v1/users/{other_id}/profile", json={"email": target_email.upper()}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "email already in use"

    assert users.find_user_by_id(target_id).email == target_email
    assert users.find_user_by_id(other_id).email is not None
    rows = _audit_rows(other_id, "user.profile_updated")
    assert rows == []  # the duplicate was refused, so nothing was written or audited

    ok = client.patch(f"/api/v1/users/{other_id}/profile", json={"title": "Reviewer"})
    assert ok.status_code == 200
    assert _audit_rows(other_id, "user.profile_updated")[-1][0] == admin_id
