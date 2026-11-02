"""F2 fixture T15 (workflow parity): the same role and the same case data give the same answer through the API and
through the container CLI for the disclosure list, approval, export, the review round and its layers, and a case the
caller may not see answers the same as a missing one.

The CLI runs unchanged. Its HTTP calls go through tests/cli_bridge.py into the FastAPI test client. The login is fixed
(the bridge answers it), and the caller is set with the same dependency override that request_as uses, so each run
is made as the named role. The data comes from the disclosure world of tests/test_fixture_p6_disclosure.py.

These need PostgreSQL: they run with SAFE_TEST_DATABASE=1 and the SAFE_DB_* settings (scripts/test-backend.sh --db).
"""

# Fixtures and helpers come from the sibling test modules and are requested by name, which pyflakes reports as a
# redefinition. That is the intended use here.
# ruff: noqa: F401, F811
import json
import uuid

from fastapi.testclient import TestClient

from cli_bridge import install, run
from error_body import without_request_id
from safe_kit import api, auth, users
from test_evidence import db_user, request_as
from test_evidence_lifecycle import provider, requires_database  # provider: fixture
from test_fixture_p6_disclosure import (  # dworld: fixture
    approval_count,
    approve,
    body,
    case_url,
    create,
    dworld,
    first_version,
    grant,
)
from test_fixture_t06a_redaction import make_derived, world  # world: fixture
from test_fixture_t15_session_parity import PASSWORD

# The login is answered by the bridge with these fields, so the CLI reaches the data calls. The caller is the override.
FIXED_LOGIN = {
    "access_token": "access-workflow-parity",
    "refresh_token": "refresh-workflow-parity",
    "token_type": "bearer",
    "expires_in": 900,
    "password_change_required": False,
}
ZERO_DIGEST = "0" * 64


def act_as(user_id: str) -> None:
    """Make the next requests, through the API and through the CLI, run as this user."""
    user = users.find_user_by_id(user_id)
    assert user is not None
    api.app.dependency_overrides[auth.current_user] = lambda: user


def cli_as(monkeypatch, capsys, who_id: str, *argv: str) -> tuple[int, str, str]:
    """One CLI run as the given user. The argv is the command group below "case", such as "disclosure" or "review"."""
    act_as(who_id)
    install(monkeypatch, TestClient(api.app), issued=FIXED_LOGIN, logout_status=204)
    return run(capsys, monkeypatch, PASSWORD, "case", *argv, "--username", "parity")


def api_as(who_id: str, method: str, url: str, **kwargs):
    return request_as(who_id, method, url, **kwargs)


def version_url(world_data, version_id: str, suffix: str = "") -> str:
    return case_url(world_data, f"/{version_id}{suffix}")


# --- the package list and the export: the same document through both clients -----------------------------------------


@requires_database
def test_the_version_list_is_the_same_through_the_api_and_the_cli(dworld, monkeypatch, capsys):
    first_version(dworld)
    approver = dworld["people"]["approver"]

    api_list = api_as(approver, "get", case_url(dworld))
    code, out, err = cli_as(
        monkeypatch, capsys, approver, "disclosure", "show", dworld["case_id"], "--json"
    )

    assert api_list.status_code == 200, api_list.text
    assert code == 0 and err == ""
    assert json.loads(out) == api_list.json()


@requires_database
def test_an_approved_export_is_the_same_document_through_the_api_and_the_cli(dworld, monkeypatch, capsys):
    view, _ = first_version(dworld)
    approver = dworld["people"]["approver"]
    assert approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], view["digest"]).status_code == 201

    api_export = api_as(approver, "get", version_url(dworld, view["id"], "/export"))
    code, out, err = cli_as(
        monkeypatch, capsys, approver, "disclosure", "export", dworld["case_id"], view["id"]
    )

    assert api_export.status_code == 200, api_export.text
    assert code == 0 and err == ""
    assert json.loads(out) == api_export.json()


