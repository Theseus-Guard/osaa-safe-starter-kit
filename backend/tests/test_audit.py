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


def test_login_failure_stores_reason_username_and_client_address(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(audit, "connect", lambda: conn)
    audit.record_login_failure("user-7", "wrong_password", "alice", "203.0.113.9")
    sql, params = conn.statements[0]
    assert params[1] == audit.LOGIN_FAILED
    assert params[3] == "user-7"
    assert params[4].obj == {"reason": "wrong_password", "username": "alice", "client_ip": "203.0.113.9"}
    assert not any("password" in key for key in params[4].obj)


def test_login_failure_for_unknown_user_has_no_target_id(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(audit, "connect", lambda: conn)
    audit.record_login_failure(None, "unknown_user", "nobody", "203.0.113.9")
    _, params = conn.statements[0]
    assert params[3] is None


def test_login_failure_with_an_ip_address_as_the_submitted_name_stores_no_name(monkeypatch):
    # The submitted name is untrusted input. An address in it must neither break the login nor reach the log.
    conn = FakeConnection()
    monkeypatch.setattr(audit, "connect", lambda: conn)
    audit.record_login_failure(None, "unknown_user", "203.0.113.9", "203.0.113.9")
    _, params = conn.statements[0]
    assert params[4].obj == {"reason": "unknown_user", "username": None, "client_ip": "203.0.113.9"}


def test_login_failed_may_carry_username_and_client_address():
    audit.check_details(
        {"reason": "wrong_password", "username": "alice", "client_ip": "203.0.113.9"},
        audit.LOGIN_FAILED,
    )


def test_login_failed_still_rejects_secret_keys():
    with pytest.raises(ValueError):
        audit.check_details({"reason": "wrong_password", "password": "x"}, audit.LOGIN_FAILED)
    with pytest.raises(ValueError):
        audit.check_details({"username": "alice", "refresh_token": "x"}, audit.LOGIN_FAILED)
    with pytest.raises(ValueError):
        audit.check_details({"client_ip": "203.0.113.9", "password_hash": "x"}, audit.LOGIN_FAILED)


def test_login_failed_rejects_an_address_under_any_other_key():
    with pytest.raises(ValueError):
        audit.check_details({"reason": "wrong_password", "note": "203.0.113.9"}, audit.LOGIN_FAILED)
    with pytest.raises(ValueError):
        audit.check_details({"username": "203.0.113.9"}, audit.LOGIN_FAILED)


def test_login_failed_relaxes_only_the_top_level_keys():
    with pytest.raises(ValueError):
        audit.check_details({"reason": "x", "changes": {"client_ip": "x"}}, audit.LOGIN_FAILED)


@pytest.mark.parametrize("action", [audit.USER_UPDATE, audit.CASE_CREATE, audit.SYSTEM_TIMEZONE_SET])
def test_other_actions_reject_username_and_client_address_keys(action):
    with pytest.raises(ValueError):
        audit.check_details({"username": "alice"}, action)
    with pytest.raises(ValueError):
        audit.check_details({"client_ip": "x"}, action)


@pytest.mark.parametrize("action", [audit.USER_UPDATE, audit.LOGIN_FAILED, audit.SYSTEM_TIMEZONE_CHANGED])
def test_raw_ip_values_are_rejected_under_every_other_key(action):
    with pytest.raises(ValueError):
        audit.check_details({"note": "203.0.113.9"}, action)
    with pytest.raises(ValueError):
        audit.check_details({"note": ["2001:db8::1"]}, action)


def test_password_keys_are_rejected_under_every_action():
    for action in (audit.USER_UPDATE, audit.LOGIN_FAILED, audit.SYSTEM_TIMEZONE_SET, None):
        with pytest.raises(ValueError):
            audit.check_details({"password": "x"}, action)
