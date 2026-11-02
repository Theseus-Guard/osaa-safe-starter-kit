"""Catalog database tests: T10 (suggestion and verified result apart), T11 (versions, corrections, disputes, withdrawal,
supersession), and T11-A (a public export carries no hidden link or digest; internal records stay internal).

They run the real service functions against PostgreSQL and need SAFE_TEST_DATABASE=1 with the SAFE_DB_* settings and the
migrations applied (see tests/test_database.py). They create rows and never remove them, so use a throwaway database.
"""

import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from safe_kit import api, auth, catalog_api, users
from safe_kit.catalog import (
    CatalogOutcome,
    add_recommendation,
    change_entry,
    create_entry,
    digest_of,
    get_entry,
    public_export,
    record_verification_run,
)
from safe_kit.db import connect
from safe_kit.migrate import main as migrate
from safe_kit.rbac import list_role_permissions
from error_body import without_request_id

requires_database = pytest.mark.skipif(
    os.environ.get("SAFE_TEST_DATABASE") != "1",
    reason="set SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings to run the database tests",
)

HIDDEN_LINK = "https://restricted.example.invalid/report/SECRET-9001"
OTHER_HIDDEN_LINK = "https://restricted.example.invalid/report/SECRET-9002"
DUE = "2026-12-01T09:00:00+00:00"


@pytest.fixture(autouse=True)
def database_ready():
    if os.environ.get("SAFE_TEST_DATABASE") == "1":
        migrate()
    yield
    api.app.dependency_overrides.clear()


def new_user(role: str) -> str:
    return users.create_user("c-" + uuid.uuid4().hex[:12], "temporary passphrase", role)


def unique(text: str) -> str:
    return f"{text} {uuid.uuid4().hex[:10]}"


def recommendation(actor: str) -> str:
    result = add_recommendation(
        {
            "failure_or_system": unique("gateway accepts unscoped calls"),
            "required_defence_outcome": "every call is checked against the approved scope",
            "minimum_control": "deny by default",
            "control_alternative": None,
            "responsible": "platform owner",
            "due_at": DUE,
            "evidence_to_keep": "policy diff",
        },
        actor,
    )
    assert result.outcome is CatalogOutcome.OK
    return result.value.id


def run(recommendation_id: str, actor: str, result: str = "pass") -> str:
    outcome = record_verification_run(
        recommendation_id,
        {
            "subject": "gateway deny-by-default rule",
            "scope": "staging gateway",
            "assumptions": "scoped tokens only",
            "evidence_path": "evidence/gateway/run-42",
            "failure_behavior": "unscoped calls are refused",
            "error_class": "none observed",
            "uncertainty": "one environment",
            "method": "replayed traffic",
            "result": result,
        },
        actor,
    )
    assert outcome.outcome is CatalogOutcome.OK
    return outcome.value.id


def entry(actor: str, *, run_id: str | None = None, reference: str | None = None):
    result = create_entry(
        {
            "title": unique("Deny unscoped tool calls"),
            "body": "Calls outside the approved scope are refused at the gateway.",
            "verification_run_id": run_id,
            "reference": reference,
        },
        actor,
    )
    assert result.outcome is CatalogOutcome.OK
    return result.value


