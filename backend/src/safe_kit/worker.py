"""Worker: writes a periodic heartbeat and processes durable outbox jobs (see safe_kit.outbox)."""

import logging
import signal
import sys
import time
from types import FrameType

from safe_kit.db import connect
from safe_kit.outbox import run_once

HEARTBEAT_SECONDS = 10
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


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    next_heartbeat = 0.0
    while _running:
        if time.monotonic() >= next_heartbeat:
            heartbeat()
            next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
        # Keep processing while jobs are due; sleep only when the queue is empty.
        if not run_once():
            time.sleep(IDLE_POLL_SECONDS)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(main())
