"""Catalog endpoints (plans/f0/api-draft.md section 2.6; T10, T11, T11-A). Unit and API tests, no database.

The service functions are replaced, so the HTTP mapping, the access rules, the validation, and the public export query
are checked here. The versioning and the database rules are checked in tests/test_catalog_db.py.
"""

import copy
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from safe_kit import api, auth, catalog, catalog_api
from safe_kit.catalog import (
    CatalogOutcome,
    Entry,
    RestrictedReference,
    Result,
    RunSummary,
    Version,
    decode_cursor,
    digest_of,
    encode_cursor,
)
from safe_kit.idempotency import StoredResponse
from safe_kit.users import User

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
USER_ID = "44444444-4444-4444-4444-444444444444"
ENTRY_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REPLACEMENT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RECOMMENDATION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
RUN_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
HIDDEN_LINK = "https://restricted.example.invalid/report/SECRET-7731"

# A test role that holds the restricted read, so the restricted view can be checked without changing the seeded roles.
ROLE_PERMISSIONS = {
    "reviewer": frozenset({"catalog:read", "catalog:write"}),
    "auditor": frozenset({"catalog:read"}),
    "restricted_auditor": frozenset({"catalog:read", "catalog:read_restricted"}),
    "reporter": frozenset({"cases:submit", "cases:read_own"}),
}

DOMAIN_FUNCTIONS = (
    "add_recommendation",
    "record_verification_run",
    "create_entry",
    "change_entry",
    "get_entry",
    "list_entries",
    "public_export",
)

ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCHEMA = ROOT / "schemas" / "catalog-export.v0.json"
EXPORT_EXAMPLE = ROOT / "schemas" / "examples" / "catalog-export-valid.json"
EXPORT_PATH = "/api/v1/catalog/export"


def make_user(user_id: str, role: str) -> User:
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


@pytest.fixture
def caller(monkeypatch):
    state = {"user": make_user(USER_ID, "reviewer")}
    api.app.dependency_overrides[auth.current_user] = lambda: state["user"]
    monkeypatch.setattr(
        auth,
        "permissions_for_role",
        lambda role: ROLE_PERMISSIONS.get(role, frozenset()),
    )
    yield state
    api.app.dependency_overrides.clear()


@pytest.fixture
def client(caller):
    return TestClient(api.app)


@pytest.fixture
def no_domain(monkeypatch):
    """Every service function fails the test: a refused request must never reach the domain layer."""

    def fail(*args, **kwargs):
        raise AssertionError("the catalog service must not run for this request")

    for name in DOMAIN_FUNCTIONS:
        monkeypatch.setattr(catalog_api, name, fail)


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def recommendation_body(**changes) -> dict:
    body = {
        "failure_or_system": "tool gateway accepts unscoped calls",
        "required_defence_outcome": "every call is checked against the approved scope",
        "minimum_control": "deny by default at the gateway",
        "control_alternative": None,
        "responsible": "platform owner",
        "due_at": "2026-12-01T09:00:00+00:00",
        "evidence_to_keep": "gateway policy diff and the test log",
    }
    body.update(changes)
    return body


def run_body(**changes) -> dict:
    body = {
        "subject": "deny-by-default gateway rule",
        "scope": "staging gateway, scoped tokens only",
        "assumptions": "the token scope is the only input",
        "evidence_path": "evidence/gateway/run-42",
        "failure_behavior": "unscoped calls are refused with 403",
        "error_class": "none observed in 200 runs",
        "uncertainty": "one environment only",
        "method": "replayed traffic from the incident",
        "result": "pass",
    }
    body.update(changes)
    return body


def version(**changes) -> Version:
    values = {
        "entry_id": ENTRY_ID,
        "version": 1,
        "operation": "create",
        "status": "active",
        "title": "Deny unscoped tool calls",
        "body": "Calls outside the approved scope are refused at the gateway.",
        "reason": None,
        "replacement_entry_id": None,
        "created_by": USER_ID,
        "created_at": NOW,
        "verification": None,
        "restricted": None,
    }
    values.update(changes)
    return Version(**values)


