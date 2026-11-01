"""Container CLI. Calls the shared API and never accesses the database directly."""

import argparse
import getpass
import json
import mimetypes
import os
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

# Same limits as the API and the web UI (the web UI pages cases 20 at a time).
MAX_TITLE_LENGTH = 200
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200
# Same limit as the API (safe_kit.case_import.MAX_IMPORT_BYTES). The CLI does not import the backend package.
MAX_IMPORT_FILE_BYTES = 1024 * 1024
# Same default as the API (SAFE_EVIDENCE_MAX_BYTES). The server sets the real limit and answers 413 when it is lower.
MAX_EVIDENCE_FILE_BYTES = 50 * 1024 * 1024

# The values the API accepts for the case update commands. The API checks them again.
REVIEW_OUTCOMES = ("pass", "fail", "insufficient_evidence", "not_applicable")
CLASSIFICATION_TRIGGERS = (
    "unauthorized_access_or_change",
    "boundary_escape",
    "confidential_data_exposure",
    "out_of_scope_continuation",
    "harm_inaction_near_miss",
)
EVENT_KINDS = ("incident", "near_miss", "blocked_attempt")
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


class UsageError(Exception):
    """Wrong arguments or input. Exit code 2."""


class RequestFailed(Exception):
    """The API or the login failed. The message is shown to the user. Exit code 1.

    status and detail are set when the API answered with an error. Only the command that asked for a clearer
    message reads them. The detail is the API's detail: a short text, or a structured object with a status string
    (see _error_detail). It is never a request body.
    """

    def __init__(self, message: str, *, status: int | None = None, detail: str | dict | None = None) -> None:
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
            result = {"status": json.load(response)["status"], "http_status": response.status}
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
) -> bytes:
    """One API request. Returns the raw response body. Raises RequestFailed for any non-2xx or transport error.

    payload is sent as JSON. raw_body is sent unchanged with content_type (multipart uploads). messages overrides
    the default text for the status codes it lists.
    """
    url = base_url + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json"}
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
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise RequestFailed(
            _http_message(error.code, messages), status=error.code, detail=_error_detail(error)
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
        raise UsageError("a password is required: enter it at the prompt or pipe it on stdin")
    return password


def _username(args: argparse.Namespace) -> str:
    username = (args.username or os.environ.get("SAFE_USERNAME", "")).strip()
    if not username:
        raise UsageError("a username is required: use --username or set SAFE_USERNAME")
    return username


def _with_session(base_url: str, args: argparse.Namespace, action) -> None:
    """Log in once, run action with the access token, then end the session.

    The tokens are kept in memory for this command only. They are never printed or written to a file.
    """
    username = _username(args)
    password = _read_password()
    tokens = _json_object(
        _call(base_url, "POST", "/api/v1/auth/token", payload={"username": username, "password": password})
    )
    if not isinstance(tokens.get("access_token"), str) or not isinstance(tokens.get("refresh_token"), str):
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
        raise UsageError(f"{label} cannot be in the future. Enter the time it happened, not a later one")
    return value


def _parse_awareness_time(text: str) -> datetime:
    return _parse_timestamp(text, "awareness time", not_in_future=True)


def _print_json(raw: bytes) -> None:
    """Print the API body as it was received. Nothing else goes to stdout."""
    text = raw.decode("utf-8")
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _print_case(case: dict) -> None:
    print(f"Case {case['reference']}")
    print(f"  ID:               {case['id']}")
    print(f"  Title:            {case['title']}")
    print(f"  Status:           {case['status']}")
    print(f"  Awareness (UTC):  {case['awareness_time']}")
    print(f"  Created (UTC):    {case['created_at']}")
    print(f"  Calendar version: {case['calendar_version']}")
    if case["deadlines"]:
        print("  Deadlines:")
        for task in case["deadlines"]:
            print(f"    {task['kind']}: due {task['due_at']} (UTC), {task['status']}")


def _print_case_page(page: dict) -> None:
    items = page["items"]
    if not items:
        print(f"No cases to show. Total: {page['total']}")
        return
    rows = [("REFERENCE", "STATUS", "AWARENESS (UTC)", "TITLE")]
    rows += [(item["reference"], item["status"], item["awareness_time"], item["title"]) for item in items]
    widths = [max(len(row[column]) for row in rows) for column in range(3)]
    for row in rows:
        cells = [row[column].ljust(widths[column]) for column in range(3)] + [row[3]]
        print("  ".join(cells).rstrip())
    first = page["offset"] + 1
    last = page["offset"] + len(items)
    print(f"Showing {first}-{last} of {page['total']}")


def _case_create(base_url: str, args: argparse.Namespace) -> None:
    title = args.title
    # The same checks as the API: 1 to 200 characters, not only whitespace.
    if not 1 <= len(title) <= MAX_TITLE_LENGTH or not title.strip():
        raise UsageError(f"the title must be 1 to {MAX_TITLE_LENGTH} characters and not only spaces")
    awareness = _parse_awareness_time(args.awareness_time)
    payload = {"title": title.strip(), "awareness_time": awareness.isoformat()}

    def action(base: str, token: str) -> None:
        raw = _call(base, "POST", "/api/v1/cases", token=token, payload=payload)
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
        raise UsageError("CASE_ID must be the case ID (a UUID) shown by 'case list'") from None


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


def _case_list(base_url: str, args: argparse.Namespace) -> None:
    _check_paging(args)

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "GET",
            "/api/v1/cases",
            token=token,
            query={"limit": args.limit, "offset": args.offset},
        )
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
        raise UsageError(f"cannot read {path}: {error.strerror or 'unknown error'}") from None
    if len(raw) > limit:
        raise UsageError(too_large)
    return raw


