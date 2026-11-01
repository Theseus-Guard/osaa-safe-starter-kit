"""Skeleton worker: writes a periodic heartbeat to the database. Queue processing is not implemented yet."""

import signal
import sys
import time
from types import FrameType

from safe_kit.db import connect

HEARTBEAT_SECONDS = 10
_running = True


def _stop(_signum: int, _frame: FrameType | None) -> None:
    global _running
    _running = False


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while _running:
        with connect() as conn:
            conn.execute(
                "INSERT INTO worker_heartbeat (worker_name, beat_at) VALUES ('default', now()) "
                "ON CONFLICT (worker_name) DO UPDATE SET beat_at = excluded.beat_at"
            )
        time.sleep(HEARTBEAT_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
