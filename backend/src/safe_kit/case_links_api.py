"""Case link endpoints: a record that two cases concern the same incident (see case_links.py).

Mounted into the main app with app.include_router(case_links_router), as the case record endpoints are.

Access rules, from CaseScope (cases.py):
- reads need one of the case read permissions, and the case must be visible to the caller;
- writes need cases:triage, and both cases must be visible and open.
A missing permission answers 403. A case the caller may not see answers 404, the same as a missing one, so a link
never reveals a case the caller cannot see: for such a case the response has no ID and no title.
No free text is accepted or returned, and no reporter field is returned.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from safe_kit.auth import permissions_of, require_any_permission, require_permission
from safe_kit.case_links import CaseLink, LinkOutcome, add_link, list_links, remove_link
from safe_kit.case_records_api import CASE_READ_PERMISSIONS_FOR_RECORDS
from safe_kit.cases import CaseScope
from safe_kit.users import User

CASE_NOT_FOUND = HTTPException(status_code=404, detail="case not found")

case_links_router = APIRouter(tags=["case links"])


class CaseLinkRequest(BaseModel):
    linked_case_id: uuid.UUID


class CaseLinkResponse(BaseModel):
    # Both are null when the caller cannot see the linked case.
    linked_case_id: str | None
    linked_case_title: str | None
    created_at: datetime


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def _link_response(link: CaseLink) -> CaseLinkResponse:
    return CaseLinkResponse(
        linked_case_id=link.linked_case_id,
        linked_case_title=link.linked_case_title,
        created_at=link.created_at,
    )


def _raise_for(outcome: LinkOutcome) -> None:
    if outcome is LinkOutcome.NOT_FOUND:
        raise CASE_NOT_FOUND
    if outcome is LinkOutcome.SAME_CASE:
        raise HTTPException(status_code=422, detail="a case cannot be linked to itself")
    if outcome is LinkOutcome.CLOSED:
        raise HTTPException(status_code=409, detail="case is closed")
    if outcome is LinkOutcome.DUPLICATE:
        raise HTTPException(status_code=409, detail="cases are already linked")
    if outcome is LinkOutcome.LINK_NOT_FOUND:
        raise HTTPException(status_code=404, detail="link not found")


@case_links_router.get("/api/v1/cases/{case_id}/links", response_model=list[CaseLinkResponse])
def list_case_links(
    case_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> list[CaseLinkResponse]:
    links = list_links(str(case_id), _scope(actor))
    if links is None:
        raise CASE_NOT_FOUND
    return [_link_response(link) for link in links]


@case_links_router.post("/api/v1/cases/{case_id}/links", status_code=201, response_model=CaseLinkResponse)
def add_case_link(
    case_id: uuid.UUID,
    body: CaseLinkRequest,
    actor: User = Depends(require_permission("cases:triage")),
) -> CaseLinkResponse:
    outcome, link = add_link(str(case_id), str(body.linked_case_id), actor.id, _scope(actor))
    _raise_for(outcome)
    assert link is not None
    return _link_response(link)


@case_links_router.delete("/api/v1/cases/{case_id}/links/{linked_case_id}", status_code=204)
def remove_case_link(
    case_id: uuid.UUID,
    linked_case_id: uuid.UUID,
    actor: User = Depends(require_permission("cases:triage")),
) -> Response:
    outcome = remove_link(str(case_id), str(linked_case_id), actor.id, _scope(actor))
    _raise_for(outcome)
    return Response(status_code=204)
