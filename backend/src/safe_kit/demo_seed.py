"""Synthetic demo dataset and the per-role demo credentials (python -m safe_kit.demo_seed seed | upgrade).

The database is seeded in seed versions. Version 1 writes one fixed, small dataset in a single transaction: eight users
(one per role, names prefixed "demo-"), three cases with fixed IDs, text evidence, a review round with layer outcomes, a
catalog entry with a verification run, a disclosure version that is a preview only (no approval), and the demo
administrator's credential. The marker row in demo_dataset is written last, with seed_version 1. Version 2 adds the
credentials of the other demo roles (one file each), and one more case assignment that lets the demo investigator read
the closed case. Version 2 sets the marker to seed_version 2. A fresh database gets both versions in one run; an existing
version-1 database gets version 2 from `upgrade`. A run finds the marker and writes only what the marker says is missing,
so repeated runs create nothing.

Each demo credential is random per install. Its hash is stored (demo_user_credentials, or the marker row for version 1),
and the plain value is written once to a file with mode 0600, created with O_EXCL and never overwritten. The file for the
demo administrator is the one named by SAFE_DEMO_CREDENTIAL_FILE when that is set (the single-file setting of version 1),
otherwise <SAFE_DEMO_CREDENTIAL_DIR>/demo-admin.credential. The other roles use <SAFE_DEMO_CREDENTIAL_DIR>/<username>.credential.
Nothing is printed, logged, or stored anywhere else.

The seed refuses to run unless the startup guard (config_guard.load_settings) passes in demo mode with the synthetic
dataset. It runs inside the API container, like the other administrative commands, and writes the database directly.
"""

import argparse
import hashlib
import os
import secrets
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from safe_kit import system_timezone
from safe_kit.cases import CALENDAR_VERSION, calendar_for, deadline_tasks_for
from safe_kit.config_guard import (
    MODE_DEMO,
    SYNTHETIC_DEMO_DATASET,
    ConfigError,
    load_settings,
)
from safe_kit.db import connect
from safe_kit.deadlines import deadline_start
from safe_kit.demo_auth import DEMO_ADMIN_ID, DEMO_ADMIN_USERNAME, DEMO_ROLES
from safe_kit.disclosure import canonical_content, digest_of
from safe_kit.evidence_crypto import seal
from safe_kit.evidence_keys import KeyProviderError, key_provider_from_env
from safe_kit.evidence_storage import StorageError, storage_from_env
from safe_kit.reviews import TEMPLATE_LAYERS
from safe_kit.security import hash_password

# The demo administrator's file in the single-file setting of seed version 1. It still works when set.
CREDENTIAL_FILE_ENV = "SAFE_DEMO_CREDENTIAL_FILE"
# The directory that receives one credential file per demo role. Required by both seed versions.
CREDENTIAL_DIR_ENV = "SAFE_DEMO_CREDENTIAL_DIR"
CREDENTIAL_SUFFIX = ".credential"
CREDENTIAL_BYTES = 24
# The seed version this code writes. Version 1 is the original seed; version 2 adds the other demo roles.
SEED_VERSION = 2
# Serializes concurrent seed runs. The marker check happens under this lock.
SEED_LOCK_KEY = 0x5AFE0004

# Fixed identifiers. Top-level rows use these literals; child rows use uuid5 names derived from them.
USERS = (
    # (id, username, role, first name, last name, title)
    (
        "5afe0d00-0000-4000-8000-000000000001",
        "demo-admin",
        "admin",
        "Demo",
        "Administrator",
        "Synthetic admin",
    ),
    (
        "5afe0d00-0000-4000-8000-000000000002",
        "demo-reporter",
        "reporter",
        "Demo",
        "Reporter",
        "Synthetic reporter",
    ),
    (
        "5afe0d00-0000-4000-8000-000000000003",
        "demo-triager",
        "triager",
        "Demo",
        "Triager",
        "Synthetic triager",
    ),
    (
        "5afe0d00-0000-4000-8000-000000000004",
        "demo-investigator",
        "investigator",
        "Demo",
        "Investigator",
        "Synthetic investigator",
    ),
    (
        "5afe0d00-0000-4000-8000-000000000005",
        "demo-reviewer",
        "reviewer",
        "Demo",
        "Reviewer",
        "Synthetic reviewer",
    ),
    (
        "5afe0d00-0000-4000-8000-000000000006",
        "demo-approver",
        "disclosure_approver",
        "Demo",
        "Approver",
        "Synthetic approver",
    ),
    (
        "5afe0d00-0000-4000-8000-000000000007",
        "demo-auditor",
        "auditor",
        "Demo",
        "Auditor",
        "Synthetic auditor",
    ),
    (
        "5afe0d00-0000-4000-8000-000000000008",
        "demo-reporting-owner",
        "reporting_owner",
        "Demo",
        "Reporting owner",
        "Synthetic reporting owner",
    ),
)
USER_BY_ROLE = {role: user_id for user_id, _, role, *_ in USERS}
TIMEZONE_ROW_ID = "5afe0d00-0000-4000-8000-000000000101"
DEMO_TIMEZONE = "UTC"

