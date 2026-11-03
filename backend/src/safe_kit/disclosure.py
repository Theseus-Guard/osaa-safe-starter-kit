"""Disclosure packages: versioned packages of redacted derivatives, the preview, and the human approval (F2, package P6).

A package version holds only derivatives (see safe_kit.redaction): each item is a redacted derived evidence item of the
case, with its own ID, never an original. A version also holds the recipient, the purpose, the TLP audience, the template
it follows, a manifest, and a digest over its canonical content. Versions are insert-only (migration 0040). A changed
package is a new version.

Approval: a human approves one version. The approval names the recipient, the purpose, and the digest the approver
confirmed, and it must match the version. The approver also confirms the recipient reference (recipient_ref, an
identifier kept outside SAFE, migration 0049). A notice of the version must name that same reference. A preparer may
not approve their own version, and the rule is recorded on the
approval row (separation_rule). Approving a CLEAR package also records the release of its CLEAR items (tlp.py), in the
same transaction. A later version of the same package revokes an earlier approval by change: the approval counts only for
the latest version.

Release attempts (migration 0047): a release is a notice record (safe_kit.external_notices) or an export. Each one first
writes an 'attempted' row with open_release_attempt, in the same transaction, and then runs the release check. The
check requires that open attempt for the version and purpose (RELEASE_TRACE_MISSING otherwise). The same transaction
closes the attempt with close_release_attempt: 'done' when the release goes through, or 'refused' with a reason code.
Only the release path writes attempts. A refused attempt never counts as a release.

The send step, which is not built yet, must call check_release before it sends. check_release re-checks the approval
against the current digest, the release rules, and the open release attempt, and returns a refusal whenever a check
fails.

Export (export_version): only a version that has an approval can be exported. The export is the approved package as
JSON (schemas/disclosure-export.v0.json). It follows the preview rule: derivatives only, no file name, no case title,
no redaction reason, no source checksum, and items outside the caller's TLP audience are withheld without details. An
approved version that a later version has replaced is exported with status "superseded", so the history is marked.
Each export is a release attempt with the same check as a notice, except that a superseded version is still exportable.
Each export writes one audit row, in the same transaction as the read, with the identifiers and the status only.

Access: the caller needs the permission of the route (disclosures:read, disclosures:prepare, or disclosures:approve), the
case must be visible to the caller (cases.CaseScope, the same rule as the other case records), and the caller must be in
the TLP audience of each non-CLEAR item (safe_kit.tlp). CLEAR items are gated by release approval, which is recorded by
the approval itself, so the creation and approval checks do not require it beforehand.

Previews show the structure only: the template sections, the recipient, the purpose, the audience, the digest, and for each
item its ID, media type, size, label, and number of removed lines. No evidence content, no file name, no case title, and
no redaction reason is included. An item the caller may not see is listed as withheld, without any of its details.

Every change runs in one transaction with its audit row. Free text (recipient, purpose) is never written to the audit log.
Idempotency follows safe_kit.idempotency: the key is checked and locked after the case row is locked.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from psycopg.types.json import Jsonb

from safe_kit import audit, tlp
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.idempotency import (
    KeyScope,
    KeyReused,
    Replayed,
    StoredResponse,
    check,
    remember,
)

MAX_EVIDENCE_PER_VERSION = 50
RECIPIENT_LIMIT = 200
PURPOSE_LIMIT = 500


@dataclass(frozen=True)
class Section:
    title: str
    contains: str


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    sections: tuple[Section, ...]


# The three templates of plans/02 and plans/f0/api-draft.md section 2.5. A template fixes the structure of the package.
# The sections name what each part of the package holds; the package itself stores only metadata and derivatives.
TEMPLATES: dict[str, Template] = {
    "confidential_rapid_alert": Template(
        key="confidential_rapid_alert",
        title="Confidential rapid alert",
        sections=(
            Section("Header", "Case reference, recipient, purpose, and TLP audience"),
            Section(
                "Attached derivatives",
                "Each redacted item: its ID, media type, size, label, and checksum",
            ),
            Section(
                "Redaction manifest",
                "Removed line ranges per item, with no removed text",
            ),
            Section(
                "Approval record",
                "The approval ID, the approver, and the digest the approval covers",
            ),
        ),
    ),
    "de_identified_member_advisory": Template(
        key="de_identified_member_advisory",
        title="De-identified member advisory",
        sections=(
            Section(
                "Header",
                "Case reference, recipient, purpose, and TLP audience; no names of individuals",
            ),
            Section(
                "Attached derivatives",
                "Each redacted item: its ID, media type, size, label, and checksum",
            ),
            Section(
                "Redaction manifest",
                "Removed line ranges per item, with no removed text",
            ),
            Section(
                "Approval record",
                "The approval ID, the approver, and the digest the approval covers",
            ),
        ),
    ),
    "redacted_public_safety_report": Template(
        key="redacted_public_safety_report",
        title="Redacted public safety report",
        sections=(
            Section("Header", "Case reference and the audience of the report"),
            Section(
                "Attached derivatives",
                "Each redacted item: its ID, media type, size, label, and checksum",
            ),
            Section(
                "Redaction manifest",
                "Removed line ranges per item, with no removed text",
            ),
            Section(
                "Approval record",
                "The approval ID, the approver, and the digest the approval covers",
            ),
        ),
    ),
}

PREVIEW_NOTICE = (
    "This preview shows the structure of the package. It contains no evidence content, no file name, and no redaction "
    "reason. Items you may not see are listed as withheld."
)


class Outcome(Enum):
    OK = "ok"
    REPLAY = "replay"
    KEY_REUSED = "key_reused"
    NOT_FOUND = "not_found"  # the case is missing or not visible to the caller
    CLOSED = "closed"
    PACKAGE_NOT_FOUND = "package_not_found"
    VERSION_NOT_FOUND = "version_not_found"
    VERSION_CONFLICT = "version_conflict"  # value is the current version number
    NO_CHANGE = "no_change"
    EVIDENCE_NOT_ELIGIBLE = "evidence_not_eligible"
    AUDIENCE_WIDER_THAN_EVIDENCE = "audience_wider_than_evidence"
    NOT_LATEST = "not_latest"
    NOT_APPROVED = "not_approved"  # export: only an approved version can be exported
    ALREADY_APPROVED = "already_approved"
    PREPARER_IS_APPROVER = "preparer_is_approver"
    BINDING_MISMATCH = "binding_mismatch"
    EVIDENCE_NOT_VISIBLE = "evidence_not_visible"
    INTEGRITY_FAILED = "integrity_failed"
    APPROVAL_MISMATCH = (
        "approval_mismatch"  # export: the approval does not match the version
    )
    RELEASE_APPROVAL_MISSING = (
        "release_approval_missing"  # export: a CLEAR item has no release approval
    )
    RELEASE_TRACE_MISSING = (
        "release_trace_missing"  # export: no open release attempt for the version
    )


@dataclass(frozen=True)
class Item:
    evidence_id: str
    redaction_id: str
    media_type: str
    size_bytes: int
    sha256_plaintext: str
    tlp_label: str


# The identifier of a recipient kept outside SAFE (approval recipient_ref, notice recipient_ref). Names and addresses
# do not match it.
RECIPIENT_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"


@dataclass(frozen=True)
class Approval:
    id: str
    version_id: str
    recipient: str
    purpose: str
    digest: str
    preparer_id: str
    approved_by: str
    approved_at: datetime
    # None only for an approval made before migration 0049. Such an approval cannot receive a notice.
    recipient_ref: str | None = None


@dataclass(frozen=True)
class Version:
    id: str
    package_id: str
    case_id: str
    version: int
    template: str
    tlp_audience: str
    recipient: str
    purpose: str
    digest: str
    manifest: dict[str, Any]
    items: tuple[Item, ...]
    created_by: str
    created_at: datetime
    is_latest: bool
    approval: Approval | None = None

    @property
    def approval_state(self) -> str:
        """approved: the approval covers this version and it is the latest. revoked_by_change: an approval exists for an
        earlier version, which a later version has replaced. not_approved: no approval exists for this version."""
        if self.approval is None:
            return "not_approved"
        return "approved" if self.is_latest else "revoked_by_change"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    value: Any = None
    stored: StoredResponse | None = None


class ReleaseOutcome(Enum):
    RELEASABLE = "releasable"
    NOT_FOUND = "not_found"
    NOT_APPROVED = "not_approved"  # no approval exists for the version
    REVOKED_BY_CHANGE = (
        "revoked_by_change"  # the approved version is no longer the latest
    )
    APPROVAL_MISMATCH = "approval_mismatch"  # the approval does not match the version's recipient, purpose or digest
    DIGEST_MISMATCH = (
        "digest_mismatch"  # the content no longer matches the stored digest
    )
    RELEASE_APPROVAL_MISSING = (
        "release_approval_missing"  # a CLEAR item has no release approval
    )
    RELEASE_TRACE_MISSING = (
        "release_trace_missing"  # no open release attempt for the version and purpose
    )


@dataclass(frozen=True)
class ReleaseCheck:
    outcome: ReleaseOutcome
    version_id: str
    digest: str | None = None


# The purpose of a release attempt: a notice record, or a disclosure export (migration 0047).
RELEASE_PURPOSE_NOTICE = "external_notice"
RELEASE_PURPOSE_EXPORT = "export"


@dataclass(frozen=True)
class ReleaseAttempt:
    id: str
    version: Version
    purpose: str
    actor_id: str


# --- canonical content and digest -------------------------------------------------------------------------------------


def _item_manifest(
    evidence_id: str, redaction_id: str, redaction_manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """The part of a redaction manifest that a package carries: line ranges and the original's checksum, no reasons."""
    return {
        "evidence_id": evidence_id,
        "redaction_id": redaction_id,
        "source_sha256": redaction_manifest.get("source_sha256"),
        "lines_total": redaction_manifest.get("lines_total"),
        "removed_lines": redaction_manifest.get("removed_lines"),
        "removed": [
            {
                "from_line": entry["from_line"],
                "to_line": entry["to_line"],
                "lines": entry["lines"],
            }
            for entry in redaction_manifest.get("removed", [])
        ],
    }


