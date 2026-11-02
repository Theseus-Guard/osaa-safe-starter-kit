"""Catalog subcommands of the container CLI: catalog list, show, add, version, correct, dispute, withdraw, supersede,
verify, and catalog recommendation list and add. The API is replaced by the fake behind urllib.request.urlopen.

The refusal paths are checked here: a role without the permission, a withdrawn or superseded entry, a stale version,
and a verification whose run was recorded but whose entry change was refused.
"""

import getpass
import io
import uuid

import pytest

from safe_kit import cli
from test_cli_cases import BASE_URL, PASSWORD, FakeAPI, run

USER_ID = "44444444-4444-4444-4444-444444444444"
ENTRY_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REPLACEMENT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RECOMMENDATION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
RUN_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
RESTRICTED = "https://restricted.example.invalid/report/SECRET-4410"
ENTRIES = "/api/v1/catalog/entries"
ENTRY_PATH = f"/api/v1/catalog/entries/{ENTRY_ID}"
RUNS_PATH = f"/api/v1/catalog/recommendations/{RECOMMENDATION_ID}/verification-runs"
RECOMMENDATIONS = "/api/v1/catalog/recommendations"
DENIED = {"detail": "forbidden"}
PASS_CHECK = {
    "run_id": RUN_ID,
    "subject": "deny-by-default gateway rule",
    "method": "replayed traffic",
    "result": "pass",
    "verified_at": "2026-11-02T09:00:00Z",
}


def version_body(
    number=1,
    status="active",
    title="Deny unscoped tool calls",
    body="Calls outside the approved scope are refused.",
    verification=None,
    restricted=False,
):
    view = {
        "version": number,
        "operation": "create" if number == 1 else "version",
        "status": status,
        "title": title,
        "body": body,
        "reason": None,
        "replacement_entry_id": None,
        "created_by": USER_ID,
        "created_at": "2026-11-01T09:00:00Z",
        "verification": verification,
        "verified": verification is not None and verification["result"] == "pass",
    }
    if restricted:
        view["restricted"] = {"reference": RESTRICTED, "digest": "ab" * 32}
    return view


def entry_body(*versions, status="active", with_history=False):
    current = versions[-1]
    entry = {
        "id": ENTRY_ID,
        "created_by": USER_ID,
        "created_at": "2026-11-01T09:00:00Z",
        "status": status,
        "version": current["version"],
        "current": current,
    }
    if with_history:
        entry["versions"] = list(versions)
    return entry


def recommendation_body(**changes):
    body = {
        "id": RECOMMENDATION_ID,
        "failure_or_system": "tool gateway accepts unscoped calls",
        "required_defence_outcome": "every call is checked against the approved scope",
        "minimum_control": "deny by default at the gateway",
        "control_alternative": None,
        "responsible": "platform owner",
        "due_at": "2026-12-01T09:00:00+00:00",
        "evidence_to_keep": "gateway policy diff",
        "created_by": USER_ID,
        "created_at": "2026-11-02T09:00:00Z",
    }
    body.update(changes)
    return body


def run_body(result="pass"):
    return {
        "id": RUN_ID,
        "recommendation_id": RECOMMENDATION_ID,
        "subject": "deny-by-default gateway rule",
        "scope": "staging gateway",
        "assumptions": "token scope is the only input",
        "evidence_path": "evidence/gateway/run-42",
        "failure_behavior": "unscoped calls are refused",
        "error_class": "none observed",
        "uncertainty": "one environment only",
        "method": "replayed traffic",
        "result": result,
        "verified_by": USER_ID,
        "verified_at": "2026-11-02T09:00:00Z",
    }