def entry_with(*versions: Version) -> Entry:
    return Entry(
        id=ENTRY_ID, created_by=USER_ID, created_at=NOW, versions=tuple(versions)
    )


# --- access ---------------------------------------------------------------------------------------------------------


def test_anonymous_requests_are_refused_and_never_reach_the_catalog(
    monkeypatch, no_domain
):
    client = TestClient(api.app)
    assert client.get("/api/v1/catalog/entries").status_code == 401
    assert client.get(f"/api/v1/catalog/entries/{ENTRY_ID}").status_code == 401
    assert client.get(EXPORT_PATH).status_code == 401
    assert (
        client.post(
            "/api/v1/catalog/recommendations",
            json=recommendation_body(),
            headers=idem(),
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"/api/v1/catalog/entries/{ENTRY_ID}/withdrawals",
            json={"expected_version": 1, "reason": "x"},
            headers=idem(),
        ).status_code
        == 401
    )


def test_a_role_without_catalog_permission_is_refused(caller, client, no_domain):
    caller["user"] = make_user(USER_ID, "reporter")
    assert client.get("/api/v1/catalog/entries").status_code == 403
    assert client.get(f"/api/v1/catalog/entries/{ENTRY_ID}").status_code == 403
    assert client.get(EXPORT_PATH).status_code == 403
    assert (
        client.post(
            "/api/v1/catalog/recommendations",
            json=recommendation_body(),
            headers=idem(),
        ).status_code
        == 403
    )


def test_a_read_only_role_cannot_record(caller, client, no_domain):
    caller["user"] = make_user(USER_ID, "auditor")
    response = client.post(
        "/api/v1/catalog/recommendations", json=recommendation_body(), headers=idem()
    )
    assert response.status_code == 403


def test_a_side_effect_post_needs_an_idempotency_key(client, no_domain):
    response = client.post(
        "/api/v1/catalog/recommendations", json=recommendation_body()
    )
    assert response.status_code == 400
    assert response.json()["detail"] == {"status": "idempotency_key_required"}


@pytest.mark.parametrize("key", ["has a space", "x" * 129, ""])
def test_an_invalid_idempotency_key_is_refused(client, no_domain, key):
    response = client.post(
        "/api/v1/catalog/recommendations",
        json=recommendation_body(),
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == {"status": "idempotency_key_invalid"}


# --- verification fields and the suggestion/result split (T10) ------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        "subject",
        "scope",
        "assumptions",
        "evidence_path",
        "failure_behavior",
        "error_class",
        "uncertainty",
        "method",
        "result",
    ],
)
def test_a_verification_run_needs_every_field(client, no_domain, missing):
    body = run_body()
    del body[missing]
    response = client.post(
        f"/api/v1/catalog/recommendations/{RECOMMENDATION_ID}/verification-runs",
        json=body,
        headers=idem(),
    )
    assert response.status_code == 400
    assert any(error["loc"][-1] == missing for error in response.json()["detail"])


def test_a_verification_result_is_one_of_three_values(client, no_domain):
    response = client.post(
        f"/api/v1/catalog/recommendations/{RECOMMENDATION_ID}/verification-runs",
        json=run_body(result="verified"),
        headers=idem(),
    )
    assert response.status_code == 400


def test_a_blank_verification_field_is_refused(client, no_domain):
    response = client.post(
        f"/api/v1/catalog/recommendations/{RECOMMENDATION_ID}/verification-runs",
        json=run_body(scope="   "),
        headers=idem(),
    )
    assert response.status_code == 400


def test_a_recommendation_cannot_carry_a_verified_flag_or_result(client, no_domain):
    for extra in ({"verified": True}, {"result": "pass"}, {"status": "verified"}):
        response = client.post(
            "/api/v1/catalog/recommendations",
            json=recommendation_body(**extra),
            headers=idem(),
        )
        assert response.status_code == 400, extra


