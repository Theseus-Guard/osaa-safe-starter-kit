"""Catalog endpoints: recommendations, verification runs, and the versioned catalog (plans/f0/api-draft.md section 2.6).

Mounted into the main app with app.include_router(catalog_router), as the review endpoints are.

Access rules:
- Every route needs a login (401 without one) and the permission of its verb: catalog:read for reads, catalog:write for
  every record (403 without it).
- A restricted reference and its digest are returned only with catalog:read_restricted. A caller without it does not see
  the key at all, so the response does not reveal that a reference exists.
- The write responses never carry a restricted reference, so a stored idempotent response holds no restricted content.
- Recommendations and verification runs are separate records. A recommendation has no result field, so a body that
  tries to state one is refused (extra fields are rejected). An entry is "verified" only through a passing run.
- Side-effect POSTs need an Idempotency-Key (plans/f0/api-draft.md section 3.4). A repeated request with the same key and
  body returns the first answer and writes nothing.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from safe_kit.auth import permissions_of, require_permission
from safe_kit.catalog import (
    BODY_LIMIT,
    FIELD_LIMIT,
    REASON_LIMIT,
    REFERENCE_LIMIT,
    TITLE_LIMIT,
    CatalogOutcome,
    Entry,
    Recommendation,
    Result,
    VerificationRun,
    Version,
    add_recommendation,
    change_entry,
    create_entry,
    get_entry,
    list_entries,
    list_recommendations,
    public_export,
    record_verification_run,
)
from safe_kit.idempotency import KeyScope, fingerprint, valid_key
from safe_kit.users import User

RECOMMENDATIONS_ROUTE = "/api/v1/catalog/recommendations"
RUNS_ROUTE = "/api/v1/catalog/recommendations/{recommendation_id}/verification-runs"
EXPORT_ROUTE = "/api/v1/catalog/export"
ENTRIES_ROUTE = "/api/v1/catalog/entries"
ENTRY_ROUTE = "/api/v1/catalog/entries/{entry_id}"
VERSIONS_ROUTE = "/api/v1/catalog/entries/{entry_id}/versions"
CORRECTIONS_ROUTE = "/api/v1/catalog/entries/{entry_id}/corrections"
DISPUTES_ROUTE = "/api/v1/catalog/entries/{entry_id}/disputes"
WITHDRAWALS_ROUTE = "/api/v1/catalog/entries/{entry_id}/withdrawals"
SUPERSESSIONS_ROUTE = "/api/v1/catalog/entries/{entry_id}/supersessions"
DEFAULT_PAGE = 50
MAX_PAGE = 200
RESTRICTED_PERMISSION = "catalog:read_restricted"

catalog_router = APIRouter(tags=["catalog"])

Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_LIMIT)
]
Body = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=BODY_LIMIT)
]
Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=REASON_LIMIT)
]
FieldText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=FIELD_LIMIT)
]
ReferenceText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=REFERENCE_LIMIT),
]


class _Strict(BaseModel):
    # An unknown field is refused. In particular a recommendation cannot carry a "verified" flag or a result.
    model_config = ConfigDict(extra="forbid")


class RecommendationRequest(_Strict):
    failure_or_system: FieldText
    required_defence_outcome: FieldText
    minimum_control: FieldText
    control_alternative: FieldText | None = None
    responsible: FieldText
    due_at: datetime
    evidence_to_keep: FieldText

    @field_validator("due_at")
    @classmethod
    def due_at_has_a_zone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("must include a time zone")
        return value


class VerificationRunRequest(_Strict):
    subject: FieldText
    scope: FieldText
    assumptions: FieldText
    evidence_path: FieldText
    failure_behavior: FieldText
    error_class: FieldText
    uncertainty: FieldText
    method: FieldText
    result: Literal["pass", "fail", "inconclusive"]


class EntryCreateRequest(_Strict):
    title: Title
    body: Body
    verification_run_id: uuid.UUID | None = None
    restricted_reference: ReferenceText | None = None


class VersionRequest(_Strict):
    # The version the editor read. A different current version is refused with 409 and nothing is stored.
    expected_version: int = Field(ge=1)
    title: Title
    body: Body
    verification_run_id: uuid.UUID | None = None
    restricted_reference: ReferenceText | None = None


class CorrectionRequest(VersionRequest):
    reason: Reason


class DisputeRequest(_Strict):
    expected_version: int = Field(ge=1)
    reason: Reason


class WithdrawalRequest(_Strict):
    expected_version: int = Field(ge=1)
    reason: Reason


class SupersessionRequest(_Strict):
    expected_version: int = Field(ge=1)
    replacement_entry_id: uuid.UUID
    reason: Reason | None = None


# --- helpers ----------------------------------------------------------------------------------------------------------


def _key_scope(
    actor: User, route: str, path: str, idempotency_key: str | None, body: BaseModel
) -> KeyScope:
    """The idempotency scope of one request. Checked after the permission check, so a caller without the permission
    learns nothing about keys (plans/f0/api-draft.md section 3.4)."""
    if idempotency_key is None:
        raise HTTPException(
            status_code=400, detail={"status": "idempotency_key_required"}
        )
    if not valid_key(idempotency_key):
        raise HTTPException(
            status_code=400, detail={"status": "idempotency_key_invalid"}
        )
    return KeyScope(
        principal_id=actor.id,
        method="POST",
        route=route,
        key=idempotency_key,
        fingerprint=fingerprint("POST", path, "", body.model_dump(mode="json")),
    )


def _values(body: BaseModel) -> dict[str, Any]:
    """The request as plain values for the domain layer: identifiers as strings, the reference under its domain name."""
    excluded = (
        {"expected_version"} if "expected_version" in type(body).model_fields else set()
    )
    values = body.model_dump(exclude=excluded)
    for key in ("verification_run_id", "replacement_entry_id"):
        if values.get(key) is not None:
            values[key] = str(values[key])
    if "restricted_reference" in values:
        values["reference"] = values.pop("restricted_reference")
    return values


def _may_read_restricted(actor: User) -> bool:
    return RESTRICTED_PERMISSION in permissions_of(actor)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _recommendation_view(recommendation: Recommendation) -> dict[str, Any]:
    return {
        "id": recommendation.id,
        "failure_or_system": recommendation.failure_or_system,
        "required_defence_outcome": recommendation.required_defence_outcome,
        "minimum_control": recommendation.minimum_control,
        "control_alternative": recommendation.control_alternative,
        "responsible": recommendation.responsible,
        "due_at": _iso(recommendation.due_at),
        "evidence_to_keep": recommendation.evidence_to_keep,
        "created_by": recommendation.created_by,
        "created_at": _iso(recommendation.created_at),
    }


def _run_view(run: VerificationRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "recommendation_id": run.recommendation_id,
        "subject": run.subject,
        "scope": run.scope,
        "assumptions": run.assumptions,
        "evidence_path": run.evidence_path,
        "failure_behavior": run.failure_behavior,
        "error_class": run.error_class,
        "uncertainty": run.uncertainty,
        "method": run.method,
        "result": run.result,
        "verified_by": run.verified_by,
        "verified_at": _iso(run.verified_at),
    }


def _version_view(version: Version, with_restricted: bool) -> dict[str, Any]:
    verification = None
    if version.verification is not None:
        verification = {
            "run_id": version.verification.run_id,
            "subject": version.verification.subject,
            "method": version.verification.method,
            "result": version.verification.result,
            "verified_at": _iso(version.verification.verified_at),
        }
    view: dict[str, Any] = {
        "version": version.version,
        "operation": version.operation,
        "status": version.status,
        "title": version.title,
        "body": version.body,
        "reason": version.reason,
        "replacement_entry_id": version.replacement_entry_id,
        "created_by": version.created_by,
        "created_at": _iso(version.created_at),
        "verification": verification,
        # True only for a passing verification run. A recommendation alone never makes an entry verified.
        "verified": version.verification is not None
        and version.verification.result == "pass",
    }
    if with_restricted:
        view["restricted"] = (
            {
                "reference": version.restricted.reference,
                "digest": version.restricted.digest,
            }
            if version.restricted is not None
            else None
        )
    return view


def _entry_view(
    entry: Entry, *, with_restricted: bool, history: bool
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": entry.id,
        "created_by": entry.created_by,
        "created_at": _iso(entry.created_at),
        "status": entry.current.status,
        "version": entry.current.version,
        "current": _version_view(entry.current, with_restricted),
    }
    if history:
        view["versions"] = [
            _version_view(version, with_restricted) for version in entry.versions
        ]
    return view


def _settle(result: Result, *, not_found: str, build) -> Any:
    """Turn a domain result into the response, or into the error the outcome maps to."""
    outcome = result.outcome
    if outcome is CatalogOutcome.REPLAY:
        assert result.stored is not None
        # The same request again: the first answer, with its status and body. Nothing was written.
        return JSONResponse(
            status_code=result.stored.status_code, content=result.stored.body
        )
    if outcome is CatalogOutcome.KEY_REUSED:
        raise HTTPException(
            status_code=409, detail={"status": "idempotency_key_reused"}
        )
    if outcome is CatalogOutcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail=not_found)
    if outcome is CatalogOutcome.ENTRY_CLOSED:
        raise HTTPException(status_code=409, detail={"status": "entry_closed"})
    if outcome is CatalogOutcome.VERSION_CONFLICT:
        # Only the current version. The caller learns nothing else from a refused write.
        raise HTTPException(
            status_code=409,
            detail={"status": "version_conflict", "current_version": result.value},
        )
    if outcome is CatalogOutcome.UNKNOWN_VERIFICATION_RUN:
        raise HTTPException(status_code=422, detail="verification run is not known")
    if outcome is CatalogOutcome.INVALID_REPLACEMENT:
        raise HTTPException(
            status_code=422, detail="replacement must be another open catalog entry"
        )
    return build(result.value)


def _entry_change(
    operation: str,
    entry_id: uuid.UUID,
    route: str,
    body: BaseModel,
    actor: User,
    idempotency_key: str | None,
) -> Any:
    path = route.format(entry_id=entry_id)
    scope = _key_scope(actor, route, path, idempotency_key, body)

    def render(entry: Entry) -> dict[str, Any]:
        return _entry_view(entry, with_restricted=False, history=False)

    result = change_entry(
        str(entry_id),
        body.expected_version,
        operation,
        _values(body),
        actor.id,
        idempotency=scope,
        render=render,
    )
    return _settle(
        result,
        not_found="catalog entry not found",
        build=lambda entry: _entry_view(entry, with_restricted=False, history=False),
    )


# --- recommendations and verification runs ----------------------------------------------------------------------------


@catalog_router.post(RECOMMENDATIONS_ROUTE, status_code=201)
def create_recommendation(
    body: RecommendationRequest,
    actor: User = Depends(require_permission("catalog:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    scope = _key_scope(
        actor, RECOMMENDATIONS_ROUTE, RECOMMENDATIONS_ROUTE, idempotency_key, body
    )
    result = add_recommendation(
        _values(body), actor.id, idempotency=scope, render=_recommendation_view
    )
    return _settle(result, not_found="", build=_recommendation_view)


@catalog_router.get(RECOMMENDATIONS_ROUTE)
def list_catalog_recommendations(
    limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    cursor: str | None = Query(default=None, max_length=200),
    actor: User = Depends(require_permission("catalog:read")),
) -> dict[str, Any]:
    """A page of recommendations, oldest first. A recommendation is a suggestion: it carries no result."""
    page = list_recommendations(limit, cursor)
    if page is None:
        raise HTTPException(status_code=400, detail={"status": "invalid_cursor"})
    recommendations, next_cursor = page
    return {
        "items": [_recommendation_view(item) for item in recommendations],
        "next_cursor": next_cursor,
    }


@catalog_router.post(RUNS_ROUTE, status_code=201)
def record_run(
    recommendation_id: uuid.UUID,
    body: VerificationRunRequest,
    actor: User = Depends(require_permission("catalog:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = RUNS_ROUTE.format(recommendation_id=recommendation_id)
    scope = _key_scope(actor, RUNS_ROUTE, path, idempotency_key, body)
    result = record_verification_run(
        str(recommendation_id),
        _values(body),
        actor.id,
        idempotency=scope,
        render=_run_view,
    )
    return _settle(result, not_found="recommendation not found", build=_run_view)


# --- entries and versions ---------------------------------------------------------------------------------------------


@catalog_router.post(ENTRIES_ROUTE, status_code=201)
def create_catalog_entry(
    body: EntryCreateRequest,
    actor: User = Depends(require_permission("catalog:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    scope = _key_scope(actor, ENTRIES_ROUTE, ENTRIES_ROUTE, idempotency_key, body)

    def render(entry: Entry) -> dict[str, Any]:
        return _entry_view(entry, with_restricted=False, history=False)

    result = create_entry(_values(body), actor.id, idempotency=scope, render=render)
    return _settle(
        result,
        not_found="catalog entry not found",
        build=lambda entry: _entry_view(entry, with_restricted=False, history=False),
    )


@catalog_router.get(ENTRIES_ROUTE)
def list_catalog_entries(
    limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    cursor: str | None = Query(default=None, max_length=200),
    actor: User = Depends(require_permission("catalog:read")),
) -> dict[str, Any]:
    with_restricted = _may_read_restricted(actor)
    page = list_entries(limit, cursor, with_restricted=with_restricted)
    if page is None:
        raise HTTPException(status_code=400, detail={"status": "invalid_cursor"})
    entries, next_cursor = page
    return {
        "items": [
            _entry_view(entry, with_restricted=with_restricted, history=False)
            for entry in entries
        ],
        "next_cursor": next_cursor,
    }


@catalog_router.get(EXPORT_ROUTE)
def export_catalog(
    actor: User = Depends(require_permission("catalog:read")),
) -> dict[str, Any]:
    """The public export (schemas/catalog-export.v0.json). It is the same for every caller with catalog:read: it holds
    no restricted reference and no digest, whatever the caller may read. Each export writes one audit row."""
    return public_export(actor.id)


@catalog_router.get(ENTRY_ROUTE)
def get_catalog_entry(
    entry_id: uuid.UUID,
    actor: User = Depends(require_permission("catalog:read")),
) -> dict[str, Any]:
    with_restricted = _may_read_restricted(actor)
    entry = get_entry(str(entry_id), with_restricted=with_restricted)
    if entry is None:
        raise HTTPException(status_code=404, detail="catalog entry not found")
    return _entry_view(entry, with_restricted=with_restricted, history=True)


@catalog_router.post(VERSIONS_ROUTE, status_code=201)
def add_catalog_version(
    entry_id: uuid.UUID,
    body: VersionRequest,
    actor: User = Depends(require_permission("catalog:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    return _entry_change(
        "version", entry_id, VERSIONS_ROUTE, body, actor, idempotency_key
    )


@catalog_router.post(CORRECTIONS_ROUTE, status_code=201)
def correct_catalog_entry(
    entry_id: uuid.UUID,
    body: CorrectionRequest,
    actor: User = Depends(require_permission("catalog:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    return _entry_change(
        "correction", entry_id, CORRECTIONS_ROUTE, body, actor, idempotency_key
    )


@catalog_router.post(DISPUTES_ROUTE, status_code=201)
def dispute_catalog_entry(
    entry_id: uuid.UUID,
    body: DisputeRequest,
    actor: User = Depends(require_permission("catalog:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    return _entry_change(
        "dispute", entry_id, DISPUTES_ROUTE, body, actor, idempotency_key
    )


@catalog_router.post(WITHDRAWALS_ROUTE, status_code=201)
def withdraw_catalog_entry(
    entry_id: uuid.UUID,
    body: WithdrawalRequest,
    actor: User = Depends(require_permission("catalog:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    return _entry_change(
        "withdrawal", entry_id, WITHDRAWALS_ROUTE, body, actor, idempotency_key
    )


@catalog_router.post(SUPERSESSIONS_ROUTE, status_code=201)
def supersede_catalog_entry(
    entry_id: uuid.UUID,
    body: SupersessionRequest,
    actor: User = Depends(require_permission("catalog:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    return _entry_change(
        "supersession", entry_id, SUPERSESSIONS_ROUTE, body, actor, idempotency_key
    )
