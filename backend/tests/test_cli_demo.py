"""Demo sign-in of the container CLI (--demo-username, --demo-credential-file, SAFE_DEMO_*), without a database.

The API is replaced by a fake behind urllib.request.urlopen, as in test_cli_cases.py. The tests check which request the
CLI sends (the demo endpoint, never the password login), what it reads from the credential file, and that the
credential value appears in no output, no error message and no command-line argument. The sign-in against the real API
is in test_demo.py (database tests).
"""

import io
import json
import urllib.error
import urllib.parse

import pytest

from safe_kit import cli

BASE_URL = "http://api.test:8000"
# A fake credential. It is written to a file for the test and must never appear in any output.
SENTINEL = "sentinel-demo-credential-5d0e28a1"
PASSWORD = "sentinel-password-9b71c3e4"
ACCESS = "access-SECRET-token-3c1d"
REFRESH = "refresh-SECRET-token-8e2a"
CASE_ID = "5afe0d00-0000-4000-8000-000000000201"
SIGN_IN = "/api/v1/demo/sign-in"


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


class FakeApi:
    """Answers the CLI's requests. Each request is recorded as (method, path, body)."""

    def __init__(
        self, sign_in_status: int = 200, sign_in_detail: str = "Invalid credentials"
    ):
        self.sign_in_status = sign_in_status
        self.sign_in_detail = sign_in_detail
        self.requests: list[tuple[str, str, dict | None]] = []

    def __call__(self, request, timeout=None):
        split = urllib.parse.urlsplit(request.full_url)
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), split.path, body))
        if split.path == SIGN_IN:
            if self.sign_in_status != 200:
                raise self._error(self.sign_in_status, {"detail": self.sign_in_detail})
            return FakeResponse(json.dumps(self._tokens()).encode("utf-8"))
        if split.path == f"/api/v1/cases/{CASE_ID}":
            return FakeResponse(
                json.dumps({"id": CASE_ID, "title": "Sample case"}).encode("utf-8")
            )
        if split.path == "/api/v1/auth/logout":
            return FakeResponse(b"{}")
        if split.path == "/api/v1/auth/token":
            return FakeResponse(json.dumps(self._tokens()).encode("utf-8"))
        raise AssertionError(f"unexpected request {request.get_method()} {split.path}")

    def _error(self, status: int, body: dict) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "http://api.test",
            status,
            "error",
            None,
            io.BytesIO(json.dumps(body).encode()),
        )

    @staticmethod
    def _tokens() -> dict:
        return {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "token_type": "bearer",
            "expires_in": 900,
            "password_change_required": False,
        }

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]


