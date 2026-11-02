"""Evidence subcommands for redaction and TLP: evidence show, derivatives, redact, and show-redaction of the container CLI.

The API is replaced by the fake behind urllib.request.urlopen. A TLP-hidden item and a missing item give the same
"evidence not found" answer, both from the API and from the CLI.
"""

import getpass
import io
import uuid

import pytest

from safe_kit import cli
from test_cli_cases import BASE_URL, PASSWORD, FakeAPI, run

USER_ID = "44444444-4444-4444-4444-444444444444"
ORIGINAL_ID = "55555555-5555-4555-8555-555555555555"
DERIVED_ID = "66666666-6666-4666-8666-666666666666"
HIDDEN_ID = "77777777-7777-4777-8777-777777777777"
CASE_ID = "11111111-2222-4333-8444-555555555555"
REDACTION_ID = "88888888-8888-4888-8888-888888888888"
ORIGINAL_PATH = f"/api/v1/evidence/{ORIGINAL_ID}"
DERIVED_PATH = f"/api/v1/evidence/{DERIVED_ID}"
HIDDEN_PATH = f"/api/v1/evidence/{HIDDEN_ID}"
DERIVATIVES_PATH = f"/api/v1/evidence/{ORIGINAL_ID}/derivatives"
REDACT_PATH = f"/api/v1/evidence/{ORIGINAL_ID}/derivatives"
REDACTION_PATH = f"/api/v1/evidence/{DERIVED_ID}/redaction"
NOT_FOUND = {"detail": "evidence not found"}


def item(evidence_id=ORIGINAL_ID, tlp_label=None, filename="notes.txt", status="available"):
    return {
        "id": evidence_id,
        "case_id": CASE_ID,
        "uploaded_by": USER_ID,
        "original_filename": filename,
        "media_type": "text/plain",
        "size_bytes": 74,
        "sha256_plaintext": "a" * 64,
        "status": status,
        "created_at": "2026-11-01T09:00:00Z",
        "tlp_label": tlp_label,
    }


def derivation_body(label="AMBER", claims=()):
    return {
        "redaction_id": REDACTION_ID,
        "original_evidence_id": ORIGINAL_ID,
        "derived": item(DERIVED_ID, tlp_label=label, filename="redacted-notes.txt"),
        "manifest": {
            "method": "replace_lines",
            "replacement": "[REDACTED]",
            "source_sha256": "a" * 64,
            "tlp_label": label,
            "lines_total": 4,
            "removed_lines": 1,
            "removed": [{"from_line": 3, "to_line": 3, "lines": 1, "reason": "contains an account number"}],
        },
        "claim_assessments": list(claims),
    }


CLAIM = {
    "review_layer_id": "99999999-9999-4999-8999-999999999999",
    "round_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    "layer": "model",
    "outcome": "insufficient_evidence",
    "reason": "The claim cites the original. The redaction removed content from the derived item.",
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


def calls(api: FakeAPI, method: str, path: str) -> list[dict]:
    return [call for call in api.calls if call["method"] == method and call["path"] == path]


# --- evidence show and derivatives -------------------------------------------------------------------------------------


def test_show_prints_the_tlp_label_of_a_derived_item(api, capsys):
    api.route("GET", DERIVED_PATH, body=item(DERIVED_ID, tlp_label="AMBER"))
    code, out, err = run(capsys, "evidence", "show", DERIVED_ID, "--username", "alice")
    assert code == 0, err
    assert f"Evidence {DERIVED_ID}" in out
    assert any(line.strip().startswith("TLP label:") and line.endswith("AMBER") for line in out.splitlines())


def test_show_says_an_original_has_no_tlp_label(api, capsys):
    api.route("GET", ORIGINAL_PATH, body=item())
    code, out, _ = run(capsys, "evidence", "show", ORIGINAL_ID, "--username", "alice")
    assert code == 0
    assert "none (original, no TLP rule)" in out


def test_show_json_prints_the_api_body_as_received(api, capsys):
    api.route("GET", DERIVED_PATH, body=item(DERIVED_ID, tlp_label="RED"))
    code, out, _ = run(capsys, "evidence", "show", DERIVED_ID, "--json", "--username", "alice")
    assert code == 0
    assert '"tlp_label":"RED"' in out


def test_show_of_an_item_the_tlp_rule_hides_says_not_found_like_a_missing_one(api, capsys, monkeypatch):
    missing_id = str(uuid.uuid4())
    api.route("GET", HIDDEN_PATH, status=404, body=NOT_FOUND)
    api.route("GET", f"/api/v1/evidence/{missing_id}", status=404, body=NOT_FOUND)
    code_hidden, _, err_hidden = run(capsys, "evidence", "show", HIDDEN_ID, "--username", "alice")
    # Each run reads its password from stdin, so the second run needs a fresh one.
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(PASSWORD + "\n"))
    code_missing, _, err_missing = run(capsys, "evidence", "show", missing_id, "--username", "alice")
    assert code_hidden == code_missing == 1
    assert err_hidden == err_missing == "error: evidence not found\n"


