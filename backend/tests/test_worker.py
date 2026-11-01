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
    monkeypatch.setattr(worker, "run_once", fake_run_once)
    monkeypatch.setattr(worker.time, "sleep", fake_sleep)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    worker._running = True

    assert worker.main() == 0
    assert events == ["beat", "job", "job", "idle", ("sleep", worker.IDLE_POLL_SECONDS)]
    worker._running = True
