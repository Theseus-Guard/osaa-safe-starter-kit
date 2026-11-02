"""Evidence endpoints. Mounted into the main app with app.include_router(evidence_router).

Access rules, from the case access control in cases.py (CaseScope):
- upload needs evidence:write, and the case must be visible to the caller;
- list, download, and custody need evidence:read, and the case must be visible to the caller. evidence:write
  alone does not grant reading the evidence of cases the caller cannot see.
A missing permission answers 403. A case or item the caller may not see answers 404, the same as a missing one.

The key provider is loaded once, when this module is imported, if SAFE_EVIDENCE_KEK_FILE or
SAFE_EVIDENCE_KEY_PROVIDER is set. A missing, empty, or badly permissioned key file raises here, so the API refuses
to start instead of failing on the first upload. When neither variable is set, the evidence endpoints answer 503.

Uploads are read with a byte limit on the request body itself, not only on the file. The multipart form is parsed
from a wrapped receive channel that stops at the limit, so a request with no Content-Length cannot exceed it.
"""

import os
import uuid
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request as ParserRequest
from starlette.types import Message, Receive

from safe_kit import evidence, redaction, tlp
from safe_kit.auth import permissions_of, require_permission
from safe_kit.cases import CaseScope, is_visible
from safe_kit.evidence_keys import KeyProvider, KeyProviderError, key_provider_from_env
from safe_kit.evidence_storage import StorageError, storage_from_env
from safe_kit.idempotency import KeyScope, fingerprint, valid_key
from safe_kit.users import User

EVIDENCE_READ = "evidence:read"
EVIDENCE_WRITE = "evidence:write"
DERIVATIVES_ROUTE = "/api/v1/evidence/{evidence_id}/derivatives"

# Refusals of a redaction, as (HTTP status, detail). The detail of a 422 names the rule that was broken.
REFUSAL_DETAILS: dict[str, tuple[int, str]] = {
    "not_found": (404, "evidence not found"),
    "not_available": (409, "evidence is not available"),
    "unsupported_media_type": (422, "redaction is supported for text/plain evidence only"),
    "invalid_removal": (422, "removed line ranges must be inside the document, in order, and not overlap"),
    "label_less_restrictive": (422, "the TLP label must be at least as restrictive as the original's label"),
    "integrity_failed": (500, "evidence failed its integrity check"),
    "expired": (409, "the redaction expired before it finished; retry"),
}

# Room for multipart headers and boundaries on top of the file limit.
MULTIPART_OVERHEAD = 64 * 1024
READ_CHUNK = 64 * 1024


def _load_provider_at_startup() -> KeyProvider | None:
    if not (os.environ.get("SAFE_EVIDENCE_KEK_FILE") or os.environ.get("SAFE_EVIDENCE_KEY_PROVIDER")):
        return None
    # Raises KeyProviderError (or NotImplementedError for the Vault stub) when the configuration is unusable.
    return key_provider_from_env()


_PROVIDER: KeyProvider | None = _load_provider_at_startup()

evidence_router = APIRouter(tags=["evidence"])


class EvidenceResponse(BaseModel):
    id: str
    case_id: str
    uploaded_by: str
    original_filename: str
    media_type: str
    size_bytes: int
    sha256_plaintext: str | None
    status: str
    created_at: datetime
    # Null for an uploaded original; a derived item carries its TLP audience label.
    tlp_label: str | None = None


class EvidencePage(BaseModel):
    items: list[EvidenceResponse]
    total: int
    limit: int
    offset: int


class CustodyEventResponse(BaseModel):
    id: str
    action: str
    actor_id: str | None
    details: dict[str, Any]
    created_at: datetime


class _BodyTooLarge(Exception):
    pass


def _key_provider() -> KeyProvider:
    if _PROVIDER is None:
        raise HTTPException(status_code=503, detail="evidence storage is not configured")
    return _PROVIDER


def _response(item: evidence.EvidenceItem) -> EvidenceResponse:
    return EvidenceResponse(
        id=item.id,
        case_id=item.case_id,
        uploaded_by=item.uploaded_by,
        original_filename=item.original_filename,
        media_type=item.media_type,
        size_bytes=item.size_bytes,
        sha256_plaintext=item.sha256_plaintext,
        status=item.status,
        created_at=item.created_at,
        tlp_label=item.tlp_label,
    )


def _scope(user: User) -> CaseScope:
    return CaseScope.from_permissions(user.id, permissions_of(user))


def _bounded_receive(receive: Receive, limit: int) -> Receive:
    """Wrap the request's receive channel so that reading the body stops once more than limit bytes arrive."""
    received = 0

    async def bounded() -> Message:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                raise _BodyTooLarge()
        return message

    return bounded


async def _read_limited(part: UploadFile, max_bytes: int) -> bytes:
    buffer = bytearray()
    while chunk := await part.read(READ_CHUNK):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(status_code=413, detail=f"file exceeds the limit of {max_bytes} bytes")
    return bytes(buffer)


