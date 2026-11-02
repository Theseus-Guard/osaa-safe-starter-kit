"""The external notice commands of the container CLI: case notice record, list, receipt, follow-up and cancel.

The API is replaced by the fake of tests/test_cli_cases.py, so these tests need no server and no database.
"""

# The fixture api and the helpers come from the sibling test modules; pyflakes reports the reuse as a redefinition.
# ruff: noqa: F401, F811
import json

import pytest

from test_cli_cases import BASE_URL, CASE_ID, CASE_PATH, FakeAPI, compact, route_me, run
from test_cli_disclosure import VERSION_ID, api, header

DISCLOSURE_PATH = f"{CASE_PATH}/disclosures"
NOTICES_PATH = f"{DISCLOSURE_PATH}/{VERSION_ID}/notices"
NOTICE_ID = "cccccccc-dddd-4eee-8fff-000000000001"
NOTICE_PATH = f"{NOTICES_PATH}/{NOTICE_ID}"
USER_ID = "bbbbbbbb-cccc-4ddd-8eee-000000000001"
SENT_AT = "2026-10-30T09:00:00Z"

NOTICE_BODY = {
    "id": NOTICE_ID,
    "revision": 1,
    "case_id": CASE_ID,
    "package_id": "aaaaaaaa-bbbb-4ccc-8ddd-000000000001",
    "version_id": VERSION_ID,
    "version": 1,
    "channel": "email",
    "channel_other": None,
    "recipient_ref": "cert-07",
    "sent_at": "2026-10-30T09:00:00+00:00",
    "recorded_at": "2026-11-02T10:00:00+00:00",
    "recorded_by": USER_ID,
    "manual_fallback": False,
    "fallback_reason": None,
    "repeat_reason": None,
    "state": "sent",
    "state_reason_code": None,
    "state_reason": None,
    "received_at": None,
    "changed_by": USER_ID,
    "changed_at": "2026-11-02T10:00:00+00:00",
}


def record_args(*extra: str) -> tuple[str, ...]:
    return (
        "case",
        "notice",
        "record",
        CASE_ID,
        VERSION_ID,
        "--channel",
        "email",
        "--recipient-ref",
        "cert-07",
        "--sent-at",
        SENT_AT,
        "--username",
        "alice",
        *extra,
    )


def posts_to(api: FakeAPI, path: str) -> list[dict]:
    return [
        call for call in api.calls if call["method"] == "POST" and call["path"] == path
    ]


def test_record_sends_the_channel_time_and_recipient_reference_with_a_key(api, capsys):
    api.route("POST", NOTICES_PATH, status=201, body=NOTICE_BODY)
    code, out, err = run(capsys, *record_args())
    assert code == 0, err
    (call,) = posts_to(api, NOTICES_PATH)
    assert call["json"] == {
        "channel": "email",
        "channel_other": None,
        "recipient_ref": "cert-07",
        "sent_at": "2026-10-30T09:00:00+00:00",
        "manual_fallback": False,
        "fallback_reason": None,
        "repeat_reason": None,
    }
    assert header(call, "Idempotency-Key")
    assert "Notice recorded as sent" in out
    assert NOTICE_ID in out and "sent; no receipt recorded yet" in out


def test_record_json_prints_the_api_body_exactly(api, capsys):
    api.route("POST", NOTICES_PATH, status=201, body=NOTICE_BODY)
    code, out, err = run(capsys, *record_args("--json"))
    assert code == 0, err
    assert json.loads(out) == NOTICE_BODY


def test_record_other_channel_needs_a_description_and_sends_nothing(api, capsys):
    code, _, err = run(
        capsys,
        "case",
        "notice",
        "record",
        CASE_ID,
        VERSION_ID,
        "--channel",
        "other",
        "--recipient-ref",
        "cert-07",
        "--sent-at",
        SENT_AT,
        "--username",
        "alice",
    )
    assert code == 2
    assert "--channel-other" in err
    assert posts_to(api, NOTICES_PATH) == []


def test_record_other_channel_with_its_description_is_sent(api, capsys):
    api.route(
        "POST",
        NOTICES_PATH,
        status=201,
        body={**NOTICE_BODY, "channel": "other", "channel_other": "courier"},
    )
    code, _, err = run(
        capsys,
        "case",
        "notice",
        "record",
        CASE_ID,
        VERSION_ID,
        "--channel",
        "other",
        "--channel-other",
        "courier",
        "--recipient-ref",
        "cert-07",
        "--sent-at",
        SENT_AT,
        "--username",
        "alice",
    )
    assert code == 0, err
    (call,) = posts_to(api, NOTICES_PATH)
    assert (
        call["json"]["channel"] == "other"
        and call["json"]["channel_other"] == "courier"
    )


