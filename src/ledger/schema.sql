CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY CHECK (length(trim(id)) > 0),
    allow_negative BOOLEAN NOT NULL DEFAULT FALSE,
    balance_cents BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT accounts_no_overdraft CHECK (allow_negative OR balance_cents >= 0)
);

CREATE TABLE IF NOT EXISTS transfers (
    id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES accounts(id),
    destination_id TEXT NOT NULL REFERENCES accounts(id),
    amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
    CONSTRAINT transfers_idempotency_key_unique UNIQUE (idempotency_key),
    CHECK (length(trim(idempotency_key)) > 0),
    CHECK (source_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS postings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id UUID NOT NULL REFERENCES transfers(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount_cents BIGINT NOT NULL CHECK (amount_cents <> 0),
    CONSTRAINT postings_transfer_account_unique UNIQUE (transfer_id, account_id)
);

CREATE INDEX IF NOT EXISTS postings_account_id_idx ON postings(account_id);
