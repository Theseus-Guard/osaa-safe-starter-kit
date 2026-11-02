"""Conflict, recusal and appeal endpoints on review rounds (see review_independence.py).

Mounted into the main app with app.include_router(review_independence_router), as reviews_router is.

Access rules:
- Declaring a conflict and recording a recusal need reviews:write and an active assignment to the case. The case must be
  open. An invisible round answers 404, a visible round without the assignment answers 403.
- Filing an appeal needs a case read permission and a visible case. It is accepted on a closed case. No approval is
  asked from anybody: an appeal cannot be blocked. The system names the independent reviewer. The request has no
  reviewer field: a body that names one is refused with 422.
- Reading needs the same case read permissions as the case. A round the caller may not see answers 404.
- Free text (a conflict description, a recusal reason, appeal grounds) is returned only to callers with cases:read_all
  and to the caller who wrote it, as a layer reason is.
- Every side-effect POST needs an Idempotency-Key, as POST /api/v1/reviews does. Corrections are new entries that name
  the entry they supersede; no entry is ever edited.
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from safe_kit.auth import permissions_of, require_any_permission, require_any_permission_except_role, require_permission
from safe_kit.case_records_api import CASE_READ_PERMISSIONS_FOR_RECORDS
from safe_kit.cases import CaseScope
from safe_kit.rbac import DISCLOSURE_APPROVER_ROLE
from safe_kit.idempotency import KeyScope, fingerprint, valid_key
from safe_kit.review_independence import (
    GROUNDS_LIMIT,
    REASON_LIMIT,
    AppealEntry,
    ConflictEntry,
    IndependenceOutcome,
    RecusalEntry,
    declare_conflict,
    file_appeal,
    list_appeals,
    list_conflicts,
    list_recusals,
    record_recusal,
)
from safe_kit.users import User

CONFLICTS_ROUTE = "/api/v1/reviews/{review_id}/conflicts"
RECUSALS_ROUTE = "/api/v1/reviews/{review_id}/recusals"
APPEALS_ROUTE = "/api/v1/reviews/{review_id}/appeals"
ROUND_NOT_FOUND = HTTPException(status_code=404, detail="review not found")

# Refusals that every write shares. Each write adds its own refusals in its mapping below.
SHARED_REFUSALS: dict[IndependenceOutcome, HTTPException] = {
    IndependenceOutcome.NOT_FOUND: ROUND_NOT_FOUND,
    IndependenceOutcome.CLOSED: HTTPException(status_code=409, detail="case is closed"),
    IndependenceOutcome.SUPERSEDES_MISMATCH: HTTPException(
        status_code=409, detail={"status": "supersedes_mismatch"}
    ),
    IndependenceOutcome.KEY_REUSED: HTTPException(
        status_code=409, detail={"status": "idempotency_key_reused"}
    ),
}
CONFLICT_REFUSALS: dict[IndependenceOutcome, HTTPException] = {
    IndependenceOutcome.NOT_ASSIGNED: HTTPException(
        status_code=403, detail="only an assigned reviewer can declare a conflict"
    ),
    IndependenceOutcome.ALREADY_DECLARED: HTTPException(
        status_code=409, detail={"status": "conflict_already_declared"}
    ),
    IndependenceOutcome.NO_DECLARED_CONFLICT: HTTPException(
        status_code=409, detail={"status": "no_declared_conflict"}
    ),
}
RECUSAL_REFUSALS: dict[IndependenceOutcome, HTTPException] = {
    IndependenceOutcome.NOT_ASSIGNED: HTTPException(
        status_code=403, detail="only an assigned reviewer can withdraw from a review round"
    ),
    IndependenceOutcome.ALREADY_RECUSED: HTTPException(
        status_code=409, detail={"status": "already_recused"}
    ),
    IndependenceOutcome.REPLACEMENT_IS_SELF: HTTPException(
        status_code=422, detail="a reviewer cannot name themselves as the replacement"
    ),
    IndependenceOutcome.REPLACEMENT_NOT_ELIGIBLE: HTTPException(
        status_code=422, detail="the replacement must be an active, assigned user who holds reviews:write"
    ),
    IndependenceOutcome.REPLACEMENT_CONFLICTED: HTTPException(
        status_code=422, detail="the replacement has a declared conflict on this review round"
    ),
    IndependenceOutcome.REPLACEMENT_WITHDRAWN: HTTPException(
        status_code=422, detail="the replacement has withdrawn from this review round"
    ),
}
APPEAL_REFUSALS: dict[IndependenceOutcome, HTTPException] = {
    IndependenceOutcome.NO_ELIGIBLE_REVIEWER: HTTPException(
        status_code=409, detail={"status": "no_eligible_reviewer"}
    ),
}

review_independence_router = APIRouter(tags=["reviews"])


def _clean_text(value: str | None, limit: int) -> str | None:
    """Strip; blank becomes None; longer than the limit is refused."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > limit:
        raise ValueError(f"must be at most {limit} characters")
    return value


