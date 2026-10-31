from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest

from ledger import check_invariants
from ledger.core import (
    MAX_CENTS, AccountConflict, AccountNotFound, BalanceOverflow,
    IdempotencyConflict, InsufficientFunds,
)


def test_empty_schema_and_initialization_are_repeatable(ledger):
    ledger.initialize()
    report = check_invariants(ledger.dsn)
    assert report.ok
    assert (report.account_count, report.transfer_count, report.posting_count) == (0, 0, 0)


def test_account_creation_is_idempotent_and_starts_at_zero(ledger):
    first = ledger.create_account("alice")
    assert first == ledger.create_account("alice")
    assert first.balance_cents == 0
    assert not first.allow_negative
    with pytest.raises(AccountConflict):
        ledger.create_account("alice", allow_negative=True)
    assert ledger.get_account("alice") == first


def test_missing_account_is_explicit(ledger):
    with pytest.raises(AccountNotFound):
        ledger.get_account("missing")


def test_transfer_persists_exact_balances_and_both_entries(funded):
    transfer = funded.transfer("alice", "bob", 123, idempotency_key="payment")
    assert funded.get_account("alice").balance_cents == 877
    assert funded.get_account("bob").balance_cents == 123
    assert funded.get_account("clearing").balance_cents == -1000
    with psycopg.connect(funded.dsn) as conn:
        rows = conn.execute(
            "SELECT account_id, amount_cents FROM postings WHERE transfer_id = %s ORDER BY account_id",
            (transfer.id,),
        ).fetchall()
    assert rows == [("alice", -123), ("bob", 123)]
    report = check_invariants(funded.dsn)
    assert report.ok
    assert (report.transfer_count, report.posting_count, report.total_balance_cents) == (2, 4, 0)


def test_replay_returns_original_even_after_balance_is_exhausted(funded):
    first = funded.transfer("alice", "bob", 1000, idempotency_key="payment")
    assert funded.transfer("alice", "bob", 1000, idempotency_key="payment") == first
    assert funded.get_account("alice").balance_cents == 0
    report = check_invariants(funded.dsn)
    assert report.ok and report.transfer_count == 2 and report.posting_count == 4


@pytest.mark.parametrize("source,destination,amount", [
    ("alice", "bob", 200), ("bob", "alice", 100), ("alice", "clearing", 100),
])
def test_key_reuse_with_changed_request_is_rejected(funded, source, destination, amount):
    funded.transfer("alice", "bob", 100, idempotency_key="payment")
    before = check_invariants(funded.dsn)
    with pytest.raises(IdempotencyConflict):
        funded.transfer(source, destination, amount, idempotency_key="payment")
    assert check_invariants(funded.dsn) == before


def test_overdraft_rolls_back_and_does_not_consume_key(funded):
    before = check_invariants(funded.dsn)
    with pytest.raises(InsufficientFunds):
        funded.transfer("alice", "bob", 1001, idempotency_key="retry")
    assert check_invariants(funded.dsn) == before
    funded.transfer("clearing", "alice", 1, idempotency_key="top-up")
    funded.transfer("alice", "bob", 1001, idempotency_key="retry")
    assert funded.get_account("alice").balance_cents == 0
    assert check_invariants(funded.dsn).ok


def test_missing_destination_does_not_leave_partial_transfer(funded):
    before = check_invariants(funded.dsn)
    with pytest.raises(AccountNotFound):
        funded.transfer("alice", "missing", 1, idempotency_key="retry")
    assert check_invariants(funded.dsn) == before
    funded.create_account("missing")
    funded.transfer("alice", "missing", 1, idempotency_key="retry")
    assert check_invariants(funded.dsn).ok


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "100", MAX_CENTS + 1])
def test_invalid_amounts_are_rejected_before_writing(funded, amount):
    before = check_invariants(funded.dsn)
    with pytest.raises(ValueError):
        funded.transfer("alice", "bob", amount, idempotency_key="bad")
    assert check_invariants(funded.dsn) == before


@pytest.mark.parametrize("source,destination,key", [
    ("alice", "alice", "bad"), ("", "bob", "bad"), ("alice", " ", "bad"),
    ("alice", "bob", ""), ("alice", "bob", "  "),
])
def test_invalid_identifiers_are_rejected(funded, source, destination, key):
    before = check_invariants(funded.dsn)
    with pytest.raises(ValueError):
        funded.transfer(source, destination, 1, idempotency_key=key)
    assert check_invariants(funded.dsn) == before


