#!/usr/bin/env python3
"""Phase 31 launch precondition F12: workspace-registration verification.

Phase 31BR root-caused the Phase 31B pilot's `FAILED_SAFE` outcome:
`executor_workspace_binding.bind_openclaw_workspace` requires an
`openclaw.json` agent whose `workspace` field exactly matches a target
project's resolved workspace path -- a fresh, purpose-created Phase 31
project (which F10 requires every target to be, to dodge legacy realpath
collisions) has no such entry until an operator adds one. This is the
Phase 22C-0 fail-closed agent-selection guard working as designed (it
closed a real Phase 22A incident where OpenClaw silently fell back to its
default agent/workspace and executed against the wrong project) -- F12
does not weaken or route around that guard. It only detects, before
dispatch, whether the operator-procedure step (registering the target
workspace) has already been done, the same way F10 detects a workspace
collision and F11 detects a dirty tree: read-only, no remediation, no new
agent identity invented.

Reuses, does not duplicate: `resolve_project_workspace_path` (workspace
path resolution, same as F10's `workspace_collision_audit`) and
`executor_workspace_binding._find_template_agent_id` /
`_paths_match` (the exact match semantics the real dispatch path uses --
importing them, not reimplementing them, is the only way this check can
guarantee it detects precisely what dispatch will hit).

Exit code 0: every declared target project resolves to a workspace path
with a matching `openclaw.json` agent entry.
Exit code 1: at least one declared target project has no matching agent
(or does not exist).

Usage:
    python3 scripts/maintenance/phase31_launch_precondition_f12_workspace_registration.py \
        --project-ids 101 102 103
    python3 scripts/maintenance/phase31_launch_precondition_f12_workspace_registration.py \
        --project-ids 101 --openclaw-config /root/.openclaw/openclaw.json --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models import Project  # noqa: E402
from app.services.orchestration.execution.executor_workspace_binding import (  # noqa: E402
    _find_template_agent_id,
)
from app.services.workspace.project_isolation_service import (  # noqa: E402
    resolve_project_workspace_path,
)

DEFAULT_OPENCLAW_CONFIG_PATH = Path("/root/.openclaw/openclaw.json")


def check_workspace_registrations(
    project_ids: list[int], openclaw_config_path: Path = DEFAULT_OPENCLAW_CONFIG_PATH
) -> dict:
    try:
        config = json.loads(openclaw_config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as a check failure, not a crash
        return {
            "declared_targets": project_ids,
            "passed": False,
            "failures": [
                {
                    "reason": "openclaw_config_unreadable",
                    "openclaw_config_path": str(openclaw_config_path),
                    "detail": str(exc),
                }
            ],
            "checked": [],
        }

    db = SessionLocal()
    try:
        checked: list[dict] = []
        failures: list[dict] = []
        for project_id in project_ids:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project is None:
                failures.append(
                    {"project_id": project_id, "reason": "project_not_found"}
                )
                continue
            resolved = resolve_project_workspace_path(
                project.workspace_path, project.name, db=db
            )
            agent_id = _find_template_agent_id(config, resolved)
            entry = {
                "project_id": project_id,
                "resolved_workspace": str(resolved),
                "matched_agent_id": agent_id,
            }
            checked.append(entry)
            if not agent_id:
                failures.append(
                    {
                        "project_id": project_id,
                        "reason": "no_matching_openclaw_agent",
                        "resolved_workspace": str(resolved),
                    }
                )
    finally:
        db.close()

    return {
        "declared_targets": project_ids,
        "openclaw_config_path": str(openclaw_config_path),
        "passed": len(failures) == 0,
        "failures": failures,
        "checked": checked,
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
        "--openclaw-config",
        type=Path,
        default=DEFAULT_OPENCLAW_CONFIG_PATH,
        help="Path to the real openclaw.json (default: operator's persistent config).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    args = parser.parse_args()

    result = check_workspace_registrations(args.project_ids, args.openclaw_config)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["passed"]:
            print(
                f"F12 PASSED: {len(result['declared_targets'])} declared target "
                "project(s) each have a matching openclaw.json agent workspace."
            )
        else:
            print(
                f"F12 FAILED: {len(result['failures'])} of "
                f"{len(result['declared_targets'])} declared target project(s) "
                "have no matching openclaw.json agent workspace."
            )
            for failure in result["failures"]:
                print(f"  - {failure}")
            print(
                "  Remediation: register an agent for the target workspace in "
                f"{args.openclaw_config} (operator procedure; this check does "
                "not modify that file)."
            )

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
