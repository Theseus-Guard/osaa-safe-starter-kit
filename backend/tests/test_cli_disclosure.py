"""The disclosure commands of the container CLI: case disclosure create, show and approve.

The API is replaced by the fake of tests/test_cli_cases.py, so these tests need no server and no database.
"""

import getpass
import io
import json

import pytest

from safe_kit import cli
from test_cli_cases import BASE_URL, CASE_ID, CASE_PATH, FakeAPI, compact, route_me, run

DISCLOSURE_PATH = f"{CASE_PATH}/disclosures"
VERSION_ID = "aaaaaaaa-bbbb-4ccc-8ddd-000000000002"
PACKAGE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-000000000001"
EVIDENCE_ONE = "00000000-0000-4000-8000-000000000001"
EVIDENCE_TWO = "00000000-0000-4000-8000-000000000002"
DIGEST = "ab" * 32
PASSWORD = "pw-SECRET-horse-battery"
ME_PATH = "/api/v1/auth/me"

VERSION_BODY = {
    "id": VERSION_ID,
    "package_id": PACKAGE_ID,
    "case_id": CASE_ID,
    "version": 1,
    "is_latest": True,
    "template": "confidential_rapid_alert",
    "tlp_audience": "AMBER",
    "recipient": "Member CERT",
    "purpose": "Rapid alert",
    "digest": DIGEST,
    "approval_state": "not_approved",
    "approval": None,
    "evidence": [],
    "withheld_count": 0,
    "manifest": {"items": []},
    "created_by": "bbbbbbbb-cccc-4ddd-8eee-000000000001",
    "created_at": "2026-11-02T09:00:00+00:00",
}

PREVIEW_BODY = {
    "version_id": VERSION_ID,
    "package_id": PACKAGE_ID,
    "case_id": CASE_ID,
    "version": 1,
    "template": {
        "key": "confidential_rapid_alert",
        "title": "Confidential rapid alert",
        "sections": [
            {
                "title": "Header",
                "contains": "Case reference, recipient, purpose, and TLP audience",
            }
        ],
    },
    "recipient": "Member CERT",
    "purpose": "Rapid alert",
    "tlp_audience": "AMBER",
    "digest": DIGEST,
    "approval_state": "not_approved",
    "evidence": [
        {
            "withheld": False,
            "evidence_id": EVIDENCE_ONE,
            "media_type": "text/plain",
            "size_bytes": 42,
            "tlp_label": "AMBER",
            "removed_lines": 1,
            "removed_ranges": 1,
        },
        {"withheld": True},
    ],
    "withheld_count": 1,
    "notice": "This preview shows the structure of the package.",
}