def _manifest(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {"items": sorted(items, key=lambda entry: entry["evidence_id"])}


def canonical_content(
    case_id: str,
    template: str,
    tlp_audience: str,
    recipient: str,
    purpose: str,
    items: Sequence[Item],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """The content a digest covers. The package ID and the version number are not part of it, so the digest names the
    content alone. Items are sorted by evidence ID, so the order of the request does not change the digest."""
    return {
        "case_id": case_id,
        "template": template,
        "tlp_audience": tlp_audience,
        "recipient": recipient,
        "purpose": purpose,
        "items": [
            {
                "evidence_id": item.evidence_id,
                "redaction_id": item.redaction_id,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
                "sha256_plaintext": item.sha256_plaintext,
                "tlp_label": item.tlp_label,
            }
            for item in sorted(items, key=lambda entry: entry.evidence_id)
        ],
        "manifest": dict(manifest),
    }


def digest_of(content: Mapping[str, Any]) -> str:
    """SHA-256, lower-case hex, of the canonical JSON of the content: sorted keys, no extra spaces."""
    canonical = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digest_of_version(version: Version) -> str:
    return digest_of(
        canonical_content(
            version.case_id,
            version.template,
            version.tlp_audience,
            version.recipient,
            version.purpose,
            version.items,
            version.manifest,
        )
    )


# --- the approver's packet rule -------------------------------------------------------------------------------------------
# A disclosure approver (disclosures:approve) holds no case read (migration 0056). Through the disclosure read path it
# reads, and may approve, one packet of a case: a version that is the latest of its package and has no approval yet
# (the packet awaiting approval), or a version it approved itself. The rule opens the version and the case status to the
# disclosure routes only. It is never part of CaseScope.visibility_sql, so the case routes stay closed to the approver.
# The approver's missing facts (the escalations of the version) are read on the same rule (external_notice_records_api).


def _packet_condition(alias: str) -> str:
    """The packet rule on one version row, with one %s for the approver's user ID (the approved_by check)."""
    return f"""(
        (
            NOT EXISTS (SELECT 1 FROM disclosure_approvals pa WHERE pa.version_id = {alias}.id AND NOT pa.is_deleted)
            AND {alias}.version = (
                SELECT MAX(lv.version) FROM disclosure_versions lv
                WHERE lv.package_id = {alias}.package_id AND NOT lv.is_deleted
            )
        )
        OR EXISTS (
            SELECT 1 FROM disclosure_approvals pa2
            WHERE pa2.version_id = {alias}.id AND NOT pa2.is_deleted AND pa2.approved_by = %s
        )
    )"""


def _packet_exists_sql() -> str:
    """The packet rule for one version ID (%s) of the case in the outer query (cases.id)."""
    return (
        "EXISTS (SELECT 1 FROM disclosure_versions pv WHERE pv.id = %s AND pv.case_id = cases.id "
        f"AND NOT pv.is_deleted AND {_packet_condition('pv')})"
    )


def _case_condition(
    scope: CaseScope, *, writing: bool, version_id: str | None
) -> tuple[str, tuple]:
    """The condition on the cases table for a disclosure operation: the case rule, or for an approver the packet rule
    on version_id as well. A non-approver, or no version, gets the case rule alone."""
    visibility, params = scope.visibility_sql(writing=writing)
    if version_id is None or not scope.approver:
        return visibility, params
    return (
        f"({visibility} OR {_packet_exists_sql()})",
        (*params, version_id, scope.user_id),
    )


# --- database helpers -----------------------------------------------------------------------------------------------


def _lock_case(
    conn, case_id: str, scope: CaseScope, version_id: str | None = None
) -> str | None:
    """Lock the case row, then check the caller's visibility. Returns the case status, or None when not visible.

    version_id: the version the operation names. An approver may lock the case of its packet through that version (see
    the packet rule above); no other caller is affected.
    """
    visibility, params = _case_condition(scope, writing=True, version_id=version_id)
    row = conn.execute(
        f"SELECT status FROM cases WHERE id = %s AND NOT is_deleted AND {visibility} FOR UPDATE",
        (case_id, *params),
    ).fetchone()
    return row[0] if row is not None else None


def lock_case_for_version(
    conn, case_id: str, scope: CaseScope, version_id: str
) -> str | None:
    """Lock the case for a write on one of its versions (a review, an approval). Returns the status, or None when the
    caller may not write to it. The case rule applies, and an approver may also write through its packet."""
    return _lock_case(conn, case_id, scope, version_id)


def _case_access_on(
    conn, case_id: str, scope: CaseScope, version_id: str | None
) -> str | None:
    """How the caller may read the case, or one of its versions: "case" when the case rule admits it, "packet" when only
    the approver's packet rule does, None when neither. The caller checks that the version belongs to the case."""
    visibility, params = scope.visibility_sql()
    if (
        conn.execute(
            f"SELECT 1 FROM cases WHERE id = %s AND NOT is_deleted AND {visibility}",
            (case_id, *params),
        ).fetchone()
        is not None
    ):
        return "case"
    if version_id is None or not scope.approver:
        return None
    if (
        conn.execute(
            f"SELECT 1 FROM cases WHERE id = %s AND NOT is_deleted AND {_packet_exists_sql()}",
            (case_id, version_id, scope.user_id),
        ).fetchone()
        is not None
    ):
        return "packet"
    return None


def access_for(case_id: str, version_id: str | None, scope: CaseScope) -> str | None:
    """The read access of the caller to a case, or to one version of it: "case", "packet" or None (see _case_access_on).
    The routes answer 404 for None, the same as for a missing case."""
    with connect() as conn:
        return _case_access_on(conn, case_id, scope, version_id)


def _is_assigned(conn, case_id: str, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM case_assignments WHERE case_id = %s AND user_id = %s AND NOT is_deleted",
        (case_id, user_id),
    ).fetchone()
    return row is not None


def _labels_visible(
    conn, case_id: str, recipient: tlp.Recipient, labels: Sequence[str]
) -> list[bool]:
    """For each label, whether the recipient is in its TLP audience. CLEAR counts as visible here: its release is
    checked and recorded by the approval, not by the caller's audience (see the module docstring)."""
    named = _is_assigned(conn, case_id, recipient.user_id)
    return [
        tlp.permits(
            label,
            named=named,
            organisation=recipient.organisation,
            external=recipient.external,
            approved=True,
        )
        for label in labels
    ]


def _eligible_items(
    conn, case_id: str, evidence_ids: Sequence[str]
) -> list[tuple[Item, dict[str, Any]]] | None:
    """The derived, available items of the case with these IDs, with their redaction manifests. None when any ID is
    not such an item (a missing one, an original, a failed one, or one of another case)."""
    rows = conn.execute(
        """
        SELECT e.id::text, r.id::text, e.media_type, e.size_bytes, e.sha256_plaintext, e.tlp_label, r.manifest
        FROM evidence_items e
        JOIN evidence_redactions r ON r.derived_evidence_id = e.id AND NOT r.is_deleted
        WHERE e.id = ANY(%s::uuid[]) AND e.case_id = %s AND NOT e.is_deleted
          AND e.status = 'available' AND e.tlp_label IS NOT NULL
        """,
        (list(evidence_ids), case_id),
    ).fetchall()
    if len(rows) != len(set(evidence_ids)):
        return None
    return [
        (
            Item(
                evidence_id=row[0],
                redaction_id=row[1],
                media_type=row[2],
                size_bytes=row[3],
                sha256_plaintext=row[4],
                tlp_label=row[5],
            ),
            row[6],
        )
        for row in rows
    ]


def _latest_version_number(conn, package_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM disclosure_versions WHERE package_id = %s AND NOT is_deleted",
        (package_id,),
    ).fetchone()[0]


def load_version_on(conn, case_id: str, version_id: str) -> Version | None:
    """One version of the case with its items, approval and latest-version state, on the caller's connection. None when
    the version is not part of the case. The caller checks the case first."""
    row = conn.execute(
        """
        SELECT id::text, package_id::text, case_id::text, version, template, tlp_audience, recipient, purpose,
               digest, manifest, created_by::text, created_at
        FROM disclosure_versions
        WHERE id = %s AND case_id = %s AND NOT is_deleted
        """,
        (version_id, case_id),
    ).fetchone()
    if row is None:
        return None
    item_rows = conn.execute(
        """
        SELECT evidence_id::text, redaction_id::text, media_type, size_bytes, sha256_plaintext, tlp_label
        FROM disclosure_version_items
        WHERE version_id = %s AND NOT is_deleted
        ORDER BY evidence_id
        """,
        (version_id,),
    ).fetchall()
    items = tuple(
        Item(
            evidence_id=item[0],
            redaction_id=item[1],
            media_type=item[2],
            size_bytes=item[3],
            sha256_plaintext=item[4],
            tlp_label=item[5],
        )
        for item in item_rows
    )
    latest = _latest_version_number(conn, row[1])
    approval_row = conn.execute(
        """
        SELECT id::text, version_id::text, recipient, purpose, digest, preparer_id::text, approved_by::text, approved_at,
               recipient_ref
        FROM disclosure_approvals
        WHERE version_id = %s AND NOT is_deleted
        """,
        (version_id,),
    ).fetchone()
    approval = Approval(*approval_row) if approval_row is not None else None
    return Version(
        id=row[0],
        package_id=row[1],
        case_id=row[2],
        version=row[3],
        template=row[4],
        tlp_audience=row[5],
        recipient=row[6],
        purpose=row[7],
        digest=row[8],
        manifest=row[9],
        items=items,
        created_by=row[10],
        created_at=row[11],
        is_latest=row[3] == latest,
        approval=approval,
    )


# --- create a version -----------------------------------------------------------------------------------------------


def create_version(
    case_id: str,
    values: Mapping[str, Any],
    scope: CaseScope,
    recipient: tlp.Recipient,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Version], Any] | None = None,
) -> Result:
    """Store the next version of a package (a new package when values has no package_id).

    values: package_id (str or None), expected_version (int or None), template, tlp_audience, recipient, purpose,
    and evidence_ids (a list of str). For an existing package, expected_version must be its latest version. A
    package version that changes nothing is refused (NO_CHANGE): a change makes a new version, and nothing else does.
    """
    try:
        with connect() as conn:
            check(conn, idempotency)
            status = _lock_case(conn, case_id, scope)
            if status is None:
                return Result(Outcome.NOT_FOUND)
            if status == "closed":
                return Result(Outcome.CLOSED)

            package_id = values.get("package_id")
            latest = 0
            if package_id is not None:
                exists = conn.execute(
                    "SELECT 1 FROM disclosure_packages WHERE id = %s AND case_id = %s AND NOT is_deleted",
                    (package_id, case_id),
                ).fetchone()
                if exists is None:
                    return Result(Outcome.PACKAGE_NOT_FOUND)
                latest = _latest_version_number(conn, package_id)
                if values.get("expected_version") != latest:
                    return Result(Outcome.VERSION_CONFLICT, value=latest)

            eligible = _eligible_items(conn, case_id, values["evidence_ids"])
            if eligible is None:
                return Result(Outcome.EVIDENCE_NOT_ELIGIBLE)
            items = [entry[0] for entry in eligible]
            visible = _labels_visible(
                conn, case_id, recipient, [item.tlp_label for item in items]
            )
            if not all(visible):
                return Result(Outcome.EVIDENCE_NOT_ELIGIBLE)
            audience = values["tlp_audience"]
            if not all(
                tlp.at_least_as_restrictive(audience, item.tlp_label) for item in items
            ):
                return Result(Outcome.AUDIENCE_WIDER_THAN_EVIDENCE)

            item_manifests = [
                _item_manifest(item.evidence_id, item.redaction_id, entry[1])
                for item, entry in zip(items, eligible)
            ]
            manifest = _manifest(item_manifests)
            content_digest = digest_of(
                canonical_content(
                    case_id,
                    values["template"],
                    audience,
                    values["recipient"],
                    values["purpose"],
                    items,
                    manifest,
                )
            )
            if package_id is not None:
                previous = conn.execute(
                    "SELECT digest FROM disclosure_versions WHERE package_id = %s AND version = %s AND NOT is_deleted",
                    (package_id, latest),
                ).fetchone()
                if previous is not None and previous[0] == content_digest:
                    return Result(Outcome.NO_CHANGE)
            else:
                package_id = conn.execute(
                    "INSERT INTO disclosure_packages (case_id, created_by) VALUES (%s, %s) RETURNING id::text",
                    (case_id, scope.user_id),
                ).fetchone()[0]

            version_number = latest + 1
            version_id, created_at = conn.execute(
                """
                INSERT INTO disclosure_versions
                    (package_id, case_id, version, template, tlp_audience, recipient, purpose, manifest, digest, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id::text, created_at
                """,
                (
                    package_id,
                    case_id,
                    version_number,
                    values["template"],
                    audience,
                    values["recipient"],
                    values["purpose"],
                    Jsonb(manifest),
                    content_digest,
                    scope.user_id,
                ),
            ).fetchone()
            for item in items:
                conn.execute(
                    """
                    INSERT INTO disclosure_version_items
                        (version_id, evidence_id, redaction_id, media_type, size_bytes, sha256_plaintext, tlp_label)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        version_id,
                        item.evidence_id,
                        item.redaction_id,
                        item.media_type,
                        item.size_bytes,
                        item.sha256_plaintext,
                        item.tlp_label,
                    ),
                )
            audit.record(
                conn,
                audit.DISCLOSURE_VERSION_CREATE,
                "disclosure_version",
                version_id,
                actor_id=scope.user_id,
                details={
                    "case_id": case_id,
                    "package_id": package_id,
                    "version": version_number,
                    "digest": content_digest,
                    "template": values["template"],
                    "tlp_audience": audience,
                    "evidence_count": len(items),
                },
            )
            created = Version(
                id=version_id,
                package_id=package_id,
                case_id=case_id,
                version=version_number,
                template=values["template"],
                tlp_audience=audience,
                recipient=values["recipient"],
                purpose=values["purpose"],
                digest=content_digest,
                manifest=manifest,
                items=tuple(sorted(items, key=lambda entry: entry.evidence_id)),
                created_by=scope.user_id,
                created_at=created_at,
                is_latest=True,
            )
            if idempotency is not None:
                assert render is not None
                remember(conn, idempotency, 201, render(created))
        return Result(Outcome.OK, value=created)
    except Replayed as replay:
        return Result(Outcome.REPLAY, stored=replay.stored)
    except KeyReused:
        return Result(Outcome.KEY_REUSED)


# --- read and preview -----------------------------------------------------------------------------------------------


def list_versions(case_id: str, scope: CaseScope, limit: int) -> list[Version] | None:
    """The newest versions of the case, newest first. None when the case is not visible to the caller.

    An approver without the case rule gets only the versions the packet rule admits (the packet awaiting its approval,
    and its own approvals), in the same order. For it an unknown case and a case it may not see give the same empty list.
    """
    with connect() as conn:
        access = _case_access_on(conn, case_id, scope, None)
        if access == "case":
            ids = conn.execute(
                """
                SELECT id::text FROM disclosure_versions
                WHERE case_id = %s AND NOT is_deleted
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (case_id, limit),
            ).fetchall()
        elif scope.approver:
            ids = conn.execute(
                f"""
                SELECT v.id::text FROM disclosure_versions v
                WHERE v.case_id = %s AND NOT v.is_deleted AND {_packet_condition("v")}
                ORDER BY v.created_at DESC, v.id DESC
                LIMIT %s
                """,
                (case_id, scope.user_id, limit),
            ).fetchall()
        else:
            return None
        return [load_version_on(conn, case_id, row[0]) for row in ids]


