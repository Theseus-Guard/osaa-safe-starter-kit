"""Personal data inventory of this installation.

The catalog below describes every table in the schema: what it holds, why, how long it is kept, how it is
protected, and how deletion works. The live part counts rows and reads timestamps only. It never returns row
contents and never returns secrets such as password hashes or tokens.

Every table created by a migration must have an entry here. tests/test_privacy.py enforces this.
"""

from dataclasses import asdict, dataclass
from datetime import datetime

from psycopg import sql

from safe_kit.db import connect


@dataclass(frozen=True)
class TableEntry:
    table: str
    description: str
    category: str
    contains_personal_data: bool
    personal_fields: tuple[str, ...]
    purpose: str
    retention: str
    retention_defined: bool
    security: str
    deletion_mode: str
    created_column: str | None


CATALOG: tuple[TableEntry, ...] = (
    TableEntry(
        table="users",
        description="Accounts that can sign in to this installation.",
        category="identity",
        contains_personal_data=True,
        personal_fields=("username", "password_hash", "last session cutoff"),
        purpose="Authentication and authorization of people who use the system.",
        retention="Kept while the account exists. Soft-deleted accounts remain in the table. No time-based expiry is defined.",
        retention_defined=False,
        security="Passwords stored as Argon2id hashes only. Login is rate limited. Listing requires users:manage.",
        deletion_mode="Soft delete: is_deleted and deleted_at are set, the account is disabled, and its username stays reserved.",
        created_column="created_at",
    ),
    TableEntry(
        table="refresh_tokens",
        description="Refresh tokens of active sessions.",
        category="session",
        contains_personal_data=True,
        personal_fields=("user_id (links to a person)", "token_hash"),
        purpose="Renewing sessions without a new password login.",
        retention="Expired and revoked tokens remain in the table. No purge or anonymization is defined.",
        retention_defined=False,
        security="Only SHA-256 hashes of tokens are stored. Each token is single use; reuse revokes the whole family.",
        deletion_mode="Revoked by setting revoked_at. Rows are not removed.",
        created_column="created_at",
    ),
    TableEntry(
        table="revoked_access_tokens",
        description="Access tokens revoked by logout until they would have expired.",
        category="session",
        contains_personal_data=False,
        personal_fields=(),
        purpose="Making logout effective before the access token expires.",
        retention="Entries are soft-deleted once the token has expired. The cleanup runs during logout only.",
        retention_defined=False,
        security="Stores token identifiers (jti) only, not tokens. Checked on every authenticated request.",
        deletion_mode="Soft delete after expiry (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="rate_limits",
        description="Counters for login rate limiting.",
        category="security",
        contains_personal_data=True,
        personal_fields=("bucket (contains a username or a client IP address)",),
        purpose="Protecting login against brute-force attempts.",
        retention="Cleared when a login succeeds. Otherwise a window resets on the next attempt. No time-based purge is defined.",
        retention_defined=False,
        security="Fixed windows. Keys include client IP addresses, which are personal data.",
        deletion_mode="Soft delete when cleared (is_deleted, deleted_at); the next attempt reuses the row.",
        created_column="created_at",
    ),
    TableEntry(
        table="worker_heartbeat",
        description="Last heartbeat of the background worker.",
        category="operations",
        contains_personal_data=False,
        personal_fields=(),
        purpose="Showing that the worker is alive.",
        retention="One row per worker, overwritten on every heartbeat.",
        retention_defined=True,
        security="Contains no personal data.",
        deletion_mode="Overwritten in place.",
        created_column="created_at",
    ),
    TableEntry(
        table="permissions",
        description="Catalog of permission names.",
        category="configuration",
        contains_personal_data=False,
        personal_fields=(),
        purpose="Defining what a role may do.",
        retention="Kept for as long as the product uses the permission.",
        retention_defined=True,
        security="Changed only through migrations.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="role_permissions",
        description="Which permissions each role has.",
        category="configuration",
        contains_personal_data=False,
        personal_fields=(),
        purpose="Role-based access control.",
        retention="Kept for as long as the role exists. Revoking a permission keeps the row.",
        retention_defined=True,
        security="Changes require roles:manage. The admin role keeps its required permissions.",
        deletion_mode="Soft delete when a permission is revoked (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="schema_migrations",
        description="Ledger of applied database migrations.",
        category="operations",
        contains_personal_data=False,
        personal_fields=(),
        purpose="Keeping the database schema in step with the code.",
        retention="Kept permanently.",
        retention_defined=True,
        security="Written only by the migration runner. Checksums detect edited migrations.",
        deletion_mode="Not deleted.",
        created_column="applied_at",
    ),
    TableEntry(
        table="audit_events",
        description="Append-only record of administrative and security-relevant actions.",
        category="audit",
        contains_personal_data=True,
        personal_fields=("actor_id (links to a person)", "target_id (may identify a user or case)"),
        purpose="Accountability for changes to users, roles, and passwords.",
        retention="Kept until a retention period is defined. No time-based expiry is defined.",
        retention_defined=False,
        security="Append-only: a database trigger rejects every change except a single soft delete. Details must hold identifiers only, never secrets or IP addresses.",
        deletion_mode="Soft delete once (is_deleted, deleted_at). Content is never changed.",
        created_column="created_at",
    ),
    TableEntry(
        table="outbox_jobs",
        description="Durable background jobs waiting for, or finished by, the worker.",
        category="operations",
        contains_personal_data=True,
        personal_fields=("payload (depends on the job type; may reference people)",),
        purpose="Running side effects reliably after a database change is committed.",
        retention="Finished and failed jobs stay until a retention period is defined. No time-based purge is defined.",
        retention_defined=False,
        security="Payloads should carry identifiers, not personal data. Only the worker role processes jobs.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="cases",
        description="Reported AI incident cases.",
        category="case",
        contains_personal_data=True,
        personal_fields=("title (may name people or organizations)", "created_by (links to a person)", "awareness_time"),
        purpose="Recording an incident and the time the reporting organization became aware of it.",
        retention="Kept until a retention period is defined. Legal hold rules are not yet implemented.",
        retention_defined=False,
        security="Visible only to the creator (cases:read_own). Administrators have no case permissions.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="deadline_tasks",
        description="Notification deadlines calculated for each case.",
        category="case",
        contains_personal_data=False,
        personal_fields=(),
        purpose="Tracking required notifications and their due times.",
        retention="Kept with its case. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Visible only through the owning case.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
)


@dataclass(frozen=True)
class RowStats:
    total: int
    active: int
    soft_deleted: int
    oldest_created_at: datetime | None
    newest_created_at: datetime | None


def _has_soft_delete(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = 'is_deleted')",
        (table,),
    ).fetchone()
    return row[0]


