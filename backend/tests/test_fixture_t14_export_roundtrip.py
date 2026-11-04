"""F2 export roundtrip (T14, export side), without the database.

The two export documents are project-defined, versioned JSON (schemas/catalog-export.v0.json and
schemas/disclosure-export.v0.json). They are NOT official SAFE schemas, and no SAFE API is claimed for them.

Reading policy, as the project chose it for F2 (plans/f0/api-draft.md sections 3.1 and 3.3):
- Unknown field: refused, never dropped. The API draft says "Bilinmeyen alan reddedilir" (400 unknown_field), and the
  schemas declare additionalProperties false at every level.
- Unknown schema version: refused before any other field is read. The reader accepts exactly the version named by the
  schema's const. A breaking change ships as a new version, so an older reader refuses a newer file instead of guessing.
- Namespace duplicate: the same ID twice in one document (two catalog entries, or two visible disclosure items) is
  refused.

The reader below is a test helper. Exports are not imported: the import dry-run reads case records only
(safe_kit.case_import), and it refuses an export document by name. The database half of the roundtrip
(real exports from the API, the CLI and the approval flow) is in test_fixture_t14_export_roundtrip_db.py.
"""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from safe_kit.case_import import (
    ImportFileError,
    parse_import_file,
    received_format_version,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
MISSING = object()

KINDS = {
    "catalog": {
        "schema": SCHEMAS / "catalog-export.v0.json",
        "example": SCHEMAS / "examples" / "catalog-export-valid.json",
        "version_key": "catalog_export_schema_version",
    },
    "disclosure": {
        "schema": SCHEMAS / "disclosure-export.v0.json",
        "example": SCHEMAS / "examples" / "disclosure-export-valid.json",
        "version_key": "disclosure_export_schema_version",
    },
}
SUPPORTED_VERSION = "0.1.0"


class ExportRefused(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _reject_constant(name: str):
    raise ValueError(f"{name} is not allowed in JSON")


def read_export(raw: bytes, kind: str) -> dict:
    """Read one export document under the F2 policy. Refuses it whole, with the reasons; never returns a partial one."""
    spec = KINDS[kind]
    try:
        document = json.loads(raw, parse_constant=_reject_constant)
    except ValueError:
        raise ExportRefused(["the file is not valid JSON"]) from None
    if not isinstance(document, dict):
        raise ExportRefused(["the export must be a JSON object"])
    version = document.get(spec["version_key"], MISSING)
    if type(version) is not str or version != SUPPORTED_VERSION:
        raise ExportRefused(
            [
                f"unsupported {spec['version_key']}; this reader reads {SUPPORTED_VERSION}"
            ]
        )
    schema = json.loads(spec["schema"].read_text(encoding="utf-8"))
    errors = sorted(
        error.message for error in Draft202012Validator(schema).iter_errors(document)
    )
    if errors:
        raise ExportRefused(errors)
    duplicates = _namespace_duplicates(document, kind)
    if duplicates:
        raise ExportRefused(
            [f"duplicate {duplicates[0][0]} in one document: {duplicates[0][1]}"]
        )
    return document


def _namespace_duplicates(document: dict, kind: str) -> list[tuple[str, str]]:
    if kind == "catalog":
        ids = [entry["id"] for entry in document["catalog_export"]["entries"]]
        label = "catalog entry id"
    else:
        ids = [
            item["evidence_id"]
            for item in document["disclosure_export"]["items"]
            if not item["withheld"]
        ]
        label = "evidence_id"
    seen: set[str] = set()
    for value in ids:
        if value in seen:
            return [(label, value)]
        seen.add(value)
    return []


def canonical(document: dict) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def example_bytes(kind: str) -> bytes:
    return KINDS[kind]["example"].read_bytes()


def container(document: dict, path: tuple) -> dict:
    node = document
    for step in path:
        node = node[step]
    return node


# --- the documents and the policy statements ----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_each_export_schema_is_valid_and_says_it_is_project_defined(kind):
    schema = json.loads(KINDS[kind]["schema"].read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert "draft 0.1.0" in schema["title"]
    assert "NOT an official SAFE schema" in schema["$comment"]
    assert (
        schema["properties"][KINDS[kind]["version_key"]]["const"] == SUPPORTED_VERSION
    )


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_the_example_reads_back_unchanged_through_the_reader(kind):
    document = read_export(example_bytes(kind), kind)
    written = json.dumps(document, indent=2).encode(
        "utf-8"
    )  # written to a file, then read again
    assert canonical(read_export(written, kind)) == canonical(document)


# --- unknown field: refused at every level, never dropped -----------------------------------------------------------------

CATALOG_CONTAINERS = [
    (),
    ("catalog_export",),
    ("catalog_export", "entries", 0),
    ("catalog_export", "entries", 0, "versions", 0),
    ("catalog_export", "entries", 0, "versions", 0, "verification"),
]
DISCLOSURE_CONTAINERS = [
    (),
    ("disclosure_export",),
    ("disclosure_export", "template"),
    ("disclosure_export", "items", 0),
    ("disclosure_export", "manifest"),
    ("disclosure_export", "approval"),
]


@pytest.mark.parametrize(
    ("kind", "path"),
    [("catalog", path) for path in CATALOG_CONTAINERS]
    + [("disclosure", path) for path in DISCLOSURE_CONTAINERS],
)
def test_an_unknown_field_is_refused_at_every_level_and_never_dropped(kind, path):
    document = json.loads(example_bytes(kind))
    # The example's verification is null for some versions; give that container a value so the path exists.
    if kind == "catalog" and path[-1:] == ("verification",):
        container(document, path[:-1])["verification"] = {
            "subject": "s",
            "method": "m",
            "result": "pass",
            "verified_at": "2026-11-01T08:30:00+00:00",
        }
    container(document, path)["unexpected_field"] = "value"
    # Some containers are oneOf (a visible item, a verification), so the reason can be the oneOf message instead.
    with pytest.raises(ExportRefused) as refused:
        read_export(json.dumps(document).encode("utf-8"), kind)
    assert refused.value.reasons


# --- unknown schema version: refused before any other field is read --------------------------------------------------


@pytest.mark.parametrize("kind", sorted(KINDS))
@pytest.mark.parametrize("bad", ["0.2.0", "1.0.0", "0.1", " 0.1.0", 1, None, MISSING])
def test_an_unknown_schema_version_is_refused_before_any_other_field_is_read(kind, bad):
    document = json.loads(example_bytes(kind))
    version_key = KINDS[kind]["version_key"]
    if bad is MISSING:
        del document[version_key]
    else:
        document[version_key] = bad
    document["unexpected_field"] = (
        "value"  # a second fault: the version reason must come first and alone
    )
    with pytest.raises(ExportRefused) as refused:
        read_export(json.dumps(document).encode("utf-8"), kind)
    assert refused.value.reasons == [
        f"unsupported {version_key}; this reader reads 0.1.0"
    ]


# --- namespace duplicate: detected ----------------------------------------------------------------------------------------


def test_a_duplicate_catalog_entry_id_in_one_file_is_refused_as_a_namespace_duplicate():
    document = json.loads(example_bytes("catalog"))
    entries = document["catalog_export"]["entries"]
    entries[1]["id"] = entries[0]["id"]
    with pytest.raises(ExportRefused) as refused:
        read_export(json.dumps(document).encode("utf-8"), "catalog")
    assert refused.value.reasons == [
        f"duplicate catalog entry id in one document: {entries[0]['id']}"
    ]


def test_a_duplicate_visible_evidence_id_in_one_disclosure_export_is_refused():
    document = json.loads(example_bytes("disclosure"))
    items = document["disclosure_export"]["items"]
    visible = next(item for item in items if not item["withheld"])
    items.append(copy.deepcopy(visible))
    with pytest.raises(ExportRefused) as refused:
        read_export(json.dumps(document).encode("utf-8"), "disclosure")
    assert refused.value.reasons == [
        f"duplicate evidence_id in one document: {visible['evidence_id']}"
    ]


def test_withheld_items_are_not_counted_as_duplicates():
    document = json.loads(example_bytes("disclosure"))
    items = document["disclosure_export"]["items"]
    items.append({"withheld": True})
    items.append({"withheld": True})
    read_export(json.dumps(document).encode("utf-8"), "disclosure")  # no ExportRefused


# --- the case import reader never takes an export file as records --------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_an_export_file_is_never_read_as_a_case_import_file(kind):
    raw = example_bytes(kind)
    with pytest.raises(
        ImportFileError, match="export document, not a case import file"
    ):
        parse_import_file(raw)
    assert received_format_version(raw) is None
