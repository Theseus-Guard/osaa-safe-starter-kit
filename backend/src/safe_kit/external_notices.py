"""External notices of a disclosure version (F2, package P8; plans/03 test T12, T12-B; plans/04 I0 manual notice).

A notice records that a person sent one approved disclosure version to one recipient, through one channel, at one time,
and what came back. The application never sends a notice and never resends one. Recording is manual, so every fact of a
notice comes from the person who records it.

States:
- sent: the send is recorded and no receipt answer has been recorded yet.
- receipt_received: a receipt from the recipient was recorded (received_at). Provider acceptance is not a receipt.
- receipt_unknown: no receipt is known. A reason code says why: provider_accepted_no_receipt, timeout, or no_answer.
  Nothing moves a notice from unknown to received except a later receipt that is recorded. The unknown answer is
  accepted only with a follow-up reason. The follow-up is written in the same transaction, as the next revision
  (follow_up_required), so a notice that is unknown is never left without a follow-up. A notice that was unknown
  before this rule, and has no follow-up, stays unknown: it blocks case closure until a follow-up is recorded.
- follow_up_required / follow_up_done: someone must chase the recipient, and then the chase is recorded as done.
- cancelled: the notice is withdrawn from the record, with a reason. Only a notice that has no receipt can be cancelled.
There is no state that means delivered without a receipt.

Rules:
- A notice may be recorded only against a version that passes disclosure.check_release: approved, still the latest version
  of its package, digest intact, for CLEAR released, and with an open release attempt (migration 0047). Recording writes
  that attempt first, in the same transaction, and closes it as done with the notice, or as refused with the reason. A
  refused attempt never counts as a release. Later versions do not change an existing notice; the notice keeps the version
  it was sent with. Receipts, follow-ups and cancels are still possible after a later version exists.
- Recipient binding: the recipient_ref of a notice must equal the recipient_ref the approver confirmed for the version
  (disclosure_approvals.recipient_ref, migration 0049). Any other reference is refused with recipient_mismatch, and so is
  a version approved before migration 0049, which has no reference.
- sent_at must not be before the approval and not in the future (a small clock allowance applies).
- A second notice for the same version and recipient reference is a new record. It needs a repeat_reason. Without one the
  record is refused, so a repeated request never creates a duplicate by itself.
- Manual fallback: a person who may prepare packages but does not hold disclosures:approve records with manual_fallback
  and a fallback_reason. The reason is required. Deadlines are not changed by a notice.
- Independence: the person who prepared a version may not record a notice of it, on the normal or the manual path. The
  refusal is written before any release attempt, so nothing is stored for it.
- Internal reminders are not external notices. Nothing in the deadline or weekly update code writes to this table, and a
  notice has no link to a deadline task.
- Each change is a new revision of the same notice (migration 0042). The rows are insert-only and are never deleted.
- Free text (reasons) is kept in the notice rows and is not written to the audit log; the audit log keeps lengths only.
- Every change runs in one transaction with its audit row. The case row is locked first, so two changes to one notice are
  serialised and the second one sees the first one's state.
- Receipt delay alarm (plans/03 T08-A): a notice still in state 'sent' is overdue when more than a threshold of elapsed
  time has passed since sent_at. The plans do not give that threshold, so it has no default and the caller must pass it.
  The alarm only reads; it sends nothing and has no recipient yet.
"""

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from safe_kit import audit, disclosure
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.idempotency import KeyReused, KeyScope, Replayed, check, remember

CHANNELS = ("email", "postal", "webhook", "secure_manual", "other")
UNKNOWN_REASON_CODES = ("provider_accepted_no_receipt", "timeout", "no_answer")
STATES = (
    "sent",
    "receipt_received",
    "receipt_unknown",
    "follow_up_required",
    "follow_up_done",
    "cancelled",
)
RECIPIENT_REF_PATTERN = disclosure.RECIPIENT_REF_PATTERN
REASON_LIMIT = 500
CHANNEL_OTHER_LIMIT = 200
CLOCK_ALLOWANCE = timedelta(minutes=1)

