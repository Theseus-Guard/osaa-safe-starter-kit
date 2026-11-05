"""F3 fixture for T08-A alarms on external notices (plans/03 T08-A: "receipt gecikmesi").

- The receipt delay alarm: a notice still in state 'sent' is overdue when more than the threshold of elapsed time has
  passed since sent_at. The plans do not define the threshold, so the tests pass their own values; no product default
  is asserted here.
- Only 'sent' is waiting for a receipt. A receipt, an unknown receipt (with its follow-up), a follow-up and a cancel all
  take the notice out of the alarm.
- The alarm only reads. It writes no revision and sends nothing.
- The threshold must be a positive duration and now must be timezone-aware.

Pure rule tests run without a database. The database tests need PostgreSQL (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safe_kit import external_notices
from safe_kit.db import connect
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import (  # dworld: fixture
    APPROVED_RECIPIENT_REF,
    dworld,
)
from test_fixture_t06a_redaction import world  # world: fixture
from test_fixture_t12_notices import REASON, receipt, recorded_notice, sent_now

# A threshold used only by these tests. It is not a product value.
TEST_THRESHOLD = timedelta(hours=2)
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def a_notice(state: str, sent_at: datetime = T0) -> external_notices.Notice:
    """One notice as read from the database, with the given state. Only the fields the alarm reads matter here."""
    return external_notices.Notice(
        id=str(uuid.uuid4()),
        revision=1,
        case_id=str(uuid.uuid4()),
        package_id=str(uuid.uuid4()),
        version_id=str(uuid.uuid4()),
        version=1,
        channel="email",
        channel_other=None,
        recipient_ref=APPROVED_RECIPIENT_REF,
        sent_at=sent_at,
        recorded_at=sent_at,
        recorded_by=str(uuid.uuid4()),
        manual_fallback=False,
        fallback_reason=None,
        repeat_reason=None,
        state=state,
        state_reason_code=None,
        state_reason=None,
        received_at=None,
        changed_by=str(uuid.uuid4()),
        changed_at=sent_at,
    )


# --- pure rules ------------------------------------------------------------------------------------------------------


def test_a_sent_notice_is_overdue_only_after_more_than_the_threshold_has_passed():
    notice = a_notice("sent")

    at_threshold = T0 + TEST_THRESHOLD
    just_after = at_threshold + timedelta(seconds=1)

    assert (
        external_notices.receipt_delay_overdue([notice], at_threshold, TEST_THRESHOLD)
        == []
    )
    assert external_notices.receipt_delay_overdue(
        [notice], just_after, TEST_THRESHOLD
    ) == [notice]


@pytest.mark.parametrize(
    "state",
    [
        "receipt_received",
        "receipt_unknown",
        "follow_up_required",
        "follow_up_done",
        "cancelled",
    ],
)
def test_only_a_notice_still_in_state_sent_is_waiting_for_a_receipt(state):
    notice = a_notice(state)

    much_later = T0 + timedelta(days=30)

    assert (
        external_notices.receipt_delay_overdue([notice], much_later, TEST_THRESHOLD)
        == []
    )


def test_the_overdue_filter_keeps_only_the_overdue_sent_notices_of_a_mixed_list():
    old_sent = a_notice("sent", sent_at=T0)
    fresh_sent = a_notice("sent", sent_at=T0 + timedelta(hours=5))
    old_received = a_notice("receipt_received", sent_at=T0)

    now = T0 + timedelta(hours=6)
    overdue = external_notices.receipt_delay_overdue(
        [old_sent, fresh_sent, old_received], now, TEST_THRESHOLD
    )

    assert [notice.id for notice in overdue] == [old_sent.id]


@pytest.mark.parametrize("threshold", [timedelta(0), timedelta(hours=-1)])
def test_the_threshold_must_be_a_positive_duration(threshold):
    with pytest.raises(ValueError):
        external_notices.receipt_delay_overdue([a_notice("sent")], T0, threshold)
    with pytest.raises(ValueError):
        external_notices.read_receipt_delay_alarms(T0, threshold, limit=10)


def test_now_must_be_timezone_aware():
    naive_now = datetime(2026, 11, 1, 12, 0)

    with pytest.raises(ValueError):
        external_notices.receipt_delay_overdue(
            [a_notice("sent")], naive_now, TEST_THRESHOLD
        )
    with pytest.raises(ValueError):
        external_notices.read_receipt_delay_alarms(naive_now, TEST_THRESHOLD, limit=10)


# --- database --------------------------------------------------------------------------------------------------------


def alarm_ids(now: datetime, threshold: timedelta = TEST_THRESHOLD) -> set[str]:
    return {
        notice.id
        for notice in external_notices.read_receipt_delay_alarms(
            now, threshold, limit=1000
        )
    }


def revision_count(notice_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM external_notices WHERE notice_id = %s", (notice_id,)
        ).fetchone()[0]


@requires_database
def test_a_sent_notice_raises_the_alarm_only_after_the_threshold_and_only_reads(
    dworld,
):
    version, notice = recorded_notice(dworld)
    sent_at = datetime.fromisoformat(notice["sent_at"])
    before = revision_count(notice["id"])

    assert notice["id"] not in alarm_ids(sent_at + TEST_THRESHOLD)
    assert notice["id"] in alarm_ids(sent_at + TEST_THRESHOLD + timedelta(seconds=1))

    assert revision_count(notice["id"]) == before == 1


@requires_database
def test_a_receipt_takes_the_notice_out_of_the_alarm(dworld):
    version, notice = recorded_notice(dworld)
    sent_at = datetime.fromisoformat(notice["sent_at"])
    later = sent_at + TEST_THRESHOLD + timedelta(hours=1)
    assert notice["id"] in alarm_ids(later)

    response = receipt(
        dworld,
        version["id"],
        notice["id"],
        {"outcome": "received", "received_at": sent_now()},
    )
    assert response.status_code == 200, response.text

    assert notice["id"] not in alarm_ids(later)


@requires_database
def test_an_unknown_receipt_with_its_follow_up_is_not_a_receipt_delay_alarm(dworld):
    version, notice = recorded_notice(dworld, channel="webhook", recipient_ref="hook-3")
    sent_at = datetime.fromisoformat(notice["sent_at"])
    response = receipt(
        dworld,
        version["id"],
        notice["id"],
        {
            "outcome": "unknown",
            "reason_code": "provider_accepted_no_receipt",
            "follow_up_reason": REASON,
        },
    )
    assert response.status_code == 200, response.text

    assert notice["id"] not in alarm_ids(sent_at + timedelta(days=30))
