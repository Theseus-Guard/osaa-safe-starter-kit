import csv
import io
import pathlib
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from safe_kit import api, auth, privacy
from safe_kit.privacy import CATALOG, CSV_COLUMNS, inventory_csv_rows
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.users import User

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
CREATE_TABLE = re.compile(r"CREATE TABLE (\w+)", re.IGNORECASE)


def migrated_tables() -> set[str]:
    tables = set()
    for path in MIGRATIONS.glob("*.sql"):
        tables.update(CREATE_TABLE.findall(path.read_text(encoding="utf-8")))
    return tables


def test_every_migrated_table_is_in_the_inventory():
    # A new table without an inventory entry fails here, so personal data cannot be added unnoticed.
    # schema_migrations is created by the migration runner itself, not by a migration file.
    assert migrated_tables() | {"schema_migrations"} == {entry.table for entry in CATALOG}


def test_every_entry_is_complete():
    for entry in CATALOG:
        assert entry.description and entry.purpose and entry.retention and entry.security and entry.deletion_mode
        if entry.contains_personal_data:
            assert entry.personal_fields, entry.table
        else:
            assert not entry.personal_fields, entry.table


def test_every_table_has_a_retention_period_shown_from_the_setting(monkeypatch):
    # The period comes from SAFE_RETENTION_YEARS for every table. Nothing reports "undefined" any more.
    monkeypatch.setattr(privacy, "table_stats", lambda entry: privacy.RowStats(0, 0, 0, None, None))
    items = privacy.inventory(5)

    assert len(items) == len(CATALOG)
    for item in items:
        assert item["retention_years"] == 5, item["table"]
        assert item["retention_defined"] is True, item["table"]
        assert item["retention"].startswith("5 years, provisional and awaiting legal review."), item["table"]
        assert "undefined" not in item["retention"].lower(), item["table"]
        assert "not defined" not in item["retention"].lower(), item["table"]


def test_retention_period_uses_the_singular_for_one_year(monkeypatch):
    monkeypatch.setattr(privacy, "table_stats", lambda entry: privacy.RowStats(0, 0, 0, None, None))
    items = privacy.inventory(1)

    assert all(item["retention"].startswith("1 year, provisional") for item in items)


def test_csv_rows_match_the_header():
    items = [
        {
            "table": "t", "description": "d", "category": "c", "contains_personal_data": True,
            "personal_fields": ["a", "b"], "purpose": "p", "storage": "s", "retention": "r",
            "retention_defined": False, "security": "x", "deletion_mode": "m",
            "rows": {"total": 3, "active": 2, "soft_deleted": 1},
            "oldest_created_at": None, "newest_created_at": None,
        }
    ]
    row = inventory_csv_rows(items)[0]
    assert len(row) == len(CSV_COLUMNS)


def _full_item(entry):
    return {
        "table": entry.table, "description": entry.description, "category": entry.category,
        "contains_personal_data": entry.contains_personal_data, "personal_fields": list(entry.personal_fields),
        "purpose": entry.purpose, "storage": f"PostgreSQL table {entry.table}", "retention": entry.retention,
        "retention_defined": entry.retention_defined, "security": entry.security,
        "deletion_mode": entry.deletion_mode, "rows": {"total": 0, "active": 0, "soft_deleted": 0},
        "oldest_created_at": None, "newest_created_at": None,
    }


@pytest.fixture
def client(monkeypatch):
    actor = {"user": User(
        id="33333333-3333-3333-3333-333333333333", username="admin-x", role="admin", is_active=True,
        password_hash="unused", sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False, created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )}
    api.app.dependency_overrides[auth.current_user] = lambda: actor["user"]
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    monkeypatch.setattr(api, "inventory", lambda years: [_full_item(entry) for entry in CATALOG])
    yield TestClient(api.app), actor
    api.app.dependency_overrides.clear()


def test_admin_reads_inventory(client):
    test_client, _ = client
    response = test_client.get("/api/v1/privacy/inventory")
    assert response.status_code == 200
    assert response.json()["items"][0]["table"] == "users"


def test_inventory_reports_the_retention_period_from_the_setting(client):
    test_client, _ = client
    body = test_client.get("/api/v1/privacy/inventory").json()
    assert body["retention_years"] == 5


def test_inventory_export_json(client):
    test_client, _ = client
    response = test_client.get("/api/v1/privacy/inventory/export?format=json")
    assert response.status_code == 200
    assert "data-inventory.json" in response.headers["content-disposition"]


def test_inventory_export_csv(client):
    test_client, _ = client
    response = test_client.get("/api/v1/privacy/inventory/export?format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    reader = list(csv.reader(io.StringIO(response.text)))
    assert reader[0] == list(CSV_COLUMNS)


def test_export_rejects_unknown_format(client):
    test_client, _ = client
    assert test_client.get("/api/v1/privacy/inventory/export?format=xml").status_code == 400


def test_non_admin_cannot_read_inventory(client):
    test_client, actor = client
    actor["user"] = User(
        id="44444444-4444-4444-4444-444444444444", username="tina", role="triager", is_active=True,
        password_hash="unused", sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False, created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert test_client.get("/api/v1/privacy/inventory").status_code == 403