CASES = (
    # (id, n, title, awareness time, status)
    (
        "5afe0d00-0000-4000-8000-000000000201",
        1,
        "Demo: tool call outside the approved scope",
        datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
        "open",
    ),
    (
        "5afe0d00-0000-4000-8000-000000000202",
        2,
        "Demo: sample identifier in a generated report",
        datetime(2026, 10, 25, 14, 30, tzinfo=UTC),
        "open",
    ),
    (
        "5afe0d00-0000-4000-8000-000000000203",
        3,
        "Demo: near miss, closed after review",
        datetime(2026, 9, 30, 9, 0, tzinfo=UTC),
        "closed",
    ),
)
CASE_ONE, CASE_TWO, CASE_THREE = (case[0] for case in CASES)
CLOSED_AT = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
REVIEW_TIME = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

EVIDENCE_ONE = "5afe0d00-0000-4000-8000-000000000301"
EVIDENCE_TWO = "5afe0d00-0000-4000-8000-000000000302"
EVIDENCE_NOTES = (
    # (id, case, storage key, file name, text)
    (
        EVIDENCE_ONE,
        CASE_ONE,
        "5afe0d00-0000-4000-8000-000000000401",
        "demo-note-1.txt",
        "Demo note (synthetic): the assistant called an external endpoint outside the approved scope.\n",
    ),
    (
        EVIDENCE_TWO,
        CASE_TWO,
        "5afe0d00-0000-4000-8000-000000000402",
        "demo-note-2.txt",
        "Demo note (synthetic): a generated report included a sample customer identifier.\n",
    ),
)

ROUND_ONE = "5afe0d00-0000-4000-8000-000000000501"
# Layer decisions of the demo review round. Layers not listed stay unset, as the template has them.
LAYER_DECISIONS = {
    "model": ("pass", None, [EVIDENCE_ONE]),
    "safeguards": (
        "fail",
        "Demo: the approval step did not stop the out-of-scope call.",
        [EVIDENCE_ONE],
    ),
    "monitoring": (
        "insufficient_evidence",
        "Demo: no monitoring extract was supplied for this period.",
        [],
    ),
    "tools": ("not_applicable", "Demo: this scenario uses no external tool.", []),
}

RECOMMENDATION_ONE = "5afe0d00-0000-4000-8000-000000000601"
VERIFICATION_RUN_ONE = "5afe0d00-0000-4000-8000-000000000602"
CATALOG_ENTRY_ONE = "5afe0d00-0000-4000-8000-000000000603"
CATALOG_VERSION_ONE = "5afe0d00-0000-4000-8000-000000000604"

DISCLOSURE_PACKAGE_ONE = "5afe0d00-0000-4000-8000-000000000701"
DISCLOSURE_VERSION_ONE = "5afe0d00-0000-4000-8000-000000000702"
DISCLOSURE_TEMPLATE = "confidential_rapid_alert"
DISCLOSURE_TLP = "AMBER"
DISCLOSURE_RECIPIENT = "demo-recipient (synthetic)"
DISCLOSURE_PURPOSE = "Demo preview only (synthetic); not approved"

MARKER_ID = "5afe0d00-0000-4000-8000-000000000801"
CHILD_NAMESPACE = uuid.UUID("5afe0d00-0000-4000-8000-0000000000ff")
# The password of every demo account is unusable, so its change time is fixed as well.
PASSWORD_SET_TIME = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


