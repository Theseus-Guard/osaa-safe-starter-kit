"""Review round endpoints: open a round, read rounds, and set one layer of a round (see reviews.py).

Mounted into the main app with app.include_router(reviews_router), as the case record endpoints are.

Access rules:
- Opening a round and setting a layer need reviews:write and an active assignment to the case. The case must be open.
  An invisible case answers 404, a visible case without the assignment answers 403.
- Reading needs the same case read permissions as the case itself. A case the caller may not see answers 404.
- A layer reason is free text. It is returned only to callers with cases:read_all. The caller who sets a layer gets
  the reason it just wrote back, as the F1 review note does.
- A round has no overall result. Every response lists the layers and the revision, nothing else.
"""

import uuid
from datetime import datetime
from typing import Literal, get_args

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from safe_kit.auth import permissions_of, require_any_permission, require_permission
from safe_kit.case_records_api import CASE_READ_PERMISSIONS_FOR_RECORDS
from safe_kit.cases import CaseScope
from safe_kit.idempotency import KeyScope, fingerprint, valid_key
from safe_kit.reviews import (
    MAX_EVIDENCE_REFERENCES,
    REASON_LIMIT,
    TEMPLATE_LAYERS,
    Round,
    RoundOutcome,
    get_round,
    list_rounds,
    open_round,
    set_layer,
)
from safe_kit.users import User

LayerName = Literal[
    "model",
    "instructions",
    "safeguards",
    "tools",
    "environment",
    "monitoring",
    "human-operations",
    "supply-chain",
    "serving-path",
    "authorization-and-commitment",
    "workflow-composition",
]
# The path parameter and the template must name the same layers.
assert get_args(LayerName) == TEMPLATE_LAYERS

OPEN_ROUTE = "/api/v1/reviews"
CASE_NOT_FOUND = HTTPException(status_code=404, detail="case not found")
CASE_CLOSED = HTTPException(status_code=409, detail="case is closed")
ROUND_CLOSED = HTTPException(status_code=409, detail="review round is closed")
ROUND_NOT_FOUND = HTTPException(status_code=404, detail="review not found")
NOT_ASSIGNED = HTTPException(
    status_code=403, detail="only an assigned reviewer can record a review"
)

reviews_router = APIRouter(tags=["reviews"])


class OpenRoundRequest(BaseModel):
    case_id: uuid.UUID


class LayerUpdateRequest(BaseModel):
    outcome: Literal["pass", "fail", "insufficient_evidence", "not_applicable"]
    reason: str | None = None
    evidence_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_REFERENCES
    )
    # The revision the caller read. A different current revision is refused with 409 and nothing is stored.
    expected_revision: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > REASON_LIMIT:
            raise ValueError(f"must be at most {REASON_LIMIT} characters")
        return value

    @model_validator(mode="after")
    def reason_unless_pass(self) -> "LayerUpdateRequest":
        if self.outcome != "pass" and self.reason is None:
            raise ValueError("a reason is required for this outcome")
        return self


class LayerResponse(BaseModel):
    layer: str
    outcome: str | None
    # Null for callers without the full view (cases:read_all), except on the layer the caller just set.
    reason: str | None
    evidence_ids: list[str]
    set_at: datetime | None


class ReviewRoundResponse(BaseModel):
    id: str
    case_id: str
    status: str
    revision: int
    opened_at: datetime
    closed_at: datetime | None
    layers: list[LayerResponse]


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def _round_response(
    review: Round, scope: CaseScope, own_layer: str | None = None
) -> ReviewRoundResponse:
    return ReviewRoundResponse(
        id=review.id,
        case_id=review.case_id,
        status=review.status,
        revision=review.revision,
        opened_at=review.opened_at,
        closed_at=review.closed_at,
        layers=[
            LayerResponse(
                layer=entry.layer,
                outcome=entry.outcome,
                reason=entry.reason
                if scope.read_all or entry.layer == own_layer
                else None,
                evidence_ids=list(entry.evidence_ids),
                set_at=entry.set_at,
            )
            for entry in review.layers
        ],
    )


