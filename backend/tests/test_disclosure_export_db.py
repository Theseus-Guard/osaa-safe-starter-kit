"""F2 export (package 7, part B): the versioned export of an approved disclosure version, over HTTP and with the database.

- An approved version exports a document that matches schemas/disclosure-export.v0.json and its example.
- A version without an approval is refused, and no document is returned.
- A superseded version (approved, then replaced by a later version) is exported with the status "superseded".
- The export holds no original content, no original checksum, no file name, no redaction reason and no case title.
- The export is identical across two calls.
- Permissions: 401 without a login, 403 without disclosures:read, 404 for a case the caller cannot see.
- Each export writes one audit row with identifiers and the status, and no content.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import json
import uuid

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from safe_kit import api, auth
from safe_kit.db import connect
from test_disclosure_export import EXAMPLE, SCHEMA, load
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import (  # dworld: fixture
    ORIGINAL_MARKERS,
    approve,
    assign,
    body,
    case_url,
    create,
    dworld,
    first_version,
    grant,
    unassign,
)
from test_fixture_t06a_redaction import make_derived, world  # world: fixture


def export_url(world_data, version_id: str) -> str:
    return case_url(world_data, f"/{version_id}/export")


def export_as(world_data, who: str, version_id: str):
    return request_as(world_data["people"][who], "get", export_url(world_data, version_id))


def schema_errors(document) -> list[str]:
    return [
        error.message
        for error in Draft202012Validator(load(SCHEMA)).iter_errors(document)
    ]


def original_markers(world_data) -> list[str]:
    """Text that only the original holds: its ID and checksum, every file name of the case, the redaction IDs and the
    case title, on top of the P6 markers (removed text, reason, file names). An export that contains any of them fails."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id::text, original_filename, sha256_plaintext FROM evidence_items WHERE case_id = %s",
            (world_data["case_id"],),
        ).fetchall()
        redactions = conn.execute(
            """
            SELECT r.id::text FROM evidence_redactions r
            JOIN evidence_items e ON e.id = r.original_evidence_id
            WHERE e.case_id = %s
            """,
            (world_data["case_id"],),
        ).fetchall()
    original_id, original_sha = world_data["original_id"], None
    markers = list(ORIGINAL_MARKERS) + ["Redaction case"]
    for evidence_id, filename, sha in rows:
        markers.append(filename)
        if evidence_id == original_id:
            original_sha = sha
    markers.append(original_id)
    markers.extend(row[0] for row in redactions)
    assert original_sha is not None
    markers.append(original_sha)  # the checksum of the original, not of its derivative
    return markers


