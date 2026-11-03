"""Migration rehearsal (expand, contract) and rollback check.

Each test builds a scratch database at an earlier migration state, writes rows the way the previous release wrote them
(raw SQL on that schema), applies the newer migrations with the real runner, and checks what the current code reads.

What is covered:
- The additive pair 0049 and 0050, the two newest migrations: existing rows survive, the new revision columns are
  backfilled to 1, the current read paths work on the migrated rows, and an approval written the previous way is refused
  by the NOT VALID check that 0049 adds.
- 0039, the newest migration that drops and re-adds a constraint: rows survive, and the manual reverse of the step is
  refused while a row already uses the wider value.
- Rollback: no migration has a down script, and the migrations are forward only. The rollback that is exercised is a
  restore of a copy taken before the migration (CREATE DATABASE ... TEMPLATE). That proves the schema and the data come
  back as they were. It does not prove a backup pipeline; the repository has none yet (README: backup and restore
  procedures are not implemented).

The tests need a PostgreSQL server and are skipped unless SAFE_TEST_DATABASE=1, as the other database tests are. Each
test creates its own scratch databases, with a random name, and drops them at teardown. On the server named by
SAFE_DB_NAME, the tests run only CREATE DATABASE and DROP DATABASE for those names. They do not delete rows.
"""

import hashlib
import os
import shutil
import uuid
from pathlib import Path

import psycopg
import pytest

from safe_kit import cases, case_records, disclosure, migrate, users
from safe_kit.cases import CaseScope
from safe_kit.db import connect

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
DIGEST = hashlib.sha256(b"rehearsal").hexdigest()

# Read by the rehearsal before and after each migration. Only columns that exist at the previous state are selected,
# so the same query returns the same rows on both sides of the migration.
SNAPSHOT_QUERIES = {
    "users": "SELECT id::text, username, role, password_hash FROM users ORDER BY id",
    "cases": "SELECT id::text, reference, title, status, calendar_timezone FROM cases ORDER BY id",
    "deadline_tasks": "SELECT id::text, case_id::text, kind, due_at, status FROM deadline_tasks ORDER BY id",
    "legal_duties": (
        "SELECT id::text, case_id::text, kind, description, due_at, status, created_by::text "
        "FROM legal_duties ORDER BY id"
    ),
    "disclosure_approvals": (
        "SELECT id::text, version_id::text, recipient, digest, preparer_id::text, approved_by::text "
        "FROM disclosure_approvals ORDER BY id"
    ),
    "idempotency_keys": (
        "SELECT id::text, principal_id::text, method, route, key, fingerprint FROM idempotency_keys ORDER BY id"
    ),
}


def migrations_up_to(number: int, target: Path) -> Path:
    """A migrations directory holding the real files up to and including migration `number`."""
    target.mkdir()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if int(path.name[:4]) <= number:
            shutil.copy(path, target / path.name)
    return target


class Server:
    """The maintenance connection to the server named by SAFE_DB_NAME. It creates and drops scratch databases only."""

    def __init__(self, admin: psycopg.Connection, monkeypatch: pytest.MonkeyPatch):
        self.admin = admin
        self.monkeypatch = monkeypatch
        self.created: list[str] = []

    def create(self, template: str | None = None) -> str:
        name = "safe_rehearsal_" + uuid.uuid4().hex[:16]
        clause = f' TEMPLATE "{template}"' if template else ""
        self.admin.execute(f'CREATE DATABASE "{name}"{clause}')
        self.created.append(name)
        return name

    def use(self, name: str) -> None:
        """Point the SAFE_DB_* settings, which safe_kit.db.connect reads, at a scratch database."""
        self.monkeypatch.setenv("SAFE_DB_NAME", name)

    def migrate(self, directory: Path) -> int:
        self.monkeypatch.setattr(migrate, "MIGRATIONS_DIR", directory)
        return migrate.main()

    def drop_all(self) -> None:
        for name in reversed(self.created):
            self.admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def server(monkeypatch):
    admin = connect()
    admin.autocommit = True
    srv = Server(admin, monkeypatch)
    yield srv
    srv.drop_all()
    admin.close()


