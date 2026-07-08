#!/usr/bin/env python3
"""Phase 21B (B2): nightly backup of the dogfood evidence corpus.

Backs up two things, per the roadmap review's recovery process and the
Phase 21A design (§8.3): the SQLite control-state database (via Python's
built-in online backup API, safe under WAL and concurrent readers/writers
— see https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup),
and the `.agent/events` diagnostic journals under every project workspace
registered in that database. Journals are included in the dogfood-window
backup scope even though steady-state `OPERATIONS.md` treats them as
non-backup-critical — during Phase 22 they are the evidentiary corpus, not
just diagnostic residue.

Read-only against the live database; does not touch application state.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tarfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB_PATH = "orchestrator.db"
DEFAULT_BACKUP_ROOT = "/root/.openclaw/workspace/vault/backups/orchestrator"
DEFAULT_RETENTION_DAYS = 14


@dataclass
class BackupResult:
    run_id: str
    db_path: Path
    backup_dir: Path
    db_backup_path: Path
    db_size_bytes: int
    db_integrity_ok: bool
    journals_archived: int
    journal_workspaces_scanned: int
    journal_archive_path: Path | None
    journal_archive_bytes: int
    pruned_runs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "db_path": str(self.db_path),
            "backup_dir": str(self.backup_dir),
            "db_backup_path": str(self.db_backup_path),
            "db_size_bytes": self.db_size_bytes,
            "db_integrity_ok": self.db_integrity_ok,
            "journals_archived": self.journals_archived,
            "journal_workspaces_scanned": self.journal_workspaces_scanned,
            "journal_archive_path": (
                str(self.journal_archive_path) if self.journal_archive_path else None
            ),
            "journal_archive_bytes": self.journal_archive_bytes,
            "pruned_runs": self.pruned_runs,
        }


def backup_database(db_path: Path, dest_path: Path) -> tuple[int, bool]:
    """Online-backup the sqlite DB via the sqlite3 backup API. Returns
    (size_bytes, integrity_ok)."""
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    check_conn = sqlite3.connect(str(dest_path))
    try:
        (result,) = check_conn.execute("PRAGMA integrity_check").fetchone()
        integrity_ok = result == "ok"
    finally:
        check_conn.close()

    return dest_path.stat().st_size, integrity_ok


def discover_project_workspaces(db_path: Path) -> list[Path]:
    """Read-only query for every non-deleted project's workspace_path."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select distinct workspace_path from projects "
            "where deleted_at is null and workspace_path is not null "
            "and workspace_path != ''"
        ).fetchall()
    finally:
        conn.close()
    return [Path(row[0]) for row in rows]


def find_journal_dirs(workspaces: list[Path]) -> list[Path]:
    """Every `.agent/events` directory under each project workspace that
    actually exists and has content."""
    found: list[Path] = []
    for workspace in workspaces:
        events_dir = workspace / ".agent" / "events"
        if events_dir.is_dir() and any(events_dir.iterdir()):
            found.append(events_dir)
    return found


def archive_journals(journal_dirs: list[Path], dest_archive: Path) -> tuple[int, int]:
    """Tar every journal file found under the given directories. Returns
    (file_count, archive_size_bytes). Writes an empty-but-valid archive if
    no journals exist yet (zeros are fine per the Phase 21A design)."""
    file_count = 0
    with tarfile.open(dest_archive, "w:gz") as tar:
        for events_dir in journal_dirs:
            for path in sorted(events_dir.rglob("*")):
                if path.is_file():
                    # Arcname keeps enough of the path to disambiguate
                    # workspaces without leaking the full host path.
                    arcname = "/".join(path.parts[-4:])
                    tar.add(path, arcname=arcname)
                    file_count += 1
    return file_count, dest_archive.stat().st_size if dest_archive.exists() else 0


def prune_old_runs(
    backup_root: Path, retention_days: int, keep_run_id: str
) -> list[str]:
    """Delete run directories older than retention_days. Never deletes the
    run just created."""
    if not backup_root.is_dir():
        return []
    cutoff = time.time() - retention_days * 86400
    pruned: list[str] = []
    for entry in sorted(backup_root.iterdir()):
        if not entry.is_dir() or entry.name == keep_run_id:
            continue
        try:
            marker = entry / "manifest.json"
            mtime = marker.stat().st_mtime if marker.exists() else entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            pruned.append(entry.name)
    return pruned


def run_backup(
    *,
    db_path: Path,
    backup_root: Path,
    retention_days: int,
) -> BackupResult:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir = backup_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    db_backup_path = run_dir / "orchestrator.db"
    db_size, integrity_ok = backup_database(db_path, db_backup_path)

    workspaces = discover_project_workspaces(db_path)
    journal_dirs = find_journal_dirs(workspaces)
    journal_archive_path = run_dir / "journals.tar.gz"
    journal_count, journal_bytes = archive_journals(journal_dirs, journal_archive_path)

    pruned = prune_old_runs(backup_root, retention_days, keep_run_id=run_id)

    result = BackupResult(
        run_id=run_id,
        db_path=db_path,
        backup_dir=run_dir,
        db_backup_path=db_backup_path,
        db_size_bytes=db_size,
        db_integrity_ok=integrity_ok,
        journals_archived=journal_count,
        journal_workspaces_scanned=len(workspaces),
        journal_archive_path=journal_archive_path,
        journal_archive_bytes=journal_bytes,
        pruned_runs=pruned,
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Back up the orchestrator DB + dogfood evidence journals."
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, type=Path)
    parser.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT, type=Path)
    parser.add_argument("--retention-days", default=DEFAULT_RETENTION_DAYS, type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_backup(
        db_path=args.db,
        backup_root=args.backup_root,
        retention_days=args.retention_days,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"Backup run: {result.run_id}")
        print(
            f"  DB backup: {result.db_backup_path} ({result.db_size_bytes} bytes, "
            f"integrity_ok={result.db_integrity_ok})"
        )
        print(f"  Journal workspaces scanned: {result.journal_workspaces_scanned}")
        print(
            f"  Journal files archived: {result.journals_archived} "
            f"({result.journal_archive_bytes} bytes) -> {result.journal_archive_path}"
        )
        if result.pruned_runs:
            print(f"  Pruned runs (> {args.retention_days}d): {result.pruned_runs}")

    return 0 if result.db_integrity_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
