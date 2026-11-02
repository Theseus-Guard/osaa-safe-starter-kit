"""F1 fixtures T16 (crash and restart) and T17 (startup, migration and volume subset).

T16: a worker that stops after claiming a job does not lose it; the job is run again after its lease expires and
takes effect once because the handler dedupes on a key in its payload (the outbox itself is at-least-once); a job
enqueued in a rolled-back transaction never runs; re-running the migrations applies nothing.
T17: readiness answers 503 when PostgreSQL cannot be reached, the migration runner refuses a changed applied
migration, and cases and evidence survive a process restart.

The database tests are skipped unless SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings are set. The readiness test
needs no database: it points the client at a port where nothing listens.
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, cases, evidence, migrate, outbox, users
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.evidence_keys import LocalFileKeyProvider
from safe_kit.evidence_storage import LocalVolumeStorage
from test_evidence_lifecycle import PDF  # noqa: F401 - sample PDF for the restart check

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def ensure_migrated_with_a_timezone() -> None:
    migrate.MIGRATIONS_DIR = MIGRATIONS
    assert migrate.main() == 0
    with connect() as conn:
        exists = conn.execute("SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)").fetchone()[0]
        if not exists:
            conn.execute("INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')")


@pytest.fixture
def clean_outbox():
    """Soft-delete jobs left by earlier runs, so that run_once only sees this test's jobs. Nothing is removed."""
    ensure_migrated_with_a_timezone()
    with connect() as conn:
        conn.execute("UPDATE outbox_jobs SET is_deleted = true, deleted_at = now() WHERE NOT is_deleted")


def job_status(job_id: str) -> tuple[str, int]:
    with connect() as conn:
        return conn.execute("SELECT status, attempts FROM outbox_jobs WHERE id = %s", (job_id,)).fetchone()


def expire_lease(job_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE outbox_jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,))


# --- T16: crash between the claim and the commit ---------------------------------------------------------------------


@requires_database
def test_job_of_a_worker_that_stopped_after_its_claim_runs_once_more_and_takes_effect_once(clean_outbox):
    effect_key = uuid.uuid4().hex
    with connect() as conn:
        job_id = outbox.enqueue(conn, "noop", {"effect_key": effect_key})

    # Worker A claims the job, applies the effect, and stops before the job is marked done.
    crashed = outbox.claim_next()
    assert crashed is not None and crashed.id == job_id
    effects: list[str] = [effect_key]

    # The restarted worker finds the job still leased, so it does nothing yet.
    assert outbox.run_once({"noop": lambda payload: effects.append(payload["effect_key"])}) is False

    expire_lease(job_id)

    def dedupe_handler(payload):
        # The outbox is at-least-once: the handler must dedupe on its own key. Here the key is in the payload.
        if payload["effect_key"] not in effects:
            effects.append(payload["effect_key"])

    assert outbox.run_once({"noop": dedupe_handler}) is True
    assert job_status(job_id) == ("done", 2)
    assert effects == [effect_key]


@requires_database
def test_job_enqueued_in_a_rolled_back_transaction_is_never_run(clean_outbox):
    marker = uuid.uuid4().hex
    with pytest.raises(RuntimeError):
        with connect() as conn:
            outbox.enqueue(conn, "noop", {"marker": marker})
            raise RuntimeError("the business change failed")

    with connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM outbox_jobs WHERE payload->>'marker' = %s", (marker,)
        ).fetchone()[0]
    assert count == 0


@requires_database
def test_job_enqueued_in_a_committed_transaction_is_run_once_after_a_restart(clean_outbox):
    marker = uuid.uuid4().hex
    with connect() as conn:
        job_id = outbox.enqueue(conn, "noop", {"marker": marker})
    seen: list[str] = []

    # A fresh call stands for a restarted worker: nothing is kept in memory between calls.
    assert outbox.run_once({"noop": lambda payload: seen.append(payload["marker"])}) is True
    assert outbox.run_once({"noop": lambda payload: seen.append(payload["marker"])}) is False
    assert job_status(job_id) == ("done", 1)
    assert seen == [marker]


# --- T16 and T17: migrations ------------------------------------------------------------------------------------------


@requires_database
def test_rerunning_the_migrations_applies_nothing_and_exits_zero(capsys):
    migrate.MIGRATIONS_DIR = MIGRATIONS
    assert migrate.main() == 0
    capsys.readouterr()
    with connect() as conn:
        before = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]

    assert migrate.main() == 0

    assert "applied:" not in capsys.readouterr().out
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == before