class ConflictRequest(BaseModel):
    # declared states a conflict, withdrawn states that the reviewer no longer declares one.
    kind: Literal["declared", "withdrawn"]
    description: str | None = None
    # The entry this one corrects: the reviewer's current entry on the round, or null when there is none.
    supersedes_id: uuid.UUID | None = None

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str | None) -> str | None:
        return _clean_text(value, REASON_LIMIT)

    @model_validator(mode="after")
    def description_when_declared(self) -> "ConflictRequest":
        if self.kind == "declared" and self.description is None:
            raise ValueError("a description is required for a declared conflict")
        return self


class ConflictResponse(BaseModel):
    id: str
    round_id: str
    reviewer_id: str
    kind: str
    # Null for callers who may not read free text (cases:read_all, or the reviewer concerned).
    description: str | None
    supersedes_id: str | None
    created_at: datetime
    # True for the entry that decides: the newest entry of the reviewer on the round.
    current: bool


class RecusalRequest(BaseModel):
    replacement_id: uuid.UUID
    reason: str | None = None
    # The recusal this one corrects: the caller's current recusal on the round, or null when there is none.
    supersedes_id: uuid.UUID | None = None

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str | None) -> str | None:
        return _clean_text(value, REASON_LIMIT)


class RecusalResponse(BaseModel):
    id: str
    round_id: str
    reviewer_id: str
    replacement_id: str
    reason: str | None
    supersedes_id: str | None
    created_at: datetime
    current: bool


class AppealRequest(BaseModel):
    # extra="forbid": a client that still sends independent_reviewer_id gets a 422, not a silently ignored field.
    model_config = ConfigDict(extra="forbid")

    grounds: str

    @field_validator("grounds")
    @classmethod
    def clean_grounds(cls, value: str) -> str:
        cleaned = _clean_text(value, GROUNDS_LIMIT)
        if cleaned is None:
            raise ValueError("the grounds of the appeal are required")
        return cleaned


class AppealResponse(BaseModel):
    id: str
    round_id: str
    filed_by: str
    independent_reviewer_id: str
    # Null for callers who may not read free text (cases:read_all, or the person who filed the appeal).
    grounds: str | None
    created_at: datetime


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def _checked_key(idempotency_key: str | None) -> str:
    # Checked after the permission check, so a caller without the permission learns nothing about keys.
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail={"status": "idempotency_key_required"})
    if not valid_key(idempotency_key):
        raise HTTPException(status_code=400, detail={"status": "idempotency_key_invalid"})
    return idempotency_key


def _conflict_response(entry: ConflictEntry, scope: CaseScope) -> ConflictResponse:
    visible = scope.read_all or entry.reviewer_id == scope.user_id
    return ConflictResponse(
        id=entry.id,
        round_id=entry.round_id,
        reviewer_id=entry.reviewer_id,
        kind=entry.kind,
        description=entry.description if visible else None,
        supersedes_id=entry.supersedes_id,
        created_at=entry.created_at,
        current=entry.current,
    )


def _recusal_response(entry: RecusalEntry, scope: CaseScope) -> RecusalResponse:
    visible = scope.read_all or entry.reviewer_id == scope.user_id
    return RecusalResponse(
        id=entry.id,
        round_id=entry.round_id,
        reviewer_id=entry.reviewer_id,
        replacement_id=entry.replacement_id,
        reason=entry.reason if visible else None,
        supersedes_id=entry.supersedes_id,
        created_at=entry.created_at,
        current=entry.current,
    )


def _appeal_response(entry: AppealEntry, scope: CaseScope) -> AppealResponse:
    visible = scope.read_all or entry.filed_by == scope.user_id
    return AppealResponse(
        id=entry.id,
        round_id=entry.round_id,
        filed_by=entry.filed_by,
        independent_reviewer_id=entry.independent_reviewer_id,
        grounds=entry.grounds if visible else None,
        created_at=entry.created_at,
    )


def _refusal(outcome: IndependenceOutcome, specific: dict[IndependenceOutcome, HTTPException]) -> HTTPException:
    error = specific.get(outcome) or SHARED_REFUSALS.get(outcome)
    assert error is not None, outcome
    return error


def _key_scope(actor: User, route: str, path: str, key: str, body: dict) -> KeyScope:
    return KeyScope(
        principal_id=actor.id,
        method="POST",
        route=route,
        key=key,
        fingerprint=fingerprint("POST", path, "", body),
    )


