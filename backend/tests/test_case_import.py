"""Basic import of case records in the project format.

Three layers, as in test_case_access.py:
- Tests without a database check the schema, the mapping, the envelope rules, and the HTTP status mapping.
  The service function is replaced.
- Tests marked requires_database run the import against PostgreSQL. They need SAFE_TEST_DATABASE=1 and the SAFE_DB_*
  settings. They create rows and never remove them, so use a throwaway database.
"""

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from safe_kit import api, auth, case_import, cases, users
from safe_kit.case_import import (
    MAX_ERRORS_PER_RECORD,
    MAX_IMPORT_BYTES,
    MAX_IMPORT_RECORDS,
    MAX_MESSAGE_LENGTH,
    RecordRejected,
    RecordResult,
    fingerprint,
    import_records,
    parse_import_file,
    prepare_record,
    validate_record,
)
from safe_kit.cases import CaseScope
from safe_kit.db import connect
from safe_kit.rbac import DEFAULT_ROLE_PERMISSIONS
from safe_kit.system_timezone import TimezoneNotConfigured
from safe_kit.users import User

ROOT = Path(__file__).resolve().parents[2]
ROOT_SCHEMA = ROOT / "schemas" / "case-minimum-record.v0.json"
EXAMPLES = ROOT / "schemas" / "examples"
REPORTER_ID = "11111111-1111-1111-1111-111111111111"
ADMIN_ID = "55555555-5555-5555-5555-555555555555"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
PAST_AWARENESS = "2026-10-30T09:00:00Z"
TITLE = "example-assistant (synthetic): out_of_scope_continuation"

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)


def idem() -> dict:
    """A fresh Idempotency-Key header for one request."""
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture(autouse=True)
def system_timezone_configured():
    """Import needs a system timezone. Set one on a freshly migrated test database."""
    if os.environ.get("SAFE_TEST_DATABASE") != "1":
        yield
        return
    from safe_kit.migrate import main as migrate

    migrate()  # idempotent: applies pending migrations on a fresh test database
    with connect() as conn:
        exists = conn.execute("SELECT EXISTS (SELECT 1 FROM system_timezone WHERE NOT is_deleted)").fetchone()[0]
        if not exists:
            conn.execute("INSERT INTO system_timezone (timezone) VALUES ('Europe/Istanbul')")
    yield


def example_record(**changes) -> dict:
    """The synthetic example, with a fresh case_id so repeated test runs do not collide."""
    record = json.loads((EXAMPLES / "valid-minimal.json").read_text(encoding="utf-8"))
    record["case_id"] = str(uuid.uuid4())
    record["timeline"]["awareness_time"]["value"] = PAST_AWARENESS
    record.update(changes)
    return record


def envelope(records: list) -> bytes:
    return json.dumps({"import_format_version": 1, "records": records}).encode("utf-8")


def make_user(user_id, role):
    return User(
        id=user_id,
        username=f"user-{role}",
        role=role,
        is_active=True,
        password_hash="unused",
        sessions_valid_after=datetime(1970, 1, 1, tzinfo=UTC),
        must_change_password=False,
        created_at=NOW,
    )


# --- schema and validation -------------------------------------------------------------------------------------


def test_bundled_schema_is_the_project_schema():
    # The container image reads the copy under src/; the repository keeps the reviewed original under schemas/.
    assert case_import.SCHEMA_PATH.read_bytes() == ROOT_SCHEMA.read_bytes()


def test_bundled_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(json.loads(case_import.SCHEMA_PATH.read_text(encoding="utf-8")))


def test_example_record_passes_the_schema():
    assert validate_record(example_record()) == []


def test_record_without_version_is_rejected_not_read_as_current():
    record = json.loads((EXAMPLES / "invalid-missing-version.json").read_text(encoding="utf-8"))
    errors = validate_record(record)
    assert any("record_schema_version" in error for error in errors)


