"""Conflicts, recusal and independent appeals on review rounds (plans/f0/api-draft.md section 2.3, T09).

Rules:
- Conflict: a reviewer declares their own conflict on a round. The reviewer's current entry decides. While it is a
  declaration, the reviewer cannot set a layer on the round (reviews.set_layer), cannot be named as a replacement by a
  recusal, and cannot be named as the independent reviewer of an appeal on the round.
- Recusal: an assigned reviewer withdraws from a round and names a replacement. A withdrawn reviewer cannot set a layer
  on the round. The replacement must be an active, assigned user who holds reviews:write, has no declared conflict on
  the round, and has not withdrawn from it.
- Appeal: any user who can see the case may file an appeal on one of its rounds. The system names the independent
  reviewer; the filer cannot choose one. An eligible reviewer is an active, assigned user who holds reviews:write, has
  not taken part in the round (opened it, set a layer, withdrawn, or been named as a replacement), has no declared
  conflict on it, and is not the appellant. The choice is the eligible user with the fewest open appeals, then the
  lowest user ID. If nobody is eligible, the appeal is refused and nothing is stored. No party can block an appeal:
  nothing asks the original reviewer or a stakeholder for an approval, and the product does not decide the appeal.
- Corrections: a declaration or recusal changes only by a new row that names the row it supersedes
  (supersedes_id). Old rows are never edited; migration 0033 rejects an update of their content.

Every write runs in one transaction with the case row locked, and records one audit row. Free text is never written
to the audit log, only its length.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from safe_kit import audit
from safe_kit.cases import (
    CaseScope,
    _is_assigned,
    _lock_visible_case,
    _text_marker,
    is_visible_on,
)
from safe_kit.db import connect
from safe_kit.idempotency import (
    KeyLookup,
    KeyScope,
    StoredResponse,
    lock_scope,
    lookup,
    store,
)

REASON_LIMIT = 1000
GROUNDS_LIMIT = 2000
REVIEW_PERMISSION = "reviews:write"
DECLARED = "declared"
WITHDRAWN = "withdrawn"


class IndependenceOutcome(Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    CLOSED = "case_closed"
    NOT_ASSIGNED = "not_assigned"
    ALREADY_DECLARED = "already_declared"
    NO_DECLARED_CONFLICT = "no_declared_conflict"
    ALREADY_RECUSED = "already_recused"
    SUPERSEDES_MISMATCH = "supersedes_mismatch"
    REPLACEMENT_IS_SELF = "replacement_is_self"
    REPLACEMENT_NOT_ELIGIBLE = "replacement_not_eligible"
    REPLACEMENT_CONFLICTED = "replacement_conflicted"
    REPLACEMENT_WITHDRAWN = "replacement_withdrawn"
    NO_ELIGIBLE_REVIEWER = "no_eligible_reviewer"
    REPLAY = "replay"
    KEY_REUSED = "key_reused"


@dataclass(frozen=True)
class ConflictEntry:
    id: str
    round_id: str
    reviewer_id: str
    kind: str
    # The full text. The API layer decides who may see it (the reviewer concerned, or cases:read_all).
    description: str | None
    supersedes_id: str | None
    created_at: datetime
    # True for the newest entry of the reviewer on the round: the one that decides.
    current: bool


@dataclass(frozen=True)
class RecusalEntry:
    id: str
    round_id: str
    reviewer_id: str
    replacement_id: str
    reason: str | None
    supersedes_id: str | None
    created_at: datetime
    current: bool


@dataclass(frozen=True)
class AppealEntry:
    id: str
    round_id: str
    filed_by: str
    independent_reviewer_id: str
    grounds: str
    created_at: datetime


def _visible_round_case(conn, round_id: str, scope: CaseScope) -> str | None:
    """The case of a round that the scope may see, or None when the round is missing or its case is not visible."""
    row = conn.execute(
        "SELECT case_id::text FROM review_rounds WHERE id = %s AND NOT is_deleted",
        (round_id,),
    ).fetchone()
    if row is None or not is_visible_on(conn, row[0], scope):
        return None
    return row[0]


def _replay(
    conn, idempotency: KeyScope | None
) -> tuple[IndependenceOutcome, StoredResponse | None] | None:
    """Take the scope lock and look up the key. Returns the outcome for a replay or a reused key, None for a new request."""
    if idempotency is None:
        return None
    lock_scope(conn, idempotency)
    found, stored = lookup(conn, idempotency)
    if found is KeyLookup.REPLAY:
        return IndependenceOutcome.REPLAY, stored
    if found is KeyLookup.REUSED:
        return IndependenceOutcome.KEY_REUSED, None
    return None


def _select_conflicts(conn, where_sql: str, params: tuple) -> list[ConflictEntry]:
    rows = conn.execute(
        f"""
        SELECT c.id::text, c.round_id::text, c.reviewer_id::text, c.kind, c.description, c.supersedes_id::text,
               c.created_at,
               c.seq = (
                   SELECT max(x.seq) FROM review_conflicts x
                   WHERE x.round_id = c.round_id AND x.reviewer_id = c.reviewer_id AND NOT x.is_deleted
               )
        FROM review_conflicts c
        WHERE NOT c.is_deleted AND {where_sql}
        ORDER BY c.seq DESC
        """,
        params,
    ).fetchall()
    return [ConflictEntry(*row) for row in rows]


def _select_recusals(conn, where_sql: str, params: tuple) -> list[RecusalEntry]:
    rows = conn.execute(
        f"""
        SELECT r.id::text, r.round_id::text, r.reviewer_id::text, r.replacement_id::text, r.reason,
               r.supersedes_id::text, r.created_at,
               r.seq = (
                   SELECT max(x.seq) FROM review_recusals x
                   WHERE x.round_id = r.round_id AND x.reviewer_id = r.reviewer_id AND NOT x.is_deleted
               )
        FROM review_recusals r
        WHERE NOT r.is_deleted AND {where_sql}
        ORDER BY r.seq DESC
        """,
        params,
    ).fetchall()
    return [RecusalEntry(*row) for row in rows]


def _select_appeals(conn, where_sql: str, params: tuple) -> list[AppealEntry]:
    rows = conn.execute(
        f"""
        SELECT a.id::text, a.round_id::text, a.filed_by::text, a.independent_reviewer_id::text, a.grounds, a.created_at
        FROM review_appeals a
        WHERE NOT a.is_deleted AND {where_sql}
        ORDER BY a.seq DESC
        """,
        params,
    ).fetchall()
    return [AppealEntry(*row) for row in rows]


def _conflict_head(conn, round_id: str, user_id: str) -> tuple[str, str] | None:
    """(id, kind) of the reviewer's newest conflict entry on the round, or None."""
    row = conn.execute(
        """
        SELECT id::text, kind FROM review_conflicts
        WHERE round_id = %s AND reviewer_id = %s AND NOT is_deleted
        ORDER BY seq DESC LIMIT 1
        """,
        (round_id, user_id),
    ).fetchone()
    return None if row is None else (row[0], row[1])


