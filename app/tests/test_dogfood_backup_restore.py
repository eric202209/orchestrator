from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.maintenance.dogfood_backup import (
    archive_journals,
    backup_database,
    discover_project_workspaces,
    find_journal_dirs,
    prune_old_runs,
    run_backup,
)
from scripts.maintenance.dogfood_restore import restore_run


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            create table projects (
                id integer primary key, name text, workspace_path text,
                deleted_at text
            );
            create table sessions (id integer primary key);
            create table task_executions (id integer primary key);
            create table plans (id integer primary key);
            create table planning_sessions (id integer primary key);
            create table tasks (id integer primary key);
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_backup_database_online_backup_and_integrity(tmp_path):
    db_path = tmp_path / "source.db"
    _make_db(db_path)
    dest_path = tmp_path / "backup.db"

    size, ok = backup_database(db_path, dest_path)

    assert ok is True
    assert size > 0
    assert dest_path.exists()


def test_discover_project_workspaces_excludes_deleted_and_empty(tmp_path):
    db_path = tmp_path / "source.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        create table projects (
            id integer primary key, name text, workspace_path text,
            deleted_at text
        );
        insert into projects (id, name, workspace_path, deleted_at) values
            (1, 'active', '/tmp/ws1', null),
            (2, 'deleted', '/tmp/ws2', '2026-01-01'),
            (3, 'no_path', '', null);
        """
    )
    conn.commit()
    conn.close()

    workspaces = discover_project_workspaces(db_path)

    assert workspaces == [Path("/tmp/ws1")]


def test_find_journal_dirs_only_returns_nonempty(tmp_path):
    ws_with_journals = tmp_path / "ws1"
    events_dir = ws_with_journals / ".agent" / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "session_1_task_1.jsonl").write_text('{"event_type": "x"}\n')

    ws_empty = tmp_path / "ws2"
    (ws_empty / ".agent" / "events").mkdir(parents=True)

    ws_missing = tmp_path / "ws3"
    ws_missing.mkdir()

    found = find_journal_dirs([ws_with_journals, ws_empty, ws_missing])

    assert found == [events_dir]


def test_archive_journals_zero_journals_is_valid(tmp_path):
    dest_archive = tmp_path / "journals.tar.gz"

    count, size = archive_journals([], dest_archive)

    assert count == 0
    assert dest_archive.exists()
    assert size > 0  # a valid empty gzip/tar archive is non-zero bytes


def test_prune_old_runs_keeps_new_run_and_recent(tmp_path):
    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    keep = backup_root / "20260708-000000"
    keep.mkdir()
    (keep / "manifest.json").write_text("{}")

    old = backup_root / "20260101-000000"
    old.mkdir()
    old_manifest = old / "manifest.json"
    old_manifest.write_text("{}")
    old_time = 0  # epoch — far older than any retention window
    import os

    os.utime(old_manifest, (old_time, old_time))

    pruned = prune_old_runs(backup_root, retention_days=14, keep_run_id=keep.name)

    assert pruned == [old.name]
    assert keep.exists()
    assert not old.exists()


def test_run_backup_end_to_end_and_restore_round_trip(tmp_path):
    db_path = tmp_path / "orchestrator.db"
    _make_db(db_path)

    ws_dir = tmp_path / "projects" / "demo"
    events_dir = ws_dir / ".agent" / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "session_1_task_1.jsonl").write_text(
        '{"event_type": "plan_candidate_validated"}\n'
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "insert into projects (id, name, workspace_path, deleted_at) values (1, 'demo', ?, null)",
        (str(ws_dir),),
    )
    conn.commit()
    conn.close()

    backup_root = tmp_path / "backups"
    result = run_backup(db_path=db_path, backup_root=backup_root, retention_days=14)

    assert result.db_integrity_ok is True
    assert result.journal_workspaces_scanned == 1
    assert result.journals_archived == 1

    restore_target = tmp_path / "restore_drill"
    restored = restore_run(backup_dir=result.backup_dir, target_dir=restore_target)

    assert restored.ok is True
    assert restored.integrity_ok is True
    assert restored.core_tables_present is True
    assert restored.journal_members_restored == 1
    assert (restore_target / "orchestrator.db").exists()
    assert (restore_target / "journals").exists()