def test_a_recommendation_due_time_needs_a_time_zone(client, no_domain):
    response = client.post(
        "/api/v1/catalog/recommendations",
        json=recommendation_body(due_at="2026-12-01T09:00:00"),
        headers=idem(),
    )
    assert response.status_code == 400


def test_an_entry_linked_to_an_unknown_run_is_refused(monkeypatch, client):
    """An entry's verification must name a run. A recommendation id is not a run, so the domain answers unknown."""
    monkeypatch.setattr(
        catalog_api,
        "create_entry",
        lambda *args, **kwargs: Result(CatalogOutcome.UNKNOWN_VERIFICATION_RUN),
    )
    response = client.post(
        "/api/v1/catalog/entries",
        json={"title": "t", "body": "b", "verification_run_id": RECOMMENDATION_ID},
        headers=idem(),
    )
    assert response.status_code == 422


def test_verified_is_true_only_through_a_passing_run(monkeypatch, client):
    passing = RunSummary(
        run_id=RUN_ID,
        subject="gateway",
        method="replay",
        result="pass",
        verified_at=NOW,
    )
    failing = RunSummary(
        run_id=RUN_ID,
        subject="gateway",
        method="replay",
        result="fail",
        verified_at=NOW,
    )
    cases = {
        "passing run": (passing, True),
        "failing run": (failing, False),
        "recommendation only": (None, False),
    }
    for label, (summary, expected) in cases.items():
        entry = entry_with(version(verification=summary))
        monkeypatch.setattr(
            catalog_api, "get_entry", lambda *args, _entry=entry, **kwargs: _entry
        )
        body = client.get(f"/api/v1/catalog/entries/{ENTRY_ID}").json()
        assert body["current"]["verified"] is expected, label


# --- versions, corrections, disputes, withdrawals, supersessions (T11, T11-A) ---------------------------------------


@pytest.mark.parametrize(
    "path, body",
    [
        ("corrections", {"expected_version": 1, "title": "t", "body": "b"}),
        ("disputes", {"expected_version": 1}),
        ("withdrawals", {"expected_version": 1}),
        ("supersessions", {"expected_version": 1}),
        ("versions", {"expected_version": 1}),
    ],
)
def test_an_operation_on_an_entry_needs_its_required_fields(
    client, no_domain, path, body
):
    response = client.post(
        f"/api/v1/catalog/entries/{ENTRY_ID}/{path}", json=body, headers=idem()
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "path, body",
    [
        (
            "corrections",
            {"expected_version": 1, "title": "t", "body": "b", "reason": "  "},
        ),
        ("disputes", {"expected_version": 1, "reason": ""}),
        ("withdrawals", {"expected_version": 1, "reason": "   "}),
        (
            "supersessions",
            {"expected_version": 1, "replacement_entry_id": "not-a-uuid"},
        ),
        ("versions", {"expected_version": 0, "title": "t", "body": "b"}),
    ],
)
def test_a_blank_reason_or_a_bad_identifier_is_refused(client, no_domain, path, body):
    response = client.post(
        f"/api/v1/catalog/entries/{ENTRY_ID}/{path}", json=body, headers=idem()
    )
    assert response.status_code == 400


def test_a_stale_version_is_a_conflict_with_only_the_current_version(
    monkeypatch, client
):
    monkeypatch.setattr(
        catalog_api,
        "change_entry",
        lambda *args, **kwargs: Result(CatalogOutcome.VERSION_CONFLICT, value=3),
    )
    response = client.post(
        f"/api/v1/catalog/entries/{ENTRY_ID}/withdrawals",
        json={"expected_version": 1, "reason": "r"},
        headers=idem(),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "status": "version_conflict",
        "current_version": 3,
    }


def test_an_entry_that_is_withdrawn_or_superseded_is_closed(monkeypatch, client):
    monkeypatch.setattr(
        catalog_api,
        "change_entry",
        lambda *args, **kwargs: Result(CatalogOutcome.ENTRY_CLOSED),
    )
    response = client.post(
        f"/api/v1/catalog/entries/{ENTRY_ID}/corrections",
        json={"expected_version": 2, "title": "t", "body": "b", "reason": "r"},
        headers=idem(),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "entry_closed"}