def get_version(case_id: str, version_id: str, scope: CaseScope) -> Version | None:
    """One version. None when the caller has no access to the case or to this version through the packet rule, or when
    the version is not part of the case (the caller cannot tell which)."""
    with connect() as conn:
        if _case_access_on(conn, case_id, scope, version_id) is None:
            return None
        return load_version_on(conn, case_id, version_id)


def item_visibility(version: Version, recipient: tlp.Recipient) -> list[bool]:
    """For each item of the version, whether the recipient may see it under the TLP audience rule."""
    with connect() as conn:
        return _labels_visible(
            conn, version.case_id, recipient, [item.tlp_label for item in version.items]
        )


def preview(version: Version, recipient: tlp.Recipient) -> dict[str, Any]:
    """The structure of one version for the caller. Holds no evidence content and no original text (see the docstring)."""
    visible = item_visibility(version, recipient)
    template = TEMPLATES[version.template]
    evidence: list[dict[str, Any]] = []
    withheld = 0
    for item, may_see in zip(version.items, visible):
        if not may_see:
            withheld += 1
            evidence.append({"withheld": True})
            continue
        removed = next(
            (
                entry
                for entry in version.manifest["items"]
                if entry["evidence_id"] == item.evidence_id
            ),
            None,
        )
        evidence.append(
            {
                "withheld": False,
                "evidence_id": item.evidence_id,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
                "tlp_label": item.tlp_label,
                "removed_lines": removed["removed_lines"] if removed else None,
                "removed_ranges": len(removed["removed"]) if removed else 0,
            }
        )
    return {
        "version_id": version.id,
        "package_id": version.package_id,
        "case_id": version.case_id,
        "version": version.version,
        "template": {
            "key": template.key,
            "title": template.title,
            "sections": [
                {"title": section.title, "contains": section.contains}
                for section in template.sections
            ],
        },
        "recipient": version.recipient,
        "purpose": version.purpose,
        "tlp_audience": version.tlp_audience,
        "digest": version.digest,
        "approval_state": version.approval_state,
        "evidence": evidence,
        "withheld_count": withheld,
        "notice": PREVIEW_NOTICE,
    }


