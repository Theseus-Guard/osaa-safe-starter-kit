"""Evidence and task subcommands of the container CLI. The API is replaced by a fake behind urllib.request.urlopen."""

import getpass
import io
import json
import urllib.error
import urllib.parse

import pytest

from safe_kit import cli

BASE_URL = "http://api.test:8000"
ACCESS = "access-SECRET-token-4d2b"
REFRESH = "refresh-SECRET-token-8e6a"
PASSWORD = "pw-SECRET-evidence-passphrase"
CASE_ID = "11111111-2222-4333-8444-555555555555"
EVIDENCE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PDF = b"%PDF-1.7\n" + b"field notes " * 20 + b"\n%%EOF\n"

EVIDENCE_BODY = {
    "id": EVIDENCE_ID,
    "case_id": CASE_ID,
    "uploaded_by": "55555555-5555-4555-8555-555555555555",
    "original_filename": "scan.pdf",
    "media_type": "application/pdf",
    "size_bytes": len(PDF),
    "sha256_plaintext": "ab" * 32,
    "status": "available",
    "created_at": "2026-11-01T10:00:00Z",
}
CASE_BODY = {
    "id": CASE_ID,
    "reference": "SAFE-2026-000001",
    "title": "Phishing campaign",
    "awareness_time": "2026-10-30T09:00:00Z",
    "status": "open",
    "calendar_version": "2026.1",
    "created_at": "2026-10-31T10:00:00Z",
    "deadlines": [
        {
            "id": "dddddddd-1111-4222-8333-444444444444",
            "kind": "initial_notice",
            "due_at": "2026-11-01T09:00:00Z",
            "status": "pending",
            "not_applicable_reason": None,
        },
        {
            "id": None,
            "kind": "follow_up",
            "due_at": "2026-11-03T09:00:00Z",
            "status": "not_applicable",
            "not_applicable_reason": "no external party",
        },
    ],
}
EVIDENCE_PAGE = {"items": [EVIDENCE_BODY], "total": 1, "limit": 20, "offset": 0}


