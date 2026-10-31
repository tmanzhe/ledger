"""Concurrent HTTP transfers and retries against a disposable local ledger."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection, HTTPException
import json
from math import ceil
from time import perf_counter
from urllib.parse import quote, urlsplit
from uuid import uuid4


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def run_load(url: str, requests: int, workers: int, accounts: int) -> dict:
    target = urlsplit(url)
    if (target.scheme != "http" or not target.hostname or target.path not in ("", "/")
            or target.query or target.fragment or target.username or target.password):
        raise ValueError("url must be an http origin, such as http://127.0.0.1:8000")
    if requests < 1 or not 1 <= workers <= 256 or accounts < 2:
        raise ValueError("need requests >= 1, workers in 1..256, and accounts >= 2")

    def call(method, path, payload=None, *, allow_violation=False):
        body = None if payload is None else json.dumps(payload).encode()
        with closing(HTTPConnection(target.hostname, target.port or 80, timeout=15)) as connection:
            connection.request(method, path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read())
            if response.status != 200 and not (allow_violation and response.status == 409):
                raise RuntimeError(f"HTTP {response.status}: {result.get('error', 'check failed')}")
            return result

    initial = call("GET", "/check", allow_violation=True)
    if not initial["ok"]:
        raise RuntimeError("initial invariant check failed")
    prefix = "load-" + uuid4().hex
    clearing = prefix + "-clearing"
    ids = [f"{prefix}-{index}" for index in range(accounts)]
    call("POST", "/accounts", {"id": clearing, "allow_negative": True})
    funding = requests + 1
    for index, account_id in enumerate(ids):
        call("POST", "/accounts", {"id": account_id})
        call("POST", "/transfers", {
            "source_id": clearing, "destination_id": account_id,
            "amount_cents": funding, "idempotency_key": f"{prefix}-fund-{index}",
        })
    before = call("GET", "/check")

    def request(index):
        # Adjacent requests race on the same key. Different keys share accounts.
        unique = index // 2
        payload = {
            "source_id": ids[unique % accounts],
            "destination_id": ids[(unique + 1) % accounts],
            "amount_cents": 1, "idempotency_key": f"{prefix}-pay-{unique}",
        }
        started = perf_counter()
        try:
            result = call("POST", "/transfers", payload)
            if any(result.get(key) != value for key, value in payload.items()) or not result.get("id"):
                raise RuntimeError("transfer response differs from request")
            return (perf_counter() - started) * 1000, unique, result["id"], None
        except (OSError, HTTPException, ValueError, RuntimeError) as exc:
            return (perf_counter() - started) * 1000, unique, None, str(exc)

    started = perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(request, range(requests)))
    elapsed = perf_counter() - started
    latencies = sorted(row[0] for row in results)
    errors = [row[3] for row in results if row[3] is not None]
    seen = {}
    retries_match = True
    for _, unique, transfer_id, error in results:
        if error is None:
            if seen.setdefault(unique, transfer_id) != transfer_id:
                retries_match = False

    unique_count = (requests + 1) // 2
    after = call("GET", "/check", allow_violation=True)
    expected = {account_id: funding for account_id in ids}
    for unique in range(unique_count):
        expected[ids[unique % accounts]] -= 1
        expected[ids[(unique + 1) % accounts]] += 1
    expected[clearing] = -accounts * funding
    balances_match = all(
        call("GET", "/accounts/" + quote(account_id, safe=""))["balance_cents"] == balance
        for account_id, balance in expected.items()
    )
    counts_match = (
        after["account_count"] == before["account_count"]
        and after["transfer_count"] == before["transfer_count"] + unique_count
        and after["posting_count"] == before["posting_count"] + 2 * unique_count
    )
    ok = (before["ok"] and after["ok"] and not errors and retries_match
          and balances_match and counts_match)
    return {
        "ok": ok, "requests": requests, "workers": workers, "accounts": accounts,
        "unique_transfers": unique_count, "replay_requests": requests - unique_count,
        "failed_requests": len(errors), "error_samples": errors[:5],
        "elapsed_seconds": round(elapsed, 3),
        "throughput_requests_per_second": round(requests / elapsed, 2),
        "p50_ms": round(latencies[ceil(requests * 0.50) - 1], 3),
        "p99_ms": round(latencies[ceil(requests * 0.99) - 1], 3),
        "retries_match": retries_match, "balances_match": balances_match,
        "counts_match": counts_match, "invariants": after,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=positive, default=1000)
    parser.add_argument("--workers", type=positive, default=16)
    parser.add_argument("--accounts", type=positive, default=8)
    args = parser.parse_args(argv)
    try:
        result = run_load(args.url, args.requests, args.workers, args.accounts)
    except (OSError, HTTPException, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
