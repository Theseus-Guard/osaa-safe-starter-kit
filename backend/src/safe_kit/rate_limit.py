"""Fixed-window rate limits kept in PostgreSQL, so they survive restarts and apply to every worker."""

from datetime import timedelta

from safe_kit.db import connect

LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_LIMIT_PER_USERNAME = 5
LOGIN_LIMIT_PER_CLIENT = 50


def hit(bucket: str, window: timedelta) -> int:
    """Count one event in the bucket and return the hit count for the current window.

    A soft-deleted bucket counts as empty, so a cleared bucket starts a new window.
    """
    params = {"bucket": bucket, "secs": window.total_seconds()}
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO rate_limits (bucket, window_start, hits) VALUES (%(bucket)s, now(), 1)
            ON CONFLICT (bucket) DO UPDATE SET
                window_start = CASE WHEN rate_limits.is_deleted
                                      OR rate_limits.window_start <= now() - make_interval(secs => %(secs)s)
                                    THEN now() ELSE rate_limits.window_start END,
                hits = CASE WHEN rate_limits.is_deleted
                                 OR rate_limits.window_start <= now() - make_interval(secs => %(secs)s)
                            THEN 1 ELSE rate_limits.hits + 1 END,
                is_deleted = false,
                deleted_at = NULL
            RETURNING hits
            """,
            params,
        ).fetchone()
    return row[0]


def clear(bucket: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE rate_limits SET is_deleted = true, deleted_at = now() WHERE bucket = %s AND NOT is_deleted",
            (bucket,),
        )
