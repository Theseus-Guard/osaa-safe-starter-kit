from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from safe_kit.deadlines import (
    BusinessCalendar,
    deadline_start,
    deadlines_for,
    next_weekly_update,
)

ISTANBUL = ZoneInfo("Europe/Istanbul")
BERLIN = ZoneInfo("Europe/Berlin")


def calendar(
    holidays: frozenset[date] = frozenset(), tz: ZoneInfo = ISTANBUL
) -> BusinessCalendar:
    return BusinessCalendar(timezone=tz, holidays=holidays)


def test_72_hours_is_elapsed_time_across_a_weekend():
    t0 = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)  # Friday
    result = deadlines_for(t0, calendar())
    assert result.affected_customers == datetime(
        2026, 11, 9, 10, 0, tzinfo=UTC
    )  # Monday


def test_four_business_days_skip_weekend_from_friday():
    t0 = datetime(2026, 11, 6, 9, 0, tzinfo=ISTANBUL)  # Friday, 09:00 local
    result = deadlines_for(t0, calendar())
    # Mon(1), Tue(2), Wed(3), Thu(4) -> Thursday 09:00 local (06:00 UTC)
    assert result.preliminary_safe_report == datetime(2026, 11, 12, 6, 0, tzinfo=UTC)


def test_holiday_is_not_counted_as_business_day():
    t0 = datetime(2026, 11, 2, 9, 0, tzinfo=ISTANBUL)  # Monday, 09:00 local
    holiday = frozenset({date(2026, 11, 3)})  # Tuesday
    result = deadlines_for(t0, calendar(holidays=holiday))
    # Wed(1), Thu(2), Fri(3), Mon(4) -> Monday 09:00 local (06:00 UTC)
    assert result.preliminary_safe_report == datetime(2026, 11, 9, 6, 0, tzinfo=UTC)


def test_business_day_keeps_wall_clock_time_across_dst_change():
    # Berlin switches to summer time on 2027-03-28.
    t0 = datetime(2027, 3, 25, 9, 0, tzinfo=BERLIN)  # Thursday
    result = deadlines_for(t0, calendar(tz=BERLIN))
    # Fri 26(1), Mon 29(2), Tue 30(3), Wed 31(4) -> 09:00 CEST = 07:00 UTC
    assert result.preliminary_safe_report == datetime(2027, 3, 31, 7, 0, tzinfo=UTC)


def test_business_days_are_counted_on_the_local_calendar_not_in_utc():
    # Saturday 01:00 in Istanbul is still Friday 22:00 UTC. The count starts from the local Saturday.
    t0 = datetime(2026, 11, 7, 1, 0, tzinfo=ISTANBUL)
    result = deadlines_for(t0, calendar())
    # Mon(1), Tue(2), Wed(3), Thu(4) -> Thursday 01:00 local, which is Wednesday 22:00 UTC
    assert result.preliminary_safe_report == datetime(2026, 11, 11, 22, 0, tzinfo=UTC)


def test_four_business_days_skip_a_weekend_and_a_holiday_together():
    t0 = datetime(2026, 11, 5, 9, 0, tzinfo=ISTANBUL)  # Thursday, 09:00 local
    holiday = frozenset({date(2026, 11, 6)})  # Friday
    result = deadlines_for(t0, calendar(holidays=holiday))
    # Fri is a holiday, Sat and Sun are weekend: Mon(1), Tue(2), Wed(3), Thu(4) -> Thursday 09:00 local
    assert result.preliminary_safe_report == datetime(2026, 11, 12, 6, 0, tzinfo=UTC)


def test_business_day_keeps_wall_clock_time_across_the_autumn_dst_change():
    # Berlin switches back to standard time on 2026-10-25.
    t0 = datetime(2026, 10, 22, 9, 0, tzinfo=BERLIN)  # Thursday, 09:00 CEST = 07:00 UTC
    result = deadlines_for(t0, calendar(tz=BERLIN))
    # Fri 23(1), Mon 26(2), Tue 27(3), Wed 28(4) -> 09:00 CET = 08:00 UTC
    assert result.preliminary_safe_report == datetime(2026, 10, 28, 8, 0, tzinfo=UTC)


def test_hour_deadline_is_elapsed_time_across_a_dst_change():
    t0 = datetime(
        2026, 10, 23, 9, 0, tzinfo=BERLIN
    )  # 07:00 UTC, before the change on 2026-10-25
    result = deadlines_for(t0, calendar(tz=BERLIN))
    assert result.affected_customers == datetime(2026, 10, 26, 7, 0, tzinfo=UTC)


def test_fixed_elapsed_deadlines():
    t0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    result = deadlines_for(t0, calendar())
    assert result.broader_advisory == datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    assert result.preliminary_public_report == datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    assert result.remediation_status == datetime(2027, 1, 30, 12, 0, tzinfo=UTC)


def test_naive_t0_is_rejected():
    with pytest.raises(ValueError):
        deadlines_for(datetime(2026, 11, 1, 12, 0), calendar())


def test_day_count_deadlines_are_elapsed_time_across_a_dst_change():
    # Berlin is on summer time on 2026-10-01 and switches to standard time on 2026-10-25. The 14, 30 and 90 days
    # are elapsed time, so the due times stay at 07:00 UTC and do not follow the 09:00 wall clock.
    t0 = datetime(2026, 10, 1, 9, 0, tzinfo=BERLIN)
    start_utc = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
    result = deadlines_for(t0, calendar(tz=BERLIN))
    assert result.broader_advisory == start_utc + timedelta(days=14)
    assert result.preliminary_public_report == start_utc + timedelta(days=30)
    assert result.remediation_status == start_utc + timedelta(days=90)


def test_weekly_update_is_seven_elapsed_days():
    last = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    assert next_weekly_update(last) == datetime(2026, 11, 8, 8, 0, tzinfo=UTC)


def test_weekly_update_is_seven_elapsed_days_across_a_dst_change():
    # The weekly cadence counts elapsed time: 2026-10-22 09:00 CEST plus 7 days is 08:00 CET on 2026-10-29.
    last = datetime(2026, 10, 22, 7, 0, tzinfo=UTC)
    assert next_weekly_update(last) == datetime(2026, 10, 29, 7, 0, tzinfo=UTC)


def test_t0_is_the_awareness_time_when_no_operator_event_exists():
    awareness = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    assert deadline_start(awareness, []) == awareness


def test_t0_is_the_earliest_operator_event_when_it_is_before_the_awareness_time():
    awareness = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    earlier = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    later = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert deadline_start(awareness, [later, earlier]) == earlier


def test_t0_never_moves_later_than_the_awareness_time():
    awareness = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    later_operator_event = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    assert deadline_start(awareness, [later_operator_event]) == awareness


def test_t0_is_returned_in_utc_and_naive_times_are_refused():
    local = datetime(2026, 11, 2, 11, 0, tzinfo=ISTANBUL)
    assert deadline_start(local, []) == datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        deadline_start(datetime(2026, 11, 2, 8, 0), [])