# The states a receipt, a follow-up step, or a cancel may start from.
_RECEIPT_FROM = ("sent", "receipt_unknown", "follow_up_required", "follow_up_done")
_FOLLOW_UP_REQUIRE_FROM = ("sent", "receipt_unknown", "follow_up_done")
_FOLLOW_UP_COMPLETE_FROM = ("follow_up_required",)
_CANCEL_FROM = ("sent", "receipt_unknown", "follow_up_required", "follow_up_done")


class Outcome(Enum):
    OK = "ok"
    REPLAY = "replay"
    KEY_REUSED = "key_reused"
    # The case is missing, deleted, or not visible to the caller.
    NOT_FOUND = "not_found"
    VERSION_NOT_FOUND = "version_not_found"
    NOTICE_NOT_FOUND = "notice_not_found"
    # The caller prepared the version, so the caller may not record its notice (independence of the sender).
    PREPARER_IS_RECORDER = "preparer_cannot_record"
    CLOSED = "closed"
    # The release check of the version failed (see disclosure.ReleaseOutcome).
    NOT_APPROVED = "version_not_approved"
    SUPERSEDED = "version_superseded"
    APPROVAL_MISMATCH = "approval_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    RELEASE_APPROVAL_MISSING = "release_approval_missing"
    RELEASE_TRACE_MISSING = "release_trace_missing"
    TIME_OUT_OF_RANGE = "time_out_of_range"
    REPEAT_REASON_REQUIRED = "repeat_reason_required"
    # The notice names another recipient than the one the approver confirmed for the version.
    RECIPIENT_MISMATCH = "recipient_mismatch"
    # An unknown receipt was given without the follow-up reason that must be recorded with it.
    UNKNOWN_NEEDS_FOLLOW_UP = "unknown_needs_follow_up"
    # The notice is not in a state that allows the change. value holds the current state.
    STATE_CONFLICT = "state_conflict"


@dataclass(frozen=True)
class Revision:
    revision: int
    state: str
    state_reason_code: str | None
    state_reason: str | None
    changed_by: str
    changed_at: datetime


@dataclass(frozen=True)
class Notice:
    """The latest revision of one notice. history holds every revision, oldest first, when the caller asked for it."""

    id: str
    revision: int
    case_id: str
    package_id: str
    version_id: str
    version: int
    channel: str
    channel_other: str | None
    recipient_ref: str
    sent_at: datetime
    recorded_at: datetime
    recorded_by: str
    manual_fallback: bool
    fallback_reason: str | None
    repeat_reason: str | None
    state: str
    state_reason_code: str | None
    state_reason: str | None
    received_at: datetime | None
    changed_by: str
    changed_at: datetime
    history: tuple[Revision, ...] = ()


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    value: Any = None
    stored: Any = None


@dataclass(frozen=True)
class NewNotice:
    """The facts a person records when a notice is sent."""

    channel: str
    channel_other: str | None
    recipient_ref: str
    sent_at: datetime
    manual_fallback: bool = False
    fallback_reason: str | None = None
    repeat_reason: str | None = None


@dataclass(frozen=True)
class Change:
    """One change to a notice: the new state and the reason that the change records.

    follow_up_reason is used only with receipt_unknown: it is recorded as the follow-up in the same transaction.
    """

    state: str
    reason_code: str | None = None
    reason: str | None = None
    received_at: datetime | None = None
    follow_up_reason: str | None = None


_COLUMNS = (
    "notice_id::text, revision, case_id::text, package_id::text, version_id::text, version, channel, channel_other, "
    "recipient_ref, sent_at, recorded_at, recorded_by::text, manual_fallback, fallback_reason, repeat_reason, state, "
    "state_reason_code, state_reason, received_at, changed_by::text, changed_at"
)


def _from_row(row: tuple, history: tuple[Revision, ...] = ()) -> Notice:
    return Notice(
        id=row[0],
        revision=row[1],
        case_id=row[2],
        package_id=row[3],
        version_id=row[4],
        version=row[5],
        channel=row[6],
        channel_other=row[7],
        recipient_ref=row[8],
        sent_at=row[9],
        recorded_at=row[10],
        recorded_by=row[11],
        manual_fallback=row[12],
        fallback_reason=row[13],
        repeat_reason=row[14],
        state=row[15],
        state_reason_code=row[16],
        state_reason=row[17],
        received_at=row[18],
        changed_by=row[19],
        changed_at=row[20],
        history=history,
    )


