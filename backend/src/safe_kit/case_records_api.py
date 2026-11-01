"""Case record endpoints: awareness events, impacts, legal duties, classification, and the preliminary report.

Mounted into the main app with app.include_router(case_records_router), as the evidence endpoints are.

Access rules, from CaseScope (cases.py):
- reads (awareness events, impacts, legal duties, classification) need one of the case read permissions, and the case
  must be visible to the caller;
- writes need cases:triage, and the case must be visible and open (a closed case answers 409);
- the preliminary report needs cases:read_all, because it gathers the free text of every record.
A missing permission answers 403. A case the caller may not see answers 404, the same as a missing one.

Free text (basis, scope, rationale, and the other notes) is returned only to callers with cases:read_all, the same rule
as the close and reopen reasons and the review notes. Other callers see the structured fields.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from safe_kit.auth import permissions_of, require_any_permission, require_permission
from safe_kit.case_records import (
    CLASSIFICATION_TEXT_FIELDS,
    AwarenessEvent,
    Classification,
    ImpactAssessment,
    LegalDuty,
    RecordOutcome,
    add_legal_duty,
    list_awareness_events,
    list_impacts,
    list_legal_duties,
    normalize_triggers,
    preliminary_report,
    read_classification,
    record_awareness_event,
    set_impact,
    update_classification,
    update_legal_duty,
)
from safe_kit.cases import CaseScope
from safe_kit.users import User

TEXT_LIMIT = 1000
SOURCE_LIMIT = 500
JURISDICTION_LIMIT = 200
# Any role that may read a case. Visibility is then decided by the case itself (cases.is_visible_on).
CASE_READ_PERMISSIONS_FOR_RECORDS = ("cases:read_own", "cases:read_all", "cases:read_assigned")

CASE_NOT_FOUND = HTTPException(status_code=404, detail="case not found")
CASE_CLOSED = HTTPException(status_code=409, detail="case is closed")

case_records_router = APIRouter(tags=["case record"])


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def _text(value: str | None, scope: CaseScope) -> str | None:
    """Free text is shown only to callers with the full view (cases:read_all)."""
    return value if scope.read_all else None


def _optional_text(value: str | None, limit: int = TEXT_LIMIT) -> str | None:
    """Strip the text. Blank becomes None. Text over the limit is refused."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > limit:
        raise ValueError(f"must be at most {limit} characters")
    return value


def _required_text(value: str, limit: int = TEXT_LIMIT) -> str:
    cleaned = _optional_text(value, limit)
    if cleaned is None:
        raise ValueError("must not be blank")
    return cleaned


def _not_in_future(value: datetime) -> datetime:
    if value > datetime.now(UTC):
        raise ValueError("cannot be in the future")
    return value


# --- awareness events ----------------------------------------------------------------------------------------------


