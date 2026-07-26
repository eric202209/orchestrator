#!/usr/bin/env python3
"""Phase 31 launch precondition F10: verified-unique resolved workspace paths.

Phase 30F finding F10 (carried forward through Phase 30L-1's freeze review)
requires that every Phase 31 target project have a resolved workspace path
that is not shared with any other project row, live or legacy. This is a
launch *check*, not a remediation tool: it reuses the existing read-only
`workspace_collision_audit.run_audit` (Phase 23B) and adds a pass/fail gate
scoped to the specific project IDs a Phase 31 run declares as its targets.

Exit code 0: every declared target project resolved successfully and is not
part of any collision group.
Exit code 1: at least one declared target project failed to resolve, or
shares a resolved workspace path with another project (target or legacy).

Usage:
    python3 scripts/maintenance/phase31_launch_precondition_f10_workspace_uniqueness.py \
        --project-ids 101 102 103
    python3 scripts/maintenance/phase31_launch_precondition_f10_workspace_uniqueness.py \
        --project-ids 101 102 103 --json
"""

from __future__ import annotations

import argparse
import json
import sys

from app.database import SessionLocal
from scripts.maintenance.workspace_collision_audit import run_audit


def check_targets(project_ids: list[int]) -> dict:
    db = SessionLocal()
    try:
        report = run_audit(db)
    finally:
        db.close()

    unresolved_ids = {row["project_id"] for row in report.unresolved}
    colliding_ids: dict[int, str] = {}
    for group in report.collisions:
        for pid in group.project_ids:
            if pid in project_ids:
                colliding_ids[pid] = group.resolved_path

    failures = []
    for pid in project_ids:
        if pid in unresolved_ids:
            failures.append({"project_id": pid, "reason": "unresolved"})
        elif pid in colliding_ids:
            failures.append(
                {
                    "project_id": pid,
                    "reason": "resolved_path_collision",
                    "resolved_path": colliding_ids[pid],
                }
            )

    return {
        "declared_targets": project_ids,
        "passed": len(failures) == 0,
        "failures": failures,
        "total_projects_scanned": report.total_projects,
        "total_collision_groups": len(report.collisions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-ids",
        type=int,
        nargs="+",
        required=True,
        help="Project IDs this Phase 31 run declares as targets.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    args = parser.parse_args()

    result = check_targets(args.project_ids)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["passed"]:
            print(
                f"F10 PASSED: {len(result['declared_targets'])} declared target "
                "project(s) each have a verified-unique resolved workspace path."
            )
        else:
            print(
                f"F10 FAILED: {len(result['failures'])} of "
                f"{len(result['declared_targets'])} declared target project(s) "
                "did not pass the uniqueness check."
            )
            for failure in result["failures"]:
                print(f"  - {failure}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
