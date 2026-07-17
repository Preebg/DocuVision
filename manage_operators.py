"""Offline administration CLI for DocuVision operator accounts."""

from __future__ import annotations

import argparse
import getpass
import sys

from docuvision.api.operator_auth import (
    change_operator_password,
    provision_operator,
    set_operator_disabled,
)


def _prompt_password() -> str:
    password = getpass.getpass("New operator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if not password or password != confirmation:
        raise ValueError("Passwords do not match.")
    return password


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Administer on-prem DocuVision operator accounts."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Create an operator.")
    create.add_argument("username")
    create.add_argument("--display-name")
    create.add_argument(
        "--role",
        choices=("operator", "supervisor", "admin"),
        default="operator",
    )

    disable = commands.add_parser(
        "disable",
        help="Disable an operator and revoke all sessions.",
    )
    disable.add_argument("username")

    enable = commands.add_parser("enable", help="Re-enable an operator.")
    enable.add_argument("username")

    reset = commands.add_parser(
        "reset-password",
        help="Replace a password and revoke all sessions.",
    )
    reset.add_argument("username")

    args = parser.parse_args()
    try:
        if args.command == "create":
            operator = provision_operator(
                args.username,
                _prompt_password(),
                display_name=args.display_name,
                role=args.role,
            )
            print(
                f"Created {operator.username} ({operator.id}) "
                f"with role {operator.role}."
            )
        elif args.command == "disable":
            set_operator_disabled(args.username, disabled=True)
            print(f"Disabled {args.username} and revoked all sessions.")
        elif args.command == "enable":
            set_operator_disabled(args.username, disabled=False)
            print(f"Enabled {args.username}.")
        else:
            change_operator_password(args.username, _prompt_password())
            print(f"Updated {args.username} and revoked all sessions.")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