class AwarenessEventRequest(BaseModel):
    # The fact that started the organization's awareness. Each kind keeps its own time and basis.
    awareness_kind: Literal["operator", "affected_party", "external_report"]
    # An incident, a near miss, or a blocked attempt. They are not interchangeable (T01).
    event_kind: Literal["incident", "near_miss", "blocked_attempt"]
    occurred_at: AwareDatetime
    basis: str = Field(min_length=1, max_length=TEXT_LIMIT)

    @field_validator("basis")
    @classmethod
    def basis_not_blank(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("occurred_at")
    @classmethod
    def occurred_not_in_future(cls, value: datetime) -> datetime:
        return _not_in_future(value)


class AwarenessEventResponse(BaseModel):
    id: str
    case_id: str
    awareness_kind: str
    event_kind: str
    occurred_at: datetime
    # Null for callers without the full view.
    basis: str | None
    recorded_by: str
    created_at: datetime


def _awareness_response(event: AwarenessEvent, scope: CaseScope) -> AwarenessEventResponse:
    return AwarenessEventResponse(
        id=event.id,
        case_id=event.case_id,
        awareness_kind=event.awareness_kind,
        event_kind=event.event_kind,
        occurred_at=event.occurred_at,
        basis=_text(event.basis, scope),
        recorded_by=event.recorded_by,
        created_at=event.created_at,
    )


@case_records_router.get("/api/v1/cases/{case_id}/awareness-events", response_model=list[AwarenessEventResponse])
def list_case_awareness_events(
    case_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> list[AwarenessEventResponse]:
    scope = _scope(actor)
    events = list_awareness_events(str(case_id), scope)
    if events is None:
        raise CASE_NOT_FOUND
    return [_awareness_response(event, scope) for event in events]


@case_records_router.post(
    "/api/v1/cases/{case_id}/awareness-events", status_code=201, response_model=AwarenessEventResponse
)
def add_case_awareness_event(
    case_id: uuid.UUID,
    body: AwarenessEventRequest,
    actor: User = Depends(require_permission("cases:triage")),
) -> AwarenessEventResponse:
    scope = _scope(actor)
    outcome, event = record_awareness_event(
        str(case_id), body.awareness_kind, body.event_kind, body.occurred_at, body.basis, scope
    )
    if outcome is RecordOutcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is RecordOutcome.CLOSED:
        raise CASE_CLOSED
    assert event is not None
    return _awareness_response(event, scope)


# --- impacts -------------------------------------------------------------------------------------------------------


class ImpactRequest(BaseModel):
    severity: Literal["none", "low", "medium", "high", "critical", "unknown"]
    scope: str = Field(min_length=1, max_length=TEXT_LIMIT)

    @field_validator("scope")
    @classmethod
    def scope_not_blank(cls, value: str) -> str:
        return _required_text(value)


class ImpactResponse(BaseModel):
    area: str
    # False when the area has no assessment yet. That is not the same as severity "none".
    assessed: bool
    severity: str | None
    # Null for callers without the full view, and for an area that is not assessed.
    scope: str | None
    updated_at: datetime | None


def _impact_response(impact: ImpactAssessment, scope: CaseScope) -> ImpactResponse:
    return ImpactResponse(
        area=impact.area,
        assessed=impact.assessed,
        severity=impact.severity,
        scope=_text(impact.scope, scope),
        updated_at=impact.updated_at,
    )


@case_records_router.get("/api/v1/cases/{case_id}/impacts", response_model=list[ImpactResponse])
def list_case_impacts(
    case_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> list[ImpactResponse]:
    """Every impact area, always in full: an area with no assessment is listed as not assessed."""
    scope = _scope(actor)
    impacts = list_impacts(str(case_id), scope)
    if impacts is None:
        raise CASE_NOT_FOUND
    return [_impact_response(impact, scope) for impact in impacts]


@case_records_router.put("/api/v1/cases/{case_id}/impacts/{area}", response_model=ImpactResponse)
def set_case_impact(
    case_id: uuid.UUID,
    area: Literal[
        "affected_people_groups", "severity", "scale", "duration", "reversibility", "disproportionate_effect"
    ],
    body: ImpactRequest,
    actor: User = Depends(require_permission("cases:triage")),
) -> ImpactResponse:
    scope = _scope(actor)
    outcome, impact = set_impact(str(case_id), area, body.severity, body.scope, scope)
    if outcome is RecordOutcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is RecordOutcome.CLOSED:
        raise CASE_CLOSED
    assert impact is not None
    return _impact_response(impact, scope)


# --- legal duties --------------------------------------------------------------------------------------------------


class LegalDutyRequest(BaseModel):
    kind: Literal["statutory", "contractual"]
    description: str = Field(min_length=1, max_length=TEXT_LIMIT)
    # The statute or the contract clause that creates the duty.
    source_reference: str = Field(min_length=1, max_length=SOURCE_LIMIT)
    jurisdiction: str | None = Field(default=None, max_length=JURISDICTION_LIMIT)
    due_at: AwareDatetime

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("source_reference")
    @classmethod
    def source_not_blank(cls, value: str) -> str:
        return _required_text(value, SOURCE_LIMIT)

    @field_validator("jurisdiction")
    @classmethod
    def jurisdiction_clean(cls, value: str | None) -> str | None:
        return _optional_text(value, JURISDICTION_LIMIT)


class LegalDutyUpdateRequest(BaseModel):
    # Only the fields that are sent change. A field sent as null is refused, except jurisdiction, which may be cleared.
    description: str | None = Field(default=None, max_length=TEXT_LIMIT)
    source_reference: str | None = Field(default=None, max_length=SOURCE_LIMIT)
    jurisdiction: str | None = Field(default=None, max_length=JURISDICTION_LIMIT)
    due_at: AwareDatetime | None = None
    status: Literal["open", "done"] | None = None

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("must not be null")
        return _required_text(value)

    @field_validator("source_reference")
    @classmethod
    def source_not_blank(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("must not be null")
        return _required_text(value, SOURCE_LIMIT)

    @field_validator("jurisdiction")
    @classmethod
    def jurisdiction_clean(cls, value: str | None) -> str | None:
        return _optional_text(value, JURISDICTION_LIMIT)

    @field_validator("due_at", "status")
    @classmethod
    def not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("must not be null")
        return value

    @model_validator(mode="after")
    def at_least_one_field(self) -> "LegalDutyUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        return self


class LegalDutyResponse(BaseModel):
    id: str
    case_id: str
    kind: str
    description: str | None
    source_reference: str | None
    jurisdiction: str | None
    due_at: datetime
    status: str
    created_by: str
    created_at: datetime


def _duty_response(duty: LegalDuty, scope: CaseScope) -> LegalDutyResponse:
    # The legal due time is always returned. It is kept apart from the SAFE deadlines of the case.
    return LegalDutyResponse(
        id=duty.id,
        case_id=duty.case_id,
        kind=duty.kind,
        description=_text(duty.description, scope),
        source_reference=_text(duty.source_reference, scope),
        jurisdiction=_text(duty.jurisdiction, scope),
        due_at=duty.due_at,
        status=duty.status,
        created_by=duty.created_by,
        created_at=duty.created_at,
    )


@case_records_router.get("/api/v1/cases/{case_id}/legal-duties", response_model=list[LegalDutyResponse])
def list_case_legal_duties(
    case_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> list[LegalDutyResponse]:
    scope = _scope(actor)
    duties = list_legal_duties(str(case_id), scope)
    if duties is None:
        raise CASE_NOT_FOUND
    return [_duty_response(duty, scope) for duty in duties]


@case_records_router.post("/api/v1/cases/{case_id}/legal-duties", status_code=201, response_model=LegalDutyResponse)
def add_case_legal_duty(
    case_id: uuid.UUID,
    body: LegalDutyRequest,
    actor: User = Depends(require_permission("cases:triage")),
) -> LegalDutyResponse:
    scope = _scope(actor)
    outcome, duty = add_legal_duty(
        str(case_id),
        body.kind,
        body.description,
        body.source_reference,
        body.jurisdiction,
        body.due_at,
        scope,
    )
    if outcome is RecordOutcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is RecordOutcome.CLOSED:
        raise CASE_CLOSED
    assert duty is not None
    return _duty_response(duty, scope)


@case_records_router.patch("/api/v1/cases/{case_id}/legal-duties/{duty_id}", response_model=LegalDutyResponse)
def update_case_legal_duty(
    case_id: uuid.UUID,
    duty_id: uuid.UUID,
    body: LegalDutyUpdateRequest,
    actor: User = Depends(require_permission("cases:triage")),
) -> LegalDutyResponse:
    scope = _scope(actor)
    changes = {name: getattr(body, name) for name in body.model_fields_set}
    outcome, duty = update_legal_duty(str(case_id), str(duty_id), changes, scope)
    if outcome is RecordOutcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is RecordOutcome.CLOSED:
        raise CASE_CLOSED
    if outcome is RecordOutcome.DUTY_NOT_FOUND:
        raise HTTPException(status_code=404, detail="legal duty not found")
    assert duty is not None
    return _duty_response(duty, scope)


# --- classification ------------------------------------------------------------------------------------------------


class ClassificationRequest(BaseModel):
    # The version the editor read (0 when nothing is recorded yet). A different current version is refused with 409.
    expected_version: int = Field(ge=0)
    # The five compact trigger values of R01. Duplicates are removed.
    triggers: list[Literal[
        "unauthorized_access_or_change",
        "boundary_escape",
        "confidential_data_exposure",
        "out_of_scope_continuation",
        "harm_inaction_near_miss",
    ]] = Field(default_factory=list, max_length=5)
    event_kind: Literal["incident", "near_miss", "blocked_attempt"] | None = None
    internal_incident_option: bool = False
    classification_rationale: str | None = Field(default=None, max_length=TEXT_LIMIT)
    responsible_operator: str | None = Field(default=None, max_length=TEXT_LIMIT)
    affected_versions: str | None = Field(default=None, max_length=TEXT_LIMIT)
    approved_scope: str | None = Field(default=None, max_length=TEXT_LIMIT)
    scope_permission_source: str | None = Field(default=None, max_length=TEXT_LIMIT)
    containment: str | None = Field(default=None, max_length=TEXT_LIMIT)
    evidence_gap_plan: str | None = Field(default=None, max_length=TEXT_LIMIT)
    next_update_due_at: AwareDatetime | None = None

    @field_validator(*CLASSIFICATION_TEXT_FIELDS)
    @classmethod
    def text_clean(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("triggers")
    @classmethod
    def triggers_distinct(cls, value: list[str]) -> list[str]:
        return list(normalize_triggers(value))

    @model_validator(mode="after")
    def internal_option_needs_rationale(self) -> "ClassificationRequest":
        if self.internal_incident_option and self.classification_rationale is None:
            raise ValueError("classification_rationale is required when internal_incident_option is true")
        return self


class ClassificationResponse(BaseModel):
    case_id: str
    version: int
    triggers: list[str]
    event_kind: str | None
    internal_incident_option: bool
    classification_rationale: str | None
    responsible_operator: str | None
    affected_versions: str | None
    approved_scope: str | None
    scope_permission_source: str | None
    containment: str | None
    evidence_gap_plan: str | None
    next_update_due_at: datetime | None
    created_by: str | None
    created_at: datetime | None


def _classification_response(case_id: str, classification: Classification, scope: CaseScope) -> ClassificationResponse:
    return ClassificationResponse(
        case_id=case_id,
        version=classification.version,
        triggers=list(classification.triggers),
        event_kind=classification.event_kind,
        internal_incident_option=classification.internal_incident_option,
        classification_rationale=_text(classification.classification_rationale, scope),
        responsible_operator=_text(classification.responsible_operator, scope),
        affected_versions=_text(classification.affected_versions, scope),
        approved_scope=_text(classification.approved_scope, scope),
        scope_permission_source=_text(classification.scope_permission_source, scope),
        containment=_text(classification.containment, scope),
        evidence_gap_plan=_text(classification.evidence_gap_plan, scope),
        next_update_due_at=classification.next_update_due_at,
        created_by=classification.created_by,
        created_at=classification.created_at,
    )


@case_records_router.get("/api/v1/cases/{case_id}/classification", response_model=ClassificationResponse)
def get_case_classification(
    case_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> ClassificationResponse:
    scope = _scope(actor)
    classification = read_classification(str(case_id), scope)
    if classification is None:
        raise CASE_NOT_FOUND
    return _classification_response(str(case_id), classification, scope)


@case_records_router.put("/api/v1/cases/{case_id}/classification", response_model=ClassificationResponse)
def update_case_classification(
    case_id: uuid.UUID,
    body: ClassificationRequest,
    actor: User = Depends(require_permission("cases:triage")),
) -> ClassificationResponse:
    """Store the classification as the next version. The body holds every field; a field not sent is cleared."""
    scope = _scope(actor)
    # The free-text fields are hidden from callers without cases:read_all, so their save would clear them. Refuse it.
    if not scope.read_all:
        raise HTTPException(status_code=403, detail="classification_requires_full_view")
    values: dict[str, Any] = {
        "triggers": tuple(body.triggers),
        "event_kind": body.event_kind,
        "internal_incident_option": body.internal_incident_option,
        "classification_rationale": body.classification_rationale,
        "responsible_operator": body.responsible_operator,
        "affected_versions": body.affected_versions,
        "approved_scope": body.approved_scope,
        "scope_permission_source": body.scope_permission_source,
        "containment": body.containment,
        "evidence_gap_plan": body.evidence_gap_plan,
        "next_update_due_at": body.next_update_due_at,
    }
    outcome, classification = update_classification(str(case_id), body.expected_version, values, scope)
    if outcome is RecordOutcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is RecordOutcome.CLOSED:
        raise CASE_CLOSED
    if outcome is RecordOutcome.VERSION_CONFLICT:
        assert classification is not None
        # A structured detail: clients read the stable status and the version without parsing text.
        raise HTTPException(
            status_code=409,
            detail={"status": "classification_version_conflict", "current_version": classification.version},
        )
    assert classification is not None
    return _classification_response(str(case_id), classification, scope)


# --- preliminary report --------------------------------------------------------------------------------------------


@case_records_router.get("/api/v1/cases/{case_id}/preliminary-report")
def get_case_preliminary_report(
    case_id: uuid.UUID,
    actor: User = Depends(require_permission("cases:read_all")),
) -> dict[str, Any]:
    """The preliminary report. Missing fields are listed as missing; the report is never refused for them."""
    report = preliminary_report(str(case_id), _scope(actor))
    if report is None:
        raise CASE_NOT_FOUND
    return report