CONTENT = [
    "--title",
    "Deny unscoped tool calls",
    "--body",
    "Calls outside the approved scope are refused.",
]
VERIFY_ARGS = [
    "--recommendation",
    RECOMMENDATION_ID,
    "--expected-version",
    "1",
    "--result",
    "pass",
    "--subject",
    "deny-by-default gateway rule",
    "--scope",
    "staging gateway",
    "--assumptions",
    "token scope is the only input",
    "--evidence-path",
    "evidence/gateway/run-42",
    "--failure-behavior",
    "unscoped calls are refused",
    "--error-class",
    "none observed",
    "--uncertainty",
    "one environment only",
    "--method",
    "replayed traffic",
]


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SAFE_API_URL", BASE_URL)
    monkeypatch.delenv("SAFE_USERNAME", raising=False)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(PASSWORD + "\n"))

    def no_prompt(prompt=""):
        raise AssertionError("getpass must not be used when stdin is not a terminal")

    monkeypatch.setattr(getpass, "getpass", no_prompt)
    return FakeAPI(monkeypatch)


def calls(api: FakeAPI, method: str, path: str) -> list[dict]:
    return [
        call for call in api.calls if call["method"] == method and call["path"] == path
    ]


# --- catalog list and show --------------------------------------------------------------------------------------------


def test_list_prints_each_entry_with_its_status_and_version(api, capsys):
    page = {"items": [entry_body(version_body())], "next_cursor": None}
    api.route("GET", ENTRIES, body=page)
    code, out, err = run(capsys, "catalog", "list", "--username", "alice")
    assert code == 0, err
    assert "STATUS" in out and "VERSION" in out
    assert "active" in out and "Deny unscoped tool calls" in out
    assert "More entries" not in out


def test_list_says_so_when_there_are_no_entries_and_gives_the_next_cursor(api, capsys):
    api.route("GET", ENTRIES, body={"items": [], "next_cursor": "abc123"})
    code, out, _ = run(capsys, "catalog", "list", "--username", "alice")
    assert code == 0
    assert "No catalog entries yet." in out
    assert "--cursor abc123" in out


def test_list_json_prints_the_api_body_exactly(api, capsys):
    page = {"items": [entry_body(version_body())], "next_cursor": None}
    api.route("GET", ENTRIES, body=page)
    code, out, _ = run(capsys, "catalog", "list", "--json", "--username", "alice")
    assert code == 0
    assert out.strip().startswith('{"items":')


def test_list_refuses_a_role_without_catalog_read(api, capsys):
    api.route("GET", ENTRIES, status=403, body=DENIED)
    code, _, err = run(capsys, "catalog", "list", "--username", "alice")
    assert code == 1
    assert "does not hold catalog:read" in err


def test_show_prints_the_versions_and_the_verification_history(api, capsys):
    first = version_body(1)
    second = version_body(2, verification=PASS_CHECK)
    api.route("GET", ENTRY_PATH, body=entry_body(first, second, with_history=True))
    code, out, err = run(capsys, "catalog", "show", ENTRY_ID, "--username", "alice")
    assert code == 0, err
    assert f"Catalog entry {ENTRY_ID}" in out
    assert "Verified:" in out and "yes" in out
    assert "Version history:" in out
    assert "Verification history:" in out
    assert f"run {RUN_ID}" in out


def test_show_never_prints_a_restricted_reference_in_the_default_output(api, capsys):
    api.route("GET", ENTRY_PATH, body=entry_body(version_body(restricted=True)))
    code, out, _ = run(capsys, "catalog", "show", ENTRY_ID, "--username", "alice")
    assert code == 0
    assert RESTRICTED not in out
    assert "Restricted reference: held (printed only by --json)" in out


def test_show_json_prints_the_api_body_as_received(api, capsys):
    body = entry_body(version_body(restricted=True), with_history=True)
    api.route("GET", ENTRY_PATH, body=body)
    code, out, _ = run(
        capsys, "catalog", "show", ENTRY_ID, "--json", "--username", "alice"
    )
    assert code == 0
    assert RESTRICTED in out


def test_show_of_an_unknown_entry_gives_the_api_not_found_text(api, capsys):
    api.route("GET", ENTRY_PATH, status=404, body={"detail": "catalog entry not found"})
    code, _, err = run(capsys, "catalog", "show", ENTRY_ID, "--username", "alice")
    assert code == 1
    assert "catalog entry not found" in err


def test_show_refuses_an_id_that_is_not_a_uuid_before_any_login(api, capsys):
    code, _, err = run(capsys, "catalog", "show", "not-a-uuid", "--username", "alice")
    assert code == 2
    assert "ENTRY_ID must be a UUID" in err
    assert api.calls == []


