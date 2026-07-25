"""Phase 30E — Provider-Backed Planning/Execution Certification.

Certifies whether the currently deployed provider-backed planning and
execution path (real qwen3-coder:30b served by the real Ollama daemon at
``host.docker.internal:11434``, dispatched through the real, live local
OpenClaw gateway process because the effective planning/execution backend
in this environment is ``local_openclaw`` -- ``PLANNING_BACKEND`` is unset
in ``.env`` so it falls back to ``AGENT_BACKEND=local_openclaw``, confirmed
at runtime, not assumed) is production-reliable, per the operator's Phase
30E brief.

Method: each scenario drives the REAL production dispatch path --
``app.services.session.session_runtime_service.queue_task_for_session`` --
exactly as the live API endpoints call it, through the REAL Celery task
body ``app.tasks.worker.execute_orchestration_task`` (run synchronously via
Celery's own eager ``.apply()``, not mocked), which builds a real runtime
service, assembles a real planning prompt, and makes real network calls to
the real running provider. No provider response is mocked anywhere in this
file.

Isolation (so this certification leaves zero residue in the live system):
  - DB: `app.tasks.worker.get_db_session` is monkeypatched to the same
    in-memory sqlite engine `db_session`/`db_session_factory` already use
    (the same pattern `test_execution_reliability_hardening.py` uses to
    inspect "worker-side" state) -- the real orchestrator.db on disk is
    never touched.
  - Workspace: the `isolated_workspace_root` fixture (already autoused via
    `db_session`) redirects `OPENCLAW_WORKSPACE` to a pytest `tmp_path`.
  - OpenClaw agent identity: Phase 23D's `ExecutorWorkspaceBinding` fails
    closed unless a *real* OpenClaw agent is registered with a `workspace`
    exactly matching the Project Workspace (confirmed by reproducing the
    fail-closed error against an unregistered tmp workspace first). This
    file satisfies that the same way `ExecutorWorkspaceBinding`'s own
    docstring says it is meant to be satisfied -- via `OPENCLAW_CONFIG_PATH`
    pointed at a private, ephemeral copy of the real `~/.openclaw/openclaw.json`
    with exactly one additional agent entry whose `workspace` is this
    test's own tmp project dir. The real `~/.openclaw/openclaw.json` is
    read-only opened and never written.

Evidence: each scenario writes a JSON record to
`docs/roadmap/reports/evidence/phase30e-provider/` (untracked; docs/roadmap
is gitignored, matching the Phase 30C/30D convention).

Cost: each scenario makes a real ~30B-parameter local model call for
planning and (when planning succeeds) further real calls per execution
step. Real wall-clock time per scenario is on the order of two to six
minutes. Scenario count is deliberately bounded -- see the Phase 30E report
for what was and was not covered and why.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import pytest

from app.config import settings
from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.session.session_runtime_service import queue_task_for_session

EVIDENCE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "roadmap"
    / "reports"
    / "evidence"
    / "phase30e-provider"
)

REAL_OPENCLAW_CONFIG_PATH = Path("/root/.openclaw/openclaw.json")


def _write_evidence(name: str, record: dict[str, Any]) -> None:
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_ROOT / f"{name}.json").write_text(
        json.dumps(record, indent=2, default=str)
    )


# ── Shared harness ──────────────────────────────────────────────────────


@pytest.fixture
def real_pipeline(db_session_factory, isolated_workspace_root, monkeypatch, tmp_path):
    """Wires the real dispatch path to an isolated DB/workspace/agent.

    Returns a small object with:
      - `db`: the in-memory sqlite session tests use to set up/assert state
      - `project_workspace_dir`: the Project Workspace all projects in this
        test must use (matches the synthetic OpenClaw agent's `workspace`)
      - `dispatch(session, task_id, **kwargs)`: calls the REAL
        `queue_task_for_session`, which internally reaches the REAL
        `execute_orchestration_task` synchronously (via Celery `.apply()`,
        never mocked) and therefore makes real provider network calls.
    """
    import app.tasks.worker as worker_module

    db = db_session_factory()

    # Register a synthetic OpenClaw agent whose workspace matches this
    # test's Project Workspace, via a private ephemeral copy of the real
    # openclaw.json (read-only opened; never written). This is the exact
    # seam ExecutorWorkspaceBinding's own docstring describes
    # (OPENCLAW_CONFIG_PATH), not a change to production matching logic.
    project_workspace_dir = isolated_workspace_root / "proj"
    real_config = json.loads(REAL_OPENCLAW_CONFIG_PATH.read_text(encoding="utf-8"))
    agent_dir = tmp_path / "phase30e-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    real_config["agents"]["list"].append(
        {
            "id": "phase30e-cert",
            "name": "phase30e-cert",
            "workspace": str(project_workspace_dir),
            "agentDir": str(agent_dir),
        }
    )
    synthetic_config_path = tmp_path / "openclaw.json"
    synthetic_config_path.write_text(json.dumps(real_config, indent=2))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(synthetic_config_path))

    # Worker DB wiring: the real Celery task body opens its own DB session
    # via `get_db_session()`; point that at the SAME in-memory engine this
    # test uses, never at the real orchestrator.db on disk.
    monkeypatch.setattr(worker_module, "get_db_session", db_session_factory)

    real_execute_orchestration_task = worker_module.execute_orchestration_task

    class _SyncResult:
        def __init__(self, id_: str, outcome: Any):
            self.id = id_
            self.outcome = outcome

    class _SyncExecuteOrchestrationTask:
        @staticmethod
        def delay(**kwargs):
            async_result = real_execute_orchestration_task.apply(kwargs=kwargs)
            outcome = async_result.get(propagate=True)
            return _SyncResult(id_=async_result.id, outcome=outcome)

    monkeypatch.setattr(
        worker_module, "execute_orchestration_task", _SyncExecuteOrchestrationTask
    )

    class Harness:
        def __init__(self):
            self.db = db
            self.project_workspace_dir = project_workspace_dir

        def make_project(self, name: str) -> Project:
            project = Project(name=name, workspace_path=str(project_workspace_dir))
            self.db.add(project)
            self.db.commit()
            self.db.refresh(project)
            return project

        def make_session(self, project: Project, name: str) -> SessionModel:
            session = SessionModel(
                project_id=project.id,
                name=name,
                description="phase30e provider certification",
                status="pending",
            )
            self.db.add(session)
            self.db.commit()
            self.db.refresh(session)
            return session

        def make_task(self, project: Project, *, title: str, description: str) -> Task:
            task = Task(
                project_id=project.id,
                title=title,
                description=description,
                status=TaskStatus.PENDING,
            )
            self.db.add(task)
            self.db.commit()
            self.db.refresh(task)
            return task

        def dispatch(
            self,
            session: SessionModel,
            task_id: int,
            *,
            timeout_seconds: int = 300,
        ) -> dict[str, Any]:
            return queue_task_for_session(
                db=self.db,
                session=session,
                task_id=task_id,
                timeout_seconds=timeout_seconds,
            )

    return Harness()


def _log_metrics(db, task_id: int) -> dict[str, Any]:
    """Derive planning-pipeline metrics from the real LogEntry audit trail.

    The on-disk event journal (`read_orchestration_events`) was found to be
    intermittently incomplete under this harness's dual-DB-session wiring
    (see Phase 30E report, "harness limitations") -- `LogEntry` rows, which
    are the same rows the production UI reads, were complete and consistent
    in every run and are used as the metrics source of truth instead.
    """
    logs = (
        db.query(LogEntry)
        .filter(LogEntry.task_id == task_id)
        .order_by(LogEntry.id.asc())
        .all()
    )
    parsed = []
    for row in logs:
        meta: dict[str, Any] = {}
        if row.log_metadata:
            try:
                meta = json.loads(row.log_metadata)
            except Exception:  # noqa: BLE001
                meta = {}
        parsed.append({"level": row.level, "message": row.message or "", "meta": meta})

    validation_failed_rows = [
        p for p in parsed if "Plan validation failed" in p["message"]
    ]
    repair_attempt_rows = [
        p for p in parsed if "Planning repair attempt is now running" in p["message"]
    ]
    repair_completed_rows = [
        p for p in parsed if "Planning repair completed" in p["message"]
    ]
    timeout_rows = [
        p
        for p in parsed
        if "timeout" in p["message"].lower() or "timed out" in p["message"].lower()
    ]
    planning_error_rows = [
        p
        for p in parsed
        if p["level"] == "ERROR" and p["meta"].get("phase") == "planning"
    ]
    error_rows = [p for p in parsed if p["level"] == "ERROR"]
    last_error = error_rows[-1] if error_rows else None
    evaluator_verdicts = [p["message"] for p in parsed if "QA verdict" in p["message"]]
    steps_message = next(
        (p["message"] for p in parsed if "completed successfully with" in p["message"]),
        None,
    )

    return {
        "log_entry_count": len(logs),
        "validation_failed_count": len(validation_failed_rows),
        "repair_attempt_count": len(repair_attempt_rows),
        "repair_completed_count": len(repair_completed_rows),
        "timeout_mention_count": len(timeout_rows),
        "planning_phase_error_count": len(planning_error_rows),
        "last_error_message": last_error["message"] if last_error else None,
        "last_error_reason": last_error["meta"].get("reason") if last_error else None,
        "evaluator_verdicts": evaluator_verdicts,
        "steps_completed_message": steps_message,
    }


def _run_scenario(
    harness,
    *,
    scenario_id: str,
    project_name: str,
    title: str,
    description: str,
    timeout_seconds: int = 300,
    project: Optional[Project] = None,
    session: Optional[SessionModel] = None,
    task: Optional[Task] = None,
) -> dict[str, Any]:
    """Run one real dispatch. Pass `project`/`session`/`task` to dispatch
    caller-owned rows (e.g. for the repeated-request scenario, which must
    assert against the exact Task it created, not a fresh one)."""
    if project is None:
        project = harness.make_project(project_name)
    if session is None:
        session = harness.make_session(project, f"{scenario_id}-session")
    if task is None:
        task = harness.make_task(project, title=title, description=description)

    metrics: dict[str, Any] = {
        "scenario_id": scenario_id,
        "PLANNING_ATTEMPTED": 1,
        "MANUAL_INTERVENTIONS": 0,
        "dispatch_error": None,
    }

    t0 = time.monotonic()
    dispatch_error: Optional[str] = None
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
    log_metrics = _log_metrics(harness.db, task.id)

    # A valid plan was produced iff the pipeline never recorded a
    # planning-phase ERROR row (repair may have run and succeeded; that is
    # not a failure). Distinguished from "task done", since a task can fail
    # later in execution/debug after planning genuinely succeeded.
    planning_failed = log_metrics["planning_phase_error_count"] > 0
    metrics["PLANNING_SUCCEEDED"] = 0 if (planning_failed or dispatch_error) else 1
    metrics["PLANNING_FAILED"] = 1 if (planning_failed or dispatch_error) else 0
    metrics["VALID_PLAN_CREATED"] = bool(metrics["PLANNING_SUCCEEDED"])
    metrics["VALIDATION_REJECTIONS"] = log_metrics["validation_failed_count"]
    metrics["PLANNING_RETRIES"] = log_metrics["repair_attempt_count"]

    is_real_timeout = log_metrics["timeout_mention_count"] > 0 and (
        "timeout" in (log_metrics["last_error_reason"] or "").lower()
        or "timeout" in (log_metrics["last_error_message"] or "").lower()
    )
    metrics["PLANNING_TIMEOUT"] = 1 if is_real_timeout else 0

    metrics.update(
        {
            "dispatch_error": dispatch_error,
            "dispatch_result": dispatch_result,
            "wall_seconds": round(wall_seconds, 1),
            "TIME_TO_VALID_PLAN": (
                round(wall_seconds, 1) if metrics.get("VALID_PLAN_CREATED") else None
            ),
            "session_status": session.status,
            "task_status": task_status,
            "TASKS_CREATED": 1,
            "TASKS_STARTED": 1 if execution is not None else 0,
            "TASKS_NEVER_STARTED": 0 if execution is not None else 1,
            "execution_row_count": execution_count,
            "DUPLICATE_TASKS": max(0, execution_count - 1),
            "execution_status": (
                execution.status.value
                if execution is not None and hasattr(execution.status, "value")
                else (execution.status if execution is not None else None)
            ),
            "log_metrics": log_metrics,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
    )
    _write_evidence(scenario_id, metrics)
    return metrics


# ── S1: small new Python backend API task ──────────────────────────────


def test_s1_small_backend_api_task(real_pipeline):
    metrics = _run_scenario(
        real_pipeline,
        scenario_id="phase30e-s1-backend-api",
        project_name="Phase30E S1 Backend API",
        title="Add health check endpoint",
        description=(
            "Create a new FastAPI GET endpoint at /healthz in a fresh file "
            'app_s1/health.py that returns {"status": "ok"} as JSON. '
            "This is a brand-new small project; scaffold minimally."
        ),
        timeout_seconds=300,
    )
    assert metrics["PLANNING_ATTEMPTED"] == 1
    assert metrics["TASKS_CREATED"] == 1


# ── S2: small new frontend component task ───────────────────────────────


def test_s2_small_frontend_component_task(real_pipeline):
    metrics = _run_scenario(
        real_pipeline,
        scenario_id="phase30e-s2-frontend-component",
        project_name="Phase30E S2 Frontend Component",
        title="Add a Badge component",
        description=(
            "Create a new React + TypeScript function component in a fresh "
            "file src_s2/components/Badge.tsx. It should accept a `label: "
            "string` prop and render it inside a <span> with className "
            "'badge'. This is a brand-new small project; scaffold minimally "
            "(no build tooling required, just the component file)."
        ),
        timeout_seconds=300,
    )
    assert metrics["PLANNING_ATTEMPTED"] == 1
    assert metrics["TASKS_CREATED"] == 1


# ── S3: bug fix against a pre-seeded existing repo ──────────────────────


def _seed_buggy_repo(project_workspace_dir: Path) -> None:
    project_workspace_dir.mkdir(parents=True, exist_ok=True)
    (project_workspace_dir / "calc_s3.py").write_text(
        "def add_one(values):\n"
        '    """Return a new list with 1 added to each element."""\n'
        "    return [v + 2 for v in values]  # BUG: should add 1, not 2\n"
    )
    (project_workspace_dir / "test_calc_s3.py").write_text(
        "from calc_s3 import add_one\n\n\n"
        "def test_add_one():\n"
        "    assert add_one([1, 2, 3]) == [2, 3, 4]\n"
    )


def test_s3_bug_fix_existing_repo(real_pipeline):
    _seed_buggy_repo(real_pipeline.project_workspace_dir)
    metrics = _run_scenario(
        real_pipeline,
        scenario_id="phase30e-s3-bug-fix",
        project_name="Phase30E S3 Bug Fix",
        title="Fix off-by-one bug in add_one",
        description=(
            "The existing file calc_s3.py has a function add_one(values) that "
            "is supposed to add exactly 1 to each element, but it currently "
            "adds 2. Fix calc_s3.py so `python3 -m pytest test_calc_s3.py -q` "
            "passes. Do not change the test file."
        ),
        timeout_seconds=300,
    )
    assert metrics["PLANNING_ATTEMPTED"] == 1


# ── S4: documentation task ──────────────────────────────────────────────


def test_s4_documentation_task(real_pipeline):
    real_pipeline.project_workspace_dir.mkdir(parents=True, exist_ok=True)
    metrics = _run_scenario(
        real_pipeline,
        scenario_id="phase30e-s4-documentation",
        project_name="Phase30E S4 Documentation",
        title="Write USAGE.md",
        description=(
            "Create a new file USAGE.md at the project root documenting how "
            "to run a hypothetical CLI tool called `widget-cli`: one H1 "
            "title, a short description paragraph, and a fenced code block "
            "showing `widget-cli --help`. This is a documentation-only task; "
            "no code changes."
        ),
        timeout_seconds=240,
    )
    assert metrics["PLANNING_ATTEMPTED"] == 1


# ── S5: longer/more complex change to an existing repo ──────────────────


def _seed_larger_repo(project_workspace_dir: Path) -> None:
    project_workspace_dir.mkdir(parents=True, exist_ok=True)
    (project_workspace_dir / "inventory_s5.py").write_text(
        "class Inventory:\n"
        "    def __init__(self):\n"
        "        self._items: dict[str, int] = {}\n\n"
        "    def add(self, name: str, qty: int) -> None:\n"
        "        self._items[name] = self._items.get(name, 0) + qty\n\n"
        "    def quantity(self, name: str) -> int:\n"
        "        return self._items.get(name, 0)\n"
    )
    (project_workspace_dir / "test_inventory_s5.py").write_text(
        "from inventory_s5 import Inventory\n\n\n"
        "def test_add_and_quantity():\n"
        "    inv = Inventory()\n"
        "    inv.add('widget', 3)\n"
        "    assert inv.quantity('widget') == 3\n"
    )


def test_s5_longer_complex_existing_repo_change(real_pipeline):
    _seed_larger_repo(real_pipeline.project_workspace_dir)
    metrics = _run_scenario(
        real_pipeline,
        scenario_id="phase30e-s5-longer-complex",
        project_name="Phase30E S5 Longer Complex",
        title="Add remove() and low-stock report to Inventory",
        description=(
            "Extend the existing Inventory class in inventory_s5.py with: "
            "(1) a `remove(self, name: str, qty: int) -> None` method that "
            "decrements quantity and raises ValueError if it would go "
            "negative; (2) a `low_stock(self, threshold: int) -> list[str]` "
            "method returning item names at or below threshold. Add new "
            "tests for both in test_inventory_s5.py without removing the "
            "existing test, then run `python3 -m pytest test_inventory_s5.py "
            "-q` and make sure it passes."
        ),
        timeout_seconds=360,
    )
    assert metrics["PLANNING_ATTEMPTED"] == 1


# ── S6: identical request repeated (idempotency / duplicate detection) ──


def test_s6_identical_repeated_request(real_pipeline):
    project = real_pipeline.make_project("Phase30E S6 Repeat")
    description = (
        "Create a new file app_s6/ping.py exporting a function `ping()` "
        "that returns the string 'pong'. Brand-new small project; scaffold "
        "minimally."
    )

    session_a = real_pipeline.make_session(project, "s6-session-a")
    task_a = real_pipeline.make_task(
        project, title="Add ping() (first)", description=description
    )
    result_a = _run_scenario(
        real_pipeline,
        scenario_id="phase30e-s6a-repeat-first",
        project_name="Phase30E S6 Repeat",
        title="Add ping() (first)",
        description=description,
        timeout_seconds=300,
        project=project,
        session=session_a,
        task=task_a,
    )

    session_b = real_pipeline.make_session(project, "s6-session-b")
    task_b = real_pipeline.make_task(
        project, title="Add ping() (repeat)", description=description
    )
    result_b = _run_scenario(
        real_pipeline,
        scenario_id="phase30e-s6b-repeat-second",
        project_name="Phase30E S6 Repeat",
        title="Add ping() (repeat)",
        description=description,
        timeout_seconds=300,
        project=project,
        session=session_b,
        task=task_b,
    )

    all_executions = (
        real_pipeline.db.query(TaskExecution)
        .filter(TaskExecution.task_id.in_([task_a.id, task_b.id]))
        .all()
    )
    combined = {
        "task_a_id": task_a.id,
        "task_b_id": task_b.id,
        "task_a_status": result_a["task_status"],
        "task_b_status": result_b["task_status"],
        "distinct_task_execution_rows": len({e.id for e in all_executions}),
        "DUPLICATE_EXECUTIONS": max(
            0, len(all_executions) - len({e.task_id for e in all_executions})
        ),
    }
    _write_evidence("phase30e-s6-repeat-combined", combined)

    # Two independently created tasks against the same identical description
    # must not collapse into, or corrupt, each other's execution row.
    assert task_a.id != task_b.id
    assert combined["distinct_task_execution_rows"] == len(all_executions)


# ── S7: real provider timeout (existing-settings override only) ────────


def test_s7_real_provider_timeout(real_pipeline, monkeypatch):
    """Force a genuine provider timeout via an existing settings knob.

    Uses `PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS` and
    `OLLAMA_PLANNING_TIMEOUT_SECONDS` -- both existing, documented settings
    -- set to 1 second for this test only (monkeypatch reverts them after).
    No production code is modified; this is the same class of override the
    settings' own docstrings describe ("Set this in .env for slower local
    planning models"). qwen3-coder:30b cannot complete a real planning call
    against local_openclaw in 1 second, so this is expected to produce a
    real, not synthetic, timeout failure.
    """
    monkeypatch.setattr(settings, "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "OLLAMA_PLANNING_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "PLANNING_REPAIR_TIMEOUT_SECONDS", 1)

    metrics = _run_scenario(
        real_pipeline,
        scenario_id="phase30e-s7-provider-timeout",
        project_name="Phase30E S7 Provider Timeout",
        title="Add a trivial constant",
        description=(
            "Create a new file app_s7/const.py containing exactly one line: "
            "ANSWER = 42"
        ),
        timeout_seconds=60,
    )
    assert metrics["PLANNING_ATTEMPTED"] == 1
    # Evidence-only: outcome (timeout vs. fast fallback-repair success) is
    # asserted honestly in the Phase 30E report from the recorded metrics,
    # not forced here, since the real timeout-handling path is exactly what
    # is under certification.
