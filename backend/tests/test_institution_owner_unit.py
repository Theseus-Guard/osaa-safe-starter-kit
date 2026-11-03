"""F3 institution owner and approver packet read (owner decisions 2026-11-03; product manager recommendation applied).

No database is needed here. The role seed, the role matrix refusals (checked before any database access), the user
administration guard, the waiting-time routes (their service is replaced), the approver's narrow escalation view and the
migration text are checked directly. The database checks are in test_institution_owner_db.py.
"""

import pathlib
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, audit, auth
from safe_kit import external_notice_backup as backup
from safe_kit import external_notice_records_api as records_api
from safe_kit.cases import CaseScope
from safe_kit.external_notice_records import Escalation
from safe_kit.rbac import (
    ADMIN_REQUIRED_PERMISSIONS,
    DEFAULT_ROLE_PERMISSIONS,
    INSTITUTION_OWNER_ROLE,
    OWNER_PERMISSIONS,
    REPORTING_OWNER_ROLE,
    RoleOutcome,
    set_role_permissions,
)
from safe_kit.users import ROLES, User

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
NOW = datetime(2026, 11, 3, 9, tzinfo=UTC)
ADMIN_ID = "11111111-1111-1111-1111-111111111111"
OWNER_ID = "22222222-2222-2222-2222-222222222222"
APPROVER_ID = "33333333-3333-3333-3333-333333333333"
CASE_ID = "44444444-4444-4444-4444-444444444444"
REASON = "Owner review of the backup rota after the staffing change"


def make_user(user_id: str, role: str) -> User:
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
    state = {"user": make_user(ADMIN_ID, "admin")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(
        auth,
        "permissions_for_role",
        lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()),
    )
    yield state
    api.app.dependency_overrides.pop(auth.current_user, None)


@pytest.fixture
def client(caller):
    return TestClient(api.app)


def as_user(caller, user_id: str, role: str) -> None:
    caller["user"] = make_user(user_id, role)


# --- the role seed and the role matrix (no database) -------------------------------------------------------------------


def test_the_institution_owner_role_is_known_and_holds_the_owner_rights():
    assert INSTITUTION_OWNER_ROLE in ROLES
    holder = DEFAULT_ROLE_PERMISSIONS[INSTITUTION_OWNER_ROLE]
    assert OWNER_PERMISSIONS <= holder
    assert "disclosures:approve" not in holder
    assert not any(
        permission.startswith(("evidence:", "reviews:", "reporter_contact:"))
        for permission in holder
    )


def test_only_the_institution_owner_holds_the_owner_rights_by_default():
    holders = {
        role
        for role, permissions in DEFAULT_ROLE_PERMISSIONS.items()
        if permissions & OWNER_PERMISSIONS
    }
    assert holders == {INSTITUTION_OWNER_ROLE}


def test_the_administrator_and_the_approver_hold_no_owner_right_and_no_case_read():
    for role in ("admin", "disclosure_approver"):
        assert not DEFAULT_ROLE_PERMISSIONS[role] & OWNER_PERMISSIONS
    assert not any(
        permission.startswith("cases:")
        for permission in DEFAULT_ROLE_PERMISSIONS["disclosure_approver"]
    )
    assert "disclosures:approve" in DEFAULT_ROLE_PERMISSIONS["disclosure_approver"]


@pytest.mark.parametrize(
    "role",
    [
        "reporter",
        "triager",
        "investigator",
        "reviewer",
        "disclosure_approver",
        "auditor",
    ],
)
def test_the_role_matrix_refuses_an_owner_right_for_every_other_role(role):
    outcome = set_role_permissions(
        role, frozenset({"disclosures:assign_backup"}), actor_id=ADMIN_ID
    )
    assert outcome is RoleOutcome.OWNER_PERMISSION_NOT_GRANTABLE


def test_the_role_matrix_refuses_the_set_wait_right_for_another_role_too():
    outcome = set_role_permissions(
        "triager", frozenset({"disclosures:set_backup_wait"}), actor_id=ADMIN_ID
    )
    assert outcome is RoleOutcome.OWNER_PERMISSION_NOT_GRANTABLE


def test_the_administrator_is_refused_the_owner_right_by_its_case_content_rule():
    outcome = set_role_permissions(
        "admin",
        ADMIN_REQUIRED_PERMISSIONS | {"disclosures:assign_backup"},
        actor_id=ADMIN_ID,
    )
    assert outcome is RoleOutcome.ADMIN_CASE_CONTENT


def test_the_institution_owner_keeps_its_owner_rights_in_the_matrix():
    outcome = set_role_permissions(
        INSTITUTION_OWNER_ROLE, frozenset({"disclosures:read"}), actor_id=ADMIN_ID
    )
    assert outcome is RoleOutcome.OWNER_GUARD


def test_the_reporting_owner_refusal_still_comes_first():
    outcome = set_role_permissions(REPORTING_OWNER_ROLE, frozenset(), actor_id=ADMIN_ID)
    assert outcome is RoleOutcome.REPORTING_OWNER_FIXED