def _recusal_head(conn, round_id: str, user_id: str) -> str | None:
    """The id of the reviewer's newest recusal on the round, or None."""
    row = conn.execute(
        """
        SELECT id::text FROM review_recusals
        WHERE round_id = %s AND reviewer_id = %s AND NOT is_deleted
        ORDER BY seq DESC LIMIT 1
        """,
        (round_id, user_id),
    ).fetchone()
    return None if row is None else row[0]


def has_declared_conflict(conn, round_id: str, user_id: str) -> bool:
    """True when the user's current conflict entry on the round is a declaration. Runs on the caller's connection."""
    head = _conflict_head(conn, round_id, user_id)
    return head is not None and head[1] == DECLARED


def has_withdrawn(conn, round_id: str, user_id: str) -> bool:
    """True when the user has withdrawn from the round. Runs on the caller's connection."""
    return _recusal_head(conn, round_id, user_id) is not None


def _eligible_reviewer(conn, case_id: str, user_id: str) -> bool:
    """An active user, not a reporter, whose role holds reviews:write, and who is assigned to the case."""
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM users u
            JOIN role_permissions rp ON rp.role = u.role AND rp.permission = %s AND NOT rp.is_deleted
            JOIN permissions p ON p.name = rp.permission AND NOT p.is_deleted
            WHERE u.id = %s AND NOT u.is_deleted AND u.is_active AND u.role <> 'reporter'
        )
        """,
        (REVIEW_PERMISSION, user_id),
    ).fetchone()
    return bool(row[0]) and _is_assigned(conn, case_id, user_id)


def _took_part(conn, round_id: str, user_id: str) -> bool:
    """True when the user opened the round, set a layer on it, withdrew from it, or was named as a replacement."""
    row = conn.execute(
        """
        SELECT EXISTS (SELECT 1 FROM review_rounds WHERE id = %s AND opened_by = %s AND NOT is_deleted)
            OR EXISTS (SELECT 1 FROM review_layers WHERE round_id = %s AND set_by = %s AND NOT is_deleted)
            OR EXISTS (
                SELECT 1 FROM review_recusals
                WHERE round_id = %s AND (reviewer_id = %s OR replacement_id = %s) AND NOT is_deleted
            )
        """,
        (round_id, user_id, round_id, user_id, round_id, user_id, user_id),
    ).fetchone()
    return bool(row[0])


def declare_conflict(
    round_id: str,
    kind: str,
    description: str | None,
    supersedes_id: str | None,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[ConflictEntry], Any] | None = None,
) -> tuple[IndependenceOutcome, ConflictEntry | None, StoredResponse | None]:
    """The caller declares (or withdraws, or corrects) their own conflict on a round. The caller must be assigned.

    supersedes_id must name the caller's current entry on the round, or be null when there is none. A declaration
    while one is already current needs supersedes_id (a correction). A withdrawal needs a current declaration.
    """
    with connect() as conn:
        replayed = _replay(conn, idempotency)
        if replayed is not None:
            outcome, stored = replayed
            return outcome, None, stored
        case_id = _visible_round_case(conn, round_id, scope)
        if case_id is None:
            return IndependenceOutcome.NOT_FOUND, None, None
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return IndependenceOutcome.NOT_FOUND, None, None
        if locked[0] == "closed":
            return IndependenceOutcome.CLOSED, None, None
        if not _is_assigned(conn, case_id, scope.user_id):
            return IndependenceOutcome.NOT_ASSIGNED, None, None
        head = _conflict_head(conn, round_id, scope.user_id)
        if (
            kind == DECLARED
            and head is not None
            and head[1] == DECLARED
            and supersedes_id is None
        ):
            return IndependenceOutcome.ALREADY_DECLARED, None, None
        if kind == WITHDRAWN and (head is None or head[1] != DECLARED):
            return IndependenceOutcome.NO_DECLARED_CONFLICT, None, None
        if supersedes_id != (head[0] if head is not None else None):
            return IndependenceOutcome.SUPERSEDES_MISMATCH, None, None
        entry_id = conn.execute(
            """
            INSERT INTO review_conflicts (round_id, reviewer_id, kind, description, supersedes_id)
            VALUES (%s, %s, %s, %s, %s) RETURNING id::text
            """,
            (round_id, scope.user_id, kind, description, supersedes_id),
        ).fetchone()[0]
        audit.record(
            conn,
            audit.CASE_REVIEW_CONFLICT_RECORDED,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "review_id": round_id,
                "conflict_id": entry_id,
                "kind": kind,
                "description": _text_marker(description),
                "supersedes_id": supersedes_id,
            },
        )
        created = _select_conflicts(conn, "c.id = %s", (entry_id,))[0]
        if idempotency is not None and render is not None:
            store(
                conn, idempotency, StoredResponse(status_code=201, body=render(created))
            )
    return IndependenceOutcome.OK, created, None


def list_conflicts(round_id: str, scope: CaseScope) -> list[ConflictEntry] | None:
    """Every conflict entry of a round, newest first, or None when the round is not visible to the scope."""
    with connect() as conn:
        if _visible_round_case(conn, round_id, scope) is None:
            return None
        return _select_conflicts(conn, "c.round_id = %s", (round_id,))


def record_recusal(
    round_id: str,
    replacement_id: str,
    reason: str | None,
    supersedes_id: str | None,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[RecusalEntry], Any] | None = None,
) -> tuple[IndependenceOutcome, RecusalEntry | None, StoredResponse | None]:
    """The caller, an assigned reviewer, withdraws from the round and names the replacement.

    supersedes_id must name the caller's current recusal on the round, or be null when there is none. A second
    recusal without supersedes_id is refused: a correction names the row it replaces.
    """
    with connect() as conn:
        replayed = _replay(conn, idempotency)
        if replayed is not None:
            outcome, stored = replayed
            return outcome, None, stored
        case_id = _visible_round_case(conn, round_id, scope)
        if case_id is None:
            return IndependenceOutcome.NOT_FOUND, None, None
        locked = _lock_visible_case(conn, case_id, scope)
        if locked is None:
            return IndependenceOutcome.NOT_FOUND, None, None
        if locked[0] == "closed":
            return IndependenceOutcome.CLOSED, None, None
        if not _is_assigned(conn, case_id, scope.user_id):
            return IndependenceOutcome.NOT_ASSIGNED, None, None
        head_id = _recusal_head(conn, round_id, scope.user_id)
        if supersedes_id is None and head_id is not None:
            return IndependenceOutcome.ALREADY_RECUSED, None, None
        if supersedes_id != head_id:
            return IndependenceOutcome.SUPERSEDES_MISMATCH, None, None
        if replacement_id == scope.user_id:
            return IndependenceOutcome.REPLACEMENT_IS_SELF, None, None
        if not _eligible_reviewer(conn, case_id, replacement_id):
            return IndependenceOutcome.REPLACEMENT_NOT_ELIGIBLE, None, None
        if has_declared_conflict(conn, round_id, replacement_id):
            return IndependenceOutcome.REPLACEMENT_CONFLICTED, None, None
        if has_withdrawn(conn, round_id, replacement_id):
            return IndependenceOutcome.REPLACEMENT_WITHDRAWN, None, None
        entry_id = conn.execute(
            """
            INSERT INTO review_recusals (round_id, reviewer_id, replacement_id, reason, supersedes_id)
            VALUES (%s, %s, %s, %s, %s) RETURNING id::text
            """,
            (round_id, scope.user_id, replacement_id, reason, supersedes_id),
        ).fetchone()[0]
        audit.record(
            conn,
            audit.CASE_REVIEW_RECUSAL_RECORDED,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "review_id": round_id,
                "recusal_id": entry_id,
                "replacement_id": replacement_id,
                "reason": _text_marker(reason),
                "supersedes_id": supersedes_id,
            },
        )
        created = _select_recusals(conn, "r.id = %s", (entry_id,))[0]
        if idempotency is not None and render is not None:
            store(
                conn, idempotency, StoredResponse(status_code=201, body=render(created))
            )
    return IndependenceOutcome.OK, created, None


def list_recusals(round_id: str, scope: CaseScope) -> list[RecusalEntry] | None:
    """Every recusal entry of a round, newest first, or None when the round is not visible to the scope."""
    with connect() as conn:
        if _visible_round_case(conn, round_id, scope) is None:
            return None
        return _select_recusals(conn, "r.round_id = %s", (round_id,))


def _choose_independent_reviewer(
    conn, round_id: str, case_id: str, filer_id: str
) -> str | None:
    """The reviewer the system names for an appeal, or None when nobody is eligible.

    Eligible users pass _eligible_reviewer (active, not a reporter, reviews:write, assigned to the case), are not the
    filer, took no part in the round, and hold no declared conflict on it. The choice is the eligible user with the
    fewest open appeals, then the lowest user ID. An open appeal is a non-deleted appeal that names the user as
    independent reviewer on a round of an open case. Runs on the caller's transaction, with the case row locked.
    """
    candidates = [
        row[0]
        for row in conn.execute(
            "SELECT id::text FROM users WHERE NOT is_deleted AND is_active ORDER BY id"
        ).fetchall()
    ]
    best: tuple[int, str] | None = None
    for user_id in candidates:
        if user_id == filer_id or not _eligible_reviewer(conn, case_id, user_id):
            continue
        if _took_part(conn, round_id, user_id) or has_declared_conflict(
            conn, round_id, user_id
        ):
            continue
        open_appeals = conn.execute(
            """
            SELECT count(*) FROM review_appeals a
            JOIN review_rounds r ON r.id = a.round_id AND NOT r.is_deleted
            JOIN cases c ON c.id = r.case_id AND NOT c.is_deleted
            WHERE a.independent_reviewer_id = %s AND NOT a.is_deleted AND c.status = 'open'
            """,
            (user_id,),
        ).fetchone()[0]
        if best is None or (open_appeals, user_id) < best:
            best = (open_appeals, user_id)
    return None if best is None else best[1]


def file_appeal(
    round_id: str,
    grounds: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[AppealEntry], Any] | None = None,
) -> tuple[IndependenceOutcome, AppealEntry | None, StoredResponse | None]:
    """File an appeal against a round. The system names the independent reviewer (_choose_independent_reviewer).

    Any caller who can see the case may file. The case state does not matter: an appeal is accepted on a closed case.
    When nobody is eligible the appeal is refused and nothing is written.
    """
    with connect() as conn:
        replayed = _replay(conn, idempotency)
        if replayed is not None:
            outcome, stored = replayed
            return outcome, None, stored
        case_id = _visible_round_case(conn, round_id, scope)
        if case_id is None:
            return IndependenceOutcome.NOT_FOUND, None, None
        # Locks the case so the eligibility checks and the choice see the state that the insert commits against.
        if _lock_visible_case(conn, case_id, scope) is None:
            return IndependenceOutcome.NOT_FOUND, None, None
        independent_reviewer_id = _choose_independent_reviewer(
            conn, round_id, case_id, scope.user_id
        )
        if independent_reviewer_id is None:
            return IndependenceOutcome.NO_ELIGIBLE_REVIEWER, None, None
        entry_id = conn.execute(
            """
            INSERT INTO review_appeals (round_id, filed_by, grounds, independent_reviewer_id)
            VALUES (%s, %s, %s, %s) RETURNING id::text
            """,
            (round_id, scope.user_id, grounds, independent_reviewer_id),
        ).fetchone()[0]
        audit.record(
            conn,
            audit.CASE_REVIEW_APPEAL_RECORDED,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "review_id": round_id,
                "appeal_id": entry_id,
                "independent_reviewer_id": independent_reviewer_id,
                "grounds": _text_marker(grounds),
            },
        )
        created = _select_appeals(conn, "a.id = %s", (entry_id,))[0]
        if idempotency is not None and render is not None:
            store(
                conn, idempotency, StoredResponse(status_code=201, body=render(created))
            )
    return IndependenceOutcome.OK, created, None


def list_appeals(round_id: str, scope: CaseScope) -> list[AppealEntry] | None:
    """Every appeal of a round, newest first, or None when the round is not visible to the scope."""
    with connect() as conn:
        if _visible_round_case(conn, round_id, scope) is None:
            return None
        return _select_appeals(conn, "a.round_id = %s", (round_id,))