# --- approval -------------------------------------------------------------------------------------------------------


def check_approval_on(
    conn,
    case_id: str,
    version: Version,
    approver_id: str,
    approval_values: Mapping[str, str],
    recipient: tlp.Recipient,
) -> Outcome | None:
    """The rules of one approval, on the caller's connection. None when the approval may be written.

    Refused: a version that already has an approval (ALREADY_APPROVED), a version that is not the latest (NOT_LATEST),
    the preparer as approver (PREPARER_IS_APPROVER), approval values that differ from the version (BINDING_MISMATCH), an
    approver who cannot see a non-CLEAR item (EVIDENCE_NOT_VISIBLE), and a stored digest that no longer matches (INTEGRITY_FAILED).
    Used by the approver (approve_version) and by a backup's approve decision (safe_kit.external_notice_backup).
    """
    if version.approval is not None:
        return Outcome.ALREADY_APPROVED
    if not version.is_latest:
        return Outcome.NOT_LATEST
    if version.created_by == approver_id:
        return Outcome.PREPARER_IS_APPROVER
    if (
        approval_values["recipient"] != version.recipient
        or approval_values["purpose"] != version.purpose
        or approval_values["digest"] != version.digest
    ):
        return Outcome.BINDING_MISMATCH
    if not all(
        _labels_visible(
            conn, case_id, recipient, [item.tlp_label for item in version.items]
        )
    ):
        return Outcome.EVIDENCE_NOT_VISIBLE
    if digest_of_version(version) != version.digest:
        return Outcome.INTEGRITY_FAILED
    return None


