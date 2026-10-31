# Milestones on this branch

The initial branch contained only the README pitch and project skeleton (commit
`0543894`), with no milestone plan or implementation. These are the minimal
milestone definitions used for this implementation.

- **M1: storage:** Postgres schema, signed 64-bit integer cents, zero-balance
  account creation, explicit overdraft policy, transfer records and postings.
- **M2: posting:** atomic transfers, exactly two opposite postings, account
  locks in a stable order, insufficient-funds checks, overflow rejection, and
  durable idempotency keys bound to the complete request. Failed transactions
  leave neither balances, postings, nor reserved keys behind.
- **M3: audit:** an independent library and JSON command that read one
  consistent, read-only snapshot and report invariant violations. Audits never
  repair data or rely on write-path checks to establish correctness.
- **M4: HTTP:** a stdlib JSON API for accounts, transfers, and invariant reports,
  HTTP behavior tests, and a concurrent load script that reports throughput and
  latency and verifies exact balances, counts, retries, and invariants.

All four milestones are implemented. Behavior tests use an isolated local
Postgres cluster (Unix socket, no TCP listener, no existing credentials) and
offline snapshot fixtures. HTTP tests talk to a local loopback server.
