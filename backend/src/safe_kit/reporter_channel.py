"""Reporter channel: the identity of the person who opened a case, kept away from the case content.

The reporter is an ordinary user account with the reporter role (cases:submit, cases:read_own). That account is the
only identity the system has for a report. It is stored twice: in cases.created_by, which decides who may read the
case, and in reporter_identities, next to the optional contact the reporter gave.

Nothing in the application returns the reporter's account or contact to a caller. The case response has no reporter
field, and the case history hides the actor of the case-creation entry for every caller, triagers included. Only the
reporter can open a case, so that actor is the reporter. Retaliation is a risk for the reporter, so the rule does not
depend on the caller's role.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from safe_kit import audit, cases
from safe_kit.cases import Case, HistoryEntry
from safe_kit.db import connect


def submit_report(title: str, awareness_time: datetime, contact: str | None, reporter_id: str) -> Case:
    """Create the case and its reporter identity in one transaction. The case is created the same way as any other."""
    with connect() as conn:
        case = cases.insert_case(conn, title, awareness_time, reporter_id)
        conn.execute(
            "INSERT INTO reporter_identities (case_id, reporter_id, contact) VALUES (%s, %s, %s)",
            (case.id, reporter_id, contact),
        )
    return case


def hide_reporter(entries: Sequence[HistoryEntry]) -> list[HistoryEntry]:
    """Blank the actor of each case-creation entry. The action, time, and details stay."""
    return [replace(entry, actor_id=None) if entry.action == audit.CASE_CREATE else entry for entry in entries]
