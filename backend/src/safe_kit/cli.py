"""Container CLI. Calls the shared API and never accesses the database directly."""

import argparse
import getpass
import hashlib
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime


TITLE = "OSAA | SAFE Starter-Kit"
API_URL_ENV = "SAFE_API_URL"
DEFAULT_API_URL = "http://api:8000"
REQUEST_TIMEOUT_SECONDS = 10
# Demo sign-in (demo mode only): the demo role and the file that holds its credential. The API decides who may use it.
DEMO_USERNAME_ENV = "SAFE_DEMO_USERNAME"
DEMO_CREDENTIAL_FILE_ENV = "SAFE_DEMO_CREDENTIAL_FILE"
DEMO_SIGN_IN_PATH = "/api/v1/demo/sign-in"
# The API accepts at most 256 characters for a credential. Only the first line of the file is read, and at most this
# many bytes of it, so a large or binary file is never loaded in full.
DEMO_CREDENTIAL_MAX_LENGTH = 256
DEMO_CREDENTIAL_READ_LIMIT = 1024

# Same limits as the API and the web UI (the web UI pages cases 20 at a time).
MAX_TITLE_LENGTH = 200
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200
# The case status values, the same as safe_kit.cases.CaseStatus. The CLI does not import the backend package, so the
# list is written out here. A test checks it against the API.
CASE_STATUS_CHOICES = ("open", "closed")
# Same limit as the API (safe_kit.case_import.MAX_IMPORT_BYTES). The CLI does not import the backend package.
MAX_IMPORT_FILE_BYTES = 1024 * 1024
# Same default as the API (SAFE_EVIDENCE_MAX_BYTES). The server sets the real limit and answers 413 when it is lower.
MAX_EVIDENCE_FILE_BYTES = 50 * 1024 * 1024

# The values the API accepts for the case update commands. The API checks them again.
REVIEW_OUTCOMES = ("pass", "fail", "insufficient_evidence", "not_applicable")
# The eleven layers of a review round, as the API names them.
REVIEW_LAYERS = (
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
REVIEW_PRECONDITION_FAILED = "precondition_failed"
CLASSIFICATION_TRIGGERS = (
    "unauthorized_access_or_change",
    "boundary_escape",
    "confidential_data_exposure",
    "out_of_scope_continuation",
    "harm_inaction_near_miss",
    "none_applies",
)
EVENT_KINDS = ("incident", "near_miss", "blocked_attempt")
# Model record values, the same as safe_kit.model_records and the API.
# Identifiers are letters, digits and . _ : / @ + -.
REASONING_MODES = ("disabled", "enabled", "unknown")
REASONING_EFFORTS = ("low", "medium", "high", "max")
REASONING_BUDGET_LIMIT = 10_000_000
MODEL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
IMPACT_AREAS = (
    "affected_people_groups",
    "severity",
    "scale",
    "duration",
    "reversibility",
    "disproportionate_effect",
)
IMPACT_SEVERITIES = ("none", "low", "medium", "high", "critical", "unknown")
AWARENESS_KINDS = ("operator", "affected_party", "external_report")
LEGAL_DUTY_KINDS = ("statutory", "contractual")
LEGAL_DUTY_STATUSES = ("open", "done")
# The API answers a stale classification save with this status in a structured 409 detail, next to current_version.
CLASSIFICATION_VERSION_CONFLICT = "classification_version_conflict"
IMPACT_VERSION_CONFLICT = "impact_version_conflict"


class UsageError(Exception):
    """Wrong arguments or input. Exit code 2."""


class RequestFailed(Exception):
    """The API or the login failed. The message is shown to the user. Exit code 1.

    status and detail are set when the API answered with an error. Only the command that asked for a clearer
    message reads them. The detail is the API's detail: a short text, or a structured object with a status string
    (see _error_detail). It is never a request body.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: str | dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


def banner(color: bool) -> str:
    """Boxed banner. Colors are ANSI escape codes and are used only when color is True."""
    border = "+" + "-" * (len(TITLE) + 4) + "+"
    if not color:
        return "\n".join([border, f"|  {TITLE}  |", border])
    cyan, bold, reset = "\033[36m", "\033[1m", "\033[0m"
    return "\n".join(
        [
            f"{cyan}{border}{reset}",
            f"{cyan}|{reset}  {bold}{TITLE}{reset}  {cyan}|{reset}",
            f"{cyan}{border}{reset}",
        ]
    )


def health(base_url: str) -> int:
    url = f"{base_url}/api/v1/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            result = {
                "status": json.load(response)["status"],
                "http_status": response.status,
            }
    except urllib.error.HTTPError as error:
        result = {"status": "unavailable", "http_status": error.code}
    except urllib.error.URLError:
        result = {"status": "unreachable", "http_status": None}
    print(json.dumps(result))
    return 0 if result["status"] == "ok" else 1


HTTP_MESSAGES = {
    401: "invalid credentials",
    403: "forbidden or password change required",
    404: "case not found",
    409: "system timezone is not configured",
    400: "check the values you entered",
    422: "check the values you entered",
}


EVIDENCE_HTTP_MESSAGES = {
    409: "the upload expired before it finished; try again",
    413: "the file is larger than the server allows",
    415: "the file type is not allowed. Allowed types: PDF, PNG, JPEG, plain text and JSON",
    500: "the evidence could not be stored; it was not recorded as available",
    503: "evidence storage is not configured on the server",
}


def _http_message(status: int, messages: dict[int, str] | None = None) -> str:
    if messages and status in messages:
        return messages[status]
    return HTTP_MESSAGES.get(status, f"request failed (HTTP {status})")


def _error_detail(error: urllib.error.HTTPError) -> str | dict | None:
    """The "detail" of an API error body: its text, or a structured object that has a status. None otherwise.

    The body is not kept. Only the detail is returned.
    """
    try:
        body = json.loads(error.read())
    except (OSError, ValueError):
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and isinstance(detail.get("status"), str):
        return detail
    return None


def _call(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict | None = None,
    query: dict | None = None,
    raw_body: bytes | None = None,
    content_type: str | None = None,
    messages: dict[int, str] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    """One API request. Returns the raw response body. Raises RequestFailed for any non-2xx or transport error.

    payload is sent as JSON. raw_body is sent unchanged with content_type (multipart uploads). messages overrides
    the default text for the status codes it lists. extra_headers adds headers such as Idempotency-Key.
    """
    url = base_url + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json", **(extra_headers or {})}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        headers["Content-Type"] = content_type or "application/octet-stream"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise RequestFailed(
            _http_message(error.code, messages),
            status=error.code,
            detail=_error_detail(error),
        ) from None
    except OSError:
        raise RequestFailed("the server could not be reached") from None


def _json_object(raw: bytes) -> dict:
    try:
        body = json.loads(raw)
    except ValueError:
        raise RequestFailed("unexpected response from the server") from None
    if not isinstance(body, dict):
        raise RequestFailed("unexpected response from the server")
    return body


def _read_password() -> str:
    """Prompt when stdin is a terminal. Otherwise read one line from stdin, so scripts can pipe the password."""
    if sys.stdin is not None and sys.stdin.isatty():
        password = getpass.getpass("Password: ")
    else:
        line = sys.stdin.readline() if sys.stdin is not None else ""
        password = line.rstrip("\r\n")
    if not password:
        raise UsageError(
            "a password is required: enter it at the prompt or pipe it on stdin"
        )
    return password


def _username(args: argparse.Namespace) -> str:
    username = (args.username or os.environ.get("SAFE_USERNAME", "")).strip()
    if not username:
        raise UsageError("a username is required: use --username or set SAFE_USERNAME")
    return username


DEMO_SIGN_IN_HTTP_MESSAGES = {
    401: "invalid credentials",
    403: "demo sign-in is not available: the server is not in demo mode, or this address is not allowed",
    429: "too many login attempts; wait and try again",
}


def _demo_sign_in_options(args: argparse.Namespace) -> tuple[str, str] | None:
    """The demo username and credential file when this command signs in as a demo role, otherwise None.

    The command-line options win over their environment variables. Giving --username selects a password sign-in
    unless a demo option is also given, which is refused. Demo sign-in needs both values.
    """
    if args.demo_username is not None or args.demo_credential_file is not None:
        if args.username:
            raise UsageError(
                "use --username for a password sign-in or the demo options for a demo sign-in, not both"
            )
        username = (args.demo_username or "").strip()
        path = (args.demo_credential_file or "").strip()
    else:
        if args.username:
            return None
        username = os.environ.get(DEMO_USERNAME_ENV, "").strip()
        path = os.environ.get(DEMO_CREDENTIAL_FILE_ENV, "").strip()
        if not username and not path:
            return None
    if not username or not path:
        raise UsageError(
            "demo sign-in needs a demo username and a credential file: "
            "--demo-username and --demo-credential-file (or SAFE_DEMO_USERNAME and SAFE_DEMO_CREDENTIAL_FILE)"
        )
    return username, path


def _read_demo_credential(path: str) -> str:
    """The first line of the demo credential file, without surrounding whitespace.

    Refusals name the file, never its content. The value is returned to the caller and is not stored anywhere else.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(DEMO_CREDENTIAL_READ_LIMIT)
    except FileNotFoundError:
        raise UsageError(f"the demo credential file is missing: {path}") from None
    except OSError:
        raise UsageError(f"the demo credential file cannot be read: {path}") from None
    try:
        credential = head.split(b"\n", 1)[0].decode("utf-8").strip()
    except UnicodeDecodeError:
        raise UsageError(
            f"the demo credential file has no usable first line: {path}"
        ) from None
    if not credential or len(credential) > DEMO_CREDENTIAL_MAX_LENGTH:
        raise UsageError(f"the demo credential file has no usable first line: {path}")
    return credential


def _demo_sign_in(base_url: str, username: str, credential_file: str) -> dict:
    """Signs in one demo role through POST /api/v1/demo/sign-in. The credential is sent once and not kept."""
    credential = _read_demo_credential(credential_file)
    return _json_object(
        _call(
            base_url,
            "POST",
            DEMO_SIGN_IN_PATH,
            payload={"username": username, "credential": credential},
            messages=DEMO_SIGN_IN_HTTP_MESSAGES,
        )
    )


def _with_session(base_url: str, args: argparse.Namespace, action) -> None:
    """Log in once, run action with the access token, then end the session.

    The tokens are kept in memory for this command only. They are never printed or written to a file.
    A demo sign-in (see _demo_sign_in_options) uses the demo endpoint instead of the password login.
    """
    demo = _demo_sign_in_options(args)
    if demo is not None:
        tokens = _demo_sign_in(base_url, *demo)
    else:
        username = _username(args)
        password = _read_password()
        tokens = _json_object(
            _call(
                base_url,
                "POST",
                "/api/v1/auth/token",
                payload={"username": username, "password": password},
            )
        )
    if not isinstance(tokens.get("access_token"), str) or not isinstance(
        tokens.get("refresh_token"), str
    ):
        raise RequestFailed("unexpected response from the server")
    try:
        if tokens.get("password_change_required") is True:
            raise RequestFailed(
                "you must change your password before continuing: change it in the web UI, then run this command again"
            )
        action(base_url, tokens["access_token"])
    finally:
        # Best effort: the session also expires on its own, so a failed logout is not reported.
        try:
            _call(
                base_url,
                "POST",
                "/api/v1/auth/logout",
                token=tokens["access_token"],
                payload={"refresh_token": tokens["refresh_token"]},
            )
        except RequestFailed:
            pass


def _parse_timestamp(text: str, label: str, *, not_in_future: bool) -> datetime:
    """An ISO 8601 timestamp with a timezone. not_in_future applies the API rule for times that already happened."""
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise UsageError(
            f"{label} must be an ISO 8601 timestamp with a timezone, for example 2026-10-30T09:00:00Z"
        ) from None
    if value.utcoffset() is None:
        raise UsageError(
            f"{label} must include a timezone, for example 2026-10-30T09:00:00Z or 2026-10-30T12:00:00+03:00"
        )
    # The same rule as the API and the web UI: a time that already happened cannot be later than now.
    if not_in_future and value > datetime.now(UTC):
        raise UsageError(
            f"{label} cannot be in the future. Enter the time it happened, not a later one"
        )
    return value


def _parse_awareness_time(text: str) -> datetime:
    return _parse_timestamp(text, "awareness time", not_in_future=True)


def _print_json(raw: bytes) -> None:
    """Print the API body as it was received. Nothing else goes to stdout."""
    text = raw.decode("utf-8")
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _format_risk(score: float | None) -> str:
    """The risk score with one decimal place, or Not assessed while a value is not set."""
    return "Not assessed" if score is None else f"{score:.1f}"


def _risk_line(record: dict) -> str:
    """The risk score with its three values, for example "3.7 (C 4, B 3, A 4)". A value that is not set shows a dash."""
    values = ", ".join(
        f"{letter} {'-' if record[key] is None else record[key]}"
        for letter, key in (("C", "impact_c"), ("B", "impact_b"), ("A", "impact_a"))
    )
    return f"{_format_risk(record['risk_score'])} ({values})"


def _print_case(case: dict) -> None:
    print(f"Case {case['reference']}")
    print(f"  ID:               {case['id']}")
    print(f"  Title:            {case['title']}")
    print(f"  Status:           {case['status']}")
    print(f"  Awareness (UTC):  {case['awareness_time']}")
    print(f"  Created (UTC):    {case['created_at']}")
    print(f"  Calendar version: {case['calendar_version']}")
    print(f"  Risk score:       {_risk_line(case)}")
    if case["deadlines"]:
        print("  Deadlines:")
        for task in case["deadlines"]:
            print(f"    {task['kind']}: due {task['due_at']} (UTC), {task['status']}")


def _print_case_page(page: dict) -> None:
    items = page["items"]
    if not items:
        print("No cases to show.")
    else:
        rows = [
            (
                "REFERENCE",
                "STATUS",
                "AWARENESS (UTC)",
                "NEXT DEADLINE (UTC)",
                "LAST UPDATED (UTC)",
                "RISK",
                "TITLE",
            )
        ]
        rows += [
            (
                item["reference"],
                item["status"],
                item["awareness_time"],
                item["next_deadline"] or "None",
                item["updated_at"],
                _format_risk(item["risk_score"]),
                item["title"],
            )
            for item in items
        ]
        _print_table(rows)
    if page["next_cursor"] is not None:
        print(f"Next page: --cursor {page['next_cursor']}")


def _case_create(base_url: str, args: argparse.Namespace) -> None:
    title = args.title
    # The same checks as the API: 1 to 200 characters, not only whitespace.
    if not 1 <= len(title) <= MAX_TITLE_LENGTH or not title.strip():
        raise UsageError(
            f"the title must be 1 to {MAX_TITLE_LENGTH} characters and not only spaces"
        )
    awareness = _parse_awareness_time(args.awareness_time)
    payload = {"title": title.strip(), "awareness_time": awareness.isoformat()}

    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "POST",
            "/api/v1/cases",
            token=token,
            payload=payload,
            extra_headers={"Idempotency-Key": key},
        )
        if args.json:
            _print_json(raw)
        else:
            print("Case created.")
            _print_case(_json_object(raw))

    _with_session(base_url, args, action)


def _case_id_arg(text: str) -> str:
    try:
        return str(uuid.UUID(text))
    except ValueError:
        raise UsageError(
            "CASE_ID must be the case ID (a UUID) shown by 'case list'"
        ) from None


def _uuid_arg(text: str, option: str) -> str:
    try:
        return str(uuid.UUID(text))
    except ValueError:
        raise UsageError(f"{option} must be a UUID") from None


