from safe_kit import worker


def test_worker_heartbeats_and_drains_jobs_then_sleeps_when_idle(monkeypatch):
    events = []
    jobs_left = iter([True, True, False])

    def fake_run_once():
        result = next(jobs_left)
        events.append("job" if result else "idle")
        return result

    def fake_sleep(seconds):
        events.append(("sleep", seconds))
        worker._running = False  # stop after the first idle pause

    monkeypatch.setattr(worker, "heartbeat", lambda: events.append("beat"))
    monkeypatch.setattr(
        worker, "reconcile_pending_uploads", lambda: events.append("reconcile") or 0
    )
    monkeypatch.setattr(
        worker, "create_weekly_updates", lambda: events.append("weekly") or 0
    )
    monkeypatch.setattr(worker, "run_once", fake_run_once)
    monkeypatch.setattr(worker.time, "sleep", fake_sleep)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    worker._running = True

    assert worker.main() == 0
    assert events == [
        "beat",
        "reconcile",
        "weekly",
        "job",
        "job",
        "idle",
        ("sleep", worker.IDLE_POLL_SECONDS),
    ]
    worker._running = True


def test_worker_failed_weekly_step_is_logged_by_type_and_the_loop_continues(
    monkeypatch, caplog
):
    events = []

    def failing_weekly():
        events.append("weekly")
        raise RuntimeError("secret detail that must not be logged")

    def fake_sleep(seconds):
        worker._running = False

    monkeypatch.setattr(worker, "heartbeat", lambda: events.append("beat"))
    monkeypatch.setattr(worker, "reconcile_pending_uploads", lambda: 0)
    monkeypatch.setattr(worker, "create_weekly_updates", failing_weekly)
    monkeypatch.setattr(worker, "run_once", lambda: events.append("idle") or False)
    monkeypatch.setattr(worker.time, "sleep", fake_sleep)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    worker._running = True

    with caplog.at_level("ERROR"):
        assert worker.main() == 0
    assert events == ["beat", "weekly", "idle"]
    assert "weekly update generation failed (RuntimeError)" in caplog.text
    assert "secret detail" not in caplog.text
    worker._running = True