def compact(body) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def token_body() -> dict:
    return {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "token_type": "bearer",
        "expires_in": 900,
        "password_change_required": False,
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
    """Routes (method, path) to a canned answer. Request bodies are kept as raw bytes. An unrouted request fails."""

    def __init__(self, monkeypatch):
        self.routes: dict[tuple[str, str], tuple[int, bytes]] = {}
        self.calls: list[dict] = []
        monkeypatch.setattr(cli.urllib.request, "urlopen", self._urlopen)
        self.route("POST", "/api/v1/auth/token", body=token_body())
        self.route("POST", "/api/v1/auth/logout", status=204)

    def route(self, method: str, path: str, status: int = 200, body=None):
        if body is None:
            encoded = b""
        elif isinstance(body, bytes):
            encoded = body
        else:
            encoded = compact(body)
        self.routes[(method, path)] = (status, encoded)

    def _urlopen(self, request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        method = request.get_method()
        self.calls.append(
            {
                "method": method,
                "path": parts.path,
                "query": urllib.parse.parse_qs(parts.query),
                "authorization": request.get_header("Authorization"),
                "content_type": request.get_header("Content-type"),
                "data": request.data,
            }
        )
        key = (method, parts.path)
        if key not in self.routes:
            raise AssertionError(f"no fake route for {method} {parts.path}")
        status, body = self.routes[key]
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(body))
        return FakeResponse(body)

    def paths(self) -> list[tuple[str, str]]:
        return [(call["method"], call["path"]) for call in self.calls]

    def api_calls(self) -> list[dict]:
        """The calls made after the login, not counting the login and the logout."""
        return [call for call in self.calls if call["path"].startswith("/api/v1/cases")]


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("SAFE_API_URL", BASE_URL)
    monkeypatch.delenv("SAFE_USERNAME", raising=False)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(PASSWORD + "\n"))

    def no_prompt(prompt=""):
        raise AssertionError("getpass must not be used when stdin is not a terminal")

    monkeypatch.setattr(getpass, "getpass", no_prompt)
    return FakeAPI(monkeypatch)


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def pdf_file(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(PDF)
    return str(path)


def add_args(path: str, *extra: str) -> tuple[str, ...]:
    return ("evidence", "add", CASE_ID, path, "--username", "ines", *extra)


def list_args(*extra: str) -> tuple[str, ...]:
    return ("evidence", "list", CASE_ID, "--username", "ines", *extra)


def task_args(*extra: str) -> tuple[str, ...]:
    return ("task", "list", CASE_ID, "--username", "ines", *extra)


# --- evidence add ---


def test_add_uploads_the_file_as_multipart_and_prints_the_item(api, capsys, pdf_file):
    api.route("POST", f"/api/v1/cases/{CASE_ID}/evidence", status=201, body=EVIDENCE_BODY)
    code, out, err = run(capsys, *add_args(pdf_file))
    assert code == 0 and err == ""
    assert out.splitlines()[0] == "Evidence stored."
    assert f"Evidence {EVIDENCE_ID}" in out
    assert "Status:           available" in out
    upload = api.api_calls()[0]
    assert (upload["method"], upload["path"]) == ("POST", f"/api/v1/cases/{CASE_ID}/evidence")
    assert upload["authorization"] == f"Bearer {ACCESS}"
    assert upload["content_type"].startswith("multipart/form-data; boundary=")
    boundary = upload["content_type"].split("boundary=", 1)[1]
    assert upload["data"].startswith(f"--{boundary}\r\n".encode())
    assert b'name="file"; filename="scan.pdf"' in upload["data"]
    assert b"Content-Type: application/pdf" in upload["data"]
    assert PDF in upload["data"]
    assert upload["data"].endswith(f"\r\n--{boundary}--\r\n".encode())


def test_add_json_prints_the_api_body_exactly(api, capsys, pdf_file):
    api.route("POST", f"/api/v1/cases/{CASE_ID}/evidence", status=201, body=EVIDENCE_BODY)
    code, out, _ = run(capsys, *add_args(pdf_file, "--json"))
    assert code == 0
    assert out == compact(EVIDENCE_BODY).decode() + "\n"


def test_add_never_prints_the_token_or_the_password(api, capsys, pdf_file):
    api.route("POST", f"/api/v1/cases/{CASE_ID}/evidence", status=201, body=EVIDENCE_BODY)
    code, out, err = run(capsys, *add_args(pdf_file))
    assert code == 0
    for secret in (ACCESS, REFRESH, PASSWORD):
        assert secret not in out and secret not in err


def test_add_sends_an_unknown_extension_as_octet_stream_and_leaves_the_decision_to_the_api(api, capsys, tmp_path):
    path = tmp_path / "capture.bin"
    path.write_bytes(b"\x00\x01binary")
    api.route("POST", f"/api/v1/cases/{CASE_ID}/evidence", status=201, body=EVIDENCE_BODY)
    assert run(capsys, *add_args(str(path)))[0] == 0
    assert b"Content-Type: application/octet-stream" in api.api_calls()[0]["data"]


def test_add_replaces_quotes_in_the_file_name_so_the_part_header_stays_valid(api, capsys, tmp_path):
    path = tmp_path / 'notes "draft".txt'
    path.write_bytes(b"plain text")
    api.route("POST", f"/api/v1/cases/{CASE_ID}/evidence", status=201, body=EVIDENCE_BODY)
    assert run(capsys, *add_args(str(path)))[0] == 0
    assert b'filename="notes _draft_.txt"' in api.api_calls()[0]["data"]


def test_add_with_a_bad_case_id_is_a_usage_error_without_any_request(api, capsys, pdf_file):
    code, _, err = run(capsys, "evidence", "add", "not-a-uuid", pdf_file, "--username", "ines")
    assert code == 2
    assert "CASE_ID must be the case ID" in err
    assert api.calls == []


def test_add_with_a_missing_file_is_a_usage_error_without_a_login(api, capsys, tmp_path):
    code, _, err = run(capsys, *add_args(str(tmp_path / "absent.pdf")))
    assert code == 2
    assert "cannot read" in err
    assert api.calls == []


def test_add_with_an_empty_file_is_a_usage_error_without_a_login(api, capsys, tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    code, _, err = run(capsys, *add_args(str(path)))
    assert code == 2
    assert "is empty" in err
    assert api.calls == []


def test_add_refuses_a_file_over_the_default_limit_before_any_request(api, capsys, pdf_file, monkeypatch):
    monkeypatch.setattr(cli, "MAX_EVIDENCE_FILE_BYTES", len(PDF) - 1)
    code, _, err = run(capsys, *add_args(pdf_file))
    assert code == 2
    assert "larger than" in err
    assert api.calls == []


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (415, "the file type is not allowed. Allowed types: PDF, PNG, JPEG, plain text and JSON"),
        (413, "the file is larger than the server allows"),
        (409, "the upload expired before it finished; try again"),
        (500, "the evidence could not be stored; it was not recorded as available"),
        (503, "evidence storage is not configured on the server"),
    ],
)
def test_add_maps_upload_failures_to_their_messages(api, capsys, pdf_file, status, message):
    api.route("POST", f"/api/v1/cases/{CASE_ID}/evidence", status=status, body=b'{"detail":"x"}')
    code, out, err = run(capsys, *add_args(pdf_file))
    assert code == 1
    assert err == f"error: {message}\n"
    assert out == ""