def write_approval_on(
    conn,
    case_id: str,
    version: Version,
    approver_id: str,
    approval_values: Mapping[str, str],
    *,
    extra_details: Mapping[str, Any] | None = None,
) -> Version:
    """Write the approval row of a version, the release approvals of its CLEAR items, and the audit row, on the caller's
    connection. The caller has run check_approval_on first. The approval row is the only record of the approval: the
    version's approval state is derived from it (Version.approval_state).

    extra_details: identifiers added to the audit row (for example the escalation of a backup's decision). No free text.
    """
    release_recorded = 0
    for item in version.items:
        if item.tlp_label == tlp.CLEAR and not tlp.is_release_approved_on(
            conn, item.evidence_id
        ):
            tlp.record_release_approval_on(conn, item.evidence_id, approver_id)
            release_recorded += 1

    approval_id, approved_at = conn.execute(
        """
        INSERT INTO disclosure_approvals
            (version_id, package_id, case_id, recipient, purpose, digest, preparer_id, approved_by,
             separation_rule, recipient_ref)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'preparer_differs_from_approver', %s)
        RETURNING id::text, approved_at
        """,
        (
            version.id,
            version.package_id,
            case_id,
            version.recipient,
            version.purpose,
            version.digest,
            version.created_by,
            approver_id,
            approval_values["recipient_ref"],
        ),
    ).fetchone()
    details: dict[str, Any] = {
        "case_id": case_id,
        "package_id": version.package_id,
        "version": version.version,
        "digest": version.digest,
        "tlp_audience": version.tlp_audience,
        "release_approvals_recorded": release_recorded,
    }
    if extra_details:
        details.update(extra_details)
    audit.record(
        conn,
        audit.DISCLOSURE_APPROVAL,
        "disclosure_version",
        version.id,
        actor_id=approver_id,
        details=details,
    )
    approval = Approval(
        id=approval_id,
        version_id=version.id,
        recipient=version.recipient,
        purpose=version.purpose,
        digest=version.digest,
        preparer_id=version.created_by,
        approved_by=approver_id,
        approved_at=approved_at,
        recipient_ref=approval_values["recipient_ref"],
    )
    return replace(version, approval=approval)


