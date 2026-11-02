"""Redaction: a derived evidence item made from a text original, with a manifest, claim assessments, and custody.

A redaction never changes the original. It creates a new evidence item (its own ID and its own encrypted object) whose
content is the original with the removed lines replaced by a marker. The manifest records each removed line range and the
reason for it, never the removed text. The derived item carries a TLP label (safe_kit.tlp).

Claims: a review layer that cites the original is a claim. The derived item keeps less content than the original, so
every such claim is assessed on the derived item as insufficient_evidence. The system does not parse what a claim says,
so the assessment is made for every claim that cites the original. The review layers themselves are not changed.

Custody: one "redacted" event on the original (the derived ID and the redaction ID), one "uploaded" event on the derived
item, and one audit row, all in the same transaction as the rows that describe the redaction.

Writing follows the upload pattern of safe_kit.evidence, with an idempotency record added:
  A. with an Idempotency-Key, look up the key (a replay returns the stored answer);
  B. encrypt the derived content, insert the derived row as pending, write and verify the object (no transaction is held
     across the storage calls);
  C. in one transaction: look up the key again, check the case is still visible, make the derived row available, and write
     the manifest, the custody events, the claim assessments, the audit row, and the stored response.
If C finds a replay, a refusal, or an expired row, the pending derived row is marked failed, as reconciliation would.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from psycopg.types.json import Jsonb

from safe_kit import audit, evidence, tlp
from safe_kit.cases import CaseScope, is_visible_on
from safe_kit.db import connect
from safe_kit.evidence_crypto import seal, sha256_hex
from safe_kit.evidence_keys import KeyProvider
from safe_kit.evidence_storage import EvidenceStorage
from safe_kit.idempotency import KeyLookup, KeyScope, StoredResponse, lock_scope, lookup, store

REPLACEMENT = "[REDACTED]"
MAX_REMOVED_RANGES = 50
REASON_LIMIT = 500
REDACTABLE_MEDIA_TYPE = "text/plain"
CLAIM_REASON = (
    "The claim cites the original. The redaction removed content from the derived item, so the claim has no support "
    "on the derived item."
)


@dataclass(frozen=True)
class RemovedRange:
    """One inclusive range of lines, 1-based, and the reason it is removed."""

    from_line: int
    to_line: int
    reason: str


class RedactionRefused(Exception):
    """The redaction is refused. code is one of the keys of REFUSAL_DETAILS in evidence_api."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ClaimAssessment:
    review_layer_id: str
    round_id: str
    layer: str
    outcome: str
    reason: str


@dataclass(frozen=True)
class Derivation:
    redaction_id: str
    original_evidence_id: str
    derived: evidence.EvidenceItem
    manifest: dict[str, Any]
    claims: list[ClaimAssessment]


class Outcome(Enum):
    CREATED = "created"
    REPLAY = "replay"  # the same key and fingerprint: the stored response is returned
    KEY_REUSED = "key_reused"  # the same key with another request: refused, nothing is returned


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    derivation: Derivation | None = None
    stored: StoredResponse | None = None


def redact_text(text: str, removed: Sequence[RemovedRange]) -> tuple[str, list[dict[str, Any]], int]:
    """Replace each removed line with REPLACEMENT. Returns the redacted text, the manifest entries, and the line count.

    Line numbers are 1-based and count the lines of the text as the reader sees them. A final newline ends the last line
    and is kept. Every removed line keeps its place, so later line numbers do not move.
    """
    if not removed or len(removed) > MAX_REMOVED_RANGES:
        raise RedactionRefused("invalid_removal")
    trailing = "\n" if text.endswith("\n") else ""
    lines = (text[:-1] if trailing else text).split("\n")
    entries: list[dict[str, Any]] = []
    previous_end = 0
    for item in sorted(removed, key=lambda r: r.from_line):
        if item.to_line < item.from_line or item.to_line > len(lines) or item.from_line <= previous_end:
            raise RedactionRefused("invalid_removal")
        previous_end = item.to_line
        for number in range(item.from_line, item.to_line + 1):
            lines[number - 1] = REPLACEMENT
        entries.append(
            {
                "from_line": item.from_line,
                "to_line": item.to_line,
                "lines": item.to_line - item.from_line + 1,
                "reason": item.reason,
            }
        )
    return "\n".join(lines) + trailing, entries, len(lines)


