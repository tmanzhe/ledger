from dataclasses import replace
from types import SimpleNamespace

import psycopg
import pytest

from ledger import check_invariants, check_snapshot
from ledger.models import Account, Posting, Transfer


def codes(report):
    return {violation.code for violation in report.violations}


@pytest.fixture
def snapshot():
    return (
        [Account("clearing", True, -100), Account("alice", False, 100), Account("bob", False, 0)],
        [Transfer("t1", "fund", "clearing", "alice", 100)],
        [Posting(1, "t1", "clearing", -100), Posting(2, "t1", "alice", 100)],
    )


def test_clean_snapshot_reports_counts_and_exact_totals(snapshot):
    report = check_snapshot(*snapshot)
    assert report.ok and report.violations == ()
    assert report.to_dict() == {
        "ok": True, "account_count": 3, "transfer_count": 1, "posting_count": 2,
        "total_balance_cents": 0, "total_posting_cents": 0, "violations": (),
    }


def test_empty_snapshot_is_valid():
    report = check_snapshot([], [], [])
    assert report.ok and report.account_count == report.transfer_count == report.posting_count == 0


def test_balance_corruption_is_detected_even_when_global_total_is_zero(snapshot):
    accounts, transfers, postings = snapshot
    accounts[1] = replace(accounts[1], balance_cents=99)
    accounts[2] = replace(accounts[2], balance_cents=1)
    report = check_snapshot(accounts, transfers, postings)
    assert report.total_balance_cents == 0
    assert codes(report) == {"balance_mismatch"}
    assert {v.entity_id for v in report.violations} == {"alice", "bob"}


def test_disallowed_negative_detected_even_when_reconciled(snapshot):
    accounts, transfers, postings = snapshot
    accounts[0] = replace(accounts[0], allow_negative=False)
    assert codes(check_snapshot(accounts, transfers, postings)) == {"negative_balance"}


def test_missing_posting_is_detected(snapshot):
    accounts, transfers, postings = snapshot
    report = check_snapshot(accounts, transfers, postings[:1])
    assert {"posting_count", "unbalanced_transfer", "unbalanced_postings", "posting_mismatch",
            "balance_mismatch"} <= codes(report)


def test_missing_both_postings_is_detected_despite_zero_posting_total(snapshot):
    accounts, transfers, _ = snapshot
    report = check_snapshot(accounts, transfers, [])
    assert report.total_posting_cents == 0
    assert {"posting_count", "posting_mismatch", "balance_mismatch"} <= codes(report)


def test_duplicate_balanced_pair_is_not_hidden_by_zero_total(snapshot):
    accounts, transfers, postings = snapshot
    accounts[0] = replace(accounts[0], balance_cents=-200)
    accounts[1] = replace(accounts[1], balance_cents=200)
    postings += [replace(p, id=p.id + 2) for p in postings]
    report = check_snapshot(accounts, transfers, postings)
    assert report.total_posting_cents == report.total_balance_cents == 0
    assert codes(report) == {"posting_count", "duplicate_posting", "posting_mismatch"}


def test_duplicate_key_is_detected_even_when_both_transfers_balance(snapshot):
    accounts, transfers, postings = snapshot
    accounts[0] = replace(accounts[0], balance_cents=-200)
    accounts[1] = replace(accounts[1], balance_cents=200)
    transfers.append(replace(transfers[0], id="t2"))
    postings += [replace(p, id=p.id + 2, transfer_id="t2") for p in postings]
    report = check_snapshot(accounts, transfers, postings)
    assert codes(report) == {"duplicate_idempotency_key"}


def test_balanced_wrong_amounts_are_detected(snapshot):
    accounts, transfers, postings = snapshot
    transfers[0] = replace(transfers[0], amount_cents=99)
    assert codes(check_snapshot(accounts, transfers, postings)) == {"posting_mismatch"}


def test_wrong_account_is_detected_even_when_all_totals_reconcile(snapshot):
    accounts, transfers, postings = snapshot
    transfers[0] = replace(transfers[0], destination_id="bob")
    assert codes(check_snapshot(accounts, transfers, postings)) == {"posting_mismatch"}


def test_opposing_corruptions_do_not_cancel_per_transfer_detection(snapshot):
    accounts, transfers, postings = snapshot
    transfers.append(Transfer("t2", "other", "alice", "bob", 10))
    postings += [Posting(3, "t2", "alice", -10), Posting(4, "t2", "bob", 9)]
    postings[1] = replace(postings[1], amount_cents=101)
    accounts[1] = replace(accounts[1], balance_cents=91)
    accounts[2] = replace(accounts[2], balance_cents=9)
    report = check_snapshot(accounts, transfers, postings)
    assert report.total_posting_cents == report.total_balance_cents == 0
    assert {v.entity_id for v in report.violations if v.code == "unbalanced_transfer"} == {"t1", "t2"}