def approve_version(
    case_id: str,
    version_id: str,
    approval_values: Mapping[str, str],
    scope: CaseScope,
    recipient: tlp.Recipient,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Version], Any] | None = None,
) -> Result:
    """Record the human approval of one version. approval_values: recipient, purpose and digest, as the approver
    confirms them. They must equal the version's values, or the approval is refused (BINDING_MISMATCH). It also holds
    recipient_ref, the identifier of the approved recipient, which the approval stores and no version field holds: a
    notice of this version must name it (external_notices.record_notice).

    Refused: a version that is not the latest, a version that already has an approval, the preparer approving their own
    version, and an approver who cannot see a non-CLEAR item. A CLEAR item without release approval gets one here.
    """
    try:
        with connect() as conn:
            check(conn, idempotency)
            status = _lock_case(conn, case_id, scope, version_id)
            if status is None:
                return Result(Outcome.NOT_FOUND)
            if status == "closed":
                return Result(Outcome.CLOSED)
            version = load_version_on(conn, case_id, version_id)
            if version is None:
                return Result(Outcome.VERSION_NOT_FOUND)
            refusal = check_approval_on(
                conn, case_id, version, scope.user_id, approval_values, recipient
            )
            if refusal is not None:
                return Result(refusal)
            approved = write_approval_on(
                conn, case_id, version, scope.user_id, approval_values
            )
            if idempotency is not None:
                assert render is not None
                remember(conn, idempotency, 201, render(approved))
        return Result(Outcome.OK, value=approved)
    except Replayed as replay:
        return Result(Outcome.REPLAY, stored=replay.stored)
    except KeyReused:
        return Result(Outcome.KEY_REUSED)


# --- the send step's check ------------------------------------------------------------------------------------------


