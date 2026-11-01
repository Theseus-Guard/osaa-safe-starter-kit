"""Coverage for fixed-window rate limits stored in PostgreSQL.

They need a PostgreSQL database with the SAFE_DB_* settings and a password file, and they run the migrations
first. They are skipped unless SAFE_TEST_DATABASE=1. Window ages are moved back with SQL, never by sleeping.
"""

import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from safe_kit import migrate, rate_limit
from safe_kit.db import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

WINDOW = timedelta(seconds=60)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    migrate.MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
    assert migrate.main() == 0


def new_bucket() -> str:
    return "test-cov-" + uuid.uuid4().hex


def age_window(bucket: str, seconds: int) -> None:
    """Move the window start back, as if the window had begun that many seconds ago."""
    with connect() as conn:
        conn.execute(
            "UPDATE rate_limits SET window_start = now() - make_interval(secs => %s) WHERE bucket = %s",
            (seconds, bucket),
        )


def rows_for(bucket: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT hits, is_deleted, deleted_at IS NOT NULL FROM rate_limits WHERE bucket = %s",
            (bucket,),
        ).fetchall()


def window_is_recent(bucket: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT window_start > now() - interval '30 seconds' FROM rate_limits WHERE bucket = %s",
            (bucket,),
        ).fetchone()[0]


def test_hits_count_up_within_one_window():
    bucket = new_bucket()
    assert [rate_limit.hit(bucket, WINDOW) for _ in range(3)] == [1, 2, 3]
    assert rows_for(bucket) == [(3, False, False)]


def test_window_does_not_reset_before_its_length_has_passed():
    bucket = new_bucket()
    rate_limit.hit(bucket, WINDOW)
    rate_limit.hit(bucket, WINDOW)
    age_window(bucket, 30)  # half the window has passed

    assert rate_limit.hit(bucket, WINDOW) == 3


def test_window_resets_once_its_length_has_passed():
    bucket = new_bucket()
    for _ in range(3):
        rate_limit.hit(bucket, WINDOW)
    age_window(bucket, 120)  # longer than the window

    assert rate_limit.hit(bucket, WINDOW) == 1
    assert window_is_recent(bucket)
    assert rows_for(bucket) == [(1, False, False)], "the same row is reused, not duplicated"


def test_cleared_bucket_starts_again_from_one():
    bucket = new_bucket()
    rate_limit.hit(bucket, WINDOW)
    rate_limit.hit(bucket, WINDOW)

    rate_limit.clear(bucket)

    assert rate_limit.hit(bucket, WINDOW) == 1
    assert rows_for(bucket) == [(1, False, False)]


def test_clear_soft_deletes_the_row_and_never_removes_it():
    bucket = new_bucket()
    rate_limit.hit(bucket, WINDOW)
    rate_limit.hit(bucket, WINDOW)

    rate_limit.clear(bucket)
    assert rows_for(bucket) == [(2, True, True)]

    with connect() as conn:
        deleted_at = conn.execute("SELECT deleted_at FROM rate_limits WHERE bucket = %s", (bucket,)).fetchone()[0]

    # A second clear changes nothing: the row is already soft-deleted, so its timestamp stays as it was.
    rate_limit.clear(bucket)
    with connect() as conn:
        again = conn.execute("SELECT deleted_at FROM rate_limits WHERE bucket = %s", (bucket,)).fetchall()
    assert again == [(deleted_at,)]


def test_clearing_a_bucket_that_was_never_used_creates_no_row():
    bucket = new_bucket()
    rate_limit.clear(bucket)
    assert rows_for(bucket) == []
