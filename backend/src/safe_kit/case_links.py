"""Case links: a typed record that two cases concern the same incident (F1; types, reason and visibility F3 A9).

Every link has a type and a reason:
- duplicate: the duplicate case is closed when the link is made, and it points to its main case. The closure is refused
  while the duplicate has an open deadline task, an open review layer or an open external notice record (see
  cases.link_closure_blockers_on). Tasks are not moved to the main case, and evidence and records are kept.
- related: visibility and reference only. Neither case's status, tasks or deadlines change.
- root_sub: a user who can read the sub-case may read its root case through the link. Read only (CaseScope).

A link made with share_visibility (default off) lets the users who can see one case see the other case as well.

Visibility is decided only by CaseScope.visibility_sql. Reads use the link extensions there, and each extension is one
step: the other case is checked by the caller's own rights, never by another link. The changes in this module lock both
cases with the write rule (writing=True), so a case seen only through a link cannot be changed through it.

Reading a link: the other case's ID and title, and the reason, are returned only when the caller can read the other case.
The main or root case ID (primary_case_id) follows the same rule, except that the case the list is read from is always
visible to its own reader. A related link has no primary case.

Rules:
- Both cases must be visible for a change. A case the caller may not change is reported as not found, so a link never
  reveals a case the caller cannot see.
- A case cannot be linked to itself.
- A closed case, on either side, refuses the change.
- A pair is linked at most once, in either order. The unique index on the unordered pair backs up the check.
- A duplicate or root_sub link names its main or root case (primary); a related link names none. The primary is stored
  in case_links.primary_case_id.
- Removing a link is a soft delete. Each add and remove writes one audit row in the same transaction. The reason is
  kept as text in case_links; the audit row keeps only its length.

The two cases are locked before the rules are checked, in one statement ordered by ID, so concurrent changes to the
same pair run one after another.
"""

from collections.abc import Callable, Mapping
from typing import Any
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from safe_kit import audit, cases
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.idempotency import KeyScope, check, remember

DUPLICATE = "duplicate"
RELATED = "related"
ROOT_SUB = "root_sub"
LINK_TYPES: tuple[str, ...] = (DUPLICATE, RELATED, ROOT_SUB)
REASON_MAX_LENGTH = 500


class LinkOutcome(Enum):
    OK = "ok"
    # The source or the linked case is missing, deleted or not visible to the caller for a change.
    NOT_FOUND = "not_found"
    SAME_CASE = "same_case"
    CLOSED = "closed"
    DUPLICATE = "duplicate"
    LINK_NOT_FOUND = "link_not_found"
    # The duplicate cannot be closed by the link yet. Nothing was changed; the blockers say why.
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CaseLink:
    """One link as seen from case_id.

    linked_case_id, linked_case_title and reason are None when the caller cannot see the other case. The reason is free
    text, so it is given only to a caller who can read the other case.

    primary_side says which case is the main case of a duplicate or the root case of a root_sub link: "this" for case_id,
    "linked" for the other case (named even when the caller cannot see it), None for a related link. primary_case_id is
    that case's ID, given when the caller can see it (the case itself, or the other case when it is visible).
    """

    case_id: str
    linked_case_id: str | None
    linked_case_title: str | None
    created_at: datetime
    link_type: str = RELATED
    reason: str | None = None
    share_visibility: bool = False
    primary_side: str | None = None
    primary_case_id: str | None = None


def request_error(
    case_id: str,
    linked_case_id: str,
    link_type: str,
    reason: str,
    primary_case_id: str | None,
) -> str | None:
    """The shape rules of a new link, checked before any database work. None when the request is well formed."""
    if link_type not in LINK_TYPES:
        return "link_type must be duplicate, related or root_sub"
    text = reason.strip()
    if not text or len(text) > REASON_MAX_LENGTH:
        return f"a reason is required, up to {REASON_MAX_LENGTH} characters"
    if link_type == RELATED:
        return "a related link has no main case" if primary_case_id else None
    if primary_case_id is None:
        return "a duplicate or root_sub link needs primary_case_id (the main or root case)"
    if primary_case_id not in (case_id, linked_case_id):
        return "primary_case_id must be one of the two cases"
    return None


