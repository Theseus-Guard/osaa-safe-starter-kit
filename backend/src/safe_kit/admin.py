"""Administrative commands. They write to the database directly, so run them only inside the API container.

create-user is bootstrap-only: it creates the first admin while no admin exists, and refuses afterwards.
All later accounts are created through the API by an admin.
"""

import argparse
import getpass
import sys

import psycopg

from safe_kit.security import MIN_PASSWORD_LENGTH
from safe_kit.users import (
    ROLES,
    AdminAlreadyExists,
    InvalidUsername,
    admin_exists,
    bootstrap_admin,
    find_user,
    reset_password,
    validate_username,
)


def _read_password() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Password: ")
    return sys.stdin.readline().rstrip("\n")


def _read_valid_password() -> str | None:
    password = _read_password()
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"password must be at least {MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
        return None
    return password


def _bootstrap(username: str, role: str) -> int:
    if role != "admin":
        print("before the first admin exists, only --role admin can be created", file=sys.stderr)
        return 2
    if admin_exists():
        print("an admin already exists; create further users through the API", file=sys.stderr)
        return 1
    try:
        validate_username(username)
    except InvalidUsername as error:
        print(str(error), file=sys.stderr)
        return 2
    password = _read_valid_password()
    if password is None:
        return 2
    try:
        user_id = bootstrap_admin(username, password)
    except AdminAlreadyExists:
        print("an admin already exists; create further users through the API", file=sys.stderr)
        return 1
    except psycopg.errors.UniqueViolation:
        print(f"username already exists: {username}", file=sys.stderr)
        return 1
    print(f"created first admin {username} id={user_id}; password change required at first login")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="safe-kit-admin")
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser(
        "create-user",
        help="Bootstrap only: create the first admin. Refused once an admin exists.",
    )
    create.add_argument("--username", required=True)
    create.add_argument("--role", required=True, choices=sorted(ROLES))
    reset = subcommands.add_parser(
        "reset-password",
        help="Reset a user's password and end all of their sessions. The password is read from stdin or prompted.",
    )
    reset.add_argument("--username", required=True)
    args = parser.parse_args(argv)

    if args.command == "create-user":
        return _bootstrap(args.username, args.role)

    password = _read_valid_password()
    if password is None:
        return 2
    user = find_user(args.username)
    if user is None:
        print(f"user not found: {args.username}", file=sys.stderr)
        return 1
    reset_password(user.id, password, source="cli")
    print(f"password reset for {args.username}; all sessions ended; password change required at next login")
    return 0


if __name__ == "__main__":
    sys.exit(main())