def _check_paging(args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= MAX_PAGE_SIZE:
        raise UsageError(f"--limit must be between 1 and {MAX_PAGE_SIZE}")
    if args.offset < 0:
        raise UsageError("--offset must be 0 or more")


CURSOR_INVALID_TEXT = "the cursor is not valid. Run 'case list' again without --cursor to start from the first page"


def _case_list(base_url: str, args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= MAX_PAGE_SIZE:
        raise UsageError(f"--limit must be between 1 and {MAX_PAGE_SIZE}")
    query: dict = {"limit": args.limit}
    if args.cursor is not None:
        query["cursor"] = args.cursor
    if args.status is not None:
        query["status"] = args.status

    def action(base: str, token: str) -> None:
        try:
            raw = _call(base, "GET", "/api/v1/cases", token=token, query=query)
        except RequestFailed as error:
            detail = error.detail
            if (
                error.status == 400
                and isinstance(detail, dict)
                and detail.get("status") == "invalid_cursor"
            ):
                raise RequestFailed(
                    CURSOR_INVALID_TEXT, status=error.status, detail=detail
                ) from None
            raise
        if args.json:
            _print_json(raw)
        else:
            _print_case_page(_json_object(raw))

    _with_session(base_url, args, action)


def _case_show(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)

    def action(base: str, token: str) -> None:
        raw = _call(base, "GET", f"/api/v1/cases/{case_id}", token=token)
        if args.json:
            _print_json(raw)
        else:
            _print_case(_json_object(raw))

    _with_session(base_url, args, action)


def _read_bounded_file(path: str, limit: int, too_large: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as error:
        raise UsageError(
            f"cannot read {path}: {error.strerror or 'unknown error'}"
        ) from None
    if len(raw) > limit:
        raise UsageError(too_large)
    return raw


def _read_import_file(path: str) -> bytes:
    return _read_bounded_file(
        path,
        MAX_IMPORT_FILE_BYTES,
        f"the import file is larger than {MAX_IMPORT_FILE_BYTES} bytes",
    )


def _print_import_summary(summary: dict) -> None:
    print(
        f"Import finished: {summary['imported']} imported, {summary['duplicate']} duplicate, "
        f"{summary['conflict']} conflict, {summary['rejected']} rejected, {summary['failed']} failed."
    )
    for result in summary["results"]:
        position = f"records[{result['index']}]"
        status = result["status"]
        if status == "imported":
            print(f"  {position}: imported as {result['reference']}")
        elif status == "duplicate":
            print(f"  {position}: duplicate of {result['reference']}, nothing written")
        elif status == "conflict":
            print(
                f"  {position}: same case ID as {result['reference']} with different content, nothing written"
            )
        elif status == "rejected":
            print(f"  {position}: rejected, nothing written")
        else:
            print(f"  {position}: failed, nothing written")
        for error in result["errors"]:
            print(f"    {error}")


def _print_dry_run_summary(summary: dict) -> None:
    print(
        f"Dry run finished: {summary['would_import']} would be imported, {summary['duplicate']} duplicate, "
        f"{summary['conflict']} conflict, {summary['rejected']} rejected. Nothing was written."
    )
    for error in summary["file_errors"]:
        print(f"  file: {error}")
    for result in summary["results"]:
        position = f"records[{result['index']}]"
        status = result["status"]
        if status == "would_import":
            print(f'  {position}: would be imported as "{result["title"]}"')
        elif status == "duplicate":
            print(
                f"  {position}: duplicate of {result['reference'] or 'a stored case'}, nothing written"
            )
        elif status == "conflict":
            print(
                f"  {position}: same case ID as {result['reference'] or 'a stored case'} with different content"
            )
        else:
            print(f"  {position}: rejected, nothing written")
        for error in result["errors"]:
            print(f"    {error}")


def _case_import_dry_run(
    base_url: str, payload: dict, args: argparse.Namespace
) -> None:
    def action(base: str, token: str) -> None:
        raw = _call(
            base, "POST", "/api/v1/cases/import/dry-run", token=token, payload=payload
        )
        summary = _json_object(raw)
        if args.json:
            _print_json(raw)
        else:
            _print_dry_run_summary(summary)
        # Duplicates are expected on a repeated import. A file the server does not accept, a rejected record, or a
        # conflict would stop the commit too, so the dry run fails for each of them.
        if summary["file_errors"]:
            raise RequestFailed("the import file is not accepted; see the report above")
        blocked = summary["rejected"] + summary["conflict"]
        if blocked:
            raise RequestFailed(
                f"{blocked} record(s) would not be stored; see the report above"
            )

    _with_session(base_url, args, action)


def _case_import(base_url: str, args: argparse.Namespace) -> None:
    if args.dry_run and args.idempotency_key:
        raise UsageError(
            "--idempotency-key does not apply to --dry-run, which writes nothing"
        )
    raw = _read_import_file(args.file)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise UsageError(f"{args.file} is not valid JSON") from None
    if not isinstance(payload, dict):
        raise UsageError(
            f"{args.file} must hold a JSON object with import_format_version and records"
        )
    if args.dry_run:
        _case_import_dry_run(base_url, payload, args)
        return

    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "POST",
            "/api/v1/cases/import",
            token=token,
            payload=payload,
            extra_headers={"Idempotency-Key": key},
        )
        summary = _json_object(raw)
        if args.json:
            _print_json(raw)
        else:
            _print_import_summary(summary)
        # Duplicates are expected on a repeated import. Anything that was not stored is a failed command.
        not_stored = summary["rejected"] + summary["conflict"] + summary["failed"]
        if not_stored:
            raise RequestFailed(
                f"{not_stored} record(s) were not stored; see the results above"
            )

    _with_session(base_url, args, action)


def _write_document(raw: bytes, out: str | None) -> None:
    """Print a versioned JSON document as received, or write it to the file out (one trailing newline)."""
    if out is None:
        _print_json(raw)
        return
    body = raw if raw.endswith(b"\n") else raw + b"\n"
    try:
        with open(out, "wb") as handle:
            handle.write(body)
    except OSError as error:
        raise UsageError(
            f"cannot write {out}: {error.strerror or 'unknown error'}"
        ) from None


def _catalog_export(base_url: str, args: argparse.Namespace) -> None:
    captured: dict[str, bytes] = {}

    def action(base: str, token: str) -> None:
        captured["raw"] = _call(base, "GET", "/api/v1/catalog/export", token=token)

    _with_session(base_url, args, action)
    _write_document(captured["raw"], args.out)


def _print_evidence(item: dict) -> None:
    print(f"Evidence {item['id']}")
    print(f"  Case ID:          {item['case_id']}")
    print(f"  File:             {item['original_filename']}")
    print(f"  Media type:       {item['media_type']}")
    print(f"  Size (bytes):     {item['size_bytes']}")
    print(f"  Status:           {item['status']}")
    print(f"  SHA-256:          {item['sha256_plaintext'] or '-'}")
    print(f"  Uploaded (UTC):   {item['created_at']}")


def _print_evidence_page(page: dict) -> None:
    items = page["items"]
    if not items:
        print(f"No evidence for this case. Total: {page['total']}")
        return
    rows = [("ID", "STATUS", "MEDIA TYPE", "SIZE (BYTES)", "FILE")]
    rows += [
        (
            item["id"],
            item["status"],
            item["media_type"],
            str(item["size_bytes"]),
            item["original_filename"],
        )
        for item in items
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    for row in rows:
        cells = [row[column].ljust(widths[column]) for column in range(4)] + [row[4]]
        print("  ".join(cells).rstrip())
    first = page["offset"] + 1
    last = page["offset"] + len(items)
    print(f"Showing {first}-{last} of {page['total']}")


def _print_tasks(case: dict) -> None:
    tasks = case["deadlines"]
    if not tasks:
        print(f"No deadline tasks for case {case['reference']}.")
        return
    # The REVISION column is the value for --revision in 'case task update'.
    rows = [("ID", "KIND", "DUE (UTC)", "STATUS", "REVISION", "NOTE")]
    rows += [
        (
            task["id"] or "-",
            task["kind"],
            task["due_at"],
            task["status"],
            str(task["revision"]) if task["revision"] is not None else "-",
            task["not_applicable_reason"] or "",
        )
        for task in tasks
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(5)]
    print(f"Deadline tasks for case {case['reference']}:")
    for row in rows:
        cells = [row[column].ljust(widths[column]) for column in range(5)] + [row[5]]
        print("  ".join(cells).rstrip())


def _multipart_file(
    field: str, filename: str, content_type: str, data: bytes
) -> tuple[bytes, str]:
    """A multipart/form-data body holding one file. Returns the body and the Content-Type header value."""
    boundary = uuid.uuid4().hex
    # A quote or line break in the name would break the part header. The API sanitizes the name again.
    safe_name = "".join("_" if char in '"\r\n' else char for char in filename)
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{safe_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + data + tail, f"multipart/form-data; boundary={boundary}"


def _evidence_add(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    # The file is read before the login, so a bad file never sends a password.
    name = os.path.basename(args.file)
    data = _read_bounded_file(
        args.file,
        MAX_EVIDENCE_FILE_BYTES,
        f"the file is larger than {MAX_EVIDENCE_FILE_BYTES} bytes, the default upload limit",
    )
    if not data:
        raise UsageError(f"{args.file} is empty")
    # The media type is a hint only. The API checks it against the content and answers 415 when they differ.
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    body, content_type = _multipart_file("file", name, media_type, data)
    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "POST",
            f"/api/v1/cases/{case_id}/evidence",
            token=token,
            raw_body=body,
            content_type=content_type,
            messages=EVIDENCE_HTTP_MESSAGES,
            extra_headers={"Idempotency-Key": key},
        )
        if args.json:
            _print_json(raw)
        else:
            print("Evidence stored.")
            _print_evidence(_json_object(raw))

    _with_session(base_url, args, action)


def _evidence_list(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    _check_paging(args)

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            f"/api/v1/cases/{case_id}/evidence",
            token=token,
            query={"limit": args.limit, "offset": args.offset},
        )
        if args.json:
            _print_json(raw)
        else:
            _print_evidence_page(_json_object(raw))

    _with_session(base_url, args, action)


def _task_list(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)

    def action(base: str, token: str) -> None:
        # The deadline tasks are part of the case response, so this is the same request as case show.
        raw = _call(base, "GET", f"/api/v1/cases/{case_id}", token=token)
        if args.json:
            _print_json(raw)
        else:
            _print_tasks(_json_object(raw))

    _with_session(base_url, args, action)


TASK_UPDATE_MESSAGES = {
    403: "forbidden: only a triager can mark a deadline not applicable or change one that is not applicable",
    404: "case or deadline task not found, or its case is not available to your account",
    409: "the case is closed, so its deadlines cannot change",
    400: "the reason is missing or too long: a not applicable deadline needs a reason of 1 to 1000 characters",
    412: "the deadline task changed since you read it, so nothing was saved. Run 'case task list' again",
}


def _precondition_revision(error: RequestFailed) -> int | None:
    """The current revision from a stale-write 412 of a deadline task or legal duty, or None for any other failure."""
    detail = error.detail
    if (
        error.status != 412
        or not isinstance(detail, dict)
        or detail.get("status") != "precondition_failed"
    ):
        return None
    revision = detail.get("current_revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        return revision
    return None


TASK_RESTORE_MESSAGES = {
    403: "forbidden: only a triager can restore a deadline that is not applicable",
    404: "case or deadline task not found, or its case is not available to your account",
    409: "the case is closed, or the deadline is not marked not applicable",
}


def _render_tasks_change(heading: str):
    def render(raw: bytes) -> None:
        print(heading)
        _print_tasks(_json_object(raw))

    return render


def _task_update(base_url: str, args: argparse.Namespace) -> None:
    """Set one deadline task to open, done or not applicable. Only one task changes; due dates never change."""
    case_id = _case_id_arg(args.case_id)
    task_id = _uuid_arg(args.task_id, "TASK_ID")
    status = args.status.replace("-", "_")
    reason = args.reason.strip() if args.reason is not None else None
    if status == "not_applicable" and not reason:
        raise UsageError("--reason is required when --status is not-applicable")
    if status != "not_applicable" and args.reason is not None:
        raise UsageError("--reason is only allowed with --status not-applicable")
    if args.revision < 1:
        raise UsageError(
            "--revision must be 1 or more. Read it with: case task list CASE_ID"
        )
    try:
        _write_case(
            base_url,
            args,
            "PATCH",
            f"/api/v1/cases/{case_id}/deadlines/{task_id}",
            payload={
                "status": status,
                "reason": reason if status == "not_applicable" else None,
            },
            messages=TASK_UPDATE_MESSAGES,
            render=_render_tasks_change("Deadline task updated."),
            if_match=args.revision,
        )
    except RequestFailed as error:
        current = _precondition_revision(error)
        if current is None:
            raise
        # Nothing was saved. The command never retries: the user reads the tasks again and runs it again.
        raise RequestFailed(
            f"the deadline task changed, so nothing was saved. Its current revision is {current}. Run "
            f"'case task list {case_id}' to check it, then run this command again with --revision {current}"
        ) from None


def _task_restore(base_url: str, args: argparse.Namespace) -> None:
    """Turn off the not applicable exception of one task: it goes back to the status it had before."""
    case_id = _case_id_arg(args.case_id)
    task_id = _uuid_arg(args.task_id, "TASK_ID")
    _write_case(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/deadlines/{task_id}/restore",
        messages=TASK_RESTORE_MESSAGES,
        render=_render_tasks_change(
            "Deadline task restored to the status it had before."
        ),
    )


def _write_case(
    base_url: str,
    args: argparse.Namespace,
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    messages: dict[int, str] | None = None,
    render=None,
    if_match: int | None = None,
) -> None:
    """One write request in its own login session. --json prints the API body; otherwise render prints a summary.

    if_match is the revision the caller read. It is sent as the quoted If-Match value (D-16).
    """
    # A POST, PUT or PATCH carries an Idempotency-Key. The key is made once per run, so a retry in the same run reuses it.
    headers = (
        None
        if method == "DELETE"
        else {
            "Idempotency-Key": getattr(args, "idempotency_key", None)
            or str(uuid.uuid4())
        }
    )
    if if_match is not None:
        headers = {**(headers or {}), "If-Match": f'"{if_match}"'}

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            method,
            path,
            token=token,
            payload=payload,
            messages=messages,
            extra_headers=headers,
        )
        if getattr(args, "json", False):
            _print_json(raw)
        else:
            render(raw)

    _with_session(base_url, args, action)


def _render_case_change(heading: str):
    def render(raw: bytes) -> None:
        print(heading)
        _print_case(_json_object(raw))

    return render


def _print_review_round(review: dict, full_view: bool) -> None:
    """One review round: its id, state and revision, then one line per layer. Reasons are shown when the caller may read them."""
    print(f"  Round ID:   {review['id']}")
    print(f"  Revision:   {review['revision']}")
    print(f"  State:      {review['status']}")
    for layer in review["layers"]:
        print(f"  {layer['layer']:<30} {layer['outcome'] or 'not set'}")
        if layer["outcome"] is None:
            continue
        print(f"    Reason: {_free_text(layer['reason'], full_view)}")


def _render_review_opened(raw: bytes) -> None:
    print("Review round opened. Every layer is not set.")
    _print_review_round(_json_object(raw), full_view=True)


def _render_review_list(raw: bytes, full_view: bool) -> None:
    rounds = json.loads(raw)
    if not isinstance(rounds, list):
        raise RequestFailed("unexpected response from the server")
    if not rounds:
        print("No review round has been opened for this case.")
        return
    for review in rounds:
        _print_review_round(review, full_view)
        print()


def _render_review_saved(raw: bytes) -> None:
    review = _json_object(raw)
    print(f"Review layer saved. The round revision is now {review['revision']}.")
    print(f"  Round ID:   {review['id']}")
    for layer in review["layers"]:
        if layer["outcome"] is not None:
            print(f"  {layer['layer']:<30} {layer['outcome']}")


def _render_assignment(raw: bytes) -> None:
    assignment = _json_object(raw)
    print("Assignment added.")
    print(f"  Case ID:          {assignment['case_id']}")
    print(f"  User:             {assignment['username']}")
    print(f"  Role:             {assignment['role']}")
    print(f"  Assigned (UTC):   {assignment['assigned_at']}")


def _render_classification(raw: bytes) -> None:
    classification = _json_object(raw)
    print(f"Classification saved as version {classification['version']}.")
    print(f"  Triggers:          {', '.join(classification['triggers']) or '-'}")
    print(f"  Event kind:        {classification['event_kind'] or '-'}")
    print(f"  Next update (UTC): {classification['next_update_due_at'] or '-'}")


def _render_impact(raw: bytes) -> None:
    impact = _json_object(raw)
    print(f"Impact area {impact['area']} saved.")
    print(f"  Severity:         {impact['severity']}")
    print(f"  Scope:            {impact['scope'] or '-'}")
    print(f"  Updated (UTC):    {impact['updated_at']}")


def _render_duty(heading: str):
    def render(raw: bytes) -> None:
        duty = _json_object(raw)
        print(heading)
        print(f"  ID:               {duty['id']}")
        print(f"  Kind:             {duty['kind']}")
        print(f"  Description:      {duty['description'] or '-'}")
        print(f"  Source reference: {duty['source_reference'] or '-'}")
        print(f"  Jurisdiction:     {duty['jurisdiction'] or '-'}")
        print(f"  Due (UTC):        {duty['due_at']}")
        print(f"  Status:           {duty['status']}")
        print(f"  Revision:         {duty['revision']}")

    return render


def _render_awareness(raw: bytes) -> None:
    event = _json_object(raw)
    print("Awareness event recorded.")
    print(f"  ID:               {event['id']}")
    print(f"  Awareness kind:   {event['awareness_kind']}")
    print(f"  Event kind:       {event['event_kind']}")
    print(f"  Occurred (UTC):   {event['occurred_at']}")
    print(f"  Basis:            {event['basis'] or '-'}")
    print(f"  Recorded (UTC):   {event['created_at']}")


# Per command, the messages for the statuses that have a specific meaning there. Other statuses use HTTP_MESSAGES.
CLOSE_MESSAGES = {
    404: "case not found",
    409: "the case cannot be closed now: it is already closed, or it has no review or an open deadline task",
}
REOPEN_MESSAGES = {
    404: "case not found",
    409: "the case is not closed, so it cannot be reopened",
}
ASSIGN_MESSAGES = {
    404: "case not found",
    409: "the case is closed, or the user is already assigned to it",
    422: "the user must be active and hold the cases:assignable permission",
}
UNASSIGN_MESSAGES = {
    404: "case or assignment not found",
    409: "the case is closed, so its assignments cannot change",
}
LINK_ADD_MESSAGES = {
    404: "case not found, or the other case is not available to your account",
    409: "a case is closed, or the two cases are already linked",
    422: "a case cannot be linked to itself",
}
LINK_REMOVE_MESSAGES = {
    404: "case not found, or there is no link between the two cases",
    409: "a case is closed, so its links cannot change",
}
REVIEW_MESSAGES = {
    403: "forbidden: changing a review needs reviews:write, and you must be assigned to the case",
    404: "case or review round not found",
    409: "the case or its review round is closed, so no review can change",
}
REVIEW_READ_MESSAGES = {
    404: "case not found",
}
CLASSIFICATION_MESSAGES = {
    403: "forbidden: saving the classification needs cases:triage and the full case view (cases:read_all)",
    404: "case not found",
    409: "the case is closed, so the classification cannot change",
}
IMPACT_MESSAGES = {
    404: "case not found",
    409: "the case is closed, so its impact assessment cannot change",
}
RISK_MESSAGES = {
    403: "forbidden: saving the impact values needs cases:triage",
    404: "case not found",
    409: "the case is closed, so the impact values cannot change",
}
# The three impact values in the order C, B, A, with the field each one has in the API body.
RISK_VALUES = (
    ("C – Confidentiality", "c"),
    ("B – Integrity", "b"),
    ("A – Availability", "a"),
)
LEGAL_DUTY_MESSAGES = {
    404: "case not found",
    409: "the case is closed, so its legal duties cannot change",
}
LEGAL_DUTY_UPDATE_MESSAGES = {
    404: "case or legal duty not found",
    409: "the case is closed, so its legal duties cannot change",
    412: "the legal duty changed since you read it, so nothing was saved. Run 'case legal-duty list' again",
}
AWARENESS_MESSAGES = {
    404: "case not found",
    409: "the case is closed, so no awareness event can be recorded",
}
READ_MESSAGES = {
    403: "forbidden: reading case records needs cases:read_own, cases:read_assigned or cases:read_all",
    404: "case not found",
}
REPORT_MESSAGES = {
    403: "forbidden: the preliminary report needs cases:read_all, which your account does not hold",
    404: "case not found",
}
# Shown for a free-text field that the API hides from this caller (it is null for callers without cases:read_all).
NOT_VISIBLE_TEXT = "(not visible to your account)"
CLASSIFICATION_FREE_TEXT = (
    ("Rationale", "classification_rationale"),
    ("Responsible operator", "responsible_operator"),
    ("Affected versions", "affected_versions"),
    ("Approved scope", "approved_scope"),
    ("Scope permission source", "scope_permission_source"),
    ("Containment", "containment"),
    ("Evidence gap plan", "evidence_gap_plan"),
)


def _case_close(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    _write_case(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/close",
        payload={"reason": args.reason},
        messages=CLOSE_MESSAGES,
        render=_render_case_change("Case closed."),
    )


def _case_reopen(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    _write_case(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/reopen",
        payload={"reason": args.reason},
        messages=REOPEN_MESSAGES,
        render=_render_case_change("Case reopened."),
    )


def _case_assign(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    user_id = _uuid_arg(args.user_id, "--user-id")
    _write_case(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/assignments",
        payload={"user_id": user_id},
        messages=ASSIGN_MESSAGES,
        render=_render_assignment,
    )


def _case_unassign(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    user_id = _uuid_arg(args.user_id, "--user-id")
    _write_case(
        base_url,
        args,
        "DELETE",
        f"/api/v1/cases/{case_id}/assignments/{user_id}",
        messages=UNASSIGN_MESSAGES,
        render=lambda raw: print("Assignment removed."),
    )


def _print_link(link: dict) -> None:
    # The other case is shown only when the API gives its ID, which it does only for a case the caller can see.
    print(
        f"  Linked case ID:   {link['linked_case_id'] or '(not available to your account)'}"
    )
    print(f"  Title:            {link['linked_case_title'] or '-'}")
    print(f"  Linked (UTC):     {link['created_at']}")


def _render_link_added(raw: bytes) -> None:
    print("Cases linked. Linking changes no task, deadline or status.")
    _print_link(_json_object(raw))


def _case_link_add(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    linked_case_id = _uuid_arg(args.linked_case_id, "LINKED_CASE_ID")
    _write_case(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/links",
        payload={"linked_case_id": linked_case_id},
        messages=LINK_ADD_MESSAGES,
        render=_render_link_added,
    )


def _case_link_list(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)

    def action(base: str, token: str) -> None:
        raw = _call(base, "GET", f"/api/v1/cases/{case_id}/links", token=token)
        if args.json:
            _print_json(raw)
            return
        links = json.loads(raw)
        if not links:
            print("No linked cases.")
        for link in links:
            _print_link(link)
            print()

    _with_session(base_url, args, action)


def _case_link_remove(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    linked_case_id = _uuid_arg(args.linked_case_id, "LINKED_CASE_ID")
    _write_case(
        base_url,
        args,
        "DELETE",
        f"/api/v1/cases/{case_id}/links/{linked_case_id}",
        messages=LINK_REMOVE_MESSAGES,
        render=lambda raw: print(
            "Link removed. Linking changes no task, deadline or status."
        ),
    )


MODEL_RECORD_MESSAGES = {
    403: "forbidden: recording needs evidence:write and evidence:read, and the case must be visible to you",
    404: "case, evidence item or review round not found",
    409: "the case is closed, so no model record can be recorded",
    400: "the model record was refused: check the identifiers and the reasoning options",
    422: "the model record was refused: check the identifiers and the reasoning options",
}
MODEL_LIST_MESSAGES = {404: "case not found"}
MODEL_REVOKE_MESSAGES = {
    403: "forbidden: revoking needs evidence:write and evidence:read, and the case must be visible to you",
    404: "case or model record not found",
    409: "the case is closed, or the model record is already revoked",
    400: "the revocation was refused: give a reason of 1 to 1000 characters",
}


def _model_identifier(text: str, option: str) -> str:
    if MODEL_IDENTIFIER.fullmatch(text) is None:
        raise UsageError(
            f"{option} may use only letters, digits and . _ : / @ + - (up to 200 characters)"
        )
    return text


def _model_record_links(args: argparse.Namespace) -> tuple[str | None, str | None]:
    evidence = (
        _uuid_arg(args.evidence_item, "--evidence-item") if args.evidence_item else None
    )
    review_round = (
        _uuid_arg(args.review_round, "--review-round") if args.review_round else None
    )
    return evidence, review_round


def _model_record_reasoning(args: argparse.Namespace) -> dict:
    if args.reasoning_mode != "enabled" and (
        args.reasoning_effort or args.reasoning_budget_tokens is not None
    ):
        raise UsageError(
            "--reasoning-effort and --reasoning-budget-tokens need --reasoning-mode enabled"
        )
    if (
        args.reasoning_budget_tokens is not None
        and not 1 <= args.reasoning_budget_tokens <= REASONING_BUDGET_LIMIT
    ):
        raise UsageError(
            f"--reasoning-budget-tokens must be between 1 and {REASONING_BUDGET_LIMIT}"
        )
    return {
        "mode": args.reasoning_mode,
        "effort": args.reasoning_effort,
        "budget_tokens": args.reasoning_budget_tokens,
    }


def _print_model_record(record: dict) -> None:
    config = record["configuration"]
    reasoning = config["reasoning"]
    effort = f", effort {reasoning['effort']}" if reasoning["effort"] else ""
    budget = (
        f", budget {reasoning['budget_tokens']} tokens"
        if reasoning["budget_tokens"]
        else ""
    )
    if record["evidence_item_id"]:
        linked = f"evidence item {record['evidence_item_id']}"
    elif record["review_round_id"]:
        linked = f"review round {record['review_round_id']}"
    else:
        linked = "the case"
    print(f"  Record ID:        {record['record_id']} (version {record['version']})")
    print(f"  Model:            {config['model_name']} {config['model_version']}")
    print(f"  Runtime:          {config['runtime']}")
    print(f"  Provider alias:   {config['provider_alias']}")
    print(f"  Reasoning:        {reasoning['mode']}{effort}{budget}")
    print(f"  Concerns:         {linked}")
    print(f"  Recorded (UTC):   {record['recorded_at']}")
    if record["revoked"]:
        reason = record["revocation_reason"] or "(reason not shown for your account)"
        print(f"  Status:           revoked at {record['revoked_at']}: {reason}")
    else:
        print("  Status:           current")


def _render_model_record_added(raw: bytes) -> None:
    print("Model record saved.")
    _print_model_record(_json_object(raw))


def _model_record_add(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    evidence, review_round = _model_record_links(args)
    if evidence and review_round:
        raise UsageError("--evidence-item and --review-round cannot be used together")
    payload = {
        "model_name": _model_identifier(args.model_name, "--model-name"),
        "model_version": _model_identifier(args.model_version, "--model-version"),
        "runtime": _model_identifier(args.runtime, "--runtime"),
        "provider_alias": _model_identifier(args.provider_alias, "--provider-alias"),
        "reasoning": _model_record_reasoning(args),
        "evidence_item_id": evidence,
        "review_round_id": review_round,
    }
    _write_case(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/model-records",
        payload=payload,
        messages=MODEL_RECORD_MESSAGES,
        render=_render_model_record_added,
    )


def _render_model_record_revoked(raw: bytes) -> None:
    print(
        "Model record revoked. It stays readable for audit and is no longer the current configuration."
    )
    _print_model_record(_json_object(raw))


def _model_record_list(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    query = {"current_only": "true"} if args.current_only else None

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            f"/api/v1/cases/{case_id}/model-records",
            token=token,
            query=query,
            messages=MODEL_LIST_MESSAGES,
        )
        if args.json:
            _print_json(raw)
            return
        records = _json_list(raw)
        if not records:
            print("No model records.")
        for record in records:
            _print_model_record(record)
            print()

    _with_session(base_url, args, action)


def _model_record_revoke(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    record_id = _uuid_arg(args.record_id, "RECORD_ID")
    reason = args.reason.strip()
    if not reason:
        raise UsageError("--reason must not be blank")
    _write_case(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/model-records/{record_id}/revoke",
        payload={"reason": reason},
        messages=MODEL_REVOKE_MESSAGES,
        render=_render_model_record_revoked,
    )


def _review_open(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    # The key makes a repeated run safe: the same key and the same case return the first round. Pass the key of an
    # earlier run to retry it; a new run gets a new key.
    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "POST",
            "/api/v1/reviews",
            token=token,
            payload={"case_id": case_id},
            messages=REVIEW_MESSAGES,
            extra_headers={"Idempotency-Key": key},
        )
        if args.json:
            _print_json(raw)
        else:
            _render_review_opened(raw)

    _with_session(base_url, args, action)


def _review_list(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    _case_read(
        base_url,
        args,
        f"/api/v1/reviews?case_id={case_id}",
        REVIEW_READ_MESSAGES,
        _render_review_list,
    )


def _review_conflict_revision(error: RequestFailed) -> int | None:
    """The current round revision from a stale-write 412, or None for any other failure."""
    detail = error.detail
    if (
        error.status != 412
        or not isinstance(detail, dict)
        or detail.get("status") != REVIEW_PRECONDITION_FAILED
    ):
        return None
    revision = detail.get("current_revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        return revision
    return None


def _review_record(base_url: str, args: argparse.Namespace) -> None:
    """Set one layer of a review round. Without --round, the open round of the case is used."""
    case_id = _case_id_arg(args.case_id)
    # One key per run: a retry with --idempotency-key of an earlier run gets the first answer.
    key = args.idempotency_key or str(uuid.uuid4())
    if args.revision < 1:
        raise UsageError(
            "--revision must be 1 or more. Read it with: case review list CASE_ID"
        )
    reason = args.reason if args.reason is not None else args.note
    if args.outcome != "pass" and (reason is None or not reason.strip()):
        raise UsageError("a reason is required for this outcome: pass --reason TEXT")
    round_id = _case_id_arg(args.round) if args.round is not None else None
    evidence_ids = [_case_id_arg(item) for item in args.evidence]
    if args.outcome == "pass" and not evidence_ids:
        raise UsageError(
            "a pass needs at least one evidence item of this case: pass --evidence EVIDENCE_ID"
        )
    payload = {
        "outcome": args.outcome,
        "reason": reason,
        "evidence_ids": evidence_ids,
    }

    def action(base: str, token: str) -> None:
        target = round_id
        if target is None:
            rounds = json.loads(
                _call(
                    base,
                    "GET",
                    f"/api/v1/reviews?case_id={case_id}",
                    token=token,
                    messages=REVIEW_READ_MESSAGES,
                )
            )
            open_rounds = [item for item in rounds if item["status"] == "open"]
            if not open_rounds:
                raise RequestFailed(
                    "this case has no open review round. Open one with: case review open CASE_ID"
                )
            target = open_rounds[0]["id"]
        try:
            raw = _call(
                base,
                "PATCH",
                f"/api/v1/reviews/{target}/layers/{args.layer}",
                token=token,
                payload=payload,
                messages=REVIEW_MESSAGES,
                # The revision read with 'review list' travels as the ETag in If-Match (D-16).
                extra_headers={
                    "Idempotency-Key": key,
                    "If-Match": f'"{args.revision}"',
                },
            )
        except RequestFailed as error:
            current = _review_conflict_revision(error)
            if current is None:
                raise
            # Nothing was saved. The command never retries: the user re-reads the round and runs it again.
            raise RequestFailed(
                f"the review changed, so nothing was saved. The current revision is {current}. Run "
                f"'case review list {case_id}' to check it, then run this command again with --revision {current}"
            ) from None
        if args.json:
            _print_json(raw)
        else:
            _render_review_saved(raw)

    _with_session(base_url, args, action)


def _optional_uuid(text: str | None, option: str) -> str | None:
    return None if text is None else _uuid_arg(text, option)


def _render_conflict(raw: bytes) -> None:
    entry = _json_object(raw)
    print(
        "Conflict declared." if entry["kind"] == "declared" else "Conflict withdrawn."
    )
    print(f"  ID:               {entry['id']}")
    print(f"  Round ID:         {entry['round_id']}")
    print(f"  Reviewer ID:      {entry['reviewer_id']}")
    print(f"  Description:      {entry['description'] or '-'}")
    print(f"  Corrects:         {entry['supersedes_id'] or '-'}")


def _render_recusal(raw: bytes) -> None:
    entry = _json_object(raw)
    print("Recusal recorded. The replacement is named on the round.")
    print(f"  ID:               {entry['id']}")
    print(f"  Round ID:         {entry['round_id']}")
    print(f"  Reviewer ID:      {entry['reviewer_id']}")
    print(f"  Replacement ID:   {entry['replacement_id']}")
    print(f"  Reason:           {entry['reason'] or '-'}")
    print(f"  Corrects:         {entry['supersedes_id'] or '-'}")


def _render_appeal(raw: bytes) -> None:
    entry = _json_object(raw)
    print(
        "Appeal filed. The system named the independent reviewer; the product does not decide the appeal."
    )
    print(f"  ID:               {entry['id']}")
    print(f"  Round ID:         {entry['round_id']}")
    print(f"  Filed by:         {entry['filed_by']}")
    print(f"  Independent ID:   {entry['independent_reviewer_id']}")
    print(f"  Grounds:          {entry['grounds'] or '-'}")


CONFLICT_MESSAGES = {
    403: "forbidden: declaring a conflict needs reviews:write, and you must be assigned to the case",
    404: "review round not found",
    409: "the case is closed, your conflict is already declared, there is nothing to withdraw, or --supersedes is not your current entry. Check the round with 'case review list' first",
    400: "the description is missing or too long",
}
RECUSAL_MESSAGES = {
    403: "forbidden: withdrawing needs reviews:write, and you must be assigned to the case",
    404: "review round not found",
    409: "the case is closed, you have already withdrawn (to correct it, pass --supersedes with your current recusal), or --supersedes is not your current entry",
    400: "the replacement must be the identifier of a user",
    422: "the replacement must be another active, assigned user who holds reviews:write, has no declared conflict on this round, and has not withdrawn from it",
}
APPEAL_MESSAGES = {
    403: "forbidden: filing an appeal needs a case read permission",
    404: "review round not found, or its case is not available to your account",
    409: "no reviewer is eligible for this round: the system needs an active, assigned user with reviews:write who did not take part in the round and has no declared conflict on it",
}


def _review_conflict(base_url: str, args: argparse.Namespace) -> None:
    """Declare, withdraw or correct your own conflict on a review round. Without a key, each run gets a new one."""
    round_id = _uuid_arg(args.round_id, "ROUND_ID")
    if args.kind == "declared" and (
        args.description is None or not args.description.strip()
    ):
        raise UsageError(
            "a description is required for a declared conflict: pass --description TEXT"
        )
    payload = {
        "kind": args.kind,
        "description": args.description,
        "supersedes_id": _optional_uuid(args.supersedes, "--supersedes"),
    }
    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "POST",
            f"/api/v1/reviews/{round_id}/conflicts",
            token=token,
            payload=payload,
            messages=CONFLICT_MESSAGES,
            extra_headers={"Idempotency-Key": key},
        )
        if args.json:
            _print_json(raw)
        else:
            _render_conflict(raw)

    _with_session(base_url, args, action)


def _review_recuse(base_url: str, args: argparse.Namespace) -> None:
    """Withdraw from a review round and name the replacement. Without a key, each run gets a new one."""
    round_id = _uuid_arg(args.round_id, "ROUND_ID")
    payload = {
        "replacement_id": _uuid_arg(args.replacement, "--replacement"),
        "reason": args.reason,
        "supersedes_id": _optional_uuid(args.supersedes, "--supersedes"),
    }
    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "POST",
            f"/api/v1/reviews/{round_id}/recusals",
            token=token,
            payload=payload,
            messages=RECUSAL_MESSAGES,
            extra_headers={"Idempotency-Key": key},
        )
        if args.json:
            _print_json(raw)
        else:
            _render_recusal(raw)

    _with_session(base_url, args, action)


def _review_appeal(base_url: str, args: argparse.Namespace) -> None:
    """File an appeal against a review round. The system names the independent reviewer. Without a key, each run gets a new one."""
    round_id = _uuid_arg(args.round_id, "ROUND_ID")
    if not args.grounds.strip():
        raise UsageError("--grounds must not be blank")
    payload = {"grounds": args.grounds}
    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "POST",
            f"/api/v1/reviews/{round_id}/appeals",
            token=token,
            payload=payload,
            messages=APPEAL_MESSAGES,
            extra_headers={"Idempotency-Key": key},
        )
        if args.json:
            _print_json(raw)
        else:
            _render_appeal(raw)

    _with_session(base_url, args, action)


def _classification_conflict_version(error: RequestFailed) -> int | None:
    """The current classification version from a stale-save 409, or None for any other failure."""
    detail = error.detail
    if (
        error.status != 409
        or not isinstance(detail, dict)
        or detail.get("status") != CLASSIFICATION_VERSION_CONFLICT
    ):
        return None
    version = detail.get("current_version")
    if isinstance(version, int) and not isinstance(version, bool):
        return version
    return None


def _classification_set(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    if args.expected_version < 0:
        raise UsageError("--expected-version must be 0 or more")
    next_update = None
    if args.next_update_due_at is not None:
        next_update = _parse_timestamp(
            args.next_update_due_at, "--next-update-due-at", not_in_future=False
        )
    # The save replaces every field. A field left out is sent as empty and cleared, as in the web UI.
    payload = {
        "expected_version": args.expected_version,
        "triggers": args.trigger or [],
        "event_kind": args.event_kind,
        "classification_rationale": args.classification_rationale,
        "responsible_operator": args.responsible_operator,
        "affected_versions": args.affected_versions,
        "approved_scope": args.approved_scope,
        "scope_permission_source": args.scope_permission_source,
        "containment": args.containment,
        "evidence_gap_plan": args.evidence_gap_plan,
        "next_update_due_at": next_update.isoformat()
        if next_update is not None
        else None,
    }
    path = f"/api/v1/cases/{case_id}/classification"

    def action(base: str, token: str) -> None:
        try:
            raw = _call(
                base,
                "PUT",
                path,
                token=token,
                payload=payload,
                messages=CLASSIFICATION_MESSAGES,
            )
        except RequestFailed as error:
            version = _classification_conflict_version(error)
            if version is None:
                raise
            # Nothing was saved. The command never retries: the user re-reads and runs it again.
            raise RequestFailed(
                f"the classification was changed by someone else, so nothing was saved. The current version is "
                f"{version}. Re-read the classification in the web UI, then run this command again with "
                f"--expected-version {version}"
            ) from None
        if args.json:
            _print_json(raw)
        else:
            _render_classification(raw)

    _with_session(base_url, args, action)


def _impact_set(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    _write_case(
        base_url,
        args,
        "PUT",
        f"/api/v1/cases/{case_id}/impacts/{args.area}",
        payload={"severity": args.severity, "scope": args.scope},
        messages=IMPACT_MESSAGES,
        render=_render_impact,
    )


def _impact_value(text: str) -> int:
    """A value of the impact scale: a whole number from 1 to 5."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number from 1 to 5") from None
    if not 1 <= value <= 5:
        raise argparse.ArgumentTypeError("must be a whole number from 1 to 5")
    return value


def _print_risk(scores: dict) -> None:
    _print_field("Risk score", _format_risk(scores["risk_score"]))
    for label, key in RISK_VALUES:
        _print_field(label, "Not set" if scores[key] is None else str(scores[key]))


def _risk_show(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            f"/api/v1/cases/{case_id}/impact-scores",
            token=token,
            messages=READ_MESSAGES,
        )
        if args.json:
            _print_json(raw)
            return
        scores = _json_object(raw)
        if scores["version"] == 0:
            print("No impact values are recorded for this case yet.")
        else:
            print(f"Impact values, version {scores['version']}:")
        _print_risk(scores)

    _with_session(base_url, args, action)


def _risk_conflict_version(error: RequestFailed) -> int | None:
    """The current impact version from a stale-save 409, or None for any other failure."""
    detail = error.detail
    if (
        error.status != 409
        or not isinstance(detail, dict)
        or detail.get("status") != IMPACT_VERSION_CONFLICT
    ):
        return None
    version = detail.get("current_version")
    if isinstance(version, int) and not isinstance(version, bool):
        return version
    return None


def _risk_set(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    if args.expected_version < 0:
        raise UsageError("--expected-version must be 0 or more")
    # The save replaces the three values. A value left out is sent as empty and cleared, as in the web UI.
    payload = {
        "expected_version": args.expected_version,
        "c": args.c,
        "b": args.b,
        "a": args.a,
    }
    path = f"/api/v1/cases/{case_id}/impact-scores"
    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        try:
            raw = _call(
                base,
                "POST",
                path,
                token=token,
                payload=payload,
                messages=RISK_MESSAGES,
                extra_headers={"Idempotency-Key": key},
            )
        except RequestFailed as error:
            version = _risk_conflict_version(error)
            if version is None:
                raise
            # Nothing was saved. The command never retries: the user reads the values again and runs it again.
            raise RequestFailed(
                f"the impact values were changed by someone else, so nothing was saved. The current version is "
                f"{version}. Read them with 'case risk show', then run this command again with --expected-version {version}"
            ) from None
        if args.json:
            _print_json(raw)
            return
        scores = _json_object(raw)
        print(f"Impact values saved as version {scores['version']}.")
        _print_risk(scores)

    _with_session(base_url, args, action)


def _legal_duty_add(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    due_at = _parse_timestamp(args.due_at, "--due-at", not_in_future=False)
    _write_case(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/legal-duties",
        payload={
            "kind": args.kind,
            "description": args.description,
            "source_reference": args.source_reference,
            "jurisdiction": args.jurisdiction,
            "due_at": due_at.isoformat(),
        },
        messages=LEGAL_DUTY_MESSAGES,
        render=_render_duty("Legal duty added."),
    )


def _legal_duty_update(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    duty_id = _uuid_arg(args.duty_id, "DUTY_ID")
    # Only the fields given on the command line are sent. An empty --jurisdiction clears it.
    changes: dict[str, str] = {}
    if args.description is not None:
        changes["description"] = args.description
    if args.source_reference is not None:
        changes["source_reference"] = args.source_reference
    if args.jurisdiction is not None:
        changes["jurisdiction"] = args.jurisdiction
    if args.due_at is not None:
        changes["due_at"] = _parse_timestamp(
            args.due_at, "--due-at", not_in_future=False
        ).isoformat()
    if args.status is not None:
        changes["status"] = args.status
    if not changes:
        raise UsageError(
            "give at least one field to change: --description, --source-reference, --jurisdiction, --due-at or --status"
        )
    if args.revision < 1:
        raise UsageError(
            "--revision must be 1 or more. Read it with: case legal-duty list CASE_ID"
        )
    try:
        _write_case(
            base_url,
            args,
            "PATCH",
            f"/api/v1/cases/{case_id}/legal-duties/{duty_id}",
            payload=changes,
            messages=LEGAL_DUTY_UPDATE_MESSAGES,
            render=_render_duty("Legal duty updated."),
            if_match=args.revision,
        )
    except RequestFailed as error:
        current = _precondition_revision(error)
        if current is None:
            raise
        # Nothing was saved. The command never retries: the user reads the duties again and runs it again.
        raise RequestFailed(
            f"the legal duty changed, so nothing was saved. Its current revision is {current}. Run "
            f"'case legal-duty list {case_id}' to check it, then run this command again with --revision {current}"
        ) from None


def _awareness_add(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    occurred_at = _parse_timestamp(
        args.occurred_at, "--occurred-at", not_in_future=True
    )
    _write_case(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/awareness-events",
        payload={
            "awareness_kind": args.awareness_kind,
            "event_kind": args.event_kind,
            "occurred_at": occurred_at.isoformat(),
            "basis": args.basis,
        },
        messages=AWARENESS_MESSAGES,
        render=_render_awareness,
    )


def _json_list(raw: bytes) -> list:
    try:
        body = json.loads(raw)
    except ValueError:
        raise RequestFailed("unexpected response from the server") from None
    if not isinstance(body, list):
        raise RequestFailed("unexpected response from the server")
    return body


def _sees_full_view(base: str, token: str) -> bool:
    """Whether the caller holds cases:read_all. The API returns free text only to that permission, so a null free text
    means "not visible" for other callers and "not set" for these. Only the human output asks."""
    me = _json_object(_call(base, "GET", "/api/v1/auth/me", token=token))
    return "cases:read_all" in me.get("permissions", [])


def _free_text(value: str | None, full_view: bool) -> str:
    if value:
        return value
    return "-" if full_view else NOT_VISIBLE_TEXT


def _print_table(rows: list[tuple[str, ...]]) -> None:
    """rows[0] is the header. Every column but the last is padded to its widest cell."""
    widths = [
        max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)
    ]
    for row in rows:
        cells = [row[column].ljust(widths[column]) for column in range(len(widths))] + [
            row[-1]
        ]
        print("  ".join(cells).rstrip())


def _print_field(label: str, value: str) -> None:
    print(f"  {label + ':':<26}{value}")


def _case_read(
    base_url: str, args: argparse.Namespace, path: str, messages: dict[int, str], render
) -> None:
    """One GET in its own login session. --json prints the API body as received. Otherwise render gets the raw body
    and whether the caller holds cases:read_all, which decides how a null free text is shown."""

    def action(base: str, token: str) -> None:
        raw = _call(base, "GET", path, token=token, messages=messages)
        if args.json:
            _print_json(raw)
        else:
            render(raw, _sees_full_view(base, token))

    _with_session(base_url, args, action)


def _classification_show(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)

    def render(raw: bytes, full_view: bool) -> None:
        classification = _json_object(raw)
        if classification["version"] == 0:
            print("No classification recorded for this case.")
            return
        print(f"Classification version {classification['version']}:")
        _print_field(
            "Triggers", ", ".join(classification["triggers"]) or "not assessed"
        )
        _print_field("Event kind", classification["event_kind"] or "-")
        for label, field in CLASSIFICATION_FREE_TEXT:
            _print_field(label, _free_text(classification[field], full_view))
        _print_field("Next update (UTC)", classification["next_update_due_at"] or "-")
        _print_field("Saved (UTC)", classification["created_at"] or "-")

    _case_read(
        base_url, args, f"/api/v1/cases/{case_id}/classification", READ_MESSAGES, render
    )


def _impact_list(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)

    def render(raw: bytes, full_view: bool) -> None:
        rows = [("AREA", "SEVERITY", "SCOPE", "UPDATED (UTC)")]
        for impact in _json_list(raw):
            # An area with no assessment is listed as not assessed. That is not the same as severity none.
            if impact["assessed"]:
                rows.append(
                    (
                        impact["area"],
                        impact["severity"] or "-",
                        _free_text(impact["scope"], full_view),
                        impact["updated_at"] or "-",
                    )
                )
            else:
                rows.append((impact["area"], "not assessed", "-", "-"))
        _print_table(rows)

    _case_read(
        base_url, args, f"/api/v1/cases/{case_id}/impacts", READ_MESSAGES, render
    )


def _legal_duty_list(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)

    def render(raw: bytes, full_view: bool) -> None:
        duties = _json_list(raw)
        if not duties:
            print("No legal duties recorded for this case.")
            return
        # The REVISION column is the value for --revision in 'case legal-duty update'.
        rows = [
            (
                "ID",
                "KIND",
                "STATUS",
                "DUE (UTC)",
                "REVISION",
                "JURISDICTION",
                "SOURCE REFERENCE",
                "DESCRIPTION",
            )
        ]
        rows += [
            (
                duty["id"],
                duty["kind"],
                duty["status"],
                duty["due_at"],
                str(duty["revision"]),
                _free_text(duty["jurisdiction"], full_view),
                _free_text(duty["source_reference"], full_view),
                _free_text(duty["description"], full_view),
            )
            for duty in duties
        ]
        _print_table(rows)

    _case_read(
        base_url, args, f"/api/v1/cases/{case_id}/legal-duties", READ_MESSAGES, render
    )


def _awareness_list(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)

    def render(raw: bytes, full_view: bool) -> None:
        events = _json_list(raw)
        if not events:
            print("No awareness events recorded for this case.")
            return
        rows = [("ID", "AWARENESS KIND", "EVENT KIND", "OCCURRED (UTC)", "BASIS")]
        rows += [
            (
                event["id"],
                event["awareness_kind"],
                event["event_kind"],
                event["occurred_at"],
                _free_text(event["basis"], full_view),
            )
            for event in events
        ]
        _print_table(rows)

    _case_read(
        base_url,
        args,
        f"/api/v1/cases/{case_id}/awareness-events",
        READ_MESSAGES,
        render,
    )


def _render_report(report: dict) -> None:
    classification = report["classification"]
    impacts = report["impacts"]
    duties = report["legal_duties"]
    print(
        f"Preliminary report for {report['reference']} (case {report['case_status']})"
    )
    _print_field("Generated (UTC)", report["generated_at"])
    _print_field("Awareness (UTC)", report["awareness_time"])
    _print_field("Complete", "yes" if report["complete"] else "no")
    if report["missing_fields"]:
        print("  Missing fields:")
        for item in report["missing_fields"]:
            print(f"    {item['field']}: {item['state']}")
    else:
        _print_field("Missing fields", "none")
    _print_field("Contact", report["contact"]["status"])
    if classification["version"] == 0:
        _print_field("Classification", "none recorded")
    else:
        triggers = ", ".join(classification["triggers"]) or "not assessed"
        _print_field(
            "Classification",
            f"version {classification['version']}, triggers: {triggers}",
        )
    _print_field("Awareness events", str(len(report["awareness_events"])))
    assessed = sum(1 for impact in impacts if impact["assessed"])
    _print_field("Impacts assessed", f"{assessed} of {len(impacts)}")
    open_duties = sum(1 for duty in duties if duty["status"] == "open")
    _print_field("Legal duties", f"{len(duties)} ({open_duties} open)")


WEEKLY_MESSAGES = {
    403: "forbidden: weekly updates need cases:read_own, cases:read_assigned or cases:read_all",
    404: "case or weekly update not found",
}


def _weekly_list(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)

    def render(raw: bytes, full_view: bool) -> None:
        updates = _json_list(raw)
        if not updates:
            print(
                "No weekly updates for this case. One is made each week while it has an open significant risk."
            )
            return
        rows = [("SEQUENCE", "GENERATED (UTC)", "NEXT DUE (UTC)", "RISKS", "UPDATE ID")]
        for update in updates:
            rows.append(
                (
                    str(update["sequence"]),
                    update["generated_at"],
                    update["next_update_due_at"],
                    str(len(update["significant_risks"])),
                    update["weekly_update_id"],
                )
            )
        _print_table(rows)

    _case_read(
        base_url,
        args,
        f"/api/v1/cases/{case_id}/weekly-updates",
        WEEKLY_MESSAGES,
        render,
    )


def _weekly_show(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    update_id = _uuid_arg(args.update_id, "UPDATE_ID")

    def render(raw: bytes, full_view: bool) -> None:
        update = _json_object(raw)
        print(f"Weekly update {update['sequence']} of {update['case_reference']}:")
        _print_field("Update ID", update["weekly_update_id"])
        _print_field("Generated (UTC)", update["generated_at"])
        _print_field("Next due (UTC)", update["next_update_due_at"])
        print("  Significant risks:")
        for risk in update["significant_risks"]:
            print(f"    {risk['area']}: {risk['severity']}")
        print("  Deadline tasks:")
        for task in update["deadline_tasks"]:
            print(f"    {task['kind']}: due {task['due_at']}, {task['status']}")

    _case_read(
        base_url,
        args,
        f"/api/v1/cases/{case_id}/weekly-updates/{update_id}",
        WEEKLY_MESSAGES,
        render,
    )


def _report_show(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            f"/api/v1/cases/{case_id}/preliminary-report",
            token=token,
            messages=REPORT_MESSAGES,
        )
        if args.json:
            _print_json(raw)
        else:
            _render_report(_json_object(raw))

    _with_session(base_url, args, action)


# --- disclosure packages (safe_kit.disclosure_api) ----------------------------------------------------------------

DISCLOSURE_TEMPLATES = (
    "confidential_rapid_alert",
    "de_identified_member_advisory",
    "redacted_public_safety_report",
)
DISCLOSURE_AUDIENCES = ("RED", "AMBER", "AMBER+STRICT", "GREEN", "CLEAR")
DISCLOSURE_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DISCLOSURE_LIST_MESSAGES = {
    404: "case not found, or the case is not visible to your account"
}
DISCLOSURE_VERSION_MESSAGES = {
    404: "case or disclosure version not found, or the case is not visible to your account"
}
DISCLOSURE_APPROVAL_TEXT = {
    "approved": "approved for this version",
    "not_approved": "not approved yet",
    "revoked_by_change": "revoked by a later version",
}
# The text for each status the API gives a refused disclosure write. Unknown statuses keep the API's HTTP message.
DISCLOSURE_STATUS_TEXT = {
    "case_closed": "the case is closed, so its disclosure packages cannot change",
    "evidence_not_eligible": (
        "one or more evidence items cannot be used: each must be a redacted derivative of this case that is available "
        "and in your TLP audience. Originals cannot be listed"
    ),
    "audience_wider_than_evidence": "the TLP audience is wider than one of the evidence items allows. Choose a narrower audience",
    "no_change": "the package content did not change, so no new version was saved",
    "not_latest_version": "a newer version of this package exists, so only its latest version can be approved",
    "already_approved": "this version already has an approval",
    "preparer_is_approver": "you prepared this version, so another approver must approve it",
    "approval_binding_mismatch": (
        "the recipient, purpose or digest does not match the version. Copy them exactly from 'case disclosure show'"
    ),
    "evidence_not_visible": "one or more items are outside your TLP audience, so you cannot approve this version",
    "digest_mismatch": "the stored content does not match its digest, so nothing was approved. Report this to an administrator",
    "version_not_approved": (
        "only an approved version can be exported. Approve it first with 'case disclosure approve', then export it again"
    ),
    "idempotency_key_reused": "this Idempotency-Key was used with another request. Run again without --idempotency-key",
}


def _disclosure_failure(error: RequestFailed) -> RequestFailed:
    """Replace the message of a refused disclosure write with the text for its status, when the API names one."""
    detail = error.detail
    status = detail.get("status") if isinstance(detail, dict) else None
    if status == "version_conflict":
        text = (
            "the package has a newer version, so nothing was saved. Run 'case disclosure show CASE_ID' to read the latest "
            f"version, then run this command again with --expected-version {detail.get('current_version')}"
        )
    else:
        text = DISCLOSURE_STATUS_TEXT.get(status or "")
    if text is None:
        return error
    return RequestFailed(text, status=error.status, detail=detail)


def _disclosure_write(
    base_url: str,
    args: argparse.Namespace,
    method: str,
    path: str,
    payload: dict,
    render,
    failure=None,
) -> None:
    """One disclosure write in its own login session, with an Idempotency-Key. --json prints the API body.

    failure maps a refused write to its message; the disclosure messages are the default. The notice commands pass their own.
    """
    headers = {"Idempotency-Key": args.idempotency_key or str(uuid.uuid4())}
    to_message = failure or _disclosure_failure

    def action(base: str, token: str) -> None:
        try:
            raw = _call(
                base, method, path, token=token, payload=payload, extra_headers=headers
            )
        except RequestFailed as error:
            raise to_message(error) from None
        if args.json:
            _print_json(raw)
        else:
            render(_json_object(raw))

    _with_session(base_url, args, action)


def _print_preview(preview: dict) -> None:
    template = preview["template"]
    print(f"Disclosure version {preview['version']}, {template['title']}:")
    _print_field("Version ID", preview["version_id"])
    _print_field("Package ID", preview["package_id"])
    _print_field("TLP audience", preview["tlp_audience"])
    _print_field("Recipient", preview["recipient"])
    _print_field("Purpose", preview["purpose"])
    _print_field("Digest", preview["digest"])
    _print_field("Approval", DISCLOSURE_APPROVAL_TEXT[preview["approval_state"]])
    print("  Sections:")
    for section in template["sections"]:
        print(f"    {section['title']}: {section['contains']}")
    print("  Evidence items (derivatives only):")
    if not preview["evidence"]:
        print("    none")
    for item in preview["evidence"]:
        if item["withheld"]:
            print("    withheld: outside your TLP audience")
        else:
            print(
                f"    {item['evidence_id']}  {item['media_type']}  {item['size_bytes']} bytes  "
                f"TLP {item['tlp_label']}  {item['removed_lines']} line(s) removed"
            )
    print(preview["notice"])


def _render_disclosure_list(raw: bytes, _full_view: bool) -> None:
    items = _json_object(raw)["items"]
    if not items:
        print("No disclosure package version exists for this case.")
        return
    for item in items:
        print(
            f"  {item['id']}  package {item['package_id']}  version {item['version']}  {item['template']}  "
            f"TLP {item['tlp_audience']}  {DISCLOSURE_APPROVAL_TEXT[item['approval_state']]}"
        )


def _render_disclosure_created(version: dict) -> None:
    print(
        "Disclosure version saved. It has no approval yet: an approver must approve this exact version before it can be sent."
    )
    _print_field("Version ID", version["id"])
    _print_field("Package ID", version["package_id"])
    _print_field("Version", str(version["version"]))
    _print_field("Digest", version["digest"])


def _render_disclosure_approved(version: dict) -> None:
    approval = version["approval"]
    print(
        "Approval recorded. It covers this version and its digest only; a later version needs its own approval."
    )
    _print_field("Version ID", version["id"])
    _print_field("Digest", approval["digest"])
    _print_field("Approved by", approval["approved_by"])
    _print_field("Approved (UTC)", approval["approved_at"])


def _disclosure_create(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    evidence_ids = [_uuid_arg(text, "--evidence") for text in args.evidence]
    if len(evidence_ids) > 50:
        raise UsageError("--evidence can be given at most 50 times")
    if (args.package_id is None) != (args.expected_version is None):
        raise UsageError(
            "--package-id and --expected-version go together: give both to add a version to a package, or neither for a new one"
        )
    if args.expected_version is not None and args.expected_version < 1:
        raise UsageError(
            "--expected-version must be 1 or more. Read it with: case disclosure show CASE_ID"
        )
    payload = {
        "package_id": _uuid_arg(args.package_id, "--package-id")
        if args.package_id
        else None,
        "expected_version": args.expected_version,
        "template": args.template,
        "tlp_audience": args.tlp_audience,
        "recipient": args.recipient,
        "purpose": args.purpose,
        "evidence_ids": evidence_ids,
    }
    _disclosure_write(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/disclosures",
        payload,
        _render_disclosure_created,
    )


def _disclosure_show(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    if args.version_id is None:
        _case_read(
            base_url,
            args,
            f"/api/v1/cases/{case_id}/disclosures",
            DISCLOSURE_LIST_MESSAGES,
            _render_disclosure_list,
        )
        return
    version_id = _uuid_arg(args.version_id, "VERSION_ID")
    _case_read(
        base_url,
        args,
        f"/api/v1/cases/{case_id}/disclosures/{version_id}/preview",
        DISCLOSURE_VERSION_MESSAGES,
        lambda raw, _full_view: _print_preview(_json_object(raw)),
    )


def _disclosure_approve(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    version_id = _uuid_arg(args.version_id, "VERSION_ID")
    if not DISCLOSURE_DIGEST_PATTERN.match(args.digest):
        raise UsageError(
            "--digest must be the 64-character digest shown by 'case disclosure show CASE_ID VERSION_ID'"
        )
    payload = {
        "recipient": args.recipient,
        "purpose": args.purpose,
        "digest": args.digest,
    }
    _disclosure_write(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/disclosures/{version_id}/approvals",
        payload,
        _render_disclosure_approved,
    )


def _disclosure_export(base_url: str, args: argparse.Namespace) -> None:
    """Export one approved version. The JSON is the API body as received; it holds no evidence content."""
    case_id = _case_id_arg(args.case_id)
    version_id = _uuid_arg(args.version_id, "VERSION_ID")
    captured: dict[str, bytes] = {}

    def action(base: str, token: str) -> None:
        try:
            captured["raw"] = _call(
                base,
                "GET",
                f"/api/v1/cases/{case_id}/disclosures/{version_id}/export",
                token=token,
                messages=DISCLOSURE_VERSION_MESSAGES,
            )
        except RequestFailed as error:
            raise _disclosure_failure(error) from None

    _with_session(base_url, args, action)
    _write_document(captured["raw"], args.out)


# --- external notices (safe_kit.external_notices_api) -----------------------------------------------------------------

NOTICE_CHANNELS = ("email", "postal", "webhook", "secure_manual", "other")
NOTICE_UNKNOWN_REASONS = ("provider_accepted_no_receipt", "timeout", "no_answer")
NOTICE_FOLLOW_UP_ACTIONS = ("require", "complete")
NOTICE_RECIPIENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
NOTICE_REASON_LIMIT = 500
NOTICE_CHANNEL_OTHER_LIMIT = 200
NOTICE_LIST_MESSAGES = {
    404: "case or disclosure version not found, or the case is not visible to your account"
}
NOTICE_STATE_TEXT = {
    "sent": "sent; no receipt recorded yet",
    "receipt_received": "receipt received",
    "receipt_unknown": "receipt unknown",
    "follow_up_required": "follow-up required",
    "follow_up_done": "follow-up done",
    "cancelled": "cancelled",
}
# The text for each status the API gives a refused notice write. Unknown statuses keep the API's HTTP message.
NOTICE_STATUS_TEXT = {
    "state_conflict": (
        "the notice is not in a state that allows this change. Run 'case notice list CASE_ID VERSION_ID' to read its state"
    ),
    "case_closed": "the case is closed, so its notices cannot change",
    "version_not_approved": (
        "the version is not approved, so no notice can be recorded for it. An approver must approve this exact version first"
    ),
    "version_superseded": (
        "a newer version of the package exists, so no notice can be recorded for this one. Use the latest approved version"
    ),
    "approval_mismatch": "the approval does not match the version. Report this to an administrator",
    "digest_mismatch": "the stored content does not match its digest, so nothing was recorded. Report this to an administrator",
    "release_approval_missing": "a CLEAR item has no release approval, so no notice can be recorded for this version",
    "time_out_of_range": (
        "the time is before the approval, before the notice was sent, or in the future. Enter the time it happened"
    ),
    "repeat_reason_required": (
        "this recipient already has a notice for this version. Give --repeat-reason to record a second notice"
    ),
    "manual_fallback_required": (
        "your account records notices on the manual path only. Give --manual-fallback and --fallback-reason"
    ),
    "preparer_cannot_record": (
        "you prepared this version, so another person must record its notice. Ask an independent person to record it"
    ),
    "idempotency_key_reused": "this Idempotency-Key was used with another request. Run again without --idempotency-key",
}


def _notice_failure(error: RequestFailed) -> RequestFailed:
    """Replace the message of a refused notice write with the text for its status, when the API names one."""
    detail = error.detail
    status = detail.get("status") if isinstance(detail, dict) else None
    if status == "state_conflict" and isinstance(detail, dict):
        current = NOTICE_STATE_TEXT.get(str(detail.get("current_state")), "unknown")
        text = (
            f"this change is not allowed while the notice is '{current}'. "
            "Run 'case notice list CASE_ID VERSION_ID' to read its state"
        )
    else:
        text = NOTICE_STATUS_TEXT.get(status or "")
    if text is None:
        return error
    return RequestFailed(text, status=error.status, detail=detail)


def _notice_text(value: str, option: str, limit: int) -> str:
    text = value.strip()
    if not text:
        raise UsageError(f"{option} cannot be empty")
    if len(text) > limit:
        raise UsageError(f"{option} can be at most {limit} characters")
    return text


def _notice_base(case_id: str, version_id: str) -> str:
    return f"/api/v1/cases/{case_id}/disclosures/{version_id}/notices"


def _render_notice(notice: dict) -> None:
    print(
        f"Notice {notice['id']} (revision {notice['revision']}): {NOTICE_STATE_TEXT[notice['state']]}"
    )
    _print_field("Version", f"{notice['version']} ({notice['version_id']})")
    channel = (
        notice["channel"]
        if notice["channel_other"] is None
        else f"other: {notice['channel_other']}"
    )
    _print_field("Channel", channel)
    _print_field("Recipient reference", notice["recipient_ref"])
    _print_field("Sent (UTC)", notice["sent_at"])
    _print_field("Recorded by", notice["recorded_by"])
    if notice["received_at"] is not None:
        _print_field("Received (UTC)", notice["received_at"])
    if notice["state_reason_code"] is not None:
        _print_field("No receipt because", notice["state_reason_code"])
    if notice["state_reason"] is not None:
        _print_field("Reason", notice["state_reason"])
    if notice["manual_fallback"]:
        _print_field("Manual path", notice["fallback_reason"])


def _render_notice_recorded(notice: dict) -> None:
    print(
        "Notice recorded as sent. No receipt is assumed: record the receipt, or that it is unknown, with 'case notice receipt'."
    )
    _render_notice(notice)


def _render_notice_list(raw: bytes, _full_view: bool) -> None:
    items = _json_object(raw)["items"]
    if not items:
        print("No external notice exists for this disclosure version.")
        return
    for item in items:
        print(
            f"  {item['id']}  version {item['version']}  {item['channel']}  {item['recipient_ref']}  "
            f"sent {item['sent_at']}  {NOTICE_STATE_TEXT[item['state']]}"
        )


def _notice_record(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    version_id = _uuid_arg(args.version_id, "VERSION_ID")
    if (args.channel == "other") != (args.channel_other is not None):
        raise UsageError(
            "--channel-other is required with --channel other, and only then"
        )
    channel_other = None
    if args.channel_other is not None:
        channel_other = _notice_text(
            args.channel_other, "--channel-other", NOTICE_CHANNEL_OTHER_LIMIT
        )
    if not NOTICE_RECIPIENT_PATTERN.match(args.recipient_ref):
        raise UsageError(
            "--recipient-ref is an identifier of the recipient kept outside SAFE: letters, digits, '.', '_', ':' or '-', "
            "up to 64 characters. Do not put a name or an address"
        )
    if args.manual_fallback != (args.fallback_reason is not None):
        raise UsageError(
            "--fallback-reason is required with --manual-fallback, and only then"
        )
    sent_at = _parse_timestamp(args.sent_at, "--sent-at", not_in_future=True)
    payload = {
        "channel": args.channel,
        "channel_other": channel_other,
        "recipient_ref": args.recipient_ref,
        "sent_at": sent_at.isoformat(),
        "manual_fallback": args.manual_fallback,
        "fallback_reason": _notice_text(
            args.fallback_reason, "--fallback-reason", NOTICE_REASON_LIMIT
        )
        if args.fallback_reason is not None
        else None,
        "repeat_reason": _notice_text(
            args.repeat_reason, "--repeat-reason", NOTICE_REASON_LIMIT
        )
        if args.repeat_reason is not None
        else None,
    }
    _disclosure_write(
        base_url,
        args,
        "POST",
        _notice_base(case_id, version_id),
        payload,
        _render_notice_recorded,
        failure=_notice_failure,
    )


def _notice_list(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    version_id = _uuid_arg(args.version_id, "VERSION_ID")
    _case_read(
        base_url,
        args,
        _notice_base(case_id, version_id),
        NOTICE_LIST_MESSAGES,
        _render_notice_list,
    )


def _notice_receipt(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    version_id = _uuid_arg(args.version_id, "VERSION_ID")
    notice_id = _uuid_arg(args.notice_id, "NOTICE_ID")
    reason = (
        _notice_text(args.reason, "--reason", NOTICE_REASON_LIMIT)
        if args.reason is not None
        else None
    )
    if args.received_at is not None:
        received_at = _parse_timestamp(
            args.received_at, "--received-at", not_in_future=True
        )
        payload = {
            "outcome": "received",
            "received_at": received_at.isoformat(),
            "reason_code": None,
            "reason": reason,
        }
    else:
        payload = {
            "outcome": "unknown",
            "received_at": None,
            "reason_code": args.unknown,
            "reason": reason,
        }
    _disclosure_write(
        base_url,
        args,
        "POST",
        f"{_notice_base(case_id, version_id)}/{notice_id}/receipt",
        payload,
        _render_notice,
        failure=_notice_failure,
    )


def _notice_follow_up(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    version_id = _uuid_arg(args.version_id, "VERSION_ID")
    notice_id = _uuid_arg(args.notice_id, "NOTICE_ID")
    payload = {
        "action": args.action,
        "reason": _notice_text(args.reason, "--reason", NOTICE_REASON_LIMIT),
    }
    _disclosure_write(
        base_url,
        args,
        "POST",
        f"{_notice_base(case_id, version_id)}/{notice_id}/follow-up",
        payload,
        _render_notice,
        failure=_notice_failure,
    )


def _notice_cancel(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    version_id = _uuid_arg(args.version_id, "VERSION_ID")
    notice_id = _uuid_arg(args.notice_id, "NOTICE_ID")
    payload = {"reason": _notice_text(args.reason, "--reason", NOTICE_REASON_LIMIT)}
    _disclosure_write(
        base_url,
        args,
        "POST",
        f"{_notice_base(case_id, version_id)}/{notice_id}/cancel",
        payload,
        _render_notice,
        failure=_notice_failure,
    )


def _add_idempotency_option(parser: argparse.ArgumentParser) -> None:
    """--idempotency-key for a write. A new run gets a new key; pass the key of an earlier run to retry it safely."""
    parser.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help="Idempotency-Key, 1 to 128 visible ASCII characters. Default: a new key for this run. "
        "Reuse the key of an earlier run to retry it safely",
    )


def _add_auth_options(
    parser: argparse.ArgumentParser, *, json_output: bool = True
) -> None:
    parser.add_argument(
        "--username",
        help="Account name. Defaults to the SAFE_USERNAME environment variable",
    )
    parser.add_argument(
        "--demo-username",
        metavar="USERNAME",
        help="Sign in as this demo role (demo mode only). Needs --demo-credential-file. "
        "Defaults to the SAFE_DEMO_USERNAME environment variable",
    )
    parser.add_argument(
        "--demo-credential-file",
        metavar="PATH",
        help="File that holds the demo role's credential. Only its first line is read and never printed. "
        "Defaults to the SAFE_DEMO_CREDENTIAL_FILE environment variable",
    )
    if json_output:
        parser.add_argument(
            "--json", action="store_true", help="Print the API response body as JSON"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="safe-kit")
    parser.add_argument(
        "--no-banner", action="store_true", help="Do not print the startup banner"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("health", help="Check whether the API is ready")

    case = subcommands.add_parser(
        "case", help="Create, import, list and show your cases"
    )
    case_commands = case.add_subparsers(dest="case_command", required=True)

    create = case_commands.add_parser("create", help="Submit a new case")
    create.add_argument(
        "--title", required=True, help="Case title (1 to 200 characters)"
    )
    create.add_argument(
        "--awareness-time",
        required=True,
        metavar="ISO8601",
        help="When you became aware, with a timezone, for example 2026-10-30T09:00:00Z",
    )
    _add_idempotency_option(create)
    _add_auth_options(create)
    create.set_defaults(handler=_case_create)

    listing = case_commands.add_parser("list", help="List your cases, newest first")
    listing.add_argument(
        "--limit", type=int, default=DEFAULT_PAGE_SIZE, help="Cases per page (1 to 200)"
    )
    listing.add_argument(
        "--cursor",
        default=None,
        metavar="CURSOR",
        help="Continue from the cursor printed as 'Next page'",
    )
    listing.add_argument(
        "--status",
        choices=CASE_STATUS_CHOICES,
        default=None,
        help="Only cases with this status (default: all)",
    )
    _add_auth_options(listing)
    listing.set_defaults(handler=_case_list)

    show = case_commands.add_parser("show", help="Show one of your cases")
    show.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(show)
    show.set_defaults(handler=_case_show)

    imported = case_commands.add_parser(
        "import", help="Import case records from a JSON file in the project format"
    )
    imported.add_argument(
        "file",
        metavar="FILE",
        help='JSON file: {"import_format_version": 1, "records": [...]}',
    )
    imported.add_argument(
        "--dry-run",
        action="store_true",
        help="Check the file and report what the import would do. Nothing is written",
    )
    _add_idempotency_option(imported)
    _add_auth_options(imported)
    imported.set_defaults(handler=_case_import)
    _add_case_update_commands(case_commands)
    _add_case_report_commands(case_commands)

    evidence = subcommands.add_parser(
        "evidence", help="Add, list, show and redact the evidence of a case"
    )
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)

    added = evidence_commands.add_parser(
        "add", help="Upload a file as evidence for a case"
    )
    added.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    added.add_argument(
        "file",
        metavar="FILE",
        help="File to upload: PDF, PNG, JPEG, plain text or JSON, up to 50 MiB by default",
    )
    _add_idempotency_option(added)
    _add_auth_options(added)
    added.set_defaults(handler=_evidence_add)

    evidence_list = evidence_commands.add_parser(
        "list", help="List the evidence of one of your cases"
    )
    evidence_list.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    evidence_list.add_argument(
        "--limit", type=int, default=DEFAULT_PAGE_SIZE, help="Items per page (1 to 200)"
    )
    evidence_list.add_argument(
        "--offset", type=int, default=0, help="Number of items to skip"
    )
    _add_auth_options(evidence_list)
    evidence_list.set_defaults(handler=_evidence_list)
    _add_evidence_commands(evidence_commands)

    task = subcommands.add_parser("task", help="Show the deadline tasks of a case")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    task_listing = task_commands.add_parser(
        "list", help="List the deadline tasks of one of your cases"
    )
    task_listing.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(task_listing)
    task_listing.set_defaults(handler=_task_list)

    task_update = task_commands.add_parser(
        "update",
        help="Set a deadline task to open, done or not-applicable. Not applicable needs a reason",
    )
    task_update.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    task_update.add_argument(
        "task_id", metavar="TASK_ID", help="Deadline task ID (UUID), from task list"
    )
    task_update.add_argument(
        "--status",
        required=True,
        choices=["open", "done", "not-applicable"],
        help="New status",
    )
    task_update.add_argument(
        "--reason",
        help="Why the deadline does not apply, 1 to 1000 characters (not-applicable only)",
    )
    task_update.add_argument(
        "--revision",
        type=int,
        required=True,
        help="Revision of the task you read with 'task list'",
    )
    _add_idempotency_option(task_update)
    _add_auth_options(task_update)
    task_update.set_defaults(handler=_task_update)

    task_restore = task_commands.add_parser(
        "restore",
        help="Turn off a not-applicable deadline: it returns to the status it had before",
    )
    task_restore.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    task_restore.add_argument(
        "task_id", metavar="TASK_ID", help="Deadline task ID (UUID), from task list"
    )
    _add_idempotency_option(task_restore)
    _add_auth_options(task_restore)
    task_restore.set_defaults(handler=_task_restore)
    _add_catalog_commands(subcommands)
    return parser


def _add_case_update_commands(case_commands) -> None:
    """The commands that change a case, and the read commands of the record groups that also have a write command.
    Each one is a thin wrapper over one API route."""
    close = case_commands.add_parser(
        "close", help="Close a case. It needs a review and every deadline task settled"
    )
    close.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    close.add_argument(
        "--reason", required=True, help="Why the case is closed (required)"
    )
    _add_idempotency_option(close)
    _add_auth_options(close)
    close.set_defaults(handler=_case_close)

    reopen = case_commands.add_parser("reopen", help="Reopen a closed case")
    reopen.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    reopen.add_argument(
        "--reason", required=True, help="Why the case is reopened (required)"
    )
    _add_idempotency_option(reopen)
    _add_auth_options(reopen)
    reopen.set_defaults(handler=_case_reopen)

    assign = case_commands.add_parser(
        "assign",
        help="Assign an active user whose role holds cases:assignable to a case",
    )
    assign.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    assign.add_argument(
        "--user-id",
        required=True,
        metavar="USER_ID",
        help="ID (UUID) of the user to assign",
    )
    _add_idempotency_option(assign)
    _add_auth_options(assign)
    assign.set_defaults(handler=_case_assign)

    unassign = case_commands.add_parser("unassign", help="Remove a user from a case")
    unassign.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    unassign.add_argument(
        "--user-id",
        required=True,
        metavar="USER_ID",
        help="ID (UUID) of the user to remove",
    )
    _add_auth_options(unassign, json_output=False)
    unassign.set_defaults(handler=_case_unassign)

    link = case_commands.add_parser(
        "link",
        help="Link two cases that concern the same incident. A link changes nothing else",
    )
    link_commands = link.add_subparsers(dest="link_command", required=True)
    link_add = link_commands.add_parser(
        "add", help="Link a case to another case you can see"
    )
    link_add.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    link_add.add_argument(
        "linked_case_id",
        metavar="LINKED_CASE_ID",
        help="Case ID (UUID) of the other case",
    )
    _add_idempotency_option(link_add)
    _add_auth_options(link_add)
    link_add.set_defaults(handler=_case_link_add)
    link_list = link_commands.add_parser(
        "list", help="List the cases linked to one of your cases"
    )
    link_list.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(link_list)
    link_list.set_defaults(handler=_case_link_list)
    link_remove = link_commands.add_parser(
        "remove", help="Remove the link between two cases"
    )
    link_remove.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    link_remove.add_argument(
        "linked_case_id",
        metavar="LINKED_CASE_ID",
        help="Case ID (UUID) of the linked case",
    )
    _add_auth_options(link_remove, json_output=False)
    link_remove.set_defaults(handler=_case_link_remove)
    model = case_commands.add_parser(
        "model",
        help="Record, list and revoke the model, runtime and reasoning configuration",
    )
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_record = model_commands.add_parser(
        "record",
        help="Record the configuration of an agent run. Identifiers only: no prompt and no reasoning text",
    )
    model_record.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    model_record.add_argument(
        "--model-name", required=True, help="Model name, up to 200 characters"
    )
    model_record.add_argument(
        "--model-version", required=True, help="Model version, up to 200 characters"
    )
    model_record.add_argument(
        "--runtime",
        required=True,
        help="Runtime name and version, up to 200 characters",
    )
    model_record.add_argument(
        "--provider-alias", required=True, help="Provider alias, up to 200 characters"
    )
    model_record.add_argument(
        "--reasoning-mode",
        required=True,
        choices=REASONING_MODES,
        help="Whether reasoning was on: " + ", ".join(REASONING_MODES),
    )
    model_record.add_argument(
        "--reasoning-effort",
        choices=REASONING_EFFORTS,
        help="Effort, only with --reasoning-mode enabled",
    )
    model_record.add_argument(
        "--reasoning-budget-tokens",
        type=int,
        metavar="N",
        help="Token budget, only with --reasoning-mode enabled",
    )
    model_record.add_argument(
        "--evidence-item",
        metavar="EVIDENCE_ID",
        help="Link to one evidence item of the case (UUID)",
    )
    model_record.add_argument(
        "--review-round",
        metavar="ROUND_ID",
        help="Link to one review round of the case (UUID)",
    )
    _add_idempotency_option(model_record)
    _add_auth_options(model_record)
    model_record.set_defaults(handler=_model_record_add)
    model_list = model_commands.add_parser(
        "list",
        help="List the model records of a case. Revoked records are shown unless --current-only is given",
    )
    model_list.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    model_list.add_argument(
        "--current-only", action="store_true", help="Leave out revoked records"
    )
    _add_auth_options(model_list)
    model_list.set_defaults(handler=_model_record_list)
    model_revoke = model_commands.add_parser(
        "revoke",
        help="Revoke a model record with a reason. The record stays readable for audit",
    )
    model_revoke.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    model_revoke.add_argument(
        "record_id", metavar="RECORD_ID", help="Model record ID (UUID)"
    )
    model_revoke.add_argument(
        "--reason",
        required=True,
        help="Why the record is revoked, up to 1000 characters",
    )
    _add_idempotency_option(model_revoke)
    _add_auth_options(model_revoke)
    model_revoke.set_defaults(handler=_model_record_revoke)

    disclosure = case_commands.add_parser(
        "disclosure",
        help="Create, show, approve and export disclosure packages: redacted derivatives for one recipient and purpose",
    )
    disclosure_commands = disclosure.add_subparsers(
        dest="disclosure_command", required=True
    )
    disclosure_create = disclosure_commands.add_parser(
        "create",
        help="Save a disclosure version: a new package, or the next version of one",
    )
    disclosure_create.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    disclosure_create.add_argument(
        "--template",
        required=True,
        choices=DISCLOSURE_TEMPLATES,
        help="Template of the package",
    )
    disclosure_create.add_argument(
        "--tlp-audience",
        required=True,
        choices=DISCLOSURE_AUDIENCES,
        help="TLP audience of the package",
    )
    disclosure_create.add_argument(
        "--recipient", required=True, help="Recipient, up to 200 characters"
    )
    disclosure_create.add_argument(
        "--purpose", required=True, help="Purpose, up to 500 characters"
    )
    disclosure_create.add_argument(
        "--evidence",
        action="append",
        required=True,
        metavar="EVIDENCE_ID",
        help="Redacted derivative evidence item (UUID). Repeat for each item, up to 50 in all",
    )
    disclosure_create.add_argument(
        "--package-id",
        metavar="PACKAGE_ID",
        help="Add a version to this package (UUID)",
    )
    disclosure_create.add_argument(
        "--expected-version",
        type=int,
        metavar="N",
        help="The latest version number of the package you read. Required with --package-id",
    )
    _add_idempotency_option(disclosure_create)
    _add_auth_options(disclosure_create)
    disclosure_create.set_defaults(handler=_disclosure_create)
    disclosure_show = disclosure_commands.add_parser(
        "show",
        help="List the disclosure versions of a case, or show one version with its preview (no evidence content)",
    )
    disclosure_show.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    disclosure_show.add_argument(
        "version_id",
        metavar="VERSION_ID",
        nargs="?",
        help="Version ID (UUID). Without it, the versions are listed",
    )
    _add_auth_options(disclosure_show)
    disclosure_show.set_defaults(handler=_disclosure_show)
    disclosure_approve = disclosure_commands.add_parser(
        "approve",
        help="Approve one version. Give its recipient, purpose and digest exactly as 'case disclosure show' prints them",
    )
    disclosure_approve.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    disclosure_approve.add_argument(
        "version_id", metavar="VERSION_ID", help="Version ID (UUID)"
    )
    disclosure_approve.add_argument(
        "--recipient", required=True, help="The version's recipient"
    )
    disclosure_approve.add_argument(
        "--purpose", required=True, help="The version's purpose"
    )
    disclosure_approve.add_argument(
        "--digest", required=True, help="The version's digest (64 hex characters)"
    )
    _add_idempotency_option(disclosure_approve)
    _add_auth_options(disclosure_approve)
    disclosure_approve.set_defaults(handler=_disclosure_approve)
    disclosure_export = disclosure_commands.add_parser(
        "export",
        help="Export one approved version as versioned JSON: derivatives, TLP, digest and approval, no evidence content. "
        "Needs disclosures:read",
    )
    disclosure_export.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    disclosure_export.add_argument(
        "version_id", metavar="VERSION_ID", help="Version ID (UUID)"
    )
    disclosure_export.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="Write the JSON to FILE instead of printing it",
    )
    _add_auth_options(disclosure_export, json_output=False)
    disclosure_export.set_defaults(handler=_disclosure_export)

    notice = case_commands.add_parser(
        "notice",
        help="Record the external notices of an approved disclosure version: the send, the receipt or unknown, follow-ups, "
        "and cancels. Nothing is sent from here",
    )
    notice_commands = notice.add_subparsers(dest="notice_command", required=True)
    notice_record = notice_commands.add_parser(
        "record",
        help="Record that you sent an approved version to a recipient. It starts as sent, with no receipt",
    )
    notice_record.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    notice_record.add_argument(
        "version_id",
        metavar="VERSION_ID",
        help="Version ID (UUID) of an approved, current version",
    )
    notice_record.add_argument(
        "--channel", required=True, choices=NOTICE_CHANNELS, help="How you sent it"
    )
    notice_record.add_argument(
        "--channel-other",
        metavar="TEXT",
        help="Describe the channel, up to 200 characters. Required with --channel other",
    )
    notice_record.add_argument(
        "--recipient-ref",
        required=True,
        metavar="REF",
        help="Identifier of the recipient kept outside SAFE, up to 64 characters: letters, digits, '.', '_', ':' or '-'",
    )
    notice_record.add_argument(
        "--sent-at",
        required=True,
        metavar="ISO8601",
        help="When you sent it, with a timezone, for example 2026-11-02T09:00:00Z",
    )
    notice_record.add_argument(
        "--manual-fallback",
        action="store_true",
        help="Record on the manual path, because the approver was unavailable. Needs --fallback-reason",
    )
    notice_record.add_argument(
        "--fallback-reason",
        metavar="TEXT",
        help="Why the manual path is used, up to 500 characters",
    )
    notice_record.add_argument(
        "--repeat-reason",
        metavar="TEXT",
        help="Why a second notice to this recipient is needed, up to 500 characters",
    )
    _add_idempotency_option(notice_record)
    _add_auth_options(notice_record)
    notice_record.set_defaults(handler=_notice_record)
    notice_list = notice_commands.add_parser(
        "list",
        help="List the notices of a disclosure version, newest first, with their current state",
    )
    notice_list.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    notice_list.add_argument(
        "version_id", metavar="VERSION_ID", help="Version ID (UUID)"
    )
    _add_auth_options(notice_list)
    notice_list.set_defaults(handler=_notice_list)
    notice_receipt = notice_commands.add_parser(
        "receipt",
        help="Record the receipt from the recipient, or that no receipt is known and why",
    )
    notice_receipt.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    notice_receipt.add_argument(
        "version_id", metavar="VERSION_ID", help="Version ID (UUID)"
    )
    notice_receipt.add_argument(
        "notice_id", metavar="NOTICE_ID", help="Notice ID (UUID)"
    )
    receipt_outcome = notice_receipt.add_mutually_exclusive_group(required=True)
    receipt_outcome.add_argument(
        "--received-at",
        metavar="ISO8601",
        help="The recipient's receipt, with its time and a timezone",
    )
    receipt_outcome.add_argument(
        "--unknown",
        choices=NOTICE_UNKNOWN_REASONS,
        help="No receipt is known. A provider's acceptance is not a receipt: use provider_accepted_no_receipt",
    )
    notice_receipt.add_argument(
        "--reason", metavar="TEXT", help="A note, up to 500 characters"
    )
    _add_idempotency_option(notice_receipt)
    _add_auth_options(notice_receipt)
    notice_receipt.set_defaults(handler=_notice_receipt)
    notice_follow_up = notice_commands.add_parser(
        "follow-up", help="Mark a follow-up as required, or as done, with the reason"
    )
    notice_follow_up.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    notice_follow_up.add_argument(
        "version_id", metavar="VERSION_ID", help="Version ID (UUID)"
    )
    notice_follow_up.add_argument(
        "notice_id", metavar="NOTICE_ID", help="Notice ID (UUID)"
    )
    notice_follow_up.add_argument(
        "--action",
        required=True,
        choices=NOTICE_FOLLOW_UP_ACTIONS,
        help="require or complete",
    )
    notice_follow_up.add_argument(
        "--reason", required=True, metavar="TEXT", help="Up to 500 characters"
    )
    _add_idempotency_option(notice_follow_up)
    _add_auth_options(notice_follow_up)
    notice_follow_up.set_defaults(handler=_notice_follow_up)
    notice_cancel = notice_commands.add_parser(
        "cancel", help="Cancel a notice with a reason. Not possible after a receipt"
    )
    notice_cancel.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    notice_cancel.add_argument(
        "version_id", metavar="VERSION_ID", help="Version ID (UUID)"
    )
    notice_cancel.add_argument(
        "notice_id", metavar="NOTICE_ID", help="Notice ID (UUID)"
    )
    notice_cancel.add_argument(
        "--reason", required=True, metavar="TEXT", help="Up to 500 characters"
    )
    _add_idempotency_option(notice_cancel)
    _add_auth_options(notice_cancel)
    notice_cancel.set_defaults(handler=_notice_cancel)

    review = case_commands.add_parser(
        "review",
        help="Review rounds of a case: open, list, record layer outcomes, and declare conflicts, recusals and appeals",
    )
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_open = review_commands.add_parser(
        "open",
        help="Open a review round with all eleven layers not set. You must be assigned to the case",
    )
    review_open.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    review_open.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help="Idempotency-Key, 1 to 128 visible ASCII characters. Default: a new key for this run. "
        "Reuse the key of an earlier run to retry it safely",
    )
    _add_auth_options(review_open)
    review_open.set_defaults(handler=_review_open)
    review_list = review_commands.add_parser(
        "list", help="List the review rounds of one of your cases, with revisions"
    )
    review_list.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(review_list)
    review_list.set_defaults(handler=_review_list)
    review_record = review_commands.add_parser(
        "record",
        help="Set the outcome of one layer of a review round. You must be assigned to the case",
    )
    review_record.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    review_record.add_argument(
        "--layer", required=True, choices=REVIEW_LAYERS, help="Layer to set"
    )
    review_record.add_argument(
        "--outcome", required=True, choices=REVIEW_OUTCOMES, help="Layer outcome"
    )
    review_record.add_argument(
        "--reason",
        help="Reason, up to 1000 characters. Required for every outcome except pass",
    )
    review_record.add_argument(
        "--note", help="Same as --reason, kept for existing scripts"
    )
    review_record.add_argument(
        "--evidence",
        action="append",
        default=[],
        metavar="EVIDENCE_ID",
        help="Evidence item ID (UUID) of this case. Repeat for more than one. Required for a pass",
    )
    review_record.add_argument(
        "--round",
        metavar="ROUND_ID",
        help="Review round ID (UUID). Default: the open round of the case",
    )
    review_record.add_argument(
        "--revision",
        type=int,
        required=True,
        help="Revision of the round you read with 'review list'",
    )
    _add_idempotency_option(review_record)
    _add_auth_options(review_record)
    review_record.set_defaults(handler=_review_record)
    review_conflict = review_commands.add_parser(
        "conflict",
        help="Declare, withdraw or correct your own conflict on a review round. A declared conflict stops you "
        "from setting its layers. You must be assigned to the case",
    )
    review_conflict.add_argument(
        "round_id", metavar="ROUND_ID", help="Review round ID (UUID)"
    )
    review_conflict.add_argument(
        "--kind",
        required=True,
        choices=("declared", "withdrawn"),
        help="declared states the conflict; withdrawn states that it no longer applies",
    )
    review_conflict.add_argument(
        "--description",
        help="What the conflict is, up to 1000 characters. Required for declared",
    )
    review_conflict.add_argument(
        "--supersedes",
        metavar="CONFLICT_ID",
        help="The entry this one corrects: your current conflict entry on the round. Leave out when there is none",
    )
    review_conflict.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help="Idempotency-Key, 1 to 128 visible ASCII characters. Default: a new key for this run",
    )
    _add_auth_options(review_conflict)
    review_conflict.set_defaults(handler=_review_conflict)
    review_recuse = review_commands.add_parser(
        "recuse",
        help="Withdraw from a review round and name the replacement reviewer. You must be assigned to the case",
    )
    review_recuse.add_argument(
        "round_id", metavar="ROUND_ID", help="Review round ID (UUID)"
    )
    review_recuse.add_argument(
        "--replacement",
        required=True,
        metavar="USER_ID",
        help="User ID (UUID) of the replacement: an active, assigned user with reviews:write and no declared conflict",
    )
    review_recuse.add_argument(
        "--reason", help="Why you withdraw, up to 1000 characters"
    )
    review_recuse.add_argument(
        "--supersedes",
        metavar="RECUSAL_ID",
        help="The recusal this one corrects: your current recusal on the round. Leave out for the first one",
    )
    review_recuse.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help="Idempotency-Key, 1 to 128 visible ASCII characters. Default: a new key for this run",
    )
    _add_auth_options(review_recuse)
    review_recuse.set_defaults(handler=_review_recuse)
    review_appeal = review_commands.add_parser(
        "appeal",
        help="File an appeal against a review round. The system names the independent reviewer. No approval is needed",
    )
    review_appeal.add_argument(
        "round_id", metavar="ROUND_ID", help="Review round ID (UUID)"
    )
    review_appeal.add_argument(
        "--grounds", required=True, help="Grounds of the appeal, up to 2000 characters"
    )
    review_appeal.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help="Idempotency-Key, 1 to 128 visible ASCII characters. Default: a new key for this run",
    )
    _add_auth_options(review_appeal)
    review_appeal.set_defaults(handler=_review_appeal)

    classification = case_commands.add_parser(
        "classification", help="Show and save the classification of a case"
    )
    classification_commands = classification.add_subparsers(
        dest="classification_command", required=True
    )
    classification_set = classification_commands.add_parser(
        "set",
        help="Save the next classification version. Every field is replaced: a field you leave out is cleared",
    )
    classification_set.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    classification_set.add_argument(
        "--expected-version",
        required=True,
        type=int,
        metavar="N",
        help="The version you read (0 when none is recorded). A save is refused when the current version differs",
    )
    classification_set.add_argument(
        "--trigger",
        action="append",
        choices=CLASSIFICATION_TRIGGERS,
        metavar="TRIGGER",
        help="Trigger value: "
        + ", ".join(CLASSIFICATION_TRIGGERS)
        + ". Repeat for each trigger (up to five). none_applies cannot be combined with another value",
    )
    classification_set.add_argument(
        "--event-kind", choices=EVENT_KINDS, help="Event kind"
    )
    classification_set.add_argument(
        "--classification-rationale", help="Rationale, up to 1000 characters"
    )
    classification_set.add_argument(
        "--responsible-operator", help="Responsible operator, up to 1000 characters"
    )
    classification_set.add_argument(
        "--affected-versions", help="Affected versions, up to 1000 characters"
    )
    classification_set.add_argument(
        "--approved-scope", help="Approved scope, up to 1000 characters"
    )
    classification_set.add_argument(
        "--scope-permission-source",
        help="Source of the scope permission, up to 1000 characters",
    )
    classification_set.add_argument(
        "--containment", help="Containment, up to 1000 characters"
    )
    classification_set.add_argument(
        "--evidence-gap-plan", help="Evidence gap plan, up to 1000 characters"
    )
    classification_set.add_argument(
        "--next-update-due-at",
        metavar="ISO8601",
        help="Next update due, with a timezone, for example 2026-11-02T09:00:00Z",
    )
    _add_idempotency_option(classification_set)
    _add_auth_options(classification_set)
    classification_set.set_defaults(handler=_classification_set)
    classification_show = classification_commands.add_parser(
        "show", help="Show the current classification of one of your cases"
    )
    classification_show.add_argument(
        "case_id", metavar="CASE_ID", help="Case ID (UUID)"
    )
    _add_auth_options(classification_show)
    classification_show.set_defaults(handler=_classification_show)

    impact = case_commands.add_parser(
        "impact", help="List and set the impact areas of a case"
    )
    impact_commands = impact.add_subparsers(dest="impact_command", required=True)
    impact_list = impact_commands.add_parser(
        "list", help="List the impact areas of one of your cases"
    )
    impact_list.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(impact_list)
    impact_list.set_defaults(handler=_impact_list)
    impact_set = impact_commands.add_parser(
        "set", help="Set the severity and scope of one impact area"
    )
    impact_set.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    impact_set.add_argument(
        "area", metavar="AREA", choices=IMPACT_AREAS, help="Impact area"
    )
    impact_set.add_argument(
        "--severity", required=True, choices=IMPACT_SEVERITIES, help="Severity"
    )
    impact_set.add_argument(
        "--scope",
        required=True,
        help="What is affected (required, up to 1000 characters)",
    )
    _add_idempotency_option(impact_set)
    _add_auth_options(impact_set)
    impact_set.set_defaults(handler=_impact_set)

    # The risk commands are named "risk": "impact set" above already sets the severity of one impact area.
    risk = case_commands.add_parser(
        "risk",
        help="Show and set the impact values C, B and A of a case, and its risk score",
    )
    risk_commands = risk.add_subparsers(dest="risk_command", required=True)
    risk_show = risk_commands.add_parser(
        "show", help="Show the impact values and the risk score of one of your cases"
    )
    risk_show.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(risk_show)
    risk_show.set_defaults(handler=_risk_show)
    risk_set = risk_commands.add_parser(
        "set",
        help="Save the impact values as the next version. Every value is replaced: a value you leave out is cleared",
    )
    risk_set.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    risk_set.add_argument(
        "--c", type=_impact_value, metavar="N", help="Confidentiality, 1 to 5"
    )
    risk_set.add_argument(
        "--b", type=_impact_value, metavar="N", help="Integrity, 1 to 5"
    )
    risk_set.add_argument(
        "--a", type=_impact_value, metavar="N", help="Availability, 1 to 5"
    )
    risk_set.add_argument(
        "--expected-version",
        required=True,
        type=int,
        metavar="V",
        help="The version you read (0 when none is recorded). A save is refused when the current version differs",
    )
    risk_set.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help="Idempotency-Key, 1 to 128 visible ASCII characters. Default: a new key for this run. "
        "Reuse the key of an earlier run to retry it safely",
    )
    _add_auth_options(risk_set)
    risk_set.set_defaults(handler=_risk_set)

    legal = case_commands.add_parser(
        "legal-duty",
        help="List, add and update the legal or contractual duties of a case. They are kept apart from deadlines",
    )
    legal_commands = legal.add_subparsers(dest="legal_duty_command", required=True)
    legal_list = legal_commands.add_parser(
        "list", help="List the legal duties of one of your cases"
    )
    legal_list.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(legal_list)
    legal_list.set_defaults(handler=_legal_duty_list)
    legal_add = legal_commands.add_parser(
        "add", help="Add a legal or contractual duty to a case"
    )
    legal_add.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    legal_add.add_argument(
        "--kind",
        required=True,
        choices=LEGAL_DUTY_KINDS,
        help="Statutory or contractual",
    )
    legal_add.add_argument(
        "--description",
        required=True,
        help="What the duty requires (required, up to 1000 characters)",
    )
    legal_add.add_argument(
        "--source-reference",
        required=True,
        help="The statute or contract clause that creates the duty (up to 500)",
    )
    legal_add.add_argument("--jurisdiction", help="Jurisdiction (up to 200 characters)")
    legal_add.add_argument(
        "--due-at",
        required=True,
        metavar="ISO8601",
        help="When the duty falls due, with a timezone",
    )
    _add_idempotency_option(legal_add)
    _add_auth_options(legal_add)
    legal_add.set_defaults(handler=_legal_duty_add)

    legal_update = legal_commands.add_parser(
        "update", help="Change one or more fields of a legal duty"
    )
    legal_update.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    legal_update.add_argument("duty_id", metavar="DUTY_ID", help="Legal duty ID (UUID)")
    legal_update.add_argument(
        "--description", help="What the duty requires (up to 1000 characters)"
    )
    legal_update.add_argument(
        "--source-reference",
        help="The statute or contract clause (up to 500 characters)",
    )
    legal_update.add_argument(
        "--jurisdiction", help="Jurisdiction. An empty string clears it"
    )
    legal_update.add_argument(
        "--due-at", metavar="ISO8601", help="When the duty falls due, with a timezone"
    )
    legal_update.add_argument(
        "--status", choices=LEGAL_DUTY_STATUSES, help="open or done"
    )
    legal_update.add_argument(
        "--revision",
        type=int,
        required=True,
        help="Revision of the legal duty you read with 'legal-duty list'",
    )
    _add_auth_options(legal_update)
    legal_update.set_defaults(handler=_legal_duty_update)

    awareness = case_commands.add_parser(
        "awareness",
        help="List and record the awareness events of a case. Recorded events are never changed",
    )
    awareness_commands = awareness.add_subparsers(
        dest="awareness_command", required=True
    )
    awareness_list = awareness_commands.add_parser(
        "list", help="List the awareness events of one of your cases"
    )
    awareness_list.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(awareness_list)
    awareness_list.set_defaults(handler=_awareness_list)
    awareness_add = awareness_commands.add_parser(
        "add", help="Record an awareness event of a case"
    )
    awareness_add.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    awareness_add.add_argument(
        "--awareness-kind",
        required=True,
        choices=AWARENESS_KINDS,
        help="How the organization became aware",
    )
    awareness_add.add_argument(
        "--event-kind",
        required=True,
        choices=EVENT_KINDS,
        help="Incident, near miss or blocked attempt",
    )
    awareness_add.add_argument(
        "--occurred-at",
        required=True,
        metavar="ISO8601",
        help="When the event occurred, with a timezone",
    )
    awareness_add.add_argument(
        "--basis",
        required=True,
        help="Basis for the event (required, up to 1000 characters)",
    )
    _add_idempotency_option(awareness_add)
    _add_auth_options(awareness_add)
    awareness_add.set_defaults(handler=_awareness_add)


def _add_case_report_commands(case_commands) -> None:
    report = case_commands.add_parser(
        "report", help="Show the preliminary report of a case. Needs cases:read_all"
    )
    report_commands = report.add_subparsers(dest="report_command", required=True)
    report_show = report_commands.add_parser(
        "show", help="Show the preliminary report of one case"
    )
    report_show.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(report_show)
    report_show.set_defaults(handler=_report_show)

    weekly = case_commands.add_parser(
        "weekly",
        help="List and show the weekly machine-readable updates of a case, made while a significant risk is open",
    )
    weekly_commands = weekly.add_subparsers(dest="weekly_command", required=True)
    weekly_list = weekly_commands.add_parser(
        "list", help="List the weekly updates of one of your cases, oldest first"
    )
    weekly_list.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(weekly_list)
    weekly_list.set_defaults(handler=_weekly_list)
    weekly_show = weekly_commands.add_parser(
        "show", help="Show one weekly update of one of your cases"
    )
    weekly_show.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    weekly_show.add_argument(
        "update_id",
        metavar="UPDATE_ID",
        help="Weekly update ID (UUID), as listed by 'case weekly list'",
    )
    _add_auth_options(weekly_show)
    weekly_show.set_defaults(handler=_weekly_show)


# --- catalog, redaction and TLP (plans/03, CLI and UI parity: catalog, redaction and TLP) -------------------------------

# The limits and values are the same as the API (safe_kit.catalog, safe_kit.redaction, safe_kit.tlp). The CLI does not
# import the backend package, so they are written out here.
CATALOG_TITLE_LIMIT = 200
CATALOG_BODY_LIMIT = 4000
CATALOG_REASON_LIMIT = 1000
CATALOG_FIELD_LIMIT = 1000
CATALOG_REFERENCE_LIMIT = 2000
CATALOG_RESULTS = ("pass", "fail", "inconclusive")
CATALOG_CLOSED_STATUSES = ("withdrawn", "superseded")
TLP_LABELS = ("RED", "AMBER", "AMBER+STRICT", "GREEN", "CLEAR")
REDACTION_MAX_RANGES = 50
REDACTION_REASON_LIMIT = 500
# Matches FROM-TO:REASON, for example 3-5:contains an account number, or 7:one line.
REMOVE_PATTERN = re.compile(r"(\d+)(?:-(\d+))?:(.*)")
CATALOG_ENTRY_PATH = "/api/v1/catalog/entries/{entry_id}"
CATALOG_WRITE_MESSAGES = {
    403: "your role does not hold the permission for this catalog action",
    404: "catalog entry not found",
    409: "the catalog could not record this change",
}
CATALOG_READ_MESSAGES = {
    403: "your role does not hold catalog:read",
    404: "catalog entry not found",
}
# The API answers a missing item and an item the TLP rule hides with the same 404. The CLI shows the same text.
EVIDENCE_READ_MESSAGES = {
    403: "your role does not hold the permission to read evidence",
    404: "evidence not found",
}
REDACT_MESSAGES = {
    403: "your role does not hold the permission to redact evidence",
    404: "evidence not found",
    409: "evidence is not available, or the redaction expired before it finished; retry",
    500: "the redacted copy could not be stored; nothing was recorded as available",
}


def _refusal_text(error: RequestFailed) -> RequestFailed:
    """Show the API's own text for a refusal that carries one (for example "evidence not found"), so the CLI and the web
    UI give the same words. A 403 keeps the message chosen for its status."""
    if isinstance(error.detail, str) and error.status != 403:
        return RequestFailed(error.detail, status=error.status, detail=error.detail)
    return error


def _catalog_refusal(error: RequestFailed, *, entry_id: str | None) -> RequestFailed:
    """The message for a refused catalog write. Nothing was saved in any of these cases."""
    detail = error.detail
    if isinstance(detail, dict):
        status = detail.get("status")
        if status == "entry_closed":
            return RequestFailed(
                "this entry is withdrawn or superseded, so it accepts no further changes",
                status=error.status,
                detail=detail,
            )
        if status == "version_conflict":
            current = detail.get("current_version")
            hint = (
                f"run 'catalog show {entry_id}' and run this command again with --expected-version {current}"
                if entry_id is not None and isinstance(current, int)
                else "read the entry again and run this command again"
            )
            return RequestFailed(
                f"the entry changed, so nothing was saved. The current version is {current}; {hint}",
                status=error.status,
                detail=detail,
            )
        if status == "idempotency_key_reused":
            return RequestFailed(
                "this Idempotency-Key was already used for a different request. Run the command again without it",
                status=error.status,
                detail=detail,
            )
    return _refusal_text(error)


def _catalog_text(
    value: str | None, option: str, limit: int, *, required: bool = True
) -> str | None:
    """Text of a catalog field: stripped, not empty, and within the API limit."""
    text = (value or "").strip()
    if not text:
        if required:
            raise UsageError(f"{option} is required and cannot be only spaces")
        return None
    if len(text) > limit:
        raise UsageError(f"{option} must be at most {limit} characters")
    return text


def _expected_version_arg(value: int) -> int:
    if value < 1:
        raise UsageError(
            "--expected-version must be 1 or more. Read it with: catalog show ENTRY_ID"
        )
    return value


def _derived_key(key: str, purpose: str) -> str:
    """A key for one request of a command that makes several. A retry with the same key gets each first answer again."""
    return hashlib.sha256(f"{key}:{purpose}".encode("utf-8")).hexdigest()


def _verified_text(version: dict) -> str:
    if version["verified"]:
        return "yes"
    verification = version.get("verification")
    return f"no (last check: {verification['result']})" if verification else "no"


def _print_catalog_entry(entry: dict) -> None:
    current = entry["current"]
    print(f"Catalog entry {entry['id']}")
    _print_field("Status", entry["status"])
    _print_field("Version", str(entry["version"]))
    _print_field("Title", current["title"])
    _print_field("Verified", _verified_text(current))
    print("  Body:")
    for line in current["body"].splitlines():
        print(f"    {line}")
    # The restricted reference is returned only to catalog:read_restricted. The default output never prints it.
    if current.get("restricted") is not None:
        print("  Restricted reference: held (printed only by --json)")
    versions = entry.get("versions")
    if not versions:
        return
    rows = [("VERSION", "OPERATION", "STATUS", "VERIFIED", "REASON")]
    rows += [
        (
            str(version["version"]),
            version["operation"],
            version["status"],
            _verified_text(version),
            version["reason"] or "-",
        )
        for version in versions
    ]
    print("Version history:")
    _print_table(rows)
    checked = [version for version in versions if version.get("verification")]
    if checked:
        print("Verification history:")
        for version in checked:
            check = version["verification"]
            print(
                f"  version {version['version']}: {check['result']}, run {check['run_id']}, "
                f"{check['method']} (checked {check['verified_at']})"
            )


def _print_recommendation(recommendation: dict) -> None:
    print(f"Recommendation {recommendation['id']}")
    _print_field("Responsible", recommendation["responsible"])
    _print_field("Due (UTC)", recommendation["due_at"])
    _print_field("Failure or system", recommendation["failure_or_system"])
    _print_field("Required defence", recommendation["required_defence_outcome"])
    _print_field("Minimum control", recommendation["minimum_control"])
    _print_field("Control alternative", recommendation["control_alternative"] or "-")
    _print_field("Evidence to keep", recommendation["evidence_to_keep"])
    _print_field("Recorded", recommendation["created_at"])


def _catalog_list(base_url: str, args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= MAX_PAGE_SIZE:
        raise UsageError(f"--limit must be between 1 and {MAX_PAGE_SIZE}")
    query: dict = {"limit": args.limit}
    if args.cursor:
        query["cursor"] = args.cursor

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            "/api/v1/catalog/entries",
            token=token,
            query=query,
            messages=CATALOG_READ_MESSAGES,
        )
        if args.json:
            _print_json(raw)
            return
        page = _json_object(raw)
        if not page["items"]:
            print("No catalog entries yet.")
        else:
            rows = [("ID", "STATUS", "VERSION", "TITLE")]
            rows += [
                (
                    item["id"],
                    item["status"],
                    str(item["version"]),
                    item["current"]["title"],
                )
                for item in page["items"]
            ]
            _print_table(rows)
        if page["next_cursor"]:
            print(
                f"More entries: run the command again with --cursor {page['next_cursor']}"
            )

    _with_session(base_url, args, action)


def _catalog_show(base_url: str, args: argparse.Namespace) -> None:
    entry_id = _uuid_arg(args.entry_id, "ENTRY_ID")

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            CATALOG_ENTRY_PATH.format(entry_id=entry_id),
            token=token,
            messages=CATALOG_READ_MESSAGES,
        )
        if args.json:
            _print_json(raw)
        else:
            _print_catalog_entry(_json_object(raw))

    _with_session(base_url, args, action)


def _catalog_write(
    base_url: str,
    args: argparse.Namespace,
    *,
    path: str,
    payload: dict,
    heading: str,
    entry_id: str | None,
) -> None:
    """One catalog write with an Idempotency-Key. A refused write saves nothing and says why."""
    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        try:
            raw = _call(
                base,
                "POST",
                path,
                token=token,
                payload=payload,
                messages=CATALOG_WRITE_MESSAGES,
                extra_headers={"Idempotency-Key": key},
            )
        except RequestFailed as error:
            raise _catalog_refusal(error, entry_id=entry_id) from None
        if args.json:
            _print_json(raw)
            return
        print(heading)
        _print_catalog_entry(_json_object(raw))

    _with_session(base_url, args, action)


def _catalog_add(base_url: str, args: argparse.Namespace) -> None:
    payload = {
        "title": _catalog_text(args.title, "--title", CATALOG_TITLE_LIMIT),
        "body": _catalog_text(args.body, "--body", CATALOG_BODY_LIMIT),
        "verification_run_id": _uuid_arg(args.verification_run, "--verification-run")
        if args.verification_run
        else None,
        "restricted_reference": _catalog_text(
            args.restricted_reference,
            "--restricted-reference",
            CATALOG_REFERENCE_LIMIT,
            required=False,
        ),
    }
    _catalog_write(
        base_url,
        args,
        path="/api/v1/catalog/entries",
        payload=payload,
        heading="Catalog entry created.",
        entry_id=None,
    )


def _entry_version_payload(args: argparse.Namespace) -> dict:
    """The fields of a new version: the expected version, the content, and the optional run and restricted reference."""
    return {
        "expected_version": _expected_version_arg(args.expected_version),
        "title": _catalog_text(args.title, "--title", CATALOG_TITLE_LIMIT),
        "body": _catalog_text(args.body, "--body", CATALOG_BODY_LIMIT),
        "verification_run_id": _uuid_arg(args.verification_run, "--verification-run")
        if args.verification_run
        else None,
        "restricted_reference": _catalog_text(
            args.restricted_reference,
            "--restricted-reference",
            CATALOG_REFERENCE_LIMIT,
            required=False,
        ),
    }


def _catalog_version(base_url: str, args: argparse.Namespace) -> None:
    entry_id = _uuid_arg(args.entry_id, "ENTRY_ID")
    _catalog_write(
        base_url,
        args,
        path=f"/api/v1/catalog/entries/{entry_id}/versions",
        payload=_entry_version_payload(args),
        heading="Version saved.",
        entry_id=entry_id,
    )


def _catalog_correct(base_url: str, args: argparse.Namespace) -> None:
    entry_id = _uuid_arg(args.entry_id, "ENTRY_ID")
    payload = _entry_version_payload(args)
    payload["reason"] = _catalog_text(args.reason, "--reason", CATALOG_REASON_LIMIT)
    _catalog_write(
        base_url,
        args,
        path=f"/api/v1/catalog/entries/{entry_id}/corrections",
        payload=payload,
        heading="Correction saved.",
        entry_id=entry_id,
    )


def _catalog_reason_change(
    base_url: str, args: argparse.Namespace, suffix: str, heading: str
) -> None:
    entry_id = _uuid_arg(args.entry_id, "ENTRY_ID")
    payload = {
        "expected_version": _expected_version_arg(args.expected_version),
        "reason": _catalog_text(args.reason, "--reason", CATALOG_REASON_LIMIT),
    }
    _catalog_write(
        base_url,
        args,
        path=f"/api/v1/catalog/entries/{entry_id}/{suffix}",
        payload=payload,
        heading=heading,
        entry_id=entry_id,
    )


def _catalog_dispute(base_url: str, args: argparse.Namespace) -> None:
    _catalog_reason_change(
        base_url, args, "disputes", "Dispute recorded. The entry content is unchanged."
    )


def _catalog_withdraw(base_url: str, args: argparse.Namespace) -> None:
    _catalog_reason_change(
        base_url, args, "withdrawals", "Entry withdrawn. Its history is kept."
    )


def _catalog_supersede(base_url: str, args: argparse.Namespace) -> None:
    entry_id = _uuid_arg(args.entry_id, "ENTRY_ID")
    payload = {
        "expected_version": _expected_version_arg(args.expected_version),
        "replacement_entry_id": _uuid_arg(args.replacement, "--replacement"),
        "reason": _catalog_text(
            args.reason, "--reason", CATALOG_REASON_LIMIT, required=False
        ),
    }
    _catalog_write(
        base_url,
        args,
        path=f"/api/v1/catalog/entries/{entry_id}/supersessions",
        payload=payload,
        heading="Entry superseded. Its history is kept.",
        entry_id=entry_id,
    )


def _catalog_verify(base_url: str, args: argparse.Namespace) -> None:
    """Record a verification run of a recommendation and attach it to the entry as a new version with the same content.

    Two API calls: the run (its own record), then the version that names it. A failing or inconclusive run is attached
    too, so the history shows it, but the entry shows as verified only after a pass. Retrying with the same
    --idempotency-key gets the first answers again. A closed entry is refused before anything is recorded.
    """
    entry_id = _uuid_arg(args.entry_id, "ENTRY_ID")
    recommendation_id = _uuid_arg(args.recommendation, "--recommendation")
    expected_version = _expected_version_arg(args.expected_version)
    run_payload = {
        "subject": _catalog_text(args.subject, "--subject", CATALOG_FIELD_LIMIT),
        "scope": _catalog_text(args.scope, "--scope", CATALOG_FIELD_LIMIT),
        "assumptions": _catalog_text(
            args.assumptions, "--assumptions", CATALOG_FIELD_LIMIT
        ),
        "evidence_path": _catalog_text(
            args.evidence_path, "--evidence-path", CATALOG_FIELD_LIMIT
        ),
        "failure_behavior": _catalog_text(
            args.failure_behavior, "--failure-behavior", CATALOG_FIELD_LIMIT
        ),
        "error_class": _catalog_text(
            args.error_class, "--error-class", CATALOG_FIELD_LIMIT
        ),
        "uncertainty": _catalog_text(
            args.uncertainty, "--uncertainty", CATALOG_FIELD_LIMIT
        ),
        "method": _catalog_text(args.method, "--method", CATALOG_FIELD_LIMIT),
        "result": args.result,
    }
    key = args.idempotency_key or str(uuid.uuid4())
    run_key = _derived_key(key, "verification-run")
    version_key = _derived_key(key, "version")

    def action(base: str, token: str) -> None:
        entry = _json_object(
            _call(
                base,
                "GET",
                CATALOG_ENTRY_PATH.format(entry_id=entry_id),
                token=token,
                messages=CATALOG_READ_MESSAGES,
            )
        )
        if entry["status"] in CATALOG_CLOSED_STATUSES:
            raise RequestFailed(
                f"this entry is {entry['status']}, so it cannot take a verification. Nothing was recorded"
            )
        run = _json_object(
            _call(
                base,
                "POST",
                f"/api/v1/catalog/recommendations/{recommendation_id}/verification-runs",
                token=token,
                payload=run_payload,
                messages=CATALOG_WRITE_MESSAGES,
                extra_headers={"Idempotency-Key": run_key},
            )
        )
        current = entry["current"]
        try:
            raw = _call(
                base,
                "POST",
                f"/api/v1/catalog/entries/{entry_id}/versions",
                token=token,
                payload={
                    "expected_version": expected_version,
                    "title": current["title"],
                    "body": current["body"],
                    "verification_run_id": run["id"],
                },
                messages=CATALOG_WRITE_MESSAGES,
                extra_headers={"Idempotency-Key": version_key},
            )
        except RequestFailed as error:
            refusal = _catalog_refusal(error, entry_id=entry_id)
            raise RequestFailed(
                f"verification run {run['id']} was recorded, but the entry was not updated: {refusal}"
            ) from None
        if args.json:
            _print_json(raw)
            return
        changed = _json_object(raw)
        print(f"Verification run {run['id']} recorded: {run['result']}.")
        print(
            f"The entry is now at version {changed['version']}, and this check is attached to it."
        )
        if changed["current"]["verified"]:
            print("The entry is verified.")
        else:
            print("The entry is not verified: only a passing run verifies it.")

    _with_session(base_url, args, action)


def _catalog_recommendation_list(base_url: str, args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= MAX_PAGE_SIZE:
        raise UsageError(f"--limit must be between 1 and {MAX_PAGE_SIZE}")
    query: dict = {"limit": args.limit}
    if args.cursor:
        query["cursor"] = args.cursor

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            "/api/v1/catalog/recommendations",
            token=token,
            query=query,
            messages=CATALOG_READ_MESSAGES,
        )
        if args.json:
            _print_json(raw)
            return
        page = _json_object(raw)
        if not page["items"]:
            print("No recommendations yet.")
        else:
            rows = [("ID", "RESPONSIBLE", "DUE (UTC)", "FAILURE OR SYSTEM")]
            rows += [
                (
                    item["id"],
                    item["responsible"],
                    item["due_at"],
                    item["failure_or_system"],
                )
                for item in page["items"]
            ]
            _print_table(rows)
        if page["next_cursor"]:
            print(
                f"More recommendations: run the command again with --cursor {page['next_cursor']}"
            )

    _with_session(base_url, args, action)


def _catalog_recommendation_add(base_url: str, args: argparse.Namespace) -> None:
    due = _parse_timestamp(args.due_at, "--due-at", not_in_future=False)
    payload = {
        "failure_or_system": _catalog_text(
            args.failure_or_system, "--failure-or-system", CATALOG_FIELD_LIMIT
        ),
        "required_defence_outcome": _catalog_text(
            args.required_defence_outcome,
            "--required-defence-outcome",
            CATALOG_FIELD_LIMIT,
        ),
        "minimum_control": _catalog_text(
            args.minimum_control, "--minimum-control", CATALOG_FIELD_LIMIT
        ),
        "control_alternative": _catalog_text(
            args.control_alternative,
            "--control-alternative",
            CATALOG_FIELD_LIMIT,
            required=False,
        ),
        "responsible": _catalog_text(
            args.responsible, "--responsible", CATALOG_FIELD_LIMIT
        ),
        "due_at": due.isoformat(),
        "evidence_to_keep": _catalog_text(
            args.evidence_to_keep, "--evidence-to-keep", CATALOG_FIELD_LIMIT
        ),
    }
    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        try:
            raw = _call(
                base,
                "POST",
                "/api/v1/catalog/recommendations",
                token=token,
                payload=payload,
                messages=CATALOG_WRITE_MESSAGES,
                extra_headers={"Idempotency-Key": key},
            )
        except RequestFailed as error:
            raise _catalog_refusal(error, entry_id=None) from None
        if args.json:
            _print_json(raw)
        else:
            print(
                "Recommendation saved. A recommendation is a suggestion until a passing verification run is recorded."
            )
            _print_recommendation(_json_object(raw))

    _with_session(base_url, args, action)


def _print_evidence_detail(item: dict) -> None:
    _print_evidence(item)
    label = item.get("tlp_label")
    _print_field("TLP label", label or "none (original, no TLP rule)")


def _print_redaction(derivation: dict) -> None:
    """The derived copy, its manifest (the removed ranges and reasons, never the removed text) and its claim assessments."""
    derived = derivation["derived"]
    print(f"Redacted copy {derived['id']}")
    _print_field("Original evidence ID", derivation["original_evidence_id"])
    _print_field("Redaction ID", derivation["redaction_id"])
    _print_field("TLP label", derived["tlp_label"] or "none")
    manifest = derivation["manifest"]
    _print_field(
        "Lines", f"{manifest['removed_lines']} removed of {manifest['lines_total']}"
    )
    print("  Removed ranges:")
    for entry in manifest["removed"]:
        span = (
            f"{entry['from_line']}"
            if entry["from_line"] == entry["to_line"]
            else f"{entry['from_line']}-{entry['to_line']}"
        )
        print(f"    lines {span} ({entry['lines']}): {entry['reason']}")
    claims = derivation["claim_assessments"]
    if not claims:
        _print_field("Claim assessments", "none (no review layer cites the original)")
        return
    print("  Claim assessments:")
    for claim in claims:
        print(
            f"    {claim['layer']} in round {claim['round_id']}: {claim['outcome']}. {claim['reason']}"
        )


def _parse_removed(text: str) -> dict:
    match = REMOVE_PATTERN.fullmatch(text.strip())
    if match is None:
        raise UsageError(
            "--remove must look like FROM-TO:REASON, for example 3-5:contains an account number"
        )
    from_line = int(match.group(1))
    to_line = int(match.group(2) or match.group(1))
    if from_line < 1 or to_line < from_line:
        raise UsageError("--remove needs line numbers from 1, with FROM not after TO")
    reason = match.group(3).strip()
    if not reason or len(reason) > REDACTION_REASON_LIMIT:
        raise UsageError(
            f"the reason of a removed range must be 1 to {REDACTION_REASON_LIMIT} characters and not only spaces"
        )
    return {"from_line": from_line, "to_line": to_line, "reason": reason}


def _evidence_show(base_url: str, args: argparse.Namespace) -> None:
    evidence_id = _uuid_arg(args.evidence_id, "EVIDENCE_ID")

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            f"/api/v1/evidence/{evidence_id}",
            token=token,
            messages=EVIDENCE_READ_MESSAGES,
        )
        if args.json:
            _print_json(raw)
        else:
            _print_evidence_detail(_json_object(raw))

    _with_session(base_url, args, action)


def _evidence_derivatives(base_url: str, args: argparse.Namespace) -> None:
    evidence_id = _uuid_arg(args.evidence_id, "EVIDENCE_ID")

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            f"/api/v1/evidence/{evidence_id}/derivatives",
            token=token,
            messages=EVIDENCE_READ_MESSAGES,
        )
        if args.json:
            _print_json(raw)
            return
        items = _json_list(raw)
        if not items:
            print("No redacted copies of this evidence are visible to your account.")
            return
        rows = [("ID", "STATUS", "TLP LABEL", "FILE")]
        rows += [
            (
                item["id"],
                item["status"],
                item["tlp_label"] or "-",
                item["original_filename"],
            )
            for item in items
        ]
        _print_table(rows)

    _with_session(base_url, args, action)


def _evidence_redact(base_url: str, args: argparse.Namespace) -> None:
    evidence_id = _uuid_arg(args.evidence_id, "EVIDENCE_ID")
    if not args.removed:
        raise UsageError("at least one --remove FROM-TO:REASON is needed")
    if len(args.removed) > REDACTION_MAX_RANGES:
        raise UsageError(f"at most {REDACTION_MAX_RANGES} removed ranges are allowed")
    payload = {
        "removed": [_parse_removed(text) for text in args.removed],
        "tlp_label": args.tlp,
    }
    key = args.idempotency_key or str(uuid.uuid4())

    def action(base: str, token: str) -> None:
        try:
            raw = _call(
                base,
                "POST",
                f"/api/v1/evidence/{evidence_id}/derivatives",
                token=token,
                payload=payload,
                messages=REDACT_MESSAGES,
                extra_headers={"Idempotency-Key": key},
            )
        except RequestFailed as error:
            raise _refusal_text(error) from None
        if args.json:
            _print_json(raw)
            return
        print("Redacted copy stored. The original is unchanged.")
        _print_redaction(_json_object(raw))

    _with_session(base_url, args, action)


def _evidence_show_redaction(base_url: str, args: argparse.Namespace) -> None:
    evidence_id = _uuid_arg(args.evidence_id, "EVIDENCE_ID")

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            f"/api/v1/evidence/{evidence_id}/redaction",
            token=token,
            messages=EVIDENCE_READ_MESSAGES,
        )
        if args.json:
            _print_json(raw)
        else:
            _print_redaction(_json_object(raw))

    _with_session(base_url, args, action)


def _add_catalog_commands(subcommands) -> None:
    catalog = subcommands.add_parser(
        "catalog", help="List, show, change and verify the catalog of defences"
    )
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)

    catalog_export = catalog_commands.add_parser(
        "export", help="Export the public catalog as versioned JSON. Needs catalog:read"
    )
    catalog_export.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="Write the JSON to FILE instead of printing it",
    )
    _add_auth_options(catalog_export, json_output=False)
    catalog_export.set_defaults(handler=_catalog_export)

    listing = catalog_commands.add_parser(
        "list", help="List the catalog entries, with their status and version"
    )
    listing.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help="Entries per page (1 to 200)",
    )
    listing.add_argument(
        "--cursor", help="Cursor printed by an earlier list, to read the next page"
    )
    _add_auth_options(listing)
    listing.set_defaults(handler=_catalog_list)

    show = catalog_commands.add_parser(
        "show", help="Show a catalog entry, its versions and its verification history"
    )
    show.add_argument(
        "entry_id",
        metavar="ENTRY_ID",
        help="Catalog entry ID (UUID), as listed by 'catalog list'",
    )
    _add_auth_options(show)
    show.set_defaults(handler=_catalog_show)

    def content_options(command) -> None:
        command.add_argument(
            "--title", required=True, help="Title, 1 to 200 characters"
        )
        command.add_argument("--body", required=True, help="Body, 1 to 4000 characters")
        command.add_argument(
            "--verification-run",
            metavar="RUN_ID",
            help="Verification run to attach (UUID)",
        )
        command.add_argument(
            "--restricted-reference",
            metavar="REFERENCE",
            help="Restricted reference, for example a link to a restricted report. Not printed by default",
        )

    added = catalog_commands.add_parser("add", help="Add a catalog entry")
    content_options(added)
    _add_idempotency_option(added)
    _add_auth_options(added)
    added.set_defaults(handler=_catalog_add)

    version = catalog_commands.add_parser(
        "version", help="Save a new version of an entry. The old version is kept"
    )
    version.add_argument("entry_id", metavar="ENTRY_ID", help="Catalog entry ID (UUID)")
    version.add_argument(
        "--expected-version",
        type=int,
        required=True,
        help="The version you read with 'catalog show'",
    )
    content_options(version)
    _add_idempotency_option(version)
    _add_auth_options(version)
    version.set_defaults(handler=_catalog_version)

    correct = catalog_commands.add_parser(
        "correct", help="Correct the content of an entry, with a reason"
    )
    correct.add_argument("entry_id", metavar="ENTRY_ID", help="Catalog entry ID (UUID)")
    correct.add_argument(
        "--expected-version",
        type=int,
        required=True,
        help="The version you read with 'catalog show'",
    )
    correct.add_argument(
        "--reason",
        required=True,
        help="Why the content is corrected, 1 to 1000 characters",
    )
    content_options(correct)
    _add_idempotency_option(correct)
    _add_auth_options(correct)
    correct.set_defaults(handler=_catalog_correct)

    for name, handler, text in (
        (
            "dispute",
            _catalog_dispute,
            "Record a dispute of an entry. The content does not change",
        ),
        (
            "withdraw",
            _catalog_withdraw,
            "Withdraw an entry. Its history is kept and it accepts no further changes",
        ),
    ):
        command = catalog_commands.add_parser(name, help=text)
        command.add_argument(
            "entry_id", metavar="ENTRY_ID", help="Catalog entry ID (UUID)"
        )
        command.add_argument(
            "--expected-version",
            type=int,
            required=True,
            help="The version you read with 'catalog show'",
        )
        command.add_argument(
            "--reason", required=True, help="Reason, 1 to 1000 characters"
        )
        _add_idempotency_option(command)
        _add_auth_options(command)
        command.set_defaults(handler=handler)

    supersede = catalog_commands.add_parser(
        "supersede",
        help="Replace an entry with another open entry. The old entry is kept and closed",
    )
    supersede.add_argument(
        "entry_id", metavar="ENTRY_ID", help="Entry to supersede (UUID)"
    )
    supersede.add_argument(
        "--expected-version",
        type=int,
        required=True,
        help="The version you read with 'catalog show'",
    )
    supersede.add_argument(
        "--replacement",
        metavar="ENTRY_ID",
        required=True,
        help="The open entry that replaces it (UUID)",
    )
    supersede.add_argument("--reason", help="Optional reason, up to 1000 characters")
    _add_idempotency_option(supersede)
    _add_auth_options(supersede)
    supersede.set_defaults(handler=_catalog_supersede)

    verify = catalog_commands.add_parser(
        "verify",
        help="Record a verification run of a recommendation and attach it to an entry. Only a pass verifies the entry",
    )
    verify.add_argument("entry_id", metavar="ENTRY_ID", help="Catalog entry ID (UUID)")
    verify.add_argument(
        "--recommendation",
        metavar="RECOMMENDATION_ID",
        required=True,
        help="Recommendation ID (UUID)",
    )
    verify.add_argument(
        "--expected-version",
        type=int,
        required=True,
        help="The version you read with 'catalog show'",
    )
    verify.add_argument(
        "--result", required=True, choices=CATALOG_RESULTS, help="Result of the run"
    )
    for option, text in (
        ("subject", "What was checked"),
        ("scope", "Scope of the check"),
        ("assumptions", "Assumptions made"),
        ("evidence-path", "Where the evidence of the run is kept"),
        ("failure-behavior", "How the control behaves when it fails"),
        ("error-class", "Error class observed"),
        ("uncertainty", "Remaining uncertainty"),
        ("method", "Method used"),
    ):
        verify.add_argument(
            f"--{option}", required=True, help=f"{text}, 1 to 1000 characters"
        )
    _add_idempotency_option(verify)
    _add_auth_options(verify)
    verify.set_defaults(handler=_catalog_verify)

    recommendation = catalog_commands.add_parser(
        "recommendation", help="List and add recommendations"
    )
    recommendation_commands = recommendation.add_subparsers(
        dest="recommendation_command", required=True
    )
    recommendation_list = recommendation_commands.add_parser(
        "list", help="List the recommendations, oldest first"
    )
    recommendation_list.add_argument(
        "--limit", type=int, default=DEFAULT_PAGE_SIZE, help="Items per page (1 to 200)"
    )
    recommendation_list.add_argument(
        "--cursor", help="Cursor printed by an earlier list, to read the next page"
    )
    _add_auth_options(recommendation_list)
    recommendation_list.set_defaults(handler=_catalog_recommendation_list)

    recommendation_add = recommendation_commands.add_parser(
        "add",
        help="Record a recommendation: a suggested defence. It has no result until a run is recorded",
    )
    for option, text in (
        ("failure-or-system", "The failure or system the recommendation is about"),
        ("required-defence-outcome", "The defence result that is required"),
        ("minimum-control", "The minimum control"),
        ("responsible", "Who is responsible"),
        ("evidence-to-keep", "The evidence to keep"),
    ):
        recommendation_add.add_argument(
            f"--{option}", required=True, help=f"{text}, 1 to 1000 characters"
        )
    recommendation_add.add_argument(
        "--control-alternative", help="An alternative control, up to 1000 characters"
    )
    recommendation_add.add_argument(
        "--due-at", required=True, help="Due time, ISO 8601 with a timezone"
    )
    _add_idempotency_option(recommendation_add)
    _add_auth_options(recommendation_add)
    recommendation_add.set_defaults(handler=_catalog_recommendation_add)


def _add_evidence_commands(evidence_commands) -> None:
    shown = evidence_commands.add_parser(
        "show", help="Show one evidence item, with its TLP label"
    )
    shown.add_argument("evidence_id", metavar="EVIDENCE_ID", help="Evidence ID (UUID)")
    _add_auth_options(shown)
    shown.set_defaults(handler=_evidence_show)

    derivatives = evidence_commands.add_parser(
        "derivatives",
        help="List the redacted copies of an evidence item that your account may see",
    )
    derivatives.add_argument(
        "evidence_id", metavar="EVIDENCE_ID", help="Original evidence ID (UUID)"
    )
    _add_auth_options(derivatives)
    derivatives.set_defaults(handler=_evidence_derivatives)

    redact = evidence_commands.add_parser(
        "redact",
        help="Make a redacted copy of a text evidence item. The original is unchanged; the copy gets its own ID and manifest",
    )
    redact.add_argument(
        "evidence_id",
        metavar="EVIDENCE_ID",
        help="Original evidence ID (UUID), text/plain only",
    )
    redact.add_argument(
        "--remove",
        dest="removed",
        action="append",
        default=[],
        metavar="FROM-TO:REASON",
        help="Lines to remove, 1-based and inclusive, with the reason. Repeat for each range, up to 50",
    )
    redact.add_argument(
        "--tlp",
        required=True,
        choices=TLP_LABELS,
        help="TLP label of the copy. It must be at least as restrictive as the original's label",
    )
    _add_idempotency_option(redact)
    _add_auth_options(redact)
    redact.set_defaults(handler=_evidence_redact)

    show_redaction = evidence_commands.add_parser(
        "show-redaction",
        help="Show the manifest and the claim assessments of a redacted copy",
    )
    show_redaction.add_argument(
        "evidence_id", metavar="EVIDENCE_ID", help="Redacted copy's evidence ID (UUID)"
    )
    _add_auth_options(show_redaction)
    show_redaction.set_defaults(handler=_evidence_show_redaction)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    # The banner goes to stderr, so JSON on stdout stays clean. It is shown only in an interactive terminal.
    if sys.stderr.isatty() and not args.no_banner:
        print(banner(color=os.environ.get("NO_COLOR") is None), file=sys.stderr)
    base_url = os.environ.get(API_URL_ENV, DEFAULT_API_URL)
    try:
        if args.command == "health":
            return health(base_url)
        args.handler(base_url, args)
        return 0
    except UsageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except RequestFailed as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
