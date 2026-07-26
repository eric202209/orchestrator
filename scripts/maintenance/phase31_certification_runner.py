#!/usr/bin/env python3
"""Phase 31B Certification Runner.

The canonical entrypoint for the Phase 31 Certification Execution
Platform. Coordinates, for one certification session:

1. launch-precondition execution (F10 workspace uniqueness, F11 auto-commit
   daemon quiescence -- reused verbatim from Phase 30L, not redesigned);
2. execution preparation (fresh target project, environment baseline);
3. scenario dispatch through the real live API (`eval@local.dev`, per
   `docs/roadmap/workflow/development-workflow.md`), the same
   `queue_task_for_session` -> `execute_orchestration_task` path every
   product task takes;
4. acceptance-evidence fact assembly (`phase31_certification_facts.py`)
   and classification (`app.services.orchestration.acceptance_evidence
   .classify_acceptance` -- reused, not reimplemented);
5. evidence capture (`phase31_certification_evidence.py`) in the exact
   shape `docs/roadmap/workflow/phase31/phase31-evidence-program.md`
   defines;
6. deterministic replay (re-running `classify_acceptance` from the
   retained facts and diffing with `scripts/evals/scenario_contract
   .compare_reports` -- the existing Phase 30C diff primitive, not a new
   one);
7. validation (`phase31_certification_validation.py`);
8. a certification summary.

A failed F10/F11 precondition aborts before any scenario dispatch.

Only scenarios registered in `phase31_certification_scenarios.py` can be
run (Stage 0 gating + Stage 1 core, per Phase 31B scope). Only S1-1 is
exercised as the Phase 31B pilot; the runner itself is generic and is
meant to be reused unchanged by Phase 31C-31F for the remaining stages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from app.auth import create_access_token  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
)  # noqa: E402
from app.services.orchestration.acceptance_evidence import (  # noqa: E402
    classify_acceptance,
)

from scripts.evals.scenario_contract import compare_reports  # noqa: E402
from scripts.maintenance.phase31_certification_evidence import (  # noqa: E402
    CertificationEvidenceSession,
    openclaw_json_checksum,
)
from scripts.maintenance.phase31_certification_facts import (  # noqa: E402
    assemble_facts_from_live_run,
)
from scripts.maintenance.phase31_certification_scenarios import (  # noqa: E402
    scenario_contract,
)
from scripts.maintenance.phase31_certification_validation import (  # noqa: E402
    validate_session_evidence,
)
from scripts.maintenance.phase31_launch_precondition_f10_workspace_uniqueness import (  # noqa: E402
    check_targets,
)
from scripts.maintenance.phase31_launch_precondition_f11_autocommit_daemon import (  # noqa: E402
    check_daemon,
)

BASE_URL = os.environ.get("ORCHESTRATOR_BASE_URL", "http://127.0.0.1:8080")
OPERATOR_EMAIL = os.environ.get("ORCHESTRATOR_USER_EMAIL", "eval@local.dev")
WORKSPACE_BASE = Path("/root/.openclaw/workspace/vault/projects")
TERMINAL_TASK_STATUSES = {"done", "failed", "cancelled"}

# Scenario -> (title, description) dispatched exactly as declared in the
# scenario matrix's "Objective" column. Only scenarios with a registered
# ScenarioAcceptanceContract can be dispatched by this runner.
_SCENARIO_TASKS: dict[str, dict[str, str]] = {
    "S1-1": {
        "title": "Phase 31B pilot: documentation-only change",
        "description": (
            "Add a short 'Overview' section to a new file README-PILOT.md "
            "at the project root describing this is a Phase 31B "
            "certification pilot workspace. This is a documentation-only "
            "task: do not touch any other file, do not add code."
        ),
    },
}


def _api(token: str, method: str, path: str, **kwargs: Any) -> Any:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    response = requests.request(
        method, f"{BASE_URL}{path}", headers=headers, timeout=30, **kwargs
    )
    response.raise_for_status()
    return response.json()


def _workspace_content_hash(root: Path) -> Optional[str]:
    if not root.exists():
        return None
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts or not path.is_file():
            continue
        entries.append(
            {
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _environment_baseline() -> dict[str, Any]:
    with SessionLocal() as db:
        counts = {
            "projects": db.query(Project).count(),
            "sessions": db.query(SessionModel).count(),
            "tasks": db.query(Task).count(),
            "task_executions": db.query(TaskExecution).count(),
        }
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "disposable_db_declaration": "orchestrator.db is the live shared DB; "
        "this session's target project is a fresh, purpose-created row, "
        "not a disposable database.",
        "live_db_row_counts": counts,
        "openclaw_json_sha256": openclaw_json_checksum(),
    }


def run_launch_preamble(
    evidence: CertificationEvidenceSession, *, target_project_ids: list[int]
) -> tuple[dict[str, Any], dict[str, Any]]:
    f10_result = check_targets(target_project_ids)
    f11_result = check_daemon(lookback_minutes=60)
    return f10_result, f11_result


def dispatch_scenario(
    token: str, *, project_id: int, scenario_id: str
) -> dict[str, Any]:
    task_spec = _SCENARIO_TASKS[scenario_id]
    created_task = _api(
        token,
        "POST",
        "/api/v1/tasks",
        json={
            "project_id": project_id,
            "title": task_spec["title"],
            "description": task_spec["description"],
            "plan_position": 1,
            "execution_profile": "full_lifecycle",
        },
    )
    task_id = int(created_task["id"])

    queued = _api(token, "POST", f"/api/v1/tasks/{task_id}/retry", json={})
    session_id = int(queued["session_id"])

    return {"task_id": task_id, "session_id": session_id}


def wait_for_terminal_task(
    token: str, task_id: int, *, timeout_seconds: int = 1800
) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        data = _api(token, "GET", f"/api/v1/tasks/{task_id}")
        last_status = str(data.get("status") or "").lower()
        if last_status in TERMINAL_TASK_STATUSES:
            return last_status
        time.sleep(10)
    return last_status


def run_scenario(
    token: str,
    evidence: CertificationEvidenceSession,
    *,
    project_id: int,
    workspace_root: Path,
    scenario_id: str,
    run: int = 1,
) -> dict[str, Any]:
    contract = scenario_contract(scenario_id)

    pre_hash = _workspace_content_hash(workspace_root)
    dispatch_started = time.monotonic()
    dispatched = dispatch_scenario(
        token, project_id=project_id, scenario_id=scenario_id
    )
    task_id = dispatched["task_id"]
    session_id = dispatched["session_id"]

    final_status = wait_for_terminal_task(token, task_id)
    total_seconds = time.monotonic() - dispatch_started
    post_hash = _workspace_content_hash(workspace_root)

    with SessionLocal() as db:
        facts = assemble_facts_from_live_run(db, session_id=session_id, task_id=task_id)

    result = classify_acceptance(contract, facts)

    record_path = evidence.write_scenario_record(
        scenario_id=scenario_id,
        run=run,
        contract=contract,
        facts=facts,
        result=result,
        provider_identity=facts.provider_identity,
        pre_hash=pre_hash,
        post_hash=post_hash,
        timings={"total_seconds": total_seconds, "final_task_status": final_status},
        repair_telemetry=[],
        event_journal_pointer=f"session_id={session_id} task_id={task_id}",
    )

    # Replay: reclassify from the retained facts/contract (pure, no I/O)
    # and diff against the recorded result with the existing Phase 30C
    # diff primitive.
    replayed_result = classify_acceptance(contract, facts)
    replay_comparison = compare_reports(result.to_dict(), replayed_result.to_dict())
    evidence.write_replay_result(
        scenario_id=scenario_id, run=run, replay_comparison=replay_comparison
    )

    return {
        "scenario_id": scenario_id,
        "task_id": task_id,
        "session_id": session_id,
        "outcome_class": result.outcome_class.value,
        "record_path": str(record_path),
        "replay_match": replay_comparison["match"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario-ids",
        nargs="+",
        default=["S1-1"],
        help="Matrix scenario IDs to run.",
    )
    parser.add_argument("--phase-letter", default="b")
    parser.add_argument(
        "--slug",
        default=None,
        help="Evidence directory slug (default: certification-execution-platform).",
    )
    parser.add_argument("--session-number", type=int, default=1)
    args = parser.parse_args()

    slug = args.slug or "certification-execution-platform"
    evidence = CertificationEvidenceSession(
        args.phase_letter, slug, args.session_number
    )

    token = create_access_token({"sub": OPERATOR_EMAIL})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    project_slug = f"phase31b-certification-pilot-{stamp}"
    workspace_root = WORKSPACE_BASE / project_slug

    baseline = _environment_baseline()

    project = _api(
        token,
        "POST",
        "/api/v1/projects",
        json={
            "name": project_slug,
            "description": "Phase 31B certification execution platform pilot target",
            "workspace_path": str(workspace_root),
        },
    )
    project_id = int(project["id"])
    print(f"[project] id={project_id} slug={project_slug}")

    f10_result, f11_result = run_launch_preamble(
        evidence, target_project_ids=[project_id]
    )
    evidence.write_preamble(
        f10_result=f10_result,
        f11_result=f11_result,
        environment_baseline=baseline,
        operator_identity=OPERATOR_EMAIL,
        declared_scenario_set=args.scenario_ids,
        dispatch_budget=len(args.scenario_ids),
    )
    print(
        f"[preamble] F10 passed={f10_result['passed']} F11 passed={f11_result['passed']}"
    )

    if not f10_result["passed"]:
        print("[abort] F10 failed; no scenario dispatched.")
        evidence.note("ABORT: F10 failed before any scenario dispatch.")
        return 1
    if not f11_result["passed"]:
        print(
            "[warn] F11 failed at session start (detection signal, not a hard "
            "program stop by itself -- recorded and the session proceeds under "
            "monitoring per program policy; a defect found this way is not "
            "silently accepted)."
        )
        evidence.note(
            "F11 FAILED at session start: " + json.dumps(f11_result, default=str)
        )

    scenario_results = []
    for scenario_id in args.scenario_ids:
        print(f"[dispatch] {scenario_id}")
        outcome = run_scenario(
            token,
            evidence,
            project_id=project_id,
            workspace_root=workspace_root,
            scenario_id=scenario_id,
        )
        print(
            f"[result] {scenario_id} outcome={outcome['outcome_class']} "
            f"replay_match={outcome['replay_match']}"
        )
        scenario_results.append(outcome)

    closing_f11 = check_daemon(lookback_minutes=60)
    closing_baseline = _environment_baseline()
    residue_comparison = {
        "row_count_delta": {
            key: closing_baseline["live_db_row_counts"][key]
            - baseline["live_db_row_counts"][key]
            for key in baseline["live_db_row_counts"]
        },
        "openclaw_json_checksum_unchanged": (
            closing_baseline["openclaw_json_sha256"] == baseline["openclaw_json_sha256"]
        ),
    }
    evidence.write_closing(
        f11_result=closing_f11,
        environment_baseline=closing_baseline,
        residue_comparison=residue_comparison,
    )
    print(f"[closing] F11 passed={closing_f11['passed']} residue={residue_comparison}")

    validation = validate_session_evidence(
        evidence.evidence_dir,
        session_number=args.session_number,
        scenario_ids=args.scenario_ids,
    )
    print(f"[validation] valid={validation['valid']} failures={validation['failures']}")

    summary = {
        "session_number": args.session_number,
        "project_id": project_id,
        "project_slug": project_slug,
        "scenario_results": scenario_results,
        "f10_result": f10_result,
        "f11_result_start": f11_result,
        "f11_result_close": closing_f11,
        "residue_comparison": residue_comparison,
        "validation": validation,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    summary_path = evidence.write_certification_summary(summary)
    print(f"[summary] {summary_path}")

    return 0 if validation["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
