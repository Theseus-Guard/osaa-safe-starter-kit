"""Worker: writes a periodic heartbeat, processes durable outbox jobs (see safe_kit.outbox), runs the evidence
reconciliation that fails pending uploads left behind by a crash (see safe_kit.evidence.fail_stale_pending), and creates
the weekly updates that are due (see safe_kit.weekly_updates.generate_due_updates)."""

import logging
import os
import signal
import sys
import time
from types import FrameType

from safe_kit.config_guard import ConfigError, load_settings
from safe_kit.db import connect
from safe_kit.evidence import fail_stale_pending
from safe_kit.outbox import run_once
from safe_kit.weekly_updates import generate_due_updates

HEARTBEAT_SECONDS = 10
RECONCILE_SECONDS = 300
# Checks for due weekly updates. An update is due 7 days after the previous one, so a 5-minute check is plenty.
WEEKLY_SECONDS = 300
IDLE_POLL_SECONDS = 1.0
_running = True

logger = logging.getLogger(__name__)


def _stop(_signum: int, _frame: FrameType | None) -> None:
    global _running
    _running = False


def heartbeat() -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO worker_heartbeat (worker_name, beat_at) VALUES ('default', now()) "
            "ON CONFLICT (worker_name) DO UPDATE SET beat_at = excluded.beat_at"
        )


def reconcile_pending_uploads() -> int:
    """Fail pending evidence rows older than the window. Returns how many were failed. Safe with several workers."""
    failed = fail_stale_pending()
    if failed:
        logger.warning("marked %d stale pending evidence upload(s) as failed", len(failed))
    return len(failed)


def create_weekly_updates() -> int:
    """Create the weekly updates that are due. Returns how many were created. Safe to run again at any time."""
    created = generate_due_updates()
    if created:
        logger.info("created %d weekly update(s)", created)
    return created


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    next_heartbeat = 0.0
    next_reconcile = 0.0
    next_weekly = 0.0
    while _running:
        if time.monotonic() >= next_heartbeat:
            heartbeat()
            next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
        if time.monotonic() >= next_reconcile:
            next_reconcile = time.monotonic() + RECONCILE_SECONDS
            try:
                reconcile_pending_uploads()
            except Exception as error:
                # Logged by error type only. The next attempt is at the next interval, and the job loop keeps running.
                logger.error("evidence reconciliation failed (%s); retrying in %d seconds", type(error).__name__,
                             RECONCILE_SECONDS)
        if time.monotonic() >= next_weekly:
            next_weekly = time.monotonic() + WEEKLY_SECONDS
            try:
                create_weekly_updates()
            except Exception as error:
                # Logged by error type only, as above. The next attempt is at the next interval.
                logger.error("weekly update generation failed (%s); retrying in %d seconds", type(error).__name__,
                             WEEKLY_SECONDS)
        # Keep processing while jobs are due; sleep only when the queue is empty.
        if not run_once():
            time.sleep(IDLE_POLL_SECONDS)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The startup guard runs before main() and before any database connection is opened.
    try:
        load_settings(os.environ)
    except ConfigError as error:
        logger.error("refusing to start: %s", error)
        sys.exit(2)
    sys.exit(main())
