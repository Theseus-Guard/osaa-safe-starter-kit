"""The export document of an approved disclosure version (safe_kit.disclosure.export_document), without a database.

The document is checked against schemas/disclosure-export.v0.json and its example, and its content rules are checked on
in-memory versions: derivatives only, withheld items as placeholders, no source checksum, no redaction reason, no file
name, and no export time. The database tests (test_disclosure_export_db.py) cover the same through the API.
"""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from safe_kit import disclosure
from safe_kit.disclosure import Approval, Version
from test_disclosure import (
    APPROVER,
    CASE_ID,
    ITEM_ONE,
    ITEM_TWO,
    PACKAGE_ID,
    VERSION_ID,
    item,
    version,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schemas" / "disclosure-export.v0.json"
EXAMPLE = ROOT / "schemas" / "examples" / "disclosure-export-valid.json"
APPROVAL_ID = "cccccccc-dddd-4eee-8fff-000000000009"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def errors_of(document) -> list[str]:
    return [
        error.message
        for error in Draft202012Validator(load(SCHEMA)).iter_errors(document)
    ]


def approved(*, is_latest: bool = True, **changes) -> Version:
    """An approved version whose digest is the digest of its content, as the database stores it."""
    base = version(is_latest=is_latest, **changes)
    base = replace(base, digest=disclosure.digest_of_version(base))
    approval = Approval(
        id=APPROVAL_ID,
        version_id=base.id,
        recipient=base.recipient,
        purpose=base.purpose,
        digest=base.digest,
        preparer_id=base.created_by,
        approved_by=APPROVER,
        approved_at=datetime(2026, 11, 2, 10, tzinfo=UTC),
    )
    return replace(base, approval=approval)


def export_of(v: Version, visible: list[bool], latest: int = 1) -> dict:
    return disclosure.export_document(v, visible, latest)


def test_the_schema_is_a_valid_draft_2020_12_schema_and_the_example_validates():
    Draft202012Validator.check_schema(load(SCHEMA))
    assert errors_of(load(EXAMPLE)) == []


def test_an_approved_version_exports_a_document_that_matches_the_schema():
    document = export_of(approved(items=[item(ITEM_ONE)]), visible=[True])

    assert errors_of(document) == []
    body = document["disclosure_export"]
    assert document["disclosure_export_schema_version"] == "0.1.0"
    assert (
        body["status"] == "approved" and body["latest_version"] == body["version"] == 1
    )
    assert body["approval"] == {
        "id": APPROVAL_ID,
        "approved_by": APPROVER,
        "approved_at": "2026-11-02T10:00:00+00:00",
        "digest": body["digest"],
        "separation_rule": "preparer_differs_from_approver",
    }
    assert body["items"] == [
        {
            "withheld": False,
            "evidence_id": ITEM_ONE,
            "media_type": "text/plain",
            "size_bytes": 42,
            "sha256_plaintext": "c" * 64,
            "tlp_label": "AMBER",
        }
    ]
    assert body["manifest"]["items"] == [
        {
            "evidence_id": ITEM_ONE,
            "lines_total": 4,
            "removed_lines": 1,
            "removed": [{"from_line": 3, "to_line": 3, "lines": 1}],
        }
    ]


def test_a_superseded_version_is_marked_as_history_and_still_matches_the_schema():
    document = export_of(approved(is_latest=False), visible=[True], latest=2)

    assert errors_of(document) == []
    body = document["disclosure_export"]
    assert body["status"] == "superseded"
    assert body["version"] == 1 and body["latest_version"] == 2
    assert body["approval"]["digest"] == body["digest"]


def test_a_withheld_item_is_only_a_placeholder_with_no_detail():
    v = approved(items=[item(ITEM_ONE), item(ITEM_TWO, label="RED")])
    document = export_of(v, visible=[True, False])
    body = document["disclosure_export"]

    assert errors_of(document) == []
    assert body["items"][1] == {"withheld": True}
    assert body["withheld_count"] == 1
    assert [entry["evidence_id"] for entry in body["manifest"]["items"]] == [ITEM_ONE]
    assert ITEM_TWO not in json.dumps(document)
    assert "RED" not in json.dumps(body["items"][1])


def test_no_source_checksum_redaction_reason_or_export_time_is_exported():
    v = approved(items=[item(ITEM_ONE)])
    # The stored manifest carries the original's checksum and, on a removed range, the reason. Neither may be exported.
    v.manifest["items"][0]["removed"][0]["reason"] = "Reason marker 9911"
    text = json.dumps(export_of(v, visible=[True]))

    assert "d" * 64 not in text  # the source_sha256 of manifest_for
    assert "9911" not in text
    assert "export_time" not in text and "exported_at" not in text


def test_the_stored_manifest_really_holds_the_fields_the_export_must_drop():
    # The guard above is not vacuous: the stored version carries the reason and the original's checksum.
    v = approved(items=[item(ITEM_ONE)])
    v.manifest["items"][0]["removed"][0]["reason"] = "Reason marker 9911"
    stored = json.dumps(v.manifest)
    assert "9911" in stored and "d" * 64 in stored


def test_the_export_is_the_same_document_for_two_calls():
    v = approved(items=[item(ITEM_ONE), item(ITEM_TWO)])
    first = export_of(v, visible=[True, True])
    second = export_of(v, visible=[True, True])

    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_the_schema_refuses_a_file_name_an_export_time_or_an_original_checksum():
    document = export_of(approved(items=[item(ITEM_ONE)]), visible=[True])
    cases = [
        (
            "file name on an item",
            lambda d: d["disclosure_export"]["items"][0].update(file_name="notes.txt"),
        ),
        ("export time", lambda d: d.update(exported_at="2026-11-02T10:00:00+00:00")),
        (
            "original checksum in the manifest",
            lambda d: d["disclosure_export"]["manifest"]["items"][0].update(
                source_sha256="d" * 64
            ),
        ),
        (
            "evidence ID on a withheld placeholder",
            lambda d: d["disclosure_export"]["items"].append(
                {"withheld": True, "evidence_id": ITEM_TWO}
            ),
        ),
        ("unknown status", lambda d: d["disclosure_export"].update(status="draft")),
    ]
    for label, change in cases:
        broken = copy.deepcopy(document)
        change(broken)
        assert errors_of(broken), label


def test_the_example_has_the_same_fields_as_a_live_export():
    example = load(EXAMPLE)
    live = export_of(approved(items=[item(ITEM_ONE)]), visible=[True])

    assert set(example) == set(live)
    assert set(example["disclosure_export"]) == set(live["disclosure_export"])
    assert set(example["disclosure_export"]["approval"]) == set(
        live["disclosure_export"]["approval"]
    )
    assert set(example["disclosure_export"]["template"]) == set(
        live["disclosure_export"]["template"]
    )
    assert set(example["disclosure_export"]["manifest"]["items"][0]) == set(
        live["disclosure_export"]["manifest"]["items"][0]
    )


@pytest.mark.parametrize("template", list(disclosure.TEMPLATES))
def test_every_template_exports_with_its_own_sections(template):
    document = export_of(approved(template=template), visible=[True])

    assert errors_of(document) == []
    assert document["disclosure_export"]["template"]["key"] == template
    assert (
        document["disclosure_export"]["template"]["title"]
        == disclosure.TEMPLATES[template].title
    )


def test_the_case_and_package_are_named_by_id_only():
    document = export_of(approved(items=[item(ITEM_ONE)]), visible=[True])
    body = document["disclosure_export"]

    assert body["case_id"] == CASE_ID
    assert body["package_id"] == PACKAGE_ID
    assert body["version_id"] == VERSION_ID
