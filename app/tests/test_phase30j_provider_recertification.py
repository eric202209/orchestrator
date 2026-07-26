"""Phase 30J bounded real-provider re-certification.

Reuses the Phase 30H/30I harness plumbing (tree hashing, log-row parsing,
runtime probes) but fixes the metrics aggregation defect Phase 30I exposed:
this file's `_scenario_metrics` derives `target_violation_resolved` /
`same_violation_repeated` from the new authoritative
`[OPENCLAW][PLANNING_REPAIR_OUTCOME_FINAL]` event
(`compute_final_repair_outcome`, added in Phase 30J) instead of an `any()`
over per-attempt events plus the last event's post-repair codes.

`PROVIDER_MODEL`/`PROVIDER_BACKEND` are captured live from the real
`openclaw.json` this run actually dispatches through, not hardcoded: this
environment's configured default agent model is a Qwen model served via an
OpenAI-compatible gateway (`ai-gateway:8000`, model id `qwen-local`), not the
`qwen3-coder:30b` via a local Ollama daemon that earlier Phase 30 briefs
assumed. Recording the real identity here keeps the evidence honest; it does
not change the model-agnostic package/root and repair-outcome semantics this
phase certifies.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from app.config import settings
from app.models import Task, TaskExecution
from app.services.orchestration.planning.workspace_identity import (
    PlannerWorkspaceIdentity,
    render_planner_workspace_identity,
)
from app.tests.test_phase30h_workspace_identity_recertification import (
    _hash_tree,
    _log_rows,
    _sha256_file,
)

pytest_plugins = ("app.tests.test_phase30e_provider_certification",)

pytestmark = pytest.mark.live

EVIDENCE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "roadmap"
    / "reports"
    / "evidence"
    / "phase30j-package-root-intent"
)
REAL_OPENCLAW_CONFIG = Path("/root/.openclaw/openclaw.json")
TARGET_CODE = "nested_project_folder_command"


def _gateway_version() -> str | None:
    executable = shutil.which("openclaw")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=False
    )
    return (result.stdout or result.stderr).strip() or None


def _real_provider_identity() -> dict[str, str | None]:
    """Read the *actual* configured backend/model from the real config.

    Deliberately not hardcoded: earlier Phase 30 harnesses assumed
    `qwen3-coder:30b` via a local Ollama daemon; this environment's real
    `openclaw.json` configures a different default agent model.
    """

    backend = settings.AGENT_BACKEND or "unknown"
    model_id = None
    try:
        config = json.loads(REAL_OPENCLAW_CONFIG.read_text(encoding="utf-8"))
        model_id = ((config.get("agents") or {}).get("defaults") or {}).get("model")
    except Exception:  # noqa: BLE001
        model_id = None
    return {"provider_backend": backend, "provider_model": model_id or "unknown"}


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    category: str
    project_name: str
    title: str
    description: str
    seed_repo: Callable[[Path], None] | None = None
    timeout_seconds: int = 300
    legitimate_alias_dirs: tuple[str, ...] = ()


def _seed_existing_package(root: Path) -> None:
    package = root / "inventory_api_i2"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("\n", encoding="utf-8")
    (package / "service.py").write_text(
        "def list_items():\n    return []\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'inventory-api-i2'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_service.py").write_text(
        "from inventory_api_i2.service import list_items\n\n"
        "def test_list_items():\n    assert list_items() == []\n",
        encoding="utf-8",
    )


def _seed_numeric_repository(root: Path) -> None:
    (root / "migrations" / "001").mkdir(parents=True, exist_ok=True)
    (root / "fixtures" / "2026").mkdir(parents=True, exist_ok=True)
    (root / "migrations" / "001" / "README.md").write_text(
        "Initial migration\n", encoding="utf-8"
    )
    (root / "fixtures" / "2026" / "sample.json").write_text("{}\n", encoding="utf-8")


SCENARIOS = (
    Scenario(
        "phase30j-j1-exact-i1-regression-shape",
        "J1",
        "inventory_api_i1",
        "Create a small Python API package",
        "Create a small Python API package named inventory_api_i1 with an "
        "__init__.py, a routes module, and a service module. Add a tests "
        "directory and a minimal pyproject.toml at the project root, then "
        "run a bounded test.",
        legitimate_alias_dirs=("inventory_api_i1",),
    ),
    Scenario(
        "phase30j-j2-existing-same-name-package",
        "J2",
        "inventory_api_i2",
        "Add a lookup endpoint to the existing inventory package",
        "Extend the existing inventory_api_i2 package with a lookup "
        "function and a matching test. Preserve the existing pyproject.toml, "
        "tests directory, and inventory_api_i2 package layout.",
        seed_repo=_seed_existing_package,
        legitimate_alias_dirs=("inventory_api_i2",),
    ),
    Scenario(
        "phase30j-j3-new-backend-scaffold",
        "J3",
        "ledger_service_j3",
        "Build a small Python backend service from scratch",
        "Build a small Python backend service named ledger_service_j3 with "
        "an application entry point, one route, a service layer, and a "
        "test. Include minimal project metadata and run the test suite.",
    ),
    Scenario(
        "phase30j-j4-new-frontend-scaffold",
        "J4",
        "ledger_dashboard_j4",
        "Create a small React ledger dashboard",
        "Create a small React and TypeScript ledger dashboard with a list "
        "view, one reusable component, and a focused test or type-check.",
        timeout_seconds=360,
    ),
    Scenario(
        "phase30j-j5-numeric-child-directory",
        "J5",
        "migration_tool_j5",
        "Add migration tooling to the existing repository",
        "Add a migration command and fixtures to this repository. Preserve "
        "the existing migrations/001 and fixtures/2026 directories, add the "
        "next necessary files, and run focused verification.",
        seed_repo=_seed_numeric_repository,
        legitimate_alias_dirs=("001", "2026"),
    ),
    Scenario(
        "phase30j-j6-standalone-installable-package",
        "J6",
        "billing_tool_j6",
        "Set up billing_tool_j6 as its own installable Python package",
        "Set up billing_tool_j6 as a standalone, independently installable "
        "Python command-line package complete with its own packaging "
        "metadata, a source module, and a test, so it can be published on "
        "its own separate from the rest of this repository.",
        # A same-name package directory is the expected, legitimate
        # materialization here (that is the scenario under test) -- not
        # evidence of a duplicated project root.
        legitimate_alias_dirs=("billing_tool_j6",),
    ),
)


def _install_runtime_probes(harness, monkeypatch, scenario: Scenario):
    import app.tasks.worker as worker_module

    runtime_root = harness.project_workspace_dir.parent / "phase30j-runtime"
    monkeypatch.setattr(settings, "RUNTIME_ROOT", str(runtime_root))
    probe: dict[str, Any] = {
        "runtime_path": None,
        "runtime_tree_hash_before": None,
        "runtime_tree_hash_after": None,
        "nested_workspace_child_dirs": [],
    }
    allocate = worker_module._maybe_allocate_runtime_workspace
    dispose = worker_module._dispose_runtime_workspace_safely

    def allocate_with_hash(**kwargs):
        sandbox = allocate(**kwargs)
        if sandbox is not None:
            probe["runtime_path"] = str(sandbox.path.resolve())
            probe["runtime_tree_hash_before"] = _hash_tree(sandbox.path)
        return sandbox

    def dispose_with_hash(sandbox, **kwargs):
        if sandbox is not None and probe["runtime_tree_hash_after"] is None:
            probe["runtime_tree_hash_after"] = _hash_tree(sandbox.path)
            entries = sorted(sandbox.path.iterdir(), key=lambda item: item.name)
            aliases = {
                scenario.project_name,
                harness.project_workspace_dir.name,
                Path(sandbox.path).name,
            }
            probe["nested_workspace_child_dirs"] = [
                item.name for item in entries if item.is_dir() and item.name in aliases
            ]
        return dispose(sandbox, **kwargs)

    monkeypatch.setattr(
        worker_module, "_maybe_allocate_runtime_workspace", allocate_with_hash
    )
    monkeypatch.setattr(
        worker_module, "_dispose_runtime_workspace_safely", dispose_with_hash
    )
    return probe, runtime_root


def _scenario_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    diagnostics = [row for row in rows if "PLANNING_DIAGNOSTICS" in row["message"]]
    attempt_outcomes = [
        row for row in rows if "PLANNING_REPAIR_OUTCOME]" in row["message"]
    ]
    final_outcomes = [
        row for row in rows if "PLANNING_REPAIR_OUTCOME_FINAL" in row["message"]
    ]
    planning_errors = [
        row
        for row in rows
        if "planning" in row["message"].lower()
        and row["level"] in {"ERROR", "CRITICAL"}
    ]
    first_codes = (
        list(diagnostics[0]["meta"].get("semantic_violation_codes") or [])
        if diagnostics
        else []
    )
    target_attempts = [
        row
        for row in attempt_outcomes
        if row["meta"].get("targeted_violation_code") == TARGET_CODE
    ]
    identity_metadata = next(
        (
            {
                key: row["meta"].get(key)
                for key in (
                    "physical_runtime_basename",
                    "logical_project_name",
                    "display_project_path",
                    "offending_root_alias",
                    "offending_fragments",
                    "corrected_fragments",
                    "violation_kind",
                )
                if row["meta"].get(key) is not None
            }
            for row in rows
            if row["meta"].get("physical_runtime_basename")
            or row["meta"].get("logical_project_name")
        ),
        {},
    )
    # Authoritative: the final aggregate event (Phase 30J), not an any() over
    # per-attempt resolved flags plus the last attempt's own codes.
    final_target_entry = None
    if final_outcomes:
        last_final = final_outcomes[-1]["meta"]
        final_target_entry = (last_final.get("target_outcomes") or {}).get(TARGET_CODE)
    target_violation_resolved = (
        final_target_entry.get("target_final_status") == "RESOLVED"
        if final_target_entry
        else None
    )
    same_violation_repeated = (
        final_target_entry.get("target_final_status")
        in {"REPEATED_AND_EXHAUSTED", "OUTCOME_INCONSISTENT"}
        if final_target_entry
        else None
    )
    repair_outcome_consistent = (
        final_target_entry.get("repair_outcome_consistent")
        if final_target_entry
        else True
    )
    post_repair_codes = (
        list(final_outcomes[-1]["meta"].get("final_violation_codes") or [])
        if final_outcomes
        else []
    )
    return {
        "first_pass_valid": not diagnostics or not first_codes,
        "first_pass_violation_codes": first_codes,
        "identity_metadata": identity_metadata,
        "repair_attempts": len(target_attempts),
        "post_repair_violation_codes": post_repair_codes,
        "target_violation_resolved": target_violation_resolved,
        "same_violation_repeated": same_violation_repeated,
        "repair_outcome_consistent": repair_outcome_consistent,
        "final_plan_valid": not planning_errors,
        "manual_intervention": any(
            "intervention" in row["message"].lower() for row in rows
        ),
        "held_for_review": any(
            row["meta"].get("held_for_review") is True
            or row["meta"].get("evaluator_verdict") == "NEEDS_REVIEW"
            for row in rows
        ),
        "baseline_published": any(
            row["meta"].get("baseline_published") is True
            or "published baseline" in row["message"].lower()
            for row in rows
        ),
    }


def _task_description_hash(scenario: Scenario) -> str:
    import hashlib

    payload = f"{scenario.title}\n{scenario.description}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _run_scenario(harness, monkeypatch, scenario: Scenario) -> dict[str, Any]:
    if scenario.seed_repo is not None:
        scenario.seed_repo(harness.project_workspace_dir)
    else:
        harness.project_workspace_dir.mkdir(parents=True, exist_ok=True)
    project = harness.make_project(scenario.project_name)
    session = harness.make_session(project, f"{scenario.scenario_id}-session")
    task = harness.make_task(
        project, title=scenario.title, description=scenario.description
    )
    probe, runtime_root = _install_runtime_probes(harness, monkeypatch, scenario)
    identity_provider = _real_provider_identity()
    config_before = _sha256_file(REAL_OPENCLAW_CONFIG)
    project_before = _hash_tree(harness.project_workspace_dir)
    outside_root = harness.project_workspace_dir.parent
    outside_before = _hash_tree(
        outside_root, excluded={harness.project_workspace_dir, runtime_root}
    )
    identity = PlannerWorkspaceIdentity.from_paths(
        project_workspace=harness.project_workspace_dir,
        physical_runtime_root=runtime_root / "<task-execution-id>",
        logical_project_name=scenario.project_name,
    )
    started = time.monotonic()
    dispatch_error = None
    try:
        harness.dispatch(session, task.id, timeout_seconds=scenario.timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - preserve real provider failures
        dispatch_error = f"{type(exc).__name__}: {exc}"
    wall_seconds = time.monotonic() - started
    harness.db.refresh(task)
    harness.db.refresh(session)
    execution = (
        harness.db.query(TaskExecution)
        .filter(TaskExecution.task_id == task.id)
        .order_by(TaskExecution.id.desc())
        .first()
    )
    execution_count = (
        harness.db.query(TaskExecution).filter(TaskExecution.task_id == task.id).count()
    )
    task_count = harness.db.query(Task).filter(Task.project_id == project.id).count()
    project_after = _hash_tree(harness.project_workspace_dir)
    config_after = _sha256_file(REAL_OPENCLAW_CONFIG)
    outside_after = _hash_tree(
        outside_root, excluded={harness.project_workspace_dir, runtime_root}
    )
    rows = _log_rows(harness.db, task.id)
    metrics = _scenario_metrics(rows)
    task_status = task.status.value if hasattr(task.status, "value") else task.status
    evaluator_verdict = next(
        (
            row["meta"].get("evaluator_verdict")
            for row in rows
            if row["meta"].get("evaluator_verdict")
        ),
        None,
    )
    nested_dirs = probe["nested_workspace_child_dirs"]
    unexpected_nested_dirs = [
        name for name in nested_dirs if name not in set(scenario.legitimate_alias_dirs)
    ]
    nested_workspace_created = bool(unexpected_nested_dirs)
    legitimate_false_rejection = scenario.category in {"J1", "J2", "J5"} and (
        TARGET_CODE in metrics["first_pass_violation_codes"]
    )
    numeric_child_rejection = scenario.category == "J5" and (
        TARGET_CODE in metrics["first_pass_violation_codes"]
    )
    record = {
        "scenario_id": scenario.scenario_id,
        "scenario_category": scenario.category,
        "task_description_hash": _task_description_hash(scenario),
        "project_name": scenario.project_name,
        "project_workspace_realpath": str(harness.project_workspace_dir.resolve()),
        "logical_project_name": project.name,
        "physical_runtime_realpath": probe["runtime_path"],
        "physical_runtime_basename": (
            Path(probe["runtime_path"]).name if probe["runtime_path"] else None
        ),
        "planner_identity_evidence": render_planner_workspace_identity(identity),
        "provider_backend": identity_provider["provider_backend"],
        "provider_model": identity_provider["provider_model"],
        "provider_model_note": (
            "Captured live from openclaw.json agents.defaults.model; earlier "
            "Phase 30 briefs assumed qwen3-coder:30b via a local Ollama "
            "daemon, which this environment does not run."
        ),
        "gateway_version": _gateway_version(),
        "first_pass_valid": metrics["first_pass_valid"],
        "first_pass_violation_codes": metrics["first_pass_violation_codes"],
        "target_violation_present": TARGET_CODE in metrics["first_pass_violation_codes"]
        or bool(metrics["repair_attempts"]),
        "identity_metadata": metrics["identity_metadata"],
        "repair_attempt_count": metrics["repair_attempts"],
        "post_repair_violation_codes": metrics["post_repair_violation_codes"],
        "target_violation_resolved": metrics["target_violation_resolved"],
        "same_violation_repeated": metrics["same_violation_repeated"],
        "repair_outcome_consistent": metrics["repair_outcome_consistent"],
        "final_plan_valid": metrics["final_plan_valid"],
        "task_started": execution is not None,
        "task_terminal_state": task_status,
        "evaluator_verdict": evaluator_verdict,
        "held_for_review": metrics["held_for_review"],
        "baseline_published": metrics["baseline_published"],
        "duplicate_task_count": max(0, task_count - 1),
        "duplicate_execution_count": max(0, execution_count - 1),
        "openclaw_config_sha256_before": config_before,
        "openclaw_config_sha256_after": config_after,
        "openclaw_config_unchanged": config_before == config_after,
        "validator_bypass": nested_workspace_created,
        "nested_workspace_created": nested_workspace_created,
        "nested_workspace_child_dirs": nested_dirs,
        "legitimate_path_false_rejection": legitimate_false_rejection,
        "ordinary_numeric_child_rejection": numeric_child_rejection,
        "outside_workspace_diff": outside_before != outside_after,
        "project_tree_hash_before": project_before,
        "project_tree_hash_after": project_after,
        "runtime_tree_hash_before": probe["runtime_tree_hash_before"],
        "runtime_tree_hash_after": probe["runtime_tree_hash_after"],
        "outside_tree_hash_before": outside_before,
        "outside_tree_hash_after": outside_after,
        "wall_time_seconds": round(wall_seconds, 1),
        "manual_intervention": metrics["manual_intervention"],
        "dispatch_error": dispatch_error,
    }
    required = (
        "scenario_id",
        "project_workspace_realpath",
        "logical_project_name",
        "provider_backend",
        "provider_model",
        "openclaw_config_sha256_before",
        "openclaw_config_sha256_after",
        "project_tree_hash_before",
        "project_tree_hash_after",
        "outside_tree_hash_before",
        "outside_tree_hash_after",
    )
    record["evidence_valid"] = all(
        record.get(key) not in (None, "") for key in required
    )
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    EVIDENCE_ROOT.chmod(0o777)
    output = EVIDENCE_ROOT / f"{scenario.scenario_id}.json"
    output.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    output.chmod(0o666)
    return record


@pytest.mark.parametrize(
    "scenario", SCENARIOS, ids=[scenario.scenario_id for scenario in SCENARIOS]
)
def test_phase30j_natural_provider_scenario(real_pipeline, monkeypatch, scenario):
    record = _run_scenario(real_pipeline, monkeypatch, scenario)
    assert record["evidence_valid"], record
    assert record["validator_bypass"] is False, record
    assert record["nested_workspace_created"] is False, record
    assert record["legitimate_path_false_rejection"] is False, record
    assert record["ordinary_numeric_child_rejection"] is False, record
    assert record["outside_workspace_diff"] is False, record
    assert record["duplicate_task_count"] == 0, record
    assert record["duplicate_execution_count"] == 0, record
    assert record["manual_intervention"] is False, record
    assert record["repair_outcome_consistent"] is not False, record
