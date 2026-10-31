"""JSON command interface. No environment files or external service discovery."""

import argparse
from dataclasses import asdict
import json
import sys

import psycopg

from ledger.checker import check_invariants
from ledger.core import Ledger, LedgerError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Integer-cent double-entry ledger")
    parser.add_argument("--dsn", required=True, help="explicit Postgres connection string")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the initial schema")
    account = commands.add_parser("account", help="create a zero-balance account")
    account.add_argument("account_id")
    account.add_argument("--allow-negative", action="store_true")
    transfer = commands.add_parser("transfer", help="post an atomic transfer")
    transfer.add_argument("source_id")
    transfer.add_argument("destination_id")
    transfer.add_argument("amount_cents", type=int)
    transfer.add_argument("--key", required=True)
    commands.add_parser("check", help="audit a consistent, read-only snapshot")
    args = parser.parse_args(argv)

    try:
        ledger = Ledger(args.dsn)
        if args.command == "check":
            report = check_invariants(args.dsn)
            print(json.dumps(report.to_dict(), sort_keys=True))
            return 0 if report.ok else 1
        if args.command == "init":
            ledger.initialize()
            result = {"initialized": True}
        elif args.command == "account":
            result = asdict(ledger.create_account(args.account_id, allow_negative=args.allow_negative))
        else:
            result = asdict(ledger.transfer(
                args.source_id, args.destination_id, args.amount_cents, idempotency_key=args.key
            ))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (LedgerError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    except psycopg.Error:
        # Driver messages can include connection details; never echo them.
        print(json.dumps({"error": "DatabaseError", "message": "database operation failed"}),
              file=sys.stderr)
        return 2
