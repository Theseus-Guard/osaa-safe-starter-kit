"""Roles and permissions, stored in PostgreSQL and editable by administrators at runtime.

Each request reads the permissions of the caller's role from the database, so a change takes effect
on the next request without a new login. DEFAULT_ROLE_PERMISSIONS is the seed used by migration 0007.
It is kept in code only so that a test can check the seed against the migration.
"""

from enum import Enum

from safe_kit import audit
from safe_kit.db import connect
from safe_kit.users import ROLES

# The admin role must always keep these, so the system cannot lock itself out of administration.
ADMIN_REQUIRED_PERMISSIONS = frozenset({"users:manage", "users:reset_password", "roles:manage"})

ROLE_LOCK_KEY = 0x5AFE0004

DEFAULT_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "reporter": frozenset({"cases:submit", "cases:read_own"}),
    "triager": frozenset(
        {"cases:read_assigned", "cases:read_all", "cases:triage", "cases:assign", "cases:close", "evidence:read"}
    ),
    "investigator": frozenset({"cases:read_assigned", "cases:assignable", "evidence:read", "evidence:write"}),
    "reviewer": frozenset({"cases:read_assigned", "cases:assignable", "evidence:read", "reviews:write"}),
    "disclosure_approver": frozenset({"disclosures:approve"}),
    "auditor": frozenset({"audit:read"}),
    # By design the admin role has no case permissions: platform administration
    # does not grant access to case content.
    "admin": ADMIN_REQUIRED_PERMISSIONS | {"privacy:read"},
}


class RoleOutcome(Enum):
    OK = "ok"
    ADMIN_GUARD = "admin_guard"
    ADMIN_CASE_CONTENT = "admin_case_content"
    REPORTER_CONTACT_NOT_GRANTABLE = "reporter_contact_not_grantable"
    UNKNOWN_PERMISSION = "unknown_permission"


# Permissions that read or change case content, including the reporter contact. The admin role never holds them:
# platform administration does not reach case content (see DEFAULT_ROLE_PERMISSIONS and plans/01, R08).
CASE_CONTENT_PERMISSION_PREFIXES = ("cases:", "evidence:", "reviews:", "reporter_contact:", "disclosures:")
# No role may hold a reporter contact permission. The contact is read only by the holders decided in F0/F2, so the
# matrix refuses it for every role, administrators included, whatever the matrix contains today.
REPORTER_CONTACT_PREFIX = "reporter_contact:"


def permissions_for_role(role: str) -> frozenset[str]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rp.permission
            FROM role_permissions rp
            JOIN permissions p ON p.name = rp.permission AND NOT p.is_deleted
            WHERE rp.role = %s AND NOT rp.is_deleted
            """,
            (role,),
        ).fetchall()
    return frozenset(row[0] for row in rows)


def list_catalog() -> list[tuple[str, str]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT name, description FROM permissions WHERE NOT is_deleted ORDER BY name"
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def list_role_permissions() -> dict[str, list[str]]:
    matrix: dict[str, list[str]] = {role: [] for role in sorted(ROLES)}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rp.role, rp.permission
            FROM role_permissions rp
            JOIN permissions p ON p.name = rp.permission AND NOT p.is_deleted
            WHERE NOT rp.is_deleted
            ORDER BY rp.role, rp.permission
            """
        ).fetchall()
    for role, permission in rows:
        matrix[role].append(permission)
    return matrix


def set_role_permissions(role: str, permissions: frozenset[str], actor_id: str | None = None) -> RoleOutcome:
    """Replace the permission set of a role. Granting is an undelete; revoking is a soft delete.

    The audit row lists the granted and revoked permissions and commits with the change.
    """
    if role == "admin":
        if not ADMIN_REQUIRED_PERMISSIONS <= permissions:
            return RoleOutcome.ADMIN_GUARD
        if any(name.startswith(CASE_CONTENT_PERMISSION_PREFIXES) for name in permissions):
            return RoleOutcome.ADMIN_CASE_CONTENT
    if any(name.startswith(REPORTER_CONTACT_PREFIX) for name in permissions):
        return RoleOutcome.REPORTER_CONTACT_NOT_GRANTABLE
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ROLE_LOCK_KEY,))
        known = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM permissions WHERE NOT is_deleted AND name = ANY(%s::text[])",
                (list(permissions),),
            ).fetchall()
        }
        if known != set(permissions):
            return RoleOutcome.UNKNOWN_PERMISSION
        current = {
            row[0]
            for row in conn.execute(
                "SELECT permission FROM role_permissions WHERE role = %s AND NOT is_deleted",
                (role,),
            ).fetchall()
        }
        granted = permissions - current
        revoked = current - permissions
        for permission in sorted(granted):
            conn.execute(
                """
                INSERT INTO role_permissions (role, permission) VALUES (%s, %s)
                ON CONFLICT (role, permission) DO UPDATE SET is_deleted = false, deleted_at = NULL
                """,
                (role, permission),
            )
        for permission in sorted(revoked):
            conn.execute(
                """
                UPDATE role_permissions SET is_deleted = true, deleted_at = now()
                WHERE role = %s AND permission = %s AND NOT is_deleted
                """,
                (role, permission),
            )
        if granted or revoked:
            audit.record(
                conn,
                audit.ROLE_PERMISSIONS_CHANGE,
                "role",
                role,
                actor_id=actor_id,
                details={"granted": sorted(granted), "revoked": sorted(revoked)},
            )
    return RoleOutcome.OK
