"""System timezone: the one display timezone of this installation, kept as an append-only history.

Stored times are always UTC. The timezone only decides how times are shown. The server decides when a change takes
effect: effective_from is the database transaction time of the insert. Clients never supply an effective time.

Only an administrator sets the timezone. Until one is set, non-admin users cannot log in or use the API (see api.py and auth.py).
"""

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from safe_kit import audit
from safe_kit.db import connect

TIMEZONE_LOCK_KEY = 0x5AFE0005
# Only this role may set the timezone while it is unset, so it is the only role let through the setup gate.
SETUP_ROLE = "admin"


class InvalidTimezone(ValueError):
    """The name is not an IANA timezone known to the zoneinfo database."""


class TimezoneNotConfigured(Exception):
    """A case cannot be created until an administrator has set the system timezone."""


@dataclass(frozen=True)
class TimezoneSetting:
    timezone: str
    effective_from: datetime  # always timezone-aware UTC


class ChangeOutcome(enum.Enum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    STALE = "stale"
    CONFIRMATION_REQUIRED = "confirmation_required"


def validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ValueError, LookupError, OSError) as error:
        raise InvalidTimezone(f"unknown IANA timezone: {name}") from error
    return name


# The current timezone is the latest row that is already in effect. The write path reads the latest row without
# the time filter: a stale client must never be compared against an older row than the one committed last.
_CURRENT_SQL = """
    SELECT timezone, effective_from FROM system_timezone
    WHERE NOT is_deleted AND effective_from <= now()
    ORDER BY effective_from DESC, created_at DESC LIMIT 1
"""
_LATEST_SQL = """
    SELECT timezone, effective_from FROM system_timezone
    WHERE NOT is_deleted
    ORDER BY effective_from DESC, created_at DESC LIMIT 1
"""


def _setting(row: tuple | None) -> TimezoneSetting | None:
    if row is None:
        return None
    return TimezoneSetting(timezone=row[0], effective_from=row[1].astimezone(UTC))


def read_current(conn) -> TimezoneSetting | None:
    """The timezone in effect at the transaction time of the caller's connection."""
    return _setting(conn.execute(_CURRENT_SQL).fetchone())


def current_setting() -> TimezoneSetting | None:
    with connect() as conn:
        return read_current(conn)


def current_timezone_name() -> str | None:
    setting = current_setting()
    return setting.timezone if setting else None


def decide(stored: str | None, current: str | None, timezone: str, confirm: bool) -> ChangeOutcome:
    """The rules for a change request, given the stored timezone and what the client last saw."""
    if current != stored:
        return ChangeOutcome.STALE
    if stored == timezone:
        return ChangeOutcome.UNCHANGED
    if stored is not None and not confirm:
        return ChangeOutcome.CONFIRMATION_REQUIRED
    return ChangeOutcome.CHANGED


def change_timezone(
    conn,
    timezone: str,
    current: str | None,
    confirm: bool,
    actor_id: str,
) -> tuple[ChangeOutcome, TimezoneSetting | None]:
    """Apply one change on the caller's connection. The row and its audit event commit together."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (TIMEZONE_LOCK_KEY,))
    latest = _setting(conn.execute(_LATEST_SQL).fetchone())
    stored = latest.timezone if latest else None
    outcome = decide(stored, current, timezone, confirm)
    if outcome is not ChangeOutcome.CHANGED:
        return outcome, latest
    row = conn.execute(
        """
        INSERT INTO system_timezone (timezone, effective_from, set_by)
        VALUES (%s, now(), %s)
        RETURNING id::text, timezone, effective_from
        """,
        (timezone, actor_id),
    ).fetchone()
    new = TimezoneSetting(timezone=row[1], effective_from=row[2].astimezone(UTC))
    audit.record(
        conn,
        audit.SYSTEM_TIMEZONE_SET if stored is None else audit.SYSTEM_TIMEZONE_CHANGED,
        "system_timezone",
        row[0],
        actor_id=actor_id,
        details={"from": stored, "to": timezone, "effective_from": new.effective_from.isoformat()},
    )
    return outcome, new


def set_timezone(
    timezone: str,
    current: str | None,
    confirm: bool,
    actor_id: str,
) -> tuple[ChangeOutcome, TimezoneSetting | None]:
    with connect() as conn:
        return change_timezone(conn, timezone, current, confirm, actor_id)


def utc_offset(timezone: str, at: datetime) -> str:
    """The offset of the timezone at the given instant, for example "+03:00"."""
    offset = at.astimezone(ZoneInfo(timezone)).utcoffset()
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"