def test_record_of_another_schema_version_is_rejected():
    errors = validate_record(example_record(record_schema_version="0.2.0"))
    assert any(error.startswith("/record_schema_version:") for error in errors)


def test_schema_errors_are_bounded_per_record():
    errors = validate_record({"unexpected": "x" * 5000})
    assert 0 < len(errors) <= MAX_ERRORS_PER_RECORD
    assert all(len(error) <= MAX_MESSAGE_LENGTH for error in errors)


def test_non_object_record_is_rejected():
    assert validate_record(["not", "an", "object"]) != []


# --- mapping ---------------------------------------------------------------------------------------------------


def test_mapping_takes_the_title_and_the_awareness_time_from_the_record():
    record = example_record()
    prepared = prepare_record(record, NOW)
    assert prepared.title == TITLE
    assert prepared.awareness_time == datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
    assert prepared.source_case_id == record["case_id"]
    assert prepared.payload == record


def test_title_is_cut_to_the_case_limit():
    record = example_record()
    record["affected_systems"][0]["system_name"] = "s" * 200
    assert len(prepare_record(record, NOW).title) == case_import.TITLE_LIMIT


def test_future_awareness_time_is_rejected():
    record = example_record(timeline={"awareness_time": {"value": "2026-11-03T09:00:00Z", "basis": "system_record", "source": "x"}})
    with pytest.raises(RecordRejected) as error:
        prepare_record(record, NOW)
    assert any("cannot be in the future" in line for line in error.value.errors)


def test_impossible_calendar_date_is_rejected_with_its_location():
    record = example_record(timeline={"awareness_time": {"value": "2026-02-30T09:00:00Z", "basis": "system_record", "source": "x"}})
    with pytest.raises(RecordRejected) as error:
        prepare_record(record, NOW)
    assert error.value.errors == ["/timeline/awareness_time/value: not a real UTC date and time"]


def test_blank_system_name_is_rejected_even_though_the_schema_allows_it():
    record = example_record()
    record["affected_systems"][0]["system_name"] = "   "
    with pytest.raises(RecordRejected) as error:
        prepare_record(record, NOW)
    assert error.value.errors == ["/affected_systems/0/system_name: must not be blank"]


def test_fingerprint_depends_on_content_not_on_key_order():
    record = example_record()
    reordered = dict(reversed(list(record.items())))
    assert fingerprint(record) == fingerprint(reordered)
    assert fingerprint(record) != fingerprint(example_record(narrative_summary="changed"))


# --- envelope ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, message",
    [
        (b"not json", "not valid JSON"),
        (b'{"import_format_version": 1, "records": [NaN]}', "not valid JSON"),
        (b"[]", "must be a JSON object"),
        (b'{"import_format_version": 1, "records": [], "extra": 1}', "field that this version does not accept"),
        (b'{"import_format_version": true, "records": []}', "unsupported import_format_version"),
        (b'{"import_format_version": 2, "records": []}', "unsupported import_format_version"),
        (b'{"import_format_version": "1", "records": []}', "unsupported import_format_version"),
        (b'{"import_format_version": 1}', "records must be a JSON array"),
        (b'{"import_format_version": 1, "records": {}}', "records must be a JSON array"),
    ],
)
def test_malformed_files_are_refused_as_a_whole(raw, message):
    with pytest.raises(case_import.ImportFileError, match=message):
        parse_import_file(raw)


def test_file_over_the_record_limit_is_refused():
    with pytest.raises(case_import.ImportFileError, match=str(MAX_IMPORT_RECORDS)):
        parse_import_file(envelope([{}] * (MAX_IMPORT_RECORDS + 1)))


def test_valid_envelope_returns_its_records():
    records = [example_record(), example_record()]
    assert parse_import_file(envelope(records)) == records


