"""Read-only audits. Deliberately do not trust writer validations or constraints."""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Sequence

import psycopg

from ledger.models import Account, Posting, Transfer


@dataclass(frozen=True)
class Violation:
    code: str
    entity_id: str
    message: str


@dataclass(frozen=True)
class CheckReport:
    account_count: int
    transfer_count: int
    posting_count: int
    total_balance_cents: int
    total_posting_cents: int
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {"ok": self.ok, **asdict(self)}


def check_snapshot(
    accounts: Sequence[Account], transfers: Sequence[Transfer], postings: Sequence[Posting]
) -> CheckReport:
    """Audit a complete, consistent snapshot; amounts use exact Python integers.

    The library also supports in-memory callers and offline fixtures. Pass every
    row from one snapshot: filtering a ledger can hide or manufacture violations.
    """
    violations = []

    def fail(code: str, entity_id: str, message: str) -> None:
        violations.append(Violation(code, entity_id, message))

    account_ids = {a.id for a in accounts}
    transfer_ids = {t.id for t in transfers}
    balances = defaultdict(int)
    by_transfer = defaultdict(list)
    for posting in postings:
        balances[posting.account_id] += posting.amount_cents
        by_transfer[posting.transfer_id].append(posting)
        if posting.account_id not in account_ids:
            fail("orphan_account", str(posting.id), "posting references a missing account")
        if posting.transfer_id not in transfer_ids:
            fail("orphan_transfer", str(posting.id), "posting references a missing transfer")
        if posting.amount_cents == 0:
            fail("zero_posting", str(posting.id), "posting has zero cents")

    for account in accounts:
        if not account.allow_negative and account.balance_cents < 0:
            fail("negative_balance", account.id, "overdraft is disallowed")
        if account.balance_cents != balances[account.id]:
            fail("balance_mismatch", account.id, "balance does not equal the sum of its postings")

    key_counts = Counter(t.idempotency_key for t in transfers)
    for key, count in key_counts.items():
        if count > 1:
            fail("duplicate_idempotency_key", key, f"key appears on {count} transfers")
        if not key.strip():
            fail("invalid_idempotency_key", key, "idempotency key is empty")

    for transfer in transfers:
        entries = by_transfer[transfer.id]
        if transfer.source_id not in account_ids or transfer.destination_id not in account_ids:
            fail("missing_transfer_account", transfer.id, "transfer references a missing account")
        if transfer.source_id == transfer.destination_id or transfer.amount_cents <= 0:
            fail("invalid_transfer", transfer.id, "transfer needs distinct accounts and positive cents")
        if len(entries) != 2:
            fail("posting_count", transfer.id, f"expected 2 postings, found {len(entries)}")
        if sum(p.amount_cents for p in entries) != 0:
            fail("unbalanced_transfer", transfer.id, "transfer postings do not sum to zero")
        actual = Counter((p.account_id, p.amount_cents) for p in entries)
        expected = Counter([
            (transfer.source_id, -transfer.amount_cents),
            (transfer.destination_id, transfer.amount_cents),
        ])
        if actual != expected:
            fail("posting_mismatch", transfer.id, "postings do not match the recorded request")
        counts = Counter(p.account_id for p in entries)
        if any(count > 1 for count in counts.values()):
            fail("duplicate_posting", transfer.id, "account was posted more than once for a transfer")

    # Include orphan transfers too: a balanced valid subset must not conceal them.
    for transfer_id in by_transfer.keys() - transfer_ids:
        if sum(p.amount_cents for p in by_transfer[transfer_id]) != 0:
            fail("unbalanced_transfer", transfer_id, "orphan transfer postings do not sum to zero")

    total_balance = sum(a.balance_cents for a in accounts)
    total_postings = sum(p.amount_cents for p in postings)
    if total_balance != 0:
        fail("unbalanced_accounts", "ledger", "account balances do not sum to zero")
    if total_postings != 0:
        fail("unbalanced_postings", "ledger", "postings do not sum to zero")

    return CheckReport(
        len(accounts), len(transfers), len(postings), total_balance, total_postings,
        tuple(sorted(violations, key=lambda v: (v.code, v.entity_id, v.message))),
    )


def check_invariants(dsn: str) -> CheckReport:
    """Read all rows in one REPEATABLE READ, READ ONLY transaction; never repair.

    Concurrent commits cannot produce a partially observed transfer. Memory use
    is linear in the number of stored rows, appropriate for this initial checker.
    """
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        accounts = [Account(*row) for row in conn.execute(
            "SELECT id, allow_negative, balance_cents FROM accounts ORDER BY id"
        )]
        transfers = [Transfer(*row) for row in conn.execute(
            "SELECT id::text, idempotency_key, source_id, destination_id, amount_cents "
            "FROM transfers ORDER BY id"
        )]
        postings = [Posting(*row) for row in conn.execute(
            "SELECT id, transfer_id::text, account_id, amount_cents FROM postings ORDER BY id"
        )]
        return check_snapshot(accounts, transfers, postings)