def _check_original(original: evidence.StoredEvidence, label: str) -> None:
    if original.status != evidence.AVAILABLE:
        raise RedactionRefused("not_available")
    if original.media_type != REDACTABLE_MEDIA_TYPE:
        raise RedactionRefused("unsupported_media_type")
    if not tlp.at_least_as_restrictive(label, original.tlp_label):
        raise RedactionRefused("label_less_restrictive")


def _insert_pending_derived(
    original: evidence.StoredEvidence,
    actor_id: str,
    name: str,
    size_bytes: int,
    storage_key: str,
    key_id: str,
    wrapped_data_key: bytes,
    nonce: bytes,
    label: str,
    scope: CaseScope,
) -> str:
    with connect() as conn:
        # The same visibility rule as the API check, on this connection (see evidence._insert_pending).
        if not is_visible_on(conn, original.case_id, scope):
            raise RedactionRefused("not_found")
        row = conn.execute(
            """
            INSERT INTO evidence_items
                (case_id, uploaded_by, original_filename, media_type, size_bytes, storage_key,
                 key_id, wrapped_data_key, nonce, status, tlp_label)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
            RETURNING id::text
            """,
            (
                original.case_id, actor_id, name, REDACTABLE_MEDIA_TYPE, size_bytes, storage_key,
                key_id, wrapped_data_key, nonce, label,
            ),
        ).fetchone()
    return row[0]