def _read_import_file(path: str) -> bytes:
    return _read_bounded_file(
        path, MAX_IMPORT_FILE_BYTES, f"the import file is larger than {MAX_IMPORT_FILE_BYTES} bytes"
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
            print(f"  {position}: same case ID as {result['reference']} with different content, nothing written")
        elif status == "rejected":
            print(f"  {position}: rejected, nothing written")
        else:
            print(f"  {position}: failed, nothing written")
        for error in result["errors"]:
            print(f"    {error}")


def _case_import(base_url: str, args: argparse.Namespace) -> None:
    raw = _read_import_file(args.file)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise UsageError(f"{args.file} is not valid JSON") from None
    if not isinstance(payload, dict):
        raise UsageError(f"{args.file} must hold a JSON object with import_format_version and records")

    def action(base: str, token: str) -> None:
        raw = _call(base, "POST", "/api/v1/cases/import", token=token, payload=payload)
        summary = _json_object(raw)
        if args.json:
            _print_json(raw)
        else:
            _print_import_summary(summary)
        # Duplicates are expected on a repeated import. Anything that was not stored is a failed command.
        not_stored = summary["rejected"] + summary["conflict"] + summary["failed"]
        if not_stored:
            raise RequestFailed(f"{not_stored} record(s) were not stored; see the results above")

    _with_session(base_url, args, action)


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
        (item["id"], item["status"], item["media_type"], str(item["size_bytes"]), item["original_filename"])
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
    rows = [("ID", "KIND", "DUE (UTC)", "STATUS", "NOTE")]
    rows += [
        (task["id"] or "-", task["kind"], task["due_at"], task["status"], task["not_applicable_reason"] or "")
        for task in tasks
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    print(f"Deadline tasks for case {case['reference']}:")
    for row in rows:
        cells = [row[column].ljust(widths[column]) for column in range(4)] + [row[4]]
        print("  ".join(cells).rstrip())


def _multipart_file(field: str, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
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

    def action(base: str, token: str) -> None:
        raw = _call(
            base,
            "POST",
            f"/api/v1/cases/{case_id}/evidence",
            token=token,
            raw_body=body,
            content_type=content_type,
            messages=EVIDENCE_HTTP_MESSAGES,
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


def _write_case(
    base_url: str,
    args: argparse.Namespace,
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    messages: dict[int, str] | None = None,
    render=None,
) -> None:
    """One write request in its own login session. --json prints the API body; otherwise render prints a summary."""

    def action(base: str, token: str) -> None:
        raw = _call(base, method, path, token=token, payload=payload, messages=messages)
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


def _render_review(raw: bytes) -> None:
    review = _json_object(raw)
    print("Review recorded.")
    print(f"  ID:               {review['id']}")
    print(f"  Outcome:          {review['outcome']}")
    print(f"  Note:             {review['note'] or '-'}")
    print(f"  Recorded (UTC):   {review['created_at']}")


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
    print(f"  Internal option:   {'yes' if classification['internal_incident_option'] else 'no'}")
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
REOPEN_MESSAGES = {404: "case not found", 409: "the case is not closed, so it cannot be reopened"}
ASSIGN_MESSAGES = {
    404: "case not found",
    409: "the case is closed, or the user is already assigned to it",
    422: "the user must be an active investigator or reviewer",
}
UNASSIGN_MESSAGES = {404: "case or assignment not found", 409: "the case is closed, so its assignments cannot change"}
REVIEW_MESSAGES = {
    403: "forbidden: recording a review needs reviews:write, and you must be assigned to the case",
    404: "case not found",
    409: "the case is closed, so no review can be recorded",
}
CLASSIFICATION_MESSAGES = {
    403: "forbidden: saving the classification needs cases:triage and the full case view (cases:read_all)",
    404: "case not found",
    409: "the case is closed, so the classification cannot change",
}
IMPACT_MESSAGES = {404: "case not found", 409: "the case is closed, so its impact assessment cannot change"}
LEGAL_DUTY_MESSAGES = {404: "case not found", 409: "the case is closed, so its legal duties cannot change"}
LEGAL_DUTY_UPDATE_MESSAGES = {
    404: "case or legal duty not found",
    409: "the case is closed, so its legal duties cannot change",
}
AWARENESS_MESSAGES = {404: "case not found", 409: "the case is closed, so no awareness event can be recorded"}


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


def _review_record(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    _write_case(
        base_url,
        args,
        "POST",
        f"/api/v1/cases/{case_id}/reviews",
        payload={"outcome": args.outcome, "note": args.note},
        messages=REVIEW_MESSAGES,
        render=_render_review,
    )


def _classification_conflict_version(error: RequestFailed) -> int | None:
    """The current classification version from a stale-save 409, or None for any other failure."""
    detail = error.detail
    if error.status != 409 or not isinstance(detail, dict) or detail.get("status") != CLASSIFICATION_VERSION_CONFLICT:
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
        next_update = _parse_timestamp(args.next_update_due_at, "--next-update-due-at", not_in_future=False)
    # The save replaces every field. A field left out is sent as empty and cleared, as in the web UI.
    payload = {
        "expected_version": args.expected_version,
        "triggers": args.trigger or [],
        "event_kind": args.event_kind,
        "internal_incident_option": args.internal_incident_option,
        "classification_rationale": args.classification_rationale,
        "responsible_operator": args.responsible_operator,
        "affected_versions": args.affected_versions,
        "approved_scope": args.approved_scope,
        "scope_permission_source": args.scope_permission_source,
        "containment": args.containment,
        "evidence_gap_plan": args.evidence_gap_plan,
        "next_update_due_at": next_update.isoformat() if next_update is not None else None,
    }
    path = f"/api/v1/cases/{case_id}/classification"

    def action(base: str, token: str) -> None:
        try:
            raw = _call(base, "PUT", path, token=token, payload=payload, messages=CLASSIFICATION_MESSAGES)
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
        changes["due_at"] = _parse_timestamp(args.due_at, "--due-at", not_in_future=False).isoformat()
    if args.status is not None:
        changes["status"] = args.status
    if not changes:
        raise UsageError(
            "give at least one field to change: --description, --source-reference, --jurisdiction, --due-at or --status"
        )
    _write_case(
        base_url,
        args,
        "PATCH",
        f"/api/v1/cases/{case_id}/legal-duties/{duty_id}",
        payload=changes,
        messages=LEGAL_DUTY_UPDATE_MESSAGES,
        render=_render_duty("Legal duty updated."),
    )


def _awareness_add(base_url: str, args: argparse.Namespace) -> None:
    case_id = _case_id_arg(args.case_id)
    occurred_at = _parse_timestamp(args.occurred_at, "--occurred-at", not_in_future=True)
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


def _add_auth_options(parser: argparse.ArgumentParser, *, json_output: bool = True) -> None:
    parser.add_argument("--username", help="Account name. Defaults to the SAFE_USERNAME environment variable")
    if json_output:
        parser.add_argument("--json", action="store_true", help="Print the API response body as JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="safe-kit")
    parser.add_argument("--no-banner", action="store_true", help="Do not print the startup banner")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("health", help="Check whether the API is ready")

    case = subcommands.add_parser("case", help="Create, import, list and show your cases")
    case_commands = case.add_subparsers(dest="case_command", required=True)

    create = case_commands.add_parser("create", help="Submit a new case")
    create.add_argument("--title", required=True, help="Case title (1 to 200 characters)")
    create.add_argument(
        "--awareness-time",
        required=True,
        metavar="ISO8601",
        help="When you became aware, with a timezone, for example 2026-10-30T09:00:00Z",
    )
    _add_auth_options(create)
    create.set_defaults(handler=_case_create)

    listing = case_commands.add_parser("list", help="List your cases, newest first")
    listing.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE, help="Cases per page (1 to 200)")
    listing.add_argument("--offset", type=int, default=0, help="Number of cases to skip")
    _add_auth_options(listing)
    listing.set_defaults(handler=_case_list)

    show = case_commands.add_parser("show", help="Show one of your cases")
    show.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(show)
    show.set_defaults(handler=_case_show)

    imported = case_commands.add_parser("import", help="Import case records from a JSON file in the project format")
    imported.add_argument("file", metavar="FILE", help='JSON file: {"import_format_version": 1, "records": [...]}')
    _add_auth_options(imported)
    imported.set_defaults(handler=_case_import)
    _add_case_update_commands(case_commands)

    evidence = subcommands.add_parser("evidence", help="Add and list the evidence of a case")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)

    added = evidence_commands.add_parser("add", help="Upload a file as evidence for a case")
    added.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    added.add_argument(
        "file",
        metavar="FILE",
        help="File to upload: PDF, PNG, JPEG, plain text or JSON, up to 50 MiB by default",
    )
    _add_auth_options(added)
    added.set_defaults(handler=_evidence_add)

    evidence_list = evidence_commands.add_parser("list", help="List the evidence of one of your cases")
    evidence_list.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    evidence_list.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE, help="Items per page (1 to 200)")
    evidence_list.add_argument("--offset", type=int, default=0, help="Number of items to skip")
    _add_auth_options(evidence_list)
    evidence_list.set_defaults(handler=_evidence_list)

    task = subcommands.add_parser("task", help="Show the deadline tasks of a case")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    task_listing = task_commands.add_parser("list", help="List the deadline tasks of one of your cases")
    task_listing.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    _add_auth_options(task_listing)
    task_listing.set_defaults(handler=_task_list)
    return parser


def _add_case_update_commands(case_commands) -> None:
    """The commands that change a case. Each one is a thin wrapper over one API route."""
    close = case_commands.add_parser("close", help="Close a case. It needs a review and every deadline task settled")
    close.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    close.add_argument("--reason", required=True, help="Why the case is closed (required)")
    _add_auth_options(close)
    close.set_defaults(handler=_case_close)

    reopen = case_commands.add_parser("reopen", help="Reopen a closed case")
    reopen.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    reopen.add_argument("--reason", required=True, help="Why the case is reopened (required)")
    _add_auth_options(reopen)
    reopen.set_defaults(handler=_case_reopen)

    assign = case_commands.add_parser("assign", help="Assign an active investigator or reviewer to a case")
    assign.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    assign.add_argument("--user-id", required=True, metavar="USER_ID", help="ID (UUID) of the user to assign")
    _add_auth_options(assign)
    assign.set_defaults(handler=_case_assign)

    unassign = case_commands.add_parser("unassign", help="Remove a user from a case")
    unassign.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    unassign.add_argument("--user-id", required=True, metavar="USER_ID", help="ID (UUID) of the user to remove")
    _add_auth_options(unassign, json_output=False)
    unassign.set_defaults(handler=_case_unassign)

    review = case_commands.add_parser("review", help="Record a review of a case")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_record = review_commands.add_parser("record", help="Record a review outcome. You must be assigned to the case")
    review_record.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    review_record.add_argument("--outcome", required=True, choices=REVIEW_OUTCOMES, help="Review outcome")
    review_record.add_argument("--note", help="Optional note, up to 1000 characters")
    _add_auth_options(review_record)
    review_record.set_defaults(handler=_review_record)

    classification = case_commands.add_parser("classification", help="Save the classification of a case")
    classification_commands = classification.add_subparsers(dest="classification_command", required=True)
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
        help="Trigger value: " + ", ".join(CLASSIFICATION_TRIGGERS) + ". Repeat for each trigger (up to five)",
    )
    classification_set.add_argument("--event-kind", choices=EVENT_KINDS, help="Event kind")
    classification_set.add_argument(
        "--internal-incident-option",
        action="store_true",
        help="Use the internal incident option. Needs --classification-rationale",
    )
    classification_set.add_argument("--classification-rationale", help="Rationale, up to 1000 characters")
    classification_set.add_argument("--responsible-operator", help="Responsible operator, up to 1000 characters")
    classification_set.add_argument("--affected-versions", help="Affected versions, up to 1000 characters")
    classification_set.add_argument("--approved-scope", help="Approved scope, up to 1000 characters")
    classification_set.add_argument(
        "--scope-permission-source", help="Source of the scope permission, up to 1000 characters"
    )
    classification_set.add_argument("--containment", help="Containment, up to 1000 characters")
    classification_set.add_argument("--evidence-gap-plan", help="Evidence gap plan, up to 1000 characters")
    classification_set.add_argument(
        "--next-update-due-at", metavar="ISO8601", help="Next update due, with a timezone, for example 2026-11-02T09:00:00Z"
    )
    _add_auth_options(classification_set)
    classification_set.set_defaults(handler=_classification_set)

    impact = case_commands.add_parser("impact", help="Set one impact area of a case")
    impact_commands = impact.add_subparsers(dest="impact_command", required=True)
    impact_set = impact_commands.add_parser("set", help="Set the severity and scope of one impact area")
    impact_set.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    impact_set.add_argument("area", metavar="AREA", choices=IMPACT_AREAS, help="Impact area")
    impact_set.add_argument("--severity", required=True, choices=IMPACT_SEVERITIES, help="Severity")
    impact_set.add_argument("--scope", required=True, help="What is affected (required, up to 1000 characters)")
    _add_auth_options(impact_set)
    impact_set.set_defaults(handler=_impact_set)

    legal = case_commands.add_parser(
        "legal-duty", help="Add and update the legal or contractual duties of a case. They are kept apart from deadlines"
    )
    legal_commands = legal.add_subparsers(dest="legal_duty_command", required=True)
    legal_add = legal_commands.add_parser("add", help="Add a legal or contractual duty to a case")
    legal_add.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    legal_add.add_argument("--kind", required=True, choices=LEGAL_DUTY_KINDS, help="Statutory or contractual")
    legal_add.add_argument("--description", required=True, help="What the duty requires (required, up to 1000 characters)")
    legal_add.add_argument(
        "--source-reference", required=True, help="The statute or contract clause that creates the duty (up to 500)"
    )
    legal_add.add_argument("--jurisdiction", help="Jurisdiction (up to 200 characters)")
    legal_add.add_argument(
        "--due-at", required=True, metavar="ISO8601", help="When the duty falls due, with a timezone"
    )
    _add_auth_options(legal_add)
    legal_add.set_defaults(handler=_legal_duty_add)

    legal_update = legal_commands.add_parser("update", help="Change one or more fields of a legal duty")
    legal_update.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    legal_update.add_argument("duty_id", metavar="DUTY_ID", help="Legal duty ID (UUID)")
    legal_update.add_argument("--description", help="What the duty requires (up to 1000 characters)")
    legal_update.add_argument("--source-reference", help="The statute or contract clause (up to 500 characters)")
    legal_update.add_argument("--jurisdiction", help="Jurisdiction. An empty string clears it")
    legal_update.add_argument("--due-at", metavar="ISO8601", help="When the duty falls due, with a timezone")
    legal_update.add_argument("--status", choices=LEGAL_DUTY_STATUSES, help="open or done")
    _add_auth_options(legal_update)
    legal_update.set_defaults(handler=_legal_duty_update)

    awareness = case_commands.add_parser("awareness", help="Record an awareness event of a case. Append only")
    awareness_commands = awareness.add_subparsers(dest="awareness_command", required=True)
    awareness_add = awareness_commands.add_parser("add", help="Record an awareness event of a case")
    awareness_add.add_argument("case_id", metavar="CASE_ID", help="Case ID (UUID)")
    awareness_add.add_argument(
        "--awareness-kind", required=True, choices=AWARENESS_KINDS, help="How the organization became aware"
    )
    awareness_add.add_argument(
        "--event-kind", required=True, choices=EVENT_KINDS, help="Incident, near miss or blocked attempt"
    )
    awareness_add.add_argument(
        "--occurred-at", required=True, metavar="ISO8601", help="When the event occurred, with a timezone"
    )
    awareness_add.add_argument("--basis", required=True, help="Basis for the event (required, up to 1000 characters)")
    _add_auth_options(awareness_add)
    awareness_add.set_defaults(handler=_awareness_add)


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
