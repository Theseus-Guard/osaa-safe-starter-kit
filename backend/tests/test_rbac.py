import pathlib

import pytest

from safe_kit import rbac
from safe_kit.rbac import (
    ADMIN_REQUIRED_PERMISSIONS,
    DEFAULT_ROLE_PERMISSIONS,
    FIXED_REPORTER_CONTACT_GRANT,
    REPORTING_OWNER_ROLE,
    RoleOutcome,
    set_role_permissions,
)
from safe_kit.users import ROLES

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"


def test_every_role_has_a_default_entry():
    assert set(DEFAULT_ROLE_PERMISSIONS) == set(ROLES)


def test_admin_defaults_keep_required_permissions_and_no_case_access():
    admin = DEFAULT_ROLE_PERMISSIONS["admin"]
    assert ADMIN_REQUIRED_PERMISSIONS <= admin
    assert not any(permission.startswith("cases:") for permission in admin)


@pytest.mark.parametrize(
    "role",
    ["reporter", "triager", "investigator", "reviewer", "disclosure_approver", "auditor", "reporting_owner"],
)
def test_non_admin_default_roles_cannot_manage_users_or_roles(role):
    assert not DEFAULT_ROLE_PERMISSIONS[role] & ADMIN_REQUIRED_PERMISSIONS


def test_reporting_owner_keeps_the_fixed_contact_grant_and_the_case_read_it_needs():
    assert REPORTING_OWNER_ROLE == "reporting_owner"
    assert FIXED_REPORTER_CONTACT_GRANT <= DEFAULT_ROLE_PERMISSIONS["reporting_owner"]
    assert DEFAULT_ROLE_PERMISSIONS["reporting_owner"] == frozenset({"cases:read_all", "reporter_contact:read"})


def test_only_reporting_owner_holds_the_contact_permission_by_default():
    holders = {role for role, permissions in DEFAULT_ROLE_PERMISSIONS.items() if "reporter_contact:read" in permissions}
    assert holders == {"reporting_owner"}


@pytest.mark.parametrize("role", ["reporter", "triager", "investigator", "reviewer", "disclosure_approver", "auditor"])
def test_other_roles_are_refused_the_contact_permission(role):
    outcome = set_role_permissions(role, frozenset(DEFAULT_ROLE_PERMISSIONS[role] | {"reporter_contact:read"}))
    assert outcome is RoleOutcome.REPORTER_CONTACT_NOT_GRANTABLE


class _ReachedDatabase(Exception):
    """Raised by the fake connect below: the request got past validation and would have written."""


@pytest.fixture
def database_must_not_be_reached(monkeypatch):
    def refuse_connect():
        raise AssertionError("the refusal must happen before any database access")

    monkeypatch.setattr(rbac, "connect", refuse_connect)


@pytest.fixture
def database_is_reached(monkeypatch):
    def reached_connect():
        raise _ReachedDatabase()

    monkeypatch.setattr(rbac, "connect", reached_connect)


@pytest.mark.parametrize(
    "permissions",
    [
        frozenset({"cases:read_all"}),  # drops the contact grant
        frozenset({"reporter_contact:read"}),  # drops cases:read_all
        frozenset(),  # empties the role
        frozenset({"cases:read_all", "reporter_contact:read", "reporter_contact:write"}),  # adds a contact permission
        frozenset({"cases:read_all", "reporter_contact:read", "cases:triage"}),  # adds a non-contact permission
        frozenset({"cases:read_all", "reporter_contact:read", "not:a_permission"}),  # names an unknown permission
    ],
)
def test_any_change_to_reporting_owner_is_refused(permissions, database_must_not_be_reached):
    assert set_role_permissions(REPORTING_OWNER_ROLE, permissions) is RoleOutcome.REPORTING_OWNER_FIXED


def test_saving_the_unchanged_set_of_reporting_owner_is_refused_too(database_must_not_be_reached):
    current = DEFAULT_ROLE_PERMISSIONS[REPORTING_OWNER_ROLE]
    assert set_role_permissions(REPORTING_OWNER_ROLE, frozenset(current)) is RoleOutcome.REPORTING_OWNER_FIXED


@pytest.mark.parametrize(
    "role",
    ["reporter", "triager", "investigator", "reviewer", "disclosure_approver", "auditor", "admin"],
)
def test_other_roles_with_a_valid_request_reach_the_database(role, database_is_reached):
    # The fixed-role refusal does not touch other roles: a valid request proceeds to the write.
    with pytest.raises(_ReachedDatabase):
        set_role_permissions(role, DEFAULT_ROLE_PERMISSIONS[role])


def test_other_roles_still_refuse_a_contact_permission_before_the_database(database_must_not_be_reached):
    outcome = set_role_permissions("triager", frozenset({"cases:triage", "reporter_contact:read"}))
    assert outcome is RoleOutcome.REPORTER_CONTACT_NOT_GRANTABLE


@pytest.mark.parametrize("permission", ["cases:triage", "cases:assign", "cases:assignable", "evidence:read", "reviews:write"])
def test_reporting_owner_holds_no_triage_assign_evidence_or_review_permission(permission):
    assert permission not in DEFAULT_ROLE_PERMISSIONS["reporting_owner"]


def test_migration_seed_matches_the_defaults():
    seed = "\n".join(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("*.sql")))
    for role, permissions in DEFAULT_ROLE_PERMISSIONS.items():
        for permission in permissions:
            assert f"('{role}', '{permission}')" in seed, (role, permission)


@pytest.mark.parametrize("role", ["investigator", "reviewer", "triager"])
def test_roles_that_work_on_cases_may_read_evidence(role):
    assert "evidence:read" in DEFAULT_ROLE_PERMISSIONS[role]


@pytest.mark.parametrize("role", ["reporter", "disclosure_approver", "auditor", "admin"])
def test_other_roles_may_not_read_evidence(role):
    assert "evidence:read" not in DEFAULT_ROLE_PERMISSIONS[role]


def test_only_the_investigator_may_upload_evidence():
    holders = {role for role, permissions in DEFAULT_ROLE_PERMISSIONS.items() if "evidence:write" in permissions}
    assert holders == {"investigator"}