def snapshot(*tables: str) -> dict[str, list]:
    """The rows of the given tables (all of them when none is named), as the current code reads them."""
    wanted = tables or tuple(SNAPSHOT_QUERIES)
    with connect() as conn:
        return {
            table: conn.execute(SNAPSHOT_QUERIES[table]).fetchall() for table in wanted
        }


def insert_user(conn: psycopg.Connection, username: str, role: str) -> str:
    # password_changed_at has no default from migration 0019 on, so the previous release always wrote it.
    return conn.execute(
        "INSERT INTO users (username, password_hash, role, password_changed_at) "
        "VALUES (%s, 'rehearsal-hash', %s, now()) RETURNING id::text",
        (username, role),
    ).fetchone()[0]


def seed_previous_release() -> dict[str, str]:
    """Rows written by the previous release, with raw SQL on the schema of migration 0048."""
    with connect() as conn:
        triager = insert_user(conn, "rehearsal-triager", "triager")
        approver = insert_user(conn, "rehearsal-approver", "disclosure_approver")
        case = conn.execute(
            "INSERT INTO cases (reference, title, awareness_time, created_by, status, calendar_version, "
            "calendar_timezone) VALUES ('REH-0001', 'Rehearsal case', '2026-11-01T09:00:00+03', %s, 'open', "
            "'rehearsal-calendar', 'Europe/Istanbul') RETURNING id::text",
            (triager,),
        ).fetchone()[0]
        for kind, status in (
            ("preliminary_safe_report", "open"),
            ("weekly_update", "done"),
        ):
            conn.execute(
                "INSERT INTO deadline_tasks (case_id, kind, due_at, status) "
                "VALUES (%s, %s, '2026-11-02T09:00:00+03', %s)",
                (case, kind, status),
            )
        duty = conn.execute(
            "INSERT INTO legal_duties (case_id, kind, description, source_reference, due_at, status, created_by) "
            "VALUES (%s, 'statutory', 'Rehearsal duty', 'REH-SOURCE-1', '2026-11-05T09:00:00+03', 'open', %s) "
            "RETURNING id::text",
            (case, triager),
        ).fetchone()[0]
        package = conn.execute(
            "INSERT INTO disclosure_packages (case_id, created_by) VALUES (%s, %s) RETURNING id::text",
            (case, triager),
        ).fetchone()[0]
        version = conn.execute(
            "INSERT INTO disclosure_versions (package_id, case_id, version, template, tlp_audience, recipient, "
            "purpose, manifest, digest, created_by) VALUES (%s, %s, 1, 'confidential_rapid_alert', 'AMBER', "
            "'Member CERT', 'Rehearsal purpose', '{}'::jsonb, %s, %s) RETURNING id::text",
            (package, case, DIGEST, triager),
        ).fetchone()[0]
        approval = conn.execute(
            "INSERT INTO disclosure_approvals (version_id, package_id, case_id, recipient, purpose, digest, "
            "preparer_id, approved_by, separation_rule) VALUES (%s, %s, %s, 'Member CERT', 'Rehearsal purpose', "
            "%s, %s, %s, 'preparer_differs_from_approver') RETURNING id::text",
            (version, package, case, DIGEST, triager, approver),
        ).fetchone()[0]
    return {
        "triager": triager,
        "approver": approver,
        "case": case,
        "duty": duty,
        "package": package,
        "version": version,
        "approval": approval,
    }


def at_previous_release(server: Server, tmp_path: Path, number: int) -> str:
    """A scratch database migrated to `number` and nothing newer. Returns its name."""
    name = server.create()
    server.use(name)
    assert server.migrate(migrations_up_to(number, tmp_path / f"upto-{number}")) == 0
    return name


