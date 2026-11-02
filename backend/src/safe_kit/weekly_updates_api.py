"""Weekly update endpoints: list the updates of a case and read one (see safe_kit.weekly_updates).

Mounted into the main app with app.include_router(weekly_updates_router), as the other case endpoints are.

Access rules, from CaseScope (cases.py): a reader needs one of the case read permissions, and the case must be visible
to the caller. A case the caller may not see answers 404, the same as a missing case. The updates are read-only here:
the worker creates them, and no endpoint writes one.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from safe_kit.auth import permissions_of, require_any_permission
from safe_kit.case_records_api import CASE_READ_PERMISSIONS_FOR_RECORDS
from safe_kit.cases import CaseScope
from safe_kit.users import User
from safe_kit.weekly_updates import get_weekly_update, list_weekly_updates

CASE_NOT_FOUND = HTTPException(status_code=404, detail="case not found")
UPDATE_NOT_FOUND = HTTPException(status_code=404, detail="weekly update not found")

weekly_updates_router = APIRouter(tags=["weekly updates"])


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


@weekly_updates_router.get("/api/v1/cases/{case_id}/weekly-updates", response_model=list[dict[str, Any]])
def list_case_weekly_updates(
    case_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> list[dict[str, Any]]:
    documents = list_weekly_updates(str(case_id), _scope(actor))
    if documents is None:
        raise CASE_NOT_FOUND
    return documents


@weekly_updates_router.get(
    "/api/v1/cases/{case_id}/weekly-updates/{update_id}", response_model=dict[str, Any]
)
def get_case_weekly_update(
    case_id: uuid.UUID,
    update_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> dict[str, Any]:
    case_visible, document = get_weekly_update(str(case_id), str(update_id), _scope(actor))
    if not case_visible:
        raise CASE_NOT_FOUND
    if document is None:
        raise UPDATE_NOT_FOUND
    return document