APPROVED_BODY = {
    **VERSION_BODY,
    "approval_state": "approved",
    "approval": {
        "id": "cccccccc-dddd-4eee-8fff-000000000009",
        "recipient": "Member CERT",
        "recipient_ref": "cert-07",
        "purpose": "Rapid alert",
        "digest": DIGEST,
        "preparer_id": "bbbbbbbb-cccc-4ddd-8eee-000000000001",
        "approved_by": "dddddddd-eeee-4fff-8000-000000000001",
        "approved_at": "2026-11-02T10:00:00+00:00",
        "separation_rule": "preparer_differs_from_approver",
    },
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


def header(call: dict, name: str) -> str | None:
    """A request header by name. urllib stores the names capitalised its own way, so the lookup ignores case."""
    return {key.lower(): value for key, value in call["headers"].items()}.get(
        name.lower()
    )


def create_args(*extra: str) -> list[str]:
    return [
        "case",
        "disclosure",
        "create",
        CASE_ID,
        "--template",
        "confidential_rapid_alert",
        "--tlp-audience",
        "AMBER",
        "--recipient",
        "Member CERT",
        "--purpose",
        "Rapid alert",
        "--evidence",
        EVIDENCE_ONE,
        "--username",
        "alice",
        *extra,
    ]


def test_create_sends_a_new_package_with_a_key_and_prints_the_summary(api, capsys):
    api.route("POST", DISCLOSURE_PATH, status=201, body=VERSION_BODY)
    code, out, err = run(capsys, *create_args())
    assert code == 0, err
    call = next(
        entry
        for entry in api.calls
        if entry["method"] == "POST" and entry["path"] == DISCLOSURE_PATH
    )
    assert call["json"] == {
        "package_id": None,
        "expected_version": None,
        "template": "confidential_rapid_alert",
        "tlp_audience": "AMBER",
        "recipient": "Member CERT",
        "purpose": "Rapid alert",
        "evidence_ids": [EVIDENCE_ONE],
    }
    assert header(call, "Idempotency-Key")
    assert "saved" in out and "no approval yet" in out
    assert DIGEST in out


def test_create_json_prints_the_api_body_as_received(api, capsys):
    api.route("POST", DISCLOSURE_PATH, status=201, body=VERSION_BODY)
    code, out, _ = run(capsys, *create_args("--json"))
    assert code == 0
    assert out == compact(VERSION_BODY).decode("utf-8") + "\n"


def test_create_a_new_version_needs_both_the_package_and_the_version_it_read(
    api, capsys
):
    code, out, err = run(capsys, *create_args("--package-id", PACKAGE_ID))
    assert code == 2
    assert "--expected-version" in err
    code, out, err = run(capsys, *create_args("--expected-version", "1"))
    assert code == 2
    assert "--package-id" in err


def test_create_refuses_more_than_fifty_items_and_bad_ids_before_any_request(
    api, capsys
):
    many = []
    for index in range(51):
        many += ["--evidence", f"00000000-0000-4000-8000-{index:012d}"]
    code, _, err = run(
        capsys,
        "case",
        "disclosure",
        "create",
        CASE_ID,
        "--template",
        "confidential_rapid_alert",
        "--tlp-audience",
        "AMBER",
        "--recipient",
        "r",
        "--purpose",
        "p",
        *many,
        "--username",
        "alice",
    )
    assert code == 2 and "at most 50" in err
    code, _, err = run(
        capsys,
        "case",
        "disclosure",
        "create",
        CASE_ID,
        "--template",
        "confidential_rapid_alert",
        "--tlp-audience",
        "AMBER",
        "--recipient",
        "r",
        "--purpose",
        "p",
        "--evidence",
        "not-a-uuid",
        "--username",
        "alice",
    )
    assert code == 2 and "--evidence must be a UUID" in err
    assert not any(entry["path"] == DISCLOSURE_PATH for entry in api.calls)


def test_create_maps_a_stale_version_to_the_read_again_hint(api, capsys):
    api.route(
        "POST",
        DISCLOSURE_PATH,
        status=409,
        body={"detail": {"status": "version_conflict", "current_version": 3}},
    )
    code, _, err = run(
        capsys,
        "case",
        "disclosure",
        "create",
        CASE_ID,
        "--template",
        "confidential_rapid_alert",
        "--tlp-audience",
        "AMBER",
        "--recipient",
        "r",
        "--purpose",
        "p",
        "--evidence",
        EVIDENCE_ONE,
        "--package-id",
        PACKAGE_ID,
        "--expected-version",
        "2",
        "--username",
        "alice",
    )
    assert code == 1
    assert "--expected-version 3" in err


def test_show_lists_the_versions_of_a_case(api, capsys):
    api.route("GET", DISCLOSURE_PATH, body={"items": [VERSION_BODY]})
    route_me(api, ["disclosures:read"])
    code, out, err = run(
        capsys, "case", "disclosure", "show", CASE_ID, "--username", "alice"
    )
    assert code == 0, err
    assert VERSION_ID in out and "not approved yet" in out


def test_show_with_no_versions_says_so(api, capsys):
    api.route("GET", DISCLOSURE_PATH, body={"items": []})
    route_me(api, ["disclosures:read"])
    code, out, _ = run(
        capsys, "case", "disclosure", "show", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert "No disclosure package version exists" in out


def test_show_one_version_prints_its_preview_without_evidence_content(api, capsys):
    api.route("GET", f"{DISCLOSURE_PATH}/{VERSION_ID}/preview", body=PREVIEW_BODY)
    route_me(api, ["disclosures:read"])
    code, out, _ = run(
        capsys, "case", "disclosure", "show", CASE_ID, VERSION_ID, "--username", "alice"
    )
    assert code == 0
    assert "Sections:" in out and "Header" in out
    assert "withheld: outside your TLP audience" in out
    assert DIGEST in out


def test_show_json_prints_the_preview_body(api, capsys):
    api.route("GET", f"{DISCLOSURE_PATH}/{VERSION_ID}/preview", body=PREVIEW_BODY)
    code, out, _ = run(
        capsys,
        "case",
        "disclosure",
        "show",
        CASE_ID,
        VERSION_ID,
        "--json",
        "--username",
        "alice",
    )
    assert code == 0
    assert json.loads(out) == PREVIEW_BODY


def test_show_of_a_case_the_caller_cannot_see_is_not_found(api, capsys):
    api.route("GET", DISCLOSURE_PATH, status=404, body={"detail": "case not found"})
    route_me(api, ["disclosures:read"])
    code, _, err = run(
        capsys, "case", "disclosure", "show", CASE_ID, "--username", "alice"
    )
    assert code == 1
    assert "case not found" in err


def test_approve_sends_the_recipient_purpose_and_digest_with_a_key(api, capsys):
    api.route(
        "POST",
        f"{DISCLOSURE_PATH}/{VERSION_ID}/approvals",
        status=201,
        body=APPROVED_BODY,
    )
    code, out, err = run(
        capsys,
        "case",
        "disclosure",
        "approve",
        CASE_ID,
        VERSION_ID,
        "--recipient",
        "Member CERT",
        "--purpose",
        "Rapid alert",
        "--digest",
        DIGEST,
        "--recipient-ref",
        "cert-07",
        "--username",
        "alice",
    )
    assert code == 0, err
    call = next(
        entry
        for entry in api.calls
        if entry["method"] == "POST"
        and entry["path"] == f"{DISCLOSURE_PATH}/{VERSION_ID}/approvals"
    )
    assert call["json"] == {
        "recipient": "Member CERT",
        "purpose": "Rapid alert",
        "digest": DIGEST,
        "recipient_ref": "cert-07",
    }
    assert header(call, "Idempotency-Key")
    assert "Approval recorded" in out and "dddddddd-eeee-4fff-8000-000000000001" in out
    assert "Recipient reference" in out and "cert-07" in out


def test_approve_refuses_a_recipient_reference_with_a_name_before_any_request(
    api, capsys
):
    code, _, err = run(
        capsys,
        "case",
        "disclosure",
        "approve",
        CASE_ID,
        VERSION_ID,
        "--recipient",
        "Member CERT",
        "--purpose",
        "Rapid alert",
        "--digest",
        DIGEST,
        "--recipient-ref",
        "Member CERT",
        "--username",
        "alice",
    )
    assert code == 2
    assert "--recipient-ref is the identifier of the approved recipient" in err
    assert not any(entry["method"] == "POST" for entry in api.calls)


def test_approve_refuses_a_digest_that_is_not_64_hex_characters_before_any_request(
    api, capsys
):
    code, _, err = run(
        capsys,
        "case",
        "disclosure",
        "approve",
        CASE_ID,
        VERSION_ID,
        "--recipient",
        "Member CERT",
        "--purpose",
        "Rapid alert",
        "--digest",
        "ABC",
        "--recipient-ref",
        "cert-07",
        "--username",
        "alice",
    )
    assert code == 2
    assert "--digest must be the 64-character digest" in err
    assert not any(entry["method"] == "POST" for entry in api.calls)


@pytest.mark.parametrize(
    ("status", "detail", "text"),
    [
        (
            "preparer_is_approver",
            {"status": "preparer_is_approver"},
            "another approver must approve it",
        ),
        (
            "approval_binding_mismatch",
            {"status": "approval_binding_mismatch"},
            "Copy them exactly",
        ),
        (
            "not_latest_version",
            {"status": "not_latest_version"},
            "only its latest version can be approved",
        ),
        (
            "evidence_not_visible",
            {"status": "evidence_not_visible"},
            "outside your TLP audience",
        ),
    ],
)
def test_approve_explains_each_refusal(api, capsys, status, detail, text):
    code_status = (
        403 if status in ("preparer_is_approver", "evidence_not_visible") else 409
    )
    api.route(
        "POST",
        f"{DISCLOSURE_PATH}/{VERSION_ID}/approvals",
        status=code_status,
        body={"detail": detail},
    )
    code, _, err = run(
        capsys,
        "case",
        "disclosure",
        "approve",
        CASE_ID,
        VERSION_ID,
        "--recipient",
        "Member CERT",
        "--purpose",
        "Rapid alert",
        "--digest",
        DIGEST,
        "--recipient-ref",
        "cert-07",
        "--username",
        "alice",
    )
    assert code == 1
    assert text in err


def test_approve_json_prints_the_api_body(api, capsys):
    api.route(
        "POST",
        f"{DISCLOSURE_PATH}/{VERSION_ID}/approvals",
        status=201,
        body=APPROVED_BODY,
    )
    code, out, _ = run(
        capsys,
        "case",
        "disclosure",
        "approve",
        CASE_ID,
        VERSION_ID,
        "--recipient",
        "Member CERT",
        "--purpose",
        "Rapid alert",
        "--digest",
        DIGEST,
        "--recipient-ref",
        "cert-07",
        "--json",
        "--username",
        "alice",
    )
    assert code == 0
    assert out == compact(APPROVED_BODY).decode("utf-8") + "\n"


# --- case disclosure export: the versioned JSON of one approved version ----------------------------------------------

EXPORT_PATH = f"{DISCLOSURE_PATH}/{VERSION_ID}/export"
EXPORT_BODY = {
    "disclosure_export_schema_version": "0.1.0",
    "disclosure_export": {
        "case_id": CASE_ID,
        "package_id": PACKAGE_ID,
        "version_id": VERSION_ID,
        "version": 1,
        "status": "approved",
        "latest_version": 1,
        "template": {
            "key": "confidential_rapid_alert",
            "title": "Confidential rapid alert",
            "sections": [],
        },
        "recipient": "Member CERT",
        "purpose": "Rapid alert",
        "tlp_audience": "AMBER",
        "digest": DIGEST,
        "items": [{"withheld": True}],
        "withheld_count": 1,
        "manifest": {"items": []},
        "approval": {
            "id": "cccccccc-dddd-4eee-8fff-000000000009",
            "approved_by": "dddddddd-eeee-4fff-8000-000000000001",
            "approved_at": "2026-11-02T10:00:00+00:00",
            "digest": DIGEST,
            "separation_rule": "preparer_differs_from_approver",
        },
    },
}


def test_export_writes_the_document_to_the_file_and_prints_nothing(
    api, capsys, tmp_path
):
    api.route("GET", EXPORT_PATH, body=EXPORT_BODY)
    target = tmp_path / "disclosure-export.json"

    code, out, err = run(
        capsys,
        "case",
        "disclosure",
        "export",
        CASE_ID,
        VERSION_ID,
        "--username",
        "alice",
        "--out",
        str(target),
    )

    assert code == 0, err
    assert out == ""
    assert target.read_bytes() == compact(EXPORT_BODY) + b"\n"
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


def test_export_without_a_file_prints_the_document_as_received(api, capsys):
    api.route("GET", EXPORT_PATH, body=EXPORT_BODY)

    code, out, _ = run(
        capsys,
        "case",
        "disclosure",
        "export",
        CASE_ID,
        VERSION_ID,
        "--username",
        "alice",
    )

    assert code == 0
    assert json.loads(out) == EXPORT_BODY


def test_export_of_an_unapproved_version_is_refused_with_the_next_step(api, capsys):
    api.route(
        "GET",
        EXPORT_PATH,
        status=409,
        body={"detail": {"status": "version_not_approved"}},
    )

    code, out, err = run(
        capsys,
        "case",
        "disclosure",
        "export",
        CASE_ID,
        VERSION_ID,
        "--username",
        "alice",
    )

    assert code == 1
    assert out == ""
    assert "only an approved version can be exported" in err
    assert "case disclosure approve" in err


def test_export_of_a_version_the_caller_cannot_see_is_not_found(api, capsys):
    api.route(
        "GET", EXPORT_PATH, status=404, body={"detail": "disclosure version not found"}
    )

    code, out, err = run(
        capsys,
        "case",
        "disclosure",
        "export",
        CASE_ID,
        VERSION_ID,
        "--username",
        "alice",
    )

    assert code == 1
    assert out == ""
    assert "not visible to your account" in err


def test_export_to_an_unwritable_path_is_a_usage_error(api, capsys, tmp_path):
    api.route("GET", EXPORT_PATH, body=EXPORT_BODY)
    target = tmp_path / "missing-directory" / "disclosure-export.json"

    code, out, err = run(
        capsys,
        "case",
        "disclosure",
        "export",
        CASE_ID,
        VERSION_ID,
        "--username",
        "alice",
        "--out",
        str(target),
    )

    assert code == 2
    assert "cannot write" in err


def test_export_with_a_malformed_version_id_is_refused_before_any_request(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "disclosure",
        "export",
        CASE_ID,
        "not-a-uuid",
        "--username",
        "alice",
    )

    assert code == 2
    assert "VERSION_ID must be a UUID" in err
    assert not any(path.endswith("/export") for _, path in api.paths())
