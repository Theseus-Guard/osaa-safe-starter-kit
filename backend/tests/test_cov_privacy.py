"""Coverage for the personal data inventory: row statistics, the inventory items, and the CSV export.

They need a PostgreSQL database with the SAFE_DB_* settings and a password file, and they run the migrations
first. They are skipped unless SAFE_TEST_DATABASE=1.
"""

import csv
import io
import os
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from safe_kit import migrate, sessions, users
from safe_kit.db import connect
from safe_kit.privacy import CATALOG, CSV_COLUMNS, inventory, inventory_csv_rows, table_stats
from safe_kit.security import hash_refresh_token

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    migrate.MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
    assert migrate.main() == 0


def entry_for(table: str):
    return next(entry for entry in CATALOG if entry.table == table)


def sql_count(table: str, condition: str = "TRUE") -> int:
    # The table names here are fixed by the test, never user input.
    with connect() as conn:
        return conn.execute(f"SELECT count(*) FROM {table} WHERE {condition}").fetchone()[0]


def csv_text(items: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    writer.writerows(inventory_csv_rows(items))
    return out.getvalue()


# --- table_stats ----------------------------------------------------------------------------------------------


def test_stats_for_a_table_with_soft_delete_separate_active_and_deleted_rows():
    entry = entry_for("users")
    before = table_stats(entry)

    user = users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", "reporter")
    users.soft_delete_user(user)
    after = table_stats(entry)

    assert after.total == before.total + 1
    assert after.active == before.active
    assert after.soft_deleted == before.soft_deleted + 1
    assert after.total == sql_count("users")
    assert after.active == sql_count("users", "NOT is_deleted")
    assert after.soft_deleted == after.total - after.active


def test_stats_for_a_table_without_soft_delete_count_every_row_as_active():
    entry = entry_for("schema_migrations")
    stats = table_stats(entry)

    assert stats.total == sql_count("schema_migrations")
    assert stats.total > 0, "the migrations just ran, so the ledger has rows"
    assert stats.active == stats.total
    assert stats.soft_deleted == 0
    assert stats.oldest_created_at is not None
    assert stats.oldest_created_at <= stats.newest_created_at


def test_timestamps_follow_the_newest_row():
    user = users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", "reporter")
    with connect() as conn:
        created_at = conn.execute("SELECT created_at FROM users WHERE id = %s", (user,)).fetchone()[0]

    stats = table_stats(entry_for("users"))

    assert stats.newest_created_at >= created_at
    assert stats.oldest_created_at <= stats.newest_created_at


def test_table_without_a_creation_column_reports_no_timestamps():
    entry = replace(entry_for("users"), created_column=None)
    stats = table_stats(entry)

    assert stats.total > 0
    assert stats.oldest_created_at is None
    assert stats.newest_created_at is None


# --- inventory ------------------------------------------------------------------------------------------------


def test_inventory_has_one_item_per_catalog_entry_with_counts_and_storage():
    items = inventory(5)

    assert [item["table"] for item in items] == [entry.table for entry in CATALOG]
    for item, entry in zip(items, CATALOG, strict=True):
        assert item["storage"] == f"PostgreSQL table {entry.table}"
        assert item["personal_fields"] == list(entry.personal_fields)
        assert "created_column" not in item
        rows = item["rows"]
        assert rows["total"] == rows["active"] + rows["soft_deleted"]


def test_inventory_row_counts_match_the_database():
    by_table = {item["table"]: item for item in inventory(5)}

    assert by_table["users"]["rows"]["total"] == sql_count("users")
    assert by_table["users"]["rows"]["active"] == sql_count("users", "NOT is_deleted")
    assert by_table["schema_migrations"]["rows"]["total"] == sql_count("schema_migrations")


# --- CSV export -----------------------------------------------------------------------------------------------


def test_csv_rows_have_one_row_per_table_and_match_the_header():
    items = inventory(5)
    rows = inventory_csv_rows(items)

    assert len(rows) == len(CATALOG)
    assert all(len(row) == len(CSV_COLUMNS) for row in rows)
    column = {name: index for index, name in enumerate(CSV_COLUMNS)}
    for row, item in zip(rows, items, strict=True):
        assert row[column["table"]] == item["table"]
        assert row[column["rows_total"]] == str(item["rows"]["total"])
        assert row[column["rows_soft_deleted"]] == str(item["rows"]["soft_deleted"])
        assert row[column["contains_personal_data"]] in ("true", "false")


def test_csv_export_never_contains_secret_values():
    # A real password hash and a real refresh token, stored in the database during this test.
    user = users.create_user("t-" + uuid.uuid4().hex[:12], "secret passphrase for the export test", "reporter")
    token = sessions.open_session(user)
    with connect() as conn:
        password_hash = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user,)).fetchone()[0]
    token_hash = hash_refresh_token(token)

    text = csv_text(inventory(5))

    assert "users" in text, "the export must contain the inventory, or the check below proves nothing"
    assert password_hash not in text
    assert "$argon2" not in text
    assert token not in text
    assert token_hash not in text
