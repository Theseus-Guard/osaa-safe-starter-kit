"""Administrative commands. They write to the database directly, so run them only inside the API container.

create-user is bootstrap-only: it creates the first admin while no admin exists, and refuses afterwards.
All later accounts are created through the API by an admin.
"""

import argparse
import getpass
import sys

import psycopg

from safe_kit.security import MIN_PASSWORD_LENGTH, PASSWORD_HISTORY_DEPTH
from safe_kit.users import (
    ROLES,
    AdminAlreadyExists,
    EmailAlreadyInUse,
    InvalidEmail,
    InvalidUsername,
    admin_exists,
    bootstrap_admin,
    PasswordReused,
    find_user,
    normalize_email,
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


def _read_email() -> str:
    # A terminal is prompted. Otherwise the email is the second line of stdin, after the password.
    if sys.stdin.isatty():
        return input("Email: ")
    return sys.stdin.readline().rstrip("\n")


def _checked_email(value: str) -> str | None:
    try:
        return normalize_email(value)
    except InvalidEmail as error:
        print(str(error), file=sys.stderr)
        return None


def _bootstrap(username: str, role: str, email_option: str | None) -> int:
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
    # An email given as an option is checked before anything is read or written.
    email = None
    if email_option is not None:
        email = _checked_email(email_option)
        if email is None:
            return 2
    password = _read_valid_password()
    if password is None:
        return 2
    if email is None:
        email = _checked_email(_read_email())
        if email is None:
            return 2
    try:
        user_id = bootstrap_admin(username, password, email)
    except AdminAlreadyExists:
        print("an admin already exists; create further users through the API", file=sys.stderr)
        return 1
    except EmailAlreadyInUse:
        print("email already in use", file=sys.stderr)
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
    create.add_argument(
        "--email",
        default=None,
        help="Email of the first admin. Prompted (or read from the second line of stdin) when omitted.",
    )
    reset = subcommands.add_parser(
        "reset-password",
        help="Reset a user's password and end all of their sessions. The password is read from stdin or prompted.",
    )
    reset.add_argument("--username", required=True)
    args = parser.parse_args(argv)

    if args.command == "create-user":
        return _bootstrap(args.username, args.role, args.email)

    password = _read_valid_password()
    if password is None:
        return 2
    user = find_user(args.username)
    if user is None:
        print(f"user not found: {args.username}", file=sys.stderr)
        return 1
    try:
        reset_password(user.id, password, source="cli")
    except PasswordReused:
        print(
            f"password was used recently: it matches one of this account's last {PASSWORD_HISTORY_DEPTH} passwords",
            file=sys.stderr,
        )
        return 1
    print(f"password reset for {args.username}; all sessions ended; password change required at next login")
    return 0


if __name__ == "__main__":
    sys.exit(main())