def table_stats(entry: TableEntry) -> RowStats:
    """Count rows and read creation timestamps. Only catalog identifiers reach the SQL, never user input."""
    table = sql.Identifier(entry.table)
    with connect() as conn:
        if _has_soft_delete(conn, entry.table):
            totals = conn.execute(
                sql.SQL("SELECT count(*), count(*) FILTER (WHERE NOT is_deleted) FROM {}").format(table)
            ).fetchone()
        else:
            totals = conn.execute(sql.SQL("SELECT count(*), count(*) FROM {}").format(table)).fetchone()
        total, active = totals
        oldest = newest = None
        if entry.created_column:
            column = sql.Identifier(entry.created_column)
            oldest, newest = conn.execute(
                sql.SQL("SELECT min({c}), max({c}) FROM {t}").format(c=column, t=table)
            ).fetchone()
    return RowStats(
        total=total,
        active=active,
        soft_deleted=total - active,
        oldest_created_at=oldest,
        newest_created_at=newest,
    )


def inventory() -> list[dict]:
    items = []
    for entry in CATALOG:
        data = asdict(entry)
        data.pop("created_column")
        data["personal_fields"] = list(entry.personal_fields)
        data["storage"] = f"PostgreSQL table {entry.table}"
        stats = table_stats(entry)
        data["rows"] = {"total": stats.total, "active": stats.active, "soft_deleted": stats.soft_deleted}
        data["oldest_created_at"] = stats.oldest_created_at.isoformat() if stats.oldest_created_at else None
        data["newest_created_at"] = stats.newest_created_at.isoformat() if stats.newest_created_at else None
        items.append(data)
    return items


CSV_COLUMNS = (
    "table",
    "description",
    "category",
    "contains_personal_data",
    "personal_fields",
    "purpose",
    "storage",
    "retention",
    "retention_defined",
    "security",
    "deletion_mode",
    "rows_total",
    "rows_active",
    "rows_soft_deleted",
    "oldest_created_at",
    "newest_created_at",
)


def inventory_csv_rows(items: list[dict]) -> list[list[str]]:
    rows = []
    for item in items:
        rows.append(
            [
                item["table"],
                item["description"],
                item["category"],
                str(item["contains_personal_data"]).lower(),
                "; ".join(item["personal_fields"]),
                item["purpose"],
                item["storage"],
                item["retention"],
                str(item["retention_defined"]).lower(),
                item["security"],
                item["deletion_mode"],
                str(item["rows"]["total"]),
                str(item["rows"]["active"]),
                str(item["rows"]["soft_deleted"]),
                item["oldest_created_at"] or "",
                item["newest_created_at"] or "",
            ]
        )
    return rows
