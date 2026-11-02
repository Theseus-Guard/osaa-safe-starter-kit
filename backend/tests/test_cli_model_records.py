"""The model record commands of the container CLI (case model record, list, revoke).

The login session and the HTTP call are replaced, so no server is needed. Each test checks the request the command
would send, or that a bad argument is refused before any request.
"""

import json

import pytest

from safe_kit import cli

BASE_URL = "http://api.test:8000"
CASE_ID = "11111111-2222-4333-8444-555555555555"
RECORD_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ROUND_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RECORD_BODY = {
    "record_id": RECORD_ID,
    "case_id": CASE_ID,
    "version": 1,
    "configuration": {
        "model_name": "model-a",
        "model_version": "2026-10",
        "runtime": "runtime/1.4",
        "provider_alias": "provider-main",
        "reasoning": {"mode": "enabled", "effort": "high", "budget_tokens": 8000},
    },
    "evidence_item_id": None,
    "review_round_id": None,
    "recorded_at": "2026-11-02T09:00:00Z",
    "written_by": "11111111-1111-4111-8111-111111111111",
    "written_at": "2026-11-02T09:00:00Z",
    "revoked": False,
    "revoked_at": None,
    "revocation_reason": None,
    "usable_as_current": True,
}
RECORD_ARGS = [
    "--model-name",
    "model-a",
    "--model-version",
    "2026-10",
    "--runtime",
    "runtime/1.4",
    "--provider-alias",
    "provider-main",
    "--reasoning-mode",
    "enabled",
    "--reasoning-effort",
    "high",
    "--reasoning-budget-tokens",
    "8000",
]


class FakeAPI:
    def __init__(self, monkeypatch, response: bytes = b"{}"):
        self.calls: list[dict] = []
        self.response = response
        monkeypatch.setenv("SAFE_API_URL", BASE_URL)
        monkeypatch.setattr(
            cli, "_with_session", lambda base, args, action: action(base, "token")
        )
        monkeypatch.setattr(cli, "_call", self._call)

    def _call(self, base_url, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})
        return self.response


@pytest.fixture
def api(monkeypatch):
    return FakeAPI(monkeypatch)


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(["--no-banner", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_record_sends_the_configuration_with_the_idempotency_key(api, capsys):
    api.response = json.dumps(RECORD_BODY).encode()

    code, out, _ = run(
        capsys,
        "case",
        "model",
        "record",
        CASE_ID,
        *RECORD_ARGS,
        "--idempotency-key",
        "run-1",
    )

    assert code == 0
    call = api.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == f"/api/v1/cases/{CASE_ID}/model-records"
    assert call["payload"] == {
        "model_name": "model-a",
        "model_version": "2026-10",
        "runtime": "runtime/1.4",
        "provider_alias": "provider-main",
        "reasoning": {"mode": "enabled", "effort": "high", "budget_tokens": 8000},
        "evidence_item_id": None,
        "review_round_id": None,
    }
    assert call["extra_headers"] == {"Idempotency-Key": "run-1"}
    assert "Model record saved." in out
    assert RECORD_ID in out
    assert "Status:           current" in out


def test_record_json_prints_the_api_body(api, capsys):
    api.response = json.dumps(RECORD_BODY).encode()

    code, out, _ = run(
        capsys, "case", "model", "record", CASE_ID, *RECORD_ARGS, "--json"
    )

    assert code == 0
    assert json.loads(out) == RECORD_BODY


def test_record_links_to_a_review_round(api, capsys):
    api.response = json.dumps(RECORD_BODY).encode()

    run(
        capsys,
        "case",
        "model",
        "record",
        CASE_ID,
        *RECORD_ARGS,
        "--review-round",
        ROUND_ID,
    )

    assert api.calls[0]["payload"]["review_round_id"] == ROUND_ID


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--model-name", "model a"),
        ("--runtime", "please answer in French"),
        ("--provider-alias", "x" * 201),
    ],
)
def test_an_identifier_with_spaces_or_too_long_is_refused_before_any_request(
    api, capsys, option, value
):
    args = list(RECORD_ARGS)
    args[args.index(option) + 1] = value

    code, _, err = run(capsys, "case", "model", "record", CASE_ID, *args)

    assert code != 0
    assert "may use only letters, digits and . _ : / @ + -" in err
    assert api.calls == []


