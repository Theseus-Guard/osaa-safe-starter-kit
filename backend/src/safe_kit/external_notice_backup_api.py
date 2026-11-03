"""Backup approver routes of an external notice (F3 A6; safe_kit.external_notice_backup; owner decisions 2026-11-03).

Routes:
- PUT    /api/v1/cases/{case_id}/notice-backups                         the owner sets the backup pool of the case (200)
- GET    /api/v1/cases/{case_id}/notice-backups                         the current pool (disclosures:read; the case visible)
- GET    .../disclosures/{version_id}/escalations/{escalation_id}/backup-packet
                                                                        the named backup's narrow read: the packet and the
                                                                        missing facts (disclosures:approve, the named backup)
- POST   .../disclosures/{version_id}/escalations/{escalation_id}/backup-decision
                                                                        the backup's decision: approve, reject (reason
                                                                        required) or emergency_manual_notice (201)

Access rules:
- Assigning needs disclosures:assign_backup. The platform administrator does not hold it, and a holder of
  disclosures:approve is refused (403 approver_cannot_assign_backup). The case must be visible to the caller.
- The backup decision and the packet need disclosures:approve. The caller must be the backup named on the escalation,
  and the backup must still be in the pool. The read does not open the case: it returns the packet and the missing facts
  only. A case-level read of the backup is governed by the case rules, which this route does not change.
- The decision waits for backup_may_act_at. Before it, the answer is 409 backup_wait_not_over with the first moment.
- Every POST needs an Idempotency-Key. A repeated request with the same key and body returns the first answer and writes
  nothing.

Nothing here sends a notice, approves a version or changes a deadline.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from safe_kit import cases, tlp
from safe_kit import external_notice_backup as backup
from safe_kit.auth import permissions_of, require_permission
from safe_kit.cases import CaseScope
from safe_kit.external_notice_backup import Backup, Decision, Outcome, Result
from safe_kit.idempotency import checked_key, key_scope
from safe_kit.users import User

POOL_ROUTE = "/api/v1/cases/{case_id}/notice-backups"
PACKET_ROUTE = "/api/v1/cases/{case_id}/disclosures/{version_id}/escalations/{escalation_id}/backup-packet"
DECISION_ROUTE = "/api/v1/cases/{case_id}/disclosures/{version_id}/escalations/{escalation_id}/backup-decision"

backup_router = APIRouter(tags=["disclosure"])

Reason = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=backup.REASON_LIMIT
    ),
]


class _Strict(BaseModel):
    # An unknown field is refused, so a request cannot carry a field the record does not hold.
    model_config = ConfigDict(extra="forbid")


class PoolReplace(_Strict):
    # The whole pool of the case. The list is replaced; the application refuses fewer than two distinct people.
    backup_user_ids: list[uuid.UUID] = Field(max_length=backup.MAX_POOL)


class BackupDecisionCreate(_Strict):
    decision: Literal["approve", "reject", "emergency_manual_notice"]
    reason: Reason | None = None

    @model_validator(mode="after")
    def reject_needs_a_reason(self) -> "BackupDecisionCreate":
        if self.decision == "reject" and self.reason is None:
            raise ValueError("a reject needs a reason")
        return self


# --- helpers --------------------------------------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _backup_view(item: Backup) -> dict[str, Any]:
    return {
        "id": item.id,
        "case_id": item.case_id,
        "backup_user_id": item.backup_user_id,
        "assigned_by": item.assigned_by,
        "assigned_at": _iso(item.assigned_at),
    }


def _decision_view(item: Decision) -> dict[str, Any]:
    return {
        "id": item.id,
        "escalation_id": item.escalation_id,
        "case_id": item.case_id,
        "version_id": item.version_id,
        "decision": item.decision,
        "reason": item.reason,
        "decided_by": item.decided_by,
        "decided_at": _iso(item.decided_at),
    }


def _packet_view(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "escalation_id": value["escalation_id"],
        "missing_facts": value["missing_facts"],
        "may_act_at": _iso(value["may_act_at"]),
        "packet": value["packet"],
    }


def _pool_view(pool: list[Backup]) -> dict[str, Any]:
    return {"items": [_backup_view(item) for item in pool]}


# The status of a refused write or read. Nothing about another object is ever in the answer.
_POOL_REFUSALS: dict[Outcome, tuple[int, dict[str, Any] | str]] = {
    Outcome.NOT_FOUND: (404, "case not found"),
    Outcome.CLOSED: (409, {"status": "case_closed"}),
    Outcome.ASSIGNER_IS_APPROVER: (403, {"status": "approver_cannot_assign_backup"}),
    Outcome.ASSIGNER_IS_BACKUP: (422, {"status": "assigner_cannot_be_backup"}),
    Outcome.POOL_TOO_SMALL: (422, {"status": "backup_pool_too_small"}),
    Outcome.BACKUP_NOT_FOUND: (422, {"status": "backup_not_found"}),
    Outcome.BACKUP_NOT_APPROVER: (422, {"status": "backup_not_approver"}),
}

_BACKUP_REFUSALS: dict[Outcome, tuple[int, dict[str, Any] | str]] = {
    Outcome.NOT_FOUND: (404, "case not found"),
    Outcome.CLOSED: (409, {"status": "case_closed"}),
    Outcome.ESCALATION_NOT_FOUND: (404, "escalation not found"),
    Outcome.NOT_THE_BACKUP: (403, {"status": "not_the_backup"}),
    Outcome.NOT_IN_POOL: (409, {"status": "backup_not_in_pool"}),
    Outcome.ALREADY_DECIDED: (409, {"status": "already_decided"}),
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
    if outcome is Outcome.WAIT_NOT_OVER:
        raise HTTPException(
            status_code=409,
            detail={"status": "backup_wait_not_over", "may_act_at": _iso(result.value)},
        )
    if outcome in _POOL_REFUSALS:
        status_code, detail = _POOL_REFUSALS[outcome]
        raise HTTPException(status_code=status_code, detail=detail)
    if outcome in _BACKUP_REFUSALS:
        status_code, detail = _BACKUP_REFUSALS[outcome]
        raise HTTPException(status_code=status_code, detail=detail)
    assert result.value is not None
    return view(result.value)


# --- routes ---------------------------------------------------------------------------------------------------------


@backup_router.put(POOL_ROUTE)
def set_backup_pool(
    case_id: uuid.UUID,
    body: PoolReplace,
    actor: User = Depends(require_permission(backup.ASSIGN_PERMISSION)),
) -> Any:
    result = backup.assign_pool(
        str(case_id),
        [str(user_id) for user_id in body.backup_user_ids],
        actor.id,
        permissions_of(actor),
    )
    return _settle(result, _pool_view)


@backup_router.get(POOL_ROUTE)
def list_backup_pool(
    case_id: uuid.UUID,
    actor: User = Depends(require_permission("disclosures:read")),
) -> dict[str, Any]:
    scope = CaseScope.from_permissions(actor.id, permissions_of(actor))
    if not cases.is_visible(str(case_id), scope):
        raise HTTPException(status_code=404, detail="case not found")
    return _pool_view(backup.list_pool(str(case_id)))


@backup_router.get(PACKET_ROUTE)
def read_backup_packet(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    escalation_id: uuid.UUID,
    actor: User = Depends(require_permission(backup.BACKUP_PERMISSION)),
) -> Any:
    result = backup.read_packet(
        str(case_id),
        str(version_id),
        str(escalation_id),
        actor.id,
        tlp.recipient_for(actor),
    )
    return _settle(result, _packet_view)


@backup_router.post(DECISION_ROUTE, status_code=201)
def record_backup_decision(
    case_id: uuid.UUID,
    version_id: uuid.UUID,
    escalation_id: uuid.UUID,
    body: BackupDecisionCreate,
    actor: User = Depends(require_permission(backup.BACKUP_PERMISSION)),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    path = DECISION_ROUTE.format(
        case_id=case_id, version_id=version_id, escalation_id=escalation_id
    )
    scope_key = key_scope(
        actor.id,
        "POST",
        DECISION_ROUTE,
        checked_key(idempotency_key),
        path,
        body.model_dump(mode="json"),
    )
    result = backup.record_decision(
        str(case_id),
        str(version_id),
        str(escalation_id),
        body.decision,
        body.reason,
        actor.id,
        idempotency=scope_key,
        render=_decision_view,
    )
    return _settle(result, _decision_view)
