"""Administrative commands. They write to the database directly, so run them only inside the API container.

create-user is bootstrap-only: it creates the first admin while no admin exists, and refuses afterwards.
All later accounts are created through the API by an admin.
owner grant and owner revoke are the only way to give or take the institution_owner role. Each one needs a reason and
writes an audit row.
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
    OwnerOutcome,
    ReporterAccountResetRefused,
    change_institution_owner,
    find_user,
    normalize_email,
    reset_password,
    validate_username,
)

# The reason of an owner change is stored in the audit row. It is kept short, as the other free text of the application.
OWNER_REASON_LIMIT = 500

OWNER_REFUSED = {
    OwnerOutcome.PROTECTED: "refused: the first administrator's role cannot change",
    OwnerOutcome.INACTIVE: "refused: the account is disabled. Enable it through the API first",
    OwnerOutcome.ALREADY_OWNER: "refused: the account already holds the institution_owner role",
    OwnerOutcome.NOT_OWNER: "refused: the account does not hold the institution_owner role, so there is nothing to revoke",
    OwnerOutcome.INVALID_ROLE: "refused: the role must be a role other than institution_owner",
}


def _read_password() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Password: ")
    return sys.stdin.readline().rstrip("\n")


def _read_valid_password() -> str | None:
    password = _read_password()
    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters",
            file=sys.stderr,
        )
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
        print(
            "before the first admin exists, only --role admin can be created",
            file=sys.stderr,
        )
        return 2
    if admin_exists():
        print(
            "an admin already exists; create further users through the API",
            file=sys.stderr,
        )
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
        print(
            "an admin already exists; create further users through the API",
            file=sys.stderr,
        )
        return 1
    except EmailAlreadyInUse:
        print("email already in use", file=sys.stderr)
        return 1
    except psycopg.errors.UniqueViolation:
        print(f"username already exists: {username}", file=sys.stderr)
        return 1
    print(
        f"created first admin {username} id={user_id}; password change required at first login"
    )
    return 0


def _owner(args) -> int:
    reason = args.reason.strip()
    if not reason or len(reason) > OWNER_REASON_LIMIT:
        print(
            f"the reason is required and at most {OWNER_REASON_LIMIT} characters",
            file=sys.stderr,
        )
        return 2
    user = find_user(args.username)
    if user is None:
        print(f"user not found: {args.username}", file=sys.stderr)
        return 1
    grant = args.owner_command == "grant"
    outcome = change_institution_owner(
        user.id, grant=grant, role=getattr(args, "role", None), reason=reason
    )
    if outcome is not OwnerOutcome.OK:
        print(f"{OWNER_REFUSED[outcome]}. Nothing was changed.", file=sys.stderr)
        return 1
    if grant:
        print(
            f"granted institution_owner to {args.username}; all sessions ended; the change is audited"
        )
    else:
        print(
            f"revoked institution_owner from {args.username}; the role is now {args.role}; "
            "all sessions ended; the change is audited"
        )
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
    owner = subcommands.add_parser(
        "owner",
        help="Grant or revoke the institution_owner role. Each change needs a reason and is audited.",
    )
    owner_commands = owner.add_subparsers(dest="owner_command", required=True)
    grant = owner_commands.add_parser(
        "grant", help="Give an active account the institution_owner role."
    )
    grant.add_argument("--username", required=True)
    grant.add_argument(
        "--reason",
        required=True,
        help="Why the role is granted. At most 500 characters.",
    )
    revoke = owner_commands.add_parser(
        "revoke",
        help="Take the institution_owner role away and give the account another role.",
    )
    revoke.add_argument("--username", required=True)
    revoke.add_argument(
        "--role", required=True, choices=sorted(ROLES - {"institution_owner"})
    )
    revoke.add_argument(
        "--reason",
        required=True,
        help="Why the role is revoked. At most 500 characters.",
    )
    args = parser.parse_args(argv)

    if args.command == "create-user":
        return _bootstrap(args.username, args.role, args.email)
    if args.command == "owner":
        return _owner(args)

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
            "password cannot be reused: it matches one of this account's last "
            f"{PASSWORD_HISTORY_DEPTH} passwords or a temporary password issued to it",
            file=sys.stderr,
        )
        return 1
    except ReporterAccountResetRefused:
        print(
            f"refused: {args.username} is a reporter account. An administrator cannot reset the password of a "
            "reporter account (reporter_account_reset_refused). The password was not changed.",
            file=sys.stderr,
        )
        return 1
    print(
        f"password reset for {args.username}; all sessions ended; password change required at next login"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
