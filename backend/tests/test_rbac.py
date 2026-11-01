import pathlib

import pytest

from safe_kit.rbac import ADMIN_REQUIRED_PERMISSIONS, DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import ROLES

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"


def test_every_role_has_a_default_entry():
    assert set(DEFAULT_ROLE_PERMISSIONS) == set(ROLES)


def test_admin_defaults_keep_required_permissions_and_no_case_access():
    admin = DEFAULT_ROLE_PERMISSIONS["admin"]
    assert ADMIN_REQUIRED_PERMISSIONS <= admin
    assert not any(permission.startswith("cases:") for permission in admin)


@pytest.mark.parametrize("role", ["reporter", "triager", "investigator", "reviewer", "disclosure_approver", "auditor"])
def test_non_admin_default_roles_cannot_manage_users_or_roles(role):
    assert not DEFAULT_ROLE_PERMISSIONS[role] & ADMIN_REQUIRED_PERMISSIONS


def test_migration_seed_matches_the_defaults():
    seed = "\n".join(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("*.sql")))
    for role, permissions in DEFAULT_ROLE_PERMISSIONS.items():
        for permission in permissions:
            assert f"('{role}', '{permission}')" in seed, (role, permission)