def version_rows(entry_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT version, operation, status, title, body, reason, replacement_entry_id::text,
                   verification_run_id::text
            FROM catalog_versions
            WHERE entry_id = %s
            ORDER BY version
            """,
            (entry_id,),
        ).fetchall()


def audit_rows(action: str, entry_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT details FROM audit_events WHERE action = %s AND target_id = %s ORDER BY created_at",
            (action, entry_id),
        ).fetchall()


def as_user(user_id: str) -> TestClient:
    """A client that makes every request as this user, with the permissions the database holds for its role."""
    user = users.find_user_by_id(user_id)
    assert user is not None
    api.app.dependency_overrides[auth.current_user] = lambda: user
    return TestClient(api.app)


# --- seed and schema -----------------------------------------------------------------------------------------------


@requires_database
def test_the_seed_grants_the_catalog_permissions_as_designed():
    permissions = list_role_permissions()
    assert {"catalog:read", "catalog:write"} <= set(permissions["reviewer"])
    assert "catalog:read" in permissions["auditor"]
    assert "catalog:write" not in permissions["auditor"]
    assert all(not name.startswith("catalog:") for name in permissions["reporter"])
    # No role is granted the restricted read by default; an administrator grants it through the matrix.
    assert all("catalog:read_restricted" not in names for names in permissions.values())


@requires_database
def test_every_catalog_table_has_the_standard_columns():
    with connect() as conn:
        for table in (
            "catalog_recommendations",
            "catalog_verification_runs",
            "catalog_entries",
            "catalog_versions",
            "catalog_restricted_references",
        ):
            columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                    (table,),
                ).fetchall()
            }
            assert {
                "created_at",
                "updated_at",
                "deleted_at",
                "is_deleted",
            } <= columns, table


@requires_database
def test_a_recommendation_has_no_result_or_verified_column():
    """T10: a suggestion cannot be stored as a verified result, because the table has nowhere to put one."""
    with connect() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'catalog_recommendations'"
            ).fetchall()
        }
    assert not {"result", "verified", "status", "verification_run_id"} & columns


# --- T10: suggestion and verified result apart ----------------------------------------------------------------------


@requires_database
def test_a_recommendation_alone_is_not_verified_and_a_failed_run_is_not_verified_either():
    reviewer = new_user("reviewer")
    recommendation_id = recommendation(reviewer)
    # A recommendation id is not a run: an entry cannot be linked to it as a verification.
    refused = create_entry(
        {
            "title": unique("t"),
            "body": "b",
            "verification_run_id": recommendation_id,
            "reference": None,
        },
        reviewer,
    )
    assert refused.outcome is CatalogOutcome.UNKNOWN_VERIFICATION_RUN

    without_run = entry(reviewer)
    assert without_run.current.verification is None

    failed = run(recommendation_id, reviewer, result="fail")
    failing_entry = entry(reviewer, run_id=failed)
    assert failing_entry.current.verification.result == "fail"

    passed = run(recommendation_id, reviewer, result="pass")
    passing_entry = entry(reviewer, run_id=passed)
    assert passing_entry.current.verification.result == "pass"

    client = as_user(reviewer)
    flags = {
        item_id: client.get(f"/api/v1/catalog/entries/{item_id}").json()["current"][
            "verified"
        ]
        for item_id in (without_run.id, failing_entry.id, passing_entry.id)
    }
    assert flags == {
        without_run.id: False,
        failing_entry.id: False,
        passing_entry.id: True,
    }


@requires_database
def test_a_verification_run_records_its_own_checker_and_time_and_leaves_the_recommendation_unchanged():
    reviewer = new_user("reviewer")
    recommendation_id = recommendation(reviewer)
    with connect() as conn:
        before = conn.execute(
            "SELECT failure_or_system, minimum_control, due_at FROM catalog_recommendations WHERE id = %s",
            (recommendation_id,),
        ).fetchone()
    run_id = run(recommendation_id, reviewer, result="inconclusive")
    with connect() as conn:
        after = conn.execute(
            "SELECT failure_or_system, minimum_control, due_at FROM catalog_recommendations WHERE id = %s",
            (recommendation_id,),
        ).fetchone()
        verified_by, result = conn.execute(
            "SELECT verified_by::text, result FROM catalog_verification_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
    assert before == after
    assert verified_by == reviewer
    assert result == "inconclusive"


# --- T11: versions, corrections, disputes, withdrawal, supersession -------------------------------------------------


@requires_database
def test_a_correction_appends_a_version_and_the_earlier_version_is_unchanged():
    reviewer = new_user("reviewer")
    created = entry(reviewer)
    before = version_rows(created.id)
    result = change_entry(
        created.id,
        1,
        "correction",
        {
            "title": "Corrected title",
            "body": "Corrected body",
            "reason": "wrong scope wording",
            "reference": None,
        },
        reviewer,
    )
    assert result.outcome is CatalogOutcome.OK
    rows = version_rows(created.id)
    assert [row[0] for row in rows] == [1, 2]
    assert rows[0] == before[0]
    assert rows[1][1:5] == ("correction", "active", "Corrected title", "Corrected body")
    assert rows[1][5] == "wrong scope wording"


@requires_database
def test_the_database_refuses_any_change_to_a_stored_version():
    reviewer = new_user("reviewer")
    created = entry(reviewer)
    with pytest.raises(psycopg.Error, match="append-only"):
        with connect() as conn:
            conn.execute(
                "UPDATE catalog_versions SET title = 'changed' WHERE entry_id = %s",
                (created.id,),
            )
    assert version_rows(created.id)[0][3] == created.current.title


@requires_database
def test_a_stale_expected_version_is_refused_and_nothing_is_stored():
    reviewer = new_user("reviewer")
    created = entry(reviewer)
    result = change_entry(
        created.id,
        5,
        "version",
        {"title": "t", "body": "b", "verification_run_id": None, "reference": None},
        reviewer,
    )
    assert result.outcome is CatalogOutcome.VERSION_CONFLICT
    assert result.value == 1
    assert len(version_rows(created.id)) == 1


@requires_database
def test_a_withdrawal_keeps_the_history_and_closes_the_entry():
    reviewer = new_user("reviewer")
    created = entry(reviewer)
    result = change_entry(
        created.id, 1, "withdrawal", {"reason": "retired after review"}, reviewer
    )
    assert result.outcome is CatalogOutcome.OK
    rows = version_rows(created.id)
    assert rows[0][2] == "active"  # the first version keeps its status
    assert rows[1][1:3] == ("withdrawal", "withdrawn")
    assert rows[1][5] == "retired after review"
    assert get_entry(created.id, with_restricted=False).current.status == "withdrawn"
    closed = change_entry(
        created.id,
        2,
        "correction",
        {"title": "t", "body": "b", "reason": "r", "reference": None},
        reviewer,
    )
    assert closed.outcome is CatalogOutcome.ENTRY_CLOSED


@requires_database
def test_a_supersession_points_to_the_replacement_and_refuses_bad_replacements():
    reviewer = new_user("reviewer")
    old = entry(reviewer)
    new = entry(reviewer)
    result = change_entry(
        old.id,
        1,
        "supersession",
        {"replacement_entry_id": new.id, "reason": None},
        reviewer,
    )
    assert result.outcome is CatalogOutcome.OK
    rows = version_rows(old.id)
    assert rows[0][2] == "active"
    assert rows[1][1:3] == ("supersession", "superseded")
    assert rows[1][6] == new.id
    assert (
        get_entry(old.id, with_restricted=False).current.replacement_entry_id == new.id
    )

    to_self = change_entry(
        new.id,
        1,
        "supersession",
        {"replacement_entry_id": new.id, "reason": None},
        reviewer,
    )
    assert to_self.outcome is CatalogOutcome.INVALID_REPLACEMENT

    to_closed = change_entry(
        new.id,
        1,
        "supersession",
        {"replacement_entry_id": old.id, "reason": None},
        reviewer,
    )
    assert to_closed.outcome is CatalogOutcome.INVALID_REPLACEMENT

    withdrawn = entry(reviewer)
    change_entry(withdrawn.id, 1, "withdrawal", {"reason": "retired"}, reviewer)
    to_withdrawn = change_entry(
        new.id,
        1,
        "supersession",
        {"replacement_entry_id": withdrawn.id, "reason": None},
        reviewer,
    )
    assert to_withdrawn.outcome is CatalogOutcome.INVALID_REPLACEMENT


@requires_database
def test_a_dispute_is_recorded_without_changing_the_content_and_a_correction_answers_it():
    reviewer = new_user("reviewer")
    created = entry(reviewer)
    first = change_entry(
        created.id, 1, "dispute", {"reason": "the scope is too wide"}, reviewer
    )
    assert first.outcome is CatalogOutcome.OK
    second = change_entry(
        created.id, 2, "dispute", {"reason": "a second reviewer disagrees"}, reviewer
    )
    assert second.outcome is CatalogOutcome.OK
    rows = version_rows(created.id)
    assert [row[2] for row in rows] == ["active", "disputed", "disputed"]
    assert rows[1][5] == "the scope is too wide"
    assert rows[2][5] == "a second reviewer disagrees"
    assert (
        rows[2][3:5] == rows[0][3:5]
    )  # the disputed content is the same as the first version

    answered = change_entry(
        created.id,
        3,
        "correction",
        {
            "title": "Narrowed title",
            "body": "Narrowed body",
            "reason": "scope narrowed",
            "reference": None,
        },
        reviewer,
    )
    assert answered.outcome is CatalogOutcome.OK
    assert get_entry(created.id, with_restricted=False).current.status == "active"
    assert len(audit_rows("catalog.entry_disputed", created.id)) == 2


@requires_database
def test_the_audit_rows_of_a_change_hold_versions_and_no_free_text():
    reviewer = new_user("reviewer")
    created = entry(reviewer)
    change_entry(
        created.id,
        1,
        "withdrawal",
        {"reason": "a reason that must not reach the audit log"},
        reviewer,
    )
    rows = audit_rows("catalog.entry_withdrawn", created.id)
    assert len(rows) == 1
    details = rows[0][0]
    assert details == {"version": {"old": 1, "new": 2}}
    assert "must not reach" not in json.dumps(details)


# --- T11-A: restricted references and the public export ------------------------------------------------------------


@requires_database
def test_the_public_export_has_no_hidden_link_or_digest_and_the_database_does_hold_them():
    reviewer = new_user("reviewer")
    run_id = run(recommendation(reviewer), reviewer)
    hidden = entry(reviewer, run_id=run_id, reference=HIDDEN_LINK)
    # A correction that gives no new reference carries the hidden one forward; the export must not show it either.
    changed = change_entry(
        hidden.id,
        1,
        "correction",
        {
            "title": unique("Corrected"),
            "body": "Corrected body",
            "reason": "wording",
            "reference": None,
        },
        reviewer,
    )
    assert changed.outcome is CatalogOutcome.OK
    other = entry(reviewer, reference=OTHER_HIDDEN_LINK)
    change_entry(other.id, 1, "withdrawal", {"reason": "retired"}, reviewer)

    with connect() as conn:
        stored = conn.execute(
            "SELECT count(*) FROM catalog_restricted_references WHERE digest = %s",
            (digest_of(HIDDEN_LINK),),
        ).fetchone()[0]
    assert stored >= 1  # the hidden reference is held in the database

    export = public_export()
    text = json.dumps(export)
    for secret in (
        HIDDEN_LINK,
        OTHER_HIDDEN_LINK,
        digest_of(HIDDEN_LINK),
        digest_of(OTHER_HIDDEN_LINK),
    ):
        assert secret not in text
    assert changed.value.current.title in text  # the public content is there
    for internal in ("evidence/gateway/run-42", reviewer):
        assert internal not in text


@requires_database
def test_the_restricted_reference_is_returned_by_the_api_only_with_the_restricted_permission(
    monkeypatch,
):
    reviewer = new_user("reviewer")
    auditor = new_user("auditor")
    created = entry(reviewer, reference=HIDDEN_LINK)

    without = as_user(auditor).get(f"/api/v1/catalog/entries/{created.id}")
    assert without.status_code == 200
    assert HIDDEN_LINK not in without.text
    assert digest_of(HIDDEN_LINK) not in without.text

    monkeypatch.setattr(catalog_api, "_may_read_restricted", lambda actor: True)
    with_permission = as_user(auditor).get(f"/api/v1/catalog/entries/{created.id}")
    assert with_permission.json()["current"]["restricted"] == {
        "reference": HIDDEN_LINK,
        "digest": digest_of(HIDDEN_LINK),
    }


@requires_database
def test_an_anonymous_request_is_refused_and_a_read_only_role_cannot_write():
    reviewer = new_user("reviewer")
    auditor = new_user("auditor")
    created = entry(reviewer)
    api.app.dependency_overrides.clear()
    anonymous = TestClient(api.app).get(f"/api/v1/catalog/entries/{created.id}")
    assert anonymous.status_code == 401

    response = as_user(auditor).post(
        "/api/v1/catalog/recommendations",
        json={
            "failure_or_system": "f",
            "required_defence_outcome": "r",
            "minimum_control": "m",
            "responsible": "p",
            "due_at": DUE,
            "evidence_to_keep": "e",
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 403


# --- idempotency through the API (plans/f0/api-draft.md section 3.4) ----------------------------------------------


@requires_database
def test_a_repeated_entry_post_with_the_same_key_writes_once_and_a_changed_body_is_refused():
    reviewer = new_user("reviewer")
    client = as_user(reviewer)
    key = str(uuid.uuid4())
    title = unique("Replay title")
    body = {"title": title, "body": "Replay body"}

    first = client.post(
        "/api/v1/catalog/entries", json=body, headers={"Idempotency-Key": key}
    )
    second = client.post(
        "/api/v1/catalog/entries", json=body, headers={"Idempotency-Key": key}
    )
    assert first.status_code == second.status_code == 201
    assert without_request_id(first) == without_request_id(second)

    with connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM catalog_versions WHERE title = %s", (title,)
        ).fetchone()[0]
        created = conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = 'catalog.entry_created' AND target_id = %s",
            (first.json()["id"],),
        ).fetchone()[0]
    assert count == 1
    assert created == 1

    changed = client.post(
        "/api/v1/catalog/entries",
        json={"title": title, "body": "a different body"},
        headers={"Idempotency-Key": key},
    )
    assert changed.status_code == 409
    assert changed.json()["detail"] == {"status": "idempotency_key_reused"}


# --- T11-A over HTTP: the export endpoint ----------------------------------------------------------------------------


def export_document_errors(document) -> list[str]:
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas" / "catalog-export.v0.json").read_text(encoding="utf-8")
    )
    return [error.message for error in Draft202012Validator(schema).iter_errors(document)]


@requires_database
def test_the_export_endpoint_hides_hidden_links_and_digests_from_every_reader():
    reviewer = new_user("reviewer")
    auditor = new_user("auditor")
    run_id = run(recommendation(reviewer), reviewer)
    hidden = entry(reviewer, run_id=run_id, reference=HIDDEN_LINK)
    other = entry(reviewer, reference=OTHER_HIDDEN_LINK)
    with connect() as conn:
        stored = conn.execute(
            "SELECT count(*) FROM catalog_restricted_references WHERE digest = %s",
            (digest_of(HIDDEN_LINK),),
        ).fetchone()[0]
    assert stored >= 1  # the hidden reference is in the database, so a leak would be possible

    response = as_user(auditor).get("/api/v1/catalog/export")
    reviewer_response = as_user(reviewer).get("/api/v1/catalog/export")

    assert response.status_code == 200
    for secret in (
        HIDDEN_LINK,
        OTHER_HIDDEN_LINK,
        digest_of(HIDDEN_LINK),
        digest_of(OTHER_HIDDEN_LINK),
    ):
        assert secret not in response.text
    document = response.json()
    assert export_document_errors(document) == []
    ids = {item["id"] for item in document["catalog_export"]["entries"]}
    assert {hidden.id, other.id} <= ids
    assert reviewer_response.content == response.content  # the same document for every reader
    assert reviewer not in response.text  # internal identifiers stay internal


@requires_database
def test_the_export_repeats_exactly_and_shows_the_stored_current_version():
    reviewer = new_user("reviewer")
    auditor = new_user("auditor")
    created = entry(reviewer)
    changed = change_entry(
        created.id,
        1,
        "correction",
        {
            "title": unique("Corrected"),
            "body": "Corrected body",
            "reason": "wording",
            "reference": None,
        },
        reviewer,
    )
    assert changed.outcome is CatalogOutcome.OK

    first = as_user(auditor).get("/api/v1/catalog/export")
    second = as_user(auditor).get("/api/v1/catalog/export")

    assert first.status_code == 200
    assert first.content == second.content
    assert export_document_errors(first.json()) == []
    [exported] = [
        item for item in first.json()["catalog_export"]["entries"] if item["id"] == created.id
    ]
    stored = get_entry(created.id, with_restricted=False)
    assert exported["version"] == stored.current.version == 2
    assert exported["status"] == stored.current.status == "active"
    assert [item["version"] for item in exported["versions"]] == [1, 2]
    assert exported["versions"][0]["title"] == created.current.title  # the earlier version is kept as it was
    assert exported["versions"][-1]["title"] == changed.value.current.title


@requires_database
def test_the_export_endpoint_refuses_a_role_without_catalog_read():
    reporter = new_user("reporter")
    assert as_user(reporter).get("/api/v1/catalog/export").status_code == 403