@reviews_router.post(
    "/api/v1/reviews", status_code=201, response_model=ReviewRoundResponse
)
def open_review_round(
    body: OpenRoundRequest,
    actor: User = Depends(require_permission("reviews:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ReviewRoundResponse | JSONResponse:
    # A side-effect POST needs an Idempotency-Key (plans/f0/api-draft.md section 3.4). Checked after the permission
    # check, so a caller without the permission learns nothing about keys.
    if idempotency_key is None:
        raise HTTPException(
            status_code=400, detail={"status": "idempotency_key_required"}
        )
    if not valid_key(idempotency_key):
        raise HTTPException(
            status_code=400, detail={"status": "idempotency_key_invalid"}
        )
    scope = _scope(actor)
    key_scope = KeyScope(
        principal_id=actor.id,
        method="POST",
        route=OPEN_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint("POST", OPEN_ROUTE, "", body.model_dump(mode="json")),
    )
    outcome, review, stored = open_round(
        str(body.case_id),
        scope,
        idempotency=key_scope,
        render=lambda created: _round_response(created, scope).model_dump(mode="json"),
    )
    if outcome is RoundOutcome.REPLAY:
        # The same request again: the first answer, with its status and body. Nothing was written.
        assert stored is not None
        return JSONResponse(status_code=stored.status_code, content=stored.body)
    if outcome is RoundOutcome.KEY_REUSED:
        raise HTTPException(
            status_code=409, detail={"status": "idempotency_key_reused"}
        )
    if outcome is RoundOutcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is RoundOutcome.CLOSED:
        raise CASE_CLOSED
    if outcome is RoundOutcome.NOT_ASSIGNED:
        raise NOT_ASSIGNED
    assert review is not None
    return _round_response(review, scope)


@reviews_router.get("/api/v1/reviews", response_model=list[ReviewRoundResponse])
def list_review_rounds(
    case_id: uuid.UUID = Query(...),
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> list[ReviewRoundResponse]:
    scope = _scope(actor)
    rounds = list_rounds(str(case_id), scope)
    if rounds is None:
        raise CASE_NOT_FOUND
    return [_round_response(review, scope) for review in rounds]


@reviews_router.get("/api/v1/reviews/{review_id}", response_model=ReviewRoundResponse)
def get_review_round(
    review_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> ReviewRoundResponse:
    scope = _scope(actor)
    review = get_round(str(review_id), scope)
    if review is None:
        raise ROUND_NOT_FOUND
    return _round_response(review, scope)


@reviews_router.patch(
    "/api/v1/reviews/{review_id}/layers/{layer}", response_model=ReviewRoundResponse
)
def set_review_layer(
    review_id: uuid.UUID,
    layer: LayerName,
    body: LayerUpdateRequest,
    actor: User = Depends(require_permission("reviews:write")),
) -> ReviewRoundResponse:
    scope = _scope(actor)
    outcome, review = set_layer(
        str(review_id),
        layer,
        body.outcome,
        body.reason,
        [str(evidence_id) for evidence_id in body.evidence_ids],
        body.expected_revision,
        scope,
    )
    if outcome is RoundOutcome.NOT_FOUND:
        raise ROUND_NOT_FOUND
    if outcome is RoundOutcome.CLOSED:
        raise CASE_CLOSED
    if outcome is RoundOutcome.ROUND_CLOSED:
        raise ROUND_CLOSED
    if outcome is RoundOutcome.NOT_ASSIGNED:
        raise NOT_ASSIGNED
    if outcome is RoundOutcome.REVISION_CONFLICT:
        assert review is not None
        # Only the current revision. The caller learns nothing else from a refused write.
        raise HTTPException(
            status_code=409,
            detail={"status": "revision_conflict", "current_revision": review.revision},
        )
    if outcome is RoundOutcome.REASON_REQUIRED:
        raise HTTPException(
            status_code=422, detail="a reason is required for this outcome"
        )
    if outcome is RoundOutcome.UNKNOWN_EVIDENCE:
        raise HTTPException(
            status_code=422, detail="evidence is not available on this case"
        )
    assert review is not None
    return _round_response(review, scope, own_layer=layer)
