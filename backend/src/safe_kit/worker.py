"""Worker: writes a periodic heartbeat, processes durable outbox jobs (see safe_kit.outbox), and runs the evidence
reconciliation that fails pending uploads left behind by a crash (see safe_kit.evidence.fail_stale_pending)."""

import logging
import signal
import sys
import time
from types import FrameType

from safe_kit.db import connect
from safe_kit.evidence import fail_stale_pending
from safe_kit.outbox import run_once

HEARTBEAT_SECONDS = 10
RECONCILE_SECONDS = 300
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


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    next_heartbeat = 0.0
    next_reconcile = 0.0
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
        # Keep processing while jobs are due; sleep only when the queue is empty.
        if not run_once():
            time.sleep(IDLE_POLL_SECONDS)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(main())
