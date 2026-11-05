"""The disclosure approver's case read, after migration 0056 (F3, owner decisions 2026-11-03; product manager recommendation).

Migration 0044 gave the approver cases:read_all. Migration 0056 soft-deletes that grant, so the approver reads only the
packet it approves through the disclosure read path (safe_kit.disclosure, access_for). The approver holds no case
permission and no other case write.

No database is needed here: the role seed, the migration text and the refusal dependency are checked directly. The
database checks (the reads, the refusals through the real app, and the migrated role_permissions rows) are in
test_approver_case_read_db.py.
"""

import pathlib
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from safe_kit import auth
from safe_kit.rbac import (
    DEFAULT_ROLE_PERMISSIONS,
    DISCLOSURE_APPROVER_ROLE,
    INSTITUTION_OWNER_ROLE,
)

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
APPROVER_PERMISSIONS = frozenset({"disclosures:approve", "disclosures:read"})

# Permissions the approver must not hold: no assignment, triage, close, evidence, review, contact, audit or user rights.
NOT_HELD_BY_APPROVER = (
    "cases:read_assigned",
    "cases:read_own",
    "cases:submit",
    "cases:triage",
    "cases:assign",
    "cases:assignable",
    "cases:close",
    "evidence:read",
    "evidence:write",
    "reviews:write",
    "reporter_contact:read",
    "audit:read",
    "users:manage",
    "users:reset_password",
    "roles:manage",
    "privacy:read",
)


def test_the_approver_seed_has_no_case_read_and_no_case_write():
    assert DEFAULT_ROLE_PERMISSIONS[DISCLOSURE_APPROVER_ROLE] == APPROVER_PERMISSIONS
    assert not APPROVER_PERMISSIONS & set(NOT_HELD_BY_APPROVER)
    assert not any(
        permission.startswith("cases:") for permission in APPROVER_PERMISSIONS
    )


def test_only_triager_and_reporting_owner_hold_read_all_by_default():
    holders = {
        role
        for role, permissions in DEFAULT_ROLE_PERMISSIONS.items()
        if "cases:read_all" in permissions
    }
    assert holders == {"triager", "reporting_owner", INSTITUTION_OWNER_ROLE}


def test_migration_0056_soft_deletes_exactly_the_approver_case_read_row():
    text = (MIGRATIONS / "0056_approver_packet_read.sql").read_text(encoding="utf-8")
    assert "role = 'disclosure_approver' AND permission = 'cases:read_all'" in text
    lowered = text.lower()
    assert "update role_permissions" in lowered
    assert "is_deleted = true" in lowered and "deleted_at = now()" in lowered
    assert "delete from" not in lowered and "truncate" not in lowered


def test_migration_0044_adds_exactly_the_approver_case_read_row():
    text = (MIGRATIONS / "0044_approver_case_read.sql").read_text(encoding="utf-8")
    rows = re.findall(r"\('([a-z_]+)', '([a-z_:]+)'\)", text)
    assert rows == [("disclosure_approver", "cases:read_all")]
    lowered = text.lower()
    assert (
        "delete" not in lowered
        and "truncate" not in lowered
        and "update " not in lowered
    )


@pytest.fixture
def seeded_permissions(monkeypatch):
    monkeypatch.setattr(
        auth,
        "permissions_for_role",
        lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()),
    )


def _deadline_writer():
    return auth.require_any_permission_except_role(
        DISCLOSURE_APPROVER_ROLE, "cases:read_all", "cases:read_assigned"
    )


def test_the_approver_is_refused_the_deadline_routes_even_with_read_all(
    seeded_permissions,
):
    user = SimpleNamespace(id="approver", role=DISCLOSURE_APPROVER_ROLE)
    with pytest.raises(HTTPException) as caught:
        _deadline_writer()(user)
    assert caught.value.status_code == 403
    assert caught.value.detail == "forbidden"


@pytest.mark.parametrize("role", ["triager", "reporting_owner", "investigator"])
def test_other_roles_that_hold_the_permission_still_pass(seeded_permissions, role):
    user = SimpleNamespace(id="someone", role=role)
    assert _deadline_writer()(user) is user


def test_a_role_without_the_permission_is_refused_as_before(seeded_permissions):
    user = SimpleNamespace(id="reporter", role="reporter")
    with pytest.raises(HTTPException) as caught:
        _deadline_writer()(user)
    assert caught.value.status_code == 403
