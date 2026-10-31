# ledger

A money-correct double-entry ledger service on Postgres: amounts are integer cents, every write is idempotent, and accounts never overdraft even under heavy concurrent transfers.

The current implementation provides a Python library and command. See
[the milestone plan](plan/README.md) for scope and implementation status.
It has no external API integrations and does not load environment files.

Install with Python 3.12+ and uv:

```sh
uv sync --group dev
```

Pass a connection string explicitly. These examples use a local, disposable
Postgres database called `ledger_demo` over a Unix socket:

```sh
uv run ledger --dsn 'host=/tmp/ledger-socket dbname=ledger_demo' init
uv run ledger --dsn 'host=/tmp/ledger-socket dbname=ledger_demo' account clearing --allow-negative
uv run ledger --dsn 'host=/tmp/ledger-socket dbname=ledger_demo' account alice
uv run ledger --dsn 'host=/tmp/ledger-socket dbname=ledger_demo' account bob
uv run ledger --dsn 'host=/tmp/ledger-socket dbname=ledger_demo' transfer clearing alice 1000 --key funding-1
uv run ledger --dsn 'host=/tmp/ledger-socket dbname=ledger_demo' transfer alice bob 125 --key payment-1
uv run ledger --dsn 'host=/tmp/ledger-socket dbname=ledger_demo' check
```

Accounts start at zero. Fund an account with a balanced transfer from an explicitly
overdraft-enabled clearing account. Normal accounts cannot go negative. All
amounts are positive integer cents, up to `2**63 - 1`; resulting balances must
also fit signed 64-bit integers. Each database represents a single currency.

Repeating a transfer with the same key and identical source, destination, and
amount returns the original transfer without posting again. Reusing that key
with a changed request fails. An unsuccessful transfer does not consume its key.
Account creation is also repeatable with the same account ID and overdraft policy.

The checker reconciles stored balances with postings, checks global zero sums,
overdraft policy, each transfer's two entries and their recorded request, duplicate
keys/postings, and dangling references. It reports all violations in stable order
as JSON, including account/transfer/posting counts and cent totals. Exit codes:
`0` clean, `1` invariant violation, `2` usage, rejected operation, or database error.
A missing/uninitialized schema is a database error, never a clean empty ledger.
The checker uses a read-only repeatable-read transaction, so concurrent writes
cannot expose half a transfer. It loads the complete snapshot into memory.

Use the same checker from Python:

```python
from ledger import Ledger, check_invariants

dsn = "host=/tmp/ledger-socket dbname=ledger_demo"
ledger = Ledger(dsn)
report = check_invariants(dsn)
assert report.ok, report.violations
print(report.to_dict())
```

`check_snapshot(accounts, transfers, postings)` also audits a complete offline
snapshot using the immutable values in `ledger.models`. Neither checker repairs
data. Database constraints reinforce the writer, while the checker independently
detects corrupt records inserted after constraints were bypassed.

Run the full behavior suite against a disposable local Postgres cluster:

```sh
uv run pytest -q --postgres-bin=/path/to/postgres/bin
```

The directory must contain `initdb` and `pg_ctl` (omit the flag if both are on
`PATH`). Run as a non-root user, as required by `initdb`. Tests create and stop
their own temporary cluster, listen only on a Unix socket, isolate each test in a
fresh schema, and never connect to an existing database. They cover concurrent
posting/retries, rollback, integer limits, corrupted data, snapshot isolation,
and the command's actual process exit codes. No paid or real external API calls
are needed. No tests existed in the initial skeleton.