def audit_rows(version_id: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT actor_id::text, details::text FROM audit_events
            WHERE action = 'disclosure.exported' AND target_id = %s
            ORDER BY created_at, id
            """,
            (version_id,),
        ).fetchall()


@requires_database
def test_the_original_markers_are_real_so_the_negative_checks_are_not_vacuous(dworld):
    # The original's checksum and the file names are stored, so the negative checks below can fail.
    markers = original_markers(dworld)
    assert "notes.txt" in markers and "Redaction case" in markers
    assert len(markers) >= 10


@requires_database
def test_an_approved_version_exports_a_document_that_matches_the_schema_and_the_example(dworld):
    view, derived = first_version(dworld)
    assert (
        approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], view["digest"]).status_code
        == 201
    )
    response = export_as(dworld, "triager", view["id"])

    assert response.status_code == 200, response.text
    document = response.json()
    assert schema_errors(document) == []
    example = load(EXAMPLE)
    assert set(document) == set(example)
    assert set(document["disclosure_export"]) == set(example["disclosure_export"])
    body_view = document["disclosure_export"]
    assert body_view["status"] == "approved"
    assert body_view["version"] == body_view["latest_version"] == 1
    assert body_view["digest"] == view["digest"] == body_view["approval"]["digest"]
    assert [entry["evidence_id"] for entry in body_view["items"]] == [derived]
    assert body_view["manifest"]["items"][0]["removed"] == [{"from_line": 3, "to_line": 3, "lines": 1}]


@requires_database
def test_the_export_holds_no_original_content_file_name_checksum_or_reason(dworld):
    view, derived = first_version(dworld)
    assert (
        approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], view["digest"]).status_code
        == 201
    )
    response = export_as(dworld, "triager", view["id"])
    assert response.status_code == 200, response.text

    for marker in original_markers(dworld):
        assert marker not in response.text, marker
    # The derivative's own checksum is part of the export; the original's is not (checked above).
    derived_sha = view["evidence"][0]["sha256_plaintext"]
    assert derived_sha in response.text


@requires_database
def test_a_superseded_version_is_exported_with_the_history_status(dworld):
    first, derived = first_version(dworld)
    assert (
        approve(dworld, "approver", first["id"], first["recipient"], first["purpose"], first["digest"]).status_code
        == 201
    )
    changed = create(
        dworld,
        "triager",
        body([derived], package_id=first["package_id"], expected_version=1, purpose="Changed after the approval"),
    )
    assert changed.status_code == 201, changed.text

    history = export_as(dworld, "triager", first["id"])
    assert history.status_code == 200, history.text
    document = history.json()
    assert schema_errors(document) == []
    assert document["disclosure_export"]["status"] == "superseded"
    assert document["disclosure_export"]["version"] == 1
    assert document["disclosure_export"]["latest_version"] == 2
    assert document["disclosure_export"]["purpose"] == first["purpose"]

    # The later version has no approval yet, so it cannot be exported.
    current = export_as(dworld, "triager", changed.json()["id"])
    assert current.status_code == 409
    assert current.json()["detail"] == {"status": "version_not_approved"}


@requires_database
def test_an_unapproved_version_is_refused_and_no_document_is_returned(dworld):
    view, _ = first_version(dworld)
    response = export_as(dworld, "triager", view["id"])

    assert response.status_code == 409
    assert response.json()["detail"] == {"status": "version_not_approved"}
    assert "disclosure_export" not in response.text
    assert audit_rows(view["id"]) == []


@requires_database
def test_the_export_is_identical_across_two_calls(dworld):
    view, _ = first_version(dworld)
    assert (
        approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], view["digest"]).status_code
        == 201
    )
    first = export_as(dworld, "triager", view["id"])
    second = export_as(dworld, "triager", view["id"])

    assert first.status_code == second.status_code == 200
    assert first.content == second.content


@requires_database
def test_the_export_needs_a_login_and_the_read_permission(dworld):
    view, _ = first_version(dworld)
    assert (
        approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], view["digest"]).status_code
        == 201
    )
    api.app.dependency_overrides.pop(auth.current_user, None)  # no caller: the real login check runs
    anonymous = TestClient(api.app).get(export_url(dworld, view["id"]))
    assert anonymous.status_code == 401

    auditor = db_user("auditor")  # an auditor reads the audit trail, not the packages
    assert request_as(auditor, "get", export_url(dworld, view["id"])).status_code == 403


@requires_database
def test_a_case_the_caller_cannot_see_answers_as_a_missing_case(dworld, monkeypatch):
    view, _ = first_version(dworld)
    assert (
        approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], view["digest"]).status_code
        == 201
    )
    # The approver reads every case (cases:read_all), so the stranger is a reviewer that is not assigned to this case.
    stranger = db_user("reviewer")
    grant(monkeypatch, "reviewer", "disclosures:read")
    hidden = request_as(stranger, "get", export_url(dworld, view["id"]))
    missing_case = request_as(stranger, "get", f"/api/v1/cases/{uuid.uuid4()}/disclosures/{view['id']}/export")
    assert hidden.status_code == missing_case.status_code == 404
    assert hidden.json() == missing_case.json() == {"detail": "case not found"}

    unknown_version = export_as(dworld, "triager", str(uuid.uuid4()))
    assert unknown_version.status_code == 404
    assert unknown_version.json() == {"detail": "disclosure version not found"}


@requires_database
def test_each_export_writes_one_audit_row_with_identifiers_and_no_content(dworld):
    view, _ = first_version(dworld)
    assert (
        approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], view["digest"]).status_code
        == 201
    )
    assert export_as(dworld, "triager", view["id"]).status_code == 200
    assert export_as(dworld, "triager", view["id"]).status_code == 200

    rows = audit_rows(view["id"])
    assert len(rows) == 2
    triager_id = dworld["people"]["triager"]
    for actor_id, details in rows:
        assert actor_id == triager_id
        assert json.loads(details).keys() == {"case_id", "package_id", "version", "digest", "status", "schema_version"}
        assert "Member CERT" not in details and "Rapid alert on the affected gateway" not in details
        for marker in original_markers(dworld):
            assert marker not in details, marker


@requires_database
def test_a_withheld_item_is_only_a_placeholder_in_the_export(dworld):
    assign(dworld["case_id"], dworld["people"]["triager"], dworld["people"]["triager"])
    red = make_derived(dworld, "RED")["derived"]["id"]
    created = create(dworld, "triager", body([red], tlp_audience="RED"))
    assert created.status_code == 201, created.text
    view = created.json()
    red_sha = view["evidence"][0]["sha256_plaintext"]
    assert (
        approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], view["digest"]).status_code
        == 201
    )
    unassign(dworld["case_id"], dworld["people"]["triager"])  # the triager keeps the case, not the RED item

    response = export_as(dworld, "triager", view["id"])
    assert response.status_code == 200, response.text
    document = response.json()
    assert schema_errors(document) == []
    assert document["disclosure_export"]["items"] == [{"withheld": True}]
    assert document["disclosure_export"]["withheld_count"] == 1
    assert document["disclosure_export"]["manifest"]["items"] == []
    assert red not in response.text
    assert red_sha not in response.text
