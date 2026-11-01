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