def test_the_owner_audit_action_is_named_and_distinct():
    assert audit.DISCLOSURE_BACKUP_WAIT_CHANGED == "disclosure.backup_wait_changed"
    assert audit.DISCLOSURE_BACKUP_WAIT_CHANGED != audit.DISCLOSURE_BACKUP_DECISION


# --- the user administration guard (one check, the route refuses before any change) --------------------------------------


def test_an_administrator_cannot_create_an_institution_owner(
    client, caller, monkeypatch
):
    monkeypatch.setattr(
        api,
        "create_user_with_profile",
        lambda *args, **kwargs: pytest.fail("must not create the account"),
    )
    body = {
        "username": "owner-candidate",
        "role": INSTITUTION_OWNER_ROLE,
        "temporary_password": "temporary passphrase for the owner",
    }
    response = client.post("/api/v1/users", json=body)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {"status": "institution_owner_not_assignable"}


@pytest.mark.parametrize("role", ["admin", INSTITUTION_OWNER_ROLE])
def test_nobody_creates_an_institution_owner_through_the_user_administration(
    client, caller, monkeypatch, role
):
    # The owner role is set outside the application, so no caller of the user routes can create or give it.
    as_user(caller, ADMIN_ID if role == "admin" else OWNER_ID, role)
    monkeypatch.setattr(
        api,
        "create_user_with_profile",
        lambda *args, **kwargs: pytest.fail("must not create the account"),
    )
    body = {
        "username": "second-owner",
        "role": INSTITUTION_OWNER_ROLE,
        "temporary_password": "temporary passphrase for the owner",
    }
    response = client.post("/api/v1/users", json=body)
    assert response.status_code == 403, response.text
    # The owner holds no users:manage, so the route refuses it before the owner rule; the answer is the same refusal.
    if role == "admin":
        assert response.json()["detail"] == {
            "status": "institution_owner_not_assignable"
        }