def test_an_invalid_replacement_is_refused(monkeypatch, client):
    monkeypatch.setattr(
        catalog_api,
        "change_entry",
        lambda *args, **kwargs: Result(CatalogOutcome.INVALID_REPLACEMENT),
    )
    response = client.post(
        f"/api/v1/catalog/entries/{ENTRY_ID}/supersessions",
        json={"expected_version": 1, "replacement_entry_id": REPLACEMENT_ID},
        headers=idem(),
    )
    assert response.status_code == 422


def test_the_supersession_request_passes_the_replacement_as_a_string(
    monkeypatch, client
):
    seen = {}

    def capture(entry_id, expected_version, operation, values, actor_id, **kwargs):
        seen.update(
            entry_id=entry_id,
            expected_version=expected_version,
            operation=operation,
            values=values,
        )
        return Result(
            CatalogOutcome.OK,
            value=entry_with(version(operation="supersession", status="superseded")),
        )

    monkeypatch.setattr(catalog_api, "change_entry", capture)
    response = client.post(
        f"/api/v1/catalog/entries/{ENTRY_ID}/supersessions",
        json={"expected_version": 1, "replacement_entry_id": REPLACEMENT_ID},
        headers=idem(),
    )
    assert response.status_code == 201
    assert seen["operation"] == "supersession"
    assert seen["values"] == {"replacement_entry_id": REPLACEMENT_ID, "reason": None}
    assert seen["expected_version"] == 1


def test_a_correction_passes_its_reason_and_restricted_reference_under_domain_names(
    monkeypatch, client
):
    seen = {}

    def capture(entry_id, expected_version, operation, values, actor_id, **kwargs):
        seen.update(values=values)
        return Result(
            CatalogOutcome.OK,
            value=entry_with(version(version=2, operation="correction")),
        )

    monkeypatch.setattr(catalog_api, "change_entry", capture)
    response = client.post(
        f"/api/v1/catalog/entries/{ENTRY_ID}/corrections",
        json={
            "expected_version": 1,
            "title": "  Deny unscoped calls  ",
            "body": "b",
            "reason": "wrong status code",
            "restricted_reference": f"  {HIDDEN_LINK}  ",
        },
        headers=idem(),
    )
    assert response.status_code == 201
    assert seen["values"]["reason"] == "wrong status code"
    assert seen["values"]["reference"] == HIDDEN_LINK
    assert "restricted_reference" not in seen["values"]


def test_a_replayed_request_returns_the_first_answer_and_writes_nothing(
    monkeypatch, client
):
    stored = {"id": ENTRY_ID, "status": "active", "version": 1}
    monkeypatch.setattr(
        catalog_api,
        "create_entry",
        lambda *args, **kwargs: Result(
            CatalogOutcome.REPLAY, stored=StoredResponse(status_code=201, body=stored)
        ),
    )
    response = client.post(
        "/api/v1/catalog/entries", json={"title": "t", "body": "b"}, headers=idem()
    )
    assert response.status_code == 201
    assert response.json() == stored


def test_the_same_key_with_another_request_is_refused(monkeypatch, client):
    monkeypatch.setattr(
        catalog_api,
        "add_recommendation",
        lambda *args, **kwargs: Result(CatalogOutcome.KEY_REUSED),
    )
    response = client.post(
        "/api/v1/catalog/recommendations", json=recommendation_body(), headers=idem()
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "idempotency_key_reused"}