def test_expand_pair_0049_0050_keeps_rows_and_current_reads_work(server, tmp_path):
    at_previous_release(server, tmp_path, 48)
    ids = seed_previous_release()
    before = snapshot()

    assert server.migrate(migrations_up_to(50, tmp_path / "head-50")) == 0

    # No row is lost or changed by the expand step.
    assert snapshot() == before

    # The revision columns that 0050 adds are backfilled to 1 on the existing rows.
    with connect() as conn:
        for table in ("users", "deadline_tasks", "legal_duties"):
            assert conn.execute(
                f"SELECT DISTINCT revision FROM {table}"
            ).fetchall() == [(1,)], table
        # 0049 adds recipient_ref with no value: the approval written before it keeps NULL.
        recipient_ref = conn.execute(
            "SELECT recipient_ref FROM disclosure_approvals WHERE id = %s",
            (ids["approval"],),
        ).fetchone()[0]
    assert recipient_ref is None

    # The current read paths work on the migrated rows.
    scope = CaseScope(user_id=ids["triager"], read_all=True)
    listed = {user.username: user for user in users.list_users(limit=100)}
    assert listed["rehearsal-triager"].revision == 1
    duties = case_records.list_legal_duties(ids["case"], scope)
    assert duties is not None
    assert [duty.revision for duty in duties] == [1]
    with connect() as conn:
        tasks = cases._load_tasks(conn, ids["case"])
    assert sorted(task.kind for task in tasks) == [
        "preliminary_safe_report",
        "weekly_update",
    ]
    assert all(task.revision == 1 for task in tasks)
    version = disclosure.get_version(ids["case"], ids["version"], scope)
    assert version is not None
    assert version.approval is not None
    assert version.approval.recipient_ref is None

    # A row written the previous way still fits: the new revision column takes its default.
    with connect() as conn:
        conn.execute(
            "INSERT INTO legal_duties (case_id, kind, description, source_reference, due_at, status, created_by) "
            "VALUES (%s, 'contractual', 'Second duty', 'REH-SOURCE-2', '2026-11-06T09:00:00+03', 'open', %s)",
            (ids["case"], ids["triager"]),
        )
        revisions = conn.execute(
            "SELECT revision FROM legal_duties WHERE description = 'Second duty'"
        ).fetchall()
    assert revisions == [(1,)]

    # Re-running the runner on the migrated database applies nothing and still passes the checksum check.
    assert server.migrate(MIGRATIONS) == 0


def test_after_expand_an_approval_written_the_previous_way_is_refused(server, tmp_path):
    at_previous_release(server, tmp_path, 48)
    ids = seed_previous_release()
    assert server.migrate(MIGRATIONS) == 0

    with connect() as conn:
        version = conn.execute(
            "INSERT INTO disclosure_versions (package_id, case_id, version, template, tlp_audience, recipient, "
            "purpose, manifest, digest, created_by) VALUES (%s, %s, 2, 'confidential_rapid_alert', 'AMBER', "
            "'Member CERT', 'Rehearsal purpose', '{}'::jsonb, %s, %s) RETURNING id::text",
            (ids["package"], ids["case"], DIGEST, ids["triager"]),
        ).fetchone()[0]

    # An approval that names no recipient_ref (the form the previous release writes) is refused for a new row.
    with pytest.raises(psycopg.errors.CheckViolation) as refused:
        with connect() as conn:
            conn.execute(
                "INSERT INTO disclosure_approvals (version_id, package_id, case_id, recipient, purpose, digest, "
                "preparer_id, approved_by, separation_rule) VALUES (%s, %s, %s, 'Member CERT', 'Rehearsal purpose', "
                "%s, %s, %s, 'preparer_differs_from_approver')",
                (
                    version,
                    ids["package"],
                    ids["case"],
                    DIGEST,
                    ids["triager"],
                    ids["approver"],
                ),
            )
    assert (
        refused.value.diag.constraint_name == "disclosure_approvals_recipient_ref_set"
    )