def test_an_administrator_cannot_give_or_take_the_owner_role_by_a_change(
    client, caller, monkeypatch
):
    monkeypatch.setattr(
        api,
        "find_user_by_id",
        lambda user_id: SimpleNamespace(role="triager", revision=1),
    )
    monkeypatch.setattr(
        api,
        "update_user",
        lambda *args, **kwargs: pytest.fail("must not change the account"),
    )
    response = client.patch(
        f"/api/v1/users/{APPROVER_ID}",
        json={"role": INSTITUTION_OWNER_ROLE},
        headers={"If-Match": '"1"'},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {"status": "institution_owner_not_assignable"}


def test_an_administrator_cannot_change_an_owner_account(client, caller, monkeypatch):
    monkeypatch.setattr(
        api,
        "find_user_by_id",
        lambda user_id: SimpleNamespace(role=INSTITUTION_OWNER_ROLE, revision=1),
    )
    monkeypatch.setattr(
        api,
        "update_user",
        lambda *args, **kwargs: pytest.fail("must not change the account"),
    )
    response = client.patch(
        f"/api/v1/users/{OWNER_ID}",
        json={"is_active": False},
        headers={"If-Match": '"1"'},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {"status": "institution_owner_not_assignable"}

    monkeypatch.setattr(
        api,
        "soft_delete_user",
        lambda *args, **kwargs: pytest.fail("must not delete the account"),
    )
    assert client.delete(f"/api/v1/users/{OWNER_ID}").status_code == 403


# --- the waiting time routes (the service is replaced; authority and body are checked) -----------------------------------


def _change_result(hours: int):
    return backup.Result(
        backup.Outcome.OK,
        value=backup.WaitChange(
            id="66666666-6666-6666-6666-666666666666",
            setting_key="backup_wait_hours",
            old_value=4,
            new_value=hours,
            reason=REASON,
            changed_by=OWNER_ID,
            changed_at=NOW,
        ),
    )


def test_only_the_institution_owner_reads_or_changes_the_waiting_time(
    client, caller, monkeypatch
):
    monkeypatch.setattr(
        backup,
        "set_wait_hours",
        lambda *args, **kwargs: pytest.fail(
            "the administrator must not reach the change"
        ),
    )
    body = {"hours": 6, "reason": REASON}
    assert (
        client.put("/api/v1/notice-settings/backup-wait", json=body).status_code == 403
    )
    assert client.get("/api/v1/notice-settings/backup-wait").status_code == 403

    as_user(caller, APPROVER_ID, "disclosure_approver")
    assert (
        client.put("/api/v1/notice-settings/backup-wait", json=body).status_code == 403
    )


def test_the_owner_changes_the_waiting_time_with_a_reason(client, caller, monkeypatch):
    as_user(caller, OWNER_ID, INSTITUTION_OWNER_ROLE)
    seen = {}

    def fake_set(hours, reason, actor_id):
        seen.update(hours=hours, reason=reason, actor_id=actor_id)
        return _change_result(hours)

    monkeypatch.setattr(backup, "set_wait_hours", fake_set)
    monkeypatch.setattr(
        backup, "list_wait_changes", lambda limit: [_change_result(6).value]
    )
    monkeypatch.setattr(backup, "current_wait_hours", lambda: 6)

    response = client.put(
        "/api/v1/notice-settings/backup-wait",
        json={"hours": 6, "reason": f"  {REASON}  "},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["key"] == "backup_wait_hours"
    assert body["hours"] == 6
    assert body["change"]["old_value"] == 4 and body["change"]["new_value"] == 6
    assert seen == {"hours": 6, "reason": REASON, "actor_id": OWNER_ID}


def test_the_waiting_time_is_bounded_and_needs_a_reason(client, caller, monkeypatch):
    as_user(caller, OWNER_ID, INSTITUTION_OWNER_ROLE)
    monkeypatch.setattr(
        backup,
        "set_wait_hours",
        lambda *args, **kwargs: pytest.fail(
            "an invalid body must not reach the service"
        ),
    )
    for body in (
        {"hours": 0, "reason": REASON},
        {"hours": 169, "reason": REASON},
        {"hours": 6},
        {"hours": 6, "reason": "   "},
        {"hours": 6, "reason": REASON, "key": "x"},
    ):
        # The application answers a validation error with 400 (api.validation_error_handler).
        assert (
            client.put("/api/v1/notice-settings/backup-wait", json=body).status_code
            == 400
        )


def test_an_unchanged_waiting_time_is_refused(client, caller, monkeypatch):
    as_user(caller, OWNER_ID, INSTITUTION_OWNER_ROLE)
    monkeypatch.setattr(
        backup,
        "set_wait_hours",
        lambda hours, reason, actor_id: backup.Result(
            backup.Outcome.VALUE_UNCHANGED, value=4
        ),
    )
    response = client.put(
        "/api/v1/notice-settings/backup-wait", json={"hours": 4, "reason": REASON}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "value_unchanged", "hours": 4}


def test_the_only_writer_of_the_waiting_time_is_the_audited_function():
    # Application code changes the setting in one place only: safe_kit.external_notice_backup.set_wait_hours.
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "safe_kit"
    writers = {
        path.name
        for path in src.glob("*.py")
        if re.search(
            r"UPDATE\s+external_notice_settings", path.read_text(encoding="utf-8")
        )
    }
    assert writers == {"external_notice_backup.py"}


# --- the approver's narrow views and the approver scope (no database) ----------------------------------------------------


def test_the_approver_sees_an_escalation_as_missing_facts_only():
    escalation = Escalation(
        id="77777777-7777-7777-7777-777777777777",
        case_id=CASE_ID,
        package_id="88888888-8888-8888-8888-888888888888",
        version_id="99999999-9999-9999-9999-999999999999",
        reason_code="unreachable",
        authority_basis="Incident rota",
        missing_facts="The member contact is not confirmed",
        next_update="Same working day",
        backup_user_id=APPROVER_ID,
        escalated_by=ADMIN_ID,
        escalated_at=NOW,
        backup_may_act_at=NOW,
    )
    view = records_api._escalation_packet_view(escalation)
    assert set(view) == {"id", "version_id", "missing_facts", "escalated_at"}
    assert view["missing_facts"] == "The member contact is not confirmed"


def test_the_approver_flag_does_not_change_the_case_rule():
    approver = CaseScope.from_permissions(
        APPROVER_ID, frozenset({"disclosures:approve"})
    )
    plain = CaseScope.from_permissions(APPROVER_ID, frozenset({"disclosures:read"}))
    assert approver.approver is True and plain.approver is False
    assert approver.visibility_sql() == plain.visibility_sql()
    assert approver.visibility_sql(writing=True) == plain.visibility_sql(writing=True)


# --- migrations (text checks: additive, soft delete, insert-only) ---------------------------------------------------------


def test_migration_0055_adds_the_role_to_both_checks_and_the_owner_rows_only():
    text = (MIGRATIONS / "0055_institution_owner_role.sql").read_text(encoding="utf-8")
    assert text.count("'institution_owner'") >= 6
    assert "DROP CONSTRAINT users_role_check" in text
    assert "DROP CONSTRAINT role_permissions_role_check" in text
    rows = re.findall(r"\('institution_owner', '([a-z_:]+)'\)", text)
    assert sorted(rows) == sorted(
        [
            "disclosures:assign_backup",
            "disclosures:set_backup_wait",
            "disclosures:read",
            "cases:read_all",
        ]
    )
    lowered = text.lower()
    assert "delete from" not in lowered and "truncate" not in lowered


def test_migration_0057_keeps_the_waiting_time_history_insert_only():
    text = (MIGRATIONS / "0057_backup_wait_changes.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE external_notice_setting_changes" in text
    assert "reject_disclosure_content_change" in text
    assert "is_deleted" in text and "deleted_at" in text
    assert "delete from" not in text.lower() and "truncate" not in text.lower()
