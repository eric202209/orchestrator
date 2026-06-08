"""RepoMemory injection validation — post-hoc evidence collector.

Scans workspace project dirs for task evidence after REPO_MEMORY_INJECTION_ENABLED=True
has been active. Produces the per-task table needed for the injection validation report.

Usage:
    PYTHONPATH=. python3 scripts/maintenance/validate_repo_memory_injection.py [workspace_root]

workspace_root defaults to /root/.openclaw/workspace/vault/projects

Output:
  - Per-task table (stdout)
  - Summary statistics (stdout)
  - docs/roadmap/reports/maintenance/repo-memory-injection-evidence-YYYYMMDD.json
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.orchestration.repo_memory import (  # noqa: E402
    load_repo_memory,
    render_repo_memory,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_TASK_TERMINAL_EVENTS = {
    "task_completed",
    "task_failed",
    "task_cancelled",
    "task_aborted",
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def _extract_verification_commands(plan_steps: List[Dict[str, Any]]) -> List[str]:
    cmds: List[str] = []
    for step in plan_steps:
        v = step.get("verification") or step.get("verify") or ""
        if v:
            cmds.append(str(v).strip())
    return cmds


def _test_cmd_in_verification(test_cmd: str, verification_cmds: List[str]) -> bool:
    """True if injected test_command appears in any plan verification command."""
    tc = test_cmd.lower().strip()
    for v in verification_cmds:
        if tc in v.lower():
            return True
    return False


def _classify_usage(
    injected_test_cmd: Optional[str],
    verification_cmds: List[str],
    task_outcome: str,
    baseline_repair_count: int,
) -> Tuple[str, str]:
    """Return (classification, note).

    USED      — injected test_command appears in at least one verification command
    IGNORED   — injected but test_command not found in plan
    HARMFUL   — injected RepoMemory data contradicts actual working commands
    SKIPPED   — injection did not fire (Task 1 / no cache / no stable facts)
    """
    if not injected_test_cmd:
        return "SKIPPED", "no stable facts / cache absent"
    if not verification_cmds:
        return "SKIPPED", "no verification commands in plan"
    match = _test_cmd_in_verification(injected_test_cmd, verification_cmds)
    if match:
        return "USED", f"test_command={injected_test_cmd!r} found in plan"
    # Not found — could be ignored or harmful.
    # Harmful = task failed AND the planner used a wrong command that clashes.
    # We can't prove causation without diff, so flag as IGNORED unless the
    # task failed AND none of the verification commands resemble a valid runner.
    if task_outcome in {"task_failed", "task_aborted"} and verification_cmds:
        # If verification cmds contain something that looks like a wrong runner
        # (e.g. "npm test" on a pure-python project where injected was "pytest"),
        # flag as potential HARMFUL.
        python_runners = {"pytest", "python -m pytest", "python3 -m pytest"}
        node_runners = {"npm test", "yarn test", "npx jest"}
        injected_is_python = any(r in injected_test_cmd.lower() for r in python_runners)
        injected_is_node = any(r in injected_test_cmd.lower() for r in node_runners)
        for v in verification_cmds:
            v_lower = v.lower()
            if injected_is_python and any(r in v_lower for r in node_runners):
                return "HARMFUL", f"python project but plan used node runner: {v!r}"
            if injected_is_node and any(r in v_lower for r in python_runners):
                return "HARMFUL", f"node project but plan used python runner: {v!r}"
    return "IGNORED", f"test_command={injected_test_cmd!r} not found in plan"


# ── Core scan ─────────────────────────────────────────────────────────────────


def scan_project_dir(project_dir: Path) -> List[Dict[str, Any]]:
    """Return per-task evidence records for a single project workspace."""
    records: List[Dict[str, Any]] = []

    # Load repo memory for this project dir.
    memory = load_repo_memory(project_dir)
    repo_line = render_repo_memory(memory) if memory else None
    injected_test_cmd = memory.test_command if memory else None

    events_dir = project_dir / ".agent" / "events"
    if not events_dir.is_dir():
        return records

    for jsonl_path in sorted(events_dir.glob("session_*_task_*.jsonl")):
        m = re.match(r"session_(\d+)_task_(\d+)\.jsonl", jsonl_path.name)
        if not m:
            continue
        session_id = int(m.group(1))
        task_id = int(m.group(2))

        events = _load_jsonl(jsonl_path)
        if not events:
            continue

        # Derive outcome from last terminal event.
        outcome = "unknown"
        for ev in reversed(events):
            et = (ev.get("event_type") or "").lower()
            if et in _TASK_TERMINAL_EVENTS:
                outcome = et
                break

        # Extract plan steps from any event that carries them.
        plan_steps: List[Dict[str, Any]] = []
        repair_count = 0
        for ev in events:
            d = ev.get("details") or {}
            if "plan_steps" in d and isinstance(d["plan_steps"], list):
                plan_steps = d["plan_steps"]
            # Count repair events as a proxy for repair activity.
            et = (ev.get("event_type") or "").lower()
            if "repair" in et:
                repair_count += 1

        verification_cmds = _extract_verification_commands(plan_steps)

        # Did injection fire? Repo line must exist and project must be Task 2+.
        # Heuristic: if repo_memory.json exists and session/task suggest continuation,
        # injection likely fired. Task 1 of a session (plan_position=1) skips.
        task_started_events = [
            ev for ev in events if (ev.get("event_type") or "") == "task_started"
        ]
        is_first_ordered = False
        for ev in task_started_events:
            d = ev.get("details") or {}
            # run_start_runtime_identity is in task_started
            # plan_position is not directly recorded — infer from task_id sequence
            pass

        injection_fired = repo_line is not None

        classification, class_note = _classify_usage(
            injected_test_cmd if injection_fired else None,
            verification_cmds,
            outcome,
            repair_count,
        )

        records.append(
            {
                "project_dir": str(project_dir),
                "session_id": session_id,
                "task_id": task_id,
                "repo_line": repo_line or "(none)",
                "injected_test_cmd": injected_test_cmd or "(none)",
                "verification_cmds": verification_cmds,
                "planned_test_cmd": verification_cmds[0] if verification_cmds else "(none)",
                "test_cmd_match": _test_cmd_in_verification(
                    injected_test_cmd or "", verification_cmds
                ) if injected_test_cmd else False,
                "outcome": outcome,
                "repair_count": repair_count,
                "classification": classification,
                "class_note": class_note,
            }
        )

    return records


def scan_workspace(workspace_root: Path) -> List[Dict[str, Any]]:
    """Scan all project workspace subdirectories."""
    all_records: List[Dict[str, Any]] = []
    for project_dir in sorted(workspace_root.iterdir()):
        if not project_dir.is_dir():
            continue
        if project_dir.name.startswith("."):
            continue
        # Check for task workspace subdirs (project workspaces have a .agent/events tree).
        records = scan_project_dir(project_dir)
        if records:
            all_records.extend(records)
        else:
            # Try one level deeper (task subfolders).
            for sub in sorted(project_dir.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    sub_records = scan_project_dir(sub)
                    all_records.extend(sub_records)
    return all_records


# ── Reporting ─────────────────────────────────────────────────────────────────


def _summarise(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(records)
    if total == 0:
        return {"total_tasks": 0}

    counts = {"USED": 0, "IGNORED": 0, "HARMFUL": 0, "SKIPPED": 0}
    outcomes = {"task_completed": 0, "task_failed": 0, "task_aborted": 0, "unknown": 0}
    matches = 0
    matchable = 0
    repair_total = 0

    for r in records:
        c = r["classification"]
        counts[c] = counts.get(c, 0) + 1
        o = r["outcome"]
        outcomes[o] = outcomes.get(o, 0) + 1
        repair_total += r["repair_count"]
        if r["injected_test_cmd"] != "(none)" and r["verification_cmds"]:
            matchable += 1
            if r["test_cmd_match"]:
                matches += 1

    agreement_pct = round(100 * matches / matchable, 1) if matchable else 0
    used_pct = round(100 * counts.get("USED", 0) / total, 1)

    return {
        "total_tasks": total,
        "classification_counts": counts,
        "outcome_counts": outcomes,
        "matchable_tasks": matchable,
        "test_cmd_match_count": matches,
        "test_cmd_agreement_pct": agreement_pct,
        "used_pct": used_pct,
        "total_repair_events": repair_total,
        "pass_criteria": {
            "zero_harmful": counts.get("HARMFUL", 0) == 0,
            "agreement_gte_80": agreement_pct >= 80,
            "used_gte_50pct": used_pct >= 50,
        },
    }


def _print_table(records: List[Dict[str, Any]]) -> None:
    cols = ["task_id", "repo_line", "injected_test_cmd", "planned_test_cmd",
            "test_cmd_match", "outcome", "classification"]
    header = " | ".join(f"{c:<22}" for c in cols)
    print("\n" + "=" * len(header))
    print(header)
    print("-" * len(header))
    for r in records:
        row = " | ".join([
            f"{r['task_id']:<22}",
            f"{str(r['repo_line'])[:22]:<22}",
            f"{str(r['injected_test_cmd'])[:22]:<22}",
            f"{str(r['planned_test_cmd'])[:22]:<22}",
            f"{'yes' if r['test_cmd_match'] else 'no':<22}",
            f"{r['outcome']:<22}",
            f"{r['classification']:<22}",
        ])
        print(row)
    print("=" * len(header) + "\n")


def _print_summary(summary: Dict[str, Any]) -> None:
    print("── Summary ──────────────────────────────────────")
    print(f"  Total tasks scanned : {summary['total_tasks']}")
    print(f"  USED                : {summary['classification_counts'].get('USED', 0)}")
    print(f"  IGNORED             : {summary['classification_counts'].get('IGNORED', 0)}")
    print(f"  HARMFUL             : {summary['classification_counts'].get('HARMFUL', 0)}")
    print(f"  SKIPPED             : {summary['classification_counts'].get('SKIPPED', 0)}")
    print(f"  test_cmd agreement  : {summary['test_cmd_agreement_pct']}%  "
          f"({summary['test_cmd_match_count']}/{summary['matchable_tasks']} matchable)")
    print(f"  used_pct            : {summary['used_pct']}%")
    print(f"  total repair events : {summary['total_repair_events']}")
    print()
    pc = summary.get("pass_criteria", {})
    print("── Pass criteria ────────────────────────────────")
    print(f"  zero_harmful        : {'PASS' if pc.get('zero_harmful') else 'FAIL'}")
    print(f"  agreement ≥ 80%     : {'PASS' if pc.get('agreement_gte_80') else 'FAIL'}")
    print(f"  used ≥ 50%          : {'PASS' if pc.get('used_gte_50pct') else 'FAIL'}")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────


def main(argv: List[str]) -> int:
    workspace_root_arg = argv[1] if len(argv) > 1 else None
    if workspace_root_arg:
        workspace_root = Path(workspace_root_arg)
    else:
        workspace_root = Path(__file__).resolve().parents[3]

    print(f"Scanning workspace: {workspace_root}")

    if not workspace_root.is_dir():
        print(f"ERROR: workspace_root not found: {workspace_root}", file=sys.stderr)
        return 1

    records = scan_workspace(workspace_root)

    if not records:
        print("No task evidence found. Run at least 10 tasks with "
              "REPO_MEMORY_INJECTION_ENABLED=True, then re-run this script.")
        return 0

    print(f"Found {len(records)} task evidence records.\n")
    _print_table(records)
    summary = _summarise(records)
    _print_summary(summary)

    # Write evidence JSON — always into docs/roadmap/reports/maintenance, never CWD.
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    project_root = Path(__file__).resolve().parents[2]
    out_path = (
        project_root
        / "docs" / "roadmap" / "reports" / "maintenance"
        / f"repo-memory-injection-evidence-{today}.json"
    )
    out_path.write_text(
        json.dumps({"summary": summary, "records": records}, indent=2),
        encoding="utf-8",
    )
    print(f"Evidence written to: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
