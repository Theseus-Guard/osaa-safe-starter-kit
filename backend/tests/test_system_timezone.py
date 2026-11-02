from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.system_timezone import ChangeOutcome, decide, utc_offset, validate_timezone
from safe_kit.users import User

ADMIN_ID = "33333333-3333-3333-3333-333333333333"
ISTANBUL = "Europe/Istanbul"
AUCKLAND = "Pacific/Auckland"


def make_user(user_id, role):
    return User(
        id=user_id,
        username=f"user-{role}",
        role=role,
        is_active=True,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user(ADMIN_ID, "admin")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(caller, timezone_db):
    return TestClient(api.app)


def put(client, timezone, current, confirm=False, **extra):
    body = {"timezone": timezone, "current": current, "confirm": confirm, **extra}
    return client.put("/api/v1/system/timezone", json=body)


# --- validation ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [ISTANBUL, AUCKLAND, "UTC", "America/St_Johns"])
def test_valid_iana_names_are_accepted(name):
    assert validate_timezone(name) == name


@pytest.mark.parametrize("name", ["Mars/Olympus", "", "Europe", "../etc/passwd", "/etc/passwd", "UTC+3", "x" * 65])
def test_invalid_names_are_rejected_with_422(client, timezone_db, name):
    response = put(client, name, current=None)
    assert response.status_code == 400
    assert timezone_db.timezone_rows == []
    assert timezone_db.audit_rows == []


def test_invalid_name_is_rejected_by_the_validator_too():
    with pytest.raises(ValueError):
        validate_timezone("Mars/Olympus")


# --- first set, changes and the confirmation rule -------------------------------------------------------------------


def test_first_set_works_without_confirmation(client, timezone_db):
    response = put(client, ISTANBUL, current=None)
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == ISTANBUL
    assert body["utc_offset"] == "+03:00"
    assert body["effective_from"].endswith(("Z", "+00:00"))
    assert [row["timezone"] for row in timezone_db.timezone_rows] == [ISTANBUL]
    assert timezone_db.timezone_rows[0]["set_by"] == ADMIN_ID


def test_first_set_is_audited_as_timezone_set(client, timezone_db):
    put(client, ISTANBUL, current=None)
    [audit_row] = timezone_db.audit_rows
    assert audit_row["action"] == "system.timezone_set"
    assert audit_row["actor_id"] == ADMIN_ID
    assert audit_row["details"]["from"] is None
    assert audit_row["details"]["to"] == ISTANBUL


def test_change_without_confirmation_is_rejected_with_422(client, timezone_db):
    put(client, ISTANBUL, current=None)
    response = put(client, AUCKLAND, current=ISTANBUL, confirm=False)
    assert response.status_code == 422
    assert response.json()["detail"] == "confirmation required"
    assert len(timezone_db.timezone_rows) == 1
    assert len(timezone_db.audit_rows) == 1


def test_change_with_a_stale_current_value_is_rejected_with_409(client, timezone_db):
    put(client, ISTANBUL, current=None)
    response = put(client, AUCKLAND, current="Europe/Berlin", confirm=True)
    assert response.status_code == 409
    assert response.json()["detail"] == "timezone changed by someone else; reload"
    assert len(timezone_db.timezone_rows) == 1


def test_stale_check_applies_even_to_a_first_set(client, timezone_db):
    put(client, ISTANBUL, current=None)
    assert put(client, AUCKLAND, current=None, confirm=True).status_code == 409


def test_confirmed_change_appends_a_row_and_audits_from_to_and_effective_from(client, timezone_db):
    put(client, ISTANBUL, current=None)
    response = put(client, AUCKLAND, current=ISTANBUL, confirm=True)
    assert response.status_code == 200
    assert response.json()["timezone"] == AUCKLAND

    assert [row["timezone"] for row in timezone_db.timezone_rows] == [ISTANBUL, AUCKLAND]
    [_, audit_row] = timezone_db.audit_rows
    assert audit_row["action"] == "system.timezone_changed"
    assert audit_row["target_type"] == "system_timezone"
    assert audit_row["details"]["from"] == ISTANBUL
    assert audit_row["details"]["to"] == AUCKLAND
    assert audit_row["details"]["effective_from"] == timezone_db.timezone_rows[1]["effective_from"].isoformat()
    assert response.json()["effective_from"].replace("Z", "+00:00") == audit_row["details"]["effective_from"]


def test_unchanged_value_writes_no_row_and_no_audit(client, timezone_db):
    put(client, ISTANBUL, current=None)
    response = put(client, ISTANBUL, current=ISTANBUL, confirm=False)
    assert response.status_code == 200
    assert response.json()["timezone"] == ISTANBUL
    assert len(timezone_db.timezone_rows) == 1
    assert len(timezone_db.audit_rows) == 1


def test_client_cannot_supply_the_effective_time(client, timezone_db):
    response = put(client, ISTANBUL, current=None, effective_from="2001-01-01T00:00:00Z")
    assert response.status_code == 200
    stored = timezone_db.timezone_rows[0]["effective_from"]
    assert stored > datetime.now(UTC) - timedelta(minutes=1)
    assert response.json()["effective_from"].replace("Z", "+00:00") == stored.isoformat()


def test_current_is_required_so_the_client_states_what_it_replaces(client):
    response = client.put("/api/v1/system/timezone", json={"timezone": ISTANBUL})
    assert response.status_code == 400


def test_put_requires_users_manage(client, caller, timezone_db):
    caller["user"] = make_user("44444444-4444-4444-4444-444444444444", "reporter")
    assert put(client, ISTANBUL, current=None).status_code == 403
    assert timezone_db.timezone_rows == []


# --- reading the setting -----------------------------------------------------------------------------------------


def test_get_returns_nulls_while_unset(client):
    response = client.get("/api/v1/system/timezone")
    assert response.status_code == 200
    assert response.json() == {"timezone": None, "effective_from": None, "utc_offset": None}


def test_get_returns_the_timezone_its_effective_time_and_the_offset_now(client, timezone_db):
    timezone_db.seed_timezone(AUCKLAND, datetime(2026, 10, 1, tzinfo=UTC), set_by=ADMIN_ID)
    body = client.get("/api/v1/system/timezone").json()
    assert body["timezone"] == AUCKLAND
    assert body["effective_from"] == "2026-10-01T00:00:00Z"
    assert body["utc_offset"] in {"+12:00", "+13:00"}


def test_future_rows_are_not_in_effect_yet(client, timezone_db):
    timezone_db.seed_timezone(ISTANBUL, datetime(2020, 1, 1, tzinfo=UTC))
    timezone_db.seed_timezone(AUCKLAND, datetime.now(UTC) + timedelta(days=1))
    assert client.get("/api/v1/system/timezone").json()["timezone"] == ISTANBUL


def test_any_authenticated_user_can_read_the_setting(client, caller, timezone_db):
    timezone_db.seed_timezone(ISTANBUL, datetime(2020, 1, 1, tzinfo=UTC))
    caller["user"] = make_user("44444444-4444-4444-4444-444444444444", "reporter")
    assert client.get("/api/v1/system/timezone").json()["timezone"] == ISTANBUL


# --- pure rules -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "current", "timezone", "confirm", "expected"),
    [
        (None, None, ISTANBUL, False, ChangeOutcome.CHANGED),
        (None, None, ISTANBUL, True, ChangeOutcome.CHANGED),
        (ISTANBUL, ISTANBUL, AUCKLAND, False, ChangeOutcome.CONFIRMATION_REQUIRED),
        (ISTANBUL, ISTANBUL, AUCKLAND, True, ChangeOutcome.CHANGED),
        (ISTANBUL, "Europe/Berlin", AUCKLAND, True, ChangeOutcome.STALE),
        (None, ISTANBUL, ISTANBUL, True, ChangeOutcome.STALE),
        (ISTANBUL, ISTANBUL, ISTANBUL, False, ChangeOutcome.UNCHANGED),
    ],
)
def test_decide(stored, current, timezone, confirm, expected):
    assert decide(stored, current, timezone, confirm) is expected


@pytest.mark.parametrize(
    ("timezone", "instant", "expected"),
    [
        (ISTANBUL, datetime(2026, 1, 15, tzinfo=UTC), "+03:00"),
        ("America/St_Johns", datetime(2026, 1, 15, tzinfo=UTC), "-03:30"),
        ("Asia/Kolkata", datetime(2026, 1, 15, tzinfo=UTC), "+05:30"),
        ("America/New_York", datetime(2026, 1, 15, tzinfo=UTC), "-05:00"),
        ("UTC", datetime(2026, 1, 15, tzinfo=UTC), "+00:00"),
    ],
)
def test_utc_offset_is_formatted_for_the_instant(timezone, instant, expected):
    assert utc_offset(timezone, instant) == expected