@pytest.mark.parametrize("reference", ["Jane Doe", "jane@example.org", "cert!"])
def test_a_recipient_reference_that_is_not_an_identifier_is_refused_without_a_request(
    api, capsys, reference
):
    code, _, err = run(
        capsys,
        "case",
        "notice",
        "record",
        CASE_ID,
        VERSION_ID,
        "--channel",
        "email",
        "--recipient-ref",
        reference,
        "--sent-at",
        SENT_AT,
        "--username",
        "alice",
    )
    assert code == 2
    assert "identifier" in err
    assert posts_to(api, NOTICES_PATH) == []


def test_the_manual_path_needs_its_reason_and_the_reason_needs_the_flag(api, capsys):
    code, _, err = run(capsys, *record_args("--manual-fallback"))
    assert code == 2 and "--fallback-reason" in err
    code, _, err = run(capsys, *record_args("--fallback-reason", "Approver on leave"))
    assert code == 2 and "--manual-fallback" in err
    assert posts_to(api, NOTICES_PATH) == []


def test_the_manual_path_sends_the_flag_and_the_reason(api, capsys):
    api.route(
        "POST", NOTICES_PATH, status=201, body={**NOTICE_BODY, "manual_fallback": True}
    )
    code, _, err = run(
        capsys,
        *record_args(
            "--manual-fallback",
            "--fallback-reason",
            "Approver unavailable during the deadline",
        ),
    )
    assert code == 0, err
    (call,) = posts_to(api, NOTICES_PATH)
    assert call["json"]["manual_fallback"] is True
    assert call["json"]["fallback_reason"] == "Approver unavailable during the deadline"


def test_a_repeat_notice_sends_its_reason(api, capsys):
    api.route("POST", NOTICES_PATH, status=201, body=NOTICE_BODY)
    code, _, err = run(
        capsys, *record_args("--repeat-reason", "The member asked again")
    )
    assert code == 0, err
    (call,) = posts_to(api, NOTICES_PATH)
    assert call["json"]["repeat_reason"] == "The member asked again"


@pytest.mark.parametrize(
    "status, detail, text",
    [
        (
            409,
            {"status": "version_not_approved"},
            "must approve this exact version first",
        ),
        (
            409,
            {"status": "version_superseded"},
            "a newer version of the package exists",
        ),
        (422, {"status": "repeat_reason_required"}, "--repeat-reason"),
        (403, {"status": "manual_fallback_required"}, "--manual-fallback"),
        (403, {"status": "preparer_cannot_record"}, "another person must record its notice"),
        (422, {"status": "time_out_of_range"}, "Enter the time it happened"),
        (
            409,
            {"status": "idempotency_key_reused"},
            "Run again without --idempotency-key",
        ),
    ],
)
def test_record_explains_each_refusal(api, capsys, status, detail, text):
    api.route("POST", NOTICES_PATH, status=status, body={"detail": detail})
    code, _, err = run(capsys, *record_args())
    assert code == 1
    assert text in err


def test_list_prints_each_notice_with_its_current_state(api, capsys):
    api.route("GET", NOTICES_PATH, body={"items": [NOTICE_BODY]})
    route_me(api, ["disclosures:read"])
    code, out, err = run(
        capsys, "case", "notice", "list", CASE_ID, VERSION_ID, "--username", "alice"
    )
    assert code == 0, err
    assert (
        NOTICE_ID in out and "cert-07" in out and "sent; no receipt recorded yet" in out
    )


def test_list_with_no_notice_says_so(api, capsys):
    api.route("GET", NOTICES_PATH, body={"items": []})
    route_me(api, ["disclosures:read"])
    code, out, _ = run(
        capsys, "case", "notice", "list", CASE_ID, VERSION_ID, "--username", "alice"
    )
    assert code == 0
    assert "No external notice exists" in out


def test_list_json_prints_the_api_body(api, capsys):
    api.route("GET", NOTICES_PATH, body={"items": [NOTICE_BODY]})
    code, out, _ = run(
        capsys,
        "case",
        "notice",
        "list",
        CASE_ID,
        VERSION_ID,
        "--json",
        "--username",
        "alice",
    )
    assert code == 0
    assert json.loads(out) == {"items": [NOTICE_BODY]}


def test_list_of_a_case_the_caller_cannot_see_is_not_found(api, capsys):
    api.route("GET", NOTICES_PATH, status=404, body={"detail": "case not found"})
    route_me(api, ["disclosures:read"])
    code, _, err = run(
        capsys, "case", "notice", "list", CASE_ID, VERSION_ID, "--username", "alice"
    )
    assert code == 1
    assert "not visible to your account" in err


def test_receipt_received_sends_the_time_of_the_recipients_receipt(api, capsys):
    received = {
        **NOTICE_BODY,
        "state": "receipt_received",
        "received_at": "2026-10-30T10:00:00+00:00",
        "revision": 2,
    }
    api.route("POST", f"{NOTICE_PATH}/receipt", body=received)
    code, out, err = run(
        capsys,
        "case",
        "notice",
        "receipt",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--received-at",
        "2026-10-30T10:00:00Z",
        "--username",
        "alice",
    )
    assert code == 0, err
    (call,) = posts_to(api, f"{NOTICE_PATH}/receipt")
    assert call["json"]["outcome"] == "received"
    assert call["json"]["received_at"] == "2026-10-30T10:00:00+00:00"
    assert header(call, "Idempotency-Key")
    assert "receipt received" in out


