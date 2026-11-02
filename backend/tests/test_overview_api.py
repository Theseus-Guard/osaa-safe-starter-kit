"""Overview dashboard endpoints (safe_kit.overview_api) and the pure parts of safe_kit.overview.

The database is never touched. overview.connect is replaced by a stub, and every query helper records its call, so
a test can assert that a denied section did not run its query at all.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, overview
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import User

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
OWN_CASE_ROLES = {"reporter", "triager", "investigator", "reviewer"}
ADMIN_ONLY = {"admin"}
ALL_ROLES = (
    "reporter",
    "triager",
    "investigator",
    "reviewer",
    "disclosure_approver",
    "auditor",
    "reporting_owner",
    "admin",
)
# Auditor and admin: the audit and ops sections.
OPS_ROLES = {"auditor", "admin"}

OWN_COUNTS = {"open_cases": 3, "opened_in_period": 1, "deadlines_due_72h": 2, "deadlines_overdue": 1}
CASE_COUNTS = {"open_cases": 9, "opened_in_period": 4}


def make_user(role: str) -> User:
    return User(
        id="11111111-1111-1111-1111-111111111111",
        username=f"{role}-user",
        role=role,
        is_active=True,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


class _NoConnection:
    def __enter__(self):
        return None

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def calls(monkeypatch) -> list[str]:
    """Stubs every query. Each stub appends its name here, so a test can check which queries ran."""
    recorded: list[str] = []

    def stub(name, value):
        def run(*args, **kwargs):
            recorded.append(name)
            return value

        return run

    monkeypatch.setattr(overview, "connect", lambda: _NoConnection())
    monkeypatch.setattr(overview, "current_timezone_name", lambda: "Europe/Istanbul")
    monkeypatch.setattr(overview, "_own_case_counts", stub("own_cases", OWN_COUNTS))
    monkeypatch.setattr(overview, "_case_counts", stub("case_counts", CASE_COUNTS))
    monkeypatch.setattr(overview, "_failed_outbox_jobs", stub("failed_outbox", 2))
    monkeypatch.setattr(overview, "_worker_status", stub("worker", {"status": "ok", "heartbeat_age_seconds": 4}))
    monkeypatch.setattr(overview, "_pending_password_changes", stub("pending_password", 1))
    monkeypatch.setattr(overview, "timeseries", lambda metric, window: recorded.append(f"timeseries:{metric}") or [])
    monkeypatch.setattr(
        overview,
        "deadline_summary",
        lambda user_id, now: recorded.append("deadlines")
        or [{"kind": kind, "open": 0, "overdue": 0, "done": 0, "not_applicable": 0} for kind in overview.DEADLINE_KINDS],
    )
    monkeypatch.setattr(
        overview,
        "audit_summary",
        lambda window: recorded.append("audit") or {"actions": [], "points": []},
    )
    monkeypatch.setattr(
        overview,
        "users_summary",
        lambda: recorded.append("users") or {"total": 0, "pending_password_change": 0, "roles": []},
    )
    monkeypatch.setattr(
        overview,
        "outbox_summary",
        lambda now: recorded.append("outbox") or {"counts": {}, "oldest_pending_age_seconds": None},
    )
    monkeypatch.setattr(
        overview,
        "worker_status",
        lambda now: recorded.append("worker_status") or {"status": "no_data", "heartbeat_age_seconds": None},
    )
    return recorded


@pytest.fixture
def as_role(monkeypatch):
    def client_for(role: str) -> TestClient:
        user = make_user(role)
        api.app.dependency_overrides[auth.current_user] = lambda: user
        monkeypatch.setattr(
            auth,
            "permissions_for_role",
            lambda name: DEFAULT_ROLE_PERMISSIONS.get(name, frozenset()),
        )
        return TestClient(api.app)

    yield client_for
    api.app.dependency_overrides.clear()


def _sections_shown(body: dict) -> set[str]:
    return {name for name in ("own_cases", "case_activity", "users", "ops") if body[name] is not None}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("reporter", {"own_cases"}),
        ("triager", {"own_cases"}),
        ("investigator", {"own_cases"}),
        ("reviewer", {"own_cases"}),
        ("auditor", {"case_activity", "ops"}),
        ("admin", {"case_activity", "ops", "users"}),
        # Not in the plan's role matrix: no overview section.
        ("disclosure_approver", set()),
        ("reporting_owner", set()),
    ],
)
def test_summary_shows_only_the_sections_of_the_role(as_role, calls, role, expected):
    response = as_role(role).get("/api/v1/overview/summary")

    assert response.status_code == 200
    assert _sections_shown(response.json()) == expected


def test_summary_denied_sections_run_no_query(as_role, calls):
    as_role("auditor").get("/api/v1/overview/summary")

    assert "own_cases" not in calls
    assert "pending_password" not in calls
    assert "case_counts" in calls


def test_summary_admin_gets_counts_for_each_section(as_role, calls):
    body = as_role("admin").get("/api/v1/overview/summary").json()

    assert body["own_cases"] is None
    assert body["case_activity"] == CASE_COUNTS
    assert body["ops"] == {"failed_outbox_jobs": 2, "status": "ok", "heartbeat_age_seconds": 4}
    assert body["users"] == {"pending_password_change": 1}
    assert body["days"] == 30
    assert body["timezone"] == "Europe/Istanbul"


def test_summary_own_case_counts_for_a_case_role(as_role, calls):
    body = as_role("triager").get("/api/v1/overview/summary?days=7").json()

    assert body["own_cases"] == OWN_COUNTS
    assert body["days"] == 7


@pytest.mark.parametrize("role", ALL_ROLES)
def test_deadline_summary_is_for_own_case_roles_only(as_role, calls, role):
    response = as_role(role).get("/api/v1/cases/deadlines/summary")

    if role in OWN_CASE_ROLES:
        assert response.status_code == 200
        kinds = [item["kind"] for item in response.json()["items"]]
        assert kinds == list(overview.DEADLINE_KINDS)
    else:
        assert response.status_code == 403
        assert "deadlines" not in calls


@pytest.mark.parametrize("role", ALL_ROLES)
@pytest.mark.parametrize("path", ["/api/v1/audit/summary", "/api/v1/ops/outbox-summary", "/api/v1/ops/worker-status"])
def test_audit_and_ops_endpoints_are_for_auditor_and_admin(as_role, calls, role, path):
    response = as_role(role).get(path)

    if role in OPS_ROLES:
        assert response.status_code == 200
    else:
        assert response.status_code == 403
        assert not any(name in calls for name in ("audit", "outbox", "worker_status"))


@pytest.mark.parametrize("role", ALL_ROLES)
def test_users_summary_is_for_admin_only(as_role, calls, role):
    response = as_role(role).get("/api/v1/users/summary")

    if role in ADMIN_ONLY:
        assert response.status_code == 200
    else:
        assert response.status_code == 403
        assert "users" not in calls


@pytest.mark.parametrize(
    ("metric", "allowed"),
    [
        ("cases_created", OPS_ROLES),
        ("audit_events", OPS_ROLES),
        ("outbox_completed", OPS_ROLES),
        ("outbox_failed", OPS_ROLES),
        ("logins_success", ADMIN_ONLY),
        ("logins_failed", ADMIN_ONLY),
    ],
)
@pytest.mark.parametrize("role", ALL_ROLES)
def test_timeseries_metric_follows_the_role_matrix(as_role, calls, metric, allowed, role):
    response = as_role(role).get(f"/api/v1/metrics/timeseries?metric={metric}")

    if role in allowed:
        assert response.status_code == 200
        assert response.json()["metric"] == metric
    else:
        assert response.status_code == 403
        assert f"timeseries:{metric}" not in calls


@pytest.mark.parametrize("days", [7, 30, 90])
def test_timeseries_accepts_the_three_ranges(as_role, calls, days):
    response = as_role("admin").get(f"/api/v1/metrics/timeseries?metric=logins_failed&days={days}")

    assert response.status_code == 200
    assert response.json()["days"] == days


def test_timeseries_default_range_is_30(as_role, calls):
    response = as_role("admin").get("/api/v1/metrics/timeseries?metric=logins_failed")

    assert response.status_code == 200
    assert response.json()["days"] == 30


@pytest.mark.parametrize("days", [0, 1, 14, 60, 91, 365, -7])
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/overview/summary",
        "/api/v1/audit/summary",
        "/api/v1/metrics/timeseries?metric=audit_events",
    ],
)
def test_range_rejects_values_other_than_7_30_90(as_role, calls, path, days):
    separator = "&" if "?" in path else "?"

    response = as_role("admin").get(f"{path}{separator}days={days}")

    assert response.status_code == 422
    assert calls == []


def test_timeseries_rejects_unknown_metric(as_role, calls):
    response = as_role("admin").get("/api/v1/metrics/timeseries?metric=passwords")

    assert response.status_code == 422
    assert calls == []


def test_summary_answers_409_when_system_timezone_is_not_set(as_role, calls, monkeypatch):
    monkeypatch.setattr(overview, "current_timezone_name", lambda: None)

    response = as_role("admin").get("/api/v1/overview/summary")

    assert response.status_code == 409
    assert response.json()["detail"] == "system timezone is not set"


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]


class _FakeConnection:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return _FakeCursor(self._rows)


def test_deadline_summary_lists_every_kind_and_fills_missing_ones_with_zero(monkeypatch):
    rows = [("weekly_update", 2, 1, 4, 0)]
    monkeypatch.setattr(overview, "connect", lambda: _Connected(_FakeConnection(rows)))

    items = overview.deadline_summary("11111111-1111-1111-1111-111111111111", NOW)

    assert [item["kind"] for item in items] == list(overview.DEADLINE_KINDS)
    weekly = items[-1]
    assert weekly == {"kind": "weekly_update", "open": 2, "overdue": 1, "done": 4, "not_applicable": 0}
    assert items[0] == {"kind": "affected_customers", "open": 0, "overdue": 0, "done": 0, "not_applicable": 0}


class _Connected:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc_info):
        return False


def test_make_window_starts_at_local_midnight_of_the_first_day():
    # 22:30 UTC on 2 November is already 3 November in Istanbul (UTC+3).
    window = overview.make_window("Europe/Istanbul", 7, datetime(2026, 11, 2, 22, 30, tzinfo=UTC))

    assert window.dates[0] == date(2026, 10, 28)
    assert window.dates[-1] == date(2026, 11, 3)
    assert len(window.dates) == 7
    assert window.start == datetime(2026, 10, 28, 0, 0, tzinfo=ZoneInfo("Europe/Istanbul"))


def test_make_window_has_one_date_per_day_for_the_widest_range():
    window = overview.make_window("UTC", 90, NOW)

    assert len(window.dates) == 90
    assert window.dates == tuple(sorted(set(window.dates)))


def test_bucket_audit_rows_keeps_top_five_and_sums_the_rest_into_other():
    window = overview.make_window("UTC", 7, NOW)
    rows = [
        (date(2026, 11, 2), "a", 5),
        (date(2026, 11, 2), "b", 1),
        (date(2026, 11, 1), "c", 2),
        (date(2026, 11, 1), "d", 1),
        (date(2026, 11, 1), "e", 1),
        (date(2026, 11, 1), "f", 1),
        (date(2026, 11, 1), "g", 1),
        # Outside the window: ignored, and not counted in the totals.
        (date(2026, 10, 1), "a", 99),
    ]

    result = overview.bucket_audit_rows(rows, window)

    assert result["actions"] == ["a", "c", "b", "d", "e", "other"]
    by_day = {point["date"]: point["counts"] for point in result["points"]}
    assert by_day["2026-11-02"] == {"a": 5, "c": 0, "b": 1, "d": 0, "e": 0, "other": 0}
    assert by_day["2026-11-01"] == {"a": 0, "c": 2, "b": 0, "d": 1, "e": 1, "other": 2}
    assert len(result["points"]) == 7


def test_bucket_audit_rows_without_rows_has_no_actions():
    window = overview.make_window("UTC", 7, NOW)

    result = overview.bucket_audit_rows([], window)

    assert result["actions"] == []
    assert all(point["counts"] == {} for point in result["points"])
