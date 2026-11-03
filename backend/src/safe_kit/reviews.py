"""Review rounds: one review of a case, with one entry per layer of the review template.

A round opens all eleven template layers as not set. A layer is set with an outcome, a reason (required for every
outcome except pass), and evidence references of the same case. Each change to a layer increments the round revision
in the same transaction. A write with a stale expected_revision changes nothing and is refused.

A round never has an overall result. The legacy entry of a round migrated from the F1 case review is kept as one
extra layer, so the history of the case reviews is not lost.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from safe_kit import audit
from safe_kit.cases import (
    CaseScope,
    _change,
    _is_assigned,
    _text_marker,
    is_visible_on,
    lock_visible_case,
)
from safe_kit.db import connect
from safe_kit.idempotency import (
    KeyLookup,
    KeyScope,
    StoredResponse,
    check,
    lock_scope,
    lookup,
    remember,
    store,
)
from safe_kit.review_independence import has_declared_conflict, has_withdrawn

# The eleven layers of the review template, in the order the plan lists them.
TEMPLATE_LAYERS: tuple[str, ...] = (
    "model",
    "instructions",
    "safeguards",
    "tools",
    "environment",
    "monitoring",
    "human-operations",
    "supply-chain",
    "serving-path",
    "authorization-and-commitment",
    "workflow-composition",
)
# The single layer entry of a round migrated from a case_reviews row (migration 0030).
LEGACY_LAYER = "legacy-case-review"

# The four outcomes of a layer. A layer that is not set has no outcome.
LAYER_OUTCOMES: frozenset[str] = frozenset(
    {"pass", "fail", "insufficient_evidence", "not_applicable"}
)

REASON_LIMIT = 1000
MAX_EVIDENCE_REFERENCES = 20


class RoundOutcome(Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    CLOSED = "case_closed"
    ROUND_CLOSED = "round_closed"
    NOT_ASSIGNED = "not_assigned"
    # The reviewer has a declared conflict on this round, or has withdrawn from it (review_independence).
    CONFLICT_DECLARED = "conflict_declared"
    WITHDRAWN = "withdrawn"
    REVISION_CONFLICT = "revision_conflict"
    REASON_REQUIRED = "reason_required"
    # A pass is supported only by evidence: it cites at least one item (plans/03, T06).
    EVIDENCE_REQUIRED = "evidence_required"
    UNKNOWN_EVIDENCE = "unknown_evidence"
    REPLAY = "replay"
    KEY_REUSED = "key_reused"


@dataclass(frozen=True)
class LayerEntry:
    layer: str
    outcome: str | None
    # The full reason. The API layer decides who may see it (cases:read_all), as it does for notes and close reasons.
    reason: str | None
    evidence_ids: tuple[str, ...]
    set_at: datetime | None


@dataclass(frozen=True)
class Round:
    id: str
    case_id: str
    status: str
    revision: int
    opened_at: datetime
    closed_at: datetime | None
    layers: tuple[LayerEntry, ...]


def _layer_position(layer: str) -> int:
    if layer == LEGACY_LAYER:
        return len(TEMPLATE_LAYERS)
    return TEMPLATE_LAYERS.index(layer)


def _load_rounds(conn, where_sql: str, params: tuple) -> list[Round]:
    """Rounds matching the condition, newest first, each with its layers in template order."""
    rows = conn.execute(
        f"""
        SELECT id::text, case_id::text, status, revision, opened_at, closed_at FROM review_rounds
        WHERE NOT is_deleted AND {where_sql}
        ORDER BY opened_at DESC, id DESC
        """,
        params,
    ).fetchall()
    if not rows:
        return []
    layer_rows = conn.execute(
        """
        SELECT round_id::text, layer, outcome, reason, evidence_ids::text[], set_at FROM review_layers
        WHERE round_id = ANY(%s::uuid[]) AND NOT is_deleted
        """,
        ([row[0] for row in rows],),
    ).fetchall()
    layers_by_round: dict[str, list[LayerEntry]] = defaultdict(list)
    for round_id, layer, outcome, reason, evidence_ids, set_at in layer_rows:
        layers_by_round[round_id].append(
            LayerEntry(
                layer=layer,
                outcome=outcome,
                reason=reason,
                evidence_ids=tuple(evidence_ids or ()),
                set_at=set_at,
            )
        )
    return [
        Round(
            id=row[0],
            case_id=row[1],
            status=row[2],
            revision=row[3],
            opened_at=row[4],
            closed_at=row[5],
            layers=tuple(
                sorted(
                    layers_by_round[row[0]],
                    key=lambda entry: _layer_position(entry.layer),
                )
            ),
        )
        for row in rows
    ]


def open_round(
    case_id: str,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Round], Any] | None = None,
) -> tuple[RoundOutcome, Round | None, StoredResponse | None]:
    """Open a round for an open case, with every template layer not set. Only an active assignee may open one.

    With an idempotency scope, a repeated request returns the stored response (REPLAY), and a key used with another
    request is refused (KEY_REUSED). A successful request stores render(round) under its key in the same transaction.
    """
    with connect() as conn:
        if idempotency is not None:
            lock_scope(conn, idempotency)
            found, stored = lookup(conn, idempotency)
            if found is KeyLookup.REPLAY:
                return RoundOutcome.REPLAY, None, stored
            if found is KeyLookup.REUSED:
                return RoundOutcome.KEY_REUSED, None, None
        locked = lock_visible_case(conn, case_id, scope)
        if locked is None:
            return RoundOutcome.NOT_FOUND, None, None
        if locked[0] == "closed":
            return RoundOutcome.CLOSED, None, None
        if not _is_assigned(conn, case_id, scope.user_id):
            return RoundOutcome.NOT_ASSIGNED, None, None
        round_id = conn.execute(
            "INSERT INTO review_rounds (case_id, opened_by) VALUES (%s, %s) RETURNING id::text",
            (case_id, scope.user_id),
        ).fetchone()[0]
        for layer in TEMPLATE_LAYERS:
            conn.execute(
                "INSERT INTO review_layers (round_id, layer) VALUES (%s, %s)",
                (round_id, layer),
            )
        audit.record(
            conn,
            audit.CASE_REVIEW_ROUND_OPENED,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={"review_id": _change(None, round_id)},
        )
        created = _load_rounds(conn, "id = %s", (round_id,))[0]
        if idempotency is not None and render is not None:
            store(
                conn, idempotency, StoredResponse(status_code=201, body=render(created))
            )
    return RoundOutcome.OK, created, None


def list_rounds(case_id: str, scope: CaseScope) -> list[Round] | None:
    """Rounds of a case, newest first, or None when the scope may not see the case."""
    with connect() as conn:
        if not is_visible_on(conn, case_id, scope):
            return None
        return _load_rounds(conn, "case_id = %s", (case_id,))


def get_round(round_id: str, scope: CaseScope) -> Round | None:
    """One round, or None when it does not exist or its case is not visible to the scope."""
    with connect() as conn:
        rounds = _load_rounds(conn, "id = %s", (round_id,))
        if not rounds or not is_visible_on(conn, rounds[0].case_id, scope):
            return None
        return rounds[0]


def set_layer(
    round_id: str,
    layer: str,
    outcome: str,
    reason: str | None,
    evidence_ids: list[str],
    expected_revision: int,
    scope: CaseScope,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Round], Any] | None = None,
) -> tuple[RoundOutcome, Round | None]:
    """Set one layer of a round.

    A pass must cite at least one evidence item of the case, and every cited item must be available (T06): otherwise
    the request is refused and nothing changes. The reason is optional for a pass and required for the other outcomes.

    A request that changes nothing stores nothing, writes no audit row, and keeps the revision. Otherwise the layer
    and the round revision change in one transaction. On a revision conflict the current round is returned, and the
    caller may read only its revision from it.

    With an idempotency scope the key is checked first (Replayed or KeyReused is raised), and a successful request,
    changed or not, stores render(round) in the same transaction.
    """
    if outcome not in LAYER_OUTCOMES or layer not in TEMPLATE_LAYERS:
        raise ValueError("unknown outcome or layer")
    if outcome != "pass" and reason is None:
        return RoundOutcome.REASON_REQUIRED, None
    unique_evidence = list(dict.fromkeys(evidence_ids))
    if outcome == "pass" and not unique_evidence:
        return RoundOutcome.EVIDENCE_REQUIRED, None
    with connect() as conn:
        check(conn, idempotency)
        head = conn.execute(
            "SELECT case_id::text FROM review_rounds WHERE id = %s AND NOT is_deleted",
            (round_id,),
        ).fetchone()
        if head is None:
            return RoundOutcome.NOT_FOUND, None
        case_id = head[0]
        locked = lock_visible_case(conn, case_id, scope)
        if locked is None:
            return RoundOutcome.NOT_FOUND, None
        if locked[0] == "closed":
            return RoundOutcome.CLOSED, None
        status, revision = conn.execute(
            "SELECT status, revision FROM review_rounds WHERE id = %s AND NOT is_deleted FOR UPDATE",
            (round_id,),
        ).fetchone()
        if status == "closed":
            return RoundOutcome.ROUND_CLOSED, None
        if not _is_assigned(conn, case_id, scope.user_id):
            return RoundOutcome.NOT_ASSIGNED, None
        # A declared conflict or a withdrawal blocks the layer, whatever the revision (T09).
        if has_declared_conflict(conn, round_id, scope.user_id):
            return RoundOutcome.CONFLICT_DECLARED, None
        if has_withdrawn(conn, round_id, scope.user_id):
            return RoundOutcome.WITHDRAWN, None
        if revision != expected_revision:
            return RoundOutcome.REVISION_CONFLICT, _load_rounds(
                conn, "id = %s", (round_id,)
            )[0]
        if unique_evidence:
            found = conn.execute(
                """
                SELECT count(*) FROM evidence_items
                WHERE case_id = %s AND NOT is_deleted AND status = 'available' AND id = ANY(%s::uuid[])
                """,
                (case_id, unique_evidence),
            ).fetchone()[0]
            if found != len(unique_evidence):
                return RoundOutcome.UNKNOWN_EVIDENCE, None
        current = conn.execute(
            """
            SELECT outcome, reason, evidence_ids::text[] FROM review_layers
            WHERE round_id = %s AND layer = %s AND NOT is_deleted
            FOR UPDATE
            """,
            (round_id, layer),
        ).fetchone()
        if current is None:
            return RoundOutcome.NOT_FOUND, None
        old_outcome, old_reason, old_evidence = current
        changes: dict[str, object] = {}
        if old_outcome != outcome:
            changes["outcome"] = _change(old_outcome, outcome)
        if old_reason != reason:
            changes["reason"] = _change(_text_marker(old_reason), _text_marker(reason))
        if sorted(old_evidence or []) != sorted(unique_evidence):
            changes["evidence_ids"] = _change(
                sorted(old_evidence or []), sorted(unique_evidence)
            )
        if not changes:
            unchanged = _load_rounds(conn, "id = %s", (round_id,))[0]
            if idempotency is not None:
                assert render is not None
                remember(conn, idempotency, 200, render(unchanged))
            return RoundOutcome.OK, unchanged
        conn.execute(
            """
            UPDATE review_layers
            SET outcome = %s, reason = %s, evidence_ids = %s::uuid[], set_by = %s, set_at = now()
            WHERE round_id = %s AND layer = %s AND NOT is_deleted
            """,
            (outcome, reason, unique_evidence, scope.user_id, round_id, layer),
        )
        new_revision = conn.execute(
            "UPDATE review_rounds SET revision = revision + 1 WHERE id = %s RETURNING revision",
            (round_id,),
        ).fetchone()[0]
        audit.record(
            conn,
            audit.CASE_REVIEW_LAYER_SET,
            "case",
            case_id,
            actor_id=scope.user_id,
            details={
                "review_id": round_id,
                "layer": layer,
                "revision": _change(revision, new_revision),
                **changes,
            },
        )
        updated = _load_rounds(conn, "id = %s", (round_id,))[0]
        if idempotency is not None:
            assert render is not None
            remember(conn, idempotency, 200, render(updated))
    return RoundOutcome.OK, updated