def check_pair(
    case_id: str, linked_case_id: str, statuses: Mapping[str, str]
) -> LinkOutcome | None:
    """The rules shared by add and remove. statuses holds only the cases the caller may change, with their status.

    Returns None when the pair may be changed. The order of the checks decides which refusal a caller sees: a
    case the caller cannot see is always "not found" before any other refusal.
    """
    if case_id not in statuses:
        return LinkOutcome.NOT_FOUND
    if case_id == linked_case_id:
        return LinkOutcome.SAME_CASE
    if linked_case_id not in statuses:
        return LinkOutcome.NOT_FOUND
    if "closed" in (statuses[case_id], statuses[linked_case_id]):
        return LinkOutcome.CLOSED
    return None


def _lock_visible_pair(
    conn, case_id: str, linked_case_id: str, scope: CaseScope
) -> dict[str, tuple[str, str]]:
    """Lock the cases of the pair the scope may change, in ID order. Returns {id: (status, title)} for those."""
    visibility, params = scope.visibility_sql(writing=True)
    rows = conn.execute(
        f"SELECT id::text, status, title FROM cases WHERE id IN (%s, %s) AND NOT is_deleted AND {visibility} "
        "ORDER BY id FOR UPDATE",
        (case_id, linked_case_id, *params),
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def _statuses(locked: Mapping[str, tuple[str, str]]) -> dict[str, str]:
    return {case_id: status for case_id, (status, _title) in locked.items()}


def _pair_condition() -> str:
    # Matches the pair in either order. The parameters are (a, b, b, a).
    return "((case_id = %s AND linked_case_id = %s) OR (case_id = %s AND linked_case_id = %s))"


def _primary_side(case_id: str, primary_case_id: str | None) -> str | None:
    if primary_case_id is None:
        return None
    return "this" if primary_case_id == case_id else "linked"


def add_link(
    case_id: str,
    linked_case_id: str,
    actor: str,
    scope: CaseScope,
    *,
    link_type: str,
    reason: str,
    primary_case_id: str | None = None,
    share_visibility: bool = False,
    idempotency: KeyScope | None = None,
    render: Callable[[CaseLink], Any] | None = None,
) -> tuple[LinkOutcome, CaseLink | None, list[str]]:
    """Record a typed link between two open cases that may be changed by the caller, and not yet linked.

    A duplicate link also closes the duplicate (the case that is not primary) in the same transaction. If it cannot be
    closed, nothing is written and the blockers are returned. The request shape is checked by request_error first.

    With an idempotency scope the key is checked first, and a new link stores render(link) in the same transaction.
    """
    text = reason.strip()
    with connect() as conn:
        check(conn, idempotency)
        locked = _lock_visible_pair(conn, case_id, linked_case_id, scope)
        refusal = check_pair(case_id, linked_case_id, _statuses(locked))
        if refusal is not None:
            return refusal, None, []
        exists = conn.execute(
            f"SELECT 1 FROM case_links WHERE NOT is_deleted AND {_pair_condition()}",
            (case_id, linked_case_id, linked_case_id, case_id),
        ).fetchone()
        if exists is not None:
            return LinkOutcome.DUPLICATE, None, []
        closing: str | None = None
        if link_type == DUPLICATE:
            closing = linked_case_id if primary_case_id == case_id else case_id
            blockers = cases.link_closure_blockers_on(conn, closing)
            if blockers:
                return LinkOutcome.BLOCKED, None, blockers
        row = conn.execute(
            """
            INSERT INTO case_links (case_id, linked_case_id, created_by, link_type, reason, primary_case_id,
                                    share_visibility)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id::text, created_at
            """,
            (
                case_id,
                linked_case_id,
                actor,
                link_type,
                text,
                primary_case_id,
                share_visibility,
            ),
        ).fetchone()
        audit.record(
            conn,
            audit.CASE_LINK_ADD,
            "case",
            case_id,
            actor_id=actor,
            details={
                "link_id": row[0],
                "linked_case_id": linked_case_id,
                "link_type": link_type,
                "share_visibility": share_visibility,
                "reason": {"length": len(text)},
            },
        )
        if closing is not None:
            cases.close_locked(
                conn, closing, f"Duplicate of case {primary_case_id}: {text}", actor
            )
        title = locked[linked_case_id][1]
        link = CaseLink(
            case_id=case_id,
            linked_case_id=linked_case_id,
            linked_case_title=title,
            created_at=row[1],
            link_type=link_type,
            reason=text,
            share_visibility=share_visibility,
            primary_side=_primary_side(case_id, primary_case_id),
            primary_case_id=primary_case_id,
        )
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 201, render(link))
    return LinkOutcome.OK, link, []


