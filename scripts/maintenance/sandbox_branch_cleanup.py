#!/usr/bin/env python3
"""Inventory and clean managed sandbox branch residue (Phase 22B-1X1 §6).

Read-only by default. ``--apply`` performs the deletion through the same
ownership validation that normal sandbox disposal uses, and prints the
before/after ledger so the operator can retain it as evidence.

    python3 scripts/maintenance/sandbox_branch_cleanup.py --project-root .
    python3 scripts/maintenance/sandbox_branch_cleanup.py --project-root . \
        --branch orchestrator/task-241 --branch orchestrator/task-242 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.workspace.sandbox_branch_maintenance import (  # noqa: E402
    cleanup_sandbox_branches,
    inventory_sandbox_branches,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument(
        "--branch",
        action="append",
        dest="branches",
        help="Restrict to these branches (repeatable). Default: every managed branch.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the selected branches instead of only reporting.",
    )
    parser.add_argument("--inventory-only", action="store_true")
    args = parser.parse_args()

    project_root = Path(args.project_root).expanduser().resolve()

    db = None
    try:
        from app.database import get_db_session

        db = get_db_session()
    except Exception as exc:  # noqa: BLE001 - inventory works without the DB
        print(f"warning: TaskExecution correlation unavailable: {exc}", file=sys.stderr)

    try:
        if args.inventory_only:
            payload = inventory_sandbox_branches(project_root, db=db)
        else:
            payload = cleanup_sandbox_branches(
                project_root, branches=args.branches, db=db, apply=args.apply
            )
    finally:
        if db is not None:
            db.close()

    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