# --- database helpers -----------------------------------------------------------------------------------------------


def _time_allowed(moment: datetime, not_before: datetime) -> bool:
    """A time the person states must not be before the event it follows, nor in the future (small clock allowance)."""
    return not_before <= moment <= datetime.now(UTC) + CLOCK_ALLOWANCE


def _lock_case(conn, case_id: str, scope: CaseScope, version_id: str) -> str | None:
    """Lock the case row, then check the caller's write visibility. Returns the case status, or None when not visible.

    The version is named because an approver writes a notice through the packet rule (safe_kit.disclosure); every other
    caller is checked by the case rule alone, as before.
    """
    return disclosure.lock_case_for_version(conn, case_id, scope, version_id)


def _latest(
    conn,
    case_id: str,
    version_id: str | None = None,
    notice_id: str | None = None,
    recipient_ref: str | None = None,
    limit: int | None = None,
) -> list[Notice]:
    """The latest revision of each notice that matches, newest first."""
    rows = conn.execute(
        f"""
        SELECT {_COLUMNS}
        FROM (
            SELECT DISTINCT ON (notice_id) *
            FROM external_notices
            WHERE case_id = %s AND NOT is_deleted
              AND (%s::uuid IS NULL OR version_id = %s::uuid)
              AND (%s::uuid IS NULL OR notice_id = %s::uuid)
              AND (%s::text IS NULL OR recipient_ref = %s::text)
            ORDER BY notice_id, revision DESC
        ) latest
        ORDER BY recorded_at DESC, notice_id DESC
        LIMIT %s
        """,
        (
            case_id,
            version_id,
            version_id,
            notice_id,
            notice_id,
            recipient_ref,
            recipient_ref,
            limit,
        ),
    ).fetchall()
    return [_from_row(row) for row in rows]


def _history(conn, notice_id: str) -> tuple[Revision, ...]:
    rows = conn.execute(
        """
        SELECT revision, state, state_reason_code, state_reason, changed_by::text, changed_at
        FROM external_notices
        WHERE notice_id = %s AND NOT is_deleted
        ORDER BY revision
        """,
        (notice_id,),
    ).fetchall()
    return tuple(Revision(*row) for row in rows)


def _insert_revision(
    conn,
    notice: Notice,
    revision: int,
    change: Change,
    scope: CaseScope,
) -> Notice:
    """Insert the next revision of a notice: a full copy of its facts, with the new state and its reason."""
    changed_at = conn.execute(
        """
        INSERT INTO external_notices (
            notice_id, revision, case_id, package_id, version_id, version, channel, channel_other, recipient_ref,
            sent_at, recorded_at, recorded_by, manual_fallback, fallback_reason, repeat_reason,
            state, state_reason_code, state_reason, received_at, changed_by, changed_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        RETURNING changed_at
        """,
        (
            notice.id,
            revision,
            notice.case_id,
            notice.package_id,
            notice.version_id,
            notice.version,
            notice.channel,
            notice.channel_other,
            notice.recipient_ref,
            notice.sent_at,
            notice.recorded_at,
            notice.recorded_by,
            notice.manual_fallback,
            notice.fallback_reason,
            notice.repeat_reason,
            change.state,
            change.reason_code,
            change.reason,
            change.received_at,
            scope.user_id,
        ),
    ).fetchone()[0]
    return Notice(
        id=notice.id,
        revision=revision,
        case_id=notice.case_id,
        package_id=notice.package_id,
        version_id=notice.version_id,
        version=notice.version,
        channel=notice.channel,
        channel_other=notice.channel_other,
        recipient_ref=notice.recipient_ref,
        sent_at=notice.sent_at,
        recorded_at=notice.recorded_at,
        recorded_by=notice.recorded_by,
        manual_fallback=notice.manual_fallback,
        fallback_reason=notice.fallback_reason,
        repeat_reason=notice.repeat_reason,
        state=change.state,
        state_reason_code=change.reason_code,
        state_reason=change.reason,
        received_at=change.received_at,
        changed_by=scope.user_id,
        changed_at=changed_at,
    )


# --- record a notice ------------------------------------------------------------------------------------------------

