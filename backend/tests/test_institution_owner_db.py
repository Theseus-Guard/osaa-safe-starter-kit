"""F3 institution owner, approver packet read, backup approval and waiting-time changes (database).

Owner decisions 2026-11-03, product manager recommendation applied:
- the institution owner (institution_owner, migration 0055) alone assigns the backup pool and changes the waiting time;
- the disclosure approver reads only the packet it approves and the missing facts of that packet (migration 0056);
- an approve decision of a backup writes the approval record of the version;
- a recipient change needs a new version and a new approval; the same content and recipient give NO_CHANGE.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import json
import uuid
from datetime import datetime, timedelta

from safe_kit import audit
from safe_kit import external_notice_backup as backup
from safe_kit import rbac
from safe_kit import users
from safe_kit.db import connect
from safe_kit.users import OwnerOutcome, change_institution_owner
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_a6_backup import (
    after_the_waiting_time,
    approve_body,
    audit_for,
    decide,
    decisions_of,
)
from test_fixture_p6_disclosure import (
    APPROVED_RECIPIENT_REF,
    approve,
    body,
    case_url,
    create,
    first_version,
)
from test_fixture_t06a_redaction import world  # world: fixture
from test_fixture_t12a_records import (
    escalate,
    escalation_values,
    pool_url,
    review,
    set_waiting_hours,
    waiting_hours,
)

REASON = "Owner review of the backup rota after the staffing change"
OWNER_KEYS = {"id", "version_id", "missing_facts", "escalated_at"}


def owner_pool(dw) -> dict:
    """The institution owner sets the pool of two disclosure approvers for the case."""
    owner = db_user("institution_owner")
    members = [db_user("disclosure_approver"), db_user("disclosure_approver")]
    response = request_as(owner, "put", pool_url(dw), json={"backup_user_ids": members})
    assert response.status_code == 200, response.text
    return {**dw, "backups": members, "owner": owner}


def with_approver(dw) -> dict:
    """The world with a disclosure approver who is not a backup, under people["approver"]."""
    return {
        **dw,
        "people": {**dw["people"], "approver": db_user("disclosure_approver")},
    }


def pooled_escalation(dw, monkeypatch) -> tuple[dict, dict, dict]:
    """A version prepared by the triager, and an escalation of it to the first backup of an owner-set pool."""
    dw = owner_pool(dw)
    version, derived = first_version(dw)
    response = escalate(
        dw, "triager", version["id"], escalation_values(dw["backups"][0])
    )
    assert response.status_code == 201, response.text
    return dw, version, response.json()


def approval_rows(version_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT approved_by::text, preparer_id::text, digest, recipient, recipient_ref
            FROM disclosure_approvals WHERE version_id = %s AND NOT is_deleted
            """,
            (version_id,),
        ).fetchall()


def settings_changes() -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT old_value, new_value, reason, changed_by::text
            FROM external_notice_setting_changes WHERE NOT is_deleted ORDER BY changed_at, id
            """
        ).fetchall()


def wait_url() -> str:
    return "/api/v1/notice-settings/backup-wait"


# --- the role rows -------------------------------------------------------------------------------------------------


@requires_database
def test_the_migrated_rows_give_the_owner_rights_to_the_owner_only(world):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rp.role, rp.permission
            FROM role_permissions rp
            JOIN permissions p ON p.name = rp.permission AND NOT p.is_deleted
            WHERE NOT rp.is_deleted AND rp.permission IN ('disclosures:assign_backup', 'disclosures:set_backup_wait')
            ORDER BY rp.role, rp.permission
            """
        ).fetchall()
        owner_rows = conn.execute(
            "SELECT rp.permission FROM role_permissions rp WHERE rp.role = 'institution_owner' AND NOT rp.is_deleted "
            "ORDER BY rp.permission"
        ).fetchall()
        approver_rows = conn.execute(
            "SELECT rp.permission FROM role_permissions rp WHERE rp.role = 'disclosure_approver' AND NOT rp.is_deleted "
            "ORDER BY rp.permission"
        ).fetchall()
    assert rows == [
        ("institution_owner", "disclosures:assign_backup"),
        ("institution_owner", "disclosures:set_backup_wait"),
    ]
    assert [row[0] for row in owner_rows] == [
        "cases:read_all",
        "disclosures:assign_backup",
        "disclosures:read",
        "disclosures:set_backup_wait",
    ]
    assert [row[0] for row in approver_rows] == [
        "disclosures:approve",
        "disclosures:read",
    ]


