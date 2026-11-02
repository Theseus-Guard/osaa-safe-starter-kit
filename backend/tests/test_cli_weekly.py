"""The case weekly commands of the container CLI (case weekly list, case weekly show).

The API is replaced by the fake of tests/test_cli_cases.py, so these tests need no server and no database.
"""

import getpass
import io
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from safe_kit import cli
from test_cli_cases import BASE_URL, CASE_ID, CASE_PATH, FULL_VIEW, ME_PATH, PASSWORD, FakeAPI, compact, route_me, run

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = Draft202012Validator(
    json.loads((ROOT / "schemas" / "weekly-update.v0.json").read_text(encoding="utf-8")),
    format_checker=FormatChecker(),
)

WEEKLY_PATH = f"{CASE_PATH}/weekly-updates"
UPDATE_ID = "dddddddd-eeee-4fff-8000-111111111111"
UPDATE_PATH = f"{WEEKLY_PATH}/{UPDATE_ID}"
UPDATE_BODY = {
    "weekly_update_schema_version": "0.1.0",
    "weekly_update_id": UPDATE_ID,
    "case_id": CASE_ID,
    "case_reference": "SAFE-2026-000001",
    "sequence": 1,
    "generated_at": "2026-11-02T09:00:00Z",
    "next_update_due_at": "2026-11-09T09:00:00Z",
    "significant_risks": [{"area": "severity", "severity": "high"}],
    "deadline_tasks": [{"kind": "affected_customers", "due_at": "2026-11-05T09:00:00Z", "status": "open"}],
}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SAFE_API_URL", BASE_URL)
    monkeypatch.delenv("SAFE_USERNAME", raising=False)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(PASSWORD + "\n"))

    def no_prompt(prompt=""):
        raise AssertionError("getpass must not be used when stdin is not a terminal")

    monkeypatch.setattr(getpass, "getpass", no_prompt)
    return FakeAPI(monkeypatch)


def test_the_sample_body_follows_the_project_schema():
    VALIDATOR.validate(UPDATE_BODY)


def test_list_prints_one_line_per_update(api, capsys):
    api.route("GET", WEEKLY_PATH, body=[UPDATE_BODY])
    route_me(api, FULL_VIEW)
    code, out, err = run(capsys, "case", "weekly", "list", CASE_ID, "--username", "alice")
    assert code == 0
    assert UPDATE_ID in out
    assert "2026-11-02T09:00:00Z" in out
    assert "2026-11-09T09:00:00Z" in out


def test_list_says_so_when_the_case_has_no_update(api, capsys):
    api.route("GET", WEEKLY_PATH, body=[])
    route_me(api, FULL_VIEW)
    code, out, err = run(capsys, "case", "weekly", "list", CASE_ID, "--username", "alice")
    assert code == 0
    assert "No weekly updates for this case" in out


def test_list_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", WEEKLY_PATH, body=[UPDATE_BODY])
    code, out, err = run(capsys, "case", "weekly", "list", CASE_ID, "--username", "alice", "--json")
    assert code == 0
    assert out == compact([UPDATE_BODY]).decode("utf-8") + "\n"
    assert ("GET", ME_PATH) not in api.paths()


def test_show_prints_the_update(api, capsys):
    api.route("GET", UPDATE_PATH, body=UPDATE_BODY)
    route_me(api, FULL_VIEW)
    code, out, err = run(capsys, "case", "weekly", "show", CASE_ID, UPDATE_ID, "--username", "alice")
    assert code == 0
    assert "Weekly update 1 of SAFE-2026-000001:" in out
    assert "severity: high" in out
    assert "affected_customers: due 2026-11-05T09:00:00Z, open" in out


def test_show_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", UPDATE_PATH, body=UPDATE_BODY)
    code, out, err = run(capsys, "case", "weekly", "show", CASE_ID, UPDATE_ID, "--username", "alice", "--json")
    assert code == 0
    assert out == compact(UPDATE_BODY).decode("utf-8") + "\n"


def test_show_not_found_reports_the_message_and_exits_1(api, capsys):
    api.route("GET", UPDATE_PATH, status=404, body={"detail": "weekly update not found"})
    code, out, err = run(capsys, "case", "weekly", "show", CASE_ID, UPDATE_ID, "--username", "alice")
    assert code == 1
    assert "case or weekly update not found" in err


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("case", "weekly", "list", "not-a-case"), id="list-case-id"),
        pytest.param(("case", "weekly", "show", CASE_ID, "not-an-update"), id="show-update-id"),
    ],
)
def test_ids_must_be_uuids_before_any_request(api, capsys, argv):
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 2
    assert api.calls == []