@pytest.mark.parametrize("mutation,expected", [
    (lambda a, t, p: a.pop(1), "orphan_account"),
    (lambda a, t, p: t.clear(), "orphan_transfer"),
    (lambda a, t, p: t.__setitem__(0, replace(t[0], source_id="missing")), "missing_transfer_account"),
    (lambda a, t, p: t.__setitem__(0, replace(t[0], amount_cents=0)), "invalid_transfer"),
    (lambda a, t, p: t.__setitem__(0, replace(t[0], source_id="alice")), "invalid_transfer"),
    (lambda a, t, p: t.__setitem__(0, replace(t[0], idempotency_key=" ")), "invalid_idempotency_key"),
    (lambda a, t, p: p.__setitem__(0, replace(p[0], amount_cents=0)), "zero_posting"),
])
def test_corrupt_references_and_invalid_values_are_reported(snapshot, mutation, expected):
    mutation(*snapshot)
    report = check_snapshot(*snapshot)
    assert not report.ok and expected in codes(report)


def test_nonzero_global_balances_and_postings_are_reported(snapshot):
    accounts, transfers, postings = snapshot
    accounts[1] = replace(accounts[1], balance_cents=101)
    postings[1] = replace(postings[1], amount_cents=101)
    report = check_snapshot(accounts, transfers, postings)
    assert report.total_balance_cents == report.total_posting_cents == 1
    assert {"unbalanced_accounts", "unbalanced_postings", "unbalanced_transfer"} <= codes(report)


def test_checking_neither_mutates_data_nor_depends_on_input_order(snapshot):
    before = tuple(tuple(rows) for rows in snapshot)
    report = check_snapshot(*snapshot)
    assert tuple(tuple(rows) for rows in snapshot) == before
    assert check_snapshot(*(list(reversed(rows)) for rows in snapshot)) == report


def test_database_checker_detects_balance_corruption_without_repair(funded):
    with psycopg.connect(funded.dsn) as conn:
        conn.execute("UPDATE accounts SET balance_cents = balance_cents + 1 WHERE id = 'alice'")
    first = check_invariants(funded.dsn)
    assert not first.ok and {"balance_mismatch", "unbalanced_accounts"} <= codes(first)
    assert first.total_balance_cents == 1
    assert funded.get_account("alice").balance_cents == 1001
    assert check_invariants(funded.dsn) == first


def test_database_checker_detects_disallowed_negative(funded):
    with psycopg.connect(funded.dsn) as conn:
        conn.execute("ALTER TABLE accounts DROP CONSTRAINT accounts_no_overdraft")
        conn.execute("UPDATE accounts SET balance_cents = -1 WHERE id = 'alice'")
    assert "negative_balance" in codes(check_invariants(funded.dsn))


def test_database_checker_detects_deleted_posting(funded):
    with psycopg.connect(funded.dsn) as conn:
        conn.execute("DELETE FROM postings WHERE account_id = 'alice'")
    assert {"posting_count", "posting_mismatch", "unbalanced_transfer"} <= codes(
        check_invariants(funded.dsn)
    )


def test_database_checker_detects_duplicate_postings_after_constraint_bypass(funded):
    with psycopg.connect(funded.dsn) as conn:
        conn.execute("ALTER TABLE postings DROP CONSTRAINT postings_transfer_account_unique")
        conn.execute("INSERT INTO postings (transfer_id, account_id, amount_cents) "
                     "SELECT transfer_id, account_id, amount_cents FROM postings")
        conn.execute("UPDATE accounts SET balance_cents = balance_cents * 2")
    report = check_invariants(funded.dsn)
    assert report.total_balance_cents == report.total_posting_cents == 0
    assert codes(report) == {"posting_count", "duplicate_posting", "posting_mismatch"}


def test_database_checker_detects_duplicate_key_after_constraint_bypass(funded):
    funded.transfer("alice", "bob", 100, idempotency_key="payment")
    with psycopg.connect(funded.dsn) as conn:
        conn.execute("ALTER TABLE transfers DROP CONSTRAINT transfers_idempotency_key_unique")
        conn.execute("UPDATE transfers SET idempotency_key = 'fund'")
    assert codes(check_invariants(funded.dsn)) == {"duplicate_idempotency_key"}


def test_database_checker_detects_orphans_after_fk_bypass(funded):
    with psycopg.connect(funded.dsn) as conn:
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM accounts WHERE id = 'alice'")
    assert {"orphan_account", "orphan_transfer"} <= codes(check_invariants(funded.dsn))


def test_checker_snapshot_is_consistent_across_concurrent_commit_and_read_only(funded, monkeypatch):
    import ledger.checker as checker

    original_connect = psycopg.connect
    before = check_invariants(funded.dsn)

    class ObservedConnection:
        def __enter__(self):
            self.conn = original_connect(funded.dsn)
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def execute(self, query):
            cursor = self.conn.execute(query)
            if query.startswith("SELECT id, allow_negative"):
                # Prove the transaction enforces read-only mode at the server.
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    with self.conn.transaction():
                        self.conn.execute("UPDATE accounts SET balance_cents = 0")
                # A different connection commits between the checker's SELECTs.
                funded.transfer("alice", "bob", 100, idempotency_key="during-audit")
            return cursor

    monkeypatch.setattr(checker, "psycopg", SimpleNamespace(connect=lambda _: ObservedConnection()))
    assert checker.check_invariants(funded.dsn) == before
    monkeypatch.undo()
    after = check_invariants(funded.dsn)
    assert after.ok and after.transfer_count == before.transfer_count + 1
