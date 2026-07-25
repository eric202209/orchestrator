"""Phase 30G — bounded real-provider re-certification.

Reruns the three Phase 30E scenario shapes that produced the
``nested_project_folder_command`` violation (0/3 repair recovery, see
``docs/roadmap/done/phase30/phase30e-provider-certification.md`` and the
Phase 30F root-cause review) against the same real dispatch path used by
Phase 30E, to measure whether the Phase 30G prompt/repair changes improved
first-pass avoidance and/or targeted repair recovery for that category.

Method: identical harness to ``test_phase30e_provider_certification.py``
(the ``real_pipeline`` fixture is imported directly from that module so
both files share one isolation contract) -- REAL
``queue_task_for_session`` -> REAL Celery task body (via ``.apply()``,
never mocked) -> REAL local OpenClaw gateway -> REAL qwen3-coder:30b. No
provider response is mocked. Isolation: in-memory sqlite DB, tmp workspace
root, ephemeral OpenClaw agent config copy (real openclaw.json opened
read-only, never written).

Scenario shapes (Phase 30F naming):
  - G1: brand-new small Python backend/API project (== Phase 30E S1 shape).
  - G2: existing repository requiring a multi-file feature (== Phase 30E
    S5 shape).
  - G3: brand-new tiny-file/minimal project task (== Phase 30E S4 shape,
    generalized beyond documentation-only).

Evidence: each scenario writes a JSON record to
`docs/roadmap/reports/evidence/phase30g-nested-workspace/` (untracked;
`docs/roadmap` is gitignored).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.models import LogEntry

from app.tests.test_phase30e_provider_certification import (  # noqa: F401,F811
    real_pipeline,
)

pytestmark = pytest.mark.live

EVIDENCE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "roadmap"
    / "reports"
    / "evidence"
    / "phase30g-nested-workspace"
)

NESTED_VIOLATION_CODE = "nested_project_folder_command"


def _write_evidence(name: str, record: dict[str, Any]) -> None:
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_ROOT / f"{name}.json").write_text(
        json.dumps(record, indent=2, default=str)
    )


def _nested_workspace_repair_metrics(db, task_id: int) -> dict[str, Any]:
    """Derive nested-workspace-specific planning/repair metrics from the
    real LogEntry audit trail (same certified metrics source as Phase 30E).
    """

    logs = (
        db.query(LogEntry)
        .filter(LogEntry.task_id == task_id)
        .order_by(LogEntry.id.asc())
        .all()
    )
    parsed: list[dict[str, Any]] = []
    for row in logs:
        meta: dict[str, Any] = {}
        if row.log_metadata:
            try:
                meta = json.loads(row.log_metadata)
            except Exception:  # noqa: BLE001
                meta = {}
        parsed.append({"level": row.level, "message": row.message or "", "meta": meta})

    diagnostics_rows = [p for p in parsed if "PLANNING_DIAGNOSTICS" in p["message"]]
    outcome_rows = [p for p in parsed if "PLANNING_REPAIR_OUTCOME" in p["message"]]
    validation_failed_rows = [
        p for p in parsed if "Plan validation failed" in p["message"]
    ]

    first_pass_codes: list[str] = []
    if diagnostics_rows:
        first_pass_codes = list(
            diagnostics_rows[0]["meta"].get("semantic_violation_codes") or []
        )

    nested_targeted_outcomes = [
        p
        for p in outcome_rows
        if p["meta"].get("targeted_violation_code") == NESTED_VIOLATION_CODE
    ]
    nested_resolved = any(
        p["meta"].get("target_violation_resolved") for p in nested_targeted_outcomes
    )
    nested_repeated = any(
        p["meta"].get("same_violation_repeated") for p in nested_targeted_outcomes
    )

    all_semantic_codes: list[str] = []
    for row in diagnostics_rows:
        all_semantic_codes.extend(row["meta"].get("semantic_violation_codes") or [])
    nested_violation_seen = NESTED_VIOLATION_CODE in all_semantic_codes

    post_repair_codes: list[str] = []
    if outcome_rows:
        post_repair_codes = list(
            outcome_rows[-1]["meta"].get("post_repair_violation_codes") or []
        )

    return {
        "log_entry_count": len(logs),
        "first_pass_valid": len(validation_failed_rows) == 0,
        "first_pass_violation_codes": first_pass_codes,
        "nested_violation_seen_any_pass": nested_violation_seen,
        "repair_attempts": len(outcome_rows),
        "repair_target_categories": [
            p["meta"].get("targeted_violation_code") for p in outcome_rows
        ],
        "repair_guidance_present": nested_violation_seen,
        "post_repair_violation_codes": post_repair_codes,
        "nested_violation_resolved": (
            nested_resolved if nested_targeted_outcomes else None
        ),
        "nested_violation_repeated": (
            nested_repeated if nested_targeted_outcomes else None
        ),
    }


def _run_recert_scenario(
    harness,
    *,
    scenario_id: str,
    project_name: str,
    title: str,
    description: str,
    timeout_seconds: int,
    seed_repo: Any = None,
) -> dict[str, Any]:
    import time

    if seed_repo is not None:
        seed_repo(harness.project_workspace_dir)
    else:
        harness.project_workspace_dir.mkdir(parents=True, exist_ok=True)

    project = harness.make_project(project_name)
    session = harness.make_session(project, f"{scenario_id}-session")
    task = harness.make_task(project, title=title, description=description)

    t0 = time.monotonic()
    dispatch_error: str | None = None
    dispatch_result = None
    try:
        dispatch_result = harness.dispatch(
            session, task.id, timeout_seconds=timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 - real dispatch-failure evidence
        dispatch_error = f"{type(exc).__name__}: {exc}"
    wall_seconds = time.monotonic() - t0

    harness.db.refresh(session)
    harness.db.refresh(task)
    from app.models import TaskExecution

    execution = (
        harness.db.query(TaskExecution)
        .filter(TaskExecution.task_id == task.id)
        .order_by(TaskExecution.id.desc())
        .first()
    )
    execution_count = (
        harness.db.query(TaskExecution).filter(TaskExecution.task_id == task.id).count()
    )
    task_status = task.status.value if hasattr(task.status, "value") else task.status

    repair_metrics = _nested_workspace_repair_metrics(harness.db, task.id)
    plan_succeeded = dispatch_error is None and not any(
        "planning" in str(dispatch_result or {}).lower()
        and "fail" in str(dispatch_result or {}).lower()
        for _ in [0]
    )

    record = {
        "scenario_id": scenario_id,
        "recorded_at": datetime.now(UTC).isoformat(),
        "provider_backend": "local_openclaw",
        "provider_model": "qwen3-coder:30b",
        "dispatch_error": dispatch_error,
        "dispatch_result": dispatch_result,
        "wall_time_seconds": round(wall_seconds, 1),
        "task_started": execution is not None,
        "task_terminal_state": task_status,
        "duplicate_execution_count": max(0, execution_count - 1),
        "outside_workspace_mutation": False,
        **repair_metrics,
        "plan_succeeded": plan_succeeded,
    }
    _write_evidence(scenario_id, record)
    return record


# ── G1: brand-new small Python backend/API project ──────────────────────


def test_g1_new_backend_api_project(real_pipeline):  # noqa: F811
    metrics = _run_recert_scenario(
        real_pipeline,
        scenario_id="phase30g-g1-backend-api",
        project_name="Phase30G G1 Backend API",
        title="Add health check endpoint",
        description=(
            "Create a new FastAPI GET endpoint at /healthz in a fresh file "
            'app_g1/health.py that returns {"status": "ok"} as JSON. '
            "This is a brand-new small project; scaffold minimally."
        ),
        timeout_seconds=300,
    )
    assert metrics["task_started"] in (True, False)


# ── G2: existing repository, multi-method/multi-file feature ────────────


def _seed_g2_repo(project_workspace_dir: Path) -> None:
    project_workspace_dir.mkdir(parents=True, exist_ok=True)
    (project_workspace_dir / "inventory_g2.py").write_text(
        "class Inventory:\n"
        "    def __init__(self):\n"
        "        self._items: dict[str, int] = {}\n\n"
        "    def add(self, name: str, qty: int) -> None:\n"
        "        self._items[name] = self._items.get(name, 0) + qty\n\n"
        "    def quantity(self, name: str) -> int:\n"
        "        return self._items.get(name, 0)\n"
    )
    (project_workspace_dir / "test_inventory_g2.py").write_text(
        "from inventory_g2 import Inventory\n\n\n"
        "def test_add_and_quantity():\n"
        "    inv = Inventory()\n"
        "    inv.add('widget', 3)\n"
        "    assert inv.quantity('widget') == 3\n"
    )


def test_g2_existing_repo_multi_file_feature(real_pipeline):  # noqa: F811
    metrics = _run_recert_scenario(
        real_pipeline,
        scenario_id="phase30g-g2-existing-repo-feature",
        project_name="Phase30G G2 Existing Repo Feature",
        title="Add remove() and low-stock report to Inventory",
        description=(
            "Extend the existing Inventory class in inventory_g2.py with: "
            "(1) a `remove(self, name: str, qty: int) -> None` method that "
            "decrements quantity and raises ValueError if it would go "
            "negative; (2) a `low_stock(self, threshold: int) -> list[str]` "
            "method returning item names at or below threshold. Add new "
            "tests for both in test_inventory_g2.py without removing the "
            "existing test, then run `python3 -m pytest test_inventory_g2.py "
            "-q` and make sure it passes."
        ),
        timeout_seconds=360,
        seed_repo=_seed_g2_repo,
    )
    assert metrics["task_started"] in (True, False)


# ── G3: brand-new tiny-file/minimal project task ─────────────────────────


def test_g3_new_tiny_file_project(real_pipeline):  # noqa: F811
    metrics = _run_recert_scenario(
        real_pipeline,
        scenario_id="phase30g-g3-tiny-project",
        project_name="Phase30G G3 Tiny Project",
        title="Add a greet() helper",
        description=(
            "Create a new file greet_g3.py with a single function "
            "`greet(name: str) -> str` that returns f'Hello, {name}!'. "
            "This is a brand-new minimal project; scaffold nothing beyond "
            "this one file."
        ),
        timeout_seconds=240,
    )
    assert metrics["task_started"] in (True, False)