# --- catalog add, version, correct, dispute, withdraw, supersede -------------------------------------------------------


def test_add_posts_the_entry_with_an_idempotency_key_and_prints_it(api, capsys):
    api.route("POST", ENTRIES, status=201, body=entry_body(version_body()))
    code, out, err = run(capsys, "catalog", "add", *CONTENT, "--username", "alice")
    assert code == 0, err
    assert "Catalog entry created." in out
    sent = calls(api, "POST", ENTRIES)[0]
    assert sent["json"] == {
        "title": "Deny unscoped tool calls",
        "body": "Calls outside the approved scope are refused.",
        "verification_run_id": None,
        "restricted_reference": None,
    }
    headers = {name.lower(): value for name, value in sent["headers"].items()}
    assert str(uuid.UUID(headers["idempotency-key"])) == headers["idempotency-key"]


def test_add_refused_for_a_role_without_catalog_write_saves_nothing(api, capsys):
    api.route("POST", ENTRIES, status=403, body=DENIED)
    code, _, err = run(capsys, "catalog", "add", *CONTENT, "--username", "alice")
    assert code == 1
    assert "your role does not hold the permission for this catalog action" in err


def test_a_restricted_reference_is_sent_but_not_echoed(api, capsys):
    api.route("POST", ENTRIES, status=201, body=entry_body(version_body()))
    code, out, _ = run(
        capsys,
        "catalog",
        "add",
        *CONTENT,
        "--restricted-reference",
        RESTRICTED,
        "--username",
        "alice",
    )
    assert code == 0
    assert calls(api, "POST", ENTRIES)[0]["json"]["restricted_reference"] == RESTRICTED
    assert RESTRICTED not in out


def test_version_sends_the_expected_version_and_the_content(api, capsys):
    path = f"{ENTRY_PATH}/versions"
    api.route("POST", path, status=201, body=entry_body(version_body(2)))
    code, out, err = run(
        capsys,
        "catalog",
        "version",
        ENTRY_ID,
        "--expected-version",
        "1",
        *CONTENT,
        "--username",
        "alice",
    )
    assert code == 0, err
    assert "Version saved." in out
    assert calls(api, "POST", path)[0]["json"]["expected_version"] == 1


def test_a_stale_version_is_refused_with_the_current_version_and_nothing_is_saved(
    api, capsys
):
    path = f"{ENTRY_PATH}/versions"
    api.route(
        "POST",
        path,
        status=409,
        body={"detail": {"status": "version_conflict", "current_version": 3}},
    )
    code, _, err = run(
        capsys,
        "catalog",
        "version",
        ENTRY_ID,
        "--expected-version",
        "1",
        *CONTENT,
        "--username",
        "alice",
    )
    assert code == 1
    assert "nothing was saved" in err
    assert "current version is 3" in err
    assert "--expected-version 3" in err


def test_a_withdrawn_entry_refuses_a_new_version_with_its_own_message(api, capsys):
    path = f"{ENTRY_PATH}/versions"
    api.route("POST", path, status=409, body={"detail": {"status": "entry_closed"}})
    code, _, err = run(
        capsys,
        "catalog",
        "version",
        ENTRY_ID,
        "--expected-version",
        "2",
        *CONTENT,
        "--username",
        "alice",
    )
    assert code == 1
    assert "withdrawn or superseded" in err
    assert "no further changes" in err