def test_a_new_recommendation_is_answered_with_201_and_no_result(monkeypatch, client):
    from safe_kit.catalog import Recommendation

    recommendation = Recommendation(
        id=RECOMMENDATION_ID,
        failure_or_system="tool gateway accepts unscoped calls",
        required_defence_outcome="every call is checked against the approved scope",
        minimum_control="deny by default at the gateway",
        control_alternative=None,
        responsible="platform owner",
        due_at=NOW,
        evidence_to_keep="gateway policy diff and the test log",
        created_by=USER_ID,
        created_at=NOW,
    )
    monkeypatch.setattr(
        catalog_api,
        "add_recommendation",
        lambda *args, **kwargs: Result(CatalogOutcome.OK, recommendation),
    )
    response = client.post(
        "/api/v1/catalog/recommendations", json=recommendation_body(), headers=idem()
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == RECOMMENDATION_ID
    assert not {"verified", "result", "verification"} & set(body)


# --- restricted references and the read view (T11-A) ----------------------------------------------------------------


def restricted_fake(monkeypatch, entry: Entry) -> None:
    """A get_entry stand-in that behaves like the real one: the restricted reference is loaded only on request."""

    def fake(entry_id, *, with_restricted):
        if with_restricted:
            return entry
        stripped = tuple(
            Version(**{**version.__dict__, "restricted": None})
            for version in entry.versions
        )
        return Entry(
            id=entry.id,
            created_by=entry.created_by,
            created_at=entry.created_at,
            versions=stripped,
        )

    monkeypatch.setattr(catalog_api, "get_entry", fake)


def test_a_restricted_reference_is_absent_without_the_restricted_permission(
    monkeypatch, caller, client
):
    entry = entry_with(
        version(
            restricted=RestrictedReference(
                reference=HIDDEN_LINK, digest=digest_of(HIDDEN_LINK)
            )
        )
    )
    restricted_fake(monkeypatch, entry)
    response = client.get(f"/api/v1/catalog/entries/{ENTRY_ID}")
    assert response.status_code == 200
    text = json.dumps(response.json())
    assert HIDDEN_LINK not in text
    assert digest_of(HIDDEN_LINK) not in text
    assert "restricted" not in response.json()["current"]


def test_a_restricted_reference_and_its_digest_are_shown_with_the_restricted_permission(
    monkeypatch, caller, client
):
    caller["user"] = make_user(USER_ID, "restricted_auditor")
    entry = entry_with(
        version(
            restricted=RestrictedReference(
                reference=HIDDEN_LINK, digest=digest_of(HIDDEN_LINK)
            )
        )
    )
    restricted_fake(monkeypatch, entry)
    response = client.get(f"/api/v1/catalog/entries/{ENTRY_ID}")
    assert response.json()["current"]["restricted"] == {
        "reference": HIDDEN_LINK,
        "digest": digest_of(HIDDEN_LINK),
    }


def test_write_responses_never_carry_a_restricted_reference(
    monkeypatch, caller, client
):
    # The writer may hold the restricted read or not; the write response carries no restricted content either way.
    caller["user"] = make_user(USER_ID, "restricted_auditor")
    caller["user"] = make_user(USER_ID, "reviewer")
    entry = entry_with(
        version(
            restricted=RestrictedReference(
                reference=HIDDEN_LINK, digest=digest_of(HIDDEN_LINK)
            )
        )
    )
    monkeypatch.setattr(
        catalog_api,
        "create_entry",
        lambda *args, **kwargs: Result(CatalogOutcome.OK, value=entry),
    )
    response = client.post(
        "/api/v1/catalog/entries",
        json={"title": "t", "body": "b", "restricted_reference": HIDDEN_LINK},
        headers=idem(),
    )
    assert response.status_code == 201
    assert HIDDEN_LINK not in json.dumps(response.json())
    assert digest_of(HIDDEN_LINK) not in json.dumps(response.json())


def test_the_detail_read_returns_the_full_history_in_order(monkeypatch, client):
    withdrawn = version(
        version=2,
        operation="withdrawal",
        status="withdrawn",
        reason="retired",
        created_at=NOW,
    )
    entry = entry_with(version(), withdrawn)
    monkeypatch.setattr(catalog_api, "get_entry", lambda *args, **kwargs: entry)
    body = client.get(f"/api/v1/catalog/entries/{ENTRY_ID}").json()
    assert body["status"] == "withdrawn"
    assert body["version"] == 2
    assert [item["version"] for item in body["versions"]] == [1, 2]
    assert body["versions"][0]["status"] == "active"
    assert body["versions"][1]["reason"] == "retired"


def test_an_unknown_entry_is_404(monkeypatch, client):
    monkeypatch.setattr(catalog_api, "get_entry", lambda *args, **kwargs: None)
    assert client.get(f"/api/v1/catalog/entries/{ENTRY_ID}").status_code == 404


def test_the_list_returns_a_page_and_the_next_cursor(monkeypatch, client):
    entry = entry_with(version())
    monkeypatch.setattr(
        catalog_api,
        "list_entries",
        lambda limit, cursor, **kwargs: ([entry], "next-token"),
    )
    body = client.get("/api/v1/catalog/entries?limit=5").json()
    assert [item["id"] for item in body["items"]] == [ENTRY_ID]
    assert body["next_cursor"] == "next-token"
    assert "versions" not in body["items"][0]


def test_an_invalid_cursor_is_refused_before_any_read(client):
    # The cursor is decoded before the database is opened, so this runs without a database.
    response = client.get("/api/v1/catalog/entries?cursor=not-a-cursor")
    assert response.status_code == 400
    assert response.json()["detail"] == {"status": "invalid_cursor"}


def test_a_cursor_round_trips_and_a_forged_one_is_refused():
    cursor = encode_cursor(NOW, ENTRY_ID)
    assert decode_cursor(cursor) == (NOW, ENTRY_ID)
    assert decode_cursor("!!!") is None
    assert decode_cursor(encode_cursor(NOW, "not-a-uuid")) is None


# --- public export (T11-A) ------------------------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RecordingConnection:
    """Answers the two reads of the public export and records every statement it is given."""

    def __init__(self, entry_ids, version_rows):
        self.entry_ids = entry_ids
        self.version_rows = version_rows
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        normalised = " ".join(sql.split())
        self.statements.append(normalised)
        if normalised.startswith("SELECT id::text FROM catalog_entries"):
            return _Rows([(entry_id,) for entry_id in self.entry_ids])
        if "FROM catalog_versions v" in normalised:
            return _Rows(self.version_rows)
        if normalised.startswith("INSERT INTO audit_events"):
            return _Rows([])
        raise AssertionError(f"unexpected statement in the export: {normalised[:120]}")


class _Connect:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *exc):
        return False


