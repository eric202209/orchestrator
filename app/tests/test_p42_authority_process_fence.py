"""Independent processes share the database fence, with no Python mutex."""

import multiprocessing
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.orchestration.phases.execution_loop import _reserve_mutation_authority


def _cancel(path, attempted, blocked, release, committed):
    connection = sqlite3.connect(path, timeout=0)
    attempted.set()
    try:
        connection.execute("UPDATE tasks SET status='CANCELLED' WHERE id=1")
    except sqlite3.OperationalError as error:
        assert "locked" in str(error)
        blocked.set()
        assert release.wait(10)
        connection.execute("UPDATE tasks SET status='CANCELLED' WHERE id=1")
    connection.execute("UPDATE sessions SET status='stopped', is_active=0 WHERE id=1")
    connection.commit()
    committed.set()
    connection.close()


def test_cross_process_mutation_and_cancellation_order(tmp_path):
    path = str(tmp_path / "authority.db")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT, updated_at TEXT);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, status TEXT, is_active BOOLEAN);
        INSERT INTO tasks VALUES(1, 'RUNNING', NULL);
        INSERT INTO sessions VALUES(1, 'running', 1);
    """
    )
    connection.close()
    engine = create_engine("sqlite:///" + path)
    context = multiprocessing.get_context("spawn")
    attempted, blocked, release, committed = [context.Event() for _ in range(4)]
    process = context.Process(
        target=_cancel, args=(path, attempted, blocked, release, committed)
    )
    try:
        with Session(engine) as db:
            assert _reserve_mutation_authority(db, 1, 1) is None
            process.start()
            assert attempted.wait(10)
            assert blocked.wait(10)
            assert not committed.is_set()
            target = tmp_path / "workspace.txt"
            target.write_text("authorized")
            db.commit()
            release.set()
            assert committed.wait(10)
            process.join(10)
            assert process.exitcode == 0
            # Cancellation now wins: a new mutation authorization is rejected.
            assert _reserve_mutation_authority(db, 1, 1) == "task_authority_lost"
            assert target.read_text() == "authorized"
            db.rollback()
    finally:
        release.set()
        if process.pid:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
        engine.dispose()