@requires_database
def test_an_unapproved_export_is_refused_by_both_clients_and_no_document_is_printed(dworld, monkeypatch, capsys):
    view, _ = first_version(dworld)
    triager = dworld["people"]["triager"]

    api_export = api_as(triager, "get", version_url(dworld, view["id"], "/export"))
    code, out, err = cli_as(monkeypatch, capsys, triager, "disclosure", "export", dworld["case_id"], view["id"])

    assert api_export.status_code == 409
    assert api_export.json()["detail"] == {"status": "version_not_approved"}
    assert code == 1
    assert out == ""
    assert err.startswith("error: ")


@requires_database
def test_a_wrong_digest_is_refused_by_both_clients_and_no_approval_is_stored(dworld, monkeypatch, capsys):
    view, _ = first_version(dworld)
    approver = dworld["people"]["approver"]

    api_answer = approve(dworld, "approver", view["id"], view["recipient"], view["purpose"], ZERO_DIGEST)
    code, out, err = cli_as(
        monkeypatch,
        capsys,
        approver,
        "disclosure",
        "approve",
        dworld["case_id"],
        view["id"],
        "--recipient",
        view["recipient"],
        "--purpose",
        view["purpose"],
        "--digest",
        ZERO_DIGEST,
    )

    assert api_answer.status_code >= 400
    assert code == 1
    assert out == ""
    assert err.startswith("error: ")
    assert approval_count(view["id"]) == 0


@requires_database
def test_an_approval_through_the_cli_reads_back_like_one_through_the_api(dworld, monkeypatch, capsys):
    api_view, _ = first_version(dworld)
    cli_view, _ = first_version(dworld, purpose="Second rapid alert on the same gateway")
    approver = dworld["people"]["approver"]
    api_answer = approve(dworld, "approver", api_view["id"], api_view["recipient"], api_view["purpose"], api_view["digest"])
    assert api_answer.status_code == 201, api_answer.text

    code, out, err = cli_as(
        monkeypatch,
        capsys,
        approver,
        "disclosure",
        "approve",
        dworld["case_id"],
        cli_view["id"],
        "--recipient",
        cli_view["recipient"],
        "--purpose",
        cli_view["purpose"],
        "--digest",
        cli_view["digest"],
        "--json",
    )

    assert code == 0 and err == ""
    cli_answer = json.loads(out)
    assert set(cli_answer) == set(api_answer.json())
    assert approval_count(cli_view["id"]) == 1
    assert approval_count(api_view["id"]) == 1


@requires_database
def test_a_case_the_caller_cannot_see_gives_the_same_answer_as_a_missing_case_through_both_clients(
    dworld, monkeypatch, capsys
):
    view, _ = first_version(dworld)
    # The stranger is a reviewer that is not assigned to this case, and is given disclosures:read for the test only.
    stranger = db_user("reviewer")
    grant(monkeypatch, "reviewer", "disclosures:read")
    missing_case = str(uuid.uuid4())

    api_hidden = api_as(stranger, "get", version_url(dworld, view["id"], "/export"))
    api_missing = api_as(stranger, "get", f"/api/v1/cases/{missing_case}/disclosures/{view['id']}/export")
    assert api_hidden.status_code == api_missing.status_code == 404
    assert without_request_id(api_hidden) == without_request_id(api_missing)

    cli_hidden = cli_as(monkeypatch, capsys, stranger, "disclosure", "export", dworld["case_id"], view["id"])
    cli_missing = cli_as(monkeypatch, capsys, stranger, "disclosure", "export", missing_case, view["id"])
    assert cli_hidden == cli_missing
    assert cli_hidden[0] == 1 and cli_hidden[1] == ""
    assert "not found" in cli_hidden[2]


# --- the review round and its layers ----------------------------------------------------------------------------------


