"""Demo sign-in: each demo role signs in with its own per-install demo credential.

Demo sign-in works only when all of these hold: the process passes the startup guard in demo mode with the synthetic
dataset (config_guard.is_demo), the resolved client address is a loopback address (client_address.py: the socket peer,
or the forwarded client when the peer is a trusted proxy), the username is one of the demo roles in DEMO_ROLES, and the
database holds the credential of that user (table demo_user_credentials, migration 0046). The credential is checked with
the password verifier. The API then issues the same session as a password login. No default credential exists anywhere.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from safe_kit.config_guard import demo_host_address, is_demo, is_loopback
from safe_kit.db import connect

# Fixed identifier of the demo administrator. The seed creates the row; the sign-in reads it.
DEMO_ADMIN_ID = "5afe0d00-0000-4000-8000-000000000001"
DEMO_ADMIN_USERNAME = "demo-admin"


@dataclass(frozen=True)
class DemoRole:
    username: str
    role: str
    name: str


# The demo roles, in the order the sign-in screen lists them. Each role holds exactly the permissions of its real role.
DEMO_ROLES = (
    DemoRole(DEMO_ADMIN_USERNAME, "admin", "Demo Administrator"),
    DemoRole("demo-reporter", "reporter", "Demo Reporter"),
    DemoRole("demo-triager", "triager", "Demo Triager"),
    DemoRole("demo-investigator", "investigator", "Demo Investigator"),
    DemoRole("demo-reviewer", "reviewer", "Demo Reviewer"),
    DemoRole("demo-approver", "disclosure_approver", "Demo Approver"),
    DemoRole("demo-auditor", "auditor", "Demo Auditor"),
)
DEMO_USERNAMES = frozenset(demo_role.username for demo_role in DEMO_ROLES)


def demo_credential_hash(user_id: str) -> str | None:
    """The stored hash of the user's demo credential, or None when the user has none (or the seed never ran)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT credential_hash FROM demo_user_credentials WHERE user_id = %s AND NOT is_deleted",
            (user_id,),
        ).fetchone()
    return row[0] if row else None


def demo_sign_in_allowed(env: Mapping[str, str], client_address: str | None) -> bool:
    """Route-level check that needs no database: demo mode and a loopback resolved client address.

    client_address must come from client_address.resolve_client_address, never from a header read here.
    In demo mode the Docker host address from SAFE_DEMO_HOST_ADDRESS also counts as local (see config_guard).
    """
    if not is_demo(env) or client_address is None:
        return False
    return is_loopback(client_address) or client_address == demo_host_address(env)
