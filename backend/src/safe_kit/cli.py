"""Container CLI. Calls the shared API and never accesses the database directly."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


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
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("health", help="Check whether the API is ready")
    args = parser.parse_args(argv)
    if args.command == "health":
        return health(os.environ.get("SAFE_API_URL", "http://api:8000"))
    return 2


if __name__ == "__main__":
    sys.exit(main())