def test_correct_needs_a_reason_and_sends_it(api, capsys):
    path = f"{ENTRY_PATH}/corrections"
    api.route("POST", path, status=201, body=entry_body(version_body(2)))
    code, out, err = run(
        capsys,
        "catalog",
        "correct",
        ENTRY_ID,
        "--expected-version",
        "1",
        *CONTENT,
        "--reason",
        "wrong scope named",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert "Correction saved." in out
    assert calls(api, "POST", path)[0]["json"]["reason"] == "wrong scope named"


def test_correct_without_a_reason_is_a_usage_error_and_no_login(api, capsys):
    code, _, err = run(
        capsys,
        "catalog",
        "correct",
        ENTRY_ID,
        "--expected-version",
        "1",
        *CONTENT,
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []


def test_dispute_is_recorded_without_a_content_change_message(api, capsys):
    path = f"{ENTRY_PATH}/disputes"
    api.route(
        "POST",
        path,
        status=201,
        body=entry_body(version_body(2, status="disputed"), status="disputed"),
    )
    code, out, _ = run(
        capsys,
        "catalog",
        "dispute",
        ENTRY_ID,
        "--expected-version",
        "1",
        "--reason",
        "the scope is wider",
        "--username",
        "alice",
    )
    assert code == 0
    assert "Dispute recorded. The entry content is unchanged." in out
    assert "disputed" in out


def test_withdraw_on_an_already_withdrawn_entry_is_refused(api, capsys):
    path = f"{ENTRY_PATH}/withdrawals"
    api.route("POST", path, status=409, body={"detail": {"status": "entry_closed"}})
    code, _, err = run(
        capsys,
        "catalog",
        "withdraw",
        ENTRY_ID,
        "--expected-version",
        "2",
        "--reason",
        "retired",
        "--username",
        "alice",
    )
    assert code == 1
    assert "withdrawn or superseded" in err


def test_withdraw_refused_for_a_role_without_catalog_write(api, capsys):
    path = f"{ENTRY_PATH}/withdrawals"
    api.route("POST", path, status=403, body=DENIED)
    code, _, err = run(
        capsys,
        "catalog",
        "withdraw",
        ENTRY_ID,
        "--expected-version",
        "1",
        "--reason",
        "retired",
        "--username",
        "alice",
    )
    assert code == 1
    assert "your role does not hold the permission" in err


def test_supersede_sends_the_replacement_and_says_the_history_is_kept(api, capsys):
    path = f"{ENTRY_PATH}/supersessions"
    api.route(
        "POST", path, status=201, body=entry_body(version_body(2, status="superseded"))
    )
    code, out, err = run(
        capsys,
        "catalog",
        "supersede",
        ENTRY_ID,
        "--expected-version",
        "1",
        "--replacement",
        REPLACEMENT_ID,
        "--username",
        "alice",
    )
    assert code == 0, err
    assert "Entry superseded. Its history is kept." in out
    assert calls(api, "POST", path)[0]["json"]["replacement_entry_id"] == REPLACEMENT_ID


def test_supersede_with_a_replacement_that_is_not_open_is_refused_with_the_api_text(
    api, capsys
):
    path = f"{ENTRY_PATH}/supersessions"
    api.route(
        "POST",
        path,
        status=422,
        body={"detail": "replacement must be another open catalog entry"},
    )
    code, _, err = run(
        capsys,
        "catalog",
        "supersede",
        ENTRY_ID,
        "--expected-version",
        "1",
        "--replacement",
        REPLACEMENT_ID,
        "--username",
        "alice",
    )
    assert code == 1
    assert "replacement must be another open catalog entry" in err


def test_expected_version_below_one_is_a_usage_error(api, capsys):
    code, _, err = run(
        capsys,
        "catalog",
        "dispute",
        ENTRY_ID,
        "--expected-version",
        "0",
        "--reason",
        "x",
        "--username",
        "alice",
    )
    assert code == 2
    assert "--expected-version must be 1 or more" in err
    assert api.calls == []


def test_a_title_of_only_spaces_is_refused_before_any_login(api, capsys):
    code, _, err = run(
        capsys,
        "catalog",
        "add",
        "--title",
        "   ",
        "--body",
        "text",
        "--username",
        "alice",
    )
    assert code == 2
    assert "--title is required" in err
    assert api.calls == []


# --- catalog verify ---------------------------------------------------------------------------------------------------


def test_verify_records_the_run_then_attaches_it_to_the_entry_and_reports_verified(
    api, capsys
):
    api.route("GET", ENTRY_PATH, body=entry_body(version_body(1)))
    api.route("POST", RUNS_PATH, status=201, body=run_body("pass"))
    api.route(
        "POST",
        f"{ENTRY_PATH}/versions",
        status=201,
        body=entry_body(version_body(2, verification=PASS_CHECK)),
    )
    code, out, err = run(
        capsys, "catalog", "verify", ENTRY_ID, *VERIFY_ARGS, "--username", "alice"
    )
    assert code == 0, err
    assert f"Verification run {RUN_ID} recorded: pass." in out
    assert "The entry is verified." in out
    version_call = calls(api, "POST", f"{ENTRY_PATH}/versions")[0]
    # The new version repeats the current content and names the run. It is not a copy of an old run.
    assert version_call["json"] == {
        "expected_version": 1,
        "title": "Deny unscoped tool calls",
        "body": "Calls outside the approved scope are refused.",
        "verification_run_id": RUN_ID,
    }


def test_a_failing_run_is_attached_but_the_entry_is_not_verified(api, capsys):
    failed = {**PASS_CHECK, "result": "fail"}
    api.route("GET", ENTRY_PATH, body=entry_body(version_body(1)))
    api.route("POST", RUNS_PATH, status=201, body=run_body("fail"))
    api.route(
        "POST",
        f"{ENTRY_PATH}/versions",
        status=201,
        body=entry_body(version_body(2, verification=failed)),
    )
    args = [*VERIFY_ARGS]
    args[args.index("pass")] = "fail"
    code, out, err = run(
        capsys, "catalog", "verify", ENTRY_ID, *args, "--username", "alice"
    )
    assert code == 0, err
    assert "The entry is not verified: only a passing run verifies it." in out


def test_verify_refuses_a_withdrawn_entry_before_recording_any_run(api, capsys):
    api.route(
        "GET",
        ENTRY_PATH,
        body=entry_body(version_body(2, status="withdrawn"), status="withdrawn"),
    )
    code, _, err = run(
        capsys, "catalog", "verify", ENTRY_ID, *VERIFY_ARGS, "--username", "alice"
    )
    assert code == 1
    assert "this entry is withdrawn, so it cannot take a verification" in err
    assert calls(api, "POST", RUNS_PATH) == []


def test_verify_refuses_a_superseded_entry_before_recording_any_run(api, capsys):
    api.route(
        "GET",
        ENTRY_PATH,
        body=entry_body(version_body(2, status="superseded"), status="superseded"),
    )
    code, _, err = run(
        capsys, "catalog", "verify", ENTRY_ID, *VERIFY_ARGS, "--username", "alice"
    )
    assert code == 1
    assert "this entry is superseded" in err
    assert calls(api, "POST", RUNS_PATH) == []


def test_verify_says_the_run_was_recorded_when_the_entry_change_is_refused(api, capsys):
    api.route("GET", ENTRY_PATH, body=entry_body(version_body(1)))
    api.route("POST", RUNS_PATH, status=201, body=run_body("pass"))
    api.route(
        "POST",
        f"{ENTRY_PATH}/versions",
        status=409,
        body={"detail": {"status": "version_conflict", "current_version": 2}},
    )
    code, _, err = run(
        capsys, "catalog", "verify", ENTRY_ID, *VERIFY_ARGS, "--username", "alice"
    )
    assert code == 1
    assert (
        f"verification run {RUN_ID} was recorded, but the entry was not updated" in err
    )
    assert "current version is 2" in err


def test_verify_refused_for_a_role_without_catalog_write_records_nothing_more(
    api, capsys
):
    api.route("GET", ENTRY_PATH, body=entry_body(version_body(1)))
    api.route("POST", RUNS_PATH, status=403, body=DENIED)
    code, _, err = run(
        capsys, "catalog", "verify", ENTRY_ID, *VERIFY_ARGS, "--username", "alice"
    )
    assert code == 1
    assert "your role does not hold the permission" in err
    assert calls(api, "POST", f"{ENTRY_PATH}/versions") == []


def test_verify_uses_stable_keys_per_request_so_a_retry_gets_the_first_answers(
    api, capsys, monkeypatch
):
    api.route("GET", ENTRY_PATH, body=entry_body(version_body(1)))
    api.route("POST", RUNS_PATH, status=201, body=run_body("pass"))
    api.route(
        "POST",
        f"{ENTRY_PATH}/versions",
        status=201,
        body=entry_body(version_body(2, verification=PASS_CHECK)),
    )
    run(
        capsys,
        "catalog",
        "verify",
        ENTRY_ID,
        *VERIFY_ARGS,
        "--idempotency-key",
        "retry-9",
        "--username",
        "alice",
    )

    def keys(path):
        return [
            {name.lower(): value for name, value in call["headers"].items()}[
                "idempotency-key"
            ]
            for call in calls(api, "POST", path)
        ]

    first_run_key = keys(RUNS_PATH)[0]
    first_version_key = keys(f"{ENTRY_PATH}/versions")[0]
    assert first_run_key != first_version_key
    assert "retry-9" not in (first_run_key, first_version_key)
    # The second run reads its password from stdin again.
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(PASSWORD + "\n"))
    run(
        capsys,
        "catalog",
        "verify",
        ENTRY_ID,
        *VERIFY_ARGS,
        "--idempotency-key",
        "retry-9",
        "--username",
        "alice",
    )
    assert keys(RUNS_PATH) == [first_run_key, first_run_key]
    assert keys(f"{ENTRY_PATH}/versions") == [first_version_key, first_version_key]


def test_verify_rejects_a_result_outside_the_three_values_before_any_login(api, capsys):
    args = [*VERIFY_ARGS]
    args[args.index("pass")] = "maybe"
    code, _, _ = run(
        capsys, "catalog", "verify", ENTRY_ID, *args, "--username", "alice"
    )
    assert code == 2
    assert api.calls == []


# --- catalog recommendation list and add -----------------------------------------------------------------------------


def test_recommendation_list_prints_the_suggestions_and_the_next_cursor(api, capsys):
    api.route(
        "GET",
        RECOMMENDATIONS,
        body={"items": [recommendation_body()], "next_cursor": "next-1"},
    )
    code, out, _ = run(
        capsys, "catalog", "recommendation", "list", "--username", "alice"
    )
    assert code == 0
    assert "platform owner" in out
    assert "--cursor next-1" in out


def test_recommendation_add_sends_the_fields_and_says_it_is_a_suggestion(api, capsys):
    api.route("POST", RECOMMENDATIONS, status=201, body=recommendation_body())
    code, out, err = run(
        capsys,
        "catalog",
        "recommendation",
        "add",
        "--failure-or-system",
        "tool gateway accepts unscoped calls",
        "--required-defence-outcome",
        "every call is checked against the approved scope",
        "--minimum-control",
        "deny by default at the gateway",
        "--responsible",
        "platform owner",
        "--evidence-to-keep",
        "gateway policy diff",
        "--due-at",
        "2026-12-01T09:00:00Z",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert "a suggestion until a passing verification run is recorded" in out
    sent = calls(api, "POST", RECOMMENDATIONS)[0]["json"]
    assert sent["due_at"].startswith("2026-12-01T09:00:00")
    assert sent["control_alternative"] is None


def test_recommendation_add_refused_without_a_time_zone(api, capsys):
    code, _, err = run(
        capsys,
        "catalog",
        "recommendation",
        "add",
        "--failure-or-system",
        "x",
        "--required-defence-outcome",
        "x",
        "--minimum-control",
        "x",
        "--responsible",
        "x",
        "--evidence-to-keep",
        "x",
        "--due-at",
        "2026-12-01T09:00:00",
        "--username",
        "alice",
    )
    assert code == 2
    assert "must include a timezone" in err
    assert api.calls == []


def test_recommendation_add_refused_for_a_read_only_role(api, capsys):
    api.route("POST", RECOMMENDATIONS, status=403, body=DENIED)
    code, _, err = run(
        capsys,
        "catalog",
        "recommendation",
        "add",
        "--failure-or-system",
        "x",
        "--required-defence-outcome",
        "x",
        "--minimum-control",
        "x",
        "--responsible",
        "x",
        "--evidence-to-keep",
        "x",
        "--due-at",
        "2026-12-01T09:00:00Z",
        "--username",
        "alice",
    )
    assert code == 1
    assert "your role does not hold the permission" in err