class SeedRefused(Exception):
    """The environment is not the synthetic demo. Nothing is written."""


class SeedError(Exception):
    """The seed cannot write its credential file. Nothing is written to the database."""


def _child_id(*parts: str) -> str:
    return str(uuid.uuid5(CHILD_NAMESPACE, ":".join(parts)))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _insert(conn, table: str, row: Mapping[str, Any]) -> None:
    columns = list(row)
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    conn.execute(query, [row[column] for column in columns])


def _marker_version(conn) -> int | None:
    """The seed version recorded in the marker row, or None when the database holds no demo dataset."""
    row = conn.execute(
        "SELECT seed_version FROM demo_dataset WHERE dataset = %s AND NOT is_deleted",
        (SYNTHETIC_DEMO_DATASET,),
    ).fetchone()
    return row[0] if row else None


def _write_credential_file(path: str, credential: str) -> None:
    """Create the file with mode 0600. O_EXCL refuses an existing file, so a credential is never overwritten."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SeedError(
            "a demo credential file already exists; the seed never overwrites a credential"
        ) from None
    except OSError:
        raise SeedError(
            f"a demo credential file cannot be created; check {CREDENTIAL_DIR_ENV} exists and is writable"
        ) from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(credential + "\n")


def _remove_credential_file(path: str) -> None:
    """Removes a file this run created, when its transaction failed. The file holds no data the database keeps."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _credential_dir(env: Mapping[str, str]) -> str:
    """The directory of the demo credential files. Created with mode 0700 when it does not exist yet."""
    value = env.get(CREDENTIAL_DIR_ENV, "").strip()
    if not value:
        raise SeedError(
            f"{CREDENTIAL_DIR_ENV} is required: the directory that receives one demo credential file per role"
        )
    try:
        os.makedirs(value, mode=0o700, exist_ok=True)
    except OSError:
        raise SeedError(
            f"{CREDENTIAL_DIR_ENV} cannot be created; check its parent directory"
        ) from None
    if not os.path.isdir(value):
        raise SeedError(f"{CREDENTIAL_DIR_ENV} must name a directory")
    return value


def _admin_credential_path(env: Mapping[str, str], credential_dir: str) -> str:
    """The demo administrator's file: the single-file setting when it is set, otherwise the directory."""
    single = env.get(CREDENTIAL_FILE_ENV, "").strip()
    if single:
        return single
    return os.path.join(credential_dir, DEMO_ADMIN_USERNAME + CREDENTIAL_SUFFIX)


def _role_credential_path(credential_dir: str, username: str) -> str:
    return os.path.join(credential_dir, username + CREDENTIAL_SUFFIX)


def _new_credential(path: str, written: list[str]) -> str:
    """Generates a credential, writes its plain value once to path, and returns the hash to store."""
    credential = secrets.token_urlsafe(CREDENTIAL_BYTES)
    _write_credential_file(path, credential)
    written.append(path)
    return hash_password(credential)


def _insert_user(conn, row: tuple) -> None:
    user_id, username, role, first_name, last_name, title = row
    # The account gets a random password nobody has. Demo sign-in is the only way in, and the password is not kept.
    unusable_hash = hash_password(secrets.token_urlsafe(32))
    _insert(
        conn,
        "users",
        {
            "id": user_id,
            "username": username,
            "password_hash": unusable_hash,
            "role": role,
            "is_active": True,
            "must_change_password": False,
            "password_changed_at": PASSWORD_SET_TIME,
            "is_protected": False,
            "first_name": first_name,
            "last_name": last_name,
            "title": title,
            "email": f"{username}@example.invalid",
        },
    )


def _insert_users(conn) -> None:
    for row in USERS:
        _insert_user(conn, row)


def _insert_timezone(conn) -> str:
    current = system_timezone.read_current(conn)
    if current is not None:
        return current.timezone
    _insert(
        conn,
        "system_timezone",
        {"id": TIMEZONE_ROW_ID, "timezone": DEMO_TIMEZONE, "set_by": DEMO_ADMIN_ID},
    )
    return DEMO_TIMEZONE