def _claims_citing(conn, case_id: str, evidence_id: str) -> list[tuple[str, str, str]]:
    """Set review layers of the case that cite the evidence item: (layer ID, round ID, layer name)."""
    rows = conn.execute(
        """
        SELECT rl.id::text, rl.round_id::text, rl.layer
        FROM review_layers rl
        JOIN review_rounds rr ON rr.id = rl.round_id AND NOT rr.is_deleted
        WHERE rr.case_id = %s AND NOT rl.is_deleted AND rl.outcome IS NOT NULL
          AND %s::uuid = ANY(rl.evidence_ids)
        ORDER BY rr.opened_at, rl.layer, rl.id
        """,
        (case_id, evidence_id),
    ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def _finalize(
    conn,
    original: evidence.StoredEvidence,
    derived_id: str,
    actor_id: str,
    removed_manifest: dict[str, Any],
    checksums: tuple[str, str],
    render: Callable[[Derivation], Any] | None,
    idempotency: KeyScope | None,
) -> Derivation | None:
    """Make the derived row available and write everything that describes the redaction. Runs in the caller's transaction.

    Returns None when the pending row is no longer pending (reconciliation marked it failed first).
    """
    row = conn.execute(
        f"""
        UPDATE evidence_items SET status = 'available', sha256_plaintext = %s, sha256_ciphertext = %s
        WHERE id = %s AND status = 'pending' AND NOT is_deleted
        RETURNING {evidence._ITEM_COLUMNS}
        """,
        (checksums[0], checksums[1], derived_id),
    ).fetchone()
    if row is None:
        return None
    derived = evidence._to_item(row)
    evidence._custody(conn, derived_id, actor_id, evidence.UPLOADED, {"redacted_from": original.id})
    redaction_id = conn.execute(
        """
        INSERT INTO evidence_redactions (original_evidence_id, derived_evidence_id, case_id, actor_id, manifest)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id::text
        """,
        (original.id, derived_id, original.case_id, actor_id, Jsonb(removed_manifest)),
    ).fetchone()[0]
    evidence._custody(
        conn, original.id, actor_id, evidence.REDACTED, {"derived_evidence_id": derived_id, "redaction_id": redaction_id}
    )
    claims: list[ClaimAssessment] = []
    for layer_id, round_id, layer in _claims_citing(conn, original.case_id, original.id):
        conn.execute(
            """
            INSERT INTO evidence_claim_assessments
                (redaction_id, derived_evidence_id, review_layer_id, outcome, reason)
            VALUES (%s, %s, %s, 'insufficient_evidence', %s)
            """,
            (redaction_id, derived_id, layer_id, CLAIM_REASON),
        )
        claims.append(ClaimAssessment(layer_id, round_id, layer, "insufficient_evidence", CLAIM_REASON))
    audit.record(
        conn,
        audit.EVIDENCE_REDACTED,
        evidence.EVIDENCE_TARGET,
        derived_id,
        actor_id=actor_id,
        details={
            "case_id": original.case_id,
            "original_evidence_id": original.id,
            "redaction_id": redaction_id,
            "claims_assessed": len(claims),
        },
    )
    derivation = Derivation(
        redaction_id=redaction_id,
        original_evidence_id=original.id,
        derived=derived,
        manifest=removed_manifest,
        claims=claims,
    )
    if idempotency is not None and render is not None:
        store(conn, idempotency, StoredResponse(status_code=201, body=render(derivation)))
    return derivation


def create_derivative(
    original: evidence.StoredEvidence,
    actor_id: str,
    scope: CaseScope,
    removed: Sequence[RemovedRange],
    label: str,
    storage: EvidenceStorage,
    provider: KeyProvider,
    idempotency: KeyScope | None = None,
    render: Callable[[Derivation], Any] | None = None,
) -> Result:
    """Create the redacted derived item of a text original. The caller has checked RBAC, the case ACL, and the TLP
    check for the original. Refusals raise RedactionRefused. A storage or key failure raises StorageError or
    KeyProviderError, after the pending row is marked failed (see the module docstring)."""
    if idempotency is not None:
        with connect() as conn:
            lock_scope(conn, idempotency)
            found, replayed = lookup(conn, idempotency)
        if found is KeyLookup.REPLAY:
            return Result(Outcome.REPLAY, stored=replayed)
        if found is KeyLookup.REUSED:
            return Result(Outcome.KEY_REUSED)

    _check_original(original, label)
    try:
        content = evidence.read_original(original, storage, provider)
    except evidence.VerificationFailed as failure:
        evidence.record_failure(original.id, actor_id, failure.reason)
        raise RedactionRefused("integrity_failed") from None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise RedactionRefused("unsupported_media_type") from None
    redacted_text, entries, line_count = redact_text(text, removed)
    redacted = redacted_text.encode("utf-8")
    manifest: dict[str, Any] = {
        "method": "replace_lines",
        "replacement": REPLACEMENT,
        "source_sha256": original.sha256_plaintext,
        "tlp_label": label,
        "lines_total": line_count,
        "removed_lines": sum(entry["lines"] for entry in entries),
        "removed": entries,
    }

    name = evidence.sanitize_filename("redacted-" + original.original_filename)
    storage_key = str(uuid.uuid4())
    sealed = seal(redacted, storage_key, provider)
    derived_id = _insert_pending_derived(
        original, actor_id, name, len(redacted), storage_key, sealed.key_id,
        sealed.wrapped_data_key, sealed.nonce, label, scope,
    )
    plaintext_digest = sha256_hex(redacted)
    ciphertext_digest = sha256_hex(sealed.ciphertext)
    try:
        storage.put(storage_key, sealed.ciphertext)
    except Exception:
        evidence._mark_failed(derived_id, actor_id, "storage_write_failed")
        raise
    expected = evidence.StoredEvidence(
        id=derived_id,
        case_id=original.case_id,
        original_filename=name,
        media_type=REDACTABLE_MEDIA_TYPE,
        sha256_plaintext=plaintext_digest,
        sha256_ciphertext=ciphertext_digest,
        storage_key=storage_key,
        key_id=sealed.key_id,
        wrapped_data_key=sealed.wrapped_data_key,
        nonce=sealed.nonce,
        status=evidence.PENDING,
        tlp_label=label,
    )
    try:
        evidence.read_original(expected, storage, provider)
    except evidence.VerificationFailed as failure:
        evidence._mark_failed(derived_id, actor_id, failure.reason)
        raise evidence.StoreVerificationFailed(failure.reason) from None

    # Phase C: the one transaction that makes the derived item available and writes the records.
    failure_reason: str | None = None
    replay_result: Result | None = None
    derivation: Derivation | None = None
    with connect() as conn:
        if idempotency is not None:
            lock_scope(conn, idempotency)
            found, replayed = lookup(conn, idempotency)
        else:
            found, replayed = KeyLookup.NEW, None
        if found is KeyLookup.REPLAY:
            failure_reason = "superseded_by_replay"
            replay_result = Result(Outcome.REPLAY, stored=replayed)
        elif found is KeyLookup.REUSED:
            failure_reason = "superseded_by_key_reuse"
            replay_result = Result(Outcome.KEY_REUSED)
        elif not is_visible_on(conn, original.case_id, scope):
            failure_reason = "case_not_visible"
        else:
            derivation = _finalize(
                conn, original, derived_id, actor_id, manifest, (plaintext_digest, ciphertext_digest),
                render, idempotency,
            )
            if derivation is None:
                raise RedactionRefused("expired")
    if derivation is not None:
        return Result(Outcome.CREATED, derivation=derivation)
    evidence._mark_failed(derived_id, actor_id, failure_reason or "not_recorded")
    if replay_result is not None:
        return replay_result
    raise RedactionRefused("not_found")


def list_derivatives(original_id: str, recipient: tlp.Recipient) -> list[evidence.EvidenceItem]:
    """The derived items of an original that the caller may see under the TLP rule, oldest first.

    Derived items the rule hides are left out, as list_evidence leaves them out of a case page. The caller checks the
    case ACL and the TLP rule for the original before calling this.
    """
    audience, audience_params = tlp.visibility_sql(recipient)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {evidence._ITEM_COLUMNS} FROM evidence_items e
            WHERE e.id IN (
                SELECT r.derived_evidence_id FROM evidence_redactions r
                WHERE r.original_evidence_id = %s AND NOT r.is_deleted
            )
              AND NOT e.is_deleted AND {audience}
            ORDER BY e.created_at, e.id
            """,
            (original_id, *audience_params),
        ).fetchall()
    return [evidence._to_item(row) for row in rows]


def load_derivation(derived_id: str) -> Derivation | None:
    """The redaction that produced a derived item, with its manifest and claim assessments. No access check here:
    the caller checks RBAC, the case ACL, and the TLP rule for the derived item first."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id::text, original_evidence_id::text, manifest
            FROM evidence_redactions
            WHERE derived_evidence_id = %s AND NOT is_deleted
            """,
            (derived_id,),
        ).fetchone()
        if row is None:
            return None
        item = conn.execute(
            f"SELECT {evidence._ITEM_COLUMNS} FROM evidence_items WHERE id = %s AND NOT is_deleted",
            (derived_id,),
        ).fetchone()
        claim_rows = conn.execute(
            """
            SELECT a.review_layer_id::text, rl.round_id::text, rl.layer, a.outcome, a.reason
            FROM evidence_claim_assessments a
            JOIN review_layers rl ON rl.id = a.review_layer_id
            WHERE a.derived_evidence_id = %s AND NOT a.is_deleted
            ORDER BY a.created_at, a.id
            """,
            (derived_id,),
        ).fetchall()
    if item is None:
        return None
    return Derivation(
        redaction_id=row[0],
        original_evidence_id=row[1],
        derived=evidence._to_item(item),
        manifest=row[2],
        claims=[ClaimAssessment(*claim) for claim in claim_rows],
    )
