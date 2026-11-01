from datetime import timedelta

import pytest

from safe_kit import outbox
from safe_kit.outbox import FAILED, MAX_ATTEMPTS, PENDING, FailureOutcome, Job


def test_backoff_doubles_from_thirty_seconds_and_is_capped_at_one_hour():
    assert [outbox.backoff_after(n) for n in range(1, 6)] == [
        timedelta(seconds=30),
        timedelta(seconds=60),
        timedelta(seconds=120),
        timedelta(seconds=240),
        timedelta(seconds=480),
    ]
    assert outbox.backoff_after(20) == timedelta(hours=1)


def test_failed_attempt_before_the_limit_is_retried_after_backoff():
    assert outbox.failure_outcome(1) == FailureOutcome(PENDING, timedelta(seconds=30))
    assert outbox.failure_outcome(MAX_ATTEMPTS - 1).status == PENDING


def test_failed_attempt_at_the_limit_is_permanent():
    outcome = outbox.failure_outcome(MAX_ATTEMPTS)
    assert outcome == FailureOutcome(FAILED, timedelta(0))


class FakeQueue:
    """Replaces the three database steps of run_once, so its orchestration can be tested without PostgreSQL."""

    def __init__(self, job):
        self.job = job
        self.completed = []
        self.failed = []

    def claim_next(self, *args, **kwargs):
        job, self.job = self.job, None
        return job

    def complete(self, job):
        self.completed.append(job)
        return True

    def record_failure(self, job, error, outcome):
        self.failed.append((job, error, outcome))
        return True


@pytest.fixture
def queue(monkeypatch):
    def make(job):
        fake = FakeQueue(job)
        monkeypatch.setattr(outbox, "claim_next", fake.claim_next)
        monkeypatch.setattr(outbox, "_complete", fake.complete)
        monkeypatch.setattr(outbox, "_record_failure", fake.record_failure)
        return fake

    return make


def test_no_due_job_means_nothing_to_do(queue):
    fake = queue(None)
    assert outbox.run_once() is False
    assert fake.completed == [] and fake.failed == []


def test_successful_handler_marks_the_job_done(queue):
    job = Job(id="j1", job_type="noop", payload={"a": 1}, attempts=1)
    fake = queue(job)
    seen = []
    assert outbox.run_once({"noop": lambda payload: seen.append(payload)}) is True
    assert seen == [{"a": 1}]
    assert fake.completed == [job]
    assert fake.failed == []


def test_failing_handler_schedules_a_retry_with_backoff(queue):
    job = Job(id="j2", job_type="noop", payload={}, attempts=1)
    fake = queue(job)

    def boom(payload):
        raise RuntimeError("delivery refused")

    assert outbox.run_once({"noop": boom}) is True
    assert fake.completed == []
    recorded_job, error, outcome = fake.failed[0]
    assert recorded_job == job
    assert error == "RuntimeError: delivery refused"
    assert outcome == FailureOutcome(PENDING, timedelta(seconds=30))


def test_last_attempt_that_fails_moves_the_job_to_failed(queue):
    job = Job(id="j3", job_type="noop", payload={}, attempts=MAX_ATTEMPTS)
    fake = queue(job)

    def boom(payload):
        raise ValueError("still broken")

    outbox.run_once({"noop": boom})
    assert fake.failed[0][2].status == FAILED


def test_unknown_job_type_fails_without_running_anything(queue):
    job = Job(id="j4", job_type="deliver_to_nowhere", payload={}, attempts=1)
    fake = queue(job)
    outbox.run_once({"noop": lambda payload: pytest.fail("must not run")})
    assert fake.failed[0][1] == "unknown job type: deliver_to_nowhere"
    assert fake.failed[0][2].status == FAILED


def test_error_text_is_bounded_and_carries_no_payload(queue):
    job = Job(id="j5", job_type="noop", payload={"email": "person@example.test"}, attempts=1)
    fake = queue(job)

    def boom(payload):
        raise RuntimeError("x" * 2000)

    outbox.run_once({"noop": boom})
    error = fake.failed[0][1]
    assert len(error) <= outbox.ERROR_MAX_LENGTH
    assert "person@example.test" not in error


def test_builtin_noop_handler_is_registered():
    assert "noop" in outbox.HANDLERS
    outbox.HANDLERS["noop"]({})