async def _read_file_field(request: Request, max_bytes: int) -> tuple[str | None, str | None, bytes]:
    body_limit = max_bytes + MULTIPART_OVERHEAD
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > body_limit:
        raise HTTPException(status_code=413, detail=f"request exceeds the limit of {max_bytes} bytes")
    parser = ParserRequest(request.scope, _bounded_receive(request.receive, body_limit))
    try:
        form = await parser.form()
    except _BodyTooLarge:
        raise HTTPException(status_code=413, detail=f"request exceeds the limit of {max_bytes} bytes") from None
    except MultiPartException:
        raise HTTPException(status_code=400, detail="malformed multipart body") from None
    try:
        part = form.get("file")
        if not isinstance(part, UploadFile):
            raise HTTPException(status_code=422, detail="multipart field 'file' is required")
        data = await _read_limited(part, max_bytes)
        return part.filename, part.content_type, data
    finally:
        await form.close()


def _content_disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace("?", "_").replace('"', "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def _readable_stored(evidence_id: str, user: User) -> evidence.StoredEvidence:
    """The item if the caller may read it. Two checks run after the RBAC check of the route: the case ACL (find_stored)
    and the TLP audience of the item (tlp.item_permitted). Either one failing answers the same 404 as a missing item."""
    stored = evidence.find_stored(evidence_id, _scope(user))
    if stored is None or not tlp.item_permitted(stored, tlp.recipient_for(user)):
        raise HTTPException(status_code=404, detail="evidence not found")
    return stored


@evidence_router.post("/api/v1/cases/{case_id}/evidence", status_code=201, response_model=EvidenceResponse)
async def upload_evidence(
    request: Request,
    case_id: uuid.UUID,
    actor: User = Depends(require_permission(EVIDENCE_WRITE)),
) -> EvidenceResponse:
    """Multipart upload, field name "file". The body is read with a size limit; the file is encrypted before storage."""
    provider = _key_provider()
    if not await run_in_threadpool(is_visible, str(case_id), _scope(actor)):
        raise HTTPException(status_code=404, detail="case not found")
    filename, content_type, data = await _read_file_field(request, evidence.max_bytes_from_env())
    if not data:
        raise HTTPException(status_code=422, detail="file is empty")
    try:
        media_type = evidence.check_media_type(content_type, data)
    except evidence.UnsupportedMediaType:
        raise HTTPException(status_code=415, detail="media type is not allowed") from None
    try:
        item = await run_in_threadpool(
            evidence.create_evidence,
            str(case_id),
            actor.id,
            filename,
            media_type,
            data,
            storage_from_env(),
            provider,
            _scope(actor),
        )
    except evidence.CaseNotVisible:
        # The case stopped being visible after the check above; refused the same way as a missing case.
        raise HTTPException(status_code=404, detail="case not found") from None
    except evidence.UploadExpired:
        raise HTTPException(status_code=409, detail="the upload expired before it finished; retry") from None
    except (StorageError, KeyProviderError):
        raise HTTPException(status_code=500, detail="evidence could not be stored") from None
    return _response(item)


@evidence_router.get("/api/v1/cases/{case_id}/evidence", response_model=EvidencePage)
def list_case_evidence(
    case_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_permission(EVIDENCE_READ)),
) -> EvidencePage:
    if not is_visible(str(case_id), _scope(user)):
        raise HTTPException(status_code=404, detail="case not found")
    items, total = evidence.list_evidence(str(case_id), limit, offset, tlp.recipient_for(user))
    return EvidencePage(items=[_response(item) for item in items], total=total, limit=limit, offset=offset)


