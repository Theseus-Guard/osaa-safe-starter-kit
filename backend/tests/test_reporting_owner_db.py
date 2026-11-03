"""F1 reporting_owner role (owner decision): the seed, the fixed contact grant, the contact read, and the audit of the
role assignment.

The contact is readable only by reporting_owner. The administrator and the triager are refused, and the reporting owner
can assign no case and triage none. The database tests use the helpers of tests/test_reporter_channel.py and are
skipped unless SAFE_TEST_DATABASE=1.
"""

import uuid
import pytest

from safe_kit import audit
from safe_kit.db import connect
from safe_kit.migrate import main as migrate
from safe_kit.rbac import RoleOutcome, list_role_permissions, set_role_permissions

# Fixtures and helpers are imported from the sibling test module and requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
from if_match import user_if_match
from test_reporter_channel import (
    CONTACT,
    contact_audit_rows,
    db_client,
    new_user,
    open_report_via_api,
    people,
    requires_database,
    system_timezone_configured,
)


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


@requires_database
def test_migration_seeds_the_role_and_its_two_grants():
    migrate()
    assert list_role_permissions()["reporting_owner"] == [
        "cases:read_all",
        "reporter_contact:read",
    ]


@requires_database
def test_the_users_role_check_accepts_reporting_owner():
    account = new_user("reporting_owner")
    with connect() as conn:
        role = conn.execute(
            "SELECT role FROM users WHERE id = %s", (account,)
        ).fetchone()[0]
    assert role == "reporting_owner"


@requires_database
def test_saving_the_fixed_set_again_is_refused_and_changes_nothing():
    migrate()
    before = list_role_permissions()["reporting_owner"]
    assert (
        set_role_permissions("reporting_owner", frozenset(before))
        is RoleOutcome.REPORTING_OWNER_FIXED
    )
    assert list_role_permissions()["reporting_owner"] == before


@requires_database
def test_the_matrix_refuses_to_remove_the_grant_and_keeps_it():
    migrate()
    outcome = set_role_permissions("reporting_owner", frozenset({"cases:read_all"}))
    assert outcome is RoleOutcome.REPORTING_OWNER_FIXED
    assert "reporter_contact:read" in list_role_permissions()["reporting_owner"]


@requires_database
def test_the_matrix_refuses_to_change_the_role_at_all():
    migrate()
    before = list_role_permissions()["reporting_owner"]
    outcome = set_role_permissions(
        "reporting_owner", frozenset({"cases:read_all", "cases:triage"})
    )
    assert outcome is RoleOutcome.REPORTING_OWNER_FIXED
    assert list_role_permissions()["reporting_owner"] == before


@requires_database
def test_the_matrix_does_not_add_back_a_grant_removed_by_hand():
    migrate()
    with connect() as conn:
        conn.execute(
            "UPDATE role_permissions SET is_deleted = true, deleted_at = now() "
            "WHERE role = 'reporting_owner' AND permission = 'reporter_contact:read' AND NOT is_deleted"
        )
    try:
        outcome = set_role_permissions(
            "reporting_owner", frozenset({"cases:read_all", "reporter_contact:read"})
        )
        assert outcome is RoleOutcome.REPORTING_OWNER_FIXED
        assert "reporter_contact:read" not in list_role_permissions()["reporting_owner"]
    finally:
        with connect() as conn:
            conn.execute(
                "UPDATE role_permissions SET is_deleted = false, deleted_at = NULL "
                "WHERE role = 'reporting_owner' AND permission = 'reporter_contact:read'"
            )


@requires_database
def test_reporting_owner_reads_the_contact_of_an_open_unassigned_case(
    db_client, people
):
    case_id = open_report_via_api(db_client, people["reporter"])
    owner = new_user("reporting_owner")

    response = db_client(owner).get(f"/api/v1/reports/{case_id}/contact")

    assert response.status_code == 200
    assert response.json()["contact"] == CONTACT
    assert contact_audit_rows(case_id) == [(owner, "case", case_id)]


@requires_database
def test_reporting_owner_can_see_the_case_it_reads(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    owner = new_user("reporting_owner")

    assert db_client(owner).get(f"/api/v1/cases/{case_id}").status_code == 200


@requires_database
@pytest.mark.parametrize("role", ["admin", "triager"])
def test_admin_and_triager_are_refused_the_contact_and_nothing_is_audited(
    db_client, people, role
):
    case_id = open_report_via_api(db_client, people["reporter"])
    caller = new_user(role)

    response = db_client(caller).get(f"/api/v1/reports/{case_id}/contact")

    assert response.status_code == 403
    assert contact_audit_rows(case_id) == []


@requires_database
def test_reporting_owner_cannot_assign_the_case(db_client, people):
    case_id = open_report_via_api(db_client, people["reporter"])
    owner = new_user("reporting_owner")

    response = db_client(owner).post(
        f"/api/v1/cases/{case_id}/assignments",
        json={"user_id": people["investigator"]},
        headers=idem(),
    )

    assert response.status_code == 403


@requires_database
def test_admin_assigns_the_reporting_owner_role_and_the_change_is_audited(db_client):
    admin = new_user("admin")
    target = new_user("auditor")

    response = db_client(admin).patch(
        f"/api/v1/users/{target}",
        json={"role": "reporting_owner"},
        headers=user_if_match(db_client(admin), target),
    )

    assert response.status_code == 200
    assert response.json()["role"] == "reporting_owner"
    with connect() as conn:
        rows = conn.execute(
            "SELECT actor_id::text, details FROM audit_events WHERE action = %s AND target_id = %s AND NOT is_deleted "
            "ORDER BY created_at",
            (audit.USER_UPDATE, target),
        ).fetchall()
    assert rows == [
        (admin, {"changes": {"role": {"from": "auditor", "to": "reporting_owner"}}})
    ]
