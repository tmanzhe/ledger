from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import select
import socket
import subprocess
import sys
from threading import Barrier, Thread
from urllib.parse import quote

import psycopg
import pytest

from ledger.http import LedgerHTTPServer, MAX_BODY_BYTES


@contextmanager
def serving(dsn):
    with LedgerHTTPServer(("127.0.0.1", 0), dsn) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server.server_address
        finally:
            server.shutdown()
            thread.join(timeout=10)


def call(address, method, path, payload=None, *, raw=None, headers=None):
    body = raw if raw is not None else (None if payload is None else json.dumps(payload))
    with closing(HTTPConnection(*address, timeout=15)) as connection:
        connection.request(method, path, body, headers or {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read()
        assert response.getheader("Content-Type") == "application/json"
        assert int(response.getheader("Content-Length")) == len(data)
        return response.status, json.loads(data)


@pytest.fixture
def api(ledger):
    with serving(ledger.dsn) as address:
        yield address


@pytest.fixture
def funded_api(api):
    for account_id in ("clearing", "alice", "bob"):
        status, _ = call(api, "POST", "/accounts", {
            "id": account_id, "allow_negative": account_id == "clearing",
        })
        assert status == 200
    assert call(api, "POST", "/transfers", {
        "source_id": "clearing", "destination_id": "alice", "amount_cents": 1000,
        "idempotency_key": "fund",
    })[0] == 200
    return api


def payment(**changes):
    return {"source_id": "alice", "destination_id": "bob", "amount_cents": 100,
            "idempotency_key": "pay", **changes}


def balance(api, account_id):
    status, account = call(api, "GET", "/accounts/" + quote(account_id, safe=""))
    assert status == 200
    return account["balance_cents"]


def audit(api):
    status, report = call(api, "GET", "/check")
    assert status == 200 and report["ok"]
    assert report["total_balance_cents"] == report["total_posting_cents"] == 0
    return report


def test_accounts_round_trip_unicode_and_repeat_creation(api):
    account = {"id": "café / savings", "allow_negative": False, "balance_cents": 0}
    assert call(api, "POST", "/accounts", {"id": account["id"]}) == (200, account)
    assert call(api, "POST", "/accounts", {"id": account["id"]}) == (200, account)
    assert balance(api, account["id"]) == 0
    status, error = call(api, "POST", "/accounts", {"id": account["id"], "allow_negative": True})
    assert status == 409 and error["error"] == "AccountConflict"
    assert audit(api)["account_count"] == 1


def test_transfer_replay_and_conflict_do_not_post_twice(funded_api):
    api = funded_api
    status, first = call(api, "POST", "/transfers", payment())
    assert status == 200
    assert call(api, "POST", "/transfers", payment()) == (200, first)
    assert {key: first[key] for key in payment()} == payment()
    before = audit(api)
    for changes in ({"amount_cents": 99}, {"source_id": "clearing"}, {"destination_id": "clearing"}):
        status, error = call(api, "POST", "/transfers", payment(**changes))
        assert status == 409 and error["error"] == "IdempotencyConflict"
    assert balance(api, "alice") == 900 and balance(api, "bob") == 100
    assert audit(api) == before
    assert before["transfer_count"] == 2 and before["posting_count"] == 4


@pytest.mark.parametrize("changes", [
    {"amount_cents": 0}, {"amount_cents": -1}, {"amount_cents": 1.0},
    {"amount_cents": True}, {"amount_cents": "1"}, {"amount_cents": None},
    {"amount_cents": 2**63}, {"source_id": "bob", "destination_id": "bob"},
    {"source_id": []}, {"destination_id": None}, {"idempotency_key": " "},
    {"source_id": "a\x00b"}, {"idempotency_key": "\ud800"},
])
def test_invalid_transfers_leave_no_writes(funded_api, changes):
    before = audit(funded_api)
    status, error = call(funded_api, "POST", "/transfers", payment(**changes))
    assert status == 400 and error["error"] == "InvalidRequest"
    assert audit(funded_api) == before


@pytest.mark.parametrize("payload", [
    {}, {"id": " "}, {"id": False}, {"id": "alice", "allow_negative": 1},
    {"id": "alice", "allow_negative": "false"}, {"id": "alice", "balance_cents": 100},
])
def test_invalid_accounts_do_not_create_rows(api, payload):
    assert call(api, "POST", "/accounts", payload)[0] == 400
    assert audit(api)["account_count"] == 0


@pytest.mark.parametrize("raw,status,headers", [
    (b'{"id":', 400, None), (b'[]', 400, None), (b'null', 400, None),
    (b'{"id":"a","id":"b"}', 400, None), (b'{"id":NaN}', 400, None),
    (b'{"id":Infinity}', 400, None), (b'{"id":"\xff"}', 400, None),
    (b'{' * 2000, 400, None),
    (b' ' * (MAX_BODY_BYTES + 1), 413, None),
    (b'{"id":"alice"}', 415, {"Content-Type": "text/plain"}),
    (b'{}', 400, {"Content-Type": "application/json", "Transfer-Encoding": "chunked"}),
])
def test_bad_json_and_framing_are_json_errors(api, raw, status, headers):
    assert call(api, "POST", "/accounts", raw=raw, headers=headers)[0] == status
    assert audit(api)["account_count"] == 0


@pytest.mark.parametrize("length_header,body", [
    (b"", b""), (b"Content-Length: -1\r\n", b""),
    (b"Content-Length: 2\r\nContent-Length: 2\r\n", b"{}"),
    (b"Content-Length: 20\r\n", b"{}"),
])
def test_missing_ambiguous_or_truncated_length(api, length_header, body):
    with socket.create_connection(api, timeout=5) as connection:
        connection.sendall(b"POST /accounts HTTP/1.0\r\nContent-Type: application/json\r\n"
                           + length_header + b"\r\n" + body)
        connection.shutdown(socket.SHUT_WR)
        with connection.makefile("rb") as stream:
            response = stream.read()
    head, data = response.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.0 400")
    assert json.loads(data)["error"] == "InvalidRequest"
    assert audit(api)["account_count"] == 0


def test_routes_methods_and_missing_accounts(api):
    assert call(api, "GET", "/unknown")[0] == 404
    assert call(api, "GET", "/accounts/missing")[0] == 404
    assert call(api, "GET", "/accounts")[0] == 405
    assert call(api, "DELETE", "/accounts/a")[0] == 405
    assert call(api, "POST", "/check", {})[0] == 405
    status, error = call(api, "POST", "/transfers", payment())
    assert status == 404 and error["error"] == "AccountNotFound"
    assert audit(api)["transfer_count"] == 0


def test_failed_debit_key_can_be_used_after_funding(funded_api):
    before = audit(funded_api)
    status, error = call(funded_api, "POST", "/transfers", payment(amount_cents=1001))
    assert status == 409 and error["error"] == "InsufficientFunds"
    assert audit(funded_api) == before
    assert call(funded_api, "POST", "/transfers", payment(
        source_id="clearing", destination_id="alice", amount_cents=1, idempotency_key="topup"
    ))[0] == 200
    assert call(funded_api, "POST", "/transfers", payment(amount_cents=1001))[0] == 200
    assert balance(funded_api, "alice") == 0
    assert audit(funded_api)["transfer_count"] == 3


def test_signed_64_bit_limit_and_overflow_roll_back(funded_api):
    assert call(funded_api, "POST", "/accounts", {"id": "big-clearing", "allow_negative": True})[0] == 200
    assert call(funded_api, "POST", "/transfers", payment(
        source_id="big-clearing", amount_cents=2**63 - 1, idempotency_key="max"
    ))[0] == 200
    before = audit(funded_api)
    status, error = call(funded_api, "POST", "/transfers", payment(amount_cents=1))
    assert status == 409 and error["error"] == "BalanceOverflow"
    assert balance(funded_api, "bob") == 2**63 - 1
    assert audit(funded_api) == before


def test_storage_failure_is_sanitized_and_rolls_back(funded_api, ledger):
    with psycopg.connect(ledger.dsn) as conn:
        conn.execute("ALTER TABLE postings ADD CONSTRAINT private_failure CHECK (account_id <> 'bob')")
    before = audit(funded_api)
    status, error = call(funded_api, "POST", "/transfers", payment())
    assert status == 503
    assert error == {"error": "DatabaseError", "message": "database operation failed"}
    assert audit(funded_api) == before
    with psycopg.connect(ledger.dsn) as conn:
        conn.execute("ALTER TABLE postings DROP CONSTRAINT private_failure")
    assert call(funded_api, "POST", "/transfers", payment())[0] == 200
    assert audit(funded_api)["transfer_count"] == 2


def test_database_errors_and_missing_schema_are_not_clean_reports(postgres_dsn):
    for dsn in (postgres_dsn, "private-invalid-option=value"):
        with serving(dsn) as address:
            status, error = call(address, "GET", "/check")
            assert status == 503
            assert error == {"error": "DatabaseError", "message": "database operation failed"}


def test_check_reports_corruption_without_repair(funded_api, ledger):
    with psycopg.connect(ledger.dsn) as conn:
        conn.execute("UPDATE accounts SET balance_cents = 42 WHERE id = 'bob'")
    status, report = call(funded_api, "GET", "/check")
    assert status == 409 and not report["ok"] and report["total_balance_cents"] == 42
    assert {v["code"] for v in report["violations"]} == {"balance_mismatch", "unbalanced_accounts"}
    assert call(funded_api, "GET", "/check") == (status, report)
    assert balance(funded_api, "bob") == 42


def together(count, action):
    barrier = Barrier(count)

    def run(index):
        barrier.wait(timeout=10)
        return action(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def test_concurrent_replays_return_one_transfer(funded_api):
    results = together(20, lambda _: call(funded_api, "POST", "/transfers", payment()))
    assert all(status == 200 for status, _ in results)
    assert len({body["id"] for _, body in results}) == 1
    assert balance(funded_api, "alice") == 900 and balance(funded_api, "bob") == 100
    assert audit(funded_api)["posting_count"] == 4


def test_concurrent_conflicting_keys_have_one_winner(funded_api):
    results = together(20, lambda i: call(funded_api, "POST", "/transfers", payment(amount_cents=100+i)))
    winners = [body for status, body in results if status == 200]
    assert len(winners) == 1
    assert all(body["error"] == "IdempotencyConflict" for status, body in results if status != 200)
    assert sum(status == 409 for status, _ in results) == 19
    assert balance(funded_api, "bob") == winners[0]["amount_cents"]
    assert audit(funded_api)["transfer_count"] == 2


def test_concurrent_debits_never_overdraft(funded_api):
    results = together(20, lambda i: call(funded_api, "POST", "/transfers", payment(idempotency_key=f"p{i}")))
    assert sum(status == 200 for status, _ in results) == 10
    assert sum(status == 409 and body["error"] == "InsufficientFunds" for status, body in results) == 10
    assert balance(funded_api, "alice") == 0 and balance(funded_api, "bob") == 1000
    assert audit(funded_api)["posting_count"] == 22


def test_serve_command_runs_real_http_process(ledger):
    process = subprocess.Popen(
        [sys.executable, "-m", "ledger", "--dsn", ledger.dsn, "serve", "--port", "0"],
        cwd=Path(__file__).resolve().parents[1] / "src",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert select.select([process.stdout], [], [], 10)[0], "server did not start"
        address = json.loads(process.stdout.readline())
        assert audit((address["host"], address["port"]))["account_count"] == 0
    finally:
        process.terminate()
        _, stderr = process.communicate(timeout=10)
    assert stderr == ""


def test_load_script_reports_latency_and_verifies_exact_state(api):
    script = Path(__file__).resolve().parents[1] / "scripts" / "load_test.py"
    result = subprocess.run(
        [sys.executable, str(script), "--url", f"http://{api[0]}:{api[1]}",
         "--requests", "81", "--workers", "12", "--accounts", "4"],
        capture_output=True, text=True, timeout=30,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0 and result.stderr == "" and report["ok"]
    assert report["requests"] == 81 and report["unique_transfers"] == 41
    assert report["replay_requests"] == 40 and report["failed_requests"] == 0
    assert report["throughput_requests_per_second"] > 0
    assert 0 < report["p50_ms"] <= report["p99_ms"]
    assert report["balances_match"] and report["counts_match"] and report["retries_match"]
    assert report["invariants"] == audit(api)
    assert report["invariants"]["transfer_count"] == 45
    assert report["invariants"]["posting_count"] == 90


def test_load_script_exits_nonzero_for_failed_invariants(funded_api, ledger):
    with psycopg.connect(ledger.dsn) as conn:
        conn.execute("UPDATE accounts SET balance_cents = 42 WHERE id = 'bob'")
    script = Path(__file__).resolve().parents[1] / "scripts" / "load_test.py"
    result = subprocess.run(
        [sys.executable, str(script), "--url", f"http://{funded_api[0]}:{funded_api[1]}", "--requests", "2"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1 and result.stderr == ""
    assert not json.loads(result.stdout)["ok"]


@pytest.mark.parametrize("fault", ["corruption", "storage_error"])
def test_load_script_reports_failures_after_concurrent_load(api, ledger, fault):
    # Real storage faults apply only to measured transfers, after funding passes.
    action = ("UPDATE accounts SET balance_cents = balance_cents + 1 WHERE id = NEW.account_id;"
              if fault == "corruption" else "RAISE EXCEPTION 'injected failure' USING ERRCODE = '23514';")
    with psycopg.connect(ledger.dsn) as conn:
        conn.execute("""
            CREATE FUNCTION fault_load() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.amount_cents < 0 AND EXISTS (
                    SELECT 1 FROM transfers WHERE id = NEW.transfer_id
                    AND idempotency_key LIKE '%%-pay-%%'
                ) THEN
            """ + action + """
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER fault_load AFTER INSERT ON postings
            FOR EACH ROW EXECUTE FUNCTION fault_load();
        """)
    script = Path(__file__).resolve().parents[1] / "scripts" / "load_test.py"
    result = subprocess.run(
        [sys.executable, str(script), "--url", f"http://{api[0]}:{api[1]}",
         "--requests", "8", "--workers", "4", "--accounts", "2"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1 and result.stderr == ""
    report = json.loads(result.stdout)
    assert not report["ok"] and report["throughput_requests_per_second"] > 0
    assert 0 < report["p50_ms"] <= report["p99_ms"]
    if fault == "corruption":
        assert report["failed_requests"] == 0 and report["counts_match"]
        assert not report["invariants"]["ok"] and not report["balances_match"]
    else:
        assert report["failed_requests"] == 8 and report["error_samples"]
        assert report["invariants"]["ok"] and not report["counts_match"]
