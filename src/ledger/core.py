"""Minimal Postgres ledger. Each operation owns one connection/transaction."""

from importlib.resources import files
from uuid import uuid4

import psycopg

from ledger.models import Account, Transfer

MAX_CENTS = 2**63 - 1
MIN_CENTS = -(2**63)


class LedgerError(Exception):
    """A rejected ledger operation (no changes committed)."""


class AccountNotFound(LedgerError):
    pass


class AccountConflict(LedgerError):
    pass


class IdempotencyConflict(LedgerError):
    pass


class InsufficientFunds(LedgerError):
    pass


class BalanceOverflow(LedgerError):
    pass


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


class Ledger:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def initialize(self) -> None:
        """Create the initial schema, safely repeatable on the same schema version."""
        schema = files("ledger").joinpath("schema.sql").read_text()
        with psycopg.connect(self.dsn) as conn:
            conn.execute(schema)

    def create_account(self, account_id: str, *, allow_negative: bool = False) -> Account:
        _nonempty(account_id, "account_id")
        if type(allow_negative) is not bool:
            raise ValueError("allow_negative must be a boolean")
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                "INSERT INTO accounts (id, allow_negative) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (account_id, allow_negative),
            )
            row = conn.execute(
                "SELECT id, allow_negative, balance_cents FROM accounts WHERE id = %s",
                (account_id,),
            ).fetchone()
            account = Account(*row)
            if account.allow_negative != allow_negative:
                raise AccountConflict("account already exists with different overdraft policy")
            return account

    def get_account(self, account_id: str) -> Account:
        _nonempty(account_id, "account_id")
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT id, allow_negative, balance_cents FROM accounts WHERE id = %s",
                (account_id,),
            ).fetchone()
            if row is None:
                raise AccountNotFound("account does not exist")
            return Account(*row)

    def transfer(
        self, source_id: str, destination_id: str, amount_cents: int, *, idempotency_key: str
    ) -> Transfer:
        for name, value in (
            ("source_id", source_id),
            ("destination_id", destination_id),
            ("idempotency_key", idempotency_key),
        ):
            _nonempty(value, name)
        if source_id == destination_id:
            raise ValueError("source and destination must differ")
        if type(amount_cents) is not int or not 0 < amount_cents <= MAX_CENTS:
            raise ValueError("amount_cents must be a positive signed 64-bit integer")

        try:
            with psycopg.connect(self.dsn) as conn:
                # A conflicting insert waits for the other writer's commit/rollback.
                # The unique key reserves the complete request in this transaction.
                row = conn.execute(
                    "INSERT INTO transfers "
                    "(id, idempotency_key, source_id, destination_id, amount_cents) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (idempotency_key) DO NOTHING "
                    "RETURNING id::text, idempotency_key, source_id, destination_id, amount_cents",
                    (uuid4(), idempotency_key, source_id, destination_id, amount_cents),
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        "SELECT id::text, idempotency_key, source_id, destination_id, amount_cents "
                        "FROM transfers WHERE idempotency_key = %s",
                        (idempotency_key,),
                    ).fetchone()
                    existing = Transfer(*row)
                    if (existing.source_id, existing.destination_id, existing.amount_cents) != (
                        source_id, destination_id, amount_cents
                    ):
                        raise IdempotencyConflict("idempotency key belongs to a different request")
                    return existing

                # Stable lock order prevents opposing transfers from deadlocking.
                # NO KEY UPDATE is compatible with the FK locks from the insert.
                accounts = conn.execute(
                    "SELECT id, allow_negative, balance_cents FROM accounts "
                    "WHERE id IN (%s, %s) ORDER BY id FOR NO KEY UPDATE",
                    (source_id, destination_id),
                ).fetchall()
                balances = {a[0]: a[2] for a in accounts}
                policies = {a[0]: a[1] for a in accounts}
                debit = balances[source_id] - amount_cents
                credit = balances[destination_id] + amount_cents
                if not policies[source_id] and debit < 0:
                    raise InsufficientFunds("source account has insufficient funds")
                if debit < MIN_CENTS or credit > MAX_CENTS:
                    raise BalanceOverflow("resulting balance exceeds signed 64-bit cents")

                for account_id, balance in ((source_id, debit), (destination_id, credit)):
                    conn.execute(
                        "UPDATE accounts SET balance_cents = %s WHERE id = %s",
                        (balance, account_id),
                    )
                transfer = Transfer(*row)
                with conn.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO postings (transfer_id, account_id, amount_cents) "
                        "VALUES (%s, %s, %s)",
                        [(transfer.id, source_id, -amount_cents),
                         (transfer.id, destination_id, amount_cents)],
                    )
                return transfer
        except psycopg.errors.ForeignKeyViolation as exc:
            raise AccountNotFound("account does not exist") from exc
