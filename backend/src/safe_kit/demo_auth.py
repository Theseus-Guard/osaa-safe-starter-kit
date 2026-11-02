"""Demo sign-in: the demo administrator signs in with the per-install demo credential.

Demo sign-in works only when all of these hold: the process passes the startup guard in demo mode with the synthetic
dataset (config_guard.is_demo), the request comes from a loopback address, and the database holds the dataset marker
row written by the seed (migration 0043). The credential is checked against the hash in that row with the password
verifier. The API then issues the same session as a password login. No default credential exists anywhere.
"""

from collections.abc import Mapping

from safe_kit.config_guard import SYNTHETIC_DEMO_DATASET, is_demo, is_loopback
from safe_kit.db import connect
from safe_kit.security import verify_password

# Fixed identifiers of the synthetic demo dataset. The seed creates these rows; the sign-in reads them.
DEMO_ADMIN_ID = "5afe0d00-0000-4000-8000-000000000001"
DEMO_ADMIN_USERNAME = "demo-admin"


def dataset_credential_hash() -> str | None:
    """The stored hash of the demo credential, or None when the database holds no demo dataset."""
    with connect() as conn:
        row = conn.execute(
            "SELECT credential_hash FROM demo_dataset WHERE dataset = %s AND NOT is_deleted",
            (SYNTHETIC_DEMO_DATASET,),
        ).fetchone()
    return row[0] if row else None


def credential_matches(credential: str) -> bool:
    """True when the credential is the one the seed generated for this install."""
    stored = dataset_credential_hash()
    return stored is not None and verify_password(stored, credential)


def demo_sign_in_allowed(env: Mapping[str, str], client_address: str | None) -> bool:
    """Route-level check that needs no database: demo mode and a loopback caller."""
    return is_demo(env) and is_loopback(client_address)
