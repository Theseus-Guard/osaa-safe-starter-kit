import pytest

from safe_kit.rbac import PERMISSIONS, has_permission
from safe_kit.users import ROLES


def test_every_role_has_a_permission_entry():
    assert set(PERMISSIONS) == set(ROLES)


def test_admin_manages_users_but_reads_no_case_content():
    assert has_permission("admin", "users:manage")
    assert has_permission("admin", "users:reset_password")
    assert not any(permission.startswith("cases:") for permission in PERMISSIONS["admin"])


def test_only_admin_can_reset_passwords():
    holders = {role for role in PERMISSIONS if has_permission(role, "users:reset_password")}
    assert holders == {"admin"}


@pytest.mark.parametrize("role", ["reporter", "triager", "investigator", "reviewer", "disclosure_approver", "auditor"])
def test_non_admin_roles_cannot_manage_users(role):
    assert not has_permission(role, "users:manage")
    assert not has_permission(role, "users:reset_password")


def test_unknown_role_has_no_permissions():
    assert not has_permission("root", "users:manage")
