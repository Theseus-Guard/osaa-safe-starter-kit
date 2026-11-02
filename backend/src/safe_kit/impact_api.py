"""Risk impact endpoints: the C, B and A values of a case, and its risk score (safe_kit.impact).

Mounted into the main app with app.include_router(impact_router), as the case record endpoints are. risk_fields gives the
same four fields to the case list and the case detail responses.

Access rules, as for the classification (case_records_api.py):
- reads need one of the case read permissions, and the case must be visible to the caller;
- the write needs cases:triage, and the case must be visible and open (a closed case answers 409);
- a missing permission answers 403. A case the caller may not see answers 404, the same as a missing one.

The write is a POST that stores the three values as the next version. It needs an Idempotency-Key (400 without one),
as the other side-effect POSTs do. A repeated request with the same key and body returns the first answer and writes
nothing. The idempotency table accepts only POST (migration 0032), so the write is a POST, not a PUT.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from safe_kit.auth import permissions_of, require_any_permission, require_permission
from safe_kit.case_records_api import CASE_READ_PERMISSIONS_FOR_RECORDS
from safe_kit.cases import CaseScope
from safe_kit.idempotency import KeyScope, fingerprint, valid_key
from safe_kit.impact import ImpactOutcome, ImpactScores, read_impact_scores, update_impact_scores
from safe_kit.users import User

IMPACT_SCORES_ROUTE = "/api/v1/cases/{case_id}/impact-scores"
# A whole number from 1 to 5. A float such as 3.0, a string, or a boolean is refused.
ImpactValue = Annotated[StrictInt, Field(ge=1, le=5)] | None

CASE_NOT_FOUND = HTTPException(status_code=404, detail="case not found")
CASE_CLOSED = HTTPException(status_code=409, detail="case is closed")

impact_router = APIRouter(tags=["case risk"])


class ImpactScoresRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The version the editor read (0 when nothing is recorded yet). A different current version is refused with 409.
    expected_version: int = Field(ge=0)
    # Confidentiality, Integrity, Availability. A value sent as null, or left out, is not set.
    c: ImpactValue = None
    b: ImpactValue = None
    a: ImpactValue = None


class ImpactScoresResponse(BaseModel):
    case_id: str
    version: int
    c: int | None
    b: int | None
    a: int | None
    # The mean of the three values to one decimal place. Null until all three are set.
    risk_score: float | None
    created_by: str | None
    created_at: datetime | None


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def impact_response(case_id: str, scores: ImpactScores) -> ImpactScoresResponse:
    return ImpactScoresResponse(
        case_id=case_id,
        version=scores.version,
        c=scores.confidentiality,
        b=scores.integrity,
        a=scores.availability,
        risk_score=scores.risk_score,
        created_by=scores.created_by,
        created_at=scores.created_at,
    )


def risk_fields(scores: ImpactScores | None) -> dict[str, Any]:
    """The four risk fields of the case responses. A case with no version has every value not set."""
    if scores is None:
        return {"risk_score": None, "impact_c": None, "impact_b": None, "impact_a": None}
    return {
        "risk_score": scores.risk_score,
        "impact_c": scores.confidentiality,
        "impact_b": scores.integrity,
        "impact_a": scores.availability,
    }


def _key_scope(actor: User, path: str, idempotency_key: str | None, body: BaseModel) -> KeyScope:
    """The idempotency scope of one request. Checked after the permission check, so a caller without it learns nothing."""
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail={"status": "idempotency_key_required"})
    if not valid_key(idempotency_key):
        raise HTTPException(status_code=400, detail={"status": "idempotency_key_invalid"})
    return KeyScope(
        principal_id=actor.id,
        method="POST",
        route=IMPACT_SCORES_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint("POST", path, "", body.model_dump(mode="json")),
    )


@impact_router.get(IMPACT_SCORES_ROUTE, response_model=ImpactScoresResponse)
def get_case_impact_scores(
    case_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> ImpactScoresResponse:
    scores = read_impact_scores(str(case_id), _scope(actor))
    if scores is None:
        raise CASE_NOT_FOUND
    return impact_response(str(case_id), scores)


@impact_router.post(IMPACT_SCORES_ROUTE, response_model=ImpactScoresResponse)
def update_case_impact_scores(
    case_id: uuid.UUID,
    body: ImpactScoresRequest,
    actor: User = Depends(require_permission("cases:triage")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = IMPACT_SCORES_ROUTE.format(case_id=case_id)
    key_scope = _key_scope(actor, path, idempotency_key, body)

    def render(scores: ImpactScores) -> dict[str, Any]:
        return impact_response(str(case_id), scores).model_dump(mode="json")

    result = update_impact_scores(
        str(case_id),
        body.expected_version,
        {"confidentiality": body.c, "integrity": body.b, "availability": body.a},
        _scope(actor),
        idempotency=key_scope,
        render=render,
    )
    if result.outcome is ImpactOutcome.REPLAY:
        assert result.stored is not None
        # The same request again: the first answer, with its status and body. Nothing was written.
        return JSONResponse(status_code=result.stored.status_code, content=result.stored.body)
    if result.outcome is ImpactOutcome.KEY_REUSED:
        raise HTTPException(status_code=409, detail={"status": "idempotency_key_reused"})
    if result.outcome is ImpactOutcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if result.outcome is ImpactOutcome.CLOSED:
        raise CASE_CLOSED
    if result.outcome is ImpactOutcome.VERSION_CONFLICT:
        assert result.value is not None
        # Only the current version is returned. The stable status lets clients react without parsing text.
        raise HTTPException(
            status_code=409,
            detail={"status": "impact_version_conflict", "current_version": result.value.version},
        )
    assert result.value is not None
    return impact_response(str(case_id), result.value)
