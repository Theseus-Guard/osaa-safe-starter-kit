"""The backup approver of an external notice (F3, work package A6; owner decisions 2026-11-03; plans/02 disclosure flow).

When the approver who should send an urgent notice cannot do so, the preparer of the version opens an escalation
(safe_kit.external_notice_records) and names one backup. This module holds the rules of that backup:

- Pool: the owner (a holder of disclosures:assign_backup) sets the backup pool of one case. The pool holds at least two
  distinct people, each an active user who holds disclosures:approve. A holder of disclosures:approve may not assign
  (the primary approver cannot choose a backup), nobody may name themselves, and the platform administrator holds no
  such permission. The pool is replaced in one transaction, so it never holds fewer than two people. Each change to a
  person is audited.
- Waiting time: the backup may decide only from backup_may_act_at, which the escalation takes from the setting
  backup_wait_hours (external_notice_settings, default 4 hours) when it is opened. The system refuses an earlier decision.
  There is no wait option: the deadline keeps running, and an escalation changes no deadline task.
- Narrow read: the backup reads only the packet to send (the structure of the version, as safe_kit.disclosure.preview
  shows it to the backup) and the missing facts of the escalation. This read does not use the case visibility rule: it
  checks that the caller is the backup named on an open escalation of that version, and that the backup is in the pool.
  The case itself is not opened to the backup by this read.
- Decision: approve, reject (a reason is required) or emergency_manual_notice. One decision per escalation. The decision
  is recorded and audited; it does not send anything and does not create the approval row of the version. Those stay
  with the existing paths (safe_kit.disclosure.approve_version and safe_kit.external_notices.record_notice).

Free text (the reason of a decision) is kept in its row and never written to the audit log; the audit log keeps lengths.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from safe_kit import audit, auth, cases, disclosure, tlp
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.idempotency import KeyReused, KeyScope, Replayed, check, remember

# The permission of the backup, and of the approver it replaces: the one approval authority the system defines.
BACKUP_PERMISSION = "disclosures:approve"
# The owner's permission to set a backup pool. No role holds it in migration 0054; the owner decides who does.
ASSIGN_PERMISSION = "disclosures:assign_backup"
MIN_POOL = 2
WAIT_SETTING = "backup_wait_hours"
DECISIONS = ("approve", "reject", "emergency_manual_notice")
MAX_POOL = 20
REASON_LIMIT = 500


def utc_now() -> datetime:
    """The clock of a backup decision. A function, so a test can move it past the waiting time."""
    return datetime.now(UTC)


class Outcome(Enum):
    OK = "ok"
    REPLAY = "replay"
    KEY_REUSED = "key_reused"
    # The case is missing, deleted, or (for an assignment) not visible to the caller.
    NOT_FOUND = "not_found"
    CLOSED = "closed"
    # Assignment: the caller holds disclosures:approve, so the caller may not choose the backups.
    ASSIGNER_IS_APPROVER = "approver_cannot_assign_backup"
    # Assignment: the caller named themselves.
    ASSIGNER_IS_BACKUP = "assigner_cannot_be_backup"
    POOL_TOO_SMALL = "backup_pool_too_small"
    BACKUP_NOT_FOUND = "backup_not_found"
    BACKUP_NOT_APPROVER = "backup_not_approver"
    # Packet and decision: the escalation is missing, or not of this case and version.
    ESCALATION_NOT_FOUND = "escalation_not_found"
    # The caller is not the backup named on the escalation.
    NOT_THE_BACKUP = "not_the_backup"
    # The named backup has been removed from the pool since the escalation was opened.
    NOT_IN_POOL = "backup_not_in_pool"
    ALREADY_DECIDED = "already_decided"
    # The waiting time has not passed. value holds the first moment the backup may act.
    WAIT_NOT_OVER = "backup_wait_not_over"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    value: Any = None
    stored: Any = None


@dataclass(frozen=True)
class Backup:
    id: str
    case_id: str
    backup_user_id: str
    assigned_by: str
    assigned_at: datetime


@dataclass(frozen=True)
class OpenEscalation:
    """What the backup may see of an escalation, and the times that decide whether it may act."""

    id: str
    version_id: str
    backup_user_id: str
    missing_facts: str
    escalated_at: datetime
    backup_may_act_at: datetime


@dataclass(frozen=True)
class Decision:
    id: str
    escalation_id: str
    case_id: str
    version_id: str
    decision: str
    reason: str | None
    decided_by: str
    decided_at: datetime


_BACKUP_COLUMNS = (
    "id::text, case_id::text, backup_user_id::text, assigned_by::text, assigned_at"
)
_DECISION_COLUMNS = "id::text, escalation_id::text, case_id::text, version_id::text, decision, reason, decided_by::text, decided_at"


def _backup_from_row(row: tuple) -> Backup:
    return Backup(*row)


def _decision_from_row(row: tuple) -> Decision:
    return Decision(*row)


# --- database helpers -------------------------------------------------------------------------------------------------


def wait_hours(conn) -> int:
    """The waiting time of a backup in hours, from the setting. A missing setting stops the escalation (no default in code)."""
    row = conn.execute(
        "SELECT value FROM external_notice_settings WHERE key = %s AND NOT is_deleted",
        (WAIT_SETTING,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "the backup waiting time is not set (external_notice_settings)"
        )
    return row[0]


def is_backup(conn, case_id: str, user_id: str) -> bool:
    """True when the user is an active member of the case's backup pool."""
    return (
        conn.execute(
            "SELECT 1 FROM external_notice_backups WHERE case_id = %s AND backup_user_id = %s AND NOT is_deleted",
            (case_id, user_id),
        ).fetchone()
        is not None
    )


