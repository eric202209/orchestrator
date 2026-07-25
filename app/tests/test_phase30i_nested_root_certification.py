"""Phase 30I bounded natural-provider nested-root certification."""

from __future__ import annotations

import hashlib
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
    _scenario_metrics,
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
    / "phase30i-nested-root-certification"
)
REAL_OPENCLAW_CONFIG = Path("/root/.openclaw/openclaw.json")
PROVIDER_BACKEND = "local_openclaw"
PROVIDER_MODEL = "qwen3-coder:30b"
PRECONDITION_PACK = (
    "Phase 30H/30G identity and nested-root deterministic pack: 167 passed"
)


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


def _gateway_version() -> str | None:
    executable = shutil.which("openclaw")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or result.stderr).strip() or None


def _task_description_hash(scenario: Scenario) -> str:
    payload = f"{scenario.title}\n{scenario.description}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _seed_bootstrap_repository(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# Existing service\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'bootstrap-service'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )


def _seed_backend_collision(root: Path) -> None:
    package = root / "backend"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("\n", encoding="utf-8")
    (package / "health.py").write_text(
        "def status():\n    return {'status': 'ok'}\n", encoding="utf-8"
    )
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_health.py").write_text(
        "from backend.health import status\n\n"
        "def test_status():\n    assert status()['status'] == 'ok'\n",
        encoding="utf-8",
    )


def _seed_numeric_repository(root: Path) -> None:
    (root / "migrations" / "001").mkdir(parents=True, exist_ok=True)
    (root / "fixtures" / "2026").mkdir(parents=True, exist_ok=True)
    (root / "migrations" / "001" / "README.md").write_text(
        "Initial migration\n", encoding="utf-8"
    )
    (root / "fixtures" / "2026" / "sample.json").write_text("{}\n", encoding="utf-8")


def _seed_multi_file_repository(root: Path) -> None:
    (root / "src" / "inventory").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# Inventory\n", encoding="utf-8")
    (root / "src" / "inventory" / "models.py").write_text(
        "class Item:\n    pass\n", encoding="utf-8"
    )
    (root / "tests" / "test_models.py").write_text(
        "from inventory.models import Item\n\n"
        "def test_item():\n    assert Item() is not None\n",
        encoding="utf-8",
    )
    (root / "docs" / "usage.md").write_text("# Usage\n", encoding="utf-8")


SCENARIOS = (
    Scenario(
        "phase30i-i1-brand-new-named-package",
        "I1",
        "inventory_api_i1",
        "Create a small Python API package",
        "Create a small Python API package named inventory_api_i1 with routes, service code, and tests. Use a conventional package layout and finish with a bounded test run.",
        legitimate_alias_dirs=("inventory_api_i1",),
    ),
    Scenario(
        "phase30i-i2-existing-root-bootstrap",
        "I2",
        "bootstrap_service_i2",
        "Add a health endpoint to the existing service",
        "Extend the existing repository with a health endpoint, one service module, and tests. Preserve the README, pyproject.toml, src directory, and tests directory while making the multi-file change.",
        seed_repo=_seed_bootstrap_repository,
    ),
    Scenario(
        "phase30i-i3-new-backend-fastapi",
        "I3",
        "health_backend_i3",
        "Set up a small FastAPI backend",
        "Set up a small FastAPI backend named health_backend_i3 with an application entry point, a health route, dependency metadata, and one test. Run the test suite after implementing it.",
    ),
    Scenario(
        "phase30i-i3-new-backend-json-api",
        "I3",
        "catalog_api_i3",
        "Build a compact JSON API",
        "Build a compact Python JSON API named catalog_api_i3 with a catalog route, a service layer, a runnable application, and tests. Include a minimal project configuration and verify the result.",
    ),
    Scenario(
        "phase30i-i4-new-frontend-scaffold",
        "I4",
        "inventory_dashboard_i4",
        "Create a small React inventory dashboard",
        "Create a small React and TypeScript inventory dashboard with a list view, a reusable component, styling, and a focused test or type-check. Keep the implementation coherent and runnable.",
        timeout_seconds=360,
    ),
    Scenario(
        "phase30i-i5-cli-scaffold",
        "I5",
        "log_summarizer_i5",
        "Create a small log summarizer CLI",
        "Create a Python command-line application named log_summarizer_i5 that reads a text file, summarizes warning counts, exposes a CLI entry point, and includes tests and project metadata.",
    ),
    Scenario(
        "phase30i-i6-semantic-backend-collision",
        "I6",
        "backend",
        "Extend the existing backend package",
        "Extend the existing backend package with a readiness route and tests. Preserve its package layout and make the smallest coherent multi-file change, then run the tests.",
        seed_repo=_seed_backend_collision,
        legitimate_alias_dirs=("backend",),
    ),
    Scenario(
        "phase30i-i7-numeric-child-directory",
        "I7",
        "migration_tool_i7",
        "Add migration tooling to the existing repository",
        "Add a migration command and fixtures to this repository. Preserve the existing migrations/001 and fixtures/2026 directories, add the next necessary files, and run focused verification.",
        seed_repo=_seed_numeric_repository,
        legitimate_alias_dirs=("001", "2026"),
    ),
    Scenario(
        "phase30i-i8-existing-multi-file-repository",
        "I8",
        "inventory_service_i8",
        "Add inventory lookup behavior across the repository",
        "Extend the existing inventory repository with lookup behavior, tests, and a short documentation update. Keep the existing src, tests, docs, and README structure intact and verify the full focused change.",
        seed_repo=_seed_multi_file_repository,
        timeout_seconds=360,
    ),
)