def test_receipt_unknown_sends_the_reason_code_and_never_a_receipt(api, capsys):
    unknown = {
        **NOTICE_BODY,
        "state": "receipt_unknown",
        "state_reason_code": "timeout",
        "revision": 2,
    }
    api.route("POST", f"{NOTICE_PATH}/receipt", body=unknown)
    code, out, err = run(
        capsys,
        "case",
        "notice",
        "receipt",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--unknown",
        "timeout",
        "--username",
        "alice",
    )
    assert code == 0, err
    (call,) = posts_to(api, f"{NOTICE_PATH}/receipt")
    assert call["json"]["outcome"] == "unknown"
    assert call["json"]["reason_code"] == "timeout"
    assert call["json"]["received_at"] is None
    assert "receipt unknown" in out


def test_receipt_needs_exactly_one_outcome(api, capsys):
    code, _, _ = run(
        capsys,
        "case",
        "notice",
        "receipt",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--username",
        "alice",
    )
    assert code == 2
    code, _, _ = run(
        capsys,
        "case",
        "notice",
        "receipt",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--unknown",
        "timeout",
        "--received-at",
        SENT_AT,
        "--username",
        "alice",
    )
    assert code == 2
    assert posts_to(api, f"{NOTICE_PATH}/receipt") == []


def test_a_receipt_in_the_future_is_refused_without_a_request(api, capsys):
    code, _, err = run(
        capsys,
        "case",
        "notice",
        "receipt",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--received-at",
        "2999-01-01T00:00:00Z",
        "--username",
        "alice",
    )
    assert code == 2
    assert "cannot be in the future" in err
    assert posts_to(api, f"{NOTICE_PATH}/receipt") == []


def test_follow_up_sends_the_action_and_the_reason(api, capsys):
    api.route(
        "POST",
        f"{NOTICE_PATH}/follow-up",
        body={**NOTICE_BODY, "state": "follow_up_required", "revision": 3},
    )
    code, out, err = run(
        capsys,
        "case",
        "notice",
        "follow-up",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--action",
        "require",
        "--reason",
        "Call the security contact",
        "--username",
        "alice",
    )
    assert code == 0, err
    (call,) = posts_to(api, f"{NOTICE_PATH}/follow-up")
    assert call["json"] == {"action": "require", "reason": "Call the security contact"}
    assert "follow-up required" in out


def test_follow_up_action_is_a_closed_choice(api, capsys):
    code, _, _ = run(
        capsys,
        "case",
        "notice",
        "follow-up",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--action",
        "close",
        "--reason",
        "x",
        "--username",
        "alice",
    )
    assert code == 2
    assert posts_to(api, f"{NOTICE_PATH}/follow-up") == []


def test_cancel_sends_the_reason(api, capsys):
    api.route(
        "POST",
        f"{NOTICE_PATH}/cancel",
        body={**NOTICE_BODY, "state": "cancelled", "revision": 2},
    )
    code, out, err = run(
        capsys,
        "case",
        "notice",
        "cancel",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--reason",
        "Wrong contact",
        "--username",
        "alice",
    )
    assert code == 0, err
    (call,) = posts_to(api, f"{NOTICE_PATH}/cancel")
    assert call["json"] == {"reason": "Wrong contact"}
    assert "cancelled" in out


def test_cancel_after_a_receipt_is_explained(api, capsys):
    api.route(
        "POST",
        f"{NOTICE_PATH}/cancel",
        status=409,
        body={
            "detail": {"status": "state_conflict", "current_state": "receipt_received"}
        },
    )
    code, _, err = run(
        capsys,
        "case",
        "notice",
        "cancel",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--reason",
        "Wrong contact",
        "--username",
        "alice",
    )
    assert code == 1
    assert "receipt received" in err


def test_a_closed_case_refuses_notice_changes_with_its_own_message(api, capsys):
    api.route(
        "POST",
        f"{NOTICE_PATH}/cancel",
        status=409,
        body={"detail": {"status": "case_closed"}},
    )
    code, _, err = run(
        capsys,
        "case",
        "notice",
        "cancel",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--reason",
        "x",
        "--username",
        "alice",
    )
    assert code == 1
    assert "the case is closed" in err


def test_notice_commands_send_a_key_on_every_write(api, capsys):
    api.route(
        "POST",
        f"{NOTICE_PATH}/cancel",
        body={**NOTICE_BODY, "state": "cancelled", "revision": 2},
    )
    run(
        capsys,
        "case",
        "notice",
        "cancel",
        CASE_ID,
        VERSION_ID,
        NOTICE_ID,
        "--reason",
        "x",
        "--idempotency-key",
        "retry-key-1",
        "--username",
        "alice",
    )
    (call,) = posts_to(api, f"{NOTICE_PATH}/cancel")
    assert header(call, "Idempotency-Key") == "retry-key-1"
