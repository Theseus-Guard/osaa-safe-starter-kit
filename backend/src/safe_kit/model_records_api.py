"""Model record endpoints: the model, runtime and reasoning configuration of an agent run (see model_records.py).

Mounted into the main app with app.include_router(model_records_router), as the other case record endpoints are.

Access rules, from CaseScope (cases.py) and the evidence permissions:
- reads need evidence:read, and the case must be visible to the caller;
- writes need evidence:write and evidence:read, as an evidence upload does, and the case must be visible and open.
A missing permission answers 403. A case the caller may not see answers 404, the same as a missing one. A write to a
closed case answers 409.

The configuration object in a response has the same keys as the request body, so a read can be sent back to record
the same configuration again (replay). The revocation reason is free text: it is returned only to callers with
cases:read_all, the same rule as the other free text. Revoked records are returned with revoked = true.
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from safe_kit.auth import permissions_of, require_permission
from safe_kit.cases import CaseScope
from safe_kit.evidence_api import EVIDENCE_READ, EVIDENCE_WRITE
from safe_kit.idempotency import KeyScope, checked_key, key_scope
from safe_kit.model_records import (
    Configuration,
    ModelRecord,
    ModelRecordOutcome,
    Reasoning,
    add_model_record,
    list_model_records,
    read_model_record,
    revoke_model_record,
)
from safe_kit.users import User

# Identifiers only (letters, digits and . _ : / @ + -), so prompt text cannot be recorded in these fields.
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$"
REVOCATION_REASON_LIMIT = 1000
TOKEN_BUDGET_LIMIT = 10_000_000

CASE_NOT_FOUND = HTTPException(status_code=404, detail="case not found")
RECORD_NOT_FOUND = HTTPException(status_code=404, detail="model record not found")
CASE_CLOSED = HTTPException(status_code=409, detail="case is closed")

MODEL_RECORDS_ROUTE = "/api/v1/cases/{case_id}/model-records"
MODEL_RECORD_ROUTE = "/api/v1/cases/{case_id}/model-records/{record_id}"
REVOKE_ROUTE = "/api/v1/cases/{case_id}/model-records/{record_id}/revoke"

model_records_router = APIRouter(tags=["model records"])


class ReasoningRequest(BaseModel):
    mode: Literal["disabled", "enabled", "unknown"]
    effort: Literal["low", "medium", "high", "max"] | None = None
    budget_tokens: int | None = Field(default=None, ge=1, le=TOKEN_BUDGET_LIMIT)


class ModelRecordRequest(BaseModel):
    model_name: str = Field(min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)
    model_version: str = Field(min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)
    runtime: str = Field(min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)
    provider_alias: str = Field(
        min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN
    )
    reasoning: ReasoningRequest
    # Optional links. A record concerns the case as a whole, or one evidence item, or one review round.
    evidence_item_id: uuid.UUID | None = None
    review_round_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def check_links_and_reasoning(self) -> "ModelRecordRequest":
        if self.evidence_item_id is not None and self.review_round_id is not None:
            raise ValueError(
                "a record links to one evidence item or one review round, not both"
            )
        reasoning = self.reasoning
        if reasoning.mode != "enabled" and (
            reasoning.effort is not None or reasoning.budget_tokens is not None
        ):
            raise ValueError(
                "effort and budget_tokens are set only when reasoning is enabled"
            )
        return self


class RevocationRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=REVOCATION_REASON_LIMIT)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class ReasoningResponse(BaseModel):
    mode: str
    effort: str | None
    budget_tokens: int | None


class ConfigurationResponse(BaseModel):
    model_name: str
    model_version: str
    runtime: str
    provider_alias: str
    reasoning: ReasoningResponse


class ModelRecordResponse(BaseModel):
    record_id: str
    case_id: str
    # The version shown is the latest one. A revocation is a version of its own.
    version: int
    configuration: ConfigurationResponse
    evidence_item_id: str | None
    review_round_id: str | None
    recorded_at: datetime
    # Who wrote this version, and when.
    written_by: str
    written_at: datetime
    revoked: bool
    revoked_at: datetime | None
    # Null for callers without the full view, and for a record that is not revoked.
    revocation_reason: str | None
    # False once revoked: the record stays for audit but is not the current configuration.
    usable_as_current: bool


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def _configuration(body: ModelRecordRequest) -> Configuration:
    reasoning = body.reasoning
    return Configuration(
        model_name=body.model_name,
        model_version=body.model_version,
        runtime=body.runtime,
        provider_alias=body.provider_alias,
        reasoning=Reasoning(
            mode=reasoning.mode,
            effort=reasoning.effort,
            budget_tokens=reasoning.budget_tokens,
        ),
    )


def _response(record: ModelRecord, scope: CaseScope) -> ModelRecordResponse:
    config = record.configuration
    return ModelRecordResponse(
        record_id=record.record_id,
        case_id=record.case_id,
        version=record.version,
        configuration=ConfigurationResponse(
            model_name=config.model_name,
            model_version=config.model_version,
            runtime=config.runtime,
            provider_alias=config.provider_alias,
            reasoning=ReasoningResponse(
                mode=config.reasoning.mode,
                effort=config.reasoning.effort,
                budget_tokens=config.reasoning.budget_tokens,
            ),
        ),
        evidence_item_id=record.evidence_item_id,
        review_round_id=record.review_round_id,
        recorded_at=record.recorded_at,
        written_by=record.written_by,
        written_at=record.written_at,
        revoked=record.revoked,
        revoked_at=record.revoked_at,
        revocation_reason=record.revocation_reason if scope.read_all else None,
        usable_as_current=not record.revoked,
    )


def _raise_for(outcome: ModelRecordOutcome) -> None:
    if outcome is ModelRecordOutcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is ModelRecordOutcome.RECORD_NOT_FOUND:
        raise RECORD_NOT_FOUND
    if outcome is ModelRecordOutcome.CLOSED:
        raise CASE_CLOSED
    if outcome is ModelRecordOutcome.REFERENCE_NOT_FOUND:
        raise HTTPException(
            status_code=404, detail="evidence item or review round not found"
        )
    if outcome is ModelRecordOutcome.ALREADY_REVOKED:
        raise HTTPException(status_code=409, detail="model record is already revoked")


@model_records_router.get(MODEL_RECORDS_ROUTE, response_model=list[ModelRecordResponse])
def list_case_model_records(
    case_id: uuid.UUID,
    current_only: bool = False,
    actor: User = Depends(require_permission(EVIDENCE_READ)),
) -> list[ModelRecordResponse]:
    scope = _scope(actor)
    records = list_model_records(str(case_id), scope, current_only=current_only)
    if records is None:
        raise CASE_NOT_FOUND
    return [_response(record, scope) for record in records]


@model_records_router.get(MODEL_RECORD_ROUTE, response_model=ModelRecordResponse)
def read_case_model_record(
    case_id: uuid.UUID,
    record_id: uuid.UUID,
    actor: User = Depends(require_permission(EVIDENCE_READ)),
) -> ModelRecordResponse:
    scope = _scope(actor)
    record = read_model_record(str(case_id), str(record_id), scope)
    if record is None:
        raise RECORD_NOT_FOUND
    return _response(record, scope)


@model_records_router.post(
    MODEL_RECORDS_ROUTE, status_code=201, response_model=ModelRecordResponse
)
def add_case_model_record(
    case_id: uuid.UUID,
    body: ModelRecordRequest,
    actor: User = Depends(require_permission(EVIDENCE_WRITE)),
    _reader: User = Depends(require_permission(EVIDENCE_READ)),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ModelRecordResponse:
    scope = _scope(actor)
    path = f"/api/v1/cases/{case_id}/model-records"
    scope_key: KeyScope = key_scope(
        actor.id,
        "POST",
        MODEL_RECORDS_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    outcome, record = add_model_record(
        str(case_id),
        _configuration(body),
        scope,
        evidence_item_id=str(body.evidence_item_id) if body.evidence_item_id else None,
        review_round_id=str(body.review_round_id) if body.review_round_id else None,
        idempotency=scope_key,
        render=lambda created: _response(created, scope).model_dump(mode="json"),
    )
    _raise_for(outcome)
    assert record is not None
    return _response(record, scope)


@model_records_router.post(REVOKE_ROUTE, response_model=ModelRecordResponse)
def revoke_case_model_record(
    case_id: uuid.UUID,
    record_id: uuid.UUID,
    body: RevocationRequest,
    actor: User = Depends(require_permission(EVIDENCE_WRITE)),
    _reader: User = Depends(require_permission(EVIDENCE_READ)),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ModelRecordResponse:
    scope = _scope(actor)
    path = f"/api/v1/cases/{case_id}/model-records/{record_id}/revoke"
    scope_key: KeyScope = key_scope(
        actor.id,
        "POST",
        REVOKE_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    outcome, record = revoke_model_record(
        str(case_id),
        str(record_id),
        body.reason,
        scope,
        idempotency=scope_key,
        render=lambda revoked: _response(revoked, scope).model_dump(mode="json"),
    )
    _raise_for(outcome)
    assert record is not None
    return _response(record, scope)