def test_show_refused_for_a_role_without_evidence_read(api, capsys):
    api.route("GET", ORIGINAL_PATH, status=403, body={"detail": "forbidden"})
    code, _, err = run(capsys, "evidence", "show", ORIGINAL_ID, "--username", "alice")
    assert code == 1
    assert "does not hold the permission to read evidence" in err


def test_derivatives_lists_only_the_copies_the_api_returns(api, capsys):
    api.route("GET", DERIVATIVES_PATH, body=[item(DERIVED_ID, tlp_label="AMBER", filename="redacted-notes.txt")])
    code, out, _ = run(capsys, "evidence", "derivatives", ORIGINAL_ID, "--username", "alice")
    assert code == 0
    assert "TLP LABEL" in out
    assert DERIVED_ID in out and "AMBER" in out


def test_derivatives_says_so_when_none_are_visible(api, capsys):
    api.route("GET", DERIVATIVES_PATH, body=[])
    code, out, _ = run(capsys, "evidence", "derivatives", ORIGINAL_ID, "--username", "alice")
    assert code == 0
    assert "No redacted copies of this evidence are visible to your account." in out


# --- evidence redact ---------------------------------------------------------------------------------------------------


def test_redact_posts_the_ranges_and_the_label_with_a_key_and_prints_the_manifest(api, capsys):
    api.route("POST", REDACT_PATH, status=201, body=derivation_body("AMBER"))
    code, out, err = run(
        capsys, "evidence", "redact", ORIGINAL_ID, "--remove", "3:contains an account number",
        "--tlp", "AMBER", "--username", "alice",
    )
    assert code == 0, err
    assert "Redacted copy stored. The original is unchanged." in out
    assert "lines 3 (1): contains an account number" in out
    assert "1 removed of 4" in out
    sent = calls(api, "POST", REDACT_PATH)[0]
    assert sent["json"] == {
        "removed": [{"from_line": 3, "to_line": 3, "reason": "contains an account number"}],
        "tlp_label": "AMBER",
    }
    headers = {name.lower(): value for name, value in sent["headers"].items()}
    assert str(uuid.UUID(headers["idempotency-key"])) == headers["idempotency-key"]


def test_a_range_with_a_dash_is_sent_as_from_and_to(api, capsys):
    api.route("POST", REDACT_PATH, status=201, body=derivation_body("GREEN"))
    code, _, _ = run(
        capsys, "evidence", "redact", ORIGINAL_ID, "--remove", "2-5:personal details", "--tlp", "GREEN", "--username", "alice"
    )
    assert code == 0
    assert calls(api, "POST", REDACT_PATH)[0]["json"]["removed"] == [
        {"from_line": 2, "to_line": 5, "reason": "personal details"}
    ]


def test_redact_prints_each_claim_assessment(api, capsys):
    api.route("POST", REDACT_PATH, status=201, body=derivation_body("AMBER", claims=[CLAIM]))
    code, out, _ = run(
        capsys, "evidence", "redact", ORIGINAL_ID, "--remove", "3:x", "--tlp", "AMBER", "--username", "alice"
    )
    assert code == 0
    assert "model in round" in out
    assert "insufficient_evidence" in out


