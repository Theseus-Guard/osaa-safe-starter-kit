"""External notice endpoints of a disclosure version (plans/f0/api-draft.md sections 2.5 and 3.9; safe_kit.external_notices).

Routes (under the case and the version, which must belong to it):
- POST   .../disclosures/{version_id}/notices                      record a notice that was sent (201)
- GET    .../disclosures/{version_id}/notices                      the latest revision of each notice, newest first
- GET    .../disclosures/{version_id}/notices/{notice_id}          one notice with its revision history
- POST   .../disclosures/{version_id}/notices/{notice_id}/receipt  record a receipt, or unknown
- POST   .../disclosures/{version_id}/notices/{notice_id}/follow-up  mark a follow-up required, or done
- POST   .../disclosures/{version_id}/notices/{notice_id}/cancel   cancel with a reason

Access rules:
- Reads need disclosures:read. The case must be visible to the caller (cases.is_visible); a case the caller may not see
  answers 404, the same as a missing one.
- Writes need disclosures:approve or disclosures:prepare. A caller with disclosures:approve records a notice on the
  normal path. A caller with only disclosures:prepare may record one only on the manual path: manual_fallback is true
  and a fallback_reason is given. Otherwise the answer is 403 with manual_fallback_required. Whatever the path, the
  person who prepared the version gets 403 with preparer_cannot_record: the sender of a notice is independent of the
  preparer.
- Every POST needs an Idempotency-Key (plans/f0/api-draft.md section 3.4). A repeated request with the same key and body
  returns the first answer and writes nothing.

Only a receipt from the recipient counts as received. Provider acceptance is recorded as receipt_unknown with the reason
provider_accepted_no_receipt, and the unknown answer needs a follow_up_reason: the follow-up is recorded with it, in the
same transaction (422 unknown_needs_follow_up otherwise). A notice names the recipient_ref the approver confirmed for
the version; any other reference is refused with 422 recipient_mismatch. Nothing here sends a notice.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from safe_kit import cases, disclosure, external_notices
from safe_kit.auth import permissions_of, require_any_permission, require_permission
from safe_kit.cases import CaseScope
from safe_kit.external_notices import Notice, Outcome, Result
from safe_kit.idempotency import checked_key, key_scope
from safe_kit.users import User

NOTICES_ROUTE = "/api/v1/cases/{case_id}/disclosures/{version_id}/notices"
NOTICE_ROUTE = "/api/v1/cases/{case_id}/disclosures/{version_id}/notices/{notice_id}"
RECEIPT_ROUTE = (
    "/api/v1/cases/{case_id}/disclosures/{version_id}/notices/{notice_id}/receipt"
)
FOLLOW_UP_ROUTE = (
    "/api/v1/cases/{case_id}/disclosures/{version_id}/notices/{notice_id}/follow-up"
)
CANCEL_ROUTE = (
    "/api/v1/cases/{case_id}/disclosures/{version_id}/notices/{notice_id}/cancel"
)
DEFAULT_PAGE = 50
MAX_PAGE = 200

WRITE_PERMISSIONS = ("disclosures:approve", "disclosures:prepare")

notice_router = APIRouter(tags=["disclosure"])

Reason = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=external_notices.REASON_LIMIT
    ),
]
ChannelOther = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=external_notices.CHANNEL_OTHER_LIMIT,
    ),
]


class _Strict(BaseModel):
    # An unknown field is refused, so a request cannot carry a field the notice does not hold.
    model_config = ConfigDict(extra="forbid")


class NoticeCreate(_Strict):
    channel: Literal["email", "postal", "webhook", "secure_manual", "other"]
    # Required for channel "other" and refused otherwise.
    channel_other: ChannelOther | None = None
    # An identifier of the recipient, kept outside SAFE. Names, addresses and free text are refused by the pattern.
    recipient_ref: str = Field(pattern=external_notices.RECIPIENT_REF_PATTERN)
    sent_at: AwareDatetime
    manual_fallback: bool = False
    # Required with manual_fallback, and refused without it.
    fallback_reason: Reason | None = None
    # Required when the same version and recipient already have a notice. Refused otherwise.
    repeat_reason: Reason | None = None

    @model_validator(mode="after")
    def fields_agree(self) -> "NoticeCreate":
        if (self.channel == "other") != (self.channel_other is not None):
            raise ValueError(
                "channel_other is required for channel other, and only then"
            )
        if self.manual_fallback != (self.fallback_reason is not None):
            raise ValueError(
                "fallback_reason is required with manual_fallback, and only then"
            )
        return self


class ReceiptRequest(_Strict):
    # received: the recipient's receipt, with its time. unknown: no receipt is known, with the reason.
    outcome: Literal["received", "unknown"]
    received_at: AwareDatetime | None = None
    reason_code: (
        Literal["provider_accepted_no_receipt", "timeout", "no_answer"] | None
    ) = None
    reason: Reason | None = None
    # Required with unknown (the notice then has a follow-up in the same change, and the answer is refused without it).
    # Refused with received.
    follow_up_reason: Reason | None = None

    @model_validator(mode="after")
    def outcome_fields_agree(self) -> "ReceiptRequest":
        if self.outcome == "received" and self.follow_up_reason is not None:
            raise ValueError("follow_up_reason is given only with an unknown receipt")
        if self.outcome == "received":
            if self.received_at is None or self.reason_code is not None:
                raise ValueError(
                    "a received receipt needs received_at and no reason_code"
                )
        else:
            if self.reason_code is None or self.received_at is not None:
                raise ValueError(
                    "an unknown receipt needs reason_code and no received_at"
                )
        return self


class FollowUpRequest(_Strict):
    action: Literal["require", "complete"]
    reason: Reason


class CancelRequest(_Strict):
    reason: Reason


# --- helpers --------------------------------------------------------------------------------------------------------


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _view(notice: Notice, with_history: bool = False) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": notice.id,
        "revision": notice.revision,
        "case_id": notice.case_id,
        "package_id": notice.package_id,
        "version_id": notice.version_id,
        "version": notice.version,
        "channel": notice.channel,
        "channel_other": notice.channel_other,
        "recipient_ref": notice.recipient_ref,
        "sent_at": _iso(notice.sent_at),
        "recorded_at": _iso(notice.recorded_at),
        "recorded_by": notice.recorded_by,
        "manual_fallback": notice.manual_fallback,
        "fallback_reason": notice.fallback_reason,
        "repeat_reason": notice.repeat_reason,
        "state": notice.state,
        "state_reason_code": notice.state_reason_code,
        "state_reason": notice.state_reason,
        "received_at": _iso(notice.received_at),
        "changed_by": notice.changed_by,
        "changed_at": _iso(notice.changed_at),
    }
    if with_history:
        view["history"] = [
            {
                "revision": entry.revision,
                "state": entry.state,
                "state_reason_code": entry.state_reason_code,
                "state_reason": entry.state_reason,
                "changed_by": entry.changed_by,
                "changed_at": _iso(entry.changed_at),
            }
            for entry in notice.history
        ]
    return view


# The status of a refused write, and the HTTP answer for it. Nothing about another object is ever in the answer.
_REFUSALS: dict[Outcome, tuple[int, dict[str, Any] | str]] = {
    Outcome.NOT_FOUND: (404, "case not found"),
    Outcome.VERSION_NOT_FOUND: (404, "disclosure version not found"),
    Outcome.NOTICE_NOT_FOUND: (404, "notice not found"),
    Outcome.CLOSED: (409, {"status": "case_closed"}),
    Outcome.NOT_APPROVED: (409, {"status": "version_not_approved"}),
    Outcome.SUPERSEDED: (409, {"status": "version_superseded"}),
    Outcome.APPROVAL_MISMATCH: (409, {"status": "approval_mismatch"}),
    Outcome.DIGEST_MISMATCH: (409, {"status": "digest_mismatch"}),
    Outcome.RELEASE_APPROVAL_MISSING: (409, {"status": "release_approval_missing"}),
    Outcome.RELEASE_TRACE_MISSING: (409, {"status": "release_trace_missing"}),
    Outcome.TIME_OUT_OF_RANGE: (422, {"status": "time_out_of_range"}),
    Outcome.REPEAT_REASON_REQUIRED: (422, {"status": "repeat_reason_required"}),
    Outcome.RECIPIENT_MISMATCH: (422, {"status": "recipient_mismatch"}),
    Outcome.UNKNOWN_NEEDS_FOLLOW_UP: (422, {"status": "unknown_needs_follow_up"}),
    Outcome.PREPARER_IS_RECORDER: (403, {"status": "preparer_cannot_record"}),
}


def _settle(result: Result) -> Any:
    """Turn a domain result into the response, or into the error the outcome maps to."""
    outcome = result.outcome
    if outcome is Outcome.REPLAY:
        assert result.stored is not None
        # The same request again: the first answer, with its status and body. Nothing was written.
        return JSONResponse(
            status_code=result.stored.status_code, content=result.stored.body
        )
    if outcome is Outcome.KEY_REUSED:
        raise HTTPException(
            status_code=409, detail={"status": "idempotency_key_reused"}
        )
    if outcome is Outcome.STATE_CONFLICT:
        # The current state is the only detail given: the caller may read the notice anyway.
        raise HTTPException(
            status_code=409,
            detail={"status": "state_conflict", "current_state": result.value},
        )
    if outcome in _REFUSALS:
        status_code, detail = _REFUSALS[outcome]
        raise HTTPException(status_code=status_code, detail=detail)
    assert result.value is not None
    return _view(result.value)


def _visible_version(
    case_id: uuid.UUID, version_id: uuid.UUID, scope: CaseScope
) -> None:
    """Read checks: the case must be visible, and the version must belong to it. Both answer 404 when they fail."""
    if not cases.is_visible(str(case_id), scope):
        raise HTTPException(status_code=404, detail="case not found")
    if disclosure.get_version(str(case_id), str(version_id), scope) is None:
        raise HTTPException(status_code=404, detail="disclosure version not found")


# --- routes ---------------------------------------------------------------------------------------------------------


@notice_router.post(NOTICES_ROUTE, status_code=201)
def record_external_notice(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    body: NoticeCreate,
    actor: User = Depends(require_any_permission(*WRITE_PERMISSIONS)),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    if not body.manual_fallback and "disclosures:approve" not in permissions_of(actor):
        # Only the approver records on the normal path. Anyone else needs the manual path and its reason.
        raise HTTPException(
            status_code=403, detail={"status": "manual_fallback_required"}
        )
    path = NOTICES_ROUTE.format(case_id=case_id, version_id=version_id)
    scope_key = key_scope(
        actor.id,
        "POST",
        NOTICES_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    new = external_notices.NewNotice(
        channel=body.channel,
        channel_other=body.channel_other,
        recipient_ref=body.recipient_ref,
        sent_at=body.sent_at,
        manual_fallback=body.manual_fallback,
        fallback_reason=body.fallback_reason,
        repeat_reason=body.repeat_reason,
    )
    result = external_notices.record_notice(
        str(case_id),
        str(version_id),
        new,
        _scope(actor),
        idempotency=scope_key,
        render=_view,
    )
    return _settle(result)


@notice_router.get(NOTICES_ROUTE)
def list_external_notices(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    actor: User = Depends(require_permission("disclosures:read")),
) -> dict[str, Any]:
    _visible_version(case_id, version_id, _scope(actor))
    notices = external_notices.list_notices(str(case_id), str(version_id), limit)
    return {"items": [_view(notice) for notice in notices]}


@notice_router.get(NOTICE_ROUTE)
def get_external_notice(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    notice_id: uuid.UUID,
    actor: User = Depends(require_permission("disclosures:read")),
) -> dict[str, Any]:
    _visible_version(case_id, version_id, _scope(actor))
    notice = external_notices.read_notice(str(case_id), str(version_id), str(notice_id))
    if notice is None:
        raise HTTPException(status_code=404, detail="notice not found")
    return _view(notice, with_history=True)


@notice_router.post(RECEIPT_ROUTE)
def record_external_notice_receipt(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    notice_id: uuid.UUID,
    body: ReceiptRequest,
    actor: User = Depends(require_any_permission(*WRITE_PERMISSIONS)),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = RECEIPT_ROUTE.format(
        case_id=case_id, version_id=version_id, notice_id=notice_id
    )
    scope_key = key_scope(
        actor.id,
        "POST",
        RECEIPT_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    if body.outcome == "received":
        assert body.received_at is not None
        change = external_notices.Change(
            state="receipt_received", received_at=body.received_at, reason=body.reason
        )
    else:
        change = external_notices.Change(
            state="receipt_unknown",
            reason_code=body.reason_code,
            reason=body.reason,
            follow_up_reason=body.follow_up_reason,
        )
    result = external_notices.record_receipt(
        str(case_id),
        str(version_id),
        str(notice_id),
        change,
        _scope(actor),
        idempotency=scope_key,
        render=_view,
    )
    return _settle(result)


@notice_router.post(FOLLOW_UP_ROUTE)
def record_external_notice_follow_up(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    notice_id: uuid.UUID,
    body: FollowUpRequest,
    actor: User = Depends(require_any_permission(*WRITE_PERMISSIONS)),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = FOLLOW_UP_ROUTE.format(
        case_id=case_id, version_id=version_id, notice_id=notice_id
    )
    scope_key = key_scope(
        actor.id,
        "POST",
        FOLLOW_UP_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    result = external_notices.record_follow_up(
        str(case_id),
        str(version_id),
        str(notice_id),
        body.action,
        body.reason,
        _scope(actor),
        idempotency=scope_key,
        render=_view,
    )
    return _settle(result)


@notice_router.post(CANCEL_ROUTE)
def cancel_external_notice(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    notice_id: uuid.UUID,
    body: CancelRequest,
    actor: User = Depends(require_any_permission(*WRITE_PERMISSIONS)),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = CANCEL_ROUTE.format(
        case_id=case_id, version_id=version_id, notice_id=notice_id
    )
    scope_key = key_scope(
        actor.id,
        "POST",
        CANCEL_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    result = external_notices.cancel_notice(
        str(case_id),
        str(version_id),
        str(notice_id),
        body.reason,
        _scope(actor),
        idempotency=scope_key,
        render=_view,
    )
    return _settle(result)