def test_add_to_a_case_the_user_cannot_see_says_case_not_found(api, capsys, pdf_file):
    api.route("POST", f"/api/v1/cases/{CASE_ID}/evidence", status=404, body=b'{"detail":"case not found"}')
    code, _, err = run(capsys, *add_args(pdf_file))
    assert code == 1
    assert "error: case not found" in err


# --- evidence list ---


def test_list_sends_the_paging_arguments_and_prints_the_page(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}/evidence", body=EVIDENCE_PAGE)
    code, out, _ = run(capsys, *list_args("--limit", "5", "--offset", "3"))
    assert code == 0
    call = api.api_calls()[0]
    assert call["query"] == {"limit": ["5"], "offset": ["3"]}
    lines = out.splitlines()
    assert lines[0].split() == ["ID", "STATUS", "MEDIA", "TYPE", "SIZE", "(BYTES)", "FILE"]
    assert EVIDENCE_ID in lines[1] and "available" in lines[1] and "scan.pdf" in lines[1]
    assert lines[-1] == "Showing 1-1 of 1"


def test_list_uses_the_default_page_size(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}/evidence", body={"items": [], "total": 0, "limit": 20, "offset": 0})
    run(capsys, *list_args())
    assert api.api_calls()[0]["query"] == {"limit": ["20"], "offset": ["0"]}


def test_list_of_an_empty_case_says_so(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}/evidence", body={"items": [], "total": 0, "limit": 20, "offset": 0})
    code, out, _ = run(capsys, *list_args())
    assert code == 0
    assert out == "No evidence for this case. Total: 0\n"


def test_list_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}/evidence", body=EVIDENCE_PAGE)
    code, out, _ = run(capsys, *list_args("--json"))
    assert code == 0
    assert out == compact(EVIDENCE_PAGE).decode() + "\n"


@pytest.mark.parametrize("extra", [("--limit", "0"), ("--limit", "201"), ("--offset", "-1")])
def test_list_rejects_out_of_range_paging_without_a_request(api, capsys, extra):
    code, _, err = run(capsys, *list_args(*extra))
    assert code == 2
    assert err.startswith("error: --")
    assert api.calls == []


def test_list_with_a_bad_case_id_is_a_usage_error_without_any_request(api, capsys):
    code, _, _ = run(capsys, "evidence", "list", "not-a-uuid", "--username", "ines")
    assert code == 2
    assert api.calls == []


# --- task list ---


def test_task_list_prints_the_deadline_tasks_of_the_case(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}", body=CASE_BODY)
    code, out, err = run(capsys, *task_args())
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == "Deadline tasks for case SAFE-2026-000001:"
    assert lines[1].split() == ["ID", "KIND", "DUE", "(UTC)", "STATUS", "NOTE"]
    assert "dddddddd-1111-4222-8333-444444444444" in lines[2] and "initial_notice" in lines[2]
    assert "2026-11-01T09:00:00Z" in lines[2] and "pending" in lines[2]
    assert lines[3].split()[0] == "-" and "not_applicable" in lines[3] and "no external party" in lines[3]


def test_task_list_uses_the_case_request_and_ends_the_session(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}", body=CASE_BODY)
    run(capsys, *task_args())
    assert api.paths() == [
        ("POST", "/api/v1/auth/token"),
        ("GET", f"/api/v1/cases/{CASE_ID}"),
        ("POST", "/api/v1/auth/logout"),
    ]


def test_task_list_json_prints_the_api_body_exactly(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}", body=CASE_BODY)
    code, out, _ = run(capsys, *task_args("--json"))
    assert code == 0
    assert out == compact(CASE_BODY).decode() + "\n"