@evidence_router.get("/api/v1/evidence/{evidence_id}/download")
def download_evidence(evidence_id: uuid.UUID, user: User = Depends(require_permission(EVIDENCE_READ))) -> Response:
    stored = _readable_stored(str(evidence_id), user)
    if stored.status != evidence.AVAILABLE:
        raise HTTPException(status_code=409, detail="evidence is not available")
    try:
        content = evidence.read_original(stored, storage_from_env(), _key_provider())
    except evidence.VerificationFailed as failure:
        # The failure is recorded in the custody log. No content is returned.
        evidence.record_failure(stored.id, user.id, failure.reason)
        raise HTTPException(status_code=500, detail="evidence failed its integrity check") from None
    # The read marker commits before any byte is returned. If it cannot be recorded, nothing is returned.
    evidence.record_download(stored.id, user.id, stored.case_id)
    return Response(
        content=content,
        media_type=stored.media_type,
        headers={
            "Content-Disposition": _content_disposition(stored.original_filename),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@evidence_router.get("/api/v1/evidence/{evidence_id}/custody", response_model=list[CustodyEventResponse])
def custody_log(
    evidence_id: uuid.UUID, user: User = Depends(require_permission(EVIDENCE_READ))
) -> list[CustodyEventResponse]:
    stored = _readable_stored(str(evidence_id), user)
    return [
        CustodyEventResponse(
            id=event.id,
            action=event.action,
            actor_id=event.actor_id,
            details=event.details,
            created_at=event.created_at,
        )
        for event in evidence.custody_events(stored.id)
    ]


class RemovedRangeRequest(BaseModel):
    from_line: int = Field(ge=1)
    to_line: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=redaction.REASON_LIMIT)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class RedactionRequest(BaseModel):
    # The lines to remove, 1-based and inclusive. Every removed line is replaced by a marker, and the reason is kept.
    removed: list[RemovedRangeRequest] = Field(min_length=1, max_length=redaction.MAX_REMOVED_RANGES)
    tlp_label: Literal["RED", "AMBER", "AMBER+STRICT", "GREEN", "CLEAR"]


class ClaimAssessmentResponse(BaseModel):
    review_layer_id: str
    round_id: str
    layer: str
    outcome: str
    reason: str


class DerivationResponse(BaseModel):
    redaction_id: str
    original_evidence_id: str
    derived: EvidenceResponse
    manifest: dict[str, Any]
    claim_assessments: list[ClaimAssessmentResponse]


def _derivation_response(derivation: redaction.Derivation) -> DerivationResponse:
    return DerivationResponse(
        redaction_id=derivation.redaction_id,
        original_evidence_id=derivation.original_evidence_id,
        derived=_response(derivation.derived),
        manifest=derivation.manifest,
        claim_assessments=[
            ClaimAssessmentResponse(
                review_layer_id=claim.review_layer_id,
                round_id=claim.round_id,
                layer=claim.layer,
                outcome=claim.outcome,
                reason=claim.reason,
            )
            for claim in derivation.claims
        ],
    )


@evidence_router.post(DERIVATIVES_ROUTE, status_code=201, response_model=DerivationResponse)
def create_derivative(
    evidence_id: uuid.UUID,
    body: RedactionRequest,
    request: Request,
    actor: User = Depends(require_permission(EVIDENCE_WRITE)),
    _reader: User = Depends(require_permission(EVIDENCE_READ)),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DerivationResponse | JSONResponse:
    """Create a redacted derived item of a text/plain evidence item. Needs evidence:write (to create the item) and
    evidence:read (to read the original). The original must pass the case ACL and its TLP rule. A replayed key
    returns the first answer."""
    # Checked after the permission checks, so a caller without them learns nothing about keys (see reviews_api).
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail={"status": "idempotency_key_required"})
    if not valid_key(idempotency_key):
        raise HTTPException(status_code=400, detail={"status": "idempotency_key_invalid"})
    provider = _key_provider()
    original = _readable_stored(str(evidence_id), actor)
    scope = _scope(actor)
    key_scope = KeyScope(
        principal_id=actor.id,
        method="POST",
        route=DERIVATIVES_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint("POST", request.url.path, request.url.query, body.model_dump(mode="json")),
    )
    removed = [
        redaction.RemovedRange(item.from_line, item.to_line, item.reason) for item in body.removed
    ]
    try:
        result = redaction.create_derivative(
            original,
            actor.id,
            scope,
            removed,
            body.tlp_label,
            storage_from_env(),
            provider,
            idempotency=key_scope,
            render=lambda derivation: _derivation_response(derivation).model_dump(mode="json"),
        )
    except redaction.RedactionRefused as refusal:
        status_code, detail = REFUSAL_DETAILS[refusal.code]
        raise HTTPException(status_code=status_code, detail=detail) from None
    except evidence.CaseNotVisible:
        raise HTTPException(status_code=404, detail=REFUSAL_DETAILS["not_found"][1]) from None
    except (StorageError, KeyProviderError):
        raise HTTPException(status_code=500, detail="evidence could not be stored") from None
    if result.outcome is redaction.Outcome.REPLAY:
        # The same request again: the first answer, with its status and body. Nothing was written.
        assert result.stored is not None
        return JSONResponse(status_code=result.stored.status_code, content=result.stored.body)
    if result.outcome is redaction.Outcome.KEY_REUSED:
        raise HTTPException(status_code=409, detail={"status": "idempotency_key_reused"})
    assert result.derivation is not None
    return _derivation_response(result.derivation)


@evidence_router.get("/api/v1/evidence/{evidence_id}/redaction", response_model=DerivationResponse)
def get_redaction(
    evidence_id: uuid.UUID, user: User = Depends(require_permission(EVIDENCE_READ))
) -> DerivationResponse:
    """The manifest and claim assessments of a derived item. An original, or an item the caller may not read, is 404."""
    _readable_stored(str(evidence_id), user)
    derivation = redaction.load_derivation(str(evidence_id))
    if derivation is None:
        raise HTTPException(status_code=404, detail="evidence not found")
    return _derivation_response(derivation)