_RELEASE_OUTCOME = {
    disclosure.ReleaseOutcome.NOT_APPROVED: Outcome.NOT_APPROVED,
    disclosure.ReleaseOutcome.REVOKED_BY_CHANGE: Outcome.SUPERSEDED,
    disclosure.ReleaseOutcome.APPROVAL_MISMATCH: Outcome.APPROVAL_MISMATCH,
    disclosure.ReleaseOutcome.DIGEST_MISMATCH: Outcome.DIGEST_MISMATCH,
    disclosure.ReleaseOutcome.RELEASE_APPROVAL_MISSING: Outcome.RELEASE_APPROVAL_MISSING,
    disclosure.ReleaseOutcome.RELEASE_TRACE_MISSING: Outcome.RELEASE_TRACE_MISSING,
}


def _refuse_release(
    conn, attempt: disclosure.ReleaseAttempt, outcome: Outcome, reason_code: str
) -> Result:
    """Close the attempt as refused, in the same transaction, and return the refusal. Nothing else is written."""
    disclosure.close_release_attempt(conn, attempt, reason_code=reason_code)
    return Result(outcome)


def record_notice(
    case_id: str,
    version_id: str,
    new: NewNotice,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Notice], Any] | None = None,
) -> Result:
    """Record a notice that a person sent. The new notice starts in state 'sent'; nothing is sent by the application."""
    try:
        with connect() as conn:
            check(conn, idempotency)
            status = _lock_case(conn, case_id, scope, version_id)
            if status is None:
                return Result(Outcome.NOT_FOUND)
            if status == "closed":
                return Result(Outcome.CLOSED)
            version = disclosure.load_version_on(conn, case_id, version_id)
            if version is None:
                return Result(Outcome.VERSION_NOT_FOUND)
            if version.created_by == scope.user_id:
                return Result(Outcome.PREPARER_IS_RECORDER)
            # The release path: the attempt is written first, and every refusal after this point closes it as refused.
            attempt = disclosure.open_release_attempt(
                conn, version, disclosure.RELEASE_PURPOSE_NOTICE, scope.user_id
            )
            release = disclosure.release_check_on(
                conn, version, disclosure.RELEASE_PURPOSE_NOTICE
            )
            if release.outcome is not disclosure.ReleaseOutcome.RELEASABLE:
                return _refuse_release(
                    conn,
                    attempt,
                    _RELEASE_OUTCOME[release.outcome],
                    release.outcome.value,
                )
            assert version.approval is not None
            # The recipient binding: the reference must be the one the approver confirmed. A version approved before
            # migration 0049 has none, so no notice can be recorded for it.
            if version.approval.recipient_ref != new.recipient_ref:
                return _refuse_release(
                    conn, attempt, Outcome.RECIPIENT_MISMATCH, "recipient_mismatch"
                )
            if not _time_allowed(new.sent_at, version.approval.approved_at):
                return _refuse_release(
                    conn, attempt, Outcome.TIME_OUT_OF_RANGE, "time_out_of_range"
                )
            earlier = _latest(
                conn, case_id, version_id=version_id, recipient_ref=new.recipient_ref
            )
            if earlier and new.repeat_reason is None:
                return _refuse_release(
                    conn,
                    attempt,
                    Outcome.REPEAT_REASON_REQUIRED,
                    "repeat_reason_required",
                )
            notice_id = str(uuid.uuid4())
            recorded_at = conn.execute(
                """
                INSERT INTO external_notices (
                    notice_id, revision, case_id, package_id, version_id, version, channel, channel_other,
                    recipient_ref, sent_at, recorded_at, recorded_by, manual_fallback, fallback_reason,
                    repeat_reason, state, changed_by, changed_at
                )
                VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, 'sent', %s, now())
                RETURNING recorded_at
                """,
                (
                    notice_id,
                    case_id,
                    version.package_id,
                    version_id,
                    version.version,
                    new.channel,
                    new.channel_other,
                    new.recipient_ref,
                    new.sent_at,
                    scope.user_id,
                    new.manual_fallback,
                    new.fallback_reason,
                    new.repeat_reason,
                    scope.user_id,
                ),
            ).fetchone()[0]
            audit.record(
                conn,
                audit.DISCLOSURE_NOTICE_RECORD,
                "disclosure_notice",
                notice_id,
                actor_id=scope.user_id,
                details={
                    "case_id": case_id,
                    "version_id": version_id,
                    "package_id": version.package_id,
                    "version": version.version,
                    "revision": 1,
                    "channel": new.channel,
                    "recipient_ref": new.recipient_ref,
                    "state": "sent",
                    "manual_fallback": new.manual_fallback,
                    "repeat": bool(earlier),
                    "fallback_reason": {"length": len(new.fallback_reason or "")},
                },
            )
            disclosure.close_release_attempt(conn, attempt)
            created = Notice(
                id=notice_id,
                revision=1,
                case_id=case_id,
                package_id=version.package_id,
                version_id=version_id,
                version=version.version,
                channel=new.channel,
                channel_other=new.channel_other,
                recipient_ref=new.recipient_ref,
                sent_at=new.sent_at,
                recorded_at=recorded_at,
                recorded_by=scope.user_id,
                manual_fallback=new.manual_fallback,
                fallback_reason=new.fallback_reason,
                repeat_reason=new.repeat_reason,
                state="sent",
                state_reason_code=None,
                state_reason=None,
                received_at=None,
                changed_by=scope.user_id,
                changed_at=recorded_at,
            )
            if idempotency is not None:
                assert render is not None
                remember(conn, idempotency, 201, render(created))
        return Result(Outcome.OK, value=created)
    except Replayed as replay:
        return Result(Outcome.REPLAY, stored=replay.stored)
    except KeyReused:
        return Result(Outcome.KEY_REUSED)


