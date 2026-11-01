import pytest

from safe_kit import audit


class FakeConnection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "new_password",
        "temporary_password",
        "password_hash",
        "refresh_token",
        "access_token",
        "token",
        "jwt_secret",
        "client_ip",
        "ip",
        "ip_address",
        "x-real-ip",
        "remote_addr",
        "api_key_hash",
    ],
)
def test_secret_and_network_keys_are_rejected(key):
    with pytest.raises(ValueError):
        audit.check_details({key: "anything"})


def test_nested_secret_key_is_rejected():
    with pytest.raises(ValueError):
        audit.check_details({"changes": {"password_hash": {"from": "x", "to": "y"}}})


@pytest.mark.parametrize("value", ["203.0.113.9", "2001:db8::1", " 10.0.0.1 "])
def test_ip_address_values_are_rejected(value):
    with pytest.raises(ValueError):
        audit.check_details({"note": value})


@pytest.mark.parametrize(
    "details",
    [
        {"role": "triager"},
        {"changes": {"role": {"from": "reporter", "to": "auditor"}, "is_active": {"from": True, "to": False}}},
        {"granted": ["cases:read_own"], "revoked": []},
        {"source": "cli"},
        # Words that only contain "ip" as a substring are not network keys.
        {"description": "ordinary text", "shipment": 3},
    ],
)
def test_ordinary_details_are_accepted(details):
    audit.check_details(details)


def test_non_json_value_is_rejected():
    with pytest.raises(TypeError):
        audit.check_details({"when": object()})


def test_record_writes_one_row_on_the_callers_connection():
    conn = FakeConnection()
    audit.record(conn, audit.USER_UPDATE, "user", "user-1", actor_id="actor-1", details={"role": "auditor"})
    assert len(conn.statements) == 1
    sql, params = conn.statements[0]
    assert sql.startswith("INSERT INTO audit_events")
    assert params[:4] == ("actor-1", "user.update", "user", "user-1")


def test_record_refuses_secret_details_before_touching_the_database():
    conn = FakeConnection()
    with pytest.raises(ValueError):
        audit.record(conn, audit.USER_PASSWORD_RESET, "user", "user-1", details={"password": "x"})
    assert conn.statements == []


def test_login_failure_stores_reason_and_account_only(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(audit, "connect", lambda: conn)
    audit.record_login_failure("user-7", "wrong_password")
    sql, params = conn.statements[0]
    assert params[1] == audit.LOGIN_FAILED
    assert params[3] == "user-7"
    assert params[4].obj == {"reason": "wrong_password"}


def test_login_failure_for_unknown_user_has_no_target_id(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(audit, "connect", lambda: conn)
    audit.record_login_failure(None, "unknown_user")
    _, params = conn.statements[0]
    assert params[3] is None
