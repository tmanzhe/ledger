# ledger

A small double-entry ledger on Postgres, with a Python library, CLI, and local
HTTP API. Money is integer cents. Transfers are atomic and idempotent, and
normal accounts cannot overdraft.

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

Start the stdlib HTTP server after `init`:

```sh
uv run ledger --dsn 'host=/tmp/ledger-socket dbname=ledger_demo' serve --port 8000
```

It binds to `127.0.0.1` by default. Requests and responses use JSON.

| Route | Request |
| --- | --- |
| `POST /accounts` | `{"id":"alice","allow_negative":false}`; policy defaults to false |
| `GET /accounts/{id}` | URL-encode the account ID; returns its balance and policy |
| `POST /transfers` | `{"source_id":"alice","destination_id":"bob","amount_cents":100,"idempotency_key":"pay-1"}` |
| `GET /check` | Returns the full invariant report |

Writes and identical retries return `200`. Bad fields or JSON return `400`,
missing accounts/routes `404`, wrong methods `405`, and rejected transfers or
conflicting retries `409`. `/check` returns `409` when it finds violations.
Bodies need `Content-Type: application/json` and `Content-Length`, with a 16 KiB
limit (`413`); other content types get `415`. Database failures return a generic
`503` JSON error.

Run the load script against a disposable database with no other writers:

```sh
uv run python scripts/load_test.py --url http://127.0.0.1:8000 --requests 1000 --workers 16 --accounts 8
```

It funds fresh accounts, races transfers and identical retries, then checks exact
balances, posting counts, retry IDs, and invariants over HTTP. It prints JSON
with requests/second and nearest-rank p50/p99 latency, and exits `1` on failure.
Timing covers transfer requests, including retries, and excludes setup and audits.
The accounts and transfers stay in the database.

One local Linux container run, Python 3.12.16 and Postgres 16.2 with fsync on:
1,000 requests, 16 workers, 8 funded accounts, 500 unique transfers and 500 retries
took 1.345 s: **743.36 requests/s**, **p50 19.038 ms**, **p99 58.525 ms**.
Zero failures; all checks passed. Including funding, the audit found 508 transfers,
1,016 postings, and zero cent totals. These are local measurements.

Run the full behavior suite against a disposable local Postgres cluster:

```sh
uv run pytest -q --postgres-bin=/path/to/postgres/bin
```

The directory must contain `initdb` and `pg_ctl` (omit the flag if both are on
`PATH`). Run as a non-root user, as required by `initdb`. Tests create and stop
their own temporary cluster with a Unix socket and a fresh schema per test.
HTTP tests use loopback sockets. They cover concurrent posting/retries, rollback,
integer limits, bad HTTP requests, corrupted data, snapshot isolation, process
exit codes, and the load script.