def _insert_cases(conn, timezone: str) -> None:
    admin = DEMO_ADMIN_ID
    for case_id, index, title, awareness, status in CASES:
        closed = {}
        if status == "closed":
            closed = {
                "closed_at": CLOSED_AT,
                "closed_by": admin,
                "close_reason": "Demo: closed after review (synthetic)",
            }
        _insert(
            conn,
            "cases",
            {
                "id": case_id,
                "reference": f"SAFE-DEMO-{index:06d}",
                "title": title,
                "awareness_time": awareness,
                "created_by": USER_BY_ROLE["triager"],
                "status": status,
                "calendar_version": CALENDAR_VERSION,
                "calendar_timezone": timezone,
                **closed,
            },
        )
        # The same pure functions the API uses, so the demo deadlines follow the real calendar rules.
        t0 = deadline_start(awareness, ())
        for task in deadline_tasks_for(t0, calendar_for(timezone)):
            _insert(
                conn,
                "deadline_tasks",
                {
                    "id": _child_id("deadline", case_id, task.kind),
                    "case_id": case_id,
                    "kind": task.kind,
                    "due_at": task.due_at,
                    "status": task.status,
                    "not_applicable_reason": task.not_applicable_reason,
                },
            )
    assignments = (
        (CASE_ONE, "investigator"),
        (CASE_ONE, "reviewer"),
        (CASE_TWO, "investigator"),
        (CASE_THREE, "reviewer"),
    )
    for case_id, role in assignments:
        _insert(
            conn,
            "case_assignments",
            {
                "id": _child_id("assignment", case_id, role),
                "case_id": case_id,
                "user_id": USER_BY_ROLE[role],
                "assigned_by": admin,
            },
        )


def _insert_evidence(conn, storage, provider) -> None:
    uploader = USER_BY_ROLE["investigator"]
    for evidence_id, case_id, storage_key, filename, text in EVIDENCE_NOTES:
        data = text.encode("utf-8")
        sealed = seal(data, storage_key, provider)
        _insert(
            conn,
            "evidence_items",
            {
                "id": evidence_id,
                "case_id": case_id,
                "uploaded_by": uploader,
                "original_filename": filename,
                "media_type": "text/plain",
                "size_bytes": len(data),
                "sha256_plaintext": _sha256(data),
                "sha256_ciphertext": _sha256(sealed.ciphertext),
                "storage_key": storage_key,
                "key_id": sealed.key_id,
                "wrapped_data_key": sealed.wrapped_data_key,
                "nonce": sealed.nonce,
                "status": "available",
            },
        )
        # The object is written last inside the transaction: a failure here rolls the rows back.
        storage.put(storage_key, sealed.ciphertext)


def _insert_review(conn) -> None:
    reviewer = USER_BY_ROLE["reviewer"]
    _insert(
        conn,
        "review_rounds",
        {
            "id": ROUND_ONE,
            "case_id": CASE_ONE,
            "status": "open",
            # Starts at 1 and grows by one for every layer set (review rounds, migration 0030).
            "revision": 1 + len(LAYER_DECISIONS),
            "opened_by": reviewer,
        },
    )
    for layer in TEMPLATE_LAYERS:
        outcome, reason, evidence_ids = LAYER_DECISIONS.get(layer, (None, None, []))
        decided = outcome is not None
        conn.execute(
            """
            INSERT INTO review_layers (id, round_id, layer, outcome, reason, evidence_ids, set_by, set_at)
            VALUES (%s, %s, %s, %s, %s, %s::uuid[], %s, %s)
            """,
            (
                _child_id("layer", ROUND_ONE, layer),
                ROUND_ONE,
                layer,
                outcome,
                reason,
                evidence_ids,
                reviewer if decided else None,
                REVIEW_TIME if decided else None,
            ),
        )