@pytest.fixture
def api(monkeypatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(cli.urllib.request, "urlopen", fake)
    monkeypatch.setenv(cli.API_URL_ENV, BASE_URL)
    for name in ("SAFE_USERNAME", cli.DEMO_USERNAME_ENV, cli.DEMO_CREDENTIAL_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(PASSWORD + "\n"))
    return fake


def _credential_file(tmp_path, content: str, name: str = "credential") -> str:
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8"))
    return str(path)


def _show(*extra: str) -> list[str]:
    return ["--no-banner", "case", "show", CASE_ID, "--json", *extra]


def test_demo_sign_in_uses_the_first_line_only_and_never_the_password_login(
    api, tmp_path, capsys
):
    path = _credential_file(
        tmp_path, f"  {SENTINEL}  \nthe second line is never read\n"
    )
    argv = _show("--demo-username", "demo-investigator", "--demo-credential-file", path)

    assert cli.main(argv) == 0

    assert api.requests[0] == (
        "POST",
        SIGN_IN,
        {"username": "demo-investigator", "credential": SENTINEL},
    )
    assert "/api/v1/auth/token" not in api.paths()
    assert api.paths() == [SIGN_IN, f"/api/v1/cases/{CASE_ID}", "/api/v1/auth/logout"]
    output = capsys.readouterr()
    assert json.loads(output.out)["id"] == CASE_ID
    assert SENTINEL not in output.out + output.err
    assert "second line" not in output.out + output.err


def test_environment_variables_select_the_demo_sign_in(
    api, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(cli.DEMO_USERNAME_ENV, "demo-reviewer")
    monkeypatch.setenv(
        cli.DEMO_CREDENTIAL_FILE_ENV, _credential_file(tmp_path, SENTINEL + "\n")
    )

    assert cli.main(_show()) == 0

    assert api.requests[0] == (
        "POST",
        SIGN_IN,
        {"username": "demo-reviewer", "credential": SENTINEL},
    )
    assert SENTINEL not in capsys.readouterr().out


def test_the_password_login_is_used_when_username_is_given_even_with_demo_variables(
    api, tmp_path, monkeypatch
):
    monkeypatch.setenv(cli.DEMO_USERNAME_ENV, "demo-reviewer")
    monkeypatch.setenv(
        cli.DEMO_CREDENTIAL_FILE_ENV, _credential_file(tmp_path, SENTINEL + "\n")
    )

    assert cli.main(_show("--username", "someone")) == 0

    assert api.requests[0] == (
        "POST",
        "/api/v1/auth/token",
        {"username": "someone", "password": PASSWORD},
    )
    assert SENTINEL not in " ".join(api.paths())


def test_a_missing_credential_file_is_refused_before_any_request(api, tmp_path, capsys):
    missing = str(tmp_path / "no-such-file.credential")

    assert (
        cli.main(
            _show(
                "--demo-username",
                "demo-investigator",
                "--demo-credential-file",
                missing,
            )
        )
        == 2
    )

    assert api.requests == []
    err = capsys.readouterr().err
    assert "the demo credential file is missing" in err and missing in err


def test_a_credential_path_that_is_not_readable_is_refused(api, tmp_path, capsys):
    # A directory cannot be read as a file (the same refusal as a file without read permission).
    assert (
        cli.main(
            _show(
                "--demo-username",
                "demo-investigator",
                "--demo-credential-file",
                str(tmp_path),
            )
        )
        == 2
    )

    assert api.requests == []
    assert "cannot be read" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    ["", "\n" + SENTINEL + "\n", "   \n" + SENTINEL, "x" * 300 + "\n", b"\xff\xfe\n"],
)
def test_a_file_without_a_usable_first_line_is_refused(api, tmp_path, capsys, content):
    if isinstance(content, bytes):
        path = tmp_path / "credential"
        path.write_bytes(content)
        path = str(path)
    else:
        path = _credential_file(tmp_path, content)

    assert (
        cli.main(
            _show(
                "--demo-username", "demo-investigator", "--demo-credential-file", path
            )
        )
        == 2
    )

    assert api.requests == []
    output = capsys.readouterr()
    assert "has no usable first line" in output.err
    assert SENTINEL not in output.out + output.err


@pytest.mark.parametrize(
    "options",
    [
        ["--demo-username", "demo-investigator"],
        ["--demo-credential-file", "PATH"],
    ],
)
def test_a_demo_option_needs_its_partner(api, tmp_path, capsys, options):
    path = _credential_file(tmp_path, SENTINEL + "\n")
    options = [path if value == "PATH" else value for value in options]

    assert cli.main(_show(*options)) == 2

    assert api.requests == []
    assert "needs a demo username and a credential file" in capsys.readouterr().err


def test_a_password_login_and_a_demo_sign_in_cannot_be_combined(api, tmp_path, capsys):
    path = _credential_file(tmp_path, SENTINEL + "\n")

    code = cli.main(
        _show(
            "--username",
            "someone",
            "--demo-username",
            "demo-investigator",
            "--demo-credential-file",
            path,
        )
    )

    assert code == 2
    assert api.requests == []
    assert "not both" in capsys.readouterr().err


def test_a_wrong_credential_gets_the_normal_refusal_and_no_secret_text(
    api, tmp_path, capsys
):
    api.sign_in_status = 401
    api.sign_in_detail = "Invalid credentials"
    path = _credential_file(tmp_path, SENTINEL + "\n")

    code = cli.main(
        _show("--demo-username", "demo-investigator", "--demo-credential-file", path)
    )

    assert code == 1
    assert api.paths() == [SIGN_IN]
    output = capsys.readouterr()
    assert output.err == "error: invalid credentials\n"
    assert output.out == ""
    assert SENTINEL not in output.out + output.err


def test_a_non_demo_username_is_refused_by_the_server_and_the_cli_reports_it(
    api, tmp_path, capsys
):
    # The API answers a name outside the demo roles with the same 401 as a wrong credential.
    api.sign_in_status = 401
    path = _credential_file(tmp_path, SENTINEL + "\n")

    code = cli.main(
        _show("--demo-username", "someone-else", "--demo-credential-file", path)
    )

    assert code == 1
    assert api.requests == [
        ("POST", SIGN_IN, {"username": "someone-else", "credential": SENTINEL})
    ]
    assert capsys.readouterr().err == "error: invalid credentials\n"


def test_a_refused_demo_sign_in_names_the_reason_without_the_response_body(
    api, tmp_path, capsys
):
    api.sign_in_status = 403
    api.sign_in_detail = "demo sign-in is not available"
    path = _credential_file(tmp_path, SENTINEL + "\n")

    code = cli.main(
        _show("--demo-username", "demo-investigator", "--demo-credential-file", path)
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "demo sign-in is not available: the server is not in demo mode" in err
    assert SENTINEL not in err


def test_the_credential_is_absent_from_the_command_line_the_cli_was_given(
    api, tmp_path
):
    path = _credential_file(tmp_path, SENTINEL + "\n")
    argv = _show("--demo-username", "demo-investigator", "--demo-credential-file", path)

    assert cli.main(argv) == 0

    assert SENTINEL not in " ".join(argv)
    assert path in argv
