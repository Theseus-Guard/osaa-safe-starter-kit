"""Deadline calculation for SAFE notification timelines.

t0 is the time the reporting member became aware of facts giving reasonable grounds to
suspect a reportable event. Hour and day-count deadlines are elapsed time. The
"business days" deadline uses a provisional, versioned calendar until the owning
organization confirms its working calendar.

Legal notification duties run on their own clocks and are not computed here.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKLY_UPDATE_INTERVAL = timedelta(days=7)


def deadline_start(
    awareness_time: datetime, operator_occurrences: Iterable[datetime]
) -> datetime:
    """The t0 that the SAFE deadlines run from. This is the only place where the rule is written.

    t0 is the earliest of the case awareness_time and the recorded operator awareness events. It never moves later:
    an operator event recorded after the awareness_time does not delay the deadlines. Affected party and external
    report events are kept as awareness events of their own and never set t0, so the caller passes only the
    occurred_at values of the operator events.
    """
    if awareness_time.tzinfo is None:
        raise ValueError("t0 must be timezone-aware")
    start = min([awareness_time, *operator_occurrences])
    if start.tzinfo is None:
        raise ValueError("t0 must be timezone-aware")
    return start.astimezone(UTC)


@dataclass(frozen=True)
class BusinessCalendar:
    """Working-day rules in a local time zone. Weekends are never working days."""

    timezone: ZoneInfo
    holidays: frozenset[date] = field(default_factory=frozenset)
    version: str = "provisional-1"

    def is_business_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def add_business_days(self, start: datetime, days: int) -> datetime:
        """Move forward by `days` business days, keeping the local wall-clock time of `start`.

        The day of `start` is not counted. The result is returned in UTC.
        """
        local_start = start.astimezone(self.timezone)
        current = local_start.date()
        remaining = days
        while remaining:
            current += timedelta(days=1)
            if self.is_business_day(current):
                remaining -= 1
        local_result = datetime.combine(
            current, local_start.time(), tzinfo=self.timezone
        )
        return local_result.astimezone(UTC)


@dataclass(frozen=True)
class DeadlineSet:
    affected_customers: datetime
    preliminary_safe_report: datetime
    broader_advisory: datetime
    preliminary_public_report: datetime
    remediation_status: datetime


def deadlines_for(t0: datetime, calendar: BusinessCalendar) -> DeadlineSet:
    """Compute the standard notification deadlines from the awareness time t0."""
    if t0.tzinfo is None:
        raise ValueError("t0 must be timezone-aware")
    t0 = t0.astimezone(UTC)
    return DeadlineSet(
        affected_customers=t0 + timedelta(hours=72),
        preliminary_safe_report=calendar.add_business_days(t0, 4),
        broader_advisory=t0 + timedelta(days=14),
        preliminary_public_report=t0 + timedelta(days=30),
        remediation_status=t0 + timedelta(days=90),
    )


def next_weekly_update(last_update: datetime) -> datetime:
    """Weekly machine-readable updates are due every 7 elapsed days while risks stay open."""
    if last_update.tzinfo is None:
        raise ValueError("last_update must be timezone-aware")
    return last_update.astimezone(UTC) + WEEKLY_UPDATE_INTERVAL
