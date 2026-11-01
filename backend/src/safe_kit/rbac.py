"""Role-to-permission mapping.

Roles are fixed. Permissions are declared here, in code, so that a change to who can do what
goes through review like any other change. Case-level access (ACL) is a separate check that
will be added with the case domain; a permission alone never grants access to a specific case.
"""

PERMISSIONS: dict[str, frozenset[str]] = {
    "reporter": frozenset({"cases:submit", "cases:read_own"}),
    "triager": frozenset({"cases:read_assigned", "cases:triage"}),
    "investigator": frozenset({"cases:read_assigned", "evidence:write"}),
    "reviewer": frozenset({"cases:read_assigned", "reviews:write"}),
    "disclosure_approver": frozenset({"disclosures:approve"}),
    "auditor": frozenset({"audit:read"}),
    # By design the admin role has no case permissions: platform administration
    # does not grant access to case content.
    "admin": frozenset({"users:manage", "users:reset_password"}),
}


def has_permission(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, frozenset())
