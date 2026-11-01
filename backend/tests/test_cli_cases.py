"""Case subcommands of the container CLI. The API is replaced by a fake behind urllib.request.urlopen."""

import getpass
import io
import json
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest

from safe_kit import cli

BASE_URL = "http://api.test:8000"
ACCESS = "access-SECRET-token-7f3a"
REFRESH = "refresh-SECRET-token-9c1e"
PASSWORD = "pw-SECRET-horse-battery"
CASE_ID = "11111111-2222-4333-8444-555555555555"
CASE_BODY = {
    "id": CASE_ID,
    "reference": "SAFE-2026-000001",
    "title": "Phishing campaign",
    "awareness_time": "2026-10-30T09:00:00Z",
    "status": "open",
    "calendar_version": "2026.1",
    "created_at": "2026-10-31T10:00:00Z",
    "deadlines": [{"kind": "initial_notice", "due_at": "2026-11-01T09:00:00Z", "status": "pending"}],
}
SUMMARY = {
    "id": CASE_ID,
    "reference": "SAFE-2026-000001",
    "title": "Phishing campaign",
    "awareness_time": "2026-10-30T09:00:00Z",
    "status": "open",
    "created_at": "2026-10-31T10:00:00Z",
}


def compact(body) -> bytes:
    """The API writes compact JSON. The fake does the same, so --json can be compared byte for byte."""
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def token_body(password_change_required: bool = False) -> dict:
    return {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "token_type": "bearer",
        "expires_in": 900,
        "password_change_required": password_change_required,
    }


class FakeResponse:
    def __init__(self, body: bytes):
        self.status = 200
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeAPI:
    """Routes (method, path) to a canned answer. An unrouted request fails the test loudly."""

    def __init__(self, monkeypatch):
        self.routes: dict[tuple[str, str], tuple[int, bytes, Exception | None]] = {}
        self.calls: list[dict] = []
        monkeypatch.setattr(cli.urllib.request, "urlopen", self._urlopen)
        self.route("POST", "/api/v1/auth/token", body=token_body())
        self.route("POST", "/api/v1/auth/logout", status=204)

    def route(self, method: str, path: str, status: int = 200, body=None, error: Exception | None = None):
        if body is None:
            encoded = b""
        elif isinstance(body, bytes):
            encoded = body
        else:
            encoded = compact(body)
        self.routes[(method, path)] = (status, encoded, error)

    def _urlopen(self, request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        method = request.get_method()
        self.calls.append(
            {
                "method": method,
                "path": parts.path,
                "query": urllib.parse.parse_qs(parts.query),
                "headers": dict(request.header_items()),
                "json": json.loads(request.data) if request.data else None,
            }
        )
        key = (method, parts.path)
        if key not in self.routes:
            raise AssertionError(f"no fake route for {method} {parts.path}")
        status, body, error = self.routes[key]
        if error is not None:
            raise error
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(body))
        return FakeResponse(body)

    def paths(self) -> list[tuple[str, str]]:
        return [(call["method"], call["path"]) for call in self.calls]


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SAFE_API_URL", BASE_URL)
    monkeypatch.delenv("SAFE_USERNAME", raising=False)
    # By default the password comes from a non-terminal stdin. A test that reaches getpass is a failure.
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(PASSWORD + "\n"))

    def no_prompt(prompt=""):
        raise AssertionError("getpass must not be used when stdin is not a terminal")

    monkeypatch.setattr(getpass, "getpass", no_prompt)
    return FakeAPI(monkeypatch)


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def future_time() -> str:
    return (datetime.now(UTC) + timedelta(days=1)).isoformat()


CREATE = ("case", "create", "--title", "Phishing campaign", "--awareness-time", "2026-10-30T09:00:00Z")


# --- create ---


def test_create_with_naive_awareness_time_is_a_usage_error(api, capsys):
    code, out, err = run(
        capsys, "case", "create", "--title", "Phishing", "--awareness-time", "2026-10-30T09:00:00", "--username", "alice"
    )
    assert code == 2
    assert api.calls == []
    assert "timezone" in err


def test_create_with_future_awareness_time_is_rejected_before_any_request(api, capsys):
    code, out, err = run(
        capsys, "case", "create", "--title", "Phishing", "--awareness-time", future_time(), "--username", "alice"
    )
    assert code == 2
    assert api.calls == []
    assert "cannot be in the future" in err


