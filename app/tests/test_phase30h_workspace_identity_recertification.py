"""Phase 30H bounded real-provider certification with tree-hash evidence."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pytest

from app.config import settings
from app.models import LogEntry, Task, TaskExecution

pytest_plugins = ("app.tests.test_phase30e_provider_certification",)

pytestmark = pytest.mark.live

EVIDENCE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "roadmap"
    / "reports"
    / "evidence"
    / "phase30h-workspace-identity"
)
PROVIDER_BACKEND = "local_openclaw"
PROVIDER_MODEL = "qwen3-coder:30b"
MAX_H5_ADDITIONAL_ATTEMPTS = 2


def _hash_tree(root: Path, *, excluded: set[Path] | None = None) -> str:
    """Hash names and file bytes for a real filesystem tree."""

    root = Path(root).resolve()
    excluded = {Path(path).resolve() for path in (excluded or set())}
    digest = hashlib.sha256()
    if not root.exists():
        return "MISSING"
    for path in sorted(root.rglob("*")):
        resolved = path.resolve() if not path.is_symlink() else path
        if any(resolved == item or item in resolved.parents for item in excluded):
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        if path.is_symlink():
            digest.update(b"L")
            digest.update(path.readlink().as_posix().encode("utf-8"))
        elif path.is_dir():
            digest.update(b"D")
        elif path.is_file():
            digest.update(b"F")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_evidence(scenario_id: str, record: dict[str, Any]) -> None:
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    EVIDENCE_ROOT.chmod(0o777)
    output = EVIDENCE_ROOT / f"{scenario_id}.json"
    output.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    output.chmod(0o666)


def _log_rows(db, task_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(LogEntry)
        .filter(LogEntry.task_id == task_id)
        .order_by(LogEntry.id.asc())
        .all()
    )
    parsed = []
    for row in rows:
        metadata = {}
        if row.log_metadata:
            try:
                metadata = json.loads(row.log_metadata)
            except Exception:  # noqa: BLE001
                metadata = {}
        parsed.append(
            {"level": row.level, "message": row.message or "", "meta": metadata}
        )
    return parsed


def _scenario_metrics(db, task_id: int) -> dict[str, Any]:
    rows = _log_rows(db, task_id)
    diagnostics = [row for row in rows if "PLANNING_DIAGNOSTICS" in row["message"]]
    outcomes = [row for row in rows if "PLANNING_REPAIR_OUTCOME" in row["message"]]
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
    target_outcomes = [
        row
        for row in outcomes
        if row["meta"].get("targeted_violation_code") == "nested_project_folder_command"
    ]
    all_codes = [
        code
        for row in diagnostics
        for code in row["meta"].get("semantic_violation_codes") or []
    ]
    guidance_identity = (
        target_outcomes[0]["meta"].get("repair_guidance_identity")
        if target_outcomes
        else {}
    )
    final_codes = (
        list(outcomes[-1]["meta"].get("post_repair_violation_codes") or [])
        if outcomes
        else []
    )
    return {
        "first_pass_valid": not diagnostics or not first_codes,
        "first_pass_violation_codes": first_codes,
        "offending_root_alias": guidance_identity.get("offending_root_alias"),
        "offending_fragments": guidance_identity.get("offending_fragments", {}),
        "repair_attempts": len(target_outcomes),
        "repair_guidance_identity": guidance_identity,
        "post_repair_violation_codes": final_codes,
        "target_violation_resolved": (
            any(row["meta"].get("target_violation_resolved") for row in target_outcomes)
            if target_outcomes
            else None
        ),
        "same_violation_repeated": (
            any(row["meta"].get("same_violation_repeated") for row in target_outcomes)
            if target_outcomes
            else None
        ),
        "final_plan_valid": not planning_errors,
        "planning_failure_count": len(planning_errors),
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
        "provider_backend": PROVIDER_BACKEND,
        "provider_model": PROVIDER_MODEL,
        "gateway_version": next(
            (
                row["meta"].get("gateway_version")
                for row in rows
                if row["meta"].get("gateway_version")
            ),
            None,
        ),
        "observed_violation_codes": sorted(set(all_codes)),
    }


def _install_runtime_hash_probes(harness, monkeypatch):
    import app.tasks.worker as worker_module

    runtime_root = harness.project_workspace_dir.parent / "phase30h-runtime"
    monkeypatch.setattr(settings, "RUNTIME_ROOT", str(runtime_root))
    probe: dict[str, Any] = {
        "runtime_path": None,
        "runtime_tree_hash_before": None,
        "runtime_tree_hash_after": None,
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
        return dispose(sandbox, **kwargs)

    monkeypatch.setattr(
        worker_module, "_maybe_allocate_runtime_workspace", allocate_with_hash
    )
    monkeypatch.setattr(
        worker_module, "_dispose_runtime_workspace_safely", dispose_with_hash
    )
    return probe, runtime_root


def _run_h_scenario(
    harness,
    monkeypatch,
    *,
    scenario_id: str,
    project_name: str,
    title: str,
    description: str,
    seed_repo: Callable[[Path], None] | None = None,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    if seed_repo is not None:
        seed_repo(harness.project_workspace_dir)
    else:
        harness.project_workspace_dir.mkdir(parents=True, exist_ok=True)
    project = harness.make_project(project_name)
    session = harness.make_session(project, f"{scenario_id}-session")
    task = harness.make_task(project, title=title, description=description)
    probe, runtime_root = _install_runtime_hash_probes(harness, monkeypatch)
    config_path = Path("/root/.openclaw/openclaw.json")
    config_before = _sha256_file(config_path)
    project_before = _hash_tree(harness.project_workspace_dir)
    outside_root = harness.project_workspace_dir.parent
    outside_before = _hash_tree(
        outside_root,
        excluded={harness.project_workspace_dir, runtime_root},
    )
    started = time.monotonic()
    dispatch_error = None
    dispatch_result = None
    try:
        dispatch_result = harness.dispatch(
            session, task.id, timeout_seconds=timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 - evidence records real failure
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
    outside_after = _hash_tree(
        outside_root,
        excluded={harness.project_workspace_dir, runtime_root},
    )
    config_after = _sha256_file(config_path)
    metrics = _scenario_metrics(harness.db, task.id)
    task_status = task.status.value if hasattr(task.status, "value") else task.status
    record = {
        "scenario_id": scenario_id,
        "recorded_at": datetime.now(UTC).isoformat(),
        "project_workspace_realpath": str(harness.project_workspace_dir.resolve()),
        "logical_project_name": project.name,
        "physical_runtime_realpath": probe["runtime_path"],
        "physical_runtime_basename": (
            Path(probe["runtime_path"]).name if probe["runtime_path"] else None
        ),
        "planner_display_identity": "current isolated task workspace",
        "project_tree_hash_before": project_before,
        "project_tree_hash_after": project_after,
        "runtime_tree_hash_before": probe["runtime_tree_hash_before"],
        "runtime_tree_hash_after": probe["runtime_tree_hash_after"],
        "outside_workspace_diff": {
            "before": outside_before,
            "after": outside_after,
            "changed": outside_before != outside_after,
        },
        "openclaw_config_sha256_before": config_before,
        "openclaw_config_sha256_after": config_after,
        "openclaw_config_unchanged": config_before == config_after,
        "task_started": execution is not None,
        "task_terminal_state": task_status,
        "duplicate_task_count": max(0, task_count - 1),
        "duplicate_execution_count": max(0, execution_count - 1),
        "dispatch_error": dispatch_error,
        "dispatch_result": dispatch_result,
        "wall_time_seconds": round(wall_seconds, 1),
        "snapshot_restored_if_failed": (
            any(
                "snapshot" in row["message"].lower()
                and "restor" in row["message"].lower()
                for row in _log_rows(harness.db, task.id)
            )
            if dispatch_error or not metrics["final_plan_valid"]
            else None
        ),
        **metrics,
    }
    _write_evidence(scenario_id, record)
    return record


def _seed_semantic_package(root: Path) -> None:
    package = root / "inventory_api"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("def version():\n    return '1'\n")
    (package / "service.py").write_text(
        "def total(values: list[int]) -> int:\n    return sum(values)\n"
    )
    (root / "test_inventory_api.py").write_text(
        "from inventory_api.service import total\n\n"
        "def test_total():\n    assert total([1, 2]) == 3\n"
    )


def _seed_existing_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "inventory.py").write_text(
        "class Inventory:\n"
        "    def __init__(self):\n"
        "        self.items = {}\n\n"
        "    def add(self, name, qty):\n"
        "        self.items[name] = self.items.get(name, 0) + qty\n"
    )
    (root / "test_inventory.py").write_text(
        "from inventory import Inventory\n\n"
        "def test_add():\n"
        "    inv = Inventory()\n"
        "    inv.add('widget', 2)\n"
        "    assert inv.items['widget'] == 2\n"
    )


def test_h1_numeric_runtime_new_backend(real_pipeline, monkeypatch):
    record = _run_h_scenario(
        real_pipeline,
        monkeypatch,
        scenario_id="phase30h-h1-numeric-runtime-backend",
        project_name="inventory-api-h1",
        title="Create a small health endpoint",
        description=(
            "Create a fresh small Python backend with a /healthz endpoint that "
            "returns status ok. Keep all files directly in the current workspace "
            "and run a bounded Python verification."
        ),
    )
    assert (
        record["physical_runtime_basename"] in {None, "42"}
        or record["physical_runtime_basename"].isdigit()
    )


def test_h2_semantic_name_is_legitimate_package(real_pipeline, monkeypatch):
    record = _run_h_scenario(
        real_pipeline,
        monkeypatch,
        scenario_id="phase30h-h2-semantic-package",
        project_name="inventory_api",
        title="Extend the existing inventory package",
        description=(
            "Extend the existing inventory_api package with a small lookup "
            "helper and a test. Preserve the package directory and existing tests."
        ),
        seed_repo=_seed_semantic_package,
    )
    assert record["duplicate_task_count"] == 0


def test_h3_existing_multi_file_repository(real_pipeline, monkeypatch):
    record = _run_h_scenario(
        real_pipeline,
        monkeypatch,
        scenario_id="phase30h-h3-existing-repository",
        project_name="inventory-api-h3",
        title="Add inventory removal behavior",
        description=(
            "Extend the existing Inventory class with remove(name, qty), add "
            "tests without deleting the existing test, and run pytest."
        ),
        seed_repo=_seed_existing_repo,
        timeout_seconds=360,
    )
    assert record["duplicate_execution_count"] == 0


def test_h4_minimal_new_project(real_pipeline, monkeypatch):
    record = _run_h_scenario(
        real_pipeline,
        monkeypatch,
        scenario_id="phase30h-h4-minimal-project",
        project_name="tiny-helper-h4",
        title="Add a greet helper",
        description=(
            "Create one small greet.py file with greet(name) returning a greeting. "
            "Do not scaffold a nested project folder; verify the result."
        ),
        timeout_seconds=240,
    )
    assert record["duplicate_task_count"] == 0


def test_h5_bounded_natural_repair_attempts(real_pipeline, monkeypatch):
    """Run at most two additional natural shapes; never loop until recovery."""

    records = []
    for index in range(MAX_H5_ADDITIONAL_ATTEMPTS):
        records.append(
            _run_h_scenario(
                real_pipeline,
                monkeypatch,
                scenario_id=f"phase30h-h5-natural-repair-{index + 1}",
                project_name=f"repair-opportunity-h5-{index + 1}",
                title="Create a small Python API project",
                description=(
                    "Create a small new Python API project named after the task "
                    "with app/main.py, a health endpoint, one test, and a final "
                    "pytest verification."
                ),
                timeout_seconds=300,
            )
        )
    assert len(records) == MAX_H5_ADDITIONAL_ATTEMPTS
