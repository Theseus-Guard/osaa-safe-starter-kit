import io
import sys
from types import SimpleNamespace

import psycopg
import pytest

from safe_kit import admin
from safe_kit.users import AdminAlreadyExists, PasswordReused, ReporterAccountResetRefused

PASSWORD = "bootstrap passphrase"
EMAIL = "first.admin@example.com"


@pytest.fixture
def calls(monkeypatch):
    """Replace database calls with recorders. Tests set the outcome they need."""
    recorded = {"bootstrap": [], "reset": []}
    state = {"admin_exists": False, "bootstrap_error": None, "reset_error": None}

    monkeypatch.setattr(admin, "admin_exists", lambda: state["admin_exists"])

    def fake_bootstrap(username, password, email):
        recorded["bootstrap"].append((username, password, email))
        if state["bootstrap_error"]:
            raise state["bootstrap_error"]
        return "00000000-0000-0000-0000-000000000001"

    monkeypatch.setattr(admin, "bootstrap_admin", fake_bootstrap)
    monkeypatch.setattr(admin, "find_user", lambda username: None)

    def fake_reset(*args, **kwargs):
        recorded["reset"].append(args)
        if state["reset_error"]:
            raise state["reset_error"]
        return True

    monkeypatch.setattr(admin, "reset_password", fake_reset)
    return recorded, state


def feed_password(monkeypatch, password, email=EMAIL):
    # Bootstrap reads the password as the first line of stdin and the email as the second.
    monkeypatch.setattr(sys, "stdin", io.StringIO(password + "\n" + email + "\n"))


def test_first_admin_is_created_when_none_exists(calls, monkeypatch, capsys):
    recorded, _ = calls
    feed_password(monkeypatch, PASSWORD)
    assert admin.main(["create-user", "--username", "root-op", "--role", "admin"]) == 0
    assert recorded["bootstrap"] == [("root-op", PASSWORD, EMAIL)]
    assert "password change required" in capsys.readouterr().out


def test_bootstrap_refused_once_an_admin_exists(calls, monkeypatch, capsys):
    recorded, state = calls
    state["admin_exists"] = True
    feed_password(monkeypatch, PASSWORD)
    assert admin.main(["create-user", "--username", "second", "--role", "admin"]) == 1
    assert recorded["bootstrap"] == []
    assert "through the API" in capsys.readouterr().err


def test_race_loser_is_refused(calls, monkeypatch):
    recorded, state = calls
    state["bootstrap_error"] = AdminAlreadyExists()
    feed_password(monkeypatch, PASSWORD)
    assert admin.main(["create-user", "--username", "second", "--role", "admin"]) == 1


def test_only_admin_role_is_allowed_before_the_first_admin(calls, monkeypatch, capsys):
    recorded, _ = calls
    feed_password(monkeypatch, PASSWORD)
    assert admin.main(["create-user", "--username", "alice", "--role", "triager"]) == 2
    assert recorded["bootstrap"] == []
    assert "only --role admin" in capsys.readouterr().err


def test_short_bootstrap_password_is_rejected(calls, monkeypatch):
    recorded, _ = calls
    feed_password(monkeypatch, "short")
    assert admin.main(["create-user", "--username", "root-op", "--role", "admin"]) == 2
    assert recorded["bootstrap"] == []


def test_duplicate_username_during_bootstrap_is_reported(calls, monkeypatch, capsys):
    recorded, state = calls
    state["bootstrap_error"] = psycopg.errors.UniqueViolation("duplicate")
    feed_password(monkeypatch, PASSWORD)
    assert admin.main(["create-user", "--username", "root-op", "--role", "admin"]) == 1
    assert "username already exists" in capsys.readouterr().err


def test_reset_password_resets_the_account(calls, monkeypatch, capsys):
    recorded, _ = calls
    monkeypatch.setattr(admin, "find_user", lambda username: SimpleNamespace(id="00000000-0000-0000-0000-000000000002"))
    feed_password(monkeypatch, PASSWORD)
    assert admin.main(["reset-password", "--username", "alice"]) == 0
    assert recorded["reset"] == [("00000000-0000-0000-0000-000000000002", PASSWORD)]
    assert "password change required" in capsys.readouterr().out


def test_reset_password_refuses_a_reporter_account(calls, monkeypatch, capsys):
    recorded, state = calls
    state["reset_error"] = ReporterAccountResetRefused()
    monkeypatch.setattr(
        admin, "find_user", lambda username: SimpleNamespace(id="00000000-0000-0000-0000-000000000003")
    )
    feed_password(monkeypatch, PASSWORD)
    assert admin.main(["reset-password", "--username", "reporter-one"]) == 1
    err = capsys.readouterr().err
    assert "reporter account" in err
    assert "reporter_account_reset_refused" in err
    assert "password was not changed" in err
    assert "password reset for" not in capsys.readouterr().out


def test_reset_password_refuses_a_recent_password(calls, monkeypatch, capsys):
    recorded, state = calls
    state["reset_error"] = PasswordReused()
    monkeypatch.setattr(admin, "find_user", lambda username: SimpleNamespace(id="00000000-0000-0000-0000-000000000002"))
    feed_password(monkeypatch, PASSWORD)
    assert admin.main(["reset-password", "--username", "alice"]) == 1
    assert "password cannot be reused" in capsys.readouterr().err
