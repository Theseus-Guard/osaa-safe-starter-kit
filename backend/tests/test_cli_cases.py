"""Case subcommands of the container CLI. The API is replaced by a fake behind urllib.request.urlopen."""

import getpass
import io
import json
import re
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
    "revision": 3,
    "deadlines": [
        {
            "kind": "initial_notice",
            "due_at": "2026-11-01T09:00:00Z",
            "status": "pending",
        }
    ],
    "risk_score": None,
    "impact_c": None,
    "impact_b": None,
    "impact_a": None,
}
SUMMARY = {
    "id": CASE_ID,
    "reference": "SAFE-2026-000001",
    "title": "Phishing campaign",
    "awareness_time": "2026-10-30T09:00:00Z",
    "status": "open",
    "created_at": "2026-10-31T10:00:00Z",
    "next_deadline": "2026-11-02T09:00:00Z",
    "updated_at": "2026-11-01T08:15:00Z",
    "risk_score": None,
    "impact_c": None,
    "impact_b": None,
    "impact_a": None,
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

    def route(
        self,
        method: str,
        path: str,
        status: int = 200,
        body=None,
        error: Exception | None = None,
    ):
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
            raise urllib.error.HTTPError(
                request.full_url, status, "error", {}, io.BytesIO(body)
            )
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


CREATE = (
    "case",
    "create",
    "--title",
    "Phishing campaign",
    "--awareness-time",
    "2026-10-30T09:00:00Z",
)


# --- create ---


def test_create_with_naive_awareness_time_is_a_usage_error(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "create",
        "--title",
        "Phishing",
        "--awareness-time",
        "2026-10-30T09:00:00",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []
    assert "timezone" in err


def test_create_with_future_awareness_time_is_rejected_before_any_request(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "create",
        "--title",
        "Phishing",
        "--awareness-time",
        future_time(),
        "--username",
        "alice",
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
    assert api.calls[1]["json"] == {
        "title": "Phishing campaign",
        "awareness_time": "2026-10-30T09:00:00+00:00",
    }
    assert api.calls[2]["json"] == {"refresh_token": REFRESH}


def test_create_json_prints_the_api_body_exactly(api, capsys):
    api.route("POST", "/api/v1/cases", status=201, body=CASE_BODY)
    code, out, err = run(capsys, *CREATE, "--username", "alice", "--json")
    assert code == 0
    assert out == compact(CASE_BODY).decode("utf-8") + "\n"
    assert json.loads(out) == CASE_BODY
    assert err == ""


def test_create_maps_missing_timezone_to_a_clear_message(api, capsys):
    api.route(
        "POST",
        "/api/v1/cases",
        status=409,
        body={"detail": "system timezone is not configured"},
    )
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
        capsys,
        "case",
        "create",
        "--title",
        "   ",
        "--awareness-time",
        "2026-10-30T09:00:00Z",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []


# --- list ---


def test_list_sends_the_paging_arguments(api, capsys):
    page = {"items": [SUMMARY], "next_cursor": None}
    api.route("GET", "/api/v1/cases", body=page)
    code, out, err = run(
        capsys,
        "case",
        "list",
        "--limit",
        "10",
        "--cursor",
        "abc-123",
        "--username",
        "alice",
    )
    assert code == 0
    get = api.calls[1]
    assert get["query"] == {"limit": ["10"], "cursor": ["abc-123"]}
    assert get["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert "SAFE-2026-000001" in out


def test_list_uses_the_web_page_size_by_default_and_no_cursor(api, capsys):
    api.route("GET", "/api/v1/cases", body={"items": [], "next_cursor": None})
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 0
    assert api.calls[1]["query"] == {"limit": ["20"]}
    assert out == "No cases to show.\n"


def test_list_json_prints_the_api_body_exactly(api, capsys):
    page = {"items": [SUMMARY], "next_cursor": None}
    api.route("GET", "/api/v1/cases", body=page)
    code, out, err = run(capsys, "case", "list", "--username", "alice", "--json")
    assert code == 0
    assert out == compact(page).decode("utf-8") + "\n"


@pytest.mark.parametrize("argv", [("--limit", "0"), ("--limit", "201")])
def test_list_rejects_out_of_range_paging_without_a_request(api, capsys, argv):
    code, out, err = run(capsys, "case", "list", "--username", "alice", *argv)
    assert code == 2
    assert api.calls == []


def test_list_no_longer_takes_an_offset(api, capsys):
    code, out, err = run(
        capsys, "case", "list", "--offset", "20", "--username", "alice"
    )
    assert code == 2
    assert api.calls == []


def test_list_prints_the_next_page_cursor(api, capsys):
    page = {"items": [SUMMARY], "next_cursor": "eyJyZWYiOiJTQUZFLTIwMjYtMDAwMDAxIn0"}
    api.route("GET", "/api/v1/cases", body=page)
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 0
    assert (
        out.splitlines()[-1]
        == "Next page: --cursor eyJyZWYiOiJTQUZFLTIwMjYtMDAwMDAxIn0"
    )


def test_list_reports_an_invalid_cursor_with_a_clear_message(api, capsys):
    api.route(
        "GET",
        "/api/v1/cases",
        status=400,
        body={"detail": {"status": "invalid_cursor"}},
    )
    code, out, err = run(
        capsys, "case", "list", "--cursor", "not-a-cursor", "--username", "alice"
    )
    assert code == 1
    assert out == ""
    assert "error: the cursor is not valid" in err
    assert "without --cursor" in err


@pytest.mark.parametrize("status", cli.CASE_STATUS_CHOICES)
def test_list_sends_the_status_filter(api, capsys, status):
    api.route("GET", "/api/v1/cases", body={"items": [SUMMARY], "next_cursor": None})
    code, out, err = run(
        capsys, "case", "list", "--status", status, "--username", "alice"
    )
    assert code == 0
    assert api.calls[1]["query"] == {"limit": ["20"], "status": [status]}


def test_list_without_a_status_sends_no_status_parameter(api, capsys):
    api.route("GET", "/api/v1/cases", body={"items": [], "next_cursor": None})
    run(capsys, "case", "list", "--username", "alice")
    assert "status" not in api.calls[1]["query"]


def test_list_refuses_an_unknown_status_without_a_request(api, capsys):
    code, out, err = run(
        capsys, "case", "list", "--status", "archived", "--username", "alice"
    )
    assert code == 2
    assert api.calls == []
    assert "archived" in err


def test_status_choices_are_the_case_statuses_of_the_backend():
    from safe_kit import cases

    assert cli.CASE_STATUS_CHOICES == cases.CASE_STATUSES


def test_list_prints_the_next_deadline_and_the_last_update(api, capsys):
    hidden = {
        **SUMMARY,
        "reference": "SAFE-2026-000002",
        "next_deadline": None,
        "updated_at": "2026-11-02T07:00:00Z",
    }
    page = {"items": [SUMMARY, hidden], "next_cursor": None}
    api.route("GET", "/api/v1/cases", body=page)
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 0
    header, first, second = out.splitlines()
    # Cells are separated by at least two spaces. The title is last, so it may contain single spaces.
    assert re.split(r"\s{2,}", header.strip()) == [
        "REFERENCE",
        "STATUS",
        "AWARENESS (UTC)",
        "NEXT DEADLINE (UTC)",
        "LAST UPDATED (UTC)",
        "RISK",
        "TITLE",
    ]
    assert re.split(r"\s{2,}", first.strip()) == [
        "SAFE-2026-000001",
        "open",
        "2026-10-30T09:00:00Z",
        "2026-11-02T09:00:00Z",
        "2026-11-01T08:15:00Z",
        "Not assessed",
        "Phishing campaign",
    ]
    assert re.split(r"\s{2,}", second.strip()) == [
        "SAFE-2026-000002",
        "open",
        "2026-10-30T09:00:00Z",
        "None",
        "2026-11-02T07:00:00Z",
        "Not assessed",
        "Phishing campaign",
    ]
    assert "Showing" not in out and "Total" not in out


# --- show ---


def test_show_success_prints_the_case(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}", body=CASE_BODY)
    code, out, err = run(capsys, "case", "show", CASE_ID, "--username", "alice")
    assert code == 0
    assert "Case SAFE-2026-000001" in out
    assert "Awareness (UTC):  2026-10-30T09:00:00Z" in out


def test_show_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}", body=CASE_BODY)
    code, out, err = run(
        capsys, "case", "show", CASE_ID, "--username", "alice", "--json"
    )
    assert code == 0
    assert out == compact(CASE_BODY).decode("utf-8") + "\n"


def test_show_not_found_reports_case_not_found(api, capsys):
    api.route(
        "GET", f"/api/v1/cases/{CASE_ID}", status=404, body={"detail": "case not found"}
    )
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
    api.route(
        "POST", "/api/v1/auth/token", status=401, body={"detail": "invalid credentials"}
    )
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 1
    assert out == ""
    assert "error: invalid credentials" in err
    # Nothing was issued, so there is nothing to log out.
    assert api.paths() == [("POST", "/api/v1/auth/token")]


def test_password_change_required_stops_with_a_web_ui_message(api, capsys):
    api.route(
        "POST", "/api/v1/auth/token", body=token_body(password_change_required=True)
    )
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
    api.route("GET", "/api/v1/cases", body={"items": [], "next_cursor": None})
    api.route("POST", "/api/v1/auth/logout", status=500, body={"detail": "boom"})
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 0


def test_unreachable_api_reports_that_the_server_could_not_be_reached(api, capsys):
    api.route(
        "POST", "/api/v1/auth/token", error=urllib.error.URLError("connection refused")
    )
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 1
    assert "could not be reached" in err


def test_username_comes_from_the_environment(api, capsys, monkeypatch):
    monkeypatch.setenv("SAFE_USERNAME", "bob")
    api.route("GET", "/api/v1/cases", body={"items": [], "next_cursor": None})
    code, out, err = run(capsys, "case", "list")
    assert code == 0
    assert api.calls[0]["json"]["username"] == "bob"


def test_missing_username_is_a_usage_error_without_a_request(api, capsys):
    code, out, err = run(capsys, "case", "list")
    assert code == 2
    assert api.calls == []
    assert "SAFE_USERNAME" in err


def test_password_is_read_from_stdin_when_stdin_is_not_a_terminal(api, capsys):
    api.route("GET", "/api/v1/cases", body={"items": [], "next_cursor": None})
    code, out, err = run(capsys, "case", "list", "--username", "alice")
    assert code == 0
    assert api.calls[0]["json"] == {"username": "alice", "password": PASSWORD}


def test_empty_password_on_stdin_is_a_usage_error_without_a_request(
    api, capsys, monkeypatch
):
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
    api.route("GET", "/api/v1/cases", body={"items": [], "next_cursor": None})
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
    api.route("GET", "/api/v1/cases", body={"items": [SUMMARY], "next_cursor": None})
    api.route("GET", f"/api/v1/cases/{CASE_ID}", body=CASE_BODY)
    code, out, err = run(capsys, *argv)
    assert code == 0
    for secret in (ACCESS, REFRESH, PASSWORD):
        assert secret not in out
        assert secret not in err


# --- import ---

IMPORT_PATH = "/api/v1/cases/import"
IMPORT_RECORD = {
    "case_id": "3f2b8c1e-6a4d-4e9b-8f1a-2c7d9e0b5a11",
    "record_schema_version": "0.1.0",
}
IMPORT_FILE = {"import_format_version": 1, "records": [IMPORT_RECORD]}


def import_summary(**counts) -> dict:
    base = {
        "imported": 0,
        "duplicate": 0,
        "conflict": 0,
        "rejected": 0,
        "failed": 0,
        "results": [],
    }
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
            {
                "index": 0,
                "status": "imported",
                "source_case_id": None,
                "case_id": CASE_ID,
                "reference": "SAFE-2026-000001",
                "errors": [],
            },
            {
                "index": 1,
                "status": "duplicate",
                "source_case_id": None,
                "case_id": CASE_ID,
                "reference": "SAFE-2026-000002",
                "errors": [],
            },
        ],
    )
    api.route("POST", IMPORT_PATH, body=summary)
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(capsys, "case", "import", path, "--username", "alice")

    assert code == 0
    assert (
        "Import finished: 1 imported, 1 duplicate, 0 conflict, 0 rejected, 0 failed."
        in out
    )
    assert "records[0]: imported as SAFE-2026-000001" in out
    assert "records[1]: duplicate of SAFE-2026-000002, nothing written" in out
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("POST", IMPORT_PATH),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert api.calls[1]["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert api.calls[1]["json"] == IMPORT_FILE
    assert err == ""


def test_import_json_prints_the_api_body_exactly(api, capsys, tmp_path):
    summary = import_summary(
        imported=1,
        results=[
            {
                "index": 0,
                "status": "imported",
                "source_case_id": None,
                "case_id": CASE_ID,
                "reference": "SAFE-2026-000001",
                "errors": [],
            }
        ],
    )
    api.route("POST", IMPORT_PATH, body=summary)
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(
        capsys, "case", "import", path, "--username", "alice", "--json"
    )

    assert code == 0
    assert out == compact(summary).decode("utf-8") + "\n"


def test_import_exits_1_and_names_the_rejected_record(api, capsys, tmp_path):
    summary = import_summary(
        imported=1,
        rejected=1,
        results=[
            {
                "index": 0,
                "status": "imported",
                "source_case_id": None,
                "case_id": CASE_ID,
                "reference": "SAFE-2026-000001",
                "errors": [],
            },
            {
                "index": 1,
                "status": "rejected",
                "source_case_id": None,
                "case_id": None,
                "reference": None,
                "errors": ["/record_schema_version: '0.2.0' was expected"],
            },
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


def test_import_with_invalid_json_is_a_usage_error_without_a_request(
    api, capsys, tmp_path
):
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
    code, out, err = run(
        capsys, "case", "import", str(tmp_path / "missing.json"), "--username", "alice"
    )

    assert code == 2
    assert api.calls == []
    assert "cannot read" in err


def test_import_file_over_the_limit_is_refused_before_any_request(
    api, capsys, tmp_path
):
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
ROUND_ID = "cccccccc-dddd-4eee-8fff-000000000001"
EVIDENCE_ID = "dddddddd-eeee-4fff-8000-000000000001"
REVIEW_LAYER_PATH = f"/api/v1/reviews/{ROUND_ID}/layers/model"
# The fake API routes by path; the case_id query is sent with the request but is not part of the route key.
REVIEW_LIST_PATH = "/api/v1/reviews"
REVIEW_LAYER_NAMES = (
    "model",
    "instructions",
    "safeguards",
    "tools",
    "environment",
    "monitoring",
    "human-operations",
    "supply-chain",
    "serving-path",
    "authorization-and-commitment",
    "workflow-composition",
)


def review_round_body(
    revision: int = 2,
    reason: str | None = "Checked",
    status: str = "open",
    model_set: bool = True,
) -> dict:
    """A round as the API returns it. The model layer is set to pass unless model_set is False; the others are not set."""
    layers = [
        {
            "layer": name,
            "outcome": None,
            "reason": None,
            "evidence_ids": [],
            "set_at": None,
        }
        for name in REVIEW_LAYER_NAMES
    ]
    if model_set:
        layers[0] = {
            "layer": "model",
            "outcome": "pass",
            "reason": reason,
            "evidence_ids": [],
            "set_at": CASE_UPDATED_AT,
        }
    return {
        "id": ROUND_ID,
        "case_id": CASE_ID,
        "status": status,
        "revision": revision,
        "opened_at": CASE_UPDATED_AT,
        "closed_at": None,
        "layers": layers,
    }


REVIEW_BODY = review_round_body()
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
    "revision": 3,
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
        "POST",
        f"{CASE_PATH}/close",
        {"reason": "Resolved"},
        200,
        CASE_BODY,
        "Case closed.",
        "cannot be closed now",
        id="close",
    ),
    pytest.param(
        ("case", "reopen", CASE_ID, "--reason", "Reported again"),
        "POST",
        f"{CASE_PATH}/reopen",
        {"reason": "Reported again"},
        200,
        CASE_BODY,
        "Case reopened.",
        "not closed",
        id="reopen",
    ),
    pytest.param(
        ("case", "assign", CASE_ID, "--user-id", USER_ID),
        "POST",
        f"{CASE_PATH}/assignments",
        {"user_id": USER_ID},
        201,
        ASSIGNMENT_BODY,
        "Assignment added.",
        "already assigned",
        id="assign",
    ),
    pytest.param(
        ("case", "unassign", CASE_ID, "--user-id", USER_ID),
        "DELETE",
        f"{CASE_PATH}/assignments/{USER_ID}",
        None,
        204,
        None,
        "Assignment removed.",
        "cannot change",
        id="unassign",
    ),
    pytest.param(
        (
            "case",
            "review",
            "record",
            CASE_ID,
            "--layer",
            "model",
            "--outcome",
            "pass",
            "--reason",
            "Checked",
            "--evidence",
            EVIDENCE_ID,
            "--round",
            ROUND_ID,
            "--revision",
            "1",
        ),
        "PATCH",
        REVIEW_LAYER_PATH,
        {"outcome": "pass", "reason": "Checked", "evidence_ids": [EVIDENCE_ID]},
        200,
        REVIEW_BODY,
        "Review layer saved.",
        "is closed",
        id="review-record",
    ),
    pytest.param(
        ("case", "classification", "set", CASE_ID, "--expected-version", "1"),
        "PUT",
        f"{CASE_PATH}/classification",
        empty_classification_payload(1),
        200,
        classification_body(),
        "Classification saved as version 2.",
        "cannot change",
        id="classification-set",
    ),
    pytest.param(
        (
            "case",
            "impact",
            "set",
            CASE_ID,
            "severity",
            "--severity",
            "high",
            "--scope",
            "Customer records",
        ),
        "PUT",
        f"{CASE_PATH}/impacts/severity",
        {"severity": "high", "scope": "Customer records"},
        200,
        IMPACT_BODY,
        "Impact area severity saved.",
        "impact assessment cannot change",
        id="impact-set",
    ),
    pytest.param(
        (
            "case",
            "legal-duty",
            "add",
            CASE_ID,
            "--kind",
            "statutory",
            "--description",
            "Notify the regulator",
            "--source-reference",
            "Art. 33",
            "--jurisdiction",
            "EU",
            "--due-at",
            "2026-11-02T09:00:00Z",
        ),
        "POST",
        f"{CASE_PATH}/legal-duties",
        {
            "kind": "statutory",
            "description": "Notify the regulator",
            "source_reference": "Art. 33",
            "jurisdiction": "EU",
            "due_at": "2026-11-02T09:00:00+00:00",
        },
        201,
        DUTY_BODY,
        "Legal duty added.",
        "legal duties cannot change",
        id="legal-duty-add",
    ),
    pytest.param(
        (
            "case",
            "legal-duty",
            "update",
            CASE_ID,
            DUTY_ID,
            "--status",
            "done",
            "--revision",
            "3",
        ),
        "PATCH",
        DUTY_PATH,
        {"status": "done"},
        200,
        DUTY_BODY,
        "Legal duty updated.",
        "legal duties cannot change",
        id="legal-duty-update",
    ),
    pytest.param(
        (
            "case",
            "awareness",
            "add",
            CASE_ID,
            "--awareness-kind",
            "operator",
            "--event-kind",
            "incident",
            "--occurred-at",
            "2026-10-30T09:00:00Z",
            "--basis",
            "Log review",
        ),
        "POST",
        f"{CASE_PATH}/awareness-events",
        {
            "awareness_kind": "operator",
            "event_kind": "incident",
            "occurred_at": "2026-10-30T09:00:00+00:00",
            "basis": "Log review",
        },
        201,
        AWARENESS_BODY,
        "Awareness event recorded.",
        "no awareness event",
        id="awareness-add",
    ),
]


@pytest.mark.parametrize(
    "argv, method, path, payload, status, body, heading, conflict", COMMAND_CASES
)
def test_case_update_sends_the_request_and_ends_the_session(
    api, capsys, argv, method, path, payload, status, body, heading, conflict
):
    api.route(method, path, status=status, body=body)
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 0, err
    assert heading in out
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        (method, path),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert api.calls[1]["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert api.calls[1]["json"] == payload
    assert api.calls[2]["json"] == {"refresh_token": REFRESH}
    assert err == ""


@pytest.mark.parametrize(
    "argv, method, path, payload, status, body, heading, conflict", COMMAND_CASES
)
def test_case_update_permission_denied_is_reported_and_the_session_ends(
    api, capsys, argv, method, path, payload, status, body, heading, conflict
):
    api.route(method, path, status=403, body={"detail": "forbidden"})
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 1
    assert out == ""
    assert "error: forbidden" in err
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


@pytest.mark.parametrize(
    "argv, method, path, payload, status, body, heading, conflict", COMMAND_CASES
)
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


@pytest.mark.parametrize(
    "argv, method, path, payload, status, body, heading, conflict", COMMAND_CASES
)
def test_case_update_password_change_required_stops_before_the_write(
    api, capsys, argv, method, path, payload, status, body, heading, conflict
):
    api.route(
        "POST", "/api/v1/auth/token", body=token_body(password_change_required=True)
    )
    api.route(method, path, status=status, body=body)
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 1
    assert "change your password" in err
    assert (method, path) not in api.paths()
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


@pytest.mark.parametrize(
    "argv, method, path, payload, status, body, heading, conflict",
    [
        case
        for case in COMMAND_CASES
        if case.id in ("close", "review-record", "classification-set")
    ],
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
        (
            ("case", "close", CASE_ID, "--reason", "Resolved", "--json"),
            "POST",
            f"{CASE_PATH}/close",
            CASE_BODY,
        ),
        (
            (
                "case",
                "review",
                "record",
                CASE_ID,
                "--layer",
                "model",
                "--outcome",
                "pass",
                "--round",
                ROUND_ID,
                "--evidence",
                EVIDENCE_ID,
                "--revision",
                "1",
                "--json",
            ),
            "PATCH",
            REVIEW_LAYER_PATH,
            REVIEW_BODY,
        ),
        (
            (
                "case",
                "impact",
                "set",
                CASE_ID,
                "scale",
                "--severity",
                "low",
                "--scope",
                "x",
                "--json",
            ),
            "PUT",
            f"{CASE_PATH}/impacts/scale",
            IMPACT_BODY,
        ),
        (
            (
                "case",
                "legal-duty",
                "add",
                CASE_ID,
                "--kind",
                "contractual",
                "--description",
                "d",
                "--source-reference",
                "s",
                "--due-at",
                "2026-11-02T09:00:00Z",
                "--json",
            ),
            "POST",
            f"{CASE_PATH}/legal-duties",
            DUTY_BODY,
        ),
    ],
)
def test_case_update_json_prints_the_api_body_exactly(
    api, capsys, argv, method, path, body
):
    api.route(method, path, status=201, body=body)
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 0
    assert out == compact(body).decode("utf-8") + "\n"
    assert err == ""


def test_unassign_sends_a_delete_with_no_body(api, capsys):
    api.route("DELETE", f"{CASE_PATH}/assignments/{USER_ID}", status=204)
    code, out, err = run(
        capsys, "case", "unassign", CASE_ID, "--user-id", USER_ID, "--username", "alice"
    )
    assert code == 0
    assert api.calls[1]["method"] == "DELETE"
    assert api.calls[1]["json"] is None


def test_unassign_has_no_json_flag(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "unassign",
        CASE_ID,
        "--user-id",
        USER_ID,
        "--username",
        "alice",
        "--json",
    )
    assert code == 2
    assert api.calls == []


def test_close_sends_the_reason_as_given(api, capsys):
    api.route("POST", f"{CASE_PATH}/close", body=CASE_BODY)
    code, out, err = run(
        capsys,
        "case",
        "close",
        CASE_ID,
        "--reason",
        "  Resolved  ",
        "--username",
        "alice",
    )
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
        (
            "case",
            "impact",
            "set",
            CASE_ID,
            "votes",
            "--severity",
            "low",
            "--scope",
            "x",
        ),
        (
            "case",
            "awareness",
            "add",
            CASE_ID,
            "--awareness-kind",
            "operator",
            "--event-kind",
            "incident",
        ),
        ("case", "legal-duty", "add", CASE_ID, "--kind", "statutory"),
        ("case", "classification", "set", CASE_ID),
    ],
)
def test_missing_or_invalid_arguments_are_usage_errors_without_a_request(
    api, capsys, argv
):
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 2
    assert api.calls == []