@review_independence_router.post(CONFLICTS_ROUTE, status_code=201, response_model=ConflictResponse)
def declare_review_conflict(
    review_id: uuid.UUID,
    body: ConflictRequest,
    actor: User = Depends(require_permission("reviews:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ConflictResponse | JSONResponse:
    key = _checked_key(idempotency_key)
    scope = _scope(actor)
    payload = body.model_dump(mode="json")
    outcome, entry, stored = declare_conflict(
        str(review_id),
        body.kind,
        body.description,
        str(body.supersedes_id) if body.supersedes_id is not None else None,
        scope,
        idempotency=_key_scope(actor, CONFLICTS_ROUTE, f"/api/v1/reviews/{review_id}/conflicts", key, payload),
        render=lambda created: _conflict_response(created, scope).model_dump(mode="json"),
    )
    if outcome is IndependenceOutcome.REPLAY:
        # The same request again: the first answer, with its status and body. Nothing was written.
        assert stored is not None
        return JSONResponse(status_code=stored.status_code, content=stored.body)
    if outcome is not IndependenceOutcome.OK:
        raise _refusal(outcome, CONFLICT_REFUSALS)
    assert entry is not None
    return _conflict_response(entry, scope)


@review_independence_router.get(CONFLICTS_ROUTE, response_model=list[ConflictResponse])
def list_review_conflicts(
    review_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> list[ConflictResponse]:
    scope = _scope(actor)
    entries = list_conflicts(str(review_id), scope)
    if entries is None:
        raise ROUND_NOT_FOUND
    return [_conflict_response(entry, scope) for entry in entries]


@review_independence_router.post(RECUSALS_ROUTE, status_code=201, response_model=RecusalResponse)
def record_review_recusal(
    review_id: uuid.UUID,
    body: RecusalRequest,
    actor: User = Depends(require_permission("reviews:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RecusalResponse | JSONResponse:
    key = _checked_key(idempotency_key)
    scope = _scope(actor)
    payload = body.model_dump(mode="json")
    outcome, entry, stored = record_recusal(
        str(review_id),
        str(body.replacement_id),
        body.reason,
        str(body.supersedes_id) if body.supersedes_id is not None else None,
        scope,
        idempotency=_key_scope(actor, RECUSALS_ROUTE, f"/api/v1/reviews/{review_id}/recusals", key, payload),
        render=lambda created: _recusal_response(created, scope).model_dump(mode="json"),
    )
    if outcome is IndependenceOutcome.REPLAY:
        assert stored is not None
        return JSONResponse(status_code=stored.status_code, content=stored.body)
    if outcome is not IndependenceOutcome.OK:
        raise _refusal(outcome, RECUSAL_REFUSALS)
    assert entry is not None
    return _recusal_response(entry, scope)


@review_independence_router.get(RECUSALS_ROUTE, response_model=list[RecusalResponse])
def list_review_recusals(
    review_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> list[RecusalResponse]:
    scope = _scope(actor)
    entries = list_recusals(str(review_id), scope)
    if entries is None:
        raise ROUND_NOT_FOUND
    return [_recusal_response(entry, scope) for entry in entries]


@review_independence_router.post(APPEALS_ROUTE, status_code=201, response_model=AppealResponse)
def file_review_appeal(
    review_id: uuid.UUID,
    body: AppealRequest,
    # The disclosure approver holds cases:read_all to read the case it approves; it may not file appeals.
    actor: User = Depends(
        require_any_permission_except_role(DISCLOSURE_APPROVER_ROLE, *CASE_READ_PERMISSIONS_FOR_RECORDS)
    ),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> AppealResponse | JSONResponse:
    key = _checked_key(idempotency_key)
    scope = _scope(actor)
    payload = body.model_dump(mode="json")
    outcome, entry, stored = file_appeal(
        str(review_id),
        body.grounds,
        scope,
        idempotency=_key_scope(actor, APPEALS_ROUTE, f"/api/v1/reviews/{review_id}/appeals", key, payload),
        render=lambda created: _appeal_response(created, scope).model_dump(mode="json"),
    )
    if outcome is IndependenceOutcome.REPLAY:
        assert stored is not None
        return JSONResponse(status_code=stored.status_code, content=stored.body)
    if outcome is not IndependenceOutcome.OK:
        raise _refusal(outcome, APPEAL_REFUSALS)
    assert entry is not None
    return _appeal_response(entry, scope)


@review_independence_router.get(APPEALS_ROUTE, response_model=list[AppealResponse])
def list_review_appeals(
    review_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> list[AppealResponse]:
    scope = _scope(actor)
    entries = list_appeals(str(review_id), scope)
    if entries is None:
        raise ROUND_NOT_FOUND
    return [_appeal_response(entry, scope) for entry in entries]
