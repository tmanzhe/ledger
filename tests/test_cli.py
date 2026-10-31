import json
from pathlib import Path
import subprocess
import sys

import psycopg
import pytest

from ledger.cli import main


def test_check_command_reports_clean_counts(funded, capsys):
    assert main(["--dsn", funded.dsn, "check"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == ""
    assert report["ok"] and report["violations"] == []
    assert (report["account_count"], report["transfer_count"], report["posting_count"]) == (3, 1, 2)
    assert report["total_balance_cents"] == report["total_posting_cents"] == 0


def test_check_command_exits_one_for_corruption(funded, capsys):
    with psycopg.connect(funded.dsn) as conn:
        conn.execute("UPDATE accounts SET balance_cents = 42 WHERE id = 'bob'")
    assert main(["--dsn", funded.dsn, "check"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert not report["ok"] and report["total_balance_cents"] == 42
    assert "balance_mismatch" in {v["code"] for v in report["violations"]}
    assert captured.err == ""


def test_command_errors_exit_two_without_echoing_connection_details(capsys):
    # Invalid connection syntax cannot reach a network or consult credentials.
    assert main(["--dsn", "private-invalid-option=value", "check"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private-invalid-option" not in captured.err
    assert json.loads(captured.err)["error"] == "DatabaseError"


def test_cli_account_transfer_and_replay(ledger, capsys):
    prefix = ["--dsn", ledger.dsn]
    for command in (
        ["init"], ["account", "clearing", "--allow-negative"], ["account", "alice"],
    ):
        assert main(prefix + command) == 0
        assert json.loads(capsys.readouterr().out)
    command = ["transfer", "clearing", "alice", "100", "--key", "fund"]
    assert main(prefix + command) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(prefix + command) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert main(prefix + ["check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["posting_count"] == 2 and report["total_balance_cents"] == 0


def test_cli_rejected_transfer_returns_two(funded, capsys):
    assert main(["--dsn", funded.dsn, "transfer", "alice", "bob", "1001", "--key", "bad"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "InsufficientFunds"


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_commands_have_real_process_exit_codes(funded, entrypoint):
    # Run from src so this also works before installing the editable package.
    executable = ([sys.executable, "-m", "ledger"] if entrypoint == "module" else
                  [str(Path(sys.executable).parent / "ledger")])
    command = executable + ["--dsn", funded.dsn, "check"]
    cwd = Path(__file__).resolve().parents[1] / "src"
    clean = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    assert clean.returncode == 0 and json.loads(clean.stdout)["ok"]
    with psycopg.connect(funded.dsn) as conn:
        conn.execute("DELETE FROM postings")
    broken = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    assert broken.returncode == 1 and not json.loads(broken.stdout)["ok"]
    assert clean.stderr == broken.stderr == ""


def test_missing_schema_is_an_error_never_a_clean_empty_report(postgres_dsn, capsys):
    assert main(["--dsn", postgres_dsn, "check"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "DatabaseError"