@requires_database
def test_the_matrix_refuses_the_owner_rights_for_other_roles_and_keeps_them_for_the_owner(
    world,
):
    assert (
        rbac.set_role_permissions("triager", frozenset({"disclosures:assign_backup"}))
        is rbac.RoleOutcome.OWNER_PERMISSION_NOT_GRANTABLE
    )
    assert (
        rbac.set_role_permissions("institution_owner", frozenset({"disclosures:read"}))
        is rbac.RoleOutcome.OWNER_GUARD
    )


# --- the backup pool (institution owner) -------------------------------------------------------------------------------


@requires_database
def test_the_owner_sets_the_pool_and_the_administrator_and_the_approver_cannot(world):
    owner = db_user("institution_owner")
    members = [db_user("disclosure_approver"), db_user("disclosure_approver")]
    url = pool_url(world)
    assert (
        request_as(
            db_user("admin"), "put", url, json={"backup_user_ids": members}
        ).status_code
        == 403
    )
    assert (
        request_as(
            db_user("disclosure_approver"),
            "put",
            url,
            json={"backup_user_ids": members},
        ).status_code
        == 403
    )
    response = request_as(owner, "put", url, json={"backup_user_ids": members})
    assert response.status_code == 200, response.text
    assert sorted(item["backup_user_id"] for item in response.json()["items"]) == sorted(members)
    assigned = audit_for(audit.DISCLOSURE_BACKUP_ASSIGNED, world["case_id"])
    assert sorted(entry["backup_user_id"] for entry in assigned) == sorted(members)


# --- the waiting time (institution owner) -------------------------------------------------------------------------------


@requires_database
def test_the_owner_changes_the_waiting_time_with_a_reason_and_the_change_is_audited(
    world,
):
    owner = db_user("institution_owner")
    try:
        response = request_as(
            owner, "put", wait_url(), json={"hours": 6, "reason": REASON}
        )
        assert response.status_code == 200, response.text
        assert response.json()["hours"] == 6
        assert response.json()["change"]["old_value"] == 4
        assert waiting_hours() == 6

        history = settings_changes()
        assert history == [(4, 6, REASON, owner)]
        with connect() as conn:
            details = conn.execute(
                "SELECT details::text FROM audit_events WHERE action = %s AND actor_id = %s ORDER BY created_at, id",
                (audit.DISCLOSURE_BACKUP_WAIT_CHANGED, owner),
            ).fetchall()
        assert len(details) == 1
        payload = json.loads(details[0][0])
        assert payload == {
            "key": "backup_wait_hours",
            "old_value": 4,
            "new_value": 6,
            "reason": {"length": len(REASON)},
        }
        assert REASON not in details[0][0]

        again = request_as(
            owner, "put", wait_url(), json={"hours": 6, "reason": REASON}
        )
        assert again.status_code == 409
        assert again.json()["detail"] == {"status": "value_unchanged", "hours": 6}

        read = request_as(owner, "get", wait_url())
        assert read.status_code == 200
        assert read.json()["hours"] == 6
        assert read.json()["changes"][0]["reason"] == REASON
        assert request_as(db_user("admin"), "get", wait_url()).status_code == 403
        assert (
            request_as(
                db_user("admin"), "put", wait_url(), json={"hours": 2, "reason": REASON}
            ).status_code
            == 403
        )
        assert waiting_hours() == 6
    finally:
        set_waiting_hours(4)


