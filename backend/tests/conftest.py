"""Shared test fixtures.

timezone_db replaces the database connection of the timezone and case modules with an in-memory fake. The fake
answers only the statements those modules issue, matched by their SQL text, and fails loudly on anything else.
"""

import itertools
import os
import uuid
from datetime import UTC, datetime

import pytest

from safe_kit import api, cases, system_timezone


# The token and session settings are required in every process (no defaults). Tests use the values that compose.yaml sets.
TOKEN_SETTINGS = {
    "SAFE_ACCESS_TTL_MINUTES": "60",
    "SAFE_REFRESH_TTL_MINUTES": "60",
    "SAFE_SESSION_MAX_HOURS": "12",
    "SAFE_RETENTION_YEARS": "5",
}


@pytest.fixture(autouse=True)
def token_settings(monkeypatch) -> None:
    for name, value in TOKEN_SETTINGS.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def impact_values_without_database(monkeypatch) -> None:
    """Case responses read the impact values from the database. Without a database, the API tests see no values, so
    every risk score is not set. Database tests (SAFE_TEST_DATABASE=1) keep the real query, and the impact tests
    replace it themselves when they need values."""
    if os.environ.get("SAFE_TEST_DATABASE") == "1":
        return
    monkeypatch.setattr(api, "current_scores_for_cases", lambda case_ids: {})


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeTimezoneDatabase:
    def __init__(self):
        self.timezone_rows: list[dict] = []
        self.audit_rows: list[dict] = []
        self.case_rows: list[tuple] = []
        self.deadline_rows: list[tuple] = []
        self._sequence = itertools.count(1)

    def connect(self):
        return _Connection(self)

    def seed_timezone(
        self, timezone: str, effective_from: datetime, set_by: str | None = None
    ) -> None:
        self.timezone_rows.append(
            {
                "id": str(uuid.uuid4()),
                "timezone": timezone,
                "effective_from": effective_from,
                "set_by": set_by,
            }
        )

    def current_timezone(self) -> str | None:
        """The timezone in effect now, read the way the application reads it."""
        rows = [
            row
            for row in self.timezone_rows
            if row["effective_from"] <= datetime.now(UTC)
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: row["effective_from"])["timezone"]

    def run(self, sql: str, params: tuple):
        if "pg_advisory_xact_lock" in sql:
            return _Result([])
        if sql.startswith("SELECT timezone, effective_from FROM system_timezone"):
            rows = self.timezone_rows
            if "effective_from <= now()" in sql:
                rows = [
                    row for row in rows if row["effective_from"] <= datetime.now(UTC)
                ]
            if not rows:
                return _Result([])
            latest = max(rows, key=lambda row: row["effective_from"])
            return _Result([(latest["timezone"], latest["effective_from"])])
        if sql.startswith("INSERT INTO system_timezone"):
            timezone, set_by = params
            row = {
                "id": str(uuid.uuid4()),
                "timezone": timezone,
                "effective_from": datetime.now(UTC),
                "set_by": set_by,
            }
            self.timezone_rows.append(row)
            return _Result([(row["id"], row["timezone"], row["effective_from"])])
        if sql.startswith("INSERT INTO audit_events"):
            actor_id, action, target_type, target_id, details = params
            self.audit_rows.append(
                {
                    "actor_id": actor_id,
                    "action": action,
                    "target_type": target_type,
                    "target_id": target_id,
                    "details": details.obj,
                }
            )
            return _Result([])
        if sql == "SELECT nextval('case_reference_seq')":
            return _Result([(next(self._sequence),)])
        if sql.startswith("INSERT INTO cases"):
            (
                reference,
                title,
                awareness_time,
                created_by,
                calendar_version,
                calendar_timezone,
            ) = params
            row = (
                str(uuid.uuid4()),
                reference,
                title,
                awareness_time,
                "open",
                calendar_version,
                datetime.now(UTC),
                created_by,
                calendar_timezone,
                None,
                None,
                None,
                1,
            )
            self.case_rows.append(row)
            return _Result([row])
        if sql.startswith("INSERT INTO deadline_tasks"):
            self.deadline_rows.append(params)
            return _Result([(str(uuid.uuid4()),)])
        if sql.startswith("SELECT id::text, fingerprint, response_status"):
            return _Result([])  # no stored idempotency record: the request is new
        if sql.startswith("INSERT INTO idempotency_keys"):
            return _Result([])
        raise AssertionError(f"unexpected SQL in a test: {sql[:100]}")


class _Connection:
    def __init__(self, database: FakeTimezoneDatabase):
        self._database = database

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return self._database.run(" ".join(sql.split()), tuple(params or ()))


@pytest.fixture
def timezone_db(monkeypatch) -> FakeTimezoneDatabase:
    database = FakeTimezoneDatabase()
    monkeypatch.setattr(system_timezone, "connect", database.connect)
    monkeypatch.setattr(cases, "connect", database.connect)
    return database