# --- change a notice ------------------------------------------------------------------------------------------------


def _change(
    case_id: str,
    version_id: str,
    notice_id: str,
    allowed_from: tuple[str, ...],
    change: Change,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None,
    render: Callable[[Notice], Any] | None,
) -> Result:
    try:
        with connect() as conn:
            check(conn, idempotency)
            status = _lock_case(conn, case_id, scope, version_id)
            if status is None:
                return Result(Outcome.NOT_FOUND)
            if status == "closed":
                return Result(Outcome.CLOSED)
            found = _latest(conn, case_id, version_id=version_id, notice_id=notice_id)
            if not found:
                return Result(Outcome.NOTICE_NOT_FOUND)
            current = found[0]
            if current.state not in allowed_from:
                return Result(Outcome.STATE_CONFLICT, value=current.state)
            if change.received_at is not None and not _time_allowed(
                change.received_at, current.sent_at
            ):
                return Result(Outcome.TIME_OUT_OF_RANGE)
            # An unknown receipt is followed by its follow-up as the next revision, in this same transaction.
            steps = [change]
            if change.state == "receipt_unknown":
                steps.append(
                    Change(state="follow_up_required", reason=change.follow_up_reason)
                )
            previous = current
            for step in steps:
                updated = _insert_revision(
                    conn, previous, previous.revision + 1, step, scope
                )
                audit.record(
                    conn,
                    audit.DISCLOSURE_NOTICE_STATE,
                    "disclosure_notice",
                    notice_id,
                    actor_id=scope.user_id,
                    details={
                        "case_id": case_id,
                        "version_id": version_id,
                        "version": current.version,
                        "revision": {"old": previous.revision, "new": updated.revision},
                        "state": {"old": previous.state, "new": step.state},
                        "reason_code": step.reason_code,
                        "reason": {"length": len(step.reason or "")},
                    },
                )
                previous = updated
            if idempotency is not None:
                assert render is not None
                remember(conn, idempotency, 200, render(updated))
        return Result(Outcome.OK, value=updated)
    except Replayed as replay:
        return Result(Outcome.REPLAY, stored=replay.stored)
    except KeyReused:
        return Result(Outcome.KEY_REUSED)


def record_receipt(
    case_id: str,
    version_id: str,
    notice_id: str,
    change: Change,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Notice], Any] | None = None,
) -> Result:
    """Record the receipt answer: received (with received_at), or unknown (with a reason code). Provider acceptance is
    recorded as unknown, never as received. An unknown answer is accepted only with its follow-up reason: the follow-up
    is recorded in the same transaction, or nothing is recorded."""
    if change.state == "receipt_unknown" and not change.follow_up_reason:
        return Result(Outcome.UNKNOWN_NEEDS_FOLLOW_UP)
    return _change(
        case_id,
        version_id,
        notice_id,
        _RECEIPT_FROM,
        change,
        scope,
        idempotency=idempotency,
        render=render,
    )