def test_the_public_export_never_reads_restricted_references_and_omits_internal_fields(
    monkeypatch,
):
    run_id = RUN_ID
    rows = [
        (
            ENTRY_ID,
            1,
            "create",
            "active",
            "Deny unscoped tool calls",
            "Refuse calls outside scope.",
            None,
            None,
            USER_ID,
            NOW,
            run_id,
            "gateway scope",
            "replay",
            "pass",
            NOW,
        ),
        (
            ENTRY_ID,
            2,
            "supersession",
            "superseded",
            "Deny unscoped tool calls",
            "Refuse calls outside scope.",
            "replaced",
            REPLACEMENT_ID,
            USER_ID,
            NOW,
            run_id,
            "gateway scope",
            "replay",
            "pass",
            NOW,
        ),
    ]
    connection = _RecordingConnection([ENTRY_ID], rows)
    monkeypatch.setattr(catalog, "connect", lambda: _Connect(connection))

    export = catalog.public_export(USER_ID)
    text = json.dumps(export)

    assert not any(
        "catalog_restricted_references" in statement
        for statement in connection.statements
    )
    # The export writes its audit row on the same connection as the reads.
    assert (
        sum(
            statement.startswith("INSERT INTO audit_events")
            for statement in connection.statements
        )
        == 1
    )
    assert HIDDEN_LINK not in text
    assert digest_of(HIDDEN_LINK) not in text
    entry = export["catalog_export"]["entries"][0]
    assert entry["status"] == "superseded"
    assert [item["version"] for item in entry["versions"]] == [1, 2]
    assert entry["versions"][1]["replacement_entry_id"] == REPLACEMENT_ID
    assert set(entry["versions"][0]) == {
        "version",
        "operation",
        "status",
        "title",
        "body",
        "reason",
        "replacement_entry_id",
        "created_at",
        "verification",
    }
    assert entry["versions"][0]["verification"] == {
        "subject": "gateway scope",
        "method": "replay",
        "result": "pass",
        "verified_at": NOW.isoformat(),
    }
    for internal in (
        "created_by",
        "verified_by",
        "evidence_path",
        "recommendation_id",
        "run_id",
    ):
        assert internal not in text


