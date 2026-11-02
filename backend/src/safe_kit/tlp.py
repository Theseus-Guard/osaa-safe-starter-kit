"""TLP audience labels on evidence items, and the TLP check of an access.

Access to a labelled evidence item is the AND of three separate checks, and a failure in any one of them denies access:
  1. RBAC: the caller's role holds the permission (evidence:read for reading, evidence:write for creating a derivative);
  2. case ACL: the case is visible to the caller (cases.CaseScope);
  3. TLP: the caller is in the audience of the item's label (this module).
An item without a label (an uploaded original) has no TLP rule, so only checks 1 and 2 apply to it.

Audience categories, from the caller's role and case assignment:
  named         the caller has an active assignment on the item's case;
  organisation  the caller holds an internal staff role (every role except the reporter-channel roles);
  external      the caller holds a reporter-channel role (reporter, reporting_owner).
Every authenticated caller is in the community that GREEN addresses.

Label audiences, from the narrowest to the widest (TLP 2.0 ordering):
  RED           named only
  AMBER+STRICT  named, organisation
  AMBER         named, organisation, external (the parties to protect)
  GREEN         any authenticated caller
  CLEAR         any caller, but only after a release approval record exists (evidence_release_approvals)

The pure function permits() and the SQL in visibility_sql() state the same rule. A database test checks that they agree.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from safe_kit import audit
from safe_kit.db import connect
from safe_kit.users import User

if TYPE_CHECKING:
    from safe_kit.evidence import StoredEvidence

RED = "RED"
AMBER = "AMBER"
AMBER_STRICT = "AMBER+STRICT"
GREEN = "GREEN"
CLEAR = "CLEAR"
LABELS = (RED, AMBER, AMBER_STRICT, GREEN, CLEAR)

# How restrictive each label is. A derivative may not be labelled less restrictively than its original.
RESTRICTIVENESS = {CLEAR: 0, GREEN: 1, AMBER: 2, AMBER_STRICT: 3, RED: 4}

REPORTER_CHANNEL_ROLES = frozenset({"reporter", "reporting_owner"})


class ReleaseNotApproved(Exception):
    """A CLEAR item cannot be released: no release approval record exists for it."""


@dataclass(frozen=True)
class Recipient:
    """The audience categories of the caller, as far as they are known without the case."""

    user_id: str
    organisation: bool
    external: bool


def recipient_for(user: User) -> Recipient:
    external = user.role in REPORTER_CHANNEL_ROLES
    return Recipient(user_id=user.id, organisation=not external, external=external)


def permits(label: str | None, *, named: bool, organisation: bool, external: bool, approved: bool) -> bool:
    """The TLP rule as a function of the audience categories. None (no label) is always permitted."""
    if label is None or label == GREEN:
        return True
    if label == RED:
        return named
    if label == AMBER_STRICT:
        return named or organisation
    if label == AMBER:
        return named or organisation or external
    if label == CLEAR:
        return approved
    raise ValueError(f"unknown TLP label: {label}")


def at_least_as_restrictive(label: str, original: str | None) -> bool:
    """True when a derivative with label may be shared no wider than its original. No original label: any label."""
    if original is None:
        return True
    return RESTRICTIVENESS[label] >= RESTRICTIVENESS[original]


_NAMED = (
    "EXISTS (SELECT 1 FROM case_assignments ta WHERE ta.case_id = e.case_id AND ta.user_id = %s AND NOT ta.is_deleted)"
)
_APPROVED = "EXISTS (SELECT 1 FROM evidence_release_approvals ra WHERE ra.evidence_id = e.id AND NOT ra.is_deleted)"


def visibility_sql(recipient: Recipient) -> tuple[str, tuple[object, ...]]:
    """A condition on evidence_items (alias e) that holds exactly for the items this recipient may see by TLP."""
    sql = (
        "(e.tlp_label IS NULL"
        " OR e.tlp_label = 'GREEN'"
        f" OR (e.tlp_label = 'RED' AND {_NAMED})"
        f" OR (e.tlp_label = 'AMBER+STRICT' AND ({_NAMED} OR %s))"
        f" OR (e.tlp_label = 'AMBER' AND ({_NAMED} OR %s OR %s))"
        f" OR (e.tlp_label = 'CLEAR' AND {_APPROVED}))"
    )
    uid = recipient.user_id
    params: tuple[object, ...] = (
        uid,  # RED
        uid, recipient.organisation,  # AMBER+STRICT
        uid, recipient.organisation, recipient.external,  # AMBER
    )
    return sql, params


def item_permitted(stored: "StoredEvidence", recipient: Recipient) -> bool:
    """The TLP check for one stored item. An unlabelled item needs no query."""
    if stored.tlp_label is None:
        return True
    clause, params = visibility_sql(recipient)
    with connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM evidence_items e WHERE e.id = %s AND NOT e.is_deleted AND {clause}",
            (stored.id, *params),
        ).fetchone()
    return row is not None


def is_release_approved_on(conn, evidence_id: str) -> bool:
    """The release check on the caller's connection, so it shares the caller's transaction."""
    row = conn.execute(
        "SELECT 1 FROM evidence_release_approvals WHERE evidence_id = %s AND NOT is_deleted", (evidence_id,)
    ).fetchone()
    return row is not None


def is_release_approved(evidence_id: str) -> bool:
    with connect() as conn:
        return is_release_approved_on(conn, evidence_id)


def record_release_approval_on(conn, evidence_id: str, approved_by: str) -> None:
    """Record the release approval on the caller's connection. Refused for any label but CLEAR (see record_release_approval)."""
    row = conn.execute(
        "SELECT tlp_label FROM evidence_items WHERE id = %s AND NOT is_deleted", (evidence_id,)
    ).fetchone()
    if row is None or row[0] != CLEAR:
        raise ReleaseNotApproved()
    conn.execute(
        "INSERT INTO evidence_release_approvals (evidence_id, approved_by) VALUES (%s, %s)",
        (evidence_id, approved_by),
    )
    audit.record(
        conn,
        audit.EVIDENCE_RELEASE_APPROVED,
        "evidence",
        evidence_id,
        actor_id=approved_by,
    )


def record_release_approval(evidence_id: str, approved_by: str) -> None:
    """Record the human approval that lets a CLEAR item be released. Refused for any other label.

    The disclosure approval (safe_kit.disclosure) calls the connection form of this, so approving a CLEAR package records
    the release of its CLEAR items in the same transaction.
    """
    with connect() as conn:
        record_release_approval_on(conn, evidence_id, approved_by)
