"""T12-A record endpoints of a disclosure version (F3 A8; safe_kit.external_notice_records; plans/03 T12-A).

Routes (under the case and the version, which must belong to it):
- POST   .../disclosures/{version_id}/escalations          record an escalation to an independent backup (201)
- GET    .../disclosures/{version_id}/escalations          the escalations of the version, newest first
- POST   .../disclosures/{version_id}/reviews/recipient   record a review of the recipient (201)
- POST   .../disclosures/{version_id}/reviews/package     record a review of the package (201)
- GET    .../disclosures/{version_id}/reviews             the reviews of the version, newest first; ?kind=recipient|package

Access rules:
- Reads need disclosures:read. The case must be visible to the caller (cases.is_visible) and the version must belong to it;
  otherwise the answer is 404, the same as a missing one.
- An escalation may be recorded by a holder of disclosures:approve or disclosures:prepare. The backup named in it must hold
  disclosures:approve and must not be the person who prepared the version. A backup who breaks either rule is refused
  (403 preparer_cannot_be_backup, 422 backup_not_approver or backup_not_found).
- A review needs disclosures:approve, and the reviewer may not be the person who prepared the version.
- Every POST needs an Idempotency-Key. A repeated request with the same key and body returns the first answer and writes
  nothing.

Nothing here sends a notice, approves a version or changes a deadline.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from safe_kit import cases, disclosure, external_notice_records as records
from safe_kit.auth import permissions_of, require_any_permission, require_permission
from safe_kit.cases import CaseScope
from safe_kit.external_notice_records import Escalation, Outcome, Result, Review
from safe_kit.external_notices import RECIPIENT_REF_PATTERN
from safe_kit.idempotency import checked_key, key_scope
from safe_kit.users import User

ESCALATIONS_ROUTE = "/api/v1/cases/{case_id}/disclosures/{version_id}/escalations"
REVIEWS_ROUTE = "/api/v1/cases/{case_id}/disclosures/{version_id}/reviews"
RECIPIENT_REVIEW_ROUTE = (
    "/api/v1/cases/{case_id}/disclosures/{version_id}/reviews/recipient"
)
PACKAGE_REVIEW_ROUTE = (
    "/api/v1/cases/{case_id}/disclosures/{version_id}/reviews/package"
)
DEFAULT_PAGE = 50
MAX_PAGE = 200

ESCALATE_PERMISSIONS = ("disclosures:approve", "disclosures:prepare")

record_router = APIRouter(tags=["disclosure"])

Text = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=records.TEXT_LIMIT
    ),
]


class _Strict(BaseModel):
    # An unknown field is refused, so a request cannot carry a field the record does not hold.
    model_config = ConfigDict(extra="forbid")


class EscalationCreate(_Strict):
    reason_code: Literal["unreachable", "conflict_of_interest"]
    authority_basis: Text
    missing_facts: Text
    next_update: Text
    backup_user_id: uuid.UUID


class RecipientReviewCreate(_Strict):
    # An identifier of the recipient, kept outside SAFE, as on a notice. Names, addresses and free text are refused.
    recipient_ref: str = Field(pattern=RECIPIENT_REF_PATTERN)


class PackageReviewCreate(_Strict):
    """A package review names nothing: the version digest is the content it looks at."""


# --- helpers --------------------------------------------------------------------------------------------------------


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _escalation_view(escalation: Escalation) -> dict[str, Any]:
    return {
        "id": escalation.id,
        "case_id": escalation.case_id,
        "package_id": escalation.package_id,
        "version_id": escalation.version_id,
        "reason_code": escalation.reason_code,
        "authority_basis": escalation.authority_basis,
        "missing_facts": escalation.missing_facts,
        "next_update": escalation.next_update,
        "backup_user_id": escalation.backup_user_id,
        "escalated_by": escalation.escalated_by,
        "escalated_at": _iso(escalation.escalated_at),
    }


def _review_view(review: Review) -> dict[str, Any]:
    return {
        "id": review.id,
        "review_kind": review.review_kind,
        "case_id": review.case_id,
        "package_id": review.package_id,
        "version_id": review.version_id,
        "version": review.version,
        "digest": review.digest,
        "recipient_ref": review.recipient_ref,
        "reviewed_by": review.reviewed_by,
        "reviewed_at": _iso(review.reviewed_at),
    }


# The status of a refused write. Nothing about another object is ever in the answer.
_REFUSALS: dict[Outcome, tuple[int, dict[str, Any] | str]] = {
    Outcome.NOT_FOUND: (404, "case not found"),
    Outcome.VERSION_NOT_FOUND: (404, "disclosure version not found"),
    Outcome.CLOSED: (409, {"status": "case_closed"}),
    Outcome.PREPARER_IS_BACKUP: (403, {"status": "preparer_cannot_be_backup"}),
    Outcome.BACKUP_NOT_FOUND: (422, {"status": "backup_not_found"}),
    Outcome.BACKUP_NOT_APPROVER: (422, {"status": "backup_not_approver"}),
    Outcome.PREPARER_IS_REVIEWER: (403, {"status": "preparer_cannot_review"}),
}


def _settle(result: Result, view: Any) -> Any:
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
    if outcome in _REFUSALS:
        status_code, detail = _REFUSALS[outcome]
        raise HTTPException(status_code=status_code, detail=detail)
    assert result.value is not None
    return view(result.value)


def _visible_version(
    case_id: uuid.UUID, version_id: uuid.UUID, scope: CaseScope
) -> None:
    """Read checks: the case must be visible, and the version must belong to it. Both answer 404 when they fail."""
    if not cases.is_visible(str(case_id), scope):
        raise HTTPException(status_code=404, detail="case not found")
    if disclosure.get_version(str(case_id), str(version_id), scope) is None:
        raise HTTPException(status_code=404, detail="disclosure version not found")


# --- routes ---------------------------------------------------------------------------------------------------------


@record_router.post(ESCALATIONS_ROUTE, status_code=201)
def record_escalation(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    body: EscalationCreate,
    actor: User = Depends(require_any_permission(*ESCALATE_PERMISSIONS)),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = ESCALATIONS_ROUTE.format(case_id=case_id, version_id=version_id)
    scope_key = key_scope(
        actor.id,
        "POST",
        ESCALATIONS_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    new = records.NewEscalation(
        reason_code=body.reason_code,
        authority_basis=body.authority_basis,
        missing_facts=body.missing_facts,
        next_update=body.next_update,
        backup_user_id=str(body.backup_user_id),
    )
    result = records.record_escalation(
        str(case_id),
        str(version_id),
        new,
        _scope(actor),
        idempotency=scope_key,
        render=_escalation_view,
    )
    return _settle(result, _escalation_view)


@record_router.get(ESCALATIONS_ROUTE)
def list_escalations(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    actor: User = Depends(require_permission("disclosures:read")),
) -> dict[str, Any]:
    _visible_version(case_id, version_id, _scope(actor))
    escalations = records.list_escalations(str(case_id), str(version_id), limit)
    return {"items": [_escalation_view(item) for item in escalations]}


@record_router.post(RECIPIENT_REVIEW_ROUTE, status_code=201)
def record_recipient_review(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    body: RecipientReviewCreate,
    actor: User = Depends(require_permission("disclosures:approve")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = RECIPIENT_REVIEW_ROUTE.format(case_id=case_id, version_id=version_id)
    scope_key = key_scope(
        actor.id,
        "POST",
        RECIPIENT_REVIEW_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    result = records.record_review(
        str(case_id),
        str(version_id),
        "recipient",
        body.recipient_ref,
        _scope(actor),
        idempotency=scope_key,
        render=_review_view,
    )
    return _settle(result, _review_view)


@record_router.post(PACKAGE_REVIEW_ROUTE, status_code=201)
def record_package_review(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    body: PackageReviewCreate,
    actor: User = Depends(require_permission("disclosures:approve")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = PACKAGE_REVIEW_ROUTE.format(case_id=case_id, version_id=version_id)
    scope_key = key_scope(
        actor.id,
        "POST",
        PACKAGE_REVIEW_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    result = records.record_review(
        str(case_id),
        str(version_id),
        "package",
        None,
        _scope(actor),
        idempotency=scope_key,
        render=_review_view,
    )
    return _settle(result, _review_view)


@record_router.get(REVIEWS_ROUTE)
def list_reviews(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    kind: Literal["recipient", "package"] | None = None,
    limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    actor: User = Depends(require_permission("disclosures:read")),
) -> dict[str, Any]:
    _visible_version(case_id, version_id, _scope(actor))
    reviews = records.list_reviews(str(case_id), str(version_id), kind, limit)
    return {"items": [_review_view(item) for item in reviews]}
