"""F2 fixture T15-A through the container CLI: the evidence states and the empty lists must look different from a clean
result, and a hidden item must look the same as a missing one.

- An empty case lists no evidence, and its text says so. It never says a case passed or is clean.
- An unavailable item (failed, or still pending) is listed with its own status, the same status the API gives.
- A hidden item and a missing item give the same CLI answer, and that answer is not the "unavailable" one.
- An account with no cases gets "No cases to show." from the CLI, which is the same as the empty API list.

The CLI runs unchanged through tests/cli_bridge.py. These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the
SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from cli_bridge import install, run
from safe_kit import api
from test_evidence import db_user, evidence_world, request_as  # evidence_world: fixture
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_t15_session_parity import PASSWORD
from test_fixture_t15_workflow_parity_db import FIXED_LOGIN, act_as
from test_fixture_t15a_evidence_states import empty_case, set_status

# Words that would read as a clean result. None may appear in an answer about an empty or unavailable state.
CLEAN_RESULT_WORDS = re.compile(
    r"\b(pass|passed|clean|compliant|compliance|verified|no findings)\b", re.IGNORECASE
)


def cli_as_root(monkeypatch, capsys, who_id: str, *argv: str) -> tuple[int, str, str]:
    """One CLI run as the given user. The argv starts at the command group, such as "evidence" or "case"."""
    act_as(who_id)
    install(monkeypatch, TestClient(api.app), issued=FIXED_LOGIN, logout_status=204)
    return run(capsys, monkeypatch, PASSWORD, *argv, "--username", "parity")


@requires_database
def test_an_empty_case_is_listed_as_no_evidence_and_never_as_a_result(
    evidence_world, monkeypatch, capsys
):
    people = evidence_world["people"]
    case_id = empty_case(people)
    api_list = request_as(
        people["investigator"], "get", f"/api/v1/cases/{case_id}/evidence"
    )
    assert api_list.status_code == 200

    as_json = cli_as_root(
        monkeypatch,
        capsys,
        people["investigator"],
        "evidence",
        "list",
        case_id,
        "--json",
    )
    as_text = cli_as_root(
        monkeypatch, capsys, people["investigator"], "evidence", "list", case_id
    )

    assert as_json[0] == 0 and as_json[2] == ""
    # The CLI asks for its own default page size, so the page size is compared by its content, not the whole body.
    cli_body = json.loads(as_json[1])
    assert api_list.json()["items"] == cli_body["items"] == []
    assert api_list.json()["total"] == cli_body["total"] == 0
    assert as_text[0] == 0 and as_text[2] == ""
    assert "No evidence for this case" in as_text[1]
    assert CLEAN_RESULT_WORDS.search(as_text[1]) is None


@requires_database
@pytest.mark.parametrize("status", ["failed", "pending"])
def test_an_unavailable_item_is_listed_with_its_own_status_in_both_clients(
    evidence_world, monkeypatch, capsys, status
):
    people = evidence_world["people"]
    case_id, evidence_id = evidence_world["case_id"], evidence_world["evidence_id"]
    set_status(evidence_id, status)

    api_list = request_as(
        people["investigator"], "get", f"/api/v1/cases/{case_id}/evidence"
    )
    api_item = next(
        item for item in api_list.json()["items"] if item["id"] == evidence_id
    )
    code, out, err = cli_as_root(
        monkeypatch,
        capsys,
        people["investigator"],
        "evidence",
        "list",
        case_id,
        "--json",
    )

    assert code == 0 and err == ""
    cli_item = next(
        item for item in json.loads(out)["items"] if item["id"] == evidence_id
    )
    assert cli_item["status"] == api_item["status"] == status


@requires_database
def test_a_hidden_item_and_a_missing_item_give_the_same_cli_answer_that_is_not_the_unavailable_one(
    evidence_world, monkeypatch, capsys
):
    other = evidence_world["people"]["other_investigator"]
    evidence_id = evidence_world["evidence_id"]
    set_status(evidence_id, "failed")

    hidden = cli_as_root(monkeypatch, capsys, other, "evidence", "show", evidence_id)
    missing = cli_as_root(
        monkeypatch, capsys, other, "evidence", "show", str(uuid.uuid4())
    )

    assert hidden == missing
    assert hidden[0] == 1 and hidden[1] == ""
    assert "evidence not found" in hidden[2]
    assert "not available" not in hidden[2]


@requires_database
def test_a_hidden_case_and_a_missing_case_give_the_same_cli_answer_for_its_evidence(
    evidence_world, monkeypatch, capsys
):
    other = evidence_world["people"]["other_investigator"]
    case_id = evidence_world["case_id"]

    hidden = cli_as_root(monkeypatch, capsys, other, "evidence", "list", case_id)
    missing = cli_as_root(
        monkeypatch, capsys, other, "evidence", "list", str(uuid.uuid4())
    )

    assert hidden == missing
    assert hidden[0] == 1 and hidden[1] == ""
    assert "case not found" in hidden[2]


@requires_database
def test_an_account_with_no_cases_gets_a_neutral_empty_list_in_both_clients(
    monkeypatch, capsys
):
    reporter = db_user("reporter")
    api_list = request_as(reporter, "get", "/api/v1/cases?limit=20")
    assert api_list.status_code == 200
    assert api_list.json()["items"] == []

    code, out, err = cli_as_root(monkeypatch, capsys, reporter, "case", "list")

    assert code == 0 and err == ""
    assert "No cases to show." in out
    assert CLEAN_RESULT_WORDS.search(out) is None