@requires_database
def test_an_escalation_copies_the_new_time_and_an_open_one_keeps_its_own(
    world, monkeypatch
):
    owner = db_user("institution_owner")
    dw, version, before = pooled_escalation(world, monkeypatch)
    try:
        response = request_as(
            owner, "put", wait_url(), json={"hours": 2, "reason": REASON}
        )
        assert response.status_code == 200, response.text
        later = escalate(
            dw, "triager", version["id"], escalation_values(dw["backups"][1])
        )
        assert later.status_code == 201, later.text
        opened = datetime.fromisoformat(later.json()["backup_may_act_at"])
        escalated = datetime.fromisoformat(later.json()["escalated_at"])
        assert opened - escalated == timedelta(hours=2)
        earlier = datetime.fromisoformat(before["backup_may_act_at"])
        still = request_as(
            dw["owner"],
            "get",
            f"/api/v1/cases/{dw['case_id']}/disclosures/{version['id']}/escalations",
        ).json()["items"]
        kept = next(item for item in still if item["id"] == before["id"])
        assert datetime.fromisoformat(kept["backup_may_act_at"]) == earlier
    finally:
        set_waiting_hours(4)


# --- the approver's packet read ------------------------------------------------------------------------------------------


@requires_database
def test_the_approver_reads_the_packet_it_approves_and_not_the_case(world):
    dw = with_approver(world)
    approver = dw["people"]["approver"]
    version, _ = first_version(dw)
    assert (
        request_as(approver, "get", f"/api/v1/cases/{dw['case_id']}").status_code == 403
    )
    listed = request_as(approver, "get", case_url(dw))
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [version["id"]]
    assert (
        request_as(approver, "get", case_url(dw, f"/{version['id']}")).status_code
        == 200
    )
    assert (
        request_as(
            approver, "get", case_url(dw, f"/{version['id']}/preview")
        ).status_code
        == 200
    )
    unknown_case = f"/api/v1/cases/{uuid.uuid4()}/disclosures/{version['id']}"
    assert (
        request_as(approver, "get", unknown_case).json()["detail"] == "case not found"
    )


@requires_database
def test_the_approver_approves_its_packet_and_still_reads_it_afterwards(world):
    dw = with_approver(world)
    approver = dw["people"]["approver"]
    version, _ = first_version(dw)
    approved = approve(
        dw,
        "approver",
        version["id"],
        version["recipient"],
        version["purpose"],
        version["digest"],
    )
    assert approved.status_code == 201, approved.text
    assert approved.json()["approval_state"] == "approved"
    assert (
        request_as(approver, "get", case_url(dw, f"/{version['id']}")).status_code
        == 200
    )
    recipient_review = review(
        dw,
        "approver",
        version["id"],
        "recipient",
        {"recipient_ref": APPROVED_RECIPIENT_REF},
    )
    assert recipient_review.status_code == 201, recipient_review.text


@requires_database
def test_the_approver_exports_its_approved_packet_and_not_another_case(world):
    dw = with_approver(world)
    approver = dw["people"]["approver"]
    version, _ = first_version(dw)
    approve(
        dw,
        "approver",
        version["id"],
        version["recipient"],
        version["purpose"],
        version["digest"],
    )
    exported = request_as(approver, "get", case_url(dw, f"/{version['id']}/export"))
    assert exported.status_code == 200, exported.text
    assert exported.json()["disclosure_export"]["digest"] == version["digest"]
    unknown_case = f"/api/v1/cases/{uuid.uuid4()}/disclosures/{version['id']}/export"
    assert request_as(approver, "get", unknown_case).status_code == 404


@requires_database
def test_the_approver_sees_the_missing_facts_of_its_packet_only(world, monkeypatch):
    dw, version, escalation = pooled_escalation(world, monkeypatch)
    dw = with_approver(dw)
    approver = dw["people"]["approver"]
    url = f"/api/v1/cases/{dw['case_id']}/disclosures/{version['id']}/escalations"
    facts = request_as(approver, "get", url)
    assert facts.status_code == 200, facts.text
    assert [set(item) for item in facts.json()["items"]] == [OWNER_KEYS]
    full = request_as(dw["owner"], "get", url).json()["items"][0]
    assert full["backup_user_id"] == dw["backups"][0]
    reviews = request_as(
        approver,
        "get",
        f"/api/v1/cases/{dw['case_id']}/disclosures/{version['id']}/reviews",
    )
    assert reviews.status_code == 404