# --- HTTP (service replaced) ------------------------------------------------------------------------------------


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user(REPORTER_ID, "reporter")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(auth, "permissions_for_role", lambda role: DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(caller):
    return TestClient(api.app)


@pytest.fixture
def service(monkeypatch):
    state = {"calls": [], "results": None, "error": None}

    def fake_import(records, scope, **kwargs):
        state["calls"].append((len(records), scope.user_id))
        if state["error"] is not None:
            raise state["error"]
        if state["results"] is not None:
            return state["results"]
        return [RecordResult(index=i, status="imported", case_id="c", reference="SAFE-2026-000001") for i in range(len(records))]

    monkeypatch.setattr(api, "import_records", fake_import)
    return state


def test_import_endpoint_reports_counts_and_one_result_per_record(client, service):
    service["results"] = [
        RecordResult(index=0, status="imported", source_case_id="a", case_id="c1", reference="SAFE-2026-000001"),
        RecordResult(index=1, status="rejected", errors=("/record_schema_version: '0.2.0' was expected",)),
        RecordResult(index=2, status="duplicate", source_case_id="b", case_id="c2", reference="SAFE-2026-000002"),
    ]
    response = client.post("/api/v1/cases/import", content=envelope([example_record()] * 3), headers={**idem(), "content-type": "application/json"})
    assert response.status_code == 200
    body = response.json()
    assert (body["imported"], body["duplicate"], body["conflict"], body["rejected"], body["failed"]) == (1, 1, 0, 1, 0)
    assert [result["index"] for result in body["results"]] == [0, 1, 2]
    assert body["results"][1]["errors"] == ["/record_schema_version: '0.2.0' was expected"]
    assert service["calls"] == [(3, REPORTER_ID)]  # the actor is the caller, never a file field


def test_import_endpoint_refuses_an_unsupported_envelope_before_any_record_is_processed(client, service):
    response = client.post("/api/v1/cases/import", json={"import_format_version": 9, "records": []}, headers=idem())
    assert response.status_code == 422
    assert service["calls"] == []


def test_import_endpoint_refuses_a_body_over_the_size_limit(client, service):
    body = b" " * (MAX_IMPORT_BYTES + 1)
    response = client.post("/api/v1/cases/import", content=body, headers={**idem(), "content-type": "application/json"})
    assert response.status_code == 413
    assert service["calls"] == []


def test_import_endpoint_needs_the_case_submit_permission(client, caller, service):
    caller["user"] = make_user(ADMIN_ID, "admin")
    response = client.post("/api/v1/cases/import", json={"import_format_version": 1, "records": []}, headers=idem())
    assert response.status_code == 403
    assert service["calls"] == []


def test_import_endpoint_refuses_without_a_system_timezone(client, service):
    service["error"] = TimezoneNotConfigured()
    response = client.post("/api/v1/cases/import", json={"import_format_version": 1, "records": []}, headers=idem())
    assert response.status_code == 409
    assert response.json() == {"detail": "system timezone is not configured"}


# --- database (requires_database) ---------------------------------------------------------------------------------


def new_reporter() -> str:
    return users.create_user("t-" + uuid.uuid4().hex[:12], "temporary passphrase", "reporter")


def scope_for(actor_id: str) -> CaseScope:
    return CaseScope.from_permissions(actor_id, DEFAULT_ROLE_PERMISSIONS["reporter"])


def cases_created_by(actor_id: str) -> int:
    with connect() as conn:
        return conn.execute("SELECT count(*) FROM cases WHERE created_by = %s", (actor_id,)).fetchone()[0]


def registry_rows(source_case_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT case_id::text, content_sha256, payload FROM case_import_records WHERE source_case_id = %s AND NOT is_deleted",
            (source_case_id,),
        ).fetchall()


@requires_database
def test_valid_record_is_imported_with_its_case_deadlines_registry_and_audit():
    actor = new_reporter()
    record = example_record()

    [result] = import_records([record], scope_for(actor))

    assert result.status == "imported"
    [(stored_case_id, digest, payload)] = registry_rows(record["case_id"])
    assert stored_case_id == result.case_id
    assert digest == fingerprint(record)
    assert payload == record  # every received field is kept
    with connect() as conn:
        title, awareness, created_by, status = conn.execute(
            "SELECT title, awareness_time, created_by::text, status FROM cases WHERE id = %s", (result.case_id,)
        ).fetchone()
        tasks = conn.execute(
            "SELECT count(*) FROM deadline_tasks WHERE case_id = %s AND NOT is_deleted", (result.case_id,)
        ).fetchone()[0]
        registry_id = conn.execute(
            "SELECT id::text FROM case_import_records WHERE source_case_id = %s AND NOT is_deleted", (record["case_id"],)
        ).fetchone()[0]
        actions = [
            row[0]
            for row in conn.execute(
                "SELECT action FROM audit_events WHERE NOT is_deleted AND "
                "((target_type = 'case' AND target_id = %s) OR (target_type = 'case_import' AND target_id = %s)) "
                "ORDER BY created_at, id",
                (result.case_id, registry_id),
            ).fetchall()
        ]
    assert (title, awareness, created_by, status) == (TITLE, datetime(2026, 10, 30, 9, tzinfo=UTC), actor, "open")
    assert tasks == 5
    assert sorted(actions) == ["case.create", "case.import"]


@requires_database
def test_repeated_identical_record_is_a_duplicate_and_creates_nothing():
    actor = new_reporter()
    record = example_record()
    [first] = import_records([record], scope_for(actor))

    [second] = import_records([record], scope_for(actor))

    assert second.status == "duplicate"
    assert (second.case_id, second.reference) == (first.case_id, first.reference)
    assert cases_created_by(actor) == 1


@requires_database
def test_same_case_id_with_different_content_is_a_conflict_and_changes_nothing():
    actor = new_reporter()
    record = example_record()
    [first] = import_records([record], scope_for(actor))

    [changed] = import_records([{**record, "narrative_summary": "Changed after the first import."}], scope_for(actor))

    assert changed.status == "conflict"
    assert changed.reference == first.reference
    assert cases_created_by(actor) == 1
    [(_, digest, payload)] = registry_rows(record["case_id"])
    assert digest == fingerprint(record)
    assert "narrative_summary" not in payload


@requires_database
def test_each_record_stands_on_its_own_within_one_file():
    actor = new_reporter()
    good = example_record()
    bad = example_record()
    del bad["record_schema_version"]
    unknown = example_record(record_schema_version="0.2.0")

    results = import_records([good, bad, unknown, good], scope_for(actor))

    assert [result.status for result in results] == ["imported", "rejected", "rejected", "duplicate"]
    assert results[1].errors and "record_schema_version" in results[1].errors[0]
    assert registry_rows(bad["case_id"]) == []
    assert registry_rows(unknown["case_id"]) == []
    assert cases_created_by(actor) == 1


@requires_database
def test_database_failure_on_one_record_leaves_nothing_for_it_and_the_batch_goes_on(monkeypatch):
    actor = new_reporter()
    broken = example_record()
    broken["affected_systems"][0]["system_name"] = "broken-system"
    healthy = example_record()
    real_insert = cases.insert_case

    def flaky_insert(conn, title, awareness_time, created_by):
        if title.startswith("broken-system"):
            raise psycopg.OperationalError("simulated outage")
        return real_insert(conn, title, awareness_time, created_by)

    monkeypatch.setattr(case_import.cases, "insert_case", flaky_insert)

    results = import_records([broken, healthy], scope_for(actor))

    assert [result.status for result in results] == ["failed", "imported"]
    assert registry_rows(broken["case_id"]) == []
    assert cases_created_by(actor) == 1


@requires_database
def test_endpoint_imports_through_the_real_service(client, caller):
    actor = new_reporter()
    caller["user"] = make_user(actor, "reporter")
    record = example_record()

    response = client.post("/api/v1/cases/import", json={"import_format_version": 1, "records": [record]}, headers=idem())

    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 1
    assert body["results"][0]["reference"].startswith("SAFE-")
    assert len(registry_rows(record["case_id"])) == 1