def _pool_on(conn, case_id: str) -> list[Backup]:
    rows = conn.execute(
        f"""
        SELECT {_BACKUP_COLUMNS}
        FROM external_notice_backups
        WHERE case_id = %s AND NOT is_deleted
        ORDER BY assigned_at, id
        """,
        (case_id,),
    ).fetchall()
    return [_backup_from_row(row) for row in rows]


def _case_status_on(conn, case_id: str) -> str | None:
    """Lock the case row and return its status. The case must exist and not be deleted; visibility is not checked here,
    because the backup read and decision do not use the case visibility rule."""
    row = conn.execute(
        "SELECT status FROM cases WHERE id = %s AND NOT is_deleted FOR UPDATE",
        (case_id,),
    ).fetchone()
    return row[0] if row is not None else None


def _open_escalation_on(
    conn, case_id: str, version_id: str, escalation_id: str
) -> OpenEscalation | None:
    row = conn.execute(
        """
        SELECT id::text, version_id::text, backup_user_id::text, missing_facts, escalated_at, backup_may_act_at
        FROM external_notice_escalations
        WHERE id = %s AND case_id = %s AND version_id = %s AND NOT is_deleted
        """,
        (escalation_id, case_id, version_id),
    ).fetchone()
    return OpenEscalation(*row) if row is not None else None


def _decided(conn, escalation_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM external_notice_backup_decisions WHERE escalation_id = %s AND NOT is_deleted",
            (escalation_id,),
        ).fetchone()
        is not None
    )


# --- the backup pool (owner) ------------------------------------------------------------------------------------------


def list_pool(case_id: str) -> list[Backup]:
    """The active backup pool of one case, oldest assignment first. The caller checks the case first."""
    with connect() as conn:
        return _pool_on(conn, case_id)


def assign_pool(
    case_id: str,
    backup_user_ids: Sequence[str],
    actor_id: str,
    actor_permissions: frozenset[str],
) -> Result:
    """Set the backup pool of one case to the given people. Each person added or removed is audited, in the same
    transaction as the change.

    Refused: a caller who holds disclosures:approve (ASSIGNER_IS_APPROVER), a caller named in the list, fewer than two
    distinct people, a person who is not an active user (BACKUP_NOT_FOUND), and a person who does not hold
    disclosures:approve (BACKUP_NOT_APPROVER). The case must be visible to the caller and open.
    """
    scope = CaseScope.from_permissions(actor_id, actor_permissions)
    wanted = list(dict.fromkeys(backup_user_ids))
    with connect() as conn:
        locked = cases.lock_visible_case(conn, case_id, scope)
        if locked is None:
            return Result(Outcome.NOT_FOUND)
        if locked[0] == "closed":
            return Result(Outcome.CLOSED)
        if BACKUP_PERMISSION in actor_permissions:
            return Result(Outcome.ASSIGNER_IS_APPROVER)
        if actor_id in wanted:
            return Result(Outcome.ASSIGNER_IS_BACKUP)
        if len(wanted) < MIN_POOL:
            return Result(Outcome.POOL_TOO_SMALL)
        for user_id in wanted:
            role_row = conn.execute(
                "SELECT role FROM users WHERE id = %s AND NOT is_deleted",
                (user_id,),
            ).fetchone()
            if role_row is None:
                return Result(Outcome.BACKUP_NOT_FOUND)
            if BACKUP_PERMISSION not in auth.permissions_for_role(role_row[0]):
                return Result(Outcome.BACKUP_NOT_APPROVER)

        current = _pool_on(conn, case_id)
        current_ids = {member.backup_user_id for member in current}
        for member in current:
            if member.backup_user_id in wanted:
                continue
            conn.execute(
                "UPDATE external_notice_backups SET is_deleted = true, deleted_at = now() WHERE id = %s",
                (member.id,),
            )
            audit.record(
                conn,
                audit.DISCLOSURE_BACKUP_REVOKED,
                "external_notice_backup",
                member.id,
                actor_id=actor_id,
                details={"case_id": case_id, "backup_user_id": member.backup_user_id},
            )
        for user_id in wanted:
            if user_id in current_ids:
                continue
            row_id = conn.execute(
                """
                INSERT INTO external_notice_backups (case_id, backup_user_id, assigned_by)
                VALUES (%s, %s, %s)
                RETURNING id::text
                """,
                (case_id, user_id, actor_id),
            ).fetchone()[0]
            audit.record(
                conn,
                audit.DISCLOSURE_BACKUP_ASSIGNED,
                "external_notice_backup",
                row_id,
                actor_id=actor_id,
                details={"case_id": case_id, "backup_user_id": user_id},
            )
        return Result(Outcome.OK, value=_pool_on(conn, case_id))