def _insert_catalog(conn) -> None:
    triager = USER_BY_ROLE["triager"]
    _insert(
        conn,
        "catalog_recommendations",
        {
            "id": RECOMMENDATION_ONE,
            "failure_or_system": "Demo: unscoped tool call reaches an external endpoint",
            "required_defence_outcome": "Every outbound call is checked against the approved scope before it is sent",
            "minimum_control": "Deny by default for outbound tool calls",
            "responsible": "Demo platform owner (synthetic)",
            "due_at": datetime(2027, 1, 31, 9, 0, tzinfo=UTC),
            "evidence_to_keep": "Demo: policy diff and test output",
            "created_by": triager,
        },
    )
    _insert(
        conn,
        "catalog_verification_runs",
        {
            "id": VERIFICATION_RUN_ONE,
            "recommendation_id": RECOMMENDATION_ONE,
            "subject": "Demo: outbound tool call policy",
            "scope": "Demo staging gateway only (synthetic)",
            "assumptions": "Demo: the gateway is the only path to external endpoints",
            "evidence_path": "Demo: policy diff, test run, and a short log extract",
            "failure_behavior": "Demo: the call is blocked and an incident record is opened",
            "error_class": "Demo: policy bypass",
            "uncertainty": "Demo: limited to the tested tool set",
            "method": "Demo: scripted out-of-scope calls against the staging gateway",
            "result": "pass",
            "verified_by": USER_BY_ROLE["reviewer"],
        },
    )
    _insert(conn, "catalog_entries", {"id": CATALOG_ENTRY_ONE, "created_by": triager})
    _insert(
        conn,
        "catalog_versions",
        {
            "id": CATALOG_VERSION_ONE,
            "entry_id": CATALOG_ENTRY_ONE,
            "version": 1,
            "operation": "create",
            "status": "active",
            "title": "Demo: deny outbound tool calls outside the approved scope",
            "body": "Demo entry (synthetic). A verified control for the scenario in the demo cases.",
            "verification_run_id": VERIFICATION_RUN_ONE,
            "created_by": triager,
        },
    )


def _insert_disclosure(conn) -> None:
    triager = USER_BY_ROLE["triager"]
    _insert(
        conn,
        "disclosure_packages",
        {"id": DISCLOSURE_PACKAGE_ONE, "case_id": CASE_ONE, "created_by": triager},
    )
    # A version with no items: a preview of the content, not an approved package. The digest is computed by the same
    # function the API uses, so the stored version checks out.
    manifest: dict[str, Any] = {"items": []}
    digest = digest_of(
        canonical_content(
            CASE_ONE,
            DISCLOSURE_TEMPLATE,
            DISCLOSURE_TLP,
            DISCLOSURE_RECIPIENT,
            DISCLOSURE_PURPOSE,
            [],
            manifest,
        )
    )
    _insert(
        conn,
        "disclosure_versions",
        {
            "id": DISCLOSURE_VERSION_ONE,
            "package_id": DISCLOSURE_PACKAGE_ONE,
            "case_id": CASE_ONE,
            "version": 1,
            "template": DISCLOSURE_TEMPLATE,
            "tlp_audience": DISCLOSURE_TLP,
            "recipient": DISCLOSURE_RECIPIENT,
            "purpose": DISCLOSURE_PURPOSE,
            "manifest": Jsonb(manifest),
            "digest": digest,
            "created_by": triager,
        },
    )


def _insert_dataset(conn, credential_hash: str, storage, provider) -> None:
    _insert_users(conn)
    timezone = _insert_timezone(conn)
    _insert_cases(conn, timezone)
    _insert_evidence(conn, storage, provider)
    _insert_review(conn)
    _insert_catalog(conn)
    _insert_disclosure(conn)
    _insert(
        conn,
        "demo_dataset",
        {
            "id": MARKER_ID,
            "dataset": SYNTHETIC_DEMO_DATASET,
            "credential_hash": credential_hash,
        },
    )


# Version 2: the other demo roles. Assignments that version 2 adds to the seeded cases (the version-1 rows stay as they are).
V2_CASE_ASSIGNMENTS = ((CASE_THREE, "investigator"),)


def _ensure_demo_user(conn, username: str) -> str:
    """The user ID of a demo role. A missing account is created from the fixed table (version 1 rows are never changed)."""
    row = conn.execute(
        "SELECT id FROM users WHERE username = %s AND NOT is_deleted", (username,)
    ).fetchone()
    if row is not None:
        return str(row[0])
    for user_row in USERS:
        if user_row[1] == username:
            _insert_user(conn, user_row)
            return user_row[0]
    raise SeedError(f"{username} is not a demo role")