def remove_link(
    case_id: str, linked_case_id: str, actor: str, scope: CaseScope
) -> LinkOutcome:
    """Soft delete the link between the two cases. The row is kept, so the history stays complete."""
    with connect() as conn:
        locked = _lock_visible_pair(conn, case_id, linked_case_id, scope)
        refusal = check_pair(case_id, linked_case_id, _statuses(locked))
        if refusal is not None:
            return refusal
        row = conn.execute(
            f"UPDATE case_links SET is_deleted = true, deleted_at = now() WHERE NOT is_deleted AND {_pair_condition()} "
            "RETURNING id::text",
            (case_id, linked_case_id, linked_case_id, case_id),
        ).fetchone()
        if row is None:
            return LinkOutcome.LINK_NOT_FOUND
        audit.record(
            conn,
            audit.CASE_LINK_REMOVE,
            "case",
            case_id,
            actor_id=actor,
            details={"link_id": row[0], "linked_case_id": linked_case_id},
        )
    return LinkOutcome.OK


def list_links(case_id: str, scope: CaseScope) -> list[CaseLink] | None:
    """Active links of the case, in the order they were made, or None when the scope may not see the case.

    A link is listed from either side of the pair. The other case's ID and title, the reason and the primary case ID are
    given only when the scope may see the other case (the primary case ID also when it is case_id itself). Read-only:
    it writes no audit row.
    """
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return None
        rows = conn.execute(
            """
            SELECT case_id::text, linked_case_id::text, created_at, link_type, reason, share_visibility,
                   primary_case_id::text
            FROM case_links
            WHERE NOT is_deleted AND (case_id = %s OR linked_case_id = %s)
            ORDER BY created_at, id
            """,
            (case_id, case_id),
        ).fetchall()
        others = list(
            dict.fromkeys(
                linked if source == case_id else source
                for source, linked, *_rest in rows
            )
        )
        titles: dict[str, str] = {}
        if others:
            visibility, params = scope.visibility_sql()
            placeholders = ", ".join(["%s"] * len(others))
            titles = dict(
                conn.execute(
                    f"SELECT id::text, title FROM cases WHERE id IN ({placeholders}) AND NOT is_deleted AND {visibility}",
                    (*others, *params),
                ).fetchall()
            )
    links: list[CaseLink] = []
    for source, linked, created_at, link_type, reason, shared, primary in rows:
        other = linked if source == case_id else source
        visible = other in titles
        # The primary is either this case (always visible here) or the other case.
        primary_visible = primary is not None and (primary == case_id or primary in titles)
        links.append(
            CaseLink(
                case_id=case_id,
                linked_case_id=other if visible else None,
                linked_case_title=titles.get(other),
                created_at=created_at,
                link_type=link_type,
                reason=reason if visible else None,
                share_visibility=shared,
                primary_side=_primary_side(case_id, primary),
                primary_case_id=primary if primary_visible else None,
            )
        )
    return links
