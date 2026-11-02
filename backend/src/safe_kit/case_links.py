"""Case links: a record that two cases concern the same incident (owner decision, F1).

A link is a record only. Linking changes nothing else: no task, deadline or status is merged or changed. Merging
coordination is F2.

Rules:
- Both cases must be visible to the caller (CaseScope, the same rule as cases.is_visible_on). A case the caller may not
  see is reported as not found, so a link never reveals a case the caller cannot see.
- A case cannot be linked to itself.
- A closed case, on either side, refuses the change.
- A pair is linked at most once, in either order. The unique index on the unordered pair backs up the check.
- Removing a link is a soft delete. Each add and remove writes one audit row in the same transaction.
- Free text: none.

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


class LinkOutcome(Enum):
    OK = "ok"
    # The source or the linked case is missing, deleted or not visible to the caller.
    NOT_FOUND = "not_found"
    SAME_CASE = "same_case"
    CLOSED = "closed"
    DUPLICATE = "duplicate"
    LINK_NOT_FOUND = "link_not_found"


@dataclass(frozen=True)
class CaseLink:
    """One link as seen from case_id. linked_case_id and linked_case_title are None when the caller cannot see the other case."""

    case_id: str
    linked_case_id: str | None
    linked_case_title: str | None
    created_at: datetime


def check_pair(case_id: str, linked_case_id: str, statuses: Mapping[str, str]) -> LinkOutcome | None:
    """The rules shared by add and remove. statuses holds only the cases the caller may see, with their status.

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


def _lock_visible_pair(conn, case_id: str, linked_case_id: str, scope: CaseScope) -> dict[str, tuple[str, str]]:
    """Lock the visible cases of the pair, in ID order, and return {id: (status, title)} for those the scope may see."""
    visibility, params = scope.visibility_sql()
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


def add_link(
    case_id: str,
    linked_case_id: str,
    actor: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[CaseLink], Any] | None = None,
) -> tuple[LinkOutcome, CaseLink | None]:
    """Record that the two cases concern the same incident. Both must be visible and open, and not yet linked.

    With an idempotency scope the key is checked first, and a new link stores render(link) in the same transaction.
    """
    with connect() as conn:
        check(conn, idempotency)
        locked = _lock_visible_pair(conn, case_id, linked_case_id, scope)
        refusal = check_pair(case_id, linked_case_id, _statuses(locked))
        if refusal is not None:
            return refusal, None
        exists = conn.execute(
            f"SELECT 1 FROM case_links WHERE NOT is_deleted AND {_pair_condition()}",
            (case_id, linked_case_id, linked_case_id, case_id),
        ).fetchone()
        if exists is not None:
            return LinkOutcome.DUPLICATE, None
        row = conn.execute(
            """
            INSERT INTO case_links (case_id, linked_case_id, created_by) VALUES (%s, %s, %s)
            RETURNING id::text, created_at
            """,
            (case_id, linked_case_id, actor),
        ).fetchone()
        audit.record(
            conn,
            audit.CASE_LINK_ADD,
            "case",
            case_id,
            actor_id=actor,
            details={"link_id": row[0], "linked_case_id": linked_case_id},
        )
        title = locked[linked_case_id][1]
        link = CaseLink(case_id, linked_case_id, title, row[1])
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 201, render(link))
    return LinkOutcome.OK, link


def remove_link(case_id: str, linked_case_id: str, actor: str, scope: CaseScope) -> LinkOutcome:
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

    A link is listed from either side of the pair. The other case's ID and title are given only when the scope may see it.
    Read-only: it writes no audit row.
    """
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return None
        rows = conn.execute(
            """
            SELECT case_id::text, linked_case_id::text, created_at FROM case_links
            WHERE NOT is_deleted AND (case_id = %s OR linked_case_id = %s)
            ORDER BY created_at, id
            """,
            (case_id, case_id),
        ).fetchall()
        others = list(dict.fromkeys(linked if source == case_id else source for source, linked, _created in rows))
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
    for source, linked, created_at in rows:
        other = linked if source == case_id else source
        visible = other in titles
        links.append(
            CaseLink(
                case_id=case_id,
                linked_case_id=other if visible else None,
                linked_case_title=titles.get(other),
                created_at=created_at,
            )
        )
    return links