def open_round(world_data) -> dict:
    response = api_as(
        world_data["people"]["reviewer"],
        "post",
        "/api/v1/reviews",
        json={"case_id": world_data["case_id"]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return response.json()


@requires_database
def test_the_review_round_is_the_same_through_the_api_and_the_cli(dworld, monkeypatch, capsys):
    reviewer = dworld["people"]["reviewer"]
    open_round(dworld)

    api_rounds = api_as(reviewer, "get", f"/api/v1/reviews?case_id={dworld['case_id']}")
    code, out, err = cli_as(monkeypatch, capsys, reviewer, "review", "list", dworld["case_id"], "--json")

    assert api_rounds.status_code == 200, api_rounds.text
    assert code == 0 and err == ""
    assert json.loads(out) == api_rounds.json()


@requires_database
def test_a_stale_layer_revision_gets_the_same_current_revision_from_the_api_and_the_cli(dworld, monkeypatch, capsys):
    reviewer = dworld["people"]["reviewer"]
    round_view = open_round(dworld)
    current = round_view["revision"]
    stale = current + 5
    url = f"/api/v1/reviews/{round_view['id']}/layers/model"

    api_answer = api_as(
        reviewer,
        "patch",
        url,
        json={"outcome": "fail", "reason": "Stale attempt", "evidence_ids": []},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": f'"{stale}"'},
    )
    code, out, err = cli_as(
        monkeypatch,
        capsys,
        reviewer,
        "review",
        "record",
        dworld["case_id"],
        "--layer",
        "model",
        "--outcome",
        "fail",
        "--reason",
        "Stale attempt",
        "--revision",
        str(stale),
    )

    assert api_answer.status_code == 412
    assert api_answer.json()["detail"] == {"status": "precondition_failed", "current_revision": current}
    assert code == 1 and out == ""
    assert f"The current revision is {current}" in err
    assert "nothing was saved" in err


@requires_database
def test_a_layer_set_through_the_cli_is_read_back_the_same_way_through_the_api(dworld, monkeypatch, capsys):
    reviewer = dworld["people"]["reviewer"]
    round_view = open_round(dworld)

    code, out, err = cli_as(
        monkeypatch,
        capsys,
        reviewer,
        "review",
        "record",
        dworld["case_id"],
        "--layer",
        "model",
        "--outcome",
        "fail",
        "--reason",
        "Model output does not match the evidence",
        "--revision",
        str(round_view["revision"]),
        "--json",
    )
    assert code == 0 and err == "", err

    api_rounds = api_as(reviewer, "get", f"/api/v1/reviews?case_id={dworld['case_id']}")
    assert api_rounds.status_code == 200
    stored = api_rounds.json()[0]
    model_layer = next(layer for layer in stored["layers"] if layer["layer"] == "model")
    assert model_layer["outcome"] == "fail"
    assert stored["revision"] == round_view["revision"] + 1
    assert json.loads(out)["revision"] == stored["revision"]


@requires_database
def test_a_pass_without_evidence_is_refused_by_both_clients_and_nothing_is_stored(dworld, monkeypatch, capsys):
    reviewer = dworld["people"]["reviewer"]
    round_view = open_round(dworld)

    api_answer = api_as(
        reviewer,
        "patch",
        f"/api/v1/reviews/{round_view['id']}/layers/model",
        json={"outcome": "pass", "reason": None, "evidence_ids": []},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": f'"{round_view["revision"]}"'},
    )
    code, out, err = cli_as(
        monkeypatch,
        capsys,
        reviewer,
        "review",
        "record",
        dworld["case_id"],
        "--layer",
        "model",
        "--outcome",
        "pass",
        "--revision",
        str(round_view["revision"]),
    )

    assert api_answer.status_code == 422
    assert "a pass needs at least one evidence item of this case" in api_answer.text
    assert code == 2 and out == ""
    assert "a pass needs at least one evidence item of this case" in err
    # Neither refused write changed the round.
    unchanged = api_as(reviewer, "get", f"/api/v1/reviews?case_id={dworld['case_id']}").json()[0]
    assert unchanged["revision"] == round_view["revision"]