def test_create_success_prints_the_case_and_ends_the_session(api, capsys):
    api.route("POST", "/api/v1/cases", status=201, body=CASE_BODY)
    code, out, err = run(capsys, *CREATE, "--username", "alice")
    assert code == 0
    assert "SAFE-2026-000001" in out
    assert "2026-10-30T09:00:00Z" in out
    assert "initial_notice: due 2026-11-01T09:00:00Z (UTC), pending" in out
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("POST", "/api/v1/cases"),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert api.calls[0]["json"] == {"username": "alice", "password": PASSWORD}
    assert api.calls[1]["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert api.calls[1]["json"] == {"title": "Phishing campaign", "awareness_time": "2026-10-30T09:00:00+00:00"}
    assert api.calls[2]["json"] == {"refresh_token": REFRESH}


def test_create_json_prints_the_api_body_exactly(api, capsys):
    api.route("POST", "/api/v1/cases", status=201, body=CASE_BODY)
    code, out, err = run(capsys, *CREATE, "--username", "alice", "--json")
    assert code == 0
    assert out == compact(CASE_BODY).decode("utf-8") + "\n"
    assert json.loads(out) == CASE_BODY
    assert err == ""


def test_create_maps_missing_timezone_to_a_clear_message(api, capsys):
    api.route("POST", "/api/v1/cases", status=409, body={"detail": "system timezone is not configured"})
    code, out, err = run(capsys, *CREATE, "--username", "alice")
    assert code == 1
    assert out == ""
    assert "error: system timezone is not configured" in err


def test_create_maps_validation_errors_to_a_clear_message(api, capsys):
    api.route("POST", "/api/v1/cases", status=422, body={"detail": []})
    code, out, err = run(capsys, *CREATE, "--username", "alice")
    assert code == 1
    assert "check the values you entered" in err


def test_create_rejects_a_blank_title_without_a_request(api, capsys):
    code, out, err = run(
        capsys, "case", "create", "--title", "   ", "--awareness-time", "2026-10-30T09:00:00Z", "--username", "alice"
    )
    assert code == 2
    assert api.calls == []


# --- list ---


def test_list_sends_the_paging_arguments(api, capsys):
    page = {"items": [SUMMARY], "total": 45, "limit": 10, "offset": 20}
    api.route("GET", "/api/v1/cases", body=page)
    code, out, err = run(capsys, "case", "list", "--limit", "10", "--offset", "20", "--username", "alice")
    assert code == 0
    get = api.calls[1]
    assert get["query"] == {"limit": ["10"], "offset": ["20"]}
    assert get["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert "Showing 21-21 of 45" in out
    assert "SAFE-2026-000001" in out


def test_list_uses_the_web_page_size_by_default(api, capsys):
    api.route("GET", "/api/v1/cases", body={"items": [], "total": 0, "limit": 20, "offset": 0})
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 0
    assert api.calls[1]["query"] == {"limit": ["20"], "offset": ["0"]}
    assert "No cases to show. Total: 0" in out


def test_list_json_prints_the_api_body_exactly(api, capsys):
    page = {"items": [SUMMARY], "total": 1, "limit": 20, "offset": 0}
    api.route("GET", "/api/v1/cases", body=page)
    code, out, err = run(capsys, "case", "list", "--username", "alice", "--json")
    assert code == 0
    assert out == compact(page).decode("utf-8") + "\n"


@pytest.mark.parametrize("argv", [("--limit", "0"), ("--limit", "201"), ("--offset", "-1")])
def test_list_rejects_out_of_range_paging_without_a_request(api, capsys, argv):
    code, out, err = run(capsys, "case", "list", "--username", "alice", *argv)
    assert code == 2
    assert api.calls == []


# --- show ---


def test_show_success_prints_the_case(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}", body=CASE_BODY)
    code, out, err = run(capsys, "case", "show", CASE_ID, "--username", "alice")
    assert code == 0
    assert "Case SAFE-2026-000001" in out
    assert "Awareness (UTC):  2026-10-30T09:00:00Z" in out


def test_show_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}", body=CASE_BODY)
    code, out, err = run(capsys, "case", "show", CASE_ID, "--username", "alice", "--json")
    assert code == 0
    assert out == compact(CASE_BODY).decode("utf-8") + "\n"


def test_show_not_found_reports_case_not_found(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}", status=404, body={"detail": "case not found"})
    code, out, err = run(capsys, "case", "show", CASE_ID, "--username", "alice")
    assert code == 1
    assert out == ""
    assert "case not found" in err


def test_show_rejects_an_id_that_is_not_a_uuid_without_a_request(api, capsys):
    code, out, err = run(capsys, "case", "show", "not-a-case", "--username", "alice")
    assert code == 2
    assert api.calls == []


# --- login and session ---


def test_login_failure_reports_invalid_credentials(api, capsys):
    api.route("POST", "/api/v1/auth/token", status=401, body={"detail": "invalid credentials"})
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 1
    assert out == ""
    assert "error: invalid credentials" in err
    # Nothing was issued, so there is nothing to log out.
    assert api.paths() == [("POST", "/api/v1/auth/token")]


def test_password_change_required_stops_with_a_web_ui_message(api, capsys):
    api.route("POST", "/api/v1/auth/token", body=token_body(password_change_required=True))
    code, out, err = run(capsys, *CREATE, "--username", "alice")
    assert code == 1
    assert out == ""
    assert "change your password" in err
    assert "web UI" in err
    # The case endpoint is never called. The session is still ended.
    assert ("POST", "/api/v1/cases") not in api.paths()
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


def test_unmapped_status_reports_the_http_status(api, capsys):
    api.route("GET", "/api/v1/cases", status=500, body={"detail": "boom"})
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 1
    assert "error: request failed (HTTP 500)" in err


def test_failed_logout_does_not_change_the_exit_code(api, capsys):
    api.route("GET", "/api/v1/cases", body={"items": [], "total": 0, "limit": 20, "offset": 0})
    api.route("POST", "/api/v1/auth/logout", status=500, body={"detail": "boom"})
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 0


def test_unreachable_api_reports_that_the_server_could_not_be_reached(api, capsys):
    api.route("POST", "/api/v1/auth/token", error=urllib.error.URLError("connection refused"))
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 1
    assert "could not be reached" in err


def test_username_comes_from_the_environment(api, capsys, monkeypatch):
    monkeypatch.setenv("SAFE_USERNAME", "bob")
    api.route("GET", "/api/v1/cases", body={"items": [], "total": 0, "limit": 20, "offset": 0})
    code, out, err = run(capsys, "case", "list")
    assert code == 0
    assert api.calls[0]["json"]["username"] == "bob"


def test_missing_username_is_a_usage_error_without_a_request(api, capsys):
    code, out, err = run(capsys, "case", "list")
    assert code == 2
    assert api.calls == []
    assert "SAFE_USERNAME" in err


def test_password_is_read_from_stdin_when_stdin_is_not_a_terminal(api, capsys):
    api.route("GET", "/api/v1/cases", body={"items": [], "total": 0, "limit": 20, "offset": 0})
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 0
    assert api.calls[0]["json"] == {"username": "alice", "password": PASSWORD}


def test_empty_password_on_stdin_is_a_usage_error_without_a_request(api, capsys, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n"))
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 2
    assert api.calls == []


def test_password_is_prompted_with_getpass_in_a_terminal(api, capsys, monkeypatch):
    prompts = []

    def fake_getpass(prompt=""):
        prompts.append(prompt)
        return PASSWORD

    monkeypatch.setattr(cli.sys, "stdin", Terminal())
    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    api.route("GET", "/api/v1/cases", body={"items": [], "total": 0, "limit": 20, "offset": 0})
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 0
    assert len(prompts) == 1
    assert api.calls[0]["json"]["password"] == PASSWORD


# --- secrets never reach the output ---


@pytest.mark.parametrize(
    "argv",
    [
        (*CREATE, "--username", "alice"),
        (*CREATE, "--username", "alice", "--json"),
        ("case", "list", "--username", "alice"),
        ("case", "show", CASE_ID, "--username", "alice", "--json"),
    ],
)
def test_stdout_and_stderr_never_contain_the_token_or_the_password(api, capsys, argv):
    api.route("POST", "/api/v1/cases", status=201, body=CASE_BODY)
    api.route("GET", "/api/v1/cases", body={"items": [SUMMARY], "total": 1, "limit": 20, "offset": 0})
    api.route("GET", f"/api/v1/cases/{CASE_ID}", body=CASE_BODY)
    code, out, err = run(capsys, *argv)
    assert code == 0
    for secret in (ACCESS, REFRESH, PASSWORD):
        assert secret not in out
        assert secret not in err


# --- import ---

IMPORT_PATH = "/api/v1/cases/import"
IMPORT_RECORD = {"case_id": "3f2b8c1e-6a4d-4e9b-8f1a-2c7d9e0b5a11", "record_schema_version": "0.1.0"}
IMPORT_FILE = {"import_format_version": 1, "records": [IMPORT_RECORD]}


def import_summary(**counts) -> dict:
    base = {"imported": 0, "duplicate": 0, "conflict": 0, "rejected": 0, "failed": 0, "results": []}
    base.update(counts)
    return base


def write_file(tmp_path, content: bytes, name: str = "import.json") -> str:
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def test_import_sends_the_file_and_prints_the_summary(api, capsys, tmp_path):
    summary = import_summary(
        imported=1,
        duplicate=1,
        results=[
            {"index": 0, "status": "imported", "source_case_id": None, "case_id": CASE_ID,
             "reference": "SAFE-2026-000001", "errors": []},
            {"index": 1, "status": "duplicate", "source_case_id": None, "case_id": CASE_ID,
             "reference": "SAFE-2026-000002", "errors": []},
        ],
    )
    api.route("POST", IMPORT_PATH, body=summary)
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(capsys, "case", "import", path, "--username", "alice")

    assert code == 0
    assert "Import finished: 1 imported, 1 duplicate, 0 conflict, 0 rejected, 0 failed." in out
    assert "records[0]: imported as SAFE-2026-000001" in out
    assert "records[1]: duplicate of SAFE-2026-000002, nothing written" in out
    assert api.paths() == [("POST", "/api/v1/auth/token"), ("POST", IMPORT_PATH), ("POST", "/api/v1/auth/logout")]
    assert api.calls[1]["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert api.calls[1]["json"] == IMPORT_FILE
    assert err == ""


def test_import_json_prints_the_api_body_exactly(api, capsys, tmp_path):
    summary = import_summary(imported=1, results=[{"index": 0, "status": "imported", "source_case_id": None,
                                                    "case_id": CASE_ID, "reference": "SAFE-2026-000001", "errors": []}])
    api.route("POST", IMPORT_PATH, body=summary)
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(capsys, "case", "import", path, "--username", "alice", "--json")

    assert code == 0
    assert out == compact(summary).decode("utf-8") + "\n"


def test_import_exits_1_and_names_the_rejected_record(api, capsys, tmp_path):
    summary = import_summary(
        imported=1,
        rejected=1,
        results=[
            {"index": 0, "status": "imported", "source_case_id": None, "case_id": CASE_ID,
             "reference": "SAFE-2026-000001", "errors": []},
            {"index": 1, "status": "rejected", "source_case_id": None, "case_id": None, "reference": None,
             "errors": ["/record_schema_version: '0.2.0' was expected"]},
        ],
    )
    api.route("POST", IMPORT_PATH, body=summary)
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(capsys, "case", "import", path, "--username", "alice")

    assert code == 1
    assert "records[1]: rejected, nothing written" in out
    assert "/record_schema_version: '0.2.0' was expected" in out
    assert "1 record(s) were not stored" in err
    # The session still ends when the command fails.
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


def test_import_with_only_duplicates_exits_0(api, capsys, tmp_path):
    api.route("POST", IMPORT_PATH, body=import_summary(duplicate=1))
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(capsys, "case", "import", path, "--username", "alice")

    assert code == 0
    assert "1 duplicate" in out


def test_import_with_invalid_json_is_a_usage_error_without_a_request(api, capsys, tmp_path):
    path = write_file(tmp_path, b"{not json")

    code, out, err = run(capsys, "case", "import", path, "--username", "alice")

    assert code == 2
    assert api.calls == []
    assert "not valid JSON" in err


def test_import_file_must_be_a_json_object(api, capsys, tmp_path):
    path = write_file(tmp_path, b"[]")

    code, out, err = run(capsys, "case", "import", path, "--username", "alice")

    assert code == 2
    assert api.calls == []


def test_import_missing_file_is_a_usage_error(api, capsys, tmp_path):
    code, out, err = run(capsys, "case", "import", str(tmp_path / "missing.json"), "--username", "alice")

    assert code == 2
    assert api.calls == []
    assert "cannot read" in err


def test_import_file_over_the_limit_is_refused_before_any_request(api, capsys, tmp_path):
    path = write_file(tmp_path, b" " * (cli.MAX_IMPORT_FILE_BYTES + 1))

    code, out, err = run(capsys, "case", "import", path, "--username", "alice")

    assert code == 2
    assert api.calls == []
    assert "larger than" in err


# --- case update commands ---

USER_ID = "66666666-7777-4888-8999-aaaaaaaaaaaa"
DUTY_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
CASE_PATH = f"/api/v1/cases/{CASE_ID}"
DUTY_PATH = f"{CASE_PATH}/legal-duties/{DUTY_ID}"
CASE_UPDATED_AT = "2026-10-31T11:00:00Z"
REVIEW_BODY = {
    "id": "cccccccc-dddd-4eee-8fff-000000000001",
    "case_id": CASE_ID,
    "outcome": "pass",
    "note": "Checked",
    "created_at": CASE_UPDATED_AT,
}
ASSIGNMENT_BODY = {
    "id": "cccccccc-dddd-4eee-8fff-000000000002",
    "case_id": CASE_ID,
    "user_id": USER_ID,
    "username": "bob",
    "role": "investigator",
    "assigned_by": "alice",
    "assigned_at": CASE_UPDATED_AT,
}
IMPACT_BODY = {
    "area": "severity",
    "assessed": True,
    "severity": "high",
    "scope": "Customer records",
    "updated_at": CASE_UPDATED_AT,
}
DUTY_BODY = {
    "id": DUTY_ID,
    "case_id": CASE_ID,
    "kind": "statutory",
    "description": "Notify the regulator",
    "source_reference": "Art. 33",
    "jurisdiction": "EU",
    "due_at": "2026-11-02T09:00:00Z",
    "status": "open",
    "created_by": "alice",
    "created_at": CASE_UPDATED_AT,
}
AWARENESS_BODY = {
    "id": "cccccccc-dddd-4eee-8fff-000000000003",
    "case_id": CASE_ID,
    "awareness_kind": "operator",
    "event_kind": "incident",
    "occurred_at": "2026-10-30T09:00:00Z",
    "basis": "Log review",
    "recorded_by": "alice",
    "created_at": CASE_UPDATED_AT,
}


def classification_body(version: int = 2) -> dict:
    return {
        "case_id": CASE_ID,
        "version": version,
        "triggers": ["boundary_escape"],
        "event_kind": "incident",
        "internal_incident_option": False,
        "classification_rationale": None,
        "responsible_operator": None,
        "affected_versions": None,
        "approved_scope": None,
        "scope_permission_source": None,
        "containment": None,
        "evidence_gap_plan": None,
        "next_update_due_at": None,
        "created_by": "alice",
        "created_at": CASE_UPDATED_AT,
    }


def empty_classification_payload(expected_version: int) -> dict:
    """A save with no optional field: every field is sent, and the omitted ones are empty or null."""
    return {
        "expected_version": expected_version,
        "triggers": [],
        "event_kind": None,
        "internal_incident_option": False,
        "classification_rationale": None,
        "responsible_operator": None,
        "affected_versions": None,
        "approved_scope": None,
        "scope_permission_source": None,
        "containment": None,
        "evidence_gap_plan": None,
        "next_update_due_at": None,
    }


# argv, method, path, payload sent, success status, success body, text shown on success, text for a 409
COMMAND_CASES = [
    pytest.param(
        ("case", "close", CASE_ID, "--reason", "Resolved"),
        "POST", f"{CASE_PATH}/close", {"reason": "Resolved"}, 200, CASE_BODY,
        "Case closed.", "cannot be closed now", id="close",
    ),
    pytest.param(
        ("case", "reopen", CASE_ID, "--reason", "Reported again"),
        "POST", f"{CASE_PATH}/reopen", {"reason": "Reported again"}, 200, CASE_BODY,
        "Case reopened.", "not closed", id="reopen",
    ),
    pytest.param(
        ("case", "assign", CASE_ID, "--user-id", USER_ID),
        "POST", f"{CASE_PATH}/assignments", {"user_id": USER_ID}, 201, ASSIGNMENT_BODY,
        "Assignment added.", "already assigned", id="assign",
    ),
    pytest.param(
        ("case", "unassign", CASE_ID, "--user-id", USER_ID),
        "DELETE", f"{CASE_PATH}/assignments/{USER_ID}", None, 204, None,
        "Assignment removed.", "cannot change", id="unassign",
    ),
    pytest.param(
        ("case", "review", "record", CASE_ID, "--outcome", "pass", "--note", "Checked"),
        "POST", f"{CASE_PATH}/reviews", {"outcome": "pass", "note": "Checked"}, 201, REVIEW_BODY,
        "Review recorded.", "no review can be recorded", id="review-record",
    ),
    pytest.param(
        ("case", "classification", "set", CASE_ID, "--expected-version", "1"),
        "PUT", f"{CASE_PATH}/classification", empty_classification_payload(1), 200, classification_body(),
        "Classification saved as version 2.", "cannot change", id="classification-set",
    ),
    pytest.param(
        ("case", "impact", "set", CASE_ID, "severity", "--severity", "high", "--scope", "Customer records"),
        "PUT", f"{CASE_PATH}/impacts/severity", {"severity": "high", "scope": "Customer records"}, 200, IMPACT_BODY,
        "Impact area severity saved.", "impact assessment cannot change", id="impact-set",
    ),
    pytest.param(
        (
            "case", "legal-duty", "add", CASE_ID,
            "--kind", "statutory", "--description", "Notify the regulator", "--source-reference", "Art. 33",
            "--jurisdiction", "EU", "--due-at", "2026-11-02T09:00:00Z",
        ),
        "POST", f"{CASE_PATH}/legal-duties",
        {
            "kind": "statutory",
            "description": "Notify the regulator",
            "source_reference": "Art. 33",
            "jurisdiction": "EU",
            "due_at": "2026-11-02T09:00:00+00:00",
        },
        201, DUTY_BODY,
        "Legal duty added.", "legal duties cannot change", id="legal-duty-add",
    ),
    pytest.param(
        ("case", "legal-duty", "update", CASE_ID, DUTY_ID, "--status", "done"),
        "PATCH", DUTY_PATH, {"status": "done"}, 200, DUTY_BODY,
        "Legal duty updated.", "legal duties cannot change", id="legal-duty-update",
    ),
    pytest.param(
        (
            "case", "awareness", "add", CASE_ID,
            "--awareness-kind", "operator", "--event-kind", "incident",
            "--occurred-at", "2026-10-30T09:00:00Z", "--basis", "Log review",
        ),
        "POST", f"{CASE_PATH}/awareness-events",
        {
            "awareness_kind": "operator",
            "event_kind": "incident",
            "occurred_at": "2026-10-30T09:00:00+00:00",
            "basis": "Log review",
        },
        201, AWARENESS_BODY,
        "Awareness event recorded.", "no awareness event", id="awareness-add",
    ),
]


@pytest.mark.parametrize("argv, method, path, payload, status, body, heading, conflict", COMMAND_CASES)
def test_case_update_sends_the_request_and_ends_the_session(
    api, capsys, argv, method, path, payload, status, body, heading, conflict
):
    api.route(method, path, status=status, body=body)
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 0, err
    assert heading in out
    assert api.paths() == [("POST", "/api/v1/auth/token"), (method, path), ("POST", "/api/v1/auth/logout")]
    assert api.calls[1]["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert api.calls[1]["json"] == payload
    assert api.calls[2]["json"] == {"refresh_token": REFRESH}
    assert err == ""


@pytest.mark.parametrize("argv, method, path, payload, status, body, heading, conflict", COMMAND_CASES)
def test_case_update_permission_denied_is_reported_and_the_session_ends(
    api, capsys, argv, method, path, payload, status, body, heading, conflict
):
    api.route(method, path, status=403, body={"detail": "forbidden"})
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 1
    assert out == ""
    assert "error: forbidden" in err
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


@pytest.mark.parametrize("argv, method, path, payload, status, body, heading, conflict", COMMAND_CASES)
def test_case_update_conflict_uses_the_command_message(
    api, capsys, argv, method, path, payload, status, body, heading, conflict
):
    api.route(method, path, status=409, body={"detail": "case is closed"})
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 1
    assert out == ""
    assert conflict in err
    # Never the timezone text that the create command uses for 409.
    assert "timezone" not in err
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


@pytest.mark.parametrize("argv, method, path, payload, status, body, heading, conflict", COMMAND_CASES)
def test_case_update_password_change_required_stops_before_the_write(
    api, capsys, argv, method, path, payload, status, body, heading, conflict
):
    api.route("POST", "/api/v1/auth/token", body=token_body(password_change_required=True))
    api.route(method, path, status=status, body=body)
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 1
    assert "change your password" in err
    assert (method, path) not in api.paths()
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


@pytest.mark.parametrize(
    "argv, method, path, payload, status, body, heading, conflict",
    [case for case in COMMAND_CASES if case.id in ("close", "review-record", "classification-set")],
)
def test_case_update_secrets_never_reach_the_output(
    api, capsys, argv, method, path, payload, status, body, heading, conflict
):
    api.route(method, path, status=status, body=body)
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 0
    for secret in (ACCESS, REFRESH, PASSWORD):
        assert secret not in out
        assert secret not in err


@pytest.mark.parametrize(
    "argv, method, path, body",
    [
        (("case", "close", CASE_ID, "--reason", "Resolved", "--json"), "POST", f"{CASE_PATH}/close", CASE_BODY),
        (("case", "review", "record", CASE_ID, "--outcome", "pass", "--json"), "POST", f"{CASE_PATH}/reviews",
         REVIEW_BODY),
        (("case", "impact", "set", CASE_ID, "scale", "--severity", "low", "--scope", "x", "--json"), "PUT",
         f"{CASE_PATH}/impacts/scale", IMPACT_BODY),
        (("case", "legal-duty", "add", CASE_ID, "--kind", "contractual", "--description", "d",
          "--source-reference", "s", "--due-at", "2026-11-02T09:00:00Z", "--json"), "POST",
         f"{CASE_PATH}/legal-duties", DUTY_BODY),
    ],
)
def test_case_update_json_prints_the_api_body_exactly(api, capsys, argv, method, path, body):
    api.route(method, path, status=201, body=body)
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 0
    assert out == compact(body).decode("utf-8") + "\n"
    assert err == ""


def test_unassign_sends_a_delete_with_no_body(api, capsys):
    api.route("DELETE", f"{CASE_PATH}/assignments/{USER_ID}", status=204)
    code, out, err = run(capsys, "case", "unassign", CASE_ID, "--user-id", USER_ID, "--username", "alice")
    assert code == 0
    assert api.calls[1]["method"] == "DELETE"
    assert api.calls[1]["json"] is None


def test_unassign_has_no_json_flag(api, capsys):
    code, out, err = run(capsys, "case", "unassign", CASE_ID, "--user-id", USER_ID, "--username", "alice", "--json")
    assert code == 2
    assert api.calls == []


def test_close_sends_the_reason_as_given(api, capsys):
    api.route("POST", f"{CASE_PATH}/close", body=CASE_BODY)
    code, out, err = run(capsys, "case", "close", CASE_ID, "--reason", "  Resolved  ", "--username", "alice")
    assert code == 0
    # Trimming and the rules for blank reasons are the API's job; the CLI sends the text as typed.
    assert api.calls[1]["json"] == {"reason": "  Resolved  "}


@pytest.mark.parametrize(
    "argv",
    [
        ("case", "close", CASE_ID),
        ("case", "reopen", CASE_ID),
        ("case", "assign", CASE_ID),
        ("case", "review", "record", CASE_ID, "--outcome", "maybe"),
        ("case", "impact", "set", CASE_ID, "votes", "--severity", "low", "--scope", "x"),
        ("case", "awareness", "add", CASE_ID, "--awareness-kind", "operator", "--event-kind", "incident"),
        ("case", "legal-duty", "add", CASE_ID, "--kind", "statutory"),
        ("case", "classification", "set", CASE_ID),
    ],
)
def test_missing_or_invalid_arguments_are_usage_errors_without_a_request(api, capsys, argv):
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 2
    assert api.calls == []


@pytest.mark.parametrize("option", ["assign", "unassign"])
def test_user_id_must_be_a_uuid_before_any_request(api, capsys, option):
    code, out, err = run(capsys, "case", option, CASE_ID, "--user-id", "bob", "--username", "alice")
    assert code == 2
    assert api.calls == []
    assert "--user-id must be a UUID" in err


def test_duty_id_must_be_a_uuid_before_any_request(api, capsys):
    code, out, err = run(
        capsys, "case", "legal-duty", "update", CASE_ID, "not-a-duty", "--status", "done", "--username", "alice"
    )
    assert code == 2
    assert api.calls == []
    assert "DUTY_ID must be a UUID" in err


def test_legal_duty_update_without_a_field_is_a_usage_error(api, capsys):
    code, out, err = run(capsys, "case", "legal-duty", "update", CASE_ID, DUTY_ID, "--username", "alice")
    assert code == 2
    assert api.calls == []
    assert "give at least one field" in err


def test_legal_duty_update_sends_only_the_fields_given(api, capsys):
    api.route("PATCH", DUTY_PATH, body=DUTY_BODY)
    code, out, err = run(
        capsys, "case", "legal-duty", "update", CASE_ID, DUTY_ID,
        "--due-at", "2026-11-05T12:00:00+03:00", "--jurisdiction", "", "--username", "alice",
    )
    assert code == 0
    # An empty jurisdiction is sent as an empty string, which the API reads as a clear.
    assert api.calls[1]["json"] == {"due_at": "2026-11-05T12:00:00+03:00", "jurisdiction": ""}


@pytest.mark.parametrize("option, value", [("--occurred-at", "2026-10-30T09:00:00"), ("--due-at", "2026-11-02T09:00:00")])
def test_timestamps_without_a_timezone_are_usage_errors(api, capsys, option, value):
    argv = ["case", "legal-duty", "add", CASE_ID, "--kind", "statutory", "--description", "d",
            "--source-reference", "s", "--due-at", value]
    if option == "--occurred-at":
        argv = ["case", "awareness", "add", CASE_ID, "--awareness-kind", "operator", "--event-kind", "incident",
                "--occurred-at", value, "--basis", "b"]
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 2
    assert api.calls == []
    assert "must include a timezone" in err


def test_awareness_occurred_at_in_the_future_is_refused_before_any_request(api, capsys):
    code, out, err = run(
        capsys, "case", "awareness", "add", CASE_ID, "--awareness-kind", "operator", "--event-kind", "incident",
        "--occurred-at", future_time(), "--basis", "b", "--username", "alice",
    )
    assert code == 2
    assert api.calls == []
    assert "--occurred-at cannot be in the future" in err


def test_classification_set_sends_every_field(api, capsys):
    api.route("PUT", f"{CASE_PATH}/classification", body=classification_body(version=4))
    code, out, err = run(
        capsys, "case", "classification", "set", CASE_ID,
        "--expected-version", "3",
        "--trigger", "boundary_escape", "--trigger", "confidential_data_exposure",
        "--event-kind", "near_miss",
        "--internal-incident-option", "--classification-rationale", "Within the plan",
        "--responsible-operator", "Ops", "--affected-versions", "1.2",
        "--approved-scope", "Scope A", "--scope-permission-source", "Contract 7",
        "--containment", "Keys rotated", "--evidence-gap-plan", "Pull logs",
        "--next-update-due-at", "2026-11-02T09:00:00Z",
        "--username", "alice",
    )
    assert code == 0, err
    assert "Classification saved as version 4." in out
    assert api.calls[1]["json"] == {
        "expected_version": 3,
        "triggers": ["boundary_escape", "confidential_data_exposure"],
        "event_kind": "near_miss",
        "internal_incident_option": True,
        "classification_rationale": "Within the plan",
        "responsible_operator": "Ops",
        "affected_versions": "1.2",
        "approved_scope": "Scope A",
        "scope_permission_source": "Contract 7",
        "containment": "Keys rotated",
        "evidence_gap_plan": "Pull logs",
        "next_update_due_at": "2026-11-02T09:00:00+00:00",
    }


def test_classification_set_clears_every_field_that_is_not_given(api, capsys):
    api.route("PUT", f"{CASE_PATH}/classification", body=classification_body(version=1))
    code, out, err = run(capsys, "case", "classification", "set", CASE_ID, "--expected-version", "0", "--username", "alice")
    assert code == 0
    assert api.calls[1]["json"] == empty_classification_payload(0)


def test_classification_set_requires_expected_version(api, capsys):
    code, out, err = run(capsys, "case", "classification", "set", CASE_ID, "--username", "alice")
    assert code == 2
    assert api.calls == []
    assert "--expected-version" in err


def test_classification_set_refuses_a_negative_expected_version(api, capsys):
    code, out, err = run(
        capsys, "case", "classification", "set", CASE_ID, "--expected-version", "-1", "--username", "alice"
    )
    assert code == 2
    assert api.calls == []


def test_classification_set_rejects_an_unknown_trigger(api, capsys):
    code, out, err = run(
        capsys, "case", "classification", "set", CASE_ID, "--expected-version", "0",
        "--trigger", "not_a_trigger", "--username", "alice",
    )
    assert code == 2
    assert api.calls == []


def test_classification_stale_save_prints_the_current_version_and_does_not_retry(api, capsys):
    api.route(
        "PUT",
        f"{CASE_PATH}/classification",
        status=409,
        body={"detail": {"status": "classification_version_conflict", "current_version": 3}},
    )
    code, out, err = run(
        capsys, "case", "classification", "set", CASE_ID, "--expected-version", "2", "--username", "alice"
    )
    assert code == 1
    assert out == ""
    assert "The current version is 3" in err
    assert "--expected-version 3" in err
    assert "Re-read" in err
    # One save only: the command never retries on its own.
    assert api.paths().count(("PUT", f"{CASE_PATH}/classification")) == 1
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


def test_classification_closed_case_conflict_uses_the_closed_message(api, capsys):
    api.route("PUT", f"{CASE_PATH}/classification", status=409, body={"detail": "case is closed"})
    code, out, err = run(
        capsys, "case", "classification", "set", CASE_ID, "--expected-version", "2", "--username", "alice"
    )
    assert code == 1
    assert "the case is closed, so the classification cannot change" in err
    assert "current version" not in err


def test_classification_permission_denied_names_the_full_view_requirement(api, capsys):
    api.route("PUT", f"{CASE_PATH}/classification", status=403, body={"detail": "classification_requires_full_view"})
    code, out, err = run(
        capsys, "case", "classification", "set", CASE_ID, "--expected-version", "2", "--username", "alice"
    )
    assert code == 1
    assert "cases:read_all" in err


def test_review_record_permission_denied_names_the_assignment_requirement(api, capsys):
    api.route("POST", f"{CASE_PATH}/reviews", status=403, body={"detail": "only an assigned reviewer can record a review"})
    code, out, err = run(capsys, "case", "review", "record", CASE_ID, "--outcome", "fail", "--username", "alice")
    assert code == 1
    assert "assigned to the case" in err


# --- case links -----------------------------------------------------------------------------------------------------

LINKS_PATH = f"{CASE_PATH}/links"
LINKED_ID = "dddddddd-eeee-4fff-8000-111111111111"
LINK_BODY = {"linked_case_id": LINKED_ID, "linked_case_title": "Related case", "created_at": "2026-11-01T09:00:00Z"}
HIDDEN_LINK_BODY = {"linked_case_id": None, "linked_case_title": None, "created_at": "2026-11-01T09:00:00Z"}


def test_link_add_sends_the_linked_case_id_and_says_nothing_else_changes(api, capsys):
    api.route("POST", LINKS_PATH, status=201, body=LINK_BODY)
    code, out, err = run(capsys, "case", "link", "add", CASE_ID, LINKED_ID, "--username", "alice")
    assert code == 0
    assert api.calls[1]["method"] == "POST"
    assert api.calls[1]["json"] == {"linked_case_id": LINKED_ID}
    assert out.startswith("Cases linked. Linking changes no task, deadline or status.")
    assert LINKED_ID in out and "Related case" in out
    assert err == ""


def test_link_add_json_prints_the_api_body_exactly(api, capsys):
    api.route("POST", LINKS_PATH, status=201, body=LINK_BODY)
    code, out, err = run(capsys, "case", "link", "add", CASE_ID, LINKED_ID, "--username", "alice", "--json")
    assert code == 0
    assert out == compact(LINK_BODY).decode("utf-8") + "\n"


@pytest.mark.parametrize(
    ("status", "detail", "message"),
    [
        pytest.param(404, {"detail": "case not found"}, "case not found, or the other case", id="not-found"),
        pytest.param(409, {"detail": "cases are already linked"}, "already linked", id="duplicate-or-closed"),
        pytest.param(422, {"detail": "a case cannot be linked to itself"}, "cannot be linked to itself", id="same-case"),
    ],
)
def test_link_add_refusals_are_reported_as_messages(api, capsys, status, detail, message):
    api.route("POST", LINKS_PATH, status=status, body=detail)
    code, out, err = run(capsys, "case", "link", "add", CASE_ID, LINKED_ID, "--username", "alice")
    assert code == 1
    assert message in err


def test_link_add_rejects_a_linked_case_id_that_is_not_a_uuid_without_a_request(api, capsys):
    code, out, err = run(capsys, "case", "link", "add", CASE_ID, "not-a-uuid", "--username", "alice")
    assert code == 2
    assert "LINKED_CASE_ID must be a UUID" in err
    assert api.calls == []


def test_link_list_prints_the_links_and_marks_a_hidden_case(api, capsys):
    api.route("GET", LINKS_PATH, body=[LINK_BODY, HIDDEN_LINK_BODY])
    code, out, err = run(capsys, "case", "link", "list", CASE_ID, "--username", "alice")
    assert code == 0
    assert api.calls[1]["method"] == "GET"
    assert LINKED_ID in out and "Related case" in out
    assert "(not available to your account)" in out


def test_link_list_empty_says_so(api, capsys):
    api.route("GET", LINKS_PATH, body=[])
    code, out, err = run(capsys, "case", "link", "list", CASE_ID, "--username", "alice")
    assert code == 0
    assert out == "No linked cases.\n"


def test_link_list_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", LINKS_PATH, body=[LINK_BODY])
    code, out, err = run(capsys, "case", "link", "list", CASE_ID, "--username", "alice", "--json")
    assert code == 0
    assert out == compact([LINK_BODY]).decode("utf-8") + "\n"


def test_link_remove_sends_a_delete_and_reports_the_removal(api, capsys):
    api.route("DELETE", f"{LINKS_PATH}/{LINKED_ID}", status=204)
    code, out, err = run(capsys, "case", "link", "remove", CASE_ID, LINKED_ID, "--username", "alice")
    assert code == 0
    assert api.calls[1]["method"] == "DELETE"
    assert api.calls[1]["json"] is None
    assert out.startswith("Link removed.")


def test_link_remove_not_found_is_reported(api, capsys):
    api.route("DELETE", f"{LINKS_PATH}/{LINKED_ID}", status=404, body={"detail": "link not found"})
    code, out, err = run(capsys, "case", "link", "remove", CASE_ID, LINKED_ID, "--username", "alice")
    assert code == 1
    assert "no link between the two cases" in err


def test_link_remove_has_no_json_flag(api, capsys):
    code, out, err = run(capsys, "case", "link", "remove", CASE_ID, LINKED_ID, "--username", "alice", "--json")
    assert code == 2
    assert api.calls == []
