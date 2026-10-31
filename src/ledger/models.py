"""Immutable values shared by the writer and the independent checker."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    id: str
    allow_negative: bool
    balance_cents: int


@dataclass(frozen=True)
class Transfer:
    id: str
    idempotency_key: str
    source_id: str
    destination_id: str
    amount_cents: int


@dataclass(frozen=True)
class Posting:
    id: int
    transfer_id: str
    account_id: str
    amount_cents: int
