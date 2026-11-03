"""Weekly updates without a database: the cadence rule, the document builder, and the project-defined schema.

The database tests (creation, visibility, the API and the worker) are in tests/test_weekly_updates_db.py.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from safe_kit import weekly_updates
from safe_kit.weekly_updates import SIGNIFICANT_SEVERITIES, build_document, is_due

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "weekly-update.v0.json"
EXAMPLE_PATH = ROOT / "schemas" / "examples" / "weekly-update-valid.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

GENERATED = datetime(2026, 11, 2, 9, 30, 15, tzinfo=UTC)


def document(**overrides) -> dict:
    values = {
        "update_id": "7d1c2a4e-3b5f-4e8a-9c6d-1f2e3a4b5c6d",
        "case_id": "3f2b8c1e-6a4d-4e9b-8f1a-2c7d9e0b5a11",
        "case_reference": "SAFE-2026-000042",
        "sequence": 1,
        "generated_at": GENERATED,
        "risks": [("severity", "high")],
        "tasks": [("affected_customers", GENERATED + timedelta(hours=72), "open")],
    }
    values.update(overrides)
    return build_document(**values)


def test_only_high_and_critical_impacts_are_significant():
    assert SIGNIFICANT_SEVERITIES == ("high", "critical")


def test_first_update_is_due_when_the_case_has_none():
    assert is_due(None, GENERATED) is True


def test_next_update_is_due_seven_elapsed_days_after_the_previous_one():
    assert (
        is_due(GENERATED, GENERATED + timedelta(days=7) - timedelta(seconds=1)) is False
    )
    assert is_due(GENERATED, GENERATED + timedelta(days=7)) is True
    assert is_due(GENERATED, GENERATED + timedelta(days=30)) is True


def test_document_follows_the_schema_and_its_due_time_is_seven_days_later():
    doc = document()
    VALIDATOR.validate(doc)
    assert doc["generated_at"] == "2026-11-02T09:30:15Z"
    assert doc["next_update_due_at"] == "2026-11-09T09:30:15Z"
    assert doc["significant_risks"] == [{"area": "severity", "severity": "high"}]
    assert doc["deadline_tasks"] == [
        {
            "kind": "affected_customers",
            "due_at": "2026-11-05T09:30:15Z",
            "status": "open",
        }
    ]


def test_document_timestamps_are_utc_with_a_z_suffix_for_any_local_zone():
    istanbul = datetime(
        2026, 11, 2, 12, 30, 15, tzinfo=ZoneInfo("Europe/Istanbul")
    )  # UTC+3, no DST
    doc = document(generated_at=istanbul)
    assert doc["generated_at"] == "2026-11-02T09:30:15Z"
    assert doc["next_update_due_at"] == "2026-11-09T09:30:15Z"


def test_document_keys_are_exactly_the_schema_properties():
    assert set(document()) == set(SCHEMA["properties"])


def test_naive_timestamps_are_refused():
    with pytest.raises(ValueError):
        document(generated_at=datetime(2026, 11, 2, 9, 30))


def test_generation_refuses_a_naive_now_before_any_database_access():
    with pytest.raises(ValueError):
        weekly_updates.generate_due_updates(datetime(2026, 11, 2, 9, 0))


def test_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(SCHEMA)


def test_example_passes_the_schema():
    VALIDATOR.validate(json.loads(EXAMPLE_PATH.read_text(encoding="utf-8")))


def test_schema_holds_no_free_text_fields():
    # Free text (scope, reasons, notes) stays in the case. No property of the document may carry it.
    names: set[str] = set(SCHEMA["properties"])
    for definition in SCHEMA["$defs"].values():
        names |= set(definition.get("properties", {}))
    assert names.isdisjoint(
        {
            "scope",
            "reason",
            "note",
            "notes",
            "text",
            "rationale",
            "description",
            "basis",
        }
    )


def _valid() -> dict:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def _with(key: str, value) -> dict:
    doc = _valid()
    doc[key] = value
    return doc


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param(_with("significant_risks", []), id="no-significant-risk"),
        pytest.param(
            _with("significant_risks", [{"area": "scale", "severity": "medium"}]),
            id="medium-is-not-significant",
        ),
        pytest.param(
            _with("significant_risks", [{"area": "legal", "severity": "high"}]),
            id="unknown-area",
        ),
        pytest.param(_with("sequence", 0), id="sequence-below-one"),
        pytest.param(
            _with("generated_at", "2026-11-09T08:00:00+00:00"), id="timestamp-without-z"
        ),
        pytest.param(_with("scope", "free text"), id="free-text-field"),
        pytest.param(
            _with("weekly_update_schema_version", "0.2.0"), id="other-version"
        ),
        pytest.param(
            _with(
                "deadline_tasks",
                [
                    {
                        "kind": "weekly_update",
                        "due_at": "2026-11-09T08:00:00Z",
                        "status": "open",
                    }
                ],
            ),
            id="weekly-kind-is-not-a-safe-task",
        ),
    ],
)
def test_schema_rejects_documents_that_break_the_format(doc):
    with pytest.raises(ValidationError):
        VALIDATOR.validate(doc)


def test_weekly_updates_module_exposes_the_schema_version_used_in_documents():
    assert (
        document()["weekly_update_schema_version"]
        == weekly_updates.WEEKLY_UPDATE_SCHEMA_VERSION
    )
    assert (
        SCHEMA["properties"]["weekly_update_schema_version"]["const"]
        == weekly_updates.WEEKLY_UPDATE_SCHEMA_VERSION
    )
