"""Disposable local Postgres only; no pre-existing database or credentials."""

from pathlib import Path
import os
import pwd
import shutil
import subprocess
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from ledger import Ledger


def pytest_addoption(parser):
    parser.addoption("--postgres-bin", help="directory containing initdb and pg_ctl")


@pytest.fixture(scope="session")
def postgres_dsn(request, tmp_path_factory):
    supplied = request.config.getoption("--postgres-bin")
    binary = Path(supplied) if supplied else Path(shutil.which("initdb") or "/nonexistent").parent
    if not all((binary / name).is_file() for name in ("initdb", "pg_ctl")):
        pytest.fail("Local Postgres required: pass --postgres-bin=/path/to/postgres/bin")
    root = tmp_path_factory.mktemp("postgres")
    data = root / "data"
    socket = root / "socket"
    socket.mkdir(mode=0o700)
    subprocess.run(
        [str(binary / "initdb"), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        [str(binary / "pg_ctl"), "-D", str(data), "-l", str(root / "server.log"),
         "-o", f"-F -h '' -k {socket}", "-w", "start"],
        check=True, capture_output=True, text=True,
    )
    try:
        yield make_conninfo(
            host=str(socket), dbname="postgres", user=pwd.getpwuid(os.getuid()).pw_name,
            passfile=str(root / "unused-password-file"), connect_timeout=5,
        )
    finally:
        subprocess.run(
            [str(binary / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"],
            check=True, capture_output=True, text=True,
        )


@pytest.fixture
def ledger(postgres_dsn):
    schema = "test_" + uuid4().hex
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    dsn = make_conninfo(postgres_dsn, options=f"-c search_path={schema}")
    ledger = Ledger(dsn)
    ledger.initialize()
    try:
        yield ledger
    finally:
        with psycopg.connect(postgres_dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def funded(ledger):
    ledger.create_account("clearing", allow_negative=True)
    ledger.create_account("alice")
    ledger.create_account("bob")
    ledger.transfer("clearing", "alice", 1000, idempotency_key="fund")
    return ledger
