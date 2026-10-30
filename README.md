# ledger

A money-correct double-entry ledger service on Postgres: amounts are integer cents, every write is idempotent, and accounts never overdraft even under heavy concurrent transfers.
