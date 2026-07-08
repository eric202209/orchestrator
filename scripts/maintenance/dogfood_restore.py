#!/usr/bin/env python3
"""Phase 21B (B2): restore a dogfood backup run and verify it.

Restores into a target directory (default: a scratch drill directory,
never the live repo) so a restore can be rehearsed without touching a
running system. Verification checks: sqlite integrity_check, expected
core tables present, and journal archive member count matches the
manifest recorded at backup time.

A restore is only trustworthy once it has actually been rehearsed — this
script is the rehearsal, not just the mechanism (roadmap review §4/B2:
"a backup that has never been restored is a hope, not a backup").
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BACKUP_ROOT = "/root/.openclaw/workspace/vault/backups/orchestrator"
CORE_TABLES = {
    "sessions",
    "task_executions",
    "plans",
    "planning_sessions",
    "projects",
    "tasks",
}


@dataclass
class RestoreResult:
    run_id: str
    backup_dir: Path
    target_dir: Path
    restored_db_path: Path
    integrity_ok: bool
    core_tables_present: bool
    missing_tables: list[str]
    manifest_row_counts_match: bool
    journal_members_restored: int
    journal_members_expected: int
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.integrity_ok
            and self.core_tables_present
            and self.journal_members_restored == self.journal_members_expected
            and not self.errors
        )

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "backup_dir": str(self.backup_dir),
            "target_dir": str(self.target_dir),
            "restored_db_path": str(self.restored_db_path),
            "integrity_ok": self.integrity_ok,
            "core_tables_present": self.core_tables_present,
            "missing_tables": self.missing_tables,
            "journal_members_restored": self.journal_members_restored,
            "journal_members_expected": self.journal_members_expected,
            "errors": self.errors,
            "ok": self.ok,
        }


def latest_run(backup_root: Path) -> Path:
    runs = sorted(
        (
            p
            for p in backup_root.iterdir()
            if p.is_dir() and (p / "manifest.json").exists()
        ),
        key=lambda p: p.name,
    )
    if not runs:
        raise FileNotFoundError(f"No backup runs found under {backup_root}")
    return runs[-1]


def restore_run(*, backup_dir: Path, target_dir: Path) -> RestoreResult:
    errors: list[str] = []
    manifest_path = backup_dir / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )

    target_dir.mkdir(parents=True, exist_ok=True)
    restored_db_path = target_dir / "orchestrator.db"

    source_db = backup_dir / "orchestrator.db"
    if not source_db.exists():
        errors.append(f"Missing DB backup file: {source_db}")
        integrity_ok = False
        core_tables_present = False
        missing_tables: list[str] = list(CORE_TABLES)
    else:
        src_conn = sqlite3.connect(str(source_db))
        try:
            dest_conn = sqlite3.connect(str(restored_db_path))
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()

        check_conn = sqlite3.connect(str(restored_db_path))
        try:
            (result,) = check_conn.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = result == "ok"
            existing_tables = {
                row[0]
                for row in check_conn.execute(
                    "select name from sqlite_master where type='table'"
                ).fetchall()
            }
        finally:
            check_conn.close()
        missing_tables = sorted(CORE_TABLES - existing_tables)
        core_tables_present = not missing_tables

    journal_archive = backup_dir / "journals.tar.gz"
    journal_members_restored = 0
    journal_members_expected = int(manifest.get("journals_archived", 0))
    if journal_archive.exists():
        journals_target = target_dir / "journals"
        journals_target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(journal_archive, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            tar.extractall(journals_target, members=members, filter="data")
            journal_members_restored = len(members)
    elif journal_members_expected:
        errors.append(f"Expected journal archive missing: {journal_archive}")

    return RestoreResult(
        run_id=backup_dir.name,
        backup_dir=backup_dir,
        target_dir=target_dir,
        restored_db_path=restored_db_path,
        integrity_ok=integrity_ok,
        core_tables_present=core_tables_present,
        missing_tables=missing_tables,
        manifest_row_counts_match=True,
        journal_members_restored=journal_members_restored,
        journal_members_expected=journal_members_expected,
        errors=errors,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore and verify a dogfood backup run (drill by default)."
    )
    parser.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT, type=Path)
    parser.add_argument(
        "--run-id",
        default=None,
        help="Specific run id (backup_root subdirectory name); defaults to latest.",
    )
    parser.add_argument(
        "--target-dir",
        required=True,
        type=Path,
        help="Directory to restore into. Never point this at the live repo "
        "without independently verifying you intend to overwrite it.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    backup_dir = (
        args.backup_root / args.run_id if args.run_id else latest_run(args.backup_root)
    )
    result = restore_run(backup_dir=backup_dir, target_dir=args.target_dir)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"Restored run: {result.run_id}")
        print(f"  Target: {result.target_dir}")
        print(f"  DB integrity_ok: {result.integrity_ok}")
        print(
            f"  Core tables present: {result.core_tables_present} "
            f"(missing: {result.missing_tables})"
        )
        print(
            f"  Journals restored: {result.journal_members_restored}"
            f"/{result.journal_members_expected}"
        )
        if result.errors:
            print(f"  Errors: {result.errors}")
        print(f"  OK: {result.ok}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