def test_integer_boundary_and_overflow_are_exact_and_atomic(ledger):
    ledger.create_account("clearing", allow_negative=True)
    ledger.create_account("alice")
    ledger.transfer("clearing", "alice", MAX_CENTS, idempotency_key="max")
    assert ledger.get_account("alice").balance_cents == MAX_CENTS
    before = check_invariants(ledger.dsn)
    with pytest.raises(BalanceOverflow):
        ledger.transfer("clearing", "alice", 1, idempotency_key="overflow")
    assert check_invariants(ledger.dsn) == before
    assert before.ok


def test_negative_balance_boundary_cannot_underflow(ledger):
    ledger.create_account("clearing", allow_negative=True)
    ledger.create_account("alice")
    ledger.create_account("bob")
    ledger.transfer("clearing", "alice", MAX_CENTS, idempotency_key="max")
    ledger.transfer("clearing", "bob", 1, idempotency_key="minimum")
    assert ledger.get_account("clearing").balance_cents == -(2**63)
    before = check_invariants(ledger.dsn)
    with pytest.raises(BalanceOverflow):
        ledger.transfer("clearing", "bob", 1, idempotency_key="underflow")
    assert check_invariants(ledger.dsn) == before
    assert before.ok


def test_posting_failure_rolls_back_balances_request_and_first_entry(funded):
    # Inject a storage failure after the debit posting, exercising real rollback.
    with psycopg.connect(funded.dsn) as conn:
        conn.execute("ALTER TABLE postings ADD CONSTRAINT reject_bob CHECK (account_id <> 'bob')")
    before = check_invariants(funded.dsn)
    with pytest.raises(psycopg.errors.CheckViolation):
        funded.transfer("alice", "bob", 100, idempotency_key="retry")
    assert check_invariants(funded.dsn) == before
    with psycopg.connect(funded.dsn) as conn:
        conn.execute("ALTER TABLE postings DROP CONSTRAINT reject_bob")
    funded.transfer("alice", "bob", 100, idempotency_key="retry")
    assert check_invariants(funded.dsn).ok


def run_together(count, action):
    barrier = Barrier(count)

    def run(index):
        barrier.wait(timeout=10)
        return action(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def test_twenty_concurrent_replays_post_exactly_once(funded):
    results = run_together(20, lambda _: funded.transfer(
        "alice", "bob", 100, idempotency_key="one-payment"
    ))
    assert len({result.id for result in results}) == 1
    assert funded.get_account("alice").balance_cents == 900
    assert funded.get_account("bob").balance_cents == 100
    report = check_invariants(funded.dsn)
    assert report.ok and report.transfer_count == 2 and report.posting_count == 4


def test_concurrent_different_requests_with_one_key_have_one_winner(funded):
    def pay(index):
        try:
            return funded.transfer("alice", "bob", 100 + index, idempotency_key="shared")
        except IdempotencyConflict:
            return None

    results = run_together(20, pay)
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    amount = winners[0].amount_cents
    assert funded.get_account("alice").balance_cents == 1000 - amount
    assert funded.get_account("bob").balance_cents == amount
    report = check_invariants(funded.dsn)
    assert report.ok and report.transfer_count == 2 and report.posting_count == 4


def test_twenty_competing_debits_never_overdraft(funded):
    def pay(index):
        try:
            funded.transfer("alice", "bob", 100, idempotency_key=f"payment-{index}")
            return True
        except InsufficientFunds:
            return False

    results = run_together(20, pay)
    assert sum(results) == 10
    assert funded.get_account("alice").balance_cents == 0
    assert funded.get_account("bob").balance_cents == 1000
    report = check_invariants(funded.dsn)
    assert report.ok and report.transfer_count == 11 and report.posting_count == 22


def test_opposing_transfers_complete_without_deadlock(funded):
    funded.transfer("clearing", "bob", 1000, idempotency_key="fund-bob")

    def pay(index):
        source, destination = ("alice", "bob") if index % 2 else ("bob", "alice")
        return funded.transfer(source, destination, 1, idempotency_key=f"opposing-{index}")

    run_together(20, pay)
    assert funded.get_account("alice").balance_cents == 1000
    assert funded.get_account("bob").balance_cents == 1000
    assert check_invariants(funded.dsn).ok