def test_task_list_of_a_case_without_tasks_says_so(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}", body={**CASE_BODY, "deadlines": []})
    code, out, _ = run(capsys, *task_args())
    assert code == 0
    assert out == "No deadline tasks for case SAFE-2026-000001.\n"


def test_task_list_of_an_unknown_case_reports_case_not_found(api, capsys):
    api.route("GET", f"/api/v1/cases/{CASE_ID}", status=404, body=b'{"detail":"case not found"}')
    code, _, err = run(capsys, *task_args())
    assert code == 1
    assert err == "error: case not found\n"


def test_task_list_with_a_bad_case_id_is_a_usage_error_without_any_request(api, capsys):
    code, _, err = run(capsys, "task", "list", "not-a-uuid", "--username", "ines")
    assert code == 2
    assert "CASE_ID must be the case ID" in err
    assert api.calls == []


TASK_PATH = f"/api/v1/cases/{CASE_ID}/deadlines/dddddddd-1111-4222-8333-444444444444"


def task_update_args(*extra: str) -> tuple[str, ...]:
    return ("task", "update", CASE_ID, "dddddddd-1111-4222-8333-444444444444", "--username", "ines", *extra)


def test_task_update_not_applicable_sends_the_trimmed_reason_and_prints_the_tasks(api, capsys):
    api.route("PATCH", TASK_PATH, body=CASE_BODY)
    code, out, err = run(capsys, *task_update_args("--status", "not-applicable", "--reason", "  No external party  "))
    assert code == 0, err
    assert out.startswith("Deadline task updated.")
    (patch,) = [call for call in api.calls if call["method"] == "PATCH"]
    assert json.loads(patch["data"]) == {"status": "not_applicable", "reason": "No external party"}


def test_task_update_open_sends_no_reason(api, capsys):
    api.route("PATCH", TASK_PATH, body=CASE_BODY)
    code, _, err = run(capsys, *task_update_args("--status", "open"))
    assert code == 0, err
    (patch,) = [call for call in api.calls if call["method"] == "PATCH"]
    assert json.loads(patch["data"]) == {"status": "open", "reason": None}


@pytest.mark.parametrize(
    "extra",
    [("--status", "not-applicable"), ("--status", "not-applicable", "--reason", "   ")],
)
def test_task_update_not_applicable_needs_a_reason_before_any_request(api, capsys, extra):
    code, _, err = run(capsys, *task_update_args(*extra))
    assert code == 2
    assert "--reason is required when --status is not-applicable" in err
    assert api.calls == []


def test_task_update_refuses_a_reason_for_an_open_or_done_task(api, capsys):
    code, _, err = run(capsys, *task_update_args("--status", "done", "--reason", "Not needed"))
    assert code == 2
    assert "--reason is only allowed with --status not-applicable" in err
    assert api.calls == []


def test_task_restore_posts_to_the_restore_route_and_prints_the_tasks(api, capsys):
    api.route("POST", f"{TASK_PATH}/restore", body=CASE_BODY)
    code, out, err = run(capsys, "task", "restore", CASE_ID, "dddddddd-1111-4222-8333-444444444444", "--username", "ines")
    assert code == 0, err
    assert out.startswith("Deadline task restored to the status it had before.")
    assert ("POST", f"{TASK_PATH}/restore") in api.paths()


def test_task_restore_of_a_task_that_is_not_not_applicable_says_so(api, capsys):
    api.route(
        "POST", f"{TASK_PATH}/restore", status=409,
        body={"detail": {"status": "deadline_not_marked_not_applicable"}},
    )
    code, _, err = run(capsys, "task", "restore", CASE_ID, "dddddddd-1111-4222-8333-444444444444", "--username", "ines")
    assert code != 0
    assert "the deadline is not marked not applicable" in err


# --- scope of the parity set ---


def test_case_update_is_not_a_command(api, capsys):
    # The plan lists case update as a candidate without defining what it changes, so it is not implemented.
    code, _, _ = run(capsys, "case", "update", CASE_ID, "--username", "ines")
    assert code == 2
    assert api.calls == []


def test_case_show_still_uses_the_shared_case_id_check(api, capsys):
    code, _, err = run(capsys, "case", "show", "not-a-uuid", "--username", "ines")
    assert code == 2
    assert "CASE_ID must be the case ID (a UUID) shown by 'case list'" in err
    assert api.calls == []