def test_contract_step_0039_keeps_rows_and_its_reverse_needs_the_rows_gone(
    server, tmp_path
):
    at_previous_release(server, tmp_path, 38)
    with connect() as conn:  # the previous release writes only POST keys
        principal = insert_user(conn, "rehearsal-poster", "triager")
        conn.execute(
            "INSERT INTO idempotency_keys (principal_id, method, route, key, fingerprint, response_status, "
            "response_body) VALUES (%s, 'POST', '/api/v1/cases', 'rehearsal-key-1', %s, 201, '{}'::jsonb)",
            (principal, DIGEST),
        )
    before = snapshot("users", "idempotency_keys")

    assert server.migrate(migrations_up_to(39, tmp_path / "upto-39")) == 0
    assert snapshot("users", "idempotency_keys") == before

    # After the step, PUT and PATCH are accepted and another method is still refused.
    with connect() as conn:
        conn.execute(
            "INSERT INTO idempotency_keys (principal_id, method, route, key, fingerprint, response_status, "
            "response_body) VALUES (%s, 'PATCH', '/api/v1/cases/x/deadlines/y', 'rehearsal-key-2', %s, 200, '{}'::jsonb)",
            (principal, DIGEST),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect() as conn:
            conn.execute(
                "INSERT INTO idempotency_keys (principal_id, method, route, key, fingerprint, response_status, "
                "response_body) VALUES (%s, 'DELETE', '/api/v1/cases', 'rehearsal-key-3', %s, 200, '{}'::jsonb)",
                (principal, DIGEST),
            )

    # The reverse of 0039 (the POST-only list) cannot be applied while a PATCH row exists. The whole reverse runs in one
    # transaction, so the failed attempt leaves the wider constraint in place.
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect() as conn:
            conn.execute(
                "ALTER TABLE idempotency_keys DROP CONSTRAINT idempotency_keys_method_check"
            )
            conn.execute(
                "ALTER TABLE idempotency_keys ADD CONSTRAINT idempotency_keys_method_check CHECK (method IN ('POST'))"
            )
    with connect() as conn:
        methods = conn.execute(
            "SELECT method FROM idempotency_keys ORDER BY method"
        ).fetchall()
    assert methods == [("PATCH",), ("POST",)]


def test_rollback_is_a_restore_of_the_pre_migration_copy(server, tmp_path):
    source = at_previous_release(server, tmp_path, 48)
    ids = seed_previous_release()
    before = snapshot()

    # The copy taken before the migration stands in for the backup. No connection to the source is open now.
    copy = server.create(template=source)

    assert server.migrate(MIGRATIONS) == 0

    # Work done after the copy exists only on the migrated database.
    with connect() as conn:
        conn.execute(
            "INSERT INTO legal_duties (case_id, kind, description, source_reference, due_at, status, created_by) "
            "VALUES (%s, 'statutory', 'Written after the copy', 'REH-SOURCE-3', '2026-11-07T09:00:00+03', 'open', %s)",
            (ids["case"], ids["triager"]),
        )

    # Rollback: the application points at the restored copy, which runs the previous schema.
    server.use(copy)
    with connect() as conn:
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        revision_columns = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'revision'"
        ).fetchall()
    assert versions[-1].startswith("0048_")
    assert revision_columns == []
    assert snapshot() == before
    with connect() as conn:
        descriptions = [
            row[0]
            for row in conn.execute(
                "SELECT description FROM legal_duties WHERE NOT is_deleted ORDER BY description"
            ).fetchall()
        ]
    assert descriptions == ["Rehearsal duty"]

    # Roll forward again on the restored copy.
    assert server.migrate(migrations_up_to(50, tmp_path / "head-50")) == 0
    assert snapshot() == before