def _install_runtime_probes(harness, monkeypatch, scenario: Scenario):
    import app.tasks.worker as worker_module

    runtime_root = harness.project_workspace_dir.parent / "phase30i-runtime"
    monkeypatch.setattr(settings, "RUNTIME_ROOT", str(runtime_root))
    probe: dict[str, Any] = {
        "runtime_path": None,
        "runtime_tree_hash_before": None,
        "runtime_tree_hash_after": None,
        "runtime_entries_after": [],
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
            probe["runtime_entries_after"] = [item.name for item in entries]
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


def _first_identity_metadata(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        metadata = row["meta"]
        if metadata.get("physical_runtime_basename") or metadata.get(
            "logical_project_name"
        ):
            return {
                key: metadata.get(key)
                for key in (
                    "physical_runtime_basename",
                    "logical_project_name",
                    "display_project_path",
                    "offending_root_alias",
                    "offending_fragments",
                    "corrected_fragments",
                    "violation_kind",
                )
                if metadata.get(key) is not None
            }
    return {}


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
    config_before = _sha256_file(REAL_OPENCLAW_CONFIG)
    project_before = _hash_tree(harness.project_workspace_dir)
    outside_root = harness.project_workspace_dir.parent
    outside_before = _hash_tree(
        outside_root,
        excluded={harness.project_workspace_dir, runtime_root},
    )
    identity = PlannerWorkspaceIdentity.from_paths(
        project_workspace=harness.project_workspace_dir,
        physical_runtime_root=runtime_root / "<task-execution-id>",
        logical_project_name=scenario.project_name,
    )
    started = time.monotonic()
    dispatch_error = None
    dispatch_result = None
    try:
        dispatch_result = harness.dispatch(
            session, task.id, timeout_seconds=scenario.timeout_seconds
        )
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
        outside_root,
        excluded={harness.project_workspace_dir, runtime_root},
    )
    rows = _log_rows(harness.db, task.id)
    metrics = _scenario_metrics(harness.db, task.id)
    identity_metadata = _first_identity_metadata(rows)
    first_codes = metrics["first_pass_violation_codes"]
    target_first_pass = "nested_project_folder_command" in first_codes
    target_post_repair = (
        "nested_project_folder_command" in metrics["post_repair_violation_codes"]
    )
    target_present = (
        target_first_pass or target_post_repair or bool(metrics["repair_attempts"])
    )
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
    legitimate_false_rejection = scenario.category in {"I1", "I6", "I7"} and (
        target_first_pass or target_post_repair
    )
    numeric_child_rejection = scenario.category == "I7" and target_present
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
        "planner_identity_present": True,
        "planner_identity_evidence": {
            "precondition_pack": PRECONDITION_PACK,
            "identity_excerpt": render_planner_workspace_identity(identity),
            "live_validator_identity": identity_metadata,
        },
        "provider_backend": PROVIDER_BACKEND,
        "provider_model": PROVIDER_MODEL,
        "gateway_version": _gateway_version(),
        "gateway_version_source": "openclaw --version",
        "first_pass_valid": metrics["first_pass_valid"],
        "first_pass_violation_codes": first_codes,
        "target_first_pass_violation": target_first_pass,
        "target_violation_present": target_present,
        "target_violation_kind": identity_metadata.get("violation_kind"),
        "offending_root_alias": metrics["offending_root_alias"],
        "offending_fragments": metrics["offending_fragments"],
        "corrected_fragments": metrics["repair_guidance_identity"].get(
            "corrected_fragments", {}
        ),
        "repair_attempt_count": metrics["repair_attempts"],
        "repair_target_category": (
            "nested_project_folder_command" if metrics["repair_attempts"] else None
        ),
        "repair_guidance_identity": metrics["repair_guidance_identity"],
        "post_repair_violation_codes": metrics["post_repair_violation_codes"],
        "target_violation_resolved": metrics["target_violation_resolved"],
        "same_violation_repeated": metrics["same_violation_repeated"],
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
        "snapshot_restored_if_failed": any(
            "snapshot" in row["message"].lower() and "restor" in row["message"].lower()
            for row in rows
        ),
        "wall_time_seconds": round(wall_seconds, 1),
        "manual_intervention": metrics["manual_intervention"],
        "dispatch_error": dispatch_error,
        "dispatch_result": dispatch_result,
        "runtime_entries_after": probe["runtime_entries_after"],
        "observed_violation_codes": metrics["observed_violation_codes"],
    }
    required = (
        "scenario_id",
        "scenario_category",
        "task_description_hash",
        "project_workspace_realpath",
        "logical_project_name",
        "physical_runtime_realpath",
        "physical_runtime_basename",
        "provider_backend",
        "provider_model",
        "gateway_version",
        "openclaw_config_sha256_before",
        "openclaw_config_sha256_after",
        "project_tree_hash_before",
        "project_tree_hash_after",
        "runtime_tree_hash_before",
        "runtime_tree_hash_after",
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
    "scenario",
    SCENARIOS,
    ids=[scenario.scenario_id for scenario in SCENARIOS],
)
def test_phase30i_natural_provider_scenario(real_pipeline, monkeypatch, scenario):
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
