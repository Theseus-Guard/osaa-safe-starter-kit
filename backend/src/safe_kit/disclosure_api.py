"""Disclosure package endpoints: versions, the preview, and the human approval (plans/f0/api-draft.md section 2.5).

Mounted into the main app with app.include_router(disclosure_router), as the case record endpoints are.

Access rules:
- Every route needs a login (401 without one) and the permission of its verb: disclosures:read for reads,
  disclosures:prepare to create a version, disclosures:approve to approve one (403 without it).
- The case must be visible to the caller (cases.CaseScope). A case the caller may not see answers 404, the same as a
  missing one. A version that is not part of the case answers 404 too.
- An item the caller may not see under the TLP audience rule is listed as withheld. Its ID, label and checksum are not
  returned, and neither is its line in the manifest.
- Side-effect POSTs need an Idempotency-Key (plans/f0/api-draft.md section 3.4). A repeated request with the same key and
  body returns the first answer and writes nothing.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from safe_kit import disclosure, tlp
from safe_kit.auth import permissions_of, require_permission
from safe_kit.cases import CaseScope
from safe_kit.disclosure import Outcome, Result, Version
from safe_kit.idempotency import checked_key, key_scope
from safe_kit.users import User

PACKAGES_ROUTE = "/api/v1/cases/{case_id}/disclosures"
VERSION_ROUTE = "/api/v1/cases/{case_id}/disclosures/{version_id}"
PREVIEW_ROUTE = "/api/v1/cases/{case_id}/disclosures/{version_id}/preview"
EXPORT_ROUTE = "/api/v1/cases/{case_id}/disclosures/{version_id}/export"
APPROVALS_ROUTE = "/api/v1/cases/{case_id}/disclosures/{version_id}/approvals"
DEFAULT_PAGE = 50
MAX_PAGE = 200

disclosure_router = APIRouter(tags=["disclosure"])

Recipient = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=disclosure.RECIPIENT_LIMIT
    ),
]
Purpose = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=disclosure.PURPOSE_LIMIT
    ),
]


class _Strict(BaseModel):
    # An unknown field is refused, so a request cannot carry a field the package does not hold.
    model_config = ConfigDict(extra="forbid")


class VersionRequest(_Strict):
    # No package_id creates a new package at version 1. A package_id adds the next version, and expected_version must
    # be the version the editor read. A different latest version answers 409 and nothing is stored.
    package_id: uuid.UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    template: Literal[
        "confidential_rapid_alert",
        "de_identified_member_advisory",
        "redacted_public_safety_report",
    ]
    tlp_audience: Literal["RED", "AMBER", "AMBER+STRICT", "GREEN", "CLEAR"]
    recipient: Recipient
    purpose: Purpose
    evidence_ids: list[uuid.UUID] = Field(
        min_length=1, max_length=disclosure.MAX_EVIDENCE_PER_VERSION
    )

    @model_validator(mode="after")
    def versions_and_items_agree(self) -> "VersionRequest":
        if self.package_id is None and self.expected_version is not None:
            raise ValueError(
                "expected_version is given only with an existing package_id"
            )
        if self.package_id is not None and self.expected_version is None:
            raise ValueError("expected_version is required with an existing package_id")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be unique")
        return self


class ApprovalRequest(_Strict):
    # The approver confirms the recipient, the purpose and the digest of the version. Each must match the version.
    recipient: Recipient
    purpose: Purpose
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The identifier of the approved recipient, kept outside SAFE. A notice of this version must name the same one.
    recipient_ref: str = Field(pattern=disclosure.RECIPIENT_REF_PATTERN)


# --- helpers --------------------------------------------------------------------------------------------------------


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _version_view(version: Version, recipient: tlp.Recipient) -> dict[str, Any]:
    """The version as the caller may see it. Items outside the caller's TLP audience are withheld from the item list
    and from the manifest; the digest is the same for every caller."""
    visible = disclosure.item_visibility(version, recipient)
    manifest_by_item = {
        entry["evidence_id"]: entry for entry in version.manifest["items"]
    }
    evidence: list[dict[str, Any]] = []
    manifest_items: list[dict[str, Any]] = []
    withheld = 0
    for item, may_see in zip(version.items, visible):
        if not may_see:
            withheld += 1
            evidence.append({"withheld": True})
            continue
        evidence.append(
            {
                "withheld": False,
                "evidence_id": item.evidence_id,
                "redaction_id": item.redaction_id,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
                "sha256_plaintext": item.sha256_plaintext,
                "tlp_label": item.tlp_label,
            }
        )
        manifest_items.append(manifest_by_item[item.evidence_id])
    approval = None
    if version.approval is not None:
        approval = {
            "id": version.approval.id,
            "recipient": version.approval.recipient,
            "recipient_ref": version.approval.recipient_ref,
            "purpose": version.approval.purpose,
            "digest": version.approval.digest,
            "preparer_id": version.approval.preparer_id,
            "approved_by": version.approval.approved_by,
            "approved_at": _iso(version.approval.approved_at),
            "separation_rule": "preparer_differs_from_approver",
        }
    return {
        "id": version.id,
        "package_id": version.package_id,
        "case_id": version.case_id,
        "version": version.version,
        "is_latest": version.is_latest,
        "template": version.template,
        "tlp_audience": version.tlp_audience,
        "recipient": version.recipient,
        "purpose": version.purpose,
        "digest": version.digest,
        "approval_state": version.approval_state,
        "approval": approval,
        "evidence": evidence,
        "withheld_count": withheld,
        "manifest": {"items": manifest_items},
        "created_by": version.created_by,
        "created_at": _iso(version.created_at),
    }


def _settle(result: Result, recipient: tlp.Recipient) -> Any:
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
    if outcome is Outcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="case not found")
    if outcome is Outcome.CLOSED:
        raise HTTPException(status_code=409, detail={"status": "case_closed"})
    if outcome is Outcome.PACKAGE_NOT_FOUND:
        raise HTTPException(status_code=404, detail="disclosure package not found")
    if outcome is Outcome.VERSION_NOT_FOUND:
        raise HTTPException(status_code=404, detail="disclosure version not found")
    if outcome is Outcome.VERSION_CONFLICT:
        # Only the latest version number. The caller learns nothing else from a refused write.
        raise HTTPException(
            status_code=409,
            detail={"status": "version_conflict", "current_version": result.value},
        )
    if outcome is Outcome.NO_CHANGE:
        raise HTTPException(status_code=409, detail={"status": "no_change"})
    if outcome is Outcome.EVIDENCE_NOT_ELIGIBLE:
        # One answer for a missing item, an original, a failed one, one of another case, and one the caller may not see.
        raise HTTPException(status_code=422, detail={"status": "evidence_not_eligible"})
    if outcome is Outcome.AUDIENCE_WIDER_THAN_EVIDENCE:
        raise HTTPException(
            status_code=422, detail={"status": "audience_wider_than_evidence"}
        )
    if outcome is Outcome.NOT_LATEST:
        raise HTTPException(status_code=409, detail={"status": "not_latest_version"})
    if outcome is Outcome.ALREADY_APPROVED:
        raise HTTPException(status_code=409, detail={"status": "already_approved"})
    if outcome is Outcome.PREPARER_IS_APPROVER:
        raise HTTPException(status_code=403, detail={"status": "preparer_is_approver"})
    if outcome is Outcome.BINDING_MISMATCH:
        raise HTTPException(
            status_code=409, detail={"status": "approval_binding_mismatch"}
        )
    if outcome is Outcome.EVIDENCE_NOT_VISIBLE:
        raise HTTPException(status_code=403, detail={"status": "evidence_not_visible"})
    if outcome is Outcome.INTEGRITY_FAILED:
        raise HTTPException(status_code=409, detail={"status": "digest_mismatch"})
    assert result.value is not None
    return _version_view(result.value, recipient)


# --- routes ---------------------------------------------------------------------------------------------------------


@disclosure_router.post(PACKAGES_ROUTE, status_code=201)
def create_disclosure_version(
    case_id: uuid.UUID,
    body: VersionRequest,
    actor: User = Depends(require_permission("disclosures:prepare")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = PACKAGES_ROUTE.format(case_id=case_id)
    scope_key = key_scope(
        actor.id,
        "POST",
        PACKAGES_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    recipient = tlp.recipient_for(actor)
    values = {
        "package_id": str(body.package_id) if body.package_id is not None else None,
        "expected_version": body.expected_version,
        "template": body.template,
        "tlp_audience": body.tlp_audience,
        "recipient": body.recipient,
        "purpose": body.purpose,
        "evidence_ids": [str(item) for item in body.evidence_ids],
    }
    result = disclosure.create_version(
        str(case_id),
        values,
        _scope(actor),
        recipient,
        idempotency=scope_key,
        render=lambda version: _version_view(version, recipient),
    )
    return _settle(result, recipient)


@disclosure_router.get(PACKAGES_ROUTE)
def list_disclosure_versions(
    case_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    actor: User = Depends(require_permission("disclosures:read")),
) -> dict[str, Any]:
    recipient = tlp.recipient_for(actor)
    versions = disclosure.list_versions(str(case_id), _scope(actor), limit)
    if versions is None:
        raise HTTPException(status_code=404, detail="case not found")
    return {"items": [_version_view(version, recipient) for version in versions]}


@disclosure_router.get(VERSION_ROUTE)
def get_disclosure_version(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    actor: User = Depends(require_permission("disclosures:read")),
) -> dict[str, Any]:
    scope = _scope(actor)
    # The case rule, or for an approver the packet rule on this version (safe_kit.disclosure, access_for).
    if disclosure.access_for(str(case_id), str(version_id), scope) is None:
        raise HTTPException(status_code=404, detail="case not found")
    version = disclosure.get_version(str(case_id), str(version_id), scope)
    if version is None:
        raise HTTPException(status_code=404, detail="disclosure version not found")
    return _version_view(version, tlp.recipient_for(actor))


@disclosure_router.get(PREVIEW_ROUTE)
def preview_disclosure_version(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    actor: User = Depends(require_permission("disclosures:read")),
) -> dict[str, Any]:
    scope = _scope(actor)
    # The case rule, or for an approver the packet rule on this version (safe_kit.disclosure, access_for).
    if disclosure.access_for(str(case_id), str(version_id), scope) is None:
        raise HTTPException(status_code=404, detail="case not found")
    version = disclosure.get_version(str(case_id), str(version_id), scope)
    if version is None:
        raise HTTPException(status_code=404, detail="disclosure version not found")
    return disclosure.preview(version, tlp.recipient_for(actor))


@disclosure_router.get(EXPORT_ROUTE)
def export_disclosure_version(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    actor: User = Depends(require_permission("disclosures:read")),
) -> dict[str, Any]:
    """The approved version as the versioned export (schemas/disclosure-export.v0.json). It is a release: it is refused
    for a version without an approval or without a release attempt, and each export is written to the audit log without
    its content."""
    result = disclosure.export_version(
        str(case_id), str(version_id), _scope(actor), tlp.recipient_for(actor)
    )
    if result.outcome is Outcome.NOT_FOUND:
        raise HTTPException(status_code=404, detail="case not found")
    if result.outcome is Outcome.VERSION_NOT_FOUND:
        raise HTTPException(status_code=404, detail="disclosure version not found")
    if result.outcome is Outcome.NOT_APPROVED:
        raise HTTPException(status_code=409, detail={"status": "version_not_approved"})
    if result.outcome is Outcome.INTEGRITY_FAILED:
        raise HTTPException(status_code=409, detail={"status": "digest_mismatch"})
    if result.outcome is Outcome.APPROVAL_MISMATCH:
        raise HTTPException(status_code=409, detail={"status": "approval_mismatch"})
    if result.outcome is Outcome.RELEASE_APPROVAL_MISSING:
        raise HTTPException(
            status_code=409, detail={"status": "release_approval_missing"}
        )
    if result.outcome is Outcome.RELEASE_TRACE_MISSING:
        raise HTTPException(status_code=409, detail={"status": "release_trace_missing"})
    assert result.outcome is Outcome.OK and result.value is not None
    return result.value


@disclosure_router.post(APPROVALS_ROUTE, status_code=201)
def approve_disclosure_version(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    body: ApprovalRequest,
    actor: User = Depends(require_permission("disclosures:approve")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = APPROVALS_ROUTE.format(case_id=case_id, version_id=version_id)
    scope_key = key_scope(
        actor.id,
        "POST",
        APPROVALS_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    recipient = tlp.recipient_for(actor)
    result = disclosure.approve_version(
        str(case_id),
        str(version_id),
        {
            "recipient": body.recipient,
            "purpose": body.purpose,
            "digest": body.digest,
            "recipient_ref": body.recipient_ref,
        },
        _scope(actor),
        recipient,
        idempotency=scope_key,
        render=lambda version: _version_view(version, recipient),
    )
    return _settle(result, recipient)
