"""Overview dashboard endpoints (F2). Counts and dates only; see safe_kit.overview for the queries and the gates.

Mounted into the main app with app.include_router(overview_router), as the other feature routers are.

Every endpoint needs an authenticated caller (current_user). The role matrix is applied inside each endpoint: a section
the caller may not see answers 403 "forbidden", the same answer as the other permission checks. The stat cards
(/overview/summary) leave a section out as null instead, so one request serves every role.

The range (days) accepts 7, 30 or 90 and defaults to 30. Other values answer 422.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from safe_kit import overview
from safe_kit.auth import current_user, permissions_of
from safe_kit.overview import Window
from safe_kit.users import User

overview_router = APIRouter(tags=["overview"])

FORBIDDEN = HTTPException(status_code=403, detail="forbidden")


class CaseCards(BaseModel):
    open_cases: int
    opened_in_period: int


class OwnCaseCards(CaseCards):
    deadlines_due_72h: int
    deadlines_overdue: int


class UserCards(BaseModel):
    pending_password_change: int


class OpsCards(BaseModel):
    failed_outbox_jobs: int
    status: Literal["ok", "stale", "no_data"]
    heartbeat_age_seconds: int | None


class ApprovalTemplateCounts(BaseModel):
    template: str
    pending: int
    oldest_pending_age_days: int | None


class ApprovalCards(BaseModel):
    pending: int
    oldest_pending_age_days: int | None
    by_template: list[ApprovalTemplateCounts]


class WeeklyStatusCounts(BaseModel):
    status: Literal["open", "closed"]
    cases_with_significant_risk: int
    cases_with_weekly_update: int
    latest_update_date: str | None


class WeeklyUpdateCards(BaseModel):
    open_significant_risk_cases: int
    latest_update_date: str | None
    by_status: list[WeeklyStatusCounts]


class SummaryResponse(BaseModel):
    days: int
    timezone: str
    own_cases: OwnCaseCards | None
    case_activity: CaseCards | None
    users: UserCards | None
    ops: OpsCards | None
    approvals: ApprovalCards | None
    weekly_updates: WeeklyUpdateCards | None


class SeriesPoint(BaseModel):
    date: str
    count: int


class TimeseriesResponse(BaseModel):
    metric: str
    days: int
    timezone: str
    points: list[SeriesPoint]


class DeadlineKindCounts(BaseModel):
    kind: str
    open: int
    overdue: int
    done: int
    not_applicable: int


class DeadlineSummaryResponse(BaseModel):
    items: list[DeadlineKindCounts]


class AuditPoint(BaseModel):
    date: str
    counts: dict[str, int]


class AuditSummaryResponse(BaseModel):
    days: int
    timezone: str
    # The top five actions by total, then "other" when there are more.
    actions: list[str]
    points: list[AuditPoint]


class RoleCounts(BaseModel):
    role: str
    active: int
    inactive: int


class UsersSummaryResponse(BaseModel):
    total: int
    pending_password_change: int
    roles: list[RoleCounts]


class OutboxSummaryResponse(BaseModel):
    counts: dict[str, int]
    oldest_pending_age_seconds: int | None


class WorkerStatusResponse(BaseModel):
    status: Literal["ok", "stale", "no_data"]
    heartbeat_age_seconds: int | None


def _checked_days(days: int) -> int:
    if days not in overview.RANGE_DAYS:
        raise HTTPException(status_code=422, detail="days must be 7, 30 or 90")
    return days


def _window(days: int, now: datetime) -> Window:
    try:
        timezone = overview.system_timezone()
    except overview.SystemTimezoneNotSet:
        raise HTTPException(status_code=409, detail="system timezone is not set") from None
    return overview.make_window(timezone, days, now)


def _metric_allowed(metric: str, role: str, permissions: frozenset[str]) -> bool:
    if metric in ("logins_success", "logins_failed"):
        return overview.sees_logins(permissions)
    if metric in overview.MY_TIMESERIES_METRICS:
        return overview.sees_own_cases(permissions)
    return overview.sees_case_activity(role, permissions)


@overview_router.get("/api/v1/overview/summary", response_model=SummaryResponse)
def get_overview_summary(
    days: int = Query(default=overview.DEFAULT_RANGE_DAYS),
    actor: User = Depends(current_user),
) -> SummaryResponse:
    _checked_days(days)
    now = datetime.now(UTC)
    window = _window(days, now)
    sections: dict[str, Any] = overview.summary(actor.id, actor.role, permissions_of(actor), window, now)
    return SummaryResponse(days=days, timezone=window.timezone, **sections)


@overview_router.get("/api/v1/metrics/timeseries", response_model=TimeseriesResponse)
def get_metric_timeseries(
    metric: str = Query(...),
    days: int = Query(default=overview.DEFAULT_RANGE_DAYS),
    actor: User = Depends(current_user),
) -> TimeseriesResponse:
    if metric not in overview.TIMESERIES_METRICS:
        raise HTTPException(status_code=422, detail="unknown metric")
    _checked_days(days)
    if not _metric_allowed(metric, actor.role, permissions_of(actor)):
        raise FORBIDDEN
    now = datetime.now(UTC)
    window = _window(days, now)
    if metric in overview.MY_TIMESERIES_METRICS:
        points = overview.my_timeseries(metric, actor.id, permissions_of(actor), window)
    else:
        points = overview.timeseries(metric, window)
    return TimeseriesResponse(metric=metric, days=days, timezone=window.timezone, points=points)


@overview_router.get("/api/v1/cases/deadlines/summary", response_model=DeadlineSummaryResponse)
def get_deadline_summary(actor: User = Depends(current_user)) -> DeadlineSummaryResponse:
    if not overview.sees_own_cases(permissions_of(actor)):
        raise FORBIDDEN
    items = overview.deadline_summary(actor.id, datetime.now(UTC))
    return DeadlineSummaryResponse(items=items)


@overview_router.get("/api/v1/audit/summary", response_model=AuditSummaryResponse)
def get_audit_summary(
    days: int = Query(default=overview.DEFAULT_RANGE_DAYS),
    actor: User = Depends(current_user),
) -> AuditSummaryResponse:
    if not overview.sees_case_activity(actor.role, permissions_of(actor)):
        raise FORBIDDEN
    _checked_days(days)
    window = _window(days, datetime.now(UTC))
    result = overview.audit_summary(window)
    return AuditSummaryResponse(days=days, timezone=window.timezone, **result)


@overview_router.get("/api/v1/users/summary", response_model=UsersSummaryResponse)
def get_users_summary(actor: User = Depends(current_user)) -> UsersSummaryResponse:
    if not overview.sees_users(permissions_of(actor)):
        raise FORBIDDEN
    return UsersSummaryResponse(**overview.users_summary())


@overview_router.get("/api/v1/ops/outbox-summary", response_model=OutboxSummaryResponse)
def get_outbox_summary(actor: User = Depends(current_user)) -> OutboxSummaryResponse:
    if not overview.sees_case_activity(actor.role, permissions_of(actor)):
        raise FORBIDDEN
    return OutboxSummaryResponse(**overview.outbox_summary(datetime.now(UTC)))


@overview_router.get("/api/v1/ops/worker-status", response_model=WorkerStatusResponse)
def get_worker_status(actor: User = Depends(current_user)) -> WorkerStatusResponse:
    if not overview.sees_case_activity(actor.role, permissions_of(actor)):
        raise FORBIDDEN
    return WorkerStatusResponse(**overview.worker_status(datetime.now(UTC)))