# --- export endpoint and schema (T11-A, R14) ----------------------------------------------------------------------


def export_errors(document) -> list[str]:
    """Every schema error of an export document, as 'location: message'. An empty list means it is valid."""
    validator = Draft202012Validator(
        json.loads(EXPORT_SCHEMA.read_text(encoding="utf-8"))
    )
    return [
        f"/{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in validator.iter_errors(document)
    ]


def export_rows() -> list[tuple]:
    """The two public version rows of one superseded entry, in the column order of the public export query."""
    run_id = RUN_ID
    first = (
        ENTRY_ID,
        1,
        "create",
        "active",
        "Deny unscoped tool calls",
        "Refuse calls outside scope.",
        None,
        None,
        USER_ID,
        NOW,
        run_id,
        "gateway scope",
        "replay",
        "pass",
        NOW,
    )
    second = (
        ENTRY_ID,
        2,
        "supersession",
        "superseded",
        "Deny unscoped tool calls",
        "Refuse calls outside scope.",
        "replaced",
        REPLACEMENT_ID,
        USER_ID,
        NOW,
        None,
        None,
        None,
        None,
        None,
    )
    return [first, second]


def test_the_export_schema_is_a_valid_draft_2020_12_schema_and_its_example_validates():
    Draft202012Validator.check_schema(
        json.loads(EXPORT_SCHEMA.read_text(encoding="utf-8"))
    )
    example = json.loads(EXPORT_EXAMPLE.read_text(encoding="utf-8"))
    assert export_errors(example) == []
    assert example["catalog_export_schema_version"] == "0.1.0"


def test_the_export_schema_refuses_a_hidden_link_or_digest_field():
    """The schema allows no field beyond the public ones. A reference or digest added to a version fails it."""
    example = json.loads(EXPORT_EXAMPLE.read_text(encoding="utf-8"))
    with_digest = copy.deepcopy(example)
    with_digest["catalog_export"]["entries"][0]["versions"][0]["digest"] = digest_of(
        HIDDEN_LINK
    )
    with_reference = copy.deepcopy(example)
    with_reference["catalog_export"]["entries"][0]["versions"][0]["restricted"] = {
        "reference": HIDDEN_LINK,
        "digest": digest_of(HIDDEN_LINK),
    }
    assert export_errors(with_digest) != []
    assert export_errors(with_reference) != []


def test_the_export_endpoint_returns_a_schema_valid_document_identical_across_two_calls(
    client, monkeypatch, caller
):
    connection = _RecordingConnection([ENTRY_ID], export_rows())
    monkeypatch.setattr(catalog, "connect", lambda: _Connect(connection))

    first = client.get(EXPORT_PATH)
    second = client.get(EXPORT_PATH)

    assert first.status_code == 200
    assert first.content == second.content
    document = first.json()
    assert export_errors(document) == []
    assert document["catalog_export_schema_version"] == "0.1.0"
    assert HIDDEN_LINK not in first.text
    assert digest_of(HIDDEN_LINK) not in first.text
    assert "created_by" not in first.text
    assert "run_id" not in first.text


def test_the_export_is_the_same_for_every_caller_with_catalog_read(
    client, monkeypatch, caller
):
    connection = _RecordingConnection([ENTRY_ID], export_rows())
    monkeypatch.setattr(catalog, "connect", lambda: _Connect(connection))
    reviewer_export = client.get(EXPORT_PATH).content
    caller["user"] = make_user(USER_ID, "auditor")
    assert client.get(EXPORT_PATH).content == reviewer_export