def record_follow_up(
    case_id: str,
    version_id: str,
    notice_id: str,
    action: str,
    reason: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Notice], Any] | None = None,
) -> Result:
    """Mark a follow-up as required, or as done. Each step needs its reason."""
    if action == "require":
        return _change(
            case_id,
            version_id,
            notice_id,
            _FOLLOW_UP_REQUIRE_FROM,
            Change(state="follow_up_required", reason=reason),
            scope,
            idempotency=idempotency,
            render=render,
        )
    return _change(
        case_id,
        version_id,
        notice_id,
        _FOLLOW_UP_COMPLETE_FROM,
        Change(state="follow_up_done", reason=reason),
        scope,
        idempotency=idempotency,
        render=render,
    )


def cancel_notice(
    case_id: str,
    version_id: str,
    notice_id: str,
    reason: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Notice], Any] | None = None,
) -> Result:
    """Withdraw a notice with a reason. A notice with a receipt cannot be cancelled."""
    return _change(
        case_id,
        version_id,
        notice_id,
        _CANCEL_FROM,
        Change(state="cancelled", reason=reason),
        scope,
        idempotency=idempotency,
        render=render,
    )


# --- alarms ---------------------------------------------------------------------------------------------------------
# T08-A receipt delay (plans/03 T08-A: "receipt gecikmesi"). Only a notice in state 'sent' is waiting for a receipt. A
# receipt_unknown notice already has an answer and its follow-up (T12-B), so it is not counted here. The delay itself is
# not defined in the plans, so the threshold is always passed in by the caller.

RECEIPT_AWAITING_STATE = "sent"


def _check_alarm_inputs(now: datetime, threshold: timedelta) -> None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if threshold <= timedelta(0):
        raise ValueError("the receipt delay threshold must be a positive duration")


def receipt_delay_overdue(
    notices: Iterable[Notice], now: datetime, threshold: timedelta
) -> list[Notice]:
    """The notices that still await a receipt and whose sent_at is more than threshold before now.

    Elapsed time is used, and a notice exactly at the threshold is not yet overdue.
    """
    _check_alarm_inputs(now, threshold)
    return [
        notice
        for notice in notices
        if notice.state == RECEIPT_AWAITING_STATE and now - notice.sent_at > threshold
    ]


def read_receipt_delay_alarms(
    now: datetime, threshold: timedelta, limit: int
) -> list[Notice]:
    """The latest revision of each notice, across all cases, that is overdue for a receipt.

    Oldest send first. Read only: nothing is written and nothing is sent.
    """
    _check_alarm_inputs(now, threshold)
    cutoff = now - threshold
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_COLUMNS}
            FROM (
                SELECT DISTINCT ON (notice_id) *
                FROM external_notices
                WHERE NOT is_deleted
                  AND case_id IN (SELECT id FROM cases WHERE NOT is_deleted)
                ORDER BY notice_id, revision DESC
            ) latest
            WHERE state = %s AND sent_at < %s
            ORDER BY sent_at, notice_id
            LIMIT %s
            """,
            (RECEIPT_AWAITING_STATE, cutoff, limit),
        ).fetchall()
    return [_from_row(row) for row in rows]


# --- read -----------------------------------------------------------------------------------------------------------
# The API checks that the case is visible and that the version belongs to it (disclosure.get_version) before it calls
# these; they only read.


def list_notices(case_id: str, version_id: str, limit: int) -> list[Notice]:
    """The latest revision of each notice of one version, newest first."""
    with connect() as conn:
        return _latest(conn, case_id, version_id=version_id, limit=limit)


def read_notice(case_id: str, version_id: str, notice_id: str) -> Notice | None:
    """One notice with its revision history, or None when the notice is not in the version."""
    with connect() as conn:
        found = _latest(conn, case_id, version_id=version_id, notice_id=notice_id)
        if not found:
            return None
        return replace(found[0], history=_history(conn, notice_id))
