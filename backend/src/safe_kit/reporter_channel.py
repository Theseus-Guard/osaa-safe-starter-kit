"""Reporter channel: the identity of the person who opened a case, kept away from the case content.

The reporter is an ordinary user account with the reporter role (cases:submit, cases:read_own). That account is the
only identity the system has for a report. It is stored twice: in cases.created_by, which decides who may read the
case, and in reporter_identities, next to the optional contact the reporter gave.

The reporter's account is never returned to a caller. The case response has no reporter field, and the case history
hides the actor of the case-creation entry for every caller, triagers included. Only the reporter can open a case, so
that actor is the reporter. Retaliation is a risk for the reporter, so the rule does not depend on the caller's role.

The contact is returned only by read_reporter_contact, to a caller with reporter_contact:read, and every read writes
an audit row in the same transaction. Reporter-channel cases are hidden from read_all callers until they are assigned
(see cases.CaseScope.visibility_sql).
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from enum import Enum

from safe_kit import audit, cases
from safe_kit.cases import Case, CaseScope, HistoryEntry
from safe_kit.db import connect

# Written for every successful read of a reporter contact. The row targets the case, so the case history shows it.
REPORTER_CONTACT_READ = "reporter_contact.read"


class ContactOutcome(Enum):
    OK = "ok"
    # The case does not exist, is deleted, or is not visible to the caller.
    NOT_FOUND = "not_found"
    # The case is visible but was not opened through the reporter channel, so it has no reporter contact.
    NO_REPORTER = "no_reporter"


def submit_report(title: str, awareness_time: datetime, contact: str | None, reporter_id: str) -> Case:
    """Create the case and its reporter identity in one transaction. The case is created the same way as any other."""
    with connect() as conn:
        case = cases.insert_case(conn, title, awareness_time, reporter_id)
        conn.execute(
            "INSERT INTO reporter_identities (case_id, reporter_id, contact) VALUES (%s, %s, %s)",
            (case.id, reporter_id, contact),
        )
    return case


def read_reporter_contact(case_id: str, scope: CaseScope) -> tuple[ContactOutcome, str | None]:
    """The contact the reporter gave for an active case, and an audit row for the read.

    The visibility check and the audit row share one transaction, so a read is never logged without its case check.
    The caller is checked for reporter_contact:read by the route, before this function runs.
    """
    with connect() as conn:
        if not cases.is_visible_on(conn, case_id, scope):
            return ContactOutcome.NOT_FOUND, None
        row = conn.execute(
            "SELECT contact FROM reporter_identities WHERE case_id = %s AND NOT is_deleted",
            (case_id,),
        ).fetchone()
        if row is None:
            return ContactOutcome.NO_REPORTER, None
        audit.record(conn, REPORTER_CONTACT_READ, "case", case_id, actor_id=scope.user_id)
    return ContactOutcome.OK, row[0]


def hide_reporter(entries: Sequence[HistoryEntry]) -> list[HistoryEntry]:
    """Blank the actor of each case-creation entry. The action, time, and details stay."""
    return [replace(entry, actor_id=None) if entry.action == audit.CASE_CREATE else entry for entry in entries]
