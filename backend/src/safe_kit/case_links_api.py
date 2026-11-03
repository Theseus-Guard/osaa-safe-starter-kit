"""Case link endpoints: a typed record that two cases concern the same incident (see case_links.py).

Mounted into the main app with app.include_router(case_links_router), as the case record endpoints are.

Access rules, from CaseScope (cases.py):
- reads need one of the case read permissions, and the case must be visible to the caller (a read may also see the
  other case through a link: see CaseScope.visibility_sql);
- writes need cases:triage, and both cases must be visible for a change (own rights only) and open.
A missing permission answers 403. A case the caller may not see answers 404, the same as a missing one, so a link
never reveals a case the caller cannot see: for such a case the response has no ID and no title.
The reason is free text of at most 500 characters. It is returned only to a user who can read the other case of the
link (reason and linked case are null otherwise). The main or root case ID is returned under the same rule. No reporter
field is returned.
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

from safe_kit.auth import permissions_of, require_any_permission, require_permission
from safe_kit.case_links import (
    CaseLink,
    LinkOutcome,
    add_link,
    list_links,
    remove_link,
    request_error,
)
from safe_kit.case_records_api import CASE_READ_PERMISSIONS_FOR_RECORDS
from safe_kit.cases import CaseScope
from safe_kit.idempotency import checked_key, key_scope
from safe_kit.users import User

CASE_NOT_FOUND = HTTPException(status_code=404, detail="case not found")
LINKS_ROUTE = "/api/v1/cases/{case_id}/links"

case_links_router = APIRouter(tags=["case links"])


class CaseLinkRequest(BaseModel):
    linked_case_id: uuid.UUID
    link_type: Literal["duplicate", "related", "root_sub"]
    reason: str
    # The checkbox "users of the linked case can see all cases": off by default.
    share_visibility: bool = False
    # The main case of a duplicate or the root case of a root_sub link (one of the two cases). Not for a related link.
    primary_case_id: uuid.UUID | None = None


class CaseLinkResponse(BaseModel):
    # linked_case_id, linked_case_title and reason are null when the caller cannot see the linked case.
    linked_case_id: str | None
    linked_case_title: str | None
    link_type: str
    reason: str | None
    share_visibility: bool
    # "this" or "linked": which case is the main or root case. Null for a related link.
    primary_side: Literal["this", "linked"] | None
    # The main or root case itself (its ID). Null for a related link, and null when the caller cannot see that case.
    primary_case_id: str | None
    created_at: datetime


def _scope(actor: User) -> CaseScope:
    return CaseScope.from_permissions(actor.id, permissions_of(actor))


def _link_response(link: CaseLink) -> CaseLinkResponse:
    return CaseLinkResponse(
        linked_case_id=link.linked_case_id,
        linked_case_title=link.linked_case_title,
        link_type=link.link_type,
        reason=link.reason,
        share_visibility=link.share_visibility,
        primary_side=link.primary_side,
        primary_case_id=link.primary_case_id,
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


@case_links_router.get(
    "/api/v1/cases/{case_id}/links", response_model=list[CaseLinkResponse]
)
def list_case_links(
    case_id: uuid.UUID,
    actor: User = Depends(require_any_permission(*CASE_READ_PERMISSIONS_FOR_RECORDS)),
) -> list[CaseLinkResponse]:
    links = list_links(str(case_id), _scope(actor))
    if links is None:
        raise CASE_NOT_FOUND
    return [_link_response(link) for link in links]


@case_links_router.post(
    "/api/v1/cases/{case_id}/links", status_code=201, response_model=CaseLinkResponse
)
def add_case_link(
    case_id: uuid.UUID,
    body: CaseLinkRequest,
    request: Request,
    actor: User = Depends(require_permission("cases:triage")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CaseLinkResponse:
    primary = str(body.primary_case_id) if body.primary_case_id else None
    error = request_error(
        str(case_id), str(body.linked_case_id), body.link_type, body.reason, primary
    )
    if error is not None:
        raise HTTPException(status_code=422, detail=error)
    key = checked_key(idempotency_key)
    scope_key = key_scope(
        actor.id,
        "POST",
        LINKS_ROUTE,
        key,
        request.url.path,
        body.model_dump(mode="json"),
    )
    outcome, link, blockers = add_link(
        str(case_id),
        str(body.linked_case_id),
        actor.id,
        _scope(actor),
        link_type=body.link_type,
        reason=body.reason,
        primary_case_id=primary,
        share_visibility=body.share_visibility,
        idempotency=scope_key,
        render=lambda created: _link_response(created).model_dump(mode="json"),
    )
    if outcome is LinkOutcome.BLOCKED:
        raise HTTPException(
            status_code=409,
            detail="duplicate case cannot be closed: " + "; ".join(blockers),
        )
    _raise_for(outcome)
    assert link is not None
    return _link_response(link)


@case_links_router.delete(
    "/api/v1/cases/{case_id}/links/{linked_case_id}", status_code=204
)
def remove_case_link(
    case_id: uuid.UUID,
    linked_case_id: uuid.UUID,
    actor: User = Depends(require_permission("cases:triage")),
) -> Response:
    outcome = remove_link(str(case_id), str(linked_case_id), actor.id, _scope(actor))
    _raise_for(outcome)
    return Response(status_code=204)