def test_redact_with_no_range_is_refused_before_any_login(api, capsys):
    code, _, err = run(capsys, "evidence", "redact", ORIGINAL_ID, "--tlp", "AMBER", "--username", "alice")
    assert code == 2
    assert "at least one --remove" in err
    assert api.calls == []


@pytest.mark.parametrize("text", ["three:reason", "0:reason", "5-3:reason", "3", "3:   "])
def test_a_malformed_range_is_refused_before_any_login(api, capsys, text):
    code, _, err = run(capsys, "evidence", "redact", ORIGINAL_ID, "--remove", text, "--tlp", "AMBER", "--username", "alice")
    assert code == 2
    assert "--remove" in err or "reason" in err
    assert api.calls == []


def test_redact_refused_when_the_label_is_less_restrictive_shows_the_api_text(api, capsys):
    api.route(
        "POST",
        REDACT_PATH,
        status=422,
        body={"detail": "the TLP label must be at least as restrictive as the original's label"},
    )
    code, _, err = run(
        capsys, "evidence", "redact", ORIGINAL_ID, "--remove", "3:x", "--tlp", "CLEAR", "--username", "alice"
    )
    assert code == 1
    assert err == "error: the TLP label must be at least as restrictive as the original's label\n"


def test_redact_of_an_item_the_tlp_rule_hides_says_not_found(api, capsys):
    api.route("POST", f"/api/v1/evidence/{HIDDEN_ID}/derivatives", status=404, body=NOT_FOUND)
    code, _, err = run(
        capsys, "evidence", "redact", HIDDEN_ID, "--remove", "3:x", "--tlp", "AMBER", "--username", "alice"
    )
    assert code == 1
    assert err == "error: evidence not found\n"


def test_redact_refused_for_a_role_without_evidence_write_says_so(api, capsys):
    api.route("POST", REDACT_PATH, status=403, body={"detail": "forbidden"})
    code, _, err = run(
        capsys, "evidence", "redact", ORIGINAL_ID, "--remove", "3:x", "--tlp", "AMBER", "--username", "alice"
    )
    assert code == 1
    assert "does not hold the permission to redact evidence" in err


def test_redact_needs_a_tlp_label_from_the_five_values(api, capsys):
    code, _, _ = run(capsys, "evidence", "redact", ORIGINAL_ID, "--remove", "3:x", "--tlp", "PURPLE", "--username", "alice")
    assert code == 2
    assert api.calls == []


# --- evidence show-redaction -------------------------------------------------------------------------------------------


def test_show_redaction_prints_the_manifest_and_the_claims(api, capsys):
    api.route("GET", REDACTION_PATH, body=derivation_body("AMBER", claims=[CLAIM]))
    code, out, err = run(capsys, "evidence", "show-redaction", DERIVED_ID, "--username", "alice")
    assert code == 0, err
    assert f"Redacted copy {DERIVED_ID}" in out
    assert "Original evidence ID" in out and ORIGINAL_ID in out
    assert "TLP label" in out and "AMBER" in out
    assert "lines 3 (1): contains an account number" in out
    assert "Claim assessments:" in out


def test_show_redaction_says_when_no_claim_cites_the_original(api, capsys):
    api.route("GET", REDACTION_PATH, body=derivation_body("AMBER", claims=[]))
    code, out, _ = run(capsys, "evidence", "show-redaction", DERIVED_ID, "--username", "alice")
    assert code == 0
    assert "none (no review layer cites the original)" in out


def test_show_redaction_of_an_original_or_a_hidden_item_gives_not_found(api, capsys):
    api.route("GET", f"/api/v1/evidence/{ORIGINAL_ID}/redaction", status=404, body=NOT_FOUND)
    code, _, err = run(capsys, "evidence", "show-redaction", ORIGINAL_ID, "--username", "alice")
    assert code == 1
    assert err == "error: evidence not found\n"
