"""Runs the real CLI against a FastAPI TestClient, for the parity tests (plans/f0/api-draft.md section 3.6).

The CLI's urllib.request.urlopen is replaced, so every HTTP call the CLI makes reaches the app through the TestClient.
The CLI itself is not changed. The login is either the real login endpoint (the account store decides) or fixed token
fields (for a test that needs a token the store would not issue, such as an expired or revoked one).
"""

import getpass
import io
import json
import sys
import urllib.error
import urllib.parse

from safe_kit import cli

BASE_URL = "http://api.test:8000"
LOGIN_PATH = "/api/v1/auth/token"
LOGOUT_PATH = "/api/v1/auth/logout"


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _no_prompt(prompt: str = "") -> str:
    raise AssertionError("getpass must not be used when stdin is not a terminal")


def install(
    monkeypatch,
    client,
    *,
    issued: dict | None = None,
    logout_status: int | None = None,
    base_url: str = BASE_URL,
) -> None:
    """Route the CLI's HTTP calls into client.

    With issued, the login answers with those token fields instead of the real login. With logout_status, the logout
    is answered with that status and never reaches the app: the tests that fix the login do not need a real session.
    """
    monkeypatch.setenv("SAFE_API_URL", base_url)
    monkeypatch.delenv("SAFE_USERNAME", raising=False)
    monkeypatch.setattr(getpass, "getpass", _no_prompt)

    def urlopen(request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        method = request.get_method()
        if issued is not None and (method, parts.path) == ("POST", LOGIN_PATH):
            return _Response(200, json.dumps(issued).encode("utf-8"))
        if logout_status is not None and (method, parts.path) == ("POST", LOGOUT_PATH):
            return _Response(logout_status, b"")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        body = json.loads(request.data) if request.data else None
        headers = {}
        if request.has_header("Authorization"):
            headers["Authorization"] = request.get_header("Authorization")
        if request.has_header("Idempotency-key"):
            headers["Idempotency-Key"] = request.get_header("Idempotency-key")
        if request.has_header("If-match"):
            headers["If-Match"] = request.get_header("If-match")
        response = client.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status_code,
                "error",
                {},
                io.BytesIO(response.content),
            )
        return _Response(response.status_code, response.content)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)


def run(capsys, monkeypatch, password: str, *argv: str) -> tuple[int, str, str]:
    """One CLI run. The password is read from stdin, as a script would pipe it."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(password + "\n"))
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err
