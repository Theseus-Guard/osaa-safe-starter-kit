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
        description="Accounts that can sign in to this installation, with their name, title, and email.",
        category="identity",
        contains_personal_data=True,
        personal_fields=(
            "username",
            "password_hash",
            "last session cutoff",
            "first_name",
            "last_name",
            "title",
            "email",
        ),
        purpose="Authentication and authorization of people who use the system.",
        retention="Kept while the account exists. Soft-deleted accounts remain in the table. No time-based expiry is defined.",
        retention_defined=False,
        security=(
            "Passwords stored as Argon2id hashes only. Login is rate limited. Listing requires users:manage. "
            "Email is validated and unique among active accounts, ignoring case. Profile changes are audited with field names only."
        ),
        deletion_mode="Soft delete: is_deleted and deleted_at are set, the account is disabled, and its username stays reserved.",
        created_column="created_at",
    ),
    TableEntry(
        table="password_history",
        description="The password hash of every password an account has had, temporary ones included.",
        category="identity",
        contains_personal_data=True,
        personal_fields=("password_hash",),
        purpose="Refusing a new password that matches one of the account's last three passwords.",
        retention="Kept while the account exists. No time-based expiry is defined.",
        retention_defined=False,
        security="Argon2id hashes only, never plaintext. Not returned by any API. Written only with the password change in the same transaction.",
        deletion_mode="Soft delete (is_deleted, deleted_at). Soft-deleting an account keeps its history rows.",
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
        personal_fields=(
            "actor_id (links to a person)",
            "target_id (may identify a user or case)",
            "username (login_failed records only)",
            "client_ip (login_failed records only)",
        ),
        purpose="Accountability for changes to users, roles, passwords, and the system timezone, and for failed logins.",
        retention="Kept until a retention period is defined. No time-based expiry is defined.",
        retention_defined=False,
        security="Append-only: a database trigger rejects every change except a single soft delete. Details hold identifiers only, never secrets. Usernames and client IP addresses are kept for login_failed only. Audit retention is not defined yet.",
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
        table="system_timezone",
        description="History of the system timezone that displays times in this installation.",
        category="configuration",
        contains_personal_data=True,
        personal_fields=("set_by (links to a person)",),
        purpose="Showing times in the local timezone of the organization. Stored times stay in UTC.",
        retention="Append-only history, kept until a retention period is defined. No time-based expiry is defined.",
        retention_defined=False,
        security="Rows are inserted, never updated. Changing the timezone requires users:manage and is audited.",
        deletion_mode="Not deleted by the application. Soft delete columns exist for consistency only.",
        created_column="created_at",
    ),
    TableEntry(
        table="cases",
        description="Reported AI incident cases.",
        category="case",
        contains_personal_data=True,
        personal_fields=(
            "title (may name people or organizations)",
            "created_by (links to a person)",
            "awareness_time",
            "closed_by (links to a person)",
            "close_reason (free text, may name people)",
        ),
        purpose="Recording an incident, the time the reporting organization became aware of it, and its closure.",
        retention="Kept until a retention period is defined. Legal hold rules are not yet implemented.",
        retention_defined=False,
        security=(
            "Visible to the creator (cases:read_own), to triagers (cases:read_all), and to investigators and reviewers "
            "with an active assignment (cases:read_assigned). A case opened through the reporter intake is hidden from "
            "triagers until it has an active assignment; holders of reporter_contact:read always see it. "
            "Administrators have no case permissions."
        ),
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
        security="Visible only through the owning case, with the same visibility rules as the case.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="case_assignments",
        description="Investigator and reviewer assignments to each case.",
        category="case",
        contains_personal_data=True,
        personal_fields=("user_id (links to a person)", "assigned_by (links to a person)"),
        purpose="Deciding who may read and work on a case, and recording who assigned them.",
        retention="Kept with its case. Removed assignments remain as soft-deleted rows.",
        retention_defined=False,
        security="Listed to every caller who can see the case. Created and removed only by holders of cases:assign. Administrators have no case permissions.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="case_reviews",
        description="F1 whole-case reviews: outcome and an optional note. Read-only history; each row is copied into review_rounds by migration 0030.",
        category="case",
        contains_personal_data=True,
        personal_fields=("reviewer_id (links to a person)", "note (free text, may name people)"),
        purpose="History of the reviews recorded before review rounds existed.",
        retention="Kept with its case. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Listed through the owning case. The note text is returned only to callers with cases:read_all; other callers see the outcome and time. No endpoint writes it any more.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="idempotency_keys",
        description="Idempotency-Key records of side-effect POST requests: the caller's key, a fingerprint of the request, and the stored response.",
        category="operations",
        contains_personal_data=True,
        personal_fields=("principal_id (links to a person)", "response_body (the stored answer, which may name a case)"),
        purpose="Returning the first answer to a repeated request, so that a retry does not write twice.",
        retention="Replay window of 7 days. An older record is soft-deleted and the request counts as new.",
        retention_defined=True,
        security="Read and written only by the request that owns the key. Never listed to other callers.",
        deletion_mode="Soft delete (is_deleted, deleted_at), after the replay window.",
        created_column="created_at",
    ),
    TableEntry(
        table="review_rounds",
        description="A review of a case, opened by an assigned reviewer. Holds the revision and the open or closed state.",
        category="case",
        contains_personal_data=True,
        personal_fields=("opened_by (links to a person)",),
        purpose="Grouping the layer decisions of one review of a case.",
        retention="Kept with its case. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Listed through the owning case. Visible to every caller who can see the case. Opened by holders of reviews:write who are assigned to the case.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="review_layers",
        description="One layer of a review round: the outcome, the reason, and the evidence references.",
        category="case",
        contains_personal_data=True,
        personal_fields=("set_by (links to a person)", "reason (free text, may name people)"),
        purpose="Recording the decision on each review layer and the reason for it.",
        retention="Kept with its review round. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Listed through the owning case. The reason text is returned only to callers with cases:read_all, or to the caller who set the layer. Evidence references are identifiers only. Set by assigned reviewers (reviews:write).",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="case_links",
        description="A record that two cases concern the same incident. Links only; no task, deadline or status changes.",
        category="case",
        contains_personal_data=True,
        personal_fields=("created_by (links to a person)",),
        purpose="Showing that two cases concern the same incident, so the handlers can see the related case.",
        retention="Kept with its cases. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Listed through the owning case. Shows the other case's ID and title only to callers who may see that case. Written by holders of cases:triage.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="awareness_events",
        description="Facts that made the organization aware of a case: the operator, an affected party, or an external report, with its time and basis.",
        category="case",
        contains_personal_data=True,
        personal_fields=("recorded_by (links to a person)", "basis (free text, may name people)"),
        purpose="Recording when and how the organization became aware, which starts the notification clocks.",
        retention="Kept with its case. Append-only. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Listed through the owning case. The basis text is returned only to callers with cases:read_all. Written by holders of cases:triage.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="impacts",
        description="Assessment of each impact area of a case: scope text and severity, with an explicit unknown value.",
        category="case",
        contains_personal_data=True,
        personal_fields=("updated_by (links to a person)", "scope (free text, may name affected people or groups)"),
        purpose="Recording who and what is affected, and how badly, for the notification and the preliminary report.",
        retention="Kept with its case. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Listed through the owning case. The scope text is returned only to callers with cases:read_all. Written by holders of cases:triage.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="legal_duties",
        description="Statutory or contractual duties attached to a case, with their own due times.",
        category="case",
        contains_personal_data=True,
        personal_fields=("created_by (links to a person)", "description (free text, may name people)", "jurisdiction (may name a place)"),
        purpose="Tracking legal and contractual duties separately from the SAFE deadlines.",
        retention="Kept with its case. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Listed through the owning case. Description, source, and jurisdiction are returned only to callers with cases:read_all. Written by holders of cases:triage. Changes are audited.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="case_classifications",
        description="Versions of the classification of a case: trigger values, event kind, rationale, scope, containment, and next update.",
        category="case",
        contains_personal_data=True,
        personal_fields=(
            "created_by (links to a person)",
            "free-text fields (rationale, responsible operator, scope, containment, evidence gap plan) may name people",
        ),
        purpose="Recording how the case is classified, and keeping every earlier version.",
        retention="Kept with its case. Versions are never edited. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Listed through the owning case. Free-text fields are returned only to callers with cases:read_all. Written by holders of cases:triage; each change is audited.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="evidence_items",
        description="Metadata of evidence files. The file content is an encrypted object in the evidence volume, not in this table.",
        category="case",
        contains_personal_data=True,
        personal_fields=(
            "original_filename (may contain personal data)",
            "uploaded_by (links to a person)",
            "case_id (links to a case)",
            "wrapped_data_key, nonce, key_id (needed to decrypt the stored content)",
        ),
        purpose="Storing evidence for a case with its checksums, so that the content can be decrypted and verified.",
        retention="Kept with its case. Evidence retention and legal hold rules are not yet defined.",
        retention_defined=False,
        security="Content is encrypted with a per-object data key. Only the wrapped data key is stored, never the key-encryption key. Visible only through a case the caller may see; reading needs evidence:read and uploads need evidence:write.",
        deletion_mode="Soft delete (is_deleted, deleted_at). The encrypted object is never removed by the application.",
        created_column="created_at",
    ),
    TableEntry(
        table="custody_events",
        description="Chain of custody: uploads, downloads, and failed integrity checks of evidence.",
        category="case",
        contains_personal_data=True,
        personal_fields=("actor_id (links to a person)", "evidence_id (links to an evidence item)"),
        purpose="Showing who handled each evidence item and whether its content passed the integrity checks.",
        retention="Append-only, kept with its evidence. Retention is not defined yet.",
        retention_defined=False,
        security="Append-only: a database trigger rejects every change except a single soft delete. Details hold reasons only, never content, keys, or file names.",
        deletion_mode="Soft delete once (is_deleted, deleted_at). Content is never changed.",
        created_column="created_at",
    ),
    TableEntry(
        table="case_status_changes",
        description="Each closure and reopening of a case: the action, the reason as given, and who did it.",
        category="case",
        contains_personal_data=True,
        personal_fields=("actor_id (links to a person)", "reason (free text, may name people)"),
        purpose="Keeping the reason for each closure and reopening, so that it can be read by triagers.",
        retention="Kept with its case. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Visible through the owning case. The reason is returned in the case history and in the case response only to callers with cases:read_all.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="case_import_records",
        description="Imported case records: the source case ID, a fingerprint of the record as received, and the whole record.",
        category="case",
        contains_personal_data=True,
        personal_fields=(
            "payload (the record as received; may name people, organizations, or contact references)",
            "imported_by (links to a person)",
        ),
        purpose="Telling a repeated import (duplicate) from a different record with the same ID (conflict), and keeping every received field.",
        retention="Kept with its case. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Written only by the import. No read endpoint exists yet; any future read must follow the case visibility rules. The payload never holds a contact value: the schema allows only a reference.",
        deletion_mode="Soft delete (is_deleted, deleted_at).",
        created_column="created_at",
    ),
    TableEntry(
        table="reporter_identities",
        description="Reporter of each case opened through the intake: the submitting account and an optional contact.",
        category="case",
        contains_personal_data=True,
        personal_fields=("reporter_id (links to a person)", "contact (how to reach the reporter)"),
        purpose="Keeping the reporter's identity apart from the case content, so the reporter is not exposed to retaliation.",
        retention="Kept with its case. Follows the case retention once it is defined.",
        retention_defined=False,
        security="Written only when a case is submitted through the reporter intake. Read only through GET /api/v1/reports/{case_id}/contact with reporter_contact:read, which is granted to no role by default and is audited. The contact is readable while the case is open, or within 72 hours after its latest closure; a later read is refused and not audited. The case history hides the reporter's account from every caller.",
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
