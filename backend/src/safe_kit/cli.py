"""Container CLI. Calls the shared API and never accesses the database directly."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


TITLE = "OSAA | SAFE Starter-Kit"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="safe-kit")
    parser.add_argument("--no-banner", action="store_true", help="Do not print the startup banner")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("health", help="Check whether the API is ready")
    args = parser.parse_args(argv)
    # The banner goes to stderr, so JSON on stdout stays clean. It is shown only in an interactive terminal.
    if sys.stderr.isatty() and not args.no_banner:
        print(banner(color=os.environ.get("NO_COLOR") is None), file=sys.stderr)
    if args.command == "health":
        return health(os.environ.get("SAFE_API_URL", "http://api:8000"))
    return 2


if __name__ == "__main__":
    sys.exit(main())