@requires_database
def test_the_approver_gets_no_packet_of_another_case_or_a_superseded_unapproved_one(
    world,
):
    dw = with_approver(world)
    approver = dw["people"]["approver"]
    first, derived = first_version(dw)
    second = create(
        dw,
        "triager",
        body(
            [derived],
            package_id=first["package_id"],
            expected_version=1,
            purpose="A new purpose",
        ),
    )
    assert second.status_code == 201, second.text
    # The first version is no longer the latest and nobody approved it: the approver may not read it.
    assert (
        request_as(approver, "get", case_url(dw, f"/{first['id']}")).status_code == 404
    )
    assert (
        request_as(approver, "get", case_url(dw, f"/{second.json()['id']}")).status_code
        == 200
    )


# --- the backup's approval record -----------------------------------------------------------------------------------------


@requires_database
def test_a_backup_approval_writes_the_approval_record_and_derives_the_state(
    world, monkeypatch
):
    dw, version, escalation = pooled_escalation(world, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    backup_user = dw["backups"][0]
    response = decide(
        dw, backup_user, version["id"], escalation["id"], approve_body(version["id"])
    )
    assert response.status_code == 201, response.text

    rows = approval_rows(version["id"])
    assert rows == [
        (
            backup_user,
            version["created_by"],
            version["digest"],
            version["recipient"],
            "cert-07",
        )
    ]
    view = request_as(dw["owner"], "get", case_url(dw, f"/{version['id']}")).json()
    assert view["approval_state"] == "approved"
    assert view["approval"]["approved_by"] == backup_user
    approvals = audit_for(audit.DISCLOSURE_APPROVAL, dw["case_id"])
    assert approvals[-1]["escalation_id"] == escalation["id"]
    assert approvals[-1]["digest"] == version["digest"]


@requires_database
def test_a_backup_approval_is_refused_after_the_primary_approval_and_writes_nothing(
    world, monkeypatch
):
    dw, version, escalation = pooled_escalation(world, monkeypatch)
    dw = with_approver(dw)
    primary = approve(
        dw,
        "approver",
        version["id"],
        version["recipient"],
        version["purpose"],
        version["digest"],
    )
    assert primary.status_code == 201, primary.text
    after_the_waiting_time(monkeypatch, escalation)
    response = decide(
        dw,
        dw["backups"][0],
        version["id"],
        escalation["id"],
        approve_body(version["id"]),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"status": "already_approved"}
    assert decisions_of(escalation["id"]) == []
    assert len(approval_rows(version["id"])) == 1


@requires_database
def test_a_backup_approval_with_other_values_is_refused_and_writes_nothing(
    world, monkeypatch
):
    dw, version, escalation = pooled_escalation(world, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    wrong = approve_body(version["id"])
    wrong["approval"]["digest"] = "0" * 64
    response = decide(dw, dw["backups"][0], version["id"], escalation["id"], wrong)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"status": "approval_binding_mismatch"}
    assert decisions_of(escalation["id"]) == []
    assert approval_rows(version["id"]) == []


@requires_database
def test_a_reject_or_an_emergency_notice_writes_no_approval_record(world, monkeypatch):
    dw, version, escalation = pooled_escalation(world, monkeypatch)
    after_the_waiting_time(monkeypatch, escalation)
    response = decide(
        dw,
        dw["backups"][0],
        version["id"],
        escalation["id"],
        {"decision": "reject", "reason": REASON},
    )
    assert response.status_code == 201, response.text
    assert approval_rows(version["id"]) == []


# --- recipient change: a new version, a new approval, NO_CHANGE for the same content ----------------------------------


@requires_database
def test_a_recipient_change_needs_a_new_version_and_a_new_approval(world):
    dw = with_approver(world)
    approver = dw["people"]["approver"]
    first, derived = first_version(dw)
    assert (
        approve(
            dw,
            "approver",
            first["id"],
            first["recipient"],
            first["purpose"],
            first["digest"],
        ).status_code
        == 201
    )

    changed = create(
        dw,
        "triager",
        body(
            [derived],
            package_id=first["package_id"],
            expected_version=1,
            recipient="Other CERT",
        ),
    )
    assert changed.status_code == 201, changed.text
    second = changed.json()
    assert (
        request_as(approver, "get", case_url(dw, f"/{first['id']}")).json()[
            "approval_state"
        ]
        == "revoked_by_change"
    )
    assert second["approval_state"] == "not_approved"

    # Without an approval of the new version, a recipient review is refused.
    assert (
        review(
            dw,
            "approver",
            second["id"],
            "recipient",
            {"recipient_ref": APPROVED_RECIPIENT_REF},
        ).status_code
        == 409
    )

    # The same content and recipient again: nothing new is stored.
    same = create(
        dw,
        "triager",
        body(
            [derived],
            package_id=first["package_id"],
            expected_version=2,
            recipient="Other CERT",
        ),
    )
    assert same.status_code == 409, same.text
    assert same.json()["detail"] == {"status": "no_change"}

    assert (
        approve(
            dw,
            "approver",
            second["id"],
            "Other CERT",
            second["purpose"],
            second["digest"],
        ).status_code
        == 201
    )
    mismatch = review(
        dw, "approver", second["id"], "recipient", {"recipient_ref": "cert-99"}
    )
    assert mismatch.status_code == 422, mismatch.text
    assert mismatch.json()["detail"] == {"status": "recipient_mismatch"}
    accepted = review(
        dw,
        "approver",
        second["id"],
        "recipient",
        {"recipient_ref": APPROVED_RECIPIENT_REF},
    )
    assert accepted.status_code == 201, accepted.text


# --- the operator's grant and revoke ----------------------------------------------------------------------------------


@requires_database
def test_the_operator_grants_and_revokes_the_owner_role_with_a_reason_and_an_audit_row(world):
    user = db_user("triager")
    granted = change_institution_owner(
        user, grant=True, role=None, reason="Runs the notice process"
    )
    assert granted is OwnerOutcome.OK
    assert users.find_user_by_id(user).role == "institution_owner"
    assert (
        change_institution_owner(user, grant=True, role=None, reason="again")
        is OwnerOutcome.ALREADY_OWNER
    )
    revoked = change_institution_owner(user, grant=False, role="auditor", reason="Moves to audit")
    assert revoked is OwnerOutcome.OK
    assert users.find_user_by_id(user).role == "auditor"
    assert (
        change_institution_owner(user, grant=False, role="auditor", reason="again")
        is OwnerOutcome.NOT_OWNER
    )
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT action, details FROM audit_events
            WHERE target_id = %s AND action IN ('user.owner_granted', 'user.owner_revoked') AND NOT is_deleted
            ORDER BY created_at, id
            """,
            (user,),
        ).fetchall()
    assert [row[0] for row in rows] == ["user.owner_granted", "user.owner_revoked"]
    assert rows[0][1] == {
        "from": "triager",
        "to": "institution_owner",
        "reason": "Runs the notice process",
        "source": "cli",
    }
    assert rows[1][1]["to"] == "auditor"


@requires_database
def test_the_operator_cannot_revoke_a_role_that_is_not_the_owner_role_or_grant_an_inactive_account(world):
    user = db_user("triager")
    assert change_institution_owner(user, grant=False, role="auditor", reason="x") is OwnerOutcome.NOT_OWNER
    assert users.find_user_by_id(user).role == "triager"
    assert change_institution_owner(user, grant=False, role="institution_owner", reason="x") is OwnerOutcome.NOT_OWNER
    disabled = db_user("triager")
    assert users.update_user(disabled, is_active=False) is users.UpdateOutcome.OK
    assert change_institution_owner(disabled, grant=True, role=None, reason="x") is OwnerOutcome.INACTIVE
    assert users.find_user_by_id(disabled).role == "triager"
