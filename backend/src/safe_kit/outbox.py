"""Durable job queue in PostgreSQL (transactional outbox) and the worker-side processing.

A job is claimed in a short transaction that sets a lease. The handler runs with no database lock held.
The result is then written in a second short transaction. Writes are fenced by the attempt number, so a worker
whose lease expired cannot overwrite the result of a worker that reclaimed the job.

Only the "noop" handler is registered. External delivery is added in a later step.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from safe_kit.db import connect

logger = logging.getLogger(__name__)

PENDING = "pending"
LEASED = "leased"
DONE = "done"
FAILED = "failed"

MAX_ATTEMPTS = 5
LEASE = timedelta(minutes=5)
BASE_BACKOFF = timedelta(seconds=30)
MAX_BACKOFF = timedelta(hours=1)
ERROR_MAX_LENGTH = 500

Handler = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class Job:
    id: str
    job_type: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class FailureOutcome:
    status: str
    delay: timedelta


def backoff_after(attempts: int) -> timedelta:
    """Delay before the next attempt, after `attempts` attempts have been made (30s, 60s, 120s, ... capped at 1h)."""
    exponent = min(max(attempts - 1, 0), 20)
    return min(BASE_BACKOFF * (2**exponent), MAX_BACKOFF)


def failure_outcome(attempts: int, max_attempts: int = MAX_ATTEMPTS) -> FailureOutcome:
    """Decide what a failed attempt leads to: another attempt after a backoff, or permanent failure."""
    if attempts >= max_attempts:
        return FailureOutcome(FAILED, timedelta(0))
    return FailureOutcome(PENDING, backoff_after(attempts))


def _describe(error: BaseException) -> str:
    # The payload is never stored here: it may contain personal data. Only the error type and message are kept.
    text = f"{type(error).__name__}: {error}"
    return text[:ERROR_MAX_LENGTH]


def enqueue(conn: psycopg.Connection, job_type: str, payload: Mapping[str, Any] | None = None) -> str:
    """Add a job using the caller's connection, so it commits together with the change that needs it."""
    row = conn.execute(
        "INSERT INTO outbox_jobs (job_type, payload, status) VALUES (%s, %s, 'pending') RETURNING id::text",
        (job_type, Jsonb(dict(payload or {}))),
    ).fetchone()
    return row[0]


def claim_next(lease: timedelta = LEASE, max_attempts: int = MAX_ATTEMPTS) -> Job | None:
    """Lease the oldest due job. Expired leases are claimable again.

    A job whose last allowed attempt lost its lease is moved to failed here, since no worker would claim it again.
    """
    with connect() as conn:
        conn.execute(
            """
            UPDATE outbox_jobs
            SET status = 'failed', lease_until = NULL, last_error = 'lease expired after the final attempt'
            WHERE NOT is_deleted AND status = 'leased' AND lease_until <= now() AND attempts >= %(max_attempts)s
            """,
            {"max_attempts": max_attempts},
        )
        row = conn.execute(
            """
            WITH due AS (
                SELECT id FROM outbox_jobs
                WHERE NOT is_deleted
                  AND attempts < %(max_attempts)s
                  AND ((status = 'pending' AND available_at <= now())
                       OR (status = 'leased' AND lease_until <= now()))
                ORDER BY available_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE outbox_jobs AS j
            SET status = 'leased',
                lease_until = now() + make_interval(secs => %(lease_seconds)s),
                attempts = j.attempts + 1
            FROM due
            WHERE j.id = due.id
            RETURNING j.id::text, j.job_type, j.payload, j.attempts
            """,
            {"max_attempts": max_attempts, "lease_seconds": lease.total_seconds()},
        ).fetchone()
    if row is None:
        return None
    return Job(id=row[0], job_type=row[1], payload=row[2], attempts=row[3])


def _complete(job: Job) -> bool:
    with connect() as conn:
        result = conn.execute(
            """
            UPDATE outbox_jobs SET status = 'done', lease_until = NULL, last_error = NULL
            WHERE id = %s AND status = 'leased' AND attempts = %s AND NOT is_deleted
            """,
            (job.id, job.attempts),
        )
    return result.rowcount == 1


def _record_failure(job: Job, error: str, outcome: FailureOutcome) -> bool:
    with connect() as conn:
        result = conn.execute(
            """
            UPDATE outbox_jobs
            SET status = %s,
                available_at = now() + make_interval(secs => %s),
                lease_until = NULL,
                last_error = %s
            WHERE id = %s AND status = 'leased' AND attempts = %s AND NOT is_deleted
            """,
            (outcome.status, outcome.delay.total_seconds(), error, job.id, job.attempts),
        )
    return result.rowcount == 1


def _noop(payload: dict[str, Any]) -> None:
    """Test handler. It does nothing and is not a real delivery."""


HANDLERS: dict[str, Handler] = {"noop": _noop}


def run_once(handlers: Mapping[str, Handler] = HANDLERS) -> bool:
    """Claim and process at most one job. Returns False when no job was due."""
    job = claim_next()
    if job is None:
        return False
    handler = handlers.get(job.job_type)
    if handler is None:
        logger.warning("outbox job %s has unknown type %s; marked failed", job.id, job.job_type)
        _record_failure(job, f"unknown job type: {job.job_type}", FailureOutcome(FAILED, timedelta(0)))
        return True
    try:
        handler(job.payload)
    except Exception as error:
        # Logged without the payload, which may contain personal data.
        logger.warning("outbox job %s (%s) attempt %d failed: %s", job.id, job.job_type, job.attempts, _describe(error))
        _record_failure(job, _describe(error), failure_outcome(job.attempts))
        return True
    if not _complete(job):
        logger.warning("outbox job %s lost its lease before completion; another worker may run it again", job.id)
    return True
