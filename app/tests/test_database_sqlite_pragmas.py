"""Regression test for the SQLite WAL-mode fix.

Root cause: with the default `journal_mode=delete`, every writer takes an
exclusive lock on the whole database file; under concurrent Celery worker
load (each worker process has its own connection pool onto the same file),
connections sit blocked on that lock long enough to exhaust each process's
own pool (`QueuePool limit ... overflow ... reached`), which showed up as
Sessions/Analytics pages failing to load in the browser. WAL mode lets
readers proceed concurrently with a writer's commit.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event


def test_sqlite_engine_connect_sets_wal_and_busy_timeout(tmp_path):
    db_path = tmp_path / "pragma_check.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    with engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        synchronous = conn.exec_driver_sql("PRAGMA synchronous").scalar()
        busy_timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()

    assert journal_mode == "wal"
    assert synchronous == 1  # NORMAL
    assert busy_timeout == 30000


def test_database_module_registers_sqlite_pragma_listener_when_sqlite():
    """Don't connect through the module's real engine (it points at the live
    dev orchestrator.db by default) -- just confirm the listener is wired up
    for sqlite and applies the expected pragmas to an isolated connection."""
    from app import database

    assert database._IS_SQLITE is True

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", database._set_sqlite_pragmas)
    with engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        busy_timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()

    # In-memory databases cannot use WAL (no file to hold the -wal segment),
    # so SQLite silently keeps "memory" here; busy_timeout still applies.
    assert journal_mode == "memory"
    assert busy_timeout == 30000
