"""Read-only Phase 32C-1 planner-grounding replay.

This script makes four direct, provider-only planning calls in a disposable
cwd. It never creates Orchestrator records and never executes a returned plan.
The current repository is read only; replay evidence is written under the
phase32c1 evidence directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.validation.validator import ValidatorService


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = (
    ROOT / "docs/roadmap/reports/evidence/phase32c1-grounding-replay-20260803"
)
TIMEOUT_SECONDS = 300

ATTEMPTS = {
    "attempt1_projects_pagination": {
        "task_title": "Remove legacy skip/limit pagination from GET /projects",
        "task_description": (
            "In app/api/v1/endpoints/projects.py, remove the legacy skip/limit "
            "pagination mode from the GET /projects endpoint so the endpoint "
            "always returns the paginated Page[ProjectResponse] envelope.\n\n"
            "Required source change in app/api/v1/endpoints/projects.py "
            "(function get_projects):\n"
            "- Change `page: Optional[int] = None` to `page: int = 1`.\n"
            "- Delete the `skip: int = 0` parameter.\n"
            "- Delete the `limit: int = 100` parameter.\n"
            "- Delete the `if page is None:` legacy branch that returns a bare list.\n"
            "- Update the docstring to drop the legacy/paginated mode wording.\n"
            "- Keep the `page < 1` 422 validation, the per_page 1..200 bounds, "
            "the search filter, and the ordering logic exactly as they are.\n"
            "- Always run the existing paginate(...) response path.\n\n"
            "Required test change in app/tests/test_pagination_infrastructure.py: "
            "update only the four GET /projects legacy assertions so they assert "
            "the Page envelope, prove omitted page behaves as page=1, and keep "
            "search working through the envelope. Do not delete pagination coverage.\n\n"
            "Hard scope: ONLY app/api/v1/endpoints/projects.py and "
            "app/tests/test_pagination_infrastructure.py may change. Do not "
            "modify app/api/v1/endpoints/tasks.py or app/api/v1/endpoints/sessions.py.\n\n"
            "Verify with: PYTHONPATH=. venv/bin/python -m pytest "
            "app/tests/test_pagination_infrastructure.py -q. All 98 tests must pass."
        ),
        "expected_files": [
            "app/api/v1/endpoints/projects.py",
            "app/tests/test_pagination_infrastructure.py",
        ],
        "source_files": [
            "app/api/v1/endpoints/projects.py",
            "app/tests/test_pagination_infrastructure.py",
        ],
    },
    "attempt4_utc_now": {
        "task_title": "Add a shared timezone-aware utc_now() helper and migrate context_service exported_at",
        "task_description": (
            "Add a shared timezone-aware `utc_now()` helper in app/time_utils.py "
            "that returns `datetime.now(timezone.utc)`. Update "
            "app/services/workspace/context_service.py so its exported_at value "
            "uses this helper instead of `datetime.utcnow()`, and remove the "
            "now-unused direct `datetime` import. Add focused regression coverage "
            "in app/tests/test_utc_now_helper.py. Run the focused context tests "
            "and the new helper tests. Do not migrate other files or change "
            "unrelated timestamp behavior."
        ),
        "expected_files": [
            "app/time_utils.py",
            "app/services/workspace/context_service.py",
            "app/tests/test_utc_now_helper.py",
        ],
        "source_files": ["app/services/workspace/context_service.py"],
    },
}


def _project_context(contract: dict[str, Any]) -> str:
    expected = ", ".join(contract["expected_files"])
    return (
        "Workspace already contains implementation files.\n"
        f"Expected task files: {expected}.\n"
        "Use only the existing workspace and preserve the task's hard scope.\n"
        "The source bodies of expected files are intentionally absent from this "
        "replay variant unless the replay adds the source-materialization block."
    )


def _source_materialization(contract: dict[str, Any]) -> str:
    blocks = [
        "## CURRENT SOURCE MATERIALIZATION",
        "The following is exact current source read before planning. It is evidence, not a future plan step.",
    ]
    for relative in contract["source_files"]:
        path = ROOT / relative
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            blocks.extend(
                [
                    f"### {relative}",
                    f"sha256: {hashlib.sha256(content.encode('utf-8')).hexdigest()}",
                    content,
                ]
            )
        else:
            blocks.extend([f"### {relative}", "ABSENT (new file)"])
    return "\n".join(blocks)


def build_replay_prompt(contract: dict[str, Any], *, include_source: bool) -> str:
    project_dir = Path(tempfile.gettempdir()) / "phase32c1-replay-workspace"
    prompt = PlannerService.build_minimal_planning_prompt(
        task_description=contract["task_description"],
        project_dir=project_dir,
        workflow_profile="default",
        workflow_phases=[],
        workspace_has_existing_files=True,
        project_context=_project_context(contract),
        project_structure_capsule=(
            "Existing target paths: " + ", ".join(contract["expected_files"])
        ),
        prompt_profile="default",
    )
    if include_source:
        prompt += "\n\n" + _source_materialization(contract)
    return prompt


def _extract_model_text(raw: str) -> str:
    match = re.search(r'"finalAssistantVisibleText"\s*:\s*"((?:\\.|[^"\\])*)"', raw)
    if match:
        try:
            return json.loads('"' + match.group(1) + '"')
        except json.JSONDecodeError:
            pass
    for start in (index for index, value in enumerate(raw) if value == "["):
        try:
            candidate, _ = json.JSONDecoder().raw_decode(raw[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, list):
            return json.dumps(candidate)
    return ""


def _parse_plan(model_text: str) -> list[dict[str, Any]] | None:
    try:
        parsed = json.loads(model_text)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or not all(
        isinstance(item, dict) for item in parsed
    ):
        return None
    return parsed


def _plan_ops(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for step in plan:
        for operation in step.get("ops") or []:
            if isinstance(operation, dict):
                operations.append(operation)
    return operations


def _score(
    plan: list[dict[str, Any]] | None,
    contract: dict[str, Any],
) -> dict[str, Any]:
    expected = set(contract["expected_files"])
    if plan is None:
        return {
            "response_parseable": False,
            "plan_runnable": False,
            "source_grounded": False,
            "old_text_exists": False,
            "expected_files_materialized": False,
            "scope_valid": False,
            "validator_passed": False,
            "repair_required": True,
            "validator_reasons": ["response was not a JSON array of step objects"],
        }

    operations = _plan_ops(plan)
    immediate = PlannerService.find_immediate_repair_step_issues(plan, ROOT)
    validator = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=contract["task_description"],
        execution_profile="full_lifecycle",
        project_dir=ROOT,
        title=contract["task_title"],
        description=contract["task_description"],
    )
    old_checks: list[bool] = []
    scope_checks: list[bool] = []
    materialized: set[str] = set()
    for operation in operations:
        op_name = str(operation.get("op") or operation.get("o") or "")
        path = str(operation.get("path") or "").lstrip("./")
        scope_checks.append(path in expected)
        if op_name == "replace_in_file":
            target = ROOT / path
            old = operation.get("old", operation.get("old_text"))
            exists = (
                target.is_file()
                and isinstance(old, str)
                and old in target.read_text(encoding="utf-8", errors="replace")
            )
            old_checks.append(exists)
        if op_name in {"write_file", "append_file", "replace_in_file"}:
            materialized.add(path)

    expected_materialized = all(
        (ROOT / path).is_file() or path in materialized for path in expected
    )
    source_grounded = (
        all(old_checks)
        if old_checks
        else bool(
            not any(
                str(op.get("op") or op.get("o") or "") == "replace_in_file"
                for op in operations
            )
        )
    )
    runnable = not any(
        immediate.get(key) for key in ("non_runnable_steps", "background_process_steps")
    )
    return {
        "response_parseable": True,
        "plan_runnable": runnable,
        "source_grounded": source_grounded,
        "old_text_exists": bool(old_checks) and all(old_checks),
        "expected_files_materialized": expected_materialized,
        "scope_valid": all(scope_checks) if scope_checks else True,
        "validator_passed": bool(validator.accepted),
        "repair_required": not (
            runnable
            and source_grounded
            and expected_materialized
            and validator.accepted
        ),
        "validator_reasons": list(validator.reasons or [])[:8],
        "immediate_repair_issues": immediate,
        "plan": plan,
    }


def run_one(
    label: str, contract: dict[str, Any], *, include_source: bool
) -> dict[str, Any]:
    prompt = build_replay_prompt(contract, include_source=include_source)
    temp_dir = Path(tempfile.mkdtemp(prefix="phase32c1-provider-"))
    session_id = f"phase32c1-{label}-{int(time.time())}"
    command = [
        "openclaw",
        "agent",
        "--agent",
        "orchestrator",
        "--local",
        "--session-id",
        session_id,
        "--message",
        prompt,
        "--json",
        "--timeout",
        str(TIMEOUT_SECONDS),
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=temp_dir,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS + 30,
            check=False,
            env=os.environ.copy(),
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        raw = f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
        model_text = _extract_model_text(raw)
        plan = _parse_plan(model_text)
        result = {
            "label": label,
            "include_source": include_source,
            "provider": "local_openclaw",
            "model": "qwen3.6:27B (configured agent)",
            "timeout_seconds": TIMEOUT_SECONDS,
            "return_code": completed.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "prompt_chars": len(prompt),
            "prompt_estimated_tokens": round(len(prompt) / 4),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "model_text_chars": len(model_text),
            "score": _score(plan, contract),
        }
        (EVIDENCE_DIR / f"{label}.prompt.txt").write_text(prompt, encoding="utf-8")
        (EVIDENCE_DIR / f"{label}.raw.txt").write_text(raw, encoding="utf-8")
        (EVIDENCE_DIR / f"{label}.result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return result
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        raw = (
            f"--- stdout before timeout ---\n{stdout}\n"
            f"--- stderr before timeout ---\n{stderr}\n"
            f"--- harness timeout ---\nsubprocess exceeded {TIMEOUT_SECONDS + 30} seconds"
        )
        result = {
            "label": label,
            "include_source": include_source,
            "provider": "local_openclaw",
            "model": "qwen3.6:27B (configured agent)",
            "timeout_seconds": TIMEOUT_SECONDS,
            "harness_timeout_seconds": TIMEOUT_SECONDS + 30,
            "return_code": None,
            "timed_out": True,
            "duration_seconds": round(time.monotonic() - started, 3),
            "prompt_chars": len(prompt),
            "prompt_estimated_tokens": round(len(prompt) / 4),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "model_text_chars": 0,
            "score": _score(None, contract),
        }
        (EVIDENCE_DIR / f"{label}.prompt.txt").write_text(prompt, encoding="utf-8")
        (EVIDENCE_DIR / f"{label}.raw.txt").write_text(raw, encoding="utf-8")
        (EVIDENCE_DIR / f"{label}.result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return result
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for label, contract in ATTEMPTS.items():
        results.append(run_one(label + "_original", contract, include_source=False))
        results.append(run_one(label + "_with_source", contract, include_source=True))
    (EVIDENCE_DIR / "matrix.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(results, indent=2, sort_keys=True))


def materialize_timeout_artifact() -> None:
    """Persist the already-observed fourth-call timeout without another call."""

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    label = "attempt4_utc_now_with_source"
    contract = ATTEMPTS["attempt4_utc_now"]
    prompt = build_replay_prompt(contract, include_source=True)
    raw = (
        "--- stdout before timeout ---\n"
        "(not captured by the completed harness traceback)\n"
        "--- stderr before timeout ---\n"
        "(not captured by the completed harness traceback)\n"
        "--- harness timeout ---\n"
        "subprocess exceeded 330 seconds; no retry issued"
    )
    result = {
        "label": label,
        "include_source": True,
        "provider": "local_openclaw",
        "model": "qwen3.6:27B (configured agent)",
        "timeout_seconds": TIMEOUT_SECONDS,
        "harness_timeout_seconds": TIMEOUT_SECONDS + 30,
        "return_code": None,
        "timed_out": True,
        "prompt_chars": len(prompt),
        "prompt_estimated_tokens": round(len(prompt) / 4),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "model_text_chars": 0,
        "score": _score(None, contract),
    }
    (EVIDENCE_DIR / f"{label}.prompt.txt").write_text(prompt, encoding="utf-8")
    (EVIDENCE_DIR / f"{label}.raw.txt").write_text(raw, encoding="utf-8")
    (EVIDENCE_DIR / f"{label}.result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    results = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(EVIDENCE_DIR.glob("*.result.json"))
    ]
    (EVIDENCE_DIR / "matrix.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    if "--materialize-timeout-artifact" in sys.argv:
        materialize_timeout_artifact()
    else:
        main()