def check_release(
    version_id: str, purpose: str = RELEASE_PURPOSE_NOTICE
) -> ReleaseCheck:
    """Re-check a version right before a send. The send step of a package calls it, and so does the notice record
    (safe_kit.external_notices), which may only record a notice for a version that passes.

    It passes only when all of these hold: the version exists; an approval exists for it; the approval's recipient,
    purpose and digest equal the version's; the version is still the latest of its package; the digest recomputed from
    the stored content equals the stored digest; for a CLEAR audience, every CLEAR item has a release approval; and an
    open release attempt exists for the version and purpose (RELEASE_TRACE_MISSING otherwise). The check reads only:
    the attempt is written by the release path, not by this function.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT case_id::text FROM disclosure_versions WHERE id = %s AND NOT is_deleted",
            (version_id,),
        ).fetchone()
        if row is None:
            return ReleaseCheck(ReleaseOutcome.NOT_FOUND, version_id)
        version = load_version_on(conn, row[0], version_id)
        assert version is not None
        return release_check_on(conn, version, purpose)


def release_check_on(
    conn,
    version: Version,
    purpose: str = RELEASE_PURPOSE_NOTICE,
    *,
    history: bool = False,
) -> ReleaseCheck:
    """The check of check_release for a version already loaded on the caller's connection (see the docstring above).

    history: an export of a superseded version is allowed as history, so REVOKED_BY_CHANGE is not raised for it. Every
    other rule, including the open release attempt, still applies.
    """
    version_id = version.id
    if version.approval is None:
        return ReleaseCheck(ReleaseOutcome.NOT_APPROVED, version_id, version.digest)
    if not version.is_latest and not history:
        return ReleaseCheck(
            ReleaseOutcome.REVOKED_BY_CHANGE, version_id, version.digest
        )
    approval = version.approval
    if (approval.recipient, approval.purpose, approval.digest) != (
        version.recipient,
        version.purpose,
        version.digest,
    ):
        return ReleaseCheck(
            ReleaseOutcome.APPROVAL_MISMATCH, version_id, version.digest
        )
    if digest_of_version(version) != version.digest:
        return ReleaseCheck(ReleaseOutcome.DIGEST_MISMATCH, version_id, version.digest)
    release_ok = all(
        tlp.is_release_approved_on(conn, item.evidence_id)
        for item in version.items
        if item.tlp_label == tlp.CLEAR
    )
    # The TLP rule (safe_kit.tlp): CLEAR is permitted only after the approval record exists, which it does here.
    # A CLEAR item still needs its own release approval, so both checks are required for a CLEAR package.
    if version.tlp_audience == tlp.CLEAR and not tlp.permits(
        tlp.CLEAR,
        named=False,
        organisation=False,
        external=False,
        approved=release_ok,
    ):
        return ReleaseCheck(
            ReleaseOutcome.RELEASE_APPROVAL_MISSING, version_id, version.digest
        )
    # The trace rule (migration 0047): the release path opens the attempt before it calls the check, on the same
    # connection, so the attempt is visible here. It is checked last so that the rules above keep their own outcomes.
    if not _open_attempt_exists(conn, version_id, purpose):
        return ReleaseCheck(
            ReleaseOutcome.RELEASE_TRACE_MISSING, version_id, version.digest
        )
    return ReleaseCheck(ReleaseOutcome.RELEASABLE, version_id, version.digest)


def _open_attempt_exists(conn, version_id: str, purpose: str) -> bool:
    """True when an 'attempted' row exists for the version and purpose and no outcome row closes it."""
    return bool(
        conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM release_attempts a
                WHERE a.version_id = %s AND a.purpose = %s AND a.state = 'attempted' AND NOT a.is_deleted
                  AND NOT EXISTS (
                      SELECT 1 FROM release_attempts r
                      WHERE r.attempt_id = a.id AND r.state <> 'attempted' AND NOT r.is_deleted
                  )
            )
            """,
            (version_id, purpose),
        ).fetchone()[0]
    )


# --- release attempts (migration 0047) -----------------------------------------------------------------------------


def open_release_attempt(
    conn, version: Version, purpose: str, actor_id: str
) -> ReleaseAttempt:
    """Write the 'attempted' row of a release, on the caller's connection, before the release check runs.

    Only the release path calls it: the notice record and the export. The caller must close the attempt with
    close_release_attempt in the same transaction, whatever the outcome, so no open attempt is ever committed.
    """
    assert purpose in (RELEASE_PURPOSE_NOTICE, RELEASE_PURPOSE_EXPORT), purpose
    attempt_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO release_attempts (id, attempt_id, state, purpose, case_id, package_id, version_id, actor_id)
        VALUES (%s, %s, 'attempted', %s, %s, %s, %s, %s)
        """,
        (
            attempt_id,
            attempt_id,
            purpose,
            version.case_id,
            version.package_id,
            version.id,
            actor_id,
        ),
    )
    return ReleaseAttempt(attempt_id, version, purpose, actor_id)


def close_release_attempt(
    conn, attempt: ReleaseAttempt, reason_code: str | None = None
) -> None:
    """Write the outcome of an attempt, on the same connection: 'done' when reason_code is None, else 'refused' with the
    reason. A refused attempt is audited with the reason code only."""
    version = attempt.version
    conn.execute(
        """
        INSERT INTO release_attempts (
            attempt_id, state, purpose, case_id, package_id, version_id, actor_id, reason_code
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            attempt.id,
            "done" if reason_code is None else "refused",
            attempt.purpose,
            version.case_id,
            version.package_id,
            version.id,
            attempt.actor_id,
            reason_code,
        ),
    )
    if reason_code is not None:
        audit.record(
            conn,
            audit.DISCLOSURE_RELEASE_REFUSED,
            "disclosure_version",
            version.id,
            actor_id=attempt.actor_id,
            details={
                "case_id": version.case_id,
                "package_id": version.package_id,
                "version": version.version,
                "purpose": attempt.purpose,
                "reason_code": reason_code,
            },
        )


# --- export of an approved version ----------------------------------------------------------------------------------

EXPORT_SCHEMA_VERSION = "0.1.0"  # disclosure_export_schema_version; changes to the fields or enums change it
EXPORT_STATUS_APPROVED = "approved"  # the latest version, with its approval
EXPORT_STATUS_SUPERSEDED = "superseded"  # an approved version that a later version of the package replaced: history