@pytest.mark.parametrize(
    "reasoning_args",
    [
        ["--reasoning-mode", "disabled", "--reasoning-effort", "low"],
        ["--reasoning-mode", "unknown", "--reasoning-budget-tokens", "100"],
    ],
)
def test_effort_and_budget_need_reasoning_enabled(api, capsys, reasoning_args):
    args = [
        "--model-name",
        "model-a",
        "--model-version",
        "1",
        "--runtime",
        "r",
        "--provider-alias",
        "p",
    ]
    args += reasoning_args

    code, _, err = run(capsys, "case", "model", "record", CASE_ID, *args)

    assert code != 0
    assert "need --reasoning-mode enabled" in err
    assert api.calls == []


def test_a_budget_outside_the_limit_is_refused_before_any_request(api, capsys):
    args = [
        "--model-name",
        "a",
        "--model-version",
        "1",
        "--runtime",
        "r",
        "--provider-alias",
        "p",
    ]
    args += ["--reasoning-mode", "enabled", "--reasoning-budget-tokens", "0"]

    code, _, err = run(capsys, "case", "model", "record", CASE_ID, *args)

    assert code != 0
    assert "--reasoning-budget-tokens must be between 1 and" in err
    assert api.calls == []


def test_evidence_item_and_review_round_cannot_both_be_given(api, capsys):
    links = ["--evidence-item", RECORD_ID, "--review-round", ROUND_ID]

    code, _, err = run(capsys, "case", "model", "record", CASE_ID, *RECORD_ARGS, *links)

    assert code != 0
    assert "cannot be used together" in err
    assert api.calls == []


def test_list_sends_current_only_only_when_asked(api, capsys):
    api.response = json.dumps([RECORD_BODY]).encode()

    run(capsys, "case", "model", "list", CASE_ID)
    run(capsys, "case", "model", "list", CASE_ID, "--current-only")

    assert api.calls[0]["query"] is None
    assert api.calls[1]["query"] == {"current_only": "true"}
    assert all(call["method"] == "GET" for call in api.calls)


def test_list_json_prints_the_api_body(api, capsys):
    api.response = json.dumps([RECORD_BODY]).encode()

    code, out, _ = run(capsys, "case", "model", "list", CASE_ID, "--json")

    assert code == 0
    assert json.loads(out) == [RECORD_BODY]


def test_list_without_records_says_so(api, capsys):
    api.response = b"[]"

    code, out, _ = run(capsys, "case", "model", "list", CASE_ID)

    assert code == 0
    assert "No model records." in out


def test_revoke_posts_the_trimmed_reason_to_the_record_path(api, capsys):
    revoked = {
        **RECORD_BODY,
        "version": 2,
        "revoked": True,
        "usable_as_current": False,
        "revocation_reason": "x",
    }
    api.response = json.dumps(revoked).encode()

    code, out, _ = run(
        capsys,
        "case",
        "model",
        "revoke",
        CASE_ID,
        RECORD_ID,
        "--reason",
        "  wrong alias  ",
        "--idempotency-key",
        "k2",
    )

    assert code == 0
    call = api.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == f"/api/v1/cases/{CASE_ID}/model-records/{RECORD_ID}/revoke"
    assert call["payload"] == {"reason": "wrong alias"}
    assert call["extra_headers"] == {"Idempotency-Key": "k2"}
    assert "Model record revoked." in out
    assert "revoked at" in out


def test_a_blank_revocation_reason_is_refused_before_any_request(api, capsys):
    code, _, err = run(
        capsys, "case", "model", "revoke", CASE_ID, RECORD_ID, "--reason", "   "
    )

    assert code != 0
    assert "must not be blank" in err
    assert api.calls == []


def test_a_record_id_that_is_not_a_uuid_is_refused_before_any_request(api, capsys):
    code, _, err = run(
        capsys, "case", "model", "revoke", CASE_ID, "not-a-uuid", "--reason", "x"
    )

    assert code != 0
    assert "RECORD_ID must be a UUID" in err
    assert api.calls == []