# --- the narrow read of the backup -----------------------------------------------------------------------------------


def read_packet(
    case_id: str,
    version_id: str,
    escalation_id: str,
    actor_id: str,
    recipient: tlp.Recipient,
) -> Result:
    """What the named backup may read of an escalation: the packet to send and the missing facts. Nothing else of the
    case. The caller must be the backup named on the escalation, the backup must still be in the pool, and the
    escalation must not be decided yet. The waiting time does not block the read; it is reported, so the backup knows
    when a decision becomes possible."""
    with connect() as conn:
        status = _case_status_on(conn, case_id)
        if status is None:
            return Result(Outcome.NOT_FOUND)
        if status == "closed":
            return Result(Outcome.CLOSED)
        escalation = _open_escalation_on(conn, case_id, version_id, escalation_id)
        if escalation is None:
            return Result(Outcome.ESCALATION_NOT_FOUND)
        if escalation.backup_user_id != actor_id:
            return Result(Outcome.NOT_THE_BACKUP)
        if not is_backup(conn, case_id, actor_id):
            return Result(Outcome.NOT_IN_POOL)
        if _decided(conn, escalation_id):
            return Result(Outcome.ALREADY_DECIDED)
        version = disclosure.load_version_on(conn, case_id, version_id)
        assert version is not None, "the escalation names a version of its case"
    return Result(
        Outcome.OK,
        value={
            "escalation_id": escalation.id,
            "missing_facts": escalation.missing_facts,
            "may_act_at": escalation.backup_may_act_at,
            "packet": disclosure.preview(version, recipient),
        },
    )


# --- the backup's decision -------------------------------------------------------------------------------------------


def record_decision(
    case_id: str,
    version_id: str,
    escalation_id: str,
    decision: str,
    reason: str | None,
    actor_id: str,
    *,
    idempotency: KeyScope | None = None,
    render: Callable[[Decision], Any] | None = None,
) -> Result:
    """Record the backup's decision on an escalation. Refused before the waiting time is over, when the caller is not
    the named backup, when the backup is no longer in the pool, and when a decision already exists. The decision is
    recorded with its audit row in one transaction."""
    assert decision in DECISIONS, decision
    try:
        with connect() as conn:
            status = _case_status_on(conn, case_id)
            check(conn, idempotency)
            if status is None:
                return Result(Outcome.NOT_FOUND)
            if status == "closed":
                return Result(Outcome.CLOSED)
            escalation = _open_escalation_on(conn, case_id, version_id, escalation_id)
            if escalation is None:
                return Result(Outcome.ESCALATION_NOT_FOUND)
            if escalation.backup_user_id != actor_id:
                return Result(Outcome.NOT_THE_BACKUP)
            if not is_backup(conn, case_id, actor_id):
                return Result(Outcome.NOT_IN_POOL)
            if _decided(conn, escalation_id):
                return Result(Outcome.ALREADY_DECIDED)
            if utc_now() < escalation.backup_may_act_at:
                return Result(Outcome.WAIT_NOT_OVER, value=escalation.backup_may_act_at)
            row = conn.execute(
                f"""
                INSERT INTO external_notice_backup_decisions (
                    escalation_id, case_id, version_id, decision, reason, decided_by
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {_DECISION_COLUMNS}
                """,
                (escalation_id, case_id, version_id, decision, reason, actor_id),
            ).fetchone()
            created = _decision_from_row(row)
            audit.record(
                conn,
                audit.DISCLOSURE_BACKUP_DECISION,
                "external_notice_backup_decision",
                created.id,
                actor_id=actor_id,
                details={
                    "case_id": case_id,
                    "version_id": version_id,
                    "escalation_id": escalation_id,
                    "decision": decision,
                    "reason": {"length": len(reason or "")},
                },
            )
            if idempotency is not None:
                assert render is not None
                remember(conn, idempotency, 201, render(created))
        return Result(Outcome.OK, value=created)
    except Replayed as replay:
        return Result(Outcome.REPLAY, stored=replay.stored)
    except KeyReused:
        return Result(Outcome.KEY_REUSED)