def export_document(
    version: Version, visible: Sequence[bool], latest: int
) -> dict[str, Any]:
    """The approved version as the versioned export document (schemas/disclosure-export.v0.json).

    visible: for each item of the version, whether the caller may see it (the TLP rule). latest: the latest version
    number of the package. The document holds no export time, so an unchanged version exports the same document. It
    holds the template, recipient, purpose, TLP audience, digest, the derivative items, the line ranges of the items the
    caller may see, and the approval reference. It never holds a file name, a case title, a redaction reason, the
    checksum of an original, or the redaction ID; a withheld item is only a placeholder.
    """
    approval = version.approval
    assert approval is not None, "only an approved version is exported"
    template = TEMPLATES[version.template]
    manifest_by_item = {
        entry["evidence_id"]: entry for entry in version.manifest["items"]
    }
    items: list[dict[str, Any]] = []
    manifest_items: list[dict[str, Any]] = []
    withheld = 0
    for item, may_see in zip(version.items, visible):
        if not may_see:
            withheld += 1
            items.append({"withheld": True})
            continue
        items.append(
            {
                "withheld": False,
                "evidence_id": item.evidence_id,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
                "sha256_plaintext": item.sha256_plaintext,
                "tlp_label": item.tlp_label,
            }
        )
        entry = manifest_by_item[item.evidence_id]
        manifest_items.append(
            {
                "evidence_id": item.evidence_id,
                "lines_total": entry["lines_total"],
                "removed_lines": entry["removed_lines"],
                "removed": [
                    {
                        "from_line": removed["from_line"],
                        "to_line": removed["to_line"],
                        "lines": removed["lines"],
                    }
                    for removed in entry["removed"]
                ],
            }
        )
    return {
        "disclosure_export_schema_version": EXPORT_SCHEMA_VERSION,
        "disclosure_export": {
            "case_id": version.case_id,
            "package_id": version.package_id,
            "version_id": version.id,
            "version": version.version,
            "status": EXPORT_STATUS_APPROVED
            if version.is_latest
            else EXPORT_STATUS_SUPERSEDED,
            "latest_version": latest,
            "template": {
                "key": template.key,
                "title": template.title,
                "sections": [
                    {"title": section.title, "contains": section.contains}
                    for section in template.sections
                ],
            },
            "recipient": version.recipient,
            "purpose": version.purpose,
            "tlp_audience": version.tlp_audience,
            "digest": version.digest,
            "items": items,
            "withheld_count": withheld,
            "manifest": {"items": manifest_items},
            "approval": {
                "id": approval.id,
                "approved_by": approval.approved_by,
                "approved_at": approval.approved_at.isoformat(),
                "digest": approval.digest,
                "separation_rule": "preparer_differs_from_approver",
            },
        },
    }


def export_version(
    case_id: str, version_id: str, scope: CaseScope, recipient: tlp.Recipient
) -> Result:
    """The export of one version, and its audit row, in one transaction.

    The export is a release: it opens a release attempt (purpose export) before the release check, and closes it with
    the outcome in the same transaction. Refused: a case the caller cannot see (NOT_FOUND), a version of another case
    (VERSION_NOT_FOUND), and any failed release rule (NOT_APPROVED, INTEGRITY_FAILED, APPROVAL_MISMATCH,
    RELEASE_APPROVAL_MISSING, or RELEASE_TRACE_MISSING). A refused export is recorded as a refused attempt. The audit row
    holds the identifiers, the digest, and the status; the export itself is never written to the log.
    """
    with connect() as conn:
        visibility, params = scope.visibility_sql()
        if (
            conn.execute(
                f"SELECT 1 FROM cases WHERE id = %s AND NOT is_deleted AND {visibility}",
                (case_id, *params),
            ).fetchone()
            is None
        ):
            return Result(Outcome.NOT_FOUND)
        version = load_version_on(conn, case_id, version_id)
        if version is None:
            return Result(Outcome.VERSION_NOT_FOUND)
        attempt = open_release_attempt(
            conn, version, RELEASE_PURPOSE_EXPORT, scope.user_id
        )
        release = release_check_on(conn, version, RELEASE_PURPOSE_EXPORT, history=True)
        if release.outcome is not ReleaseOutcome.RELEASABLE:
            close_release_attempt(conn, attempt, reason_code=release.outcome.value)
            return Result(_EXPORT_REFUSALS[release.outcome])
        visible = _labels_visible(
            conn, case_id, recipient, [item.tlp_label for item in version.items]
        )
        latest = _latest_version_number(conn, version.package_id)
        document = export_document(version, visible, latest)
        status = document["disclosure_export"]["status"]
        audit.record(
            conn,
            audit.DISCLOSURE_EXPORT,
            "disclosure_version",
            version.id,
            actor_id=scope.user_id,
            details={
                "case_id": case_id,
                "package_id": version.package_id,
                "version": version.version,
                "digest": version.digest,
                "status": status,
                "schema_version": EXPORT_SCHEMA_VERSION,
            },
        )
        close_release_attempt(conn, attempt)
    return Result(Outcome.OK, value=document)


# The outcome of a refused export for each failed release rule.
_EXPORT_REFUSALS: dict[ReleaseOutcome, Outcome] = {
    ReleaseOutcome.NOT_FOUND: Outcome.VERSION_NOT_FOUND,
    ReleaseOutcome.NOT_APPROVED: Outcome.NOT_APPROVED,
    ReleaseOutcome.APPROVAL_MISMATCH: Outcome.APPROVAL_MISMATCH,
    ReleaseOutcome.DIGEST_MISMATCH: Outcome.INTEGRITY_FAILED,
    ReleaseOutcome.RELEASE_APPROVAL_MISSING: Outcome.RELEASE_APPROVAL_MISSING,
    ReleaseOutcome.RELEASE_TRACE_MISSING: Outcome.RELEASE_TRACE_MISSING,
}
