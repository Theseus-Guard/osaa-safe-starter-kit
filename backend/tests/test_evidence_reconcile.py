"""Reconciliation of pending evidence rows, and the worker step that runs it on an interval.

Most tests need no database. The database tests at the end run against PostgreSQL and are skipped unless
SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings are set, like tests/test_database.py.
"""

import contextlib
import logging
import os
import uuid
from pathlib import Path

import pytest

from safe_kit import evidence, migrate, users, worker
from safe_kit.db import connect

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


class FakeConnection:
    def __init__(self, log: list) -> None:
        self.log = log

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


@pytest.fixture
def sql_log(monkeypatch) -> list:
    log: list = []

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConnection(log)

    monkeypatch.setattr(evidence, "connect", fake_connect)
    return log


# --- the reconciliation query --------------------------------------------------------------------------------------


def test_reconciliation_selects_only_stale_pending_rows_and_skips_locked_ones(sql_log):
    assert evidence.fail_stale_pending(30) == []
    assert len(sql_log) == 1
    sql, params = sql_log[0]
    assert sql.startswith(
        "UPDATE evidence_items SET status = 'failed' WHERE id IN ( SELECT id FROM evidence_items"
    )
    assert (
        "WHERE status = 'pending' AND NOT is_deleted AND created_at < now() - make_interval(mins => %s::int)"
        in sql
    )
    assert "FOR UPDATE SKIP LOCKED ) RETURNING id::text" in sql
    assert params == (30,)
    assert "DELETE" not in sql and "TRUNCATE" not in sql


def test_reconciliation_writes_no_custody_event_when_nothing_is_stale(sql_log):
    evidence.fail_stale_pending(30)
    assert [
        sql for sql, _ in sql_log if sql.startswith("INSERT INTO custody_events")
    ] == []


def test_reconciliation_window_must_be_positive():
    with pytest.raises(ValueError):
        evidence.fail_stale_pending(-5)


def test_default_window_is_the_pending_window_constant():
    assert evidence.DEFAULT_PENDING_MINUTES == 30


# --- the worker step -----------------------------------------------------------------------------------------------


def test_worker_step_returns_how_many_rows_were_failed(monkeypatch, caplog):
    monkeypatch.setattr(worker, "fail_stale_pending", lambda: ["row-a", "row-b"])
    with caplog.at_level(logging.WARNING, logger=worker.logger.name):
        assert worker.reconcile_pending_uploads() == 2
    assert "marked 2 stale pending evidence upload(s) as failed" in caplog.text


def test_worker_step_is_silent_when_nothing_was_stale(monkeypatch, caplog):
    monkeypatch.setattr(worker, "fail_stale_pending", lambda: [])
    with caplog.at_level(logging.WARNING, logger=worker.logger.name):
        assert worker.reconcile_pending_uploads() == 0
    assert caplog.text == ""