@requires_database
def test_migration_runner_refuses_an_applied_migration_whose_file_changed(tmp_path, monkeypatch, capsys):
    version = f"zz_fixture_{uuid.uuid4().hex[:12]}"
    migration = tmp_path / f"{version}.sql"
    migration.write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    assert migrate.main() == 0
    assert f"applied: {version}" in capsys.readouterr().out

    migration.write_text("SELECT 2;\n", encoding="utf-8")

    assert migrate.main() == 1
    assert f"checksum mismatch: applied migration {version} was modified" in capsys.readouterr().err


# --- T17: readiness when the database is down --------------------------------------------------------------------------


def _closed_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_readiness_is_503_when_the_database_cannot_be_reached_and_liveness_stays_ok(tmp_path, monkeypatch):
    password_file = tmp_path / "db_password.txt"
    password_file.write_text("not-used-because-nothing-listens", encoding="utf-8")
    monkeypatch.setenv("SAFE_DB_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("SAFE_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("SAFE_DB_PORT", str(_closed_local_port()))
    monkeypatch.setenv("SAFE_DB_NAME", "safe")
    monkeypatch.setenv("SAFE_DB_USER", "safe_api")
    client = TestClient(api.app)

    ready = client.get("/api/v1/health/ready")
    live = client.get("/api/v1/health/live")

    assert ready.status_code == 503
    assert ready.json() == {"detail": {"status": "unavailable"}}
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}


# --- T17: data survives a restart ------------------------------------------------------------------------------------

RESTART_CHECK = """
import hashlib, json, os
from pathlib import Path
from safe_kit import cases, evidence
from safe_kit.cases import CaseScope
from safe_kit.evidence_keys import LocalFileKeyProvider
from safe_kit.evidence_storage import LocalVolumeStorage

scope = CaseScope.from_permissions(os.environ["FIXTURE_USER"], frozenset({"cases:read_all"}))
case = cases.find_visible_case(os.environ["FIXTURE_CASE"], scope)
stored = evidence.find_stored(os.environ["FIXTURE_EVIDENCE"], scope)
content = evidence.read_original(
    stored,
    LocalVolumeStorage(Path(os.environ["FIXTURE_STORE"])),
    LocalFileKeyProvider(Path(os.environ["FIXTURE_KEK"])),
)
print(json.dumps({
    "reference": case.reference,
    "title": case.title,
    "status": stored.status,
    "sha256": hashlib.sha256(content).hexdigest(),
}))
"""


@requires_database
def test_case_and_evidence_survive_a_process_restart(tmp_path):
    ensure_migrated_with_a_timezone()
    suffix = uuid.uuid4().hex[:12]
    user_id = users.create_user("t-" + suffix, "temporary passphrase", "investigator")
    scope = CaseScope.from_permissions(user_id, frozenset({"cases:read_all"}))
    case = cases.create_case("Restart " + suffix, datetime.now(UTC) - timedelta(hours=1), user_id)
    kek = tmp_path / "kek.bin"
    kek.write_bytes(os.urandom(32))
    os.chmod(kek, 0o600)
    store_dir = tmp_path / "evidence"
    store_dir.mkdir()
    storage = LocalVolumeStorage(store_dir)
    item = evidence.create_evidence(
        case.id, user_id, "scan.pdf", "application/pdf", PDF, storage, LocalFileKeyProvider(kek), scope,
    )

    # A new interpreter has no state from this test: whatever it reads comes from PostgreSQL and the volume.
    env = {
        **os.environ,
        # The child is a bare interpreter; pytest adds src/ to sys.path only inside this process.
        "PYTHONPATH": str(SOURCE_DIR),
        "FIXTURE_USER": user_id,
        "FIXTURE_CASE": case.id,
        "FIXTURE_EVIDENCE": item.id,
        "FIXTURE_STORE": str(store_dir),
        "FIXTURE_KEK": str(kek),
    }
    result = subprocess.run(
        [sys.executable, "-c", RESTART_CHECK], env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    after_restart = json.loads(result.stdout.strip().splitlines()[-1])

    assert after_restart == {
        "reference": case.reference,
        "title": "Restart " + suffix,
        "status": "available",
        "sha256": hashlib.sha256(PDF).hexdigest(),
    }