def _apply_version_2(conn, credential_dir: str, written: list[str]) -> None:
    """Adds one credential and one file per demo role that has none. Existing credentials are never replaced."""
    for demo_role in DEMO_ROLES:
        user_id = _ensure_demo_user(conn, demo_role.username)
        has_credential = conn.execute(
            "SELECT 1 FROM demo_user_credentials WHERE user_id = %s AND NOT is_deleted",
            (user_id,),
        ).fetchone()
        if has_credential is not None:
            continue
        path = _role_credential_path(credential_dir, demo_role.username)
        _insert(
            conn,
            "demo_user_credentials",
            {"user_id": user_id, "credential_hash": _new_credential(path, written)},
        )
    for case_id, role in V2_CASE_ASSIGNMENTS:
        user_id = USER_BY_ROLE[role]
        active = conn.execute(
            "SELECT 1 FROM case_assignments WHERE case_id = %s AND user_id = %s AND NOT is_deleted",
            (case_id, user_id),
        ).fetchone()
        if active is None:
            _insert(
                conn,
                "case_assignments",
                {"case_id": case_id, "user_id": user_id, "assigned_by": DEMO_ADMIN_ID},
            )
    conn.execute(
        "UPDATE demo_dataset SET seed_version = %s WHERE dataset = %s AND NOT is_deleted",
        (SEED_VERSION, SYNTHETIC_DEMO_DATASET),
    )


@dataclass(frozen=True)
class SeedOutcome:
    created: bool
    applied: tuple[int, ...] = ()


def _apply(env: Mapping[str, str], *, upgrade_only: bool) -> SeedOutcome:
    """Applies the seed versions the marker row does not have yet. Nothing is written when there is nothing to apply."""
    settings = load_settings(env)
    if settings.mode != MODE_DEMO or settings.dataset != SYNTHETIC_DEMO_DATASET:
        raise SeedRefused(
            f"the demo seed runs only with SAFE_MODE={MODE_DEMO} and SAFE_DATASET={SYNTHETIC_DEMO_DATASET}"
        )
    credential_dir = _credential_dir(env)
    admin_path = _admin_credential_path(env, credential_dir)
    storage = storage_from_env()
    provider = key_provider_from_env()
    written: list[str] = []
    applied: list[int] = []
    try:
        with connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SEED_LOCK_KEY,))
            version = _marker_version(conn)
            if version is None:
                if upgrade_only:
                    raise SeedError(
                        "this database holds no demo dataset; run `python -m safe_kit.demo_seed seed` instead"
                    )
                admin_hash = _new_credential(admin_path, written)
                _insert_dataset(conn, admin_hash, storage, provider)
                _insert(
                    conn,
                    "demo_user_credentials",
                    {"user_id": DEMO_ADMIN_ID, "credential_hash": admin_hash},
                )
                applied.append(1)
                version = 1
            if version < SEED_VERSION:
                _apply_version_2(conn, credential_dir, written)
                applied.append(2)
    except BaseException:
        for path in written:
            _remove_credential_file(path)
        raise
    return SeedOutcome(created=bool(applied), applied=tuple(applied))


def seed(env: Mapping[str, str]) -> SeedOutcome:
    """Applies every seed version the database does not have yet. A database at the current version gets nothing."""
    return _apply(env, upgrade_only=False)


def upgrade(env: Mapping[str, str]) -> SeedOutcome:
    """Applies the pending seed versions to an existing demo database. It refuses a database without the dataset."""
    return _apply(env, upgrade_only=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m safe_kit.demo_seed")
    parser.add_argument(
        "command",
        choices=["seed", "upgrade"],
        help="seed: write the demo dataset once (all seed versions); upgrade: add the pending seed versions to an existing demo database",
    )
    args = parser.parse_args(argv)
    run = seed if args.command == "seed" else upgrade
    try:
        outcome = run(os.environ)
    except (
        ConfigError,
        SeedRefused,
        SeedError,
        KeyProviderError,
        StorageError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    except psycopg.Error:
        print(
            "the demo seed could not write to the database; nothing was committed",
            file=sys.stderr,
        )
        return 1
    if outcome.created:
        versions = ", ".join(str(version) for version in outcome.applied)
        print(
            f"demo dataset seed version(s) {versions} applied; the demo credential files are in the directory named by {CREDENTIAL_DIR_ENV}"
        )
    else:
        print(
            "demo dataset already seeded at the current seed version; nothing was written"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