class FakeClock:
    """Stands in for the time module inside worker.main. Time moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        # Sleeping means the queue is empty: end the loop, so a test cannot hang.
        self.now += seconds
        worker._running = False


@pytest.fixture
def loop(monkeypatch):
    """Run worker.main with a fake clock. Each job takes 60 seconds of fake time; idle sleeps stop the loop."""
    clock = FakeClock()
    state = {"jobs_left": 0, "reconcile_times": [], "reconcile_error": None}

    def fake_run_once() -> bool:
        if state["jobs_left"] <= 0:
            return False
        state["jobs_left"] -= 1
        clock.now += 60
        return True

    def fake_reconcile() -> int:
        state["reconcile_times"].append(clock.now)
        if state["reconcile_error"] is not None:
            raise state["reconcile_error"]
        return 0

    monkeypatch.setattr(worker, "time", clock)
    monkeypatch.setattr(worker, "heartbeat", lambda: None)
    monkeypatch.setattr(worker, "run_once", fake_run_once)
    monkeypatch.setattr(worker, "reconcile_pending_uploads", fake_reconcile)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    worker._running = True
    yield state
    worker._running = True


def test_worker_runs_reconciliation_at_start_and_then_every_interval(loop):
    loop["jobs_left"] = 12  # twelve minutes of jobs, then idle
    assert worker.main() == 0
    # Jobs run at 0, 60, ..., 660 seconds. Reconciliation runs at 0, then at each RECONCILE_SECONDS (300).
    assert loop["reconcile_times"] == [0.0, 300.0, 600.0]
    assert worker.RECONCILE_SECONDS == 300


def test_worker_runs_reconciliation_once_when_the_queue_is_empty(loop):
    assert worker.main() == 0
    assert loop["reconcile_times"] == [0.0]


def test_failed_reconciliation_is_logged_by_error_type_and_the_loop_keeps_running(
    loop, caplog
):
    loop["jobs_left"] = 3
    loop["reconcile_error"] = RuntimeError(
        "connection to the database was lost: secret detail"
    )
    with caplog.at_level(logging.ERROR, logger=worker.logger.name):
        assert worker.main() == 0
    assert loop["reconcile_times"] == [0.0]
    assert "evidence reconciliation failed (RuntimeError)" in caplog.text
    assert "secret detail" not in caplog.text


# --- database: the reconciliation against PostgreSQL ----------------------------------------------------------------

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def database_case() -> tuple[str, str]:
    migrate.MIGRATIONS_DIR = MIGRATIONS
    assert migrate.main() == 0
    suffix = uuid.uuid4().hex[:12]
    user_id = users.create_user("r-" + suffix, "temporary passphrase", "investigator")
    with connect() as conn:
        case_id = conn.execute(
            """
            INSERT INTO cases (reference, title, awareness_time, created_by, status, calendar_version, calendar_timezone)
            VALUES (%s, 'reconcile case', now(), %s, 'open', 'test', 'UTC') RETURNING id::text
            """,
            ("SAFE-TEST-" + suffix, user_id),
        ).fetchone()[0]
    return user_id, case_id


def insert_pending_row(case_id: str, user_id: str, minutes_old: int) -> str:
    """A pending row whose created_at is minutes_old minutes in the past. The object is not stored (a crash)."""
    with connect() as conn:
        return conn.execute(
            """
            INSERT INTO evidence_items (case_id, uploaded_by, original_filename, media_type, size_bytes,
                                        storage_key, key_id, wrapped_data_key, nonce, status, created_at)
            VALUES (%s, %s, 'note.txt', 'text/plain', 1, %s, 'test', '\\x00'::bytea, '\\x00'::bytea,
                    'pending', now() - make_interval(mins => %s::int))
            RETURNING id::text
            """,
            (case_id, user_id, str(uuid.uuid4()), minutes_old),
        ).fetchone()[0]


def row_status(evidence_id: str) -> str:
    with connect() as conn:
        return conn.execute(
            "SELECT status FROM evidence_items WHERE id = %s", (evidence_id,)
        ).fetchone()[0]


def custody_actions(evidence_id: str) -> list[tuple[str, dict]]:
    return [
        (event.action, event.details) for event in evidence.custody_events(evidence_id)
    ]


@requires_database
def test_stale_pending_row_is_failed_and_a_recent_pending_row_is_not_touched():
    user_id, case_id = database_case()
    stale = insert_pending_row(case_id, user_id, minutes_old=120)
    recent = insert_pending_row(case_id, user_id, minutes_old=0)
    failed = evidence.fail_stale_pending(30)
    assert stale in failed
    assert recent not in failed
    assert row_status(stale) == "failed"
    assert row_status(recent) == "pending"
    assert custody_actions(stale) == [("failed", {"reason": "stale_pending"})]
    assert custody_actions(recent) == []


@requires_database
def test_reconciliation_is_idempotent_and_writes_one_custody_event_per_row():
    user_id, case_id = database_case()
    stale = insert_pending_row(case_id, user_id, minutes_old=120)
    assert stale in evidence.fail_stale_pending(30)
    assert stale not in evidence.fail_stale_pending(30)
    assert custody_actions(stale) == [("failed", {"reason": "stale_pending"})]


@requires_database
def test_row_locked_by_another_transaction_is_skipped_and_picked_up_later():
    user_id, case_id = database_case()
    stale = insert_pending_row(case_id, user_id, minutes_old=120)
    # Another transaction holds the row, as an upload finishing would. Reconciliation must not wait for it.
    with connect() as holder:
        holder.execute(
            "SELECT id FROM evidence_items WHERE id = %s FOR UPDATE", (stale,)
        ).fetchone()
        assert stale not in evidence.fail_stale_pending(30)
    assert row_status(stale) == "pending"
    assert stale in evidence.fail_stale_pending(30)
    assert row_status(stale) == "failed"


@requires_database
def test_worker_step_fails_the_stale_row_in_the_database():
    user_id, case_id = database_case()
    stale = insert_pending_row(case_id, user_id, minutes_old=120)
    assert worker.reconcile_pending_uploads() >= 1
    assert row_status(stale) == "failed"
    assert custody_actions(stale) == [("failed", {"reason": "stale_pending"})]