@pytest.mark.parametrize("option", ["assign", "unassign"])
def test_user_id_must_be_a_uuid_before_any_request(api, capsys, option):
    code, out, err = run(
        capsys, "case", option, CASE_ID, "--user-id", "bob", "--username", "alice"
    )
    assert code == 2
    assert api.calls == []
    assert "--user-id must be a UUID" in err


def test_duty_id_must_be_a_uuid_before_any_request(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "legal-duty",
        "update",
        CASE_ID,
        "not-a-duty",
        "--status",
        "done",
        "--revision",
        "3",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []
    assert "DUTY_ID must be a UUID" in err


def test_legal_duty_update_without_a_field_is_a_usage_error(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "legal-duty",
        "update",
        CASE_ID,
        DUTY_ID,
        "--revision",
        "3",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []
    assert "give at least one field" in err


def legal_duty_update_without_revision(*extra: str) -> tuple[str, ...]:
    return (
        "case",
        "legal-duty",
        "update",
        CASE_ID,
        DUTY_ID,
        "--username",
        "alice",
        *extra,
    )


def test_legal_duty_update_without_a_revision_reads_the_current_one_and_sends_it(
    api, capsys
):
    api.route("GET", LEGAL_DUTIES_PATH, body=[DUTY_BODY])
    api.route("PATCH", DUTY_PATH, body=DUTY_BODY)
    code, out, err = run(
        capsys, *legal_duty_update_without_revision("--status", "done")
    )
    assert code == 0, err
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("GET", LEGAL_DUTIES_PATH),
        ("PATCH", DUTY_PATH),
        ("POST", "/api/v1/auth/logout"),
    ]
    # The revision the duty list shows (3) travels as If-Match.
    sent = {name.lower(): value for name, value in api.calls[2]["headers"].items()}
    assert sent["if-match"] == '"3"'


def test_legal_duty_update_without_a_revision_of_a_duty_not_listed_sends_nothing(
    api, capsys
):
    api.route("GET", LEGAL_DUTIES_PATH, body=[])
    code, out, err = run(
        capsys, *legal_duty_update_without_revision("--status", "done")
    )
    assert code == 1
    assert out == ""
    assert "legal duty not found" in err
    assert not any(call["method"] == "PATCH" for call in api.calls)


def test_legal_duty_update_without_a_revision_that_changes_meanwhile_says_so(
    api, capsys
):
    api.route("GET", LEGAL_DUTIES_PATH, body=[DUTY_BODY])
    api.route(
        "PATCH",
        DUTY_PATH,
        status=412,
        body={"detail": {"status": "precondition_failed", "current_revision": 5}},
    )
    code, out, err = run(
        capsys, *legal_duty_update_without_revision("--status", "done")
    )
    assert code == 1
    assert out == ""
    assert "changed after this command read it, so nothing was saved" in err
    assert "case legal-duty list" in err
    assert "--revision" not in err


def test_legal_duty_update_sends_only_the_fields_given(api, capsys):
    api.route("PATCH", DUTY_PATH, body=DUTY_BODY)
    code, out, err = run(
        capsys,
        "case",
        "legal-duty",
        "update",
        CASE_ID,
        DUTY_ID,
        "--due-at",
        "2026-11-05T12:00:00+03:00",
        "--jurisdiction",
        "",
        "--revision",
        "3",
        "--username",
        "alice",
    )
    assert code == 0
    # An empty jurisdiction is sent as an empty string, which the API reads as a clear.
    assert api.calls[1]["json"] == {
        "due_at": "2026-11-05T12:00:00+03:00",
        "jurisdiction": "",
    }
    # The revision read from the duty list travels as If-Match (D-16).
    sent = {name.lower(): value for name, value in api.calls[1]["headers"].items()}
    assert sent["if-match"] == '"3"'


def test_legal_duty_update_with_a_stale_revision_names_the_current_one(api, capsys):
    api.route(
        "PATCH",
        DUTY_PATH,
        status=412,
        body={"detail": {"status": "precondition_failed", "current_revision": 5}},
    )
    code, out, err = run(
        capsys,
        "case",
        "legal-duty",
        "update",
        CASE_ID,
        DUTY_ID,
        "--status",
        "done",
        "--revision",
        "3",
        "--username",
        "alice",
    )
    assert code == 1
    assert out == ""
    assert "current revision is 5" in err
    assert "--revision 5" in err


@pytest.mark.parametrize(
    "option, value",
    [("--occurred-at", "2026-10-30T09:00:00"), ("--due-at", "2026-11-02T09:00:00")],
)
def test_timestamps_without_a_timezone_are_usage_errors(api, capsys, option, value):
    argv = [
        "case",
        "legal-duty",
        "add",
        CASE_ID,
        "--kind",
        "statutory",
        "--description",
        "d",
        "--source-reference",
        "s",
        "--due-at",
        value,
    ]
    if option == "--occurred-at":
        argv = [
            "case",
            "awareness",
            "add",
            CASE_ID,
            "--awareness-kind",
            "operator",
            "--event-kind",
            "incident",
            "--occurred-at",
            value,
            "--basis",
            "b",
        ]
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 2
    assert api.calls == []
    assert "must include a timezone" in err


def test_awareness_occurred_at_in_the_future_is_refused_before_any_request(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "awareness",
        "add",
        CASE_ID,
        "--awareness-kind",
        "operator",
        "--event-kind",
        "incident",
        "--occurred-at",
        future_time(),
        "--basis",
        "b",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []
    assert "--occurred-at cannot be in the future" in err


def test_classification_set_accepts_none_applies_as_a_trigger(api, capsys):
    api.route("PUT", f"{CASE_PATH}/classification", body=classification_body(version=1))
    code, out, err = run(
        capsys,
        "case",
        "classification",
        "set",
        CASE_ID,
        "--expected-version",
        "0",
        "--trigger",
        "none_applies",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert api.calls[1]["json"]["triggers"] == ["none_applies"]


def test_classification_set_sends_every_field(api, capsys):
    api.route("PUT", f"{CASE_PATH}/classification", body=classification_body(version=4))
    code, out, err = run(
        capsys,
        "case",
        "classification",
        "set",
        CASE_ID,
        "--expected-version",
        "3",
        "--trigger",
        "boundary_escape",
        "--trigger",
        "confidential_data_exposure",
        "--event-kind",
        "near_miss",
        "--classification-rationale",
        "Within the plan",
        "--responsible-operator",
        "Ops",
        "--affected-versions",
        "1.2",
        "--approved-scope",
        "Scope A",
        "--scope-permission-source",
        "Contract 7",
        "--containment",
        "Keys rotated",
        "--evidence-gap-plan",
        "Pull logs",
        "--next-update-due-at",
        "2026-11-02T09:00:00Z",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert "Classification saved as version 4." in out
    assert api.calls[1]["json"] == {
        "expected_version": 3,
        "triggers": ["boundary_escape", "confidential_data_exposure"],
        "event_kind": "near_miss",
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
    code, out, err = run(
        capsys,
        "case",
        "classification",
        "set",
        CASE_ID,
        "--expected-version",
        "0",
        "--username",
        "alice",
    )
    assert code == 0
    assert api.calls[1]["json"] == empty_classification_payload(0)


def test_classification_set_requires_expected_version(api, capsys):
    code, out, err = run(
        capsys, "case", "classification", "set", CASE_ID, "--username", "alice"
    )
    assert code == 2
    assert api.calls == []
    assert "--expected-version" in err


def test_classification_set_refuses_a_negative_expected_version(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "classification",
        "set",
        CASE_ID,
        "--expected-version",
        "-1",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []


def test_classification_set_rejects_an_unknown_trigger(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "classification",
        "set",
        CASE_ID,
        "--expected-version",
        "0",
        "--trigger",
        "not_a_trigger",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []


def test_classification_stale_save_prints_the_current_version_and_does_not_retry(
    api, capsys
):
    api.route(
        "PUT",
        f"{CASE_PATH}/classification",
        status=409,
        body={
            "detail": {
                "status": "classification_version_conflict",
                "current_version": 3,
            }
        },
    )
    code, out, err = run(
        capsys,
        "case",
        "classification",
        "set",
        CASE_ID,
        "--expected-version",
        "2",
        "--username",
        "alice",
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
    api.route(
        "PUT",
        f"{CASE_PATH}/classification",
        status=409,
        body={"detail": "case is closed"},
    )
    code, out, err = run(
        capsys,
        "case",
        "classification",
        "set",
        CASE_ID,
        "--expected-version",
        "2",
        "--username",
        "alice",
    )
    assert code == 1
    assert "the case is closed, so the classification cannot change" in err
    assert "current version" not in err


def test_classification_permission_denied_names_the_full_view_requirement(api, capsys):
    api.route(
        "PUT",
        f"{CASE_PATH}/classification",
        status=403,
        body={"detail": "classification_requires_full_view"},
    )
    code, out, err = run(
        capsys,
        "case",
        "classification",
        "set",
        CASE_ID,
        "--expected-version",
        "2",
        "--username",
        "alice",
    )
    assert code == 1
    assert "cases:read_all" in err


def test_assign_refusal_names_the_assignable_permission_not_the_role_names(api, capsys):
    api.route(
        "POST",
        f"{CASE_PATH}/assignments",
        status=422,
        body={"detail": "assignee must be an investigator or reviewer"},
    )
    code, out, err = run(
        capsys, "case", "assign", CASE_ID, "--user-id", USER_ID, "--username", "alice"
    )
    assert code == 1
    assert "cases:assignable" in err
    assert "investigator" not in err


def test_review_record_permission_denied_names_the_assignment_requirement(api, capsys):
    api.route(
        "PATCH",
        REVIEW_LAYER_PATH,
        status=403,
        body={"detail": "only an assigned reviewer can record a review"},
    )
    code, out, err = run(
        capsys,
        "case",
        "review",
        "record",
        CASE_ID,
        "--layer",
        "model",
        "--outcome",
        "fail",
        "--reason",
        "Why",
        "--round",
        ROUND_ID,
        "--evidence",
        EVIDENCE_ID,
        "--revision",
        "1",
        "--username",
        "alice",
    )
    assert code == 1
    assert "assigned to the case" in err


def test_review_record_without_round_uses_the_open_round_and_its_revision_is_the_one_given(
    api, capsys
):
    api.route(
        "GET",
        REVIEW_LIST_PATH,
        body=[
            review_round_body(status="closed", revision=9),
            review_round_body(revision=4),
        ],
    )
    api.route("PATCH", REVIEW_LAYER_PATH, body=review_round_body(revision=5))
    code, out, err = run(
        capsys,
        "case",
        "review",
        "record",
        CASE_ID,
        "--layer",
        "model",
        "--outcome",
        "pass",
        "--evidence",
        EVIDENCE_ID,
        "--revision",
        "4",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert "The round revision is now 5." in out
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("GET", "/api/v1/reviews"),
        ("PATCH", REVIEW_LAYER_PATH),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert api.calls[2]["json"] == {
        "outcome": "pass",
        "reason": None,
        "evidence_ids": [EVIDENCE_ID],
    }
    # The revision read with 'review list' travels as If-Match, not in the body.
    sent = {name.lower(): value for name, value in api.calls[2]["headers"].items()}
    assert sent["if-match"] == '"4"'


def test_review_record_pass_without_evidence_is_refused_before_any_request(api, capsys):
    # A pass cites at least one evidence item of the case (T06). The check runs before the login and the request.
    code, out, err = run(
        capsys,
        "case",
        "review",
        "record",
        CASE_ID,
        "--layer",
        "model",
        "--outcome",
        "pass",
        "--revision",
        "1",
        "--username",
        "alice",
    )
    assert code == 2
    assert "a pass needs at least one evidence item of this case" in err
    assert api.calls == []


def test_review_record_without_an_open_round_says_how_to_open_one(api, capsys):
    api.route("GET", REVIEW_LIST_PATH, body=[review_round_body(status="closed")])
    code, out, err = run(
        capsys,
        "case",
        "review",
        "record",
        CASE_ID,
        "--layer",
        "model",
        "--outcome",
        "pass",
        "--evidence",
        EVIDENCE_ID,
        "--revision",
        "1",
        "--username",
        "alice",
    )
    assert code == 1
    assert "no open review round" in err
    assert "review open" in err


def test_review_record_refuses_a_non_pass_outcome_without_a_reason_before_any_request(
    api, capsys
):
    code, out, err = run(
        capsys,
        "case",
        "review",
        "record",
        CASE_ID,
        "--layer",
        "model",
        "--outcome",
        "fail",
        "--round",
        ROUND_ID,
        "--evidence",
        EVIDENCE_ID,
        "--revision",
        "1",
        "--username",
        "alice",
    )
    assert code == 2  # a usage error, as in the other argument checks
    assert "a reason is required" in err
    assert all(method != "PATCH" for method, _ in api.paths())


def test_review_record_revision_conflict_reports_the_current_revision(api, capsys):
    api.route(
        "PATCH",
        REVIEW_LAYER_PATH,
        status=412,
        body={"detail": {"status": "precondition_failed", "current_revision": 7}},
    )
    code, out, err = run(
        capsys,
        "case",
        "review",
        "record",
        CASE_ID,
        "--layer",
        "model",
        "--outcome",
        "pass",
        "--evidence",
        EVIDENCE_ID,
        "--round",
        ROUND_ID,
        "--revision",
        "3",
        "--username",
        "alice",
    )
    assert code == 1
    assert out == ""
    assert "nothing was saved" in err
    assert "current revision is 7" in err
    assert "--revision 7" in err


def test_review_open_posts_the_case_with_an_idempotency_key_and_prints_the_round(
    api, capsys
):
    api.route(
        "POST",
        "/api/v1/reviews",
        status=201,
        body=review_round_body(revision=1, model_set=False),
    )
    code, out, err = run(
        capsys, "case", "review", "open", CASE_ID, "--username", "alice"
    )
    assert code == 0, err
    assert "Review round opened." in out
    assert f"Round ID:   {ROUND_ID}" in out
    assert "Revision:   1" in out
    assert "not set" in out
    assert api.calls[1]["json"] == {"case_id": CASE_ID}
    sent = {name.lower(): value for name, value in api.calls[1]["headers"].items()}
    assert len(sent["idempotency-key"]) >= 1


def test_review_open_sends_the_key_given_so_a_retry_can_reuse_it(api, capsys):
    api.route(
        "POST",
        "/api/v1/reviews",
        status=201,
        body=review_round_body(revision=1, model_set=False),
    )
    code, out, err = run(
        capsys,
        "case",
        "review",
        "open",
        CASE_ID,
        "--idempotency-key",
        "retry-1",
        "--username",
        "alice",
    )
    assert code == 0, err
    sent = {name.lower(): value for name, value in api.calls[1]["headers"].items()}
    assert sent["idempotency-key"] == "retry-1"


def test_review_list_hides_a_reason_the_caller_may_not_read(api, capsys):
    api.route("GET", REVIEW_LIST_PATH, body=[review_round_body(reason=None)])
    route_me(api, ["cases:read_own"])
    code, out, err = run(
        capsys, "case", "review", "list", CASE_ID, "--username", "alice"
    )
    assert code == 0, err
    assert "Revision:   2" in out
    assert "model" in out and "pass" in out
    assert cli.NOT_VISIBLE_TEXT in out


def test_review_list_shows_a_reason_to_the_full_view(api, capsys):
    api.route(
        "GET", REVIEW_LIST_PATH, body=[review_round_body(reason="Export missing")]
    )
    route_me(api, FULL_VIEW)
    code, out, err = run(
        capsys, "case", "review", "list", CASE_ID, "--username", "alice"
    )
    assert code == 0, err
    assert "Reason: Export missing" in out


# --- case links -----------------------------------------------------------------------------------------------------

LINKS_PATH = f"{CASE_PATH}/links"
LINKED_ID = "dddddddd-eeee-4fff-8000-111111111111"
LINK_BODY = {
    "linked_case_id": LINKED_ID,
    "linked_case_title": "Related case",
    "link_type": "related",
    "reason": "Same incident",
    "share_visibility": False,
    "primary_side": None,
    "created_at": "2026-11-01T09:00:00Z",
}
HIDDEN_LINK_BODY = {
    "linked_case_id": None,
    "linked_case_title": None,
    "link_type": "related",
    "reason": "Same incident",
    "share_visibility": False,
    "primary_side": None,
    "created_at": "2026-11-01T09:00:00Z",
}


def test_link_add_sends_the_type_reason_and_defaults_then_says_nothing_else_changes(
    api, capsys
):
    api.route("POST", LINKS_PATH, status=201, body=LINK_BODY)
    code, out, err = run(
        capsys,
        "case",
        "link",
        "add",
        CASE_ID,
        LINKED_ID,
        "--type",
        "related",
        "--reason",
        "Same incident",
        "--username",
        "alice",
    )
    assert code == 0
    assert api.calls[1]["method"] == "POST"
    assert api.calls[1]["json"] == {
        "linked_case_id": LINKED_ID,
        "link_type": "related",
        "reason": "Same incident",
        "share_visibility": False,
        "primary_case_id": None,
    }
    assert out.startswith("Cases linked. Linking changes no task, deadline or status.")
    assert LINKED_ID in out and "Related case" in out
    assert "Link type:        related" in out
    assert err == ""


def test_link_add_sends_the_main_case_and_the_visibility_checkbox(api, capsys):
    api.route("POST", LINKS_PATH, status=201, body=LINK_BODY)
    code, out, err = run(
        capsys,
        "case",
        "link",
        "add",
        CASE_ID,
        LINKED_ID,
        "--type",
        "root_sub",
        "--reason",
        "Root of the analysis",
        "--primary-case-id",
        LINKED_ID,
        "--share-visibility",
        "--username",
        "alice",
    )
    assert code == 0
    assert api.calls[1]["json"] == {
        "linked_case_id": LINKED_ID,
        "link_type": "root_sub",
        "reason": "Root of the analysis",
        "share_visibility": True,
        "primary_case_id": LINKED_ID,
    }


def test_link_add_duplicate_reports_that_the_duplicate_is_closed(api, capsys):
    duplicate_body = dict(LINK_BODY, link_type="duplicate", primary_side="linked")
    api.route("POST", LINKS_PATH, status=201, body=duplicate_body)
    code, out, err = run(
        capsys,
        "case",
        "link",
        "add",
        CASE_ID,
        LINKED_ID,
        "--type",
        "duplicate",
        "--reason",
        "Same incident",
        "--primary-case-id",
        LINKED_ID,
        "--username",
        "alice",
    )
    assert code == 0
    assert out.startswith("Duplicate linked. The duplicate case is now closed.")
    assert "Main or root:     the linked case" in out


def test_link_add_type_is_required_and_checked_by_the_parser(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "link",
        "add",
        CASE_ID,
        LINKED_ID,
        "--type",
        "partner",
        "--reason",
        "Same incident",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []


def test_link_add_reason_is_required_by_the_parser(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "link",
        "add",
        CASE_ID,
        LINKED_ID,
        "--type",
        "related",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []


def test_link_add_json_prints_the_api_body_exactly(api, capsys):
    api.route("POST", LINKS_PATH, status=201, body=LINK_BODY)
    code, out, err = run(
        capsys,
        "case",
        "link",
        "add",
        CASE_ID,
        LINKED_ID,
        "--type",
        "related",
        "--reason",
        "Same incident",
        "--username",
        "alice",
        "--json",
    )
    assert code == 0
    assert out == compact(LINK_BODY).decode("utf-8") + "\n"


@pytest.mark.parametrize(
    ("status", "detail", "message"),
    [
        pytest.param(
            404,
            {"detail": "case not found"},
            "case not found, or the other case",
            id="not-found",
        ),
        pytest.param(
            409,
            {"detail": "cases are already linked"},
            "already linked",
            id="duplicate-or-closed",
        ),
        pytest.param(
            409,
            {"detail": "duplicate case cannot be closed: no review is recorded"},
            "cannot be closed yet",
            id="duplicate-blocked",
        ),
        pytest.param(
            422,
            {"detail": "a case cannot be linked to itself"},
            "cannot be linked to itself",
            id="same-case",
        ),
    ],
)
def test_link_add_refusals_are_reported_as_messages(
    api, capsys, status, detail, message
):
    api.route("POST", LINKS_PATH, status=status, body=detail)
    code, out, err = run(
        capsys,
        "case",
        "link",
        "add",
        CASE_ID,
        LINKED_ID,
        "--type",
        "related",
        "--reason",
        "Same incident",
        "--username",
        "alice",
    )
    assert code == 1
    assert message in err


def test_link_add_rejects_a_linked_case_id_that_is_not_a_uuid_without_a_request(
    api, capsys
):
    code, out, err = run(
        capsys,
        "case",
        "link",
        "add",
        CASE_ID,
        "not-a-uuid",
        "--type",
        "related",
        "--reason",
        "Same incident",
        "--username",
        "alice",
    )
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
    code, out, err = run(
        capsys, "case", "link", "list", CASE_ID, "--username", "alice", "--json"
    )
    assert code == 0
    assert out == compact([LINK_BODY]).decode("utf-8") + "\n"


def test_link_remove_sends_a_delete_and_reports_the_removal(api, capsys):
    api.route("DELETE", f"{LINKS_PATH}/{LINKED_ID}", status=204)
    code, out, err = run(
        capsys, "case", "link", "remove", CASE_ID, LINKED_ID, "--username", "alice"
    )
    assert code == 0
    assert api.calls[1]["method"] == "DELETE"
    assert api.calls[1]["json"] is None
    assert out.startswith("Link removed.")


def test_link_remove_not_found_is_reported(api, capsys):
    api.route(
        "DELETE",
        f"{LINKS_PATH}/{LINKED_ID}",
        status=404,
        body={"detail": "link not found"},
    )
    code, out, err = run(
        capsys, "case", "link", "remove", CASE_ID, LINKED_ID, "--username", "alice"
    )
    assert code == 1
    assert "no link between the two cases" in err


def test_link_remove_has_no_json_flag(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "link",
        "remove",
        CASE_ID,
        LINKED_ID,
        "--username",
        "alice",
        "--json",
    )
    assert code == 2
    assert api.calls == []


# --- case record reads: classification, impacts, legal duties, awareness events, preliminary report ---

CLASSIFICATION_PATH = f"{CASE_PATH}/classification"
IMPACTS_PATH = f"{CASE_PATH}/impacts"
LEGAL_DUTIES_PATH = f"{CASE_PATH}/legal-duties"
AWARENESS_PATH = f"{CASE_PATH}/awareness-events"
REPORT_PATH = f"{CASE_PATH}/preliminary-report"
ME_PATH = "/api/v1/auth/me"
AWARENESS_ID = "cccccccc-dddd-4eee-8fff-000000000000"
NOT_VISIBLE = "(not visible to your account)"
FULL_VIEW = ["cases:read_all", "cases:triage"]
LIMITED_VIEW = ["cases:read_own"]
CLASSIFICATION_FREE_FIELDS = (
    "classification_rationale",
    "responsible_operator",
    "affected_versions",
    "approved_scope",
    "scope_permission_source",
    "containment",
    "evidence_gap_plan",
)
CLASSIFICATION_BODY = {
    "case_id": CASE_ID,
    "version": 2,
    "triggers": ["boundary_escape", "confidential_data_exposure"],
    "event_kind": "incident",
    "classification_rationale": "Customer data left the boundary",
    "responsible_operator": "Ops lead",
    "affected_versions": None,
    "approved_scope": "Tenant A",
    "scope_permission_source": None,
    "containment": "Keys rotated",
    "evidence_gap_plan": None,
    "next_update_due_at": "2026-11-02T09:00:00Z",
    "created_by": "alice",
    "created_at": "2026-11-01T08:00:00Z",
}
IMPACTS_BODY = [
    {
        "area": "affected_people_groups",
        "assessed": True,
        "severity": "high",
        "scope": "Tenant A users",
        "updated_at": "2026-11-01T08:30:00Z",
    },
    {
        "area": "severity",
        "assessed": False,
        "severity": None,
        "scope": None,
        "updated_at": None,
    },
]
LEGAL_DUTIES_BODY = [
    {
        "id": DUTY_ID,
        "case_id": CASE_ID,
        "kind": "statutory",
        "description": "Notify the regulator",
        "source_reference": "Art. 33",
        "jurisdiction": "EU",
        "due_at": "2026-11-03T09:00:00Z",
        "status": "open",
        "created_by": "alice",
        "created_at": "2026-11-01T08:00:00Z",
        "revision": 3,
    }
]
AWARENESS_LIST_BODY = [
    {
        "id": AWARENESS_ID,
        "case_id": CASE_ID,
        "awareness_kind": "operator",
        "event_kind": "incident",
        "occurred_at": "2026-10-30T08:00:00Z",
        "basis": "Log review",
        "recorded_by": "alice",
        "created_at": "2026-10-31T10:00:00Z",
    }
]
REPORT_BODY = {
    "case_id": CASE_ID,
    "reference": "SAFE-2026-000001",
    "case_status": "open",
    "generated_at": "2026-11-01T10:00:00+00:00",
    "awareness_time": "2026-10-30T09:00:00+00:00",
    "complete": False,
    "missing_fields": [
        {"field": "contact", "state": "missing"},
        {"field": "impacts.severity", "state": "unknown"},
    ],
    "contact": {"status": "missing"},
    "classification": {
        "version": 2,
        "triggers": ["boundary_escape"],
        "event_kind": "incident",
        "next_update_due_at": "2026-11-02T09:00:00+00:00",
    },
    "awareness_events": [
        {
            "id": AWARENESS_ID,
            "awareness_kind": "operator",
            "event_kind": "incident",
            "occurred_at": "2026-10-30T08:00:00+00:00",
            "basis": "Log review",
        }
    ],
    "impacts": [
        {
            "area": "affected_people_groups",
            "assessed": True,
            "severity": "high",
            "scope": "Tenant A users",
        },
        {"area": "severity", "assessed": False, "severity": None, "scope": None},
    ],
    "legal_duties": [
        {
            "id": DUTY_ID,
            "kind": "statutory",
            "description": "Notify the regulator",
            "source_reference": "Art. 33",
            "jurisdiction": "EU",
            "due_at": "2026-11-03T09:00:00+00:00",
            "status": "open",
        }
    ],
    "safe_deadlines": [
        {
            "kind": "initial_notice",
            "due_at": "2026-11-01T09:00:00+00:00",
            "status": "pending",
        }
    ],
}


def route_me(api: FakeAPI, permissions: list[str]) -> None:
    """The caller's permissions, read by the human output of the free-text commands."""
    api.route(
        "GET",
        ME_PATH,
        body={
            "id": USER_ID,
            "username": "alice",
            "role": "investigator",
            "password_change_required": False,
            "timezone_setup_required": False,
            "permissions": permissions,
        },
    )


def hidden(body: dict, fields: tuple[str, ...]) -> dict:
    """The body as the API returns it to a caller without cases:read_all: the named free-text fields are null."""
    return {**body, **{name: None for name in fields}}


def field_line(out: str, label: str) -> str:
    """The output line of one label. Labels are unique within one command's output."""
    return next(
        line for line in out.splitlines() if line.strip().startswith(f"{label}:")
    )


def row_of(out: str, first_cell: str) -> str:
    """The table row whose first cell is first_cell."""
    return next(line for line in out.splitlines() if line.split()[:1] == [first_cell])


CASE_RECORD_READS = [
    pytest.param(
        ("case", "classification", "show", CASE_ID),
        CLASSIFICATION_PATH,
        id="classification",
    ),
    pytest.param(("case", "impact", "list", CASE_ID), IMPACTS_PATH, id="impacts"),
    pytest.param(
        ("case", "legal-duty", "list", CASE_ID), LEGAL_DUTIES_PATH, id="legal-duties"
    ),
    pytest.param(
        ("case", "awareness", "list", CASE_ID), AWARENESS_PATH, id="awareness"
    ),
    pytest.param(("case", "report", "show", CASE_ID), REPORT_PATH, id="report"),
]


# classification show


def test_classification_show_prints_the_fields_for_a_full_view_caller(api, capsys):
    api.route("GET", CLASSIFICATION_PATH, body=CLASSIFICATION_BODY)
    route_me(api, FULL_VIEW)
    code, out, err = run(
        capsys, "case", "classification", "show", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert "Classification version 2:" in out
    assert field_line(out, "Triggers").endswith(
        "boundary_escape, confidential_data_exposure"
    )
    assert field_line(out, "Rationale").endswith("Customer data left the boundary")
    assert field_line(out, "Next update (UTC)").endswith("2026-11-02T09:00:00Z")
    # Unset free text is a dash for this caller: nothing is hidden from it.
    assert field_line(out, "Affected versions").endswith("-")
    assert NOT_VISIBLE not in out


def test_classification_show_sends_a_bearer_get_and_ends_the_session(api, capsys):
    api.route("GET", CLASSIFICATION_PATH, body=CLASSIFICATION_BODY)
    route_me(api, FULL_VIEW)
    code, out, err = run(
        capsys, "case", "classification", "show", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("GET", CLASSIFICATION_PATH),
        ("GET", ME_PATH),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert api.calls[1]["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert api.calls[1]["json"] is None


def test_classification_show_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", CLASSIFICATION_PATH, body=CLASSIFICATION_BODY)
    code, out, err = run(
        capsys,
        "case",
        "classification",
        "show",
        CASE_ID,
        "--username",
        "alice",
        "--json",
    )
    assert code == 0
    assert out == compact(CLASSIFICATION_BODY).decode("utf-8") + "\n"
    # The JSON output makes no second request: the permission is read only for the human output.
    assert ("GET", ME_PATH) not in api.paths()


def test_classification_show_marks_hidden_free_text_for_a_caller_without_read_all(
    api, capsys
):
    api.route(
        "GET",
        CLASSIFICATION_PATH,
        body=hidden(CLASSIFICATION_BODY, CLASSIFICATION_FREE_FIELDS),
    )
    route_me(api, LIMITED_VIEW)
    code, out, err = run(
        capsys, "case", "classification", "show", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert field_line(out, "Rationale").endswith(NOT_VISIBLE)
    assert field_line(out, "Approved scope").endswith(NOT_VISIBLE)
    assert field_line(out, "Containment").endswith(NOT_VISIBLE)
    # Fields that are not free text stay visible.
    assert field_line(out, "Event kind").endswith("incident")
    assert field_line(out, "Triggers").endswith(
        "boundary_escape, confidential_data_exposure"
    )


def test_classification_show_says_when_none_is_recorded(api, capsys):
    empty = {
        **CLASSIFICATION_BODY,
        "version": 0,
        "triggers": [],
        "event_kind": None,
        "created_by": None,
        "created_at": None,
    }
    body = hidden(empty, CLASSIFICATION_FREE_FIELDS)
    api.route("GET", CLASSIFICATION_PATH, body=body)
    route_me(api, FULL_VIEW)
    code, out, err = run(
        capsys, "case", "classification", "show", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert out == "No classification recorded for this case.\n"


def test_classification_show_with_no_triggers_says_not_assessed(api, capsys):
    api.route("GET", CLASSIFICATION_PATH, body={**CLASSIFICATION_BODY, "triggers": []})
    route_me(api, FULL_VIEW)
    code, out, err = run(
        capsys, "case", "classification", "show", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert field_line(out, "Triggers").endswith("not assessed")


# impact list


def test_impact_list_prints_one_line_per_area_for_a_full_view_caller(api, capsys):
    api.route("GET", IMPACTS_PATH, body=IMPACTS_BODY)
    route_me(api, FULL_VIEW)
    code, out, err = run(
        capsys, "case", "impact", "list", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert out.splitlines()[0].split() == [
        "AREA",
        "SEVERITY",
        "SCOPE",
        "UPDATED",
        "(UTC)",
    ]
    assert "Tenant A users" in row_of(out, "affected_people_groups")
    assert "not assessed" in row_of(out, "severity")
    assert NOT_VISIBLE not in out


def test_impact_list_marks_hidden_scope_and_keeps_unassessed_areas_apart(api, capsys):
    api.route(
        "GET", IMPACTS_PATH, body=[hidden(IMPACTS_BODY[0], ("scope",)), IMPACTS_BODY[1]]
    )
    route_me(api, LIMITED_VIEW)
    code, out, err = run(
        capsys, "case", "impact", "list", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert NOT_VISIBLE in row_of(out, "affected_people_groups")
    # An area with no assessment is "not assessed", never the hidden text or a severity.
    assert NOT_VISIBLE not in row_of(out, "severity")
    assert "not assessed" in row_of(out, "severity")


def test_impact_list_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", IMPACTS_PATH, body=IMPACTS_BODY)
    code, out, err = run(
        capsys, "case", "impact", "list", CASE_ID, "--username", "alice", "--json"
    )
    assert code == 0
    assert out == compact(IMPACTS_BODY).decode("utf-8") + "\n"
    assert ("GET", ME_PATH) not in api.paths()


# legal-duty list


def test_legal_duty_list_prints_one_line_per_duty(api, capsys):
    api.route("GET", LEGAL_DUTIES_PATH, body=LEGAL_DUTIES_BODY)
    route_me(api, FULL_VIEW)
    code, out, err = run(
        capsys, "case", "legal-duty", "list", CASE_ID, "--username", "alice"
    )
    assert code == 0
    row = row_of(out, DUTY_ID)
    assert "statutory" in row and "open" in row and "2026-11-03T09:00:00Z" in row
    assert "Notify the regulator" in row and "Art. 33" in row and "EU" in row
    assert NOT_VISIBLE not in out


def test_legal_duty_list_marks_hidden_free_text_for_a_caller_without_read_all(
    api, capsys
):
    hidden_duties = [
        hidden(
            LEGAL_DUTIES_BODY[0], ("description", "source_reference", "jurisdiction")
        )
    ]
    api.route("GET", LEGAL_DUTIES_PATH, body=hidden_duties)
    route_me(api, LIMITED_VIEW)
    code, out, err = run(
        capsys, "case", "legal-duty", "list", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert row_of(out, DUTY_ID).count(NOT_VISIBLE) == 3
    assert "statutory" in row_of(out, DUTY_ID)


def test_legal_duty_list_empty_says_so(api, capsys):
    api.route("GET", LEGAL_DUTIES_PATH, body=[])
    route_me(api, FULL_VIEW)
    code, out, err = run(
        capsys, "case", "legal-duty", "list", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert out == "No legal duties recorded for this case.\n"


def test_legal_duty_list_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", LEGAL_DUTIES_PATH, body=LEGAL_DUTIES_BODY)
    code, out, err = run(
        capsys, "case", "legal-duty", "list", CASE_ID, "--username", "alice", "--json"
    )
    assert code == 0
    assert out == compact(LEGAL_DUTIES_BODY).decode("utf-8") + "\n"
    assert ("GET", ME_PATH) not in api.paths()


# awareness list


def test_awareness_list_prints_one_line_per_event(api, capsys):
    api.route("GET", AWARENESS_PATH, body=AWARENESS_LIST_BODY)
    route_me(api, FULL_VIEW)
    code, out, err = run(
        capsys, "case", "awareness", "list", CASE_ID, "--username", "alice"
    )
    assert code == 0
    row = row_of(out, AWARENESS_ID)
    assert (
        "operator" in row
        and "incident" in row
        and "2026-10-30T08:00:00Z" in row
        and "Log review" in row
    )


def test_awareness_list_marks_hidden_basis_for_a_caller_without_read_all(api, capsys):
    api.route("GET", AWARENESS_PATH, body=[hidden(AWARENESS_LIST_BODY[0], ("basis",))])
    route_me(api, LIMITED_VIEW)
    code, out, err = run(
        capsys, "case", "awareness", "list", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert row_of(out, AWARENESS_ID).endswith(NOT_VISIBLE)


def test_awareness_list_empty_says_so(api, capsys):
    api.route("GET", AWARENESS_PATH, body=[])
    route_me(api, FULL_VIEW)
    code, out, err = run(
        capsys, "case", "awareness", "list", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert out == "No awareness events recorded for this case.\n"


def test_awareness_list_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", AWARENESS_PATH, body=AWARENESS_LIST_BODY)
    code, out, err = run(
        capsys, "case", "awareness", "list", CASE_ID, "--username", "alice", "--json"
    )
    assert code == 0
    assert out == compact(AWARENESS_LIST_BODY).decode("utf-8") + "\n"
    assert ("GET", ME_PATH) not in api.paths()


# preliminary report


def test_report_show_prints_the_summary_without_a_permission_read(api, capsys):
    api.route("GET", REPORT_PATH, body=REPORT_BODY)
    code, out, err = run(
        capsys, "case", "report", "show", CASE_ID, "--username", "alice"
    )
    assert code == 0
    assert out.splitlines()[0] == "Preliminary report for SAFE-2026-000001 (case open)"
    assert field_line(out, "Complete").endswith("no")
    assert "    contact: missing" in out.splitlines()
    assert "    impacts.severity: unknown" in out.splitlines()
    assert field_line(out, "Classification").endswith(
        "version 2, triggers: boundary_escape"
    )
    assert field_line(out, "Impacts assessed").endswith("1 of 2")
    assert field_line(out, "Legal duties").endswith("1 (1 open)")
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("GET", REPORT_PATH),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert api.calls[1]["headers"]["Authorization"] == f"Bearer {ACCESS}"


def test_report_show_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", REPORT_PATH, body=REPORT_BODY)
    code, out, err = run(
        capsys, "case", "report", "show", CASE_ID, "--username", "alice", "--json"
    )
    assert code == 0
    assert out == compact(REPORT_BODY).decode("utf-8") + "\n"


def test_report_show_forbidden_names_the_read_all_permission(api, capsys):
    api.route("GET", REPORT_PATH, status=403, body={"detail": "forbidden"})
    code, out, err = run(
        capsys, "case", "report", "show", CASE_ID, "--username", "alice"
    )
    assert code == 1
    assert out == ""
    assert (
        "the preliminary report needs cases:read_all, which your account does not hold"
        in err
    )
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


# refusals, usage errors and secrets, for every case record read


@pytest.mark.parametrize("argv, path", CASE_RECORD_READS)
@pytest.mark.parametrize(
    ("status", "message"),
    [
        pytest.param(403, "cases:read_all", id="forbidden"),
        pytest.param(404, "case not found", id="not-found"),
    ],
)
def test_case_record_reads_report_refusals_and_end_the_session(
    api, capsys, argv, path, status, message
):
    api.route("GET", path, status=status, body={"detail": "refused"})
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 1
    assert out == ""
    assert message in err
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(
            ("case", "classification", "show", "not-a-case"), id="classification"
        ),
        pytest.param(("case", "impact", "list", "not-a-case"), id="impacts"),
        pytest.param(("case", "legal-duty", "list", "not-a-case"), id="legal-duties"),
        pytest.param(("case", "awareness", "list", "not-a-case"), id="awareness"),
        pytest.param(("case", "report", "show", "not-a-case"), id="report"),
    ],
)
def test_case_record_reads_reject_a_case_id_that_is_not_a_uuid_without_a_request(
    api, capsys, argv
):
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 2
    assert "CASE_ID must be the case ID" in err
    assert api.calls == []


@pytest.mark.parametrize(
    ("argv", "path", "body"),
    [
        pytest.param(
            ("case", "classification", "show", CASE_ID),
            CLASSIFICATION_PATH,
            CLASSIFICATION_BODY,
            id="classification",
        ),
        pytest.param(
            ("case", "impact", "list", CASE_ID),
            IMPACTS_PATH,
            IMPACTS_BODY,
            id="impacts",
        ),
        pytest.param(
            ("case", "legal-duty", "list", CASE_ID),
            LEGAL_DUTIES_PATH,
            LEGAL_DUTIES_BODY,
            id="legal-duties",
        ),
        pytest.param(
            ("case", "awareness", "list", CASE_ID),
            AWARENESS_PATH,
            AWARENESS_LIST_BODY,
            id="awareness",
        ),
        pytest.param(
            ("case", "report", "show", CASE_ID), REPORT_PATH, REPORT_BODY, id="report"
        ),
    ],
)
def test_case_record_reads_never_print_the_token_or_the_password(
    api, capsys, argv, path, body
):
    api.route("GET", path, body=body)
    route_me(api, FULL_VIEW)
    code, out, err = run(capsys, *argv, "--username", "alice")
    assert code == 0
    assert ACCESS not in out + err
    assert REFRESH not in out + err
    assert PASSWORD not in out + err


# --- review conflict, recuse and appeal ----------------------------------------------------------------------------

REPLACEMENT_ID = "66666666-7777-4888-8999-bbbbbbbbbbbb"
CONFLICT_PATH = f"/api/v1/reviews/{ROUND_ID}/conflicts"
RECUSAL_PATH = f"/api/v1/reviews/{ROUND_ID}/recusals"
APPEAL_PATH = f"/api/v1/reviews/{ROUND_ID}/appeals"
CONFLICT_ENTRY = {
    "id": "dddddddd-eeee-4fff-8000-000000000001",
    "round_id": ROUND_ID,
    "reviewer_id": USER_ID,
    "kind": "declared",
    "description": "Built the system",
    "supersedes_id": None,
    "created_at": CASE_UPDATED_AT,
    "current": True,
}
RECUSAL_ENTRY = {
    "id": "dddddddd-eeee-4fff-8000-000000000002",
    "round_id": ROUND_ID,
    "reviewer_id": USER_ID,
    "replacement_id": REPLACEMENT_ID,
    "reason": "Too close",
    "supersedes_id": None,
    "created_at": CASE_UPDATED_AT,
    "current": True,
}
APPEAL_ENTRY = {
    "id": "dddddddd-eeee-4fff-8000-000000000003",
    "round_id": ROUND_ID,
    "filed_by": USER_ID,
    "independent_reviewer_id": REPLACEMENT_ID,
    "grounds": "The layer was not tested",
    "created_at": CASE_UPDATED_AT,
}


def last_post(api) -> dict:
    """The last request to a review route. The logout that ends each session comes after it."""
    posts = [
        call
        for call in api.calls
        if call["method"] == "POST" and call["path"].startswith("/api/v1/reviews/")
    ]
    return posts[-1]


def posted_key(api) -> str:
    sent = {name.lower(): value for name, value in last_post(api)["headers"].items()}
    return sent["idempotency-key"]


def test_review_conflict_declared_posts_the_kind_and_description_with_a_key(
    api, capsys
):
    api.route("POST", CONFLICT_PATH, status=201, body=CONFLICT_ENTRY)
    code, out, err = run(
        capsys,
        "case",
        "review",
        "conflict",
        ROUND_ID,
        "--kind",
        "declared",
        "--description",
        "Built the system",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert "Conflict declared." in out
    assert f"ID:               {CONFLICT_ENTRY['id']}" in out
    assert last_post(api)["json"] == {
        "kind": "declared",
        "description": "Built the system",
        "supersedes_id": None,
    }
    assert len(posted_key(api)) >= 1


def test_review_conflict_declared_needs_a_description_before_any_request(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "review",
        "conflict",
        ROUND_ID,
        "--kind",
        "declared",
        "--username",
        "alice",
    )
    assert code == 2
    assert "a description is required for a declared conflict" in err
    assert all(method != "POST" for method, _ in api.paths())


def test_review_conflict_withdrawn_needs_no_description_and_can_correct_an_entry(
    api, capsys
):
    api.route(
        "POST",
        CONFLICT_PATH,
        status=201,
        body={**CONFLICT_ENTRY, "kind": "withdrawn", "description": None},
    )
    code, out, err = run(
        capsys,
        "case",
        "review",
        "conflict",
        ROUND_ID,
        "--kind",
        "withdrawn",
        "--supersedes",
        CONFLICT_ENTRY["id"],
        "--idempotency-key",
        "retry-7",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert "Conflict withdrawn." in out
    assert last_post(api)["json"] == {
        "kind": "withdrawn",
        "description": None,
        "supersedes_id": CONFLICT_ENTRY["id"],
    }
    assert posted_key(api) == "retry-7"


def test_review_conflict_409_says_how_to_check_the_round(api, capsys):
    api.route(
        "POST",
        CONFLICT_PATH,
        status=409,
        body={"detail": {"status": "conflict_already_declared"}},
    )
    code, out, err = run(
        capsys,
        "case",
        "review",
        "conflict",
        ROUND_ID,
        "--kind",
        "declared",
        "--description",
        "Again",
        "--username",
        "alice",
    )
    assert code != 0
    assert "already declared" in err
    assert "case review list" in err


def test_review_conflict_json_prints_the_api_body_exactly(api, capsys):
    api.route("POST", CONFLICT_PATH, status=201, body=CONFLICT_ENTRY)
    code, out, err = run(
        capsys,
        "case",
        "review",
        "conflict",
        ROUND_ID,
        "--kind",
        "declared",
        "--description",
        "Built the system",
        "--json",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert json.loads(out) == CONFLICT_ENTRY


def test_review_recuse_posts_the_replacement_and_prints_it(api, capsys):
    api.route("POST", RECUSAL_PATH, status=201, body=RECUSAL_ENTRY)
    code, out, err = run(
        capsys,
        "case",
        "review",
        "recuse",
        ROUND_ID,
        "--replacement",
        REPLACEMENT_ID,
        "--reason",
        "Too close",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert "Recusal recorded." in out
    assert f"Replacement ID:   {REPLACEMENT_ID}" in out
    assert last_post(api)["json"] == {
        "replacement_id": REPLACEMENT_ID,
        "reason": "Too close",
        "supersedes_id": None,
    }
    assert len(posted_key(api)) >= 1


def test_review_recuse_refuses_a_replacement_that_is_not_a_uuid_before_any_request(
    api, capsys
):
    code, out, err = run(
        capsys,
        "case",
        "review",
        "recuse",
        ROUND_ID,
        "--replacement",
        "nobody",
        "--username",
        "alice",
    )
    assert code == 2
    assert "--replacement must be a UUID" in err
    assert all(method != "POST" for method, _ in api.paths())


def test_review_recuse_replacement_refusal_names_the_rule(api, capsys):
    api.route(
        "POST",
        RECUSAL_PATH,
        status=422,
        body={"detail": "the replacement has a declared conflict"},
    )
    code, out, err = run(
        capsys,
        "case",
        "review",
        "recuse",
        ROUND_ID,
        "--replacement",
        REPLACEMENT_ID,
        "--username",
        "alice",
    )
    assert code != 0
    assert "declared conflict" in err


def test_review_appeal_posts_only_the_grounds_and_the_system_names_the_reviewer(
    api, capsys
):
    api.route("POST", APPEAL_PATH, status=201, body=APPEAL_ENTRY)
    code, out, err = run(
        capsys,
        "case",
        "review",
        "appeal",
        ROUND_ID,
        "--grounds",
        "The layer was not tested",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert "Appeal filed." in out
    assert "the product does not decide the appeal" in out
    assert last_post(api)["json"] == {"grounds": "The layer was not tested"}
    assert len(posted_key(api)) >= 1


def test_review_appeal_has_no_reviewer_option(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "review",
        "appeal",
        ROUND_ID,
        "--grounds",
        "Wrong",
        "--independent",
        REPLACEMENT_ID,
        "--username",
        "alice",
    )
    assert code == 2
    assert all(method != "POST" for method, _ in api.paths())


def test_review_appeal_refuses_blank_grounds_before_any_request(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "review",
        "appeal",
        ROUND_ID,
        "--grounds",
        "   ",
        "--username",
        "alice",
    )
    assert code == 2
    assert "--grounds must not be blank" in err
    assert all(method != "POST" for method, _ in api.paths())


def test_review_appeal_409_says_no_reviewer_is_eligible(api, capsys):
    api.route(
        "POST",
        APPEAL_PATH,
        status=409,
        body={"detail": {"status": "no_eligible_reviewer"}},
    )
    code, out, err = run(
        capsys,
        "case",
        "review",
        "appeal",
        ROUND_ID,
        "--grounds",
        "Wrong",
        "--username",
        "alice",
    )
    assert code != 0
    assert "no reviewer is eligible for this round" in err


# --- import dry run ---

DRY_RUN_PATH = "/api/v1/cases/import/dry-run"


def dry_run_summary(**counts) -> dict:
    base = {
        "written": False,
        "import_format_version": {"supported": 1, "received": 1, "accepted": True},
        "record_schema_version": "0.1.0",
        "file_errors": [],
        "would_import": 0,
        "duplicate": 0,
        "conflict": 0,
        "rejected": 0,
        "results": [],
    }
    base.update(counts)
    return base


def test_import_dry_run_posts_to_the_dry_run_route_and_prints_the_report(
    api, capsys, tmp_path
):
    summary = dry_run_summary(
        would_import=1,
        results=[
            {
                "index": 0,
                "status": "would_import",
                "source_case_id": None,
                "case_id": None,
                "reference": None,
                "title": "Phishing campaign",
                "awareness_time": "2026-10-30T09:00:00+00:00",
                "errors": [],
            }
        ],
    )
    api.route("POST", DRY_RUN_PATH, body=summary)
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(
        capsys, "case", "import", path, "--username", "alice", "--dry-run"
    )

    assert code == 0
    assert (
        "Dry run finished: 1 would be imported, 0 duplicate, 0 conflict, 0 rejected. Nothing was written."
        in out
    )
    assert 'records[0]: would be imported as "Phishing campaign"' in out
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("POST", DRY_RUN_PATH),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert api.calls[1]["json"] == IMPORT_FILE
    assert not any(
        name.lower() == "idempotency-key" for name in api.calls[1]["headers"]
    )
    assert err == ""


def test_import_dry_run_json_prints_the_api_body_exactly(api, capsys, tmp_path):
    summary = dry_run_summary(duplicate=1)
    api.route("POST", DRY_RUN_PATH, body=summary)
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(
        capsys, "case", "import", path, "--username", "alice", "--dry-run", "--json"
    )

    assert code == 0
    assert out == compact(summary).decode("utf-8") + "\n"


def test_import_dry_run_exits_1_when_a_record_would_be_rejected(api, capsys, tmp_path):
    api.route(
        "POST",
        DRY_RUN_PATH,
        body=dry_run_summary(
            rejected=1,
            results=[
                {
                    "index": 0,
                    "status": "rejected",
                    "source_case_id": None,
                    "case_id": None,
                    "reference": None,
                    "title": None,
                    "awareness_time": None,
                    "errors": ["/record_schema_version: '0.2.0' was expected"],
                }
            ],
        ),
    )
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(
        capsys, "case", "import", path, "--username", "alice", "--dry-run"
    )

    assert code == 1
    assert "records[0]: rejected, nothing written" in out
    assert "/record_schema_version: '0.2.0' was expected" in out
    assert "1 record(s) would not be stored" in err
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


def test_import_dry_run_exits_1_when_the_file_is_not_accepted(api, capsys, tmp_path):
    api.route(
        "POST",
        DRY_RUN_PATH,
        body=dry_run_summary(
            file_errors=["unsupported import_format_version; this version reads 1"],
            import_format_version={"supported": 1, "received": 9, "accepted": False},
        ),
    )
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(
        capsys, "case", "import", path, "--username", "alice", "--dry-run"
    )

    assert code == 1
    assert "file: unsupported import_format_version; this version reads 1" in out
    assert "the import file is not accepted" in err


def test_import_dry_run_with_only_duplicates_exits_0(api, capsys, tmp_path):
    api.route("POST", DRY_RUN_PATH, body=dry_run_summary(duplicate=1))
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(
        capsys, "case", "import", path, "--username", "alice", "--dry-run"
    )

    assert code == 0
    assert "1 duplicate" in out


def test_import_dry_run_refuses_an_idempotency_key_before_any_request(
    api, capsys, tmp_path
):
    path = write_file(tmp_path, json.dumps(IMPORT_FILE).encode("utf-8"))

    code, out, err = run(
        capsys,
        "case",
        "import",
        path,
        "--username",
        "alice",
        "--dry-run",
        "--idempotency-key",
        "k-1",
    )

    assert code == 2
    assert api.calls == []
    assert "does not apply to --dry-run" in err


# --- catalog export ---

CATALOG_EXPORT_PATH = "/api/v1/catalog/export"
CATALOG_BODY = {
    "catalog_export_schema_version": "0.1.0",
    "catalog_export": {"entries": []},
}


def test_catalog_export_prints_the_api_body_with_a_session(api, capsys):
    api.route("GET", CATALOG_EXPORT_PATH, body=CATALOG_BODY)

    code, out, err = run(capsys, "catalog", "export", "--username", "alice")

    assert code == 0
    assert out == compact(CATALOG_BODY).decode("utf-8") + "\n"
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("GET", CATALOG_EXPORT_PATH),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert api.calls[1]["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert err == ""


def test_catalog_export_out_writes_the_same_json_to_the_file(api, capsys, tmp_path):
    api.route("GET", CATALOG_EXPORT_PATH, body=CATALOG_BODY)
    target = tmp_path / "catalog-export.json"

    code, out, err = run(
        capsys, "catalog", "export", "--username", "alice", "--out", str(target)
    )

    assert code == 0
    assert out == ""
    assert target.read_bytes() == compact(CATALOG_BODY) + b"\n"
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


def test_catalog_export_refused_for_a_role_without_catalog_read_is_a_failed_command(
    api, capsys, tmp_path
):
    api.route("GET", CATALOG_EXPORT_PATH, status=403, body={"detail": "forbidden"})

    code, out, err = run(capsys, "catalog", "export", "--username", "alice")

    assert code == 1
    assert out == ""
    assert err.startswith("error:")
    assert api.paths()[-1] == ("POST", "/api/v1/auth/logout")


def test_catalog_export_to_an_unwritable_path_is_a_usage_error(api, capsys, tmp_path):
    api.route("GET", CATALOG_EXPORT_PATH, body=CATALOG_BODY)
    target = tmp_path / "missing-directory" / "catalog-export.json"

    code, out, err = run(
        capsys, "catalog", "export", "--username", "alice", "--out", str(target)
    )

    assert code == 2
    assert "cannot write" in err


# --- title update ---

TITLE_PATH = f"/api/v1/cases/{CASE_ID}"


def title_update_without_revision(*extra: str) -> tuple[str, ...]:
    return (
        "case",
        "title",
        "update",
        CASE_ID,
        "--username",
        "alice",
        *extra,
    )


def test_title_update_without_a_revision_reads_the_current_one_and_sends_it(
    api, capsys
):
    api.route("GET", TITLE_PATH, body=CASE_BODY)
    api.route("PATCH", TITLE_PATH, body={**CASE_BODY, "title": "New title"})
    code, out, err = run(
        capsys, *title_update_without_revision("--title", "  New title  ")
    )
    assert code == 0, err
    assert out.startswith("Case title updated.")
    assert "New title" in out
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("GET", TITLE_PATH),
        ("PATCH", TITLE_PATH),
        ("POST", "/api/v1/auth/logout"),
    ]
    # The title is sent trimmed, as case create sends it.
    assert api.calls[2]["json"] == {"title": "New title"}
    # The revision the case show response carries (3) travels as If-Match.
    sent = {name.lower(): value for name, value in api.calls[2]["headers"].items()}
    assert sent["if-match"] == '"3"'


def test_title_update_with_a_revision_sends_it_without_reading_the_case(api, capsys):
    api.route("PATCH", TITLE_PATH, body={**CASE_BODY, "title": "New title"})
    code, out, err = run(
        capsys,
        "case",
        "title",
        "update",
        CASE_ID,
        "--title",
        "New title",
        "--revision",
        "7",
        "--username",
        "alice",
    )
    assert code == 0, err
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("PATCH", TITLE_PATH),
        ("POST", "/api/v1/auth/logout"),
    ]
    sent = {name.lower(): value for name, value in api.calls[1]["headers"].items()}
    assert sent["if-match"] == '"7"'


def test_title_update_without_a_revision_that_changes_meanwhile_says_so(api, capsys):
    api.route("GET", TITLE_PATH, body=CASE_BODY)
    api.route(
        "PATCH",
        TITLE_PATH,
        status=412,
        body={"detail": {"status": "precondition_failed", "current_revision": 5}},
    )
    code, out, err = run(capsys, *title_update_without_revision("--title", "New"))
    assert code == 1
    assert out == ""
    assert "changed after this command read it, so nothing was saved" in err
    assert f"case show {CASE_ID}" in err
    # No number is offered, because the command read the revision itself.
    assert "--revision" not in err
    assert "current revision" not in err


def test_title_update_with_a_stale_revision_names_the_current_one(api, capsys):
    api.route(
        "PATCH",
        TITLE_PATH,
        status=412,
        body={"detail": {"status": "precondition_failed", "current_revision": 5}},
    )
    code, out, err = run(
        capsys,
        "case",
        "title",
        "update",
        CASE_ID,
        "--title",
        "New",
        "--revision",
        "3",
        "--username",
        "alice",
    )
    assert code == 1
    assert out == ""
    assert "current revision is 5" in err
    assert "--revision 5" in err
    assert f"case show {CASE_ID}" in err


def test_title_update_rejects_a_blank_title_without_a_request(api, capsys):
    code, out, err = run(
        capsys, *title_update_without_revision("--title", "   ")
    )
    assert code == 2
    assert api.calls == []
    assert "the title must be 1 to 200 characters and not only spaces" in err


def test_title_update_rejects_a_title_over_200_characters_without_a_request(api, capsys):
    code, out, err = run(
        capsys, *title_update_without_revision("--title", "x" * 201)
    )
    assert code == 2
    assert api.calls == []
    assert "1 to 200 characters" in err


def test_title_update_rejects_a_revision_below_one_without_a_request(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "title",
        "update",
        CASE_ID,
        "--title",
        "New",
        "--revision",
        "0",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []
    assert "--revision must be 1 or more" in err


def test_title_update_case_id_must_be_a_uuid_before_any_request(api, capsys):
    code, out, err = run(
        capsys,
        "case",
        "title",
        "update",
        "not-a-case",
        "--title",
        "New",
        "--username",
        "alice",
    )
    assert code == 2
    assert api.calls == []
    assert "CASE_ID must be the case ID" in err


def test_title_update_of_a_case_not_available_sends_no_write(api, capsys):
    api.route("GET", TITLE_PATH, status=404, body={"detail": "case not found"})
    code, out, err = run(capsys, *title_update_without_revision("--title", "New"))
    assert code == 1
    assert out == ""
    assert "case not found" in err
    assert not any(call["method"] == "PATCH" for call in api.calls)


def test_title_update_forbidden_names_the_triager_permission(api, capsys):
    api.route("PATCH", TITLE_PATH, status=403, body={"detail": "forbidden"})
    code, out, err = run(
        capsys,
        "case",
        "title",
        "update",
        CASE_ID,
        "--title",
        "New",
        "--revision",
        "3",
        "--username",
        "alice",
    )
    assert code == 1
    assert "only a triager can change the case title" in err
