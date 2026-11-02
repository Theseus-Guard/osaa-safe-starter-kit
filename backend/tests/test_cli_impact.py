"""Risk commands of the container CLI (case risk show and case risk set), and the risk score in case list and case show.

The API is replaced by the fake behind urllib.request.urlopen from test_cli_cases.py.
"""

import getpass
import io
import uuid

import pytest

from safe_kit import cli
from test_cli_cases import (
    BASE_URL,
    CASE_BODY,
    CASE_ID,
    PASSWORD,
    SUMMARY,
    FakeAPI,
    compact,
    run,
)

SCORES_PATH = f"/api/v1/cases/{CASE_ID}/impact-scores"
CASE_PATH = f"/api/v1/cases/{CASE_ID}"
LIST_PATH = "/api/v1/cases"
SCORES = {
    "case_id": CASE_ID,
    "version": 2,
    "c": 4,
    "b": 3,
    "a": 4,
    "risk_score": 3.7,
    "created_by": "44444444-4444-4444-4444-444444444444",
    "created_at": "2026-11-02T09:00:00Z",
}
UNSET = {
    "case_id": CASE_ID,
    "version": 0,
    "c": None,
    "b": None,
    "a": None,
    "risk_score": None,
    "created_by": None,
    "created_at": None,
}
ASSESSED_CASE = {
    **CASE_BODY,
    "risk_score": 3.7,
    "impact_c": 4,
    "impact_b": 3,
    "impact_a": 4,
}
ASSESSED_SUMMARY = {
    **SUMMARY,
    "risk_score": 3.7,
    "impact_c": 4,
    "impact_b": 3,
    "impact_a": 4,
}
UNASSESSED_SUMMARY = {
    **SUMMARY,
    "id": "99999999-8888-4777-8666-555555555555",
    "title": "Second case",
    "reference": "SAFE-2026-000002",
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


def write_calls(api: FakeAPI) -> list[dict]:
    """The risk writes only: the login and logout calls are POSTs too, so the path is checked as well."""
    return [
        call
        for call in api.calls
        if call["method"] == "POST" and call["path"] == SCORES_PATH
    ]


# --- case risk show ----------------------------------------------------------------------------------------------------


def test_show_prints_the_version_the_score_and_the_three_labelled_values(api, capsys):
    api.route("GET", SCORES_PATH, body=SCORES)
    code, out, err = run(capsys, "case", "risk", "show", CASE_ID, "--username", "alice")
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == "Impact values, version 2:"
    assert any(
        line.startswith("  Risk score:") and line.endswith("3.7") for line in lines
    )
    assert any(
        line.startswith("  C – Confidentiality:") and line.endswith(" 4")
        for line in lines
    )
    assert any(
        line.startswith("  B – Integrity:") and line.endswith(" 3") for line in lines
    )
    assert any(
        line.startswith("  A – Availability:") and line.endswith(" 4") for line in lines
    )


def test_show_says_not_assessed_and_not_set_when_no_values_are_recorded(api, capsys):
    api.route("GET", SCORES_PATH, body=UNSET)
    code, out, _ = run(capsys, "case", "risk", "show", CASE_ID, "--username", "alice")
    assert code == 0
    assert "No impact values are recorded for this case yet." in out
    assert "Not assessed" in out
    assert out.count("Not set") == 3


def test_show_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", SCORES_PATH, body=SCORES)
    code, out, _ = run(
        capsys, "case", "risk", "show", CASE_ID, "--json", "--username", "alice"
    )
    assert code == 0
    assert out == compact(SCORES).decode("utf-8") + "\n"


def test_show_reports_a_missing_or_hidden_case(api, capsys):
    api.route("GET", SCORES_PATH, status=404, body={"detail": "case not found"})
    code, _, err = run(capsys, "case", "risk", "show", CASE_ID, "--username", "alice")
    assert code == 1
    assert "case not found" in err


# --- case risk set -----------------------------------------------------------------------------------------------------


def test_set_sends_the_three_values_with_the_version_and_an_idempotency_key(
    api, capsys
):
    api.route("POST", SCORES_PATH, body=SCORES)
    code, out, err = run(
        capsys,
        "case",
        "risk",
        "set",
        CASE_ID,
        "--c",
        "4",
        "--b",
        "3",
        "--a",
        "4",
        "--expected-version",
        "1",
        "--username",
        "alice",
    )
    assert code == 0, err
    [call] = write_calls(api)
    assert call["json"] == {"expected_version": 1, "c": 4, "b": 3, "a": 4}
    headers = {name.lower(): value for name, value in call["headers"].items()}
    assert str(uuid.UUID(headers["idempotency-key"])) == headers["idempotency-key"]
    assert out.startswith("Impact values saved as version 2.")
    assert "3.7" in out


def test_set_uses_the_idempotency_key_given_for_a_retry(api, capsys):
    api.route("POST", SCORES_PATH, body=SCORES)
    key = "retry-key-1"
    code, _, _ = run(
        capsys,
        "case",
        "risk",
        "set",
        CASE_ID,
        "--c",
        "4",
        "--expected-version",
        "0",
        "--idempotency-key",
        key,
        "--username",
        "alice",
    )
    assert code == 0
    headers = {
        name.lower(): value for name, value in write_calls(api)[0]["headers"].items()
    }
    assert headers["idempotency-key"] == key


def test_set_clears_a_value_that_is_left_out(api, capsys):
    api.route("POST", SCORES_PATH, body=SCORES)
    run(
        capsys,
        "case",
        "risk",
        "set",
        CASE_ID,
        "--c",
        "5",
        "--expected-version",
        "0",
        "--username",
        "alice",
    )
    assert write_calls(api)[0]["json"] == {
        "expected_version": 0,
        "c": 5,
        "b": None,
        "a": None,
    }


def test_set_json_prints_the_api_body_exactly(api, capsys):
    api.route("POST", SCORES_PATH, body=SCORES)
    code, out, _ = run(
        capsys,
        "case",
        "risk",
        "set",
        CASE_ID,
        "--c",
        "4",
        "--b",
        "3",
        "--a",
        "4",
        "--expected-version",
        "1",
        "--json",
        "--username",
        "alice",
    )
    assert code == 0
    assert out == compact(SCORES).decode("utf-8") + "\n"


@pytest.mark.parametrize("value", ["0", "6", "2.5", "3.0", "three"])
def test_set_refuses_a_value_outside_one_to_five_before_any_request(api, capsys, value):
    code, out, err = run(
        capsys,
        "case",
        "risk",
        "set",
        CASE_ID,
        "--c",
        value,
        "--expected-version",
        "0",
        "--username",
        "alice",
    )
    assert code == 2
    assert "must be a whole number from 1 to 5" in err
    assert write_calls(api) == []


def test_set_needs_the_expected_version(api, capsys):
    code, _, err = run(
        capsys, "case", "risk", "set", CASE_ID, "--c", "4", "--username", "alice"
    )
    assert code == 2
    assert "--expected-version" in err
    assert write_calls(api) == []


def test_set_refuses_a_negative_expected_version(api, capsys):
    code, _, err = run(
        capsys,
        "case",
        "risk",
        "set",
        CASE_ID,
        "--c",
        "4",
        "--expected-version",
        "-1",
        "--username",
        "alice",
    )
    assert code == 2
    assert "--expected-version must be 0 or more" in err
    assert write_calls(api) == []


def test_a_stale_version_is_reported_with_the_current_version_and_nothing_is_saved(
    api, capsys
):
    api.route(
        "POST",
        SCORES_PATH,
        status=409,
        body={"detail": {"status": "impact_version_conflict", "current_version": 3}},
    )
    code, out, err = run(
        capsys,
        "case",
        "risk",
        "set",
        CASE_ID,
        "--c",
        "4",
        "--expected-version",
        "1",
        "--username",
        "alice",
    )
    assert code == 1
    assert out == ""
    assert "nothing was saved" in err
    assert "The current version is 3" in err
    assert "--expected-version 3" in err


def test_a_closed_case_is_reported_as_closed(api, capsys):
    api.route("POST", SCORES_PATH, status=409, body={"detail": "case is closed"})
    code, _, err = run(
        capsys,
        "case",
        "risk",
        "set",
        CASE_ID,
        "--c",
        "4",
        "--expected-version",
        "0",
        "--username",
        "alice",
    )
    assert code == 1
    assert "the case is closed, so the impact values cannot change" in err


def test_a_caller_without_triage_is_told_what_the_permission_is(api, capsys):
    api.route("POST", SCORES_PATH, status=403, body={"detail": "forbidden"})
    code, _, err = run(
        capsys,
        "case",
        "risk",
        "set",
        CASE_ID,
        "--c",
        "4",
        "--expected-version",
        "0",
        "--username",
        "alice",
    )
    assert code == 1
    assert "saving the impact values needs cases:triage" in err


# --- the risk score in case list and case show -------------------------------------------------------------------------


def test_case_list_has_a_risk_column_with_the_score_or_not_assessed(api, capsys):
    api.route(
        "GET",
        LIST_PATH,
        body={"items": [ASSESSED_SUMMARY, UNASSESSED_SUMMARY], "next_cursor": None},
    )
    code, out, _ = run(capsys, "case", "list", "--username", "alice")
    assert code == 0
    lines = out.splitlines()
    assert "RISK" in lines[0]
    assert any("3.7" in line and "SAFE-2026-000001" in line for line in lines)
    assert any("Not assessed" in line and "SAFE-2026-000002" in line for line in lines)


def test_case_show_prints_the_score_with_its_three_values(api, capsys):
    api.route("GET", CASE_PATH, body=ASSESSED_CASE)
    code, out, _ = run(capsys, "case", "show", CASE_ID, "--username", "alice")
    assert code == 0
    assert "  Risk score:       3.7 (C 4, B 3, A 4)" in out.splitlines()


def test_case_show_says_not_assessed_and_dashes_for_unset_values(api, capsys):
    api.route(
        "GET",
        CASE_PATH,
        body={
            **CASE_BODY,
            "risk_score": None,
            "impact_c": None,
            "impact_b": 3,
            "impact_a": None,
        },
    )
    code, out, _ = run(capsys, "case", "show", CASE_ID, "--username", "alice")
    assert code == 0
    assert "  Risk score:       Not assessed (C -, B 3, A -)" in out.splitlines()
