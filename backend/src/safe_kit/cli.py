"""Container CLI. Calls the shared API and never accesses the database directly."""

import argparse
import getpass
import json
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


class UsageError(Exception):
    """Wrong arguments or input. Exit code 2."""


class RequestFailed(Exception):
    """The API or the login failed. The message is shown to the user. Exit code 1."""


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


def _http_message(status: int) -> str:
    return HTTP_MESSAGES.get(status, f"request failed (HTTP {status})")


def _call(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict | None = None,
    query: dict | None = None,
) -> bytes:
    """One API request. Returns the raw response body. Raises RequestFailed for any non-2xx or transport error."""
    url = base_url + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise RequestFailed(_http_message(error.code)) from None
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


def _parse_awareness_time(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise UsageError(
            "awareness time must be an ISO 8601 timestamp with a timezone, for example 2026-10-30T09:00:00Z"
        ) from None
    if value.utcoffset() is None:
        raise UsageError(
            "awareness time must include a timezone, for example 2026-10-30T09:00:00Z or 2026-10-30T12:00:00+03:00"
        )
    # The same rule as the API and the web UI: the awareness time cannot be later than now.
    if value > datetime.now(UTC):
        raise UsageError(
            "the awareness time cannot be in the future. Choose the time you became aware, not a later one"
        )
    return value


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


def _case_list(base_url: str, args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= MAX_PAGE_SIZE:
        raise UsageError(f"--limit must be between 1 and {MAX_PAGE_SIZE}")
    if args.offset < 0:
        raise UsageError("--offset must be 0 or more")

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
    try:
        case_id = str(uuid.UUID(args.case_id))
    except ValueError:
        raise UsageError("CASE_ID must be the case ID (a UUID) shown by 'case list'") from None

    def action(base: str, token: str) -> None:
        raw = _call(base, "GET", f"/api/v1/cases/{case_id}", token=token)
        if args.json:
            _print_json(raw)
        else:
            _print_case(_json_object(raw))

    _with_session(base_url, args, action)


def _read_import_file(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_IMPORT_FILE_BYTES + 1)
    except OSError as error:
        raise UsageError(f"cannot read {path}: {error.strerror or 'unknown error'}") from None
    if len(raw) > MAX_IMPORT_FILE_BYTES:
        raise UsageError(f"the import file is larger than {MAX_IMPORT_FILE_BYTES} bytes")
    return raw


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


def _add_auth_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--username", help="Account name. Defaults to the SAFE_USERNAME environment variable")
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
    return parser


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
