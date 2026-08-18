"""V1-B certification evidence: one bounded D3-shaped grounded job.

Test-only. Records runtime/control evidence for the certification gate and
compares Orchestrator against a direct grounded-edit baseline on the same job.
Evidence is written to $GROUNDED_CERT_EVIDENCE_DIR when that variable is set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.workspace.checkpoint_service import CheckpointService

OPS_SOURCE = '''"""Bounded arithmetic helpers."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b
'''

TEST_SOURCE = """from calc import ops


def test_add():
    assert ops.add(2, 3) == 5


def test_subtract():
    assert ops.subtract(3, 2) == 1
"""

OPS_QUOTE = "def subtract(a, b):\n    return a - b"
OPS_NEW = """def subtract(a, b):
    return a - b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero is not supported")
    return a / b
"""

TEST_QUOTE = "def test_subtract():\n    assert ops.subtract(3, 2) == 1"
TEST_NEW = """def test_subtract():
    assert ops.subtract(3, 2) == 1


def test_divide():
    assert ops.divide(6, 3) == 2


def test_divide_by_zero():
    try:
        ops.divide(1, 0)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
"""

PROJECT_FILES = {
    "calc/__init__.py": "",
    "calc/ops.py": OPS_SOURCE,
    "tests/test_ops.py": TEST_SOURCE,
}

EVIDENCE: dict = {}


def _record(key: str, value) -> None:
    EVIDENCE[key] = value
    target = os.environ.get("GROUNDED_CERT_EVIDENCE_DIR")
    if target:
        out = Path(target) / "certification_evidence.json"
        out.write_text(json.dumps(EVIDENCE, indent=2, default=str), encoding="utf-8")


def _submission():
    return {
        "execution_kind": "grounded_external_submission",
        "operations": [
            {
                "op": "replace_in_file",
                "path": "calc/ops.py",
                "quote": OPS_QUOTE,
                "new": OPS_NEW,
            },
            {
                "op": "replace_in_file",
                "path": "tests/test_ops.py",
                "quote": TEST_QUOTE,
                "new": TEST_NEW,
            },
        ],
        "verification": {"kind": "focused_tests", "paths": ["tests/test_ops.py"]},
    }


def _seed(db_session, root: Path):
    task_root = root / "task-1"
    task_root.mkdir(parents=True)
    for path, content in PROJECT_FILES.items():
        target = task_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (root / "CANONICAL_BASELINE.txt").write_text("unchanged\n", encoding="utf-8")
    project = Project(name="cert", workspace_path=str(root))
    db_session.add(project)
    db_session.commit()
    session = SessionModel(
        project_id=project.id,
        name="cert-session",
        status="running",
        is_active=True,
        instance_id="cert-instance",
    )
    db_session.add(session)
    db_session.commit()
    task = Task(
        project_id=project.id,
        title="add divide with zero guard",
        description="add divide with zero guard and cover it",
        status=TaskStatus.PENDING,
        task_subfolder="task-1",
    )
    db_session.add(task)
    db_session.commit()
    link = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.PENDING
    )
    db_session.add(link)
    db_session.commit()
    return project, session, task, link, task_root


def _install_bypass_spies(monkeypatch):
    """Count every provider/planning/repair entry point the lane must not reach."""

    import app.services.orchestration.phases.execution_loop as execution_loop
    import app.tasks.worker as worker

    counters = {
        "resolve_runtime_configuration": 0,
        "validate_runtime_provider_contract": 0,
        "create_agent_runtime": 0,
        "execute_planning_phase": 0,
        "completion_coordinator": 0,
        "step_command_repair": 0,
        "planner_grounding_evidence": 0,
    }

    def spy(name, target, attr):
        original = getattr(target, attr)

        def wrapper(*args, **kwargs):
            counters[name] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(target, attr, wrapper)

    spy("resolve_runtime_configuration", worker, "resolve_runtime_configuration")
    spy(
        "validate_runtime_provider_contract",
        worker,
        "validate_runtime_provider_contract",
    )
    spy("create_agent_runtime", worker, "create_agent_runtime")
    spy("execute_planning_phase", worker, "_execute_planning_phase")
    spy("completion_coordinator", worker._CompletionCoordinator, "complete_task")
    spy(
        "step_command_repair",
        execution_loop,
        "repair_step_commands_with_self_correction",
    )
    return counters


def _state_snapshot(db_session, session_id, task_id, task_root) -> dict:
    payload = CheckpointService(db_session).load_checkpoint(
        session_id, "autosave_latest"
    )
    state = payload.get("orchestration_state") or {}
    envelope = state.get("grounded_execution_envelope") or {}
    task = db_session.get(Task, task_id)
    session = db_session.get(SessionModel, session_id)
    executions = (
        db_session.query(TaskExecution)
        .filter(TaskExecution.task_id == task_id)
        .order_by(TaskExecution.id)
        .all()
    )
    return {
        "task_id": task_id,
        "task_status": str(task.status),
        "session_id": session_id,
        "session_status": session.status,
        "task_execution_ids": [row.id for row in executions],
        "task_execution_attempts": [row.attempt_number for row in executions],
        "envelope_task_execution_id": envelope.get("task_execution_id"),
        "accepted_plan_identity": envelope.get("accepted_plan_identity"),
        "apa_identity": (envelope.get("accepted_path_authority") or {}).get(
            "authority_identity"
        ),
        "apa_grant_paths": [
            grant.get("path")
            for grant in (envelope.get("accepted_path_authority") or {}).get(
                "grants", []
            )
        ],
        "current_step_index": envelope.get("current_step_index"),
        "step_results": envelope.get("execution_results"),
        "changed_files": envelope.get("changed_files"),
        "partial_work": envelope.get("partial_work"),
        "checkpoint_name": "autosave_latest",
        "checkpoint_status": state.get("status"),
        "plan_step_count": len(state.get("plan") or []),
        "ops_py": (task_root / "calc/ops.py").read_text(encoding="utf-8"),
        "test_py": (task_root / "tests/test_ops.py").read_text(encoding="utf-8"),
    }


def test_certification_orchestrator_interrupted_run(
    db_session, isolated_workspace_root, monkeypatch
):
    """Op1 -> checkpoint -> pause -> resume -> op2 -> verification -> Candidate."""

    import app.services.orchestration.phases.execution_loop as execution_loop
    import app.tasks.worker as worker

    project_root = isolated_workspace_root / "cert-orchestrator"
    project, session, task, link, task_root = _seed(db_session, project_root)
    session_id, task_id, link_id = session.id, task.id, link.id
    baseline_marker = project_root / "CANONICAL_BASELINE.txt"
    monkeypatch.setattr(worker, "get_db_session", lambda: db_session)
    counters = _install_bypass_spies(monkeypatch)

    original_ops = execution_loop.ExecutorService.execute_file_ops
    mutation_attempts = {"count": 0}

    def pause_after_first(*args, **kwargs):
        result = original_ops(*args, **kwargs)
        mutation_attempts["count"] += 1
        if mutation_attempts["count"] == 1:
            paused = db_session.get(SessionModel, session_id)
            paused.status = "paused"
            db_session.commit()
        return result

    monkeypatch.setattr(
        execution_loop.ExecutorService, "execute_file_ops", pause_after_first
    )

    started = time.monotonic()
    first = worker.execute_orchestration_task.apply(
        kwargs={
            "session_id": session_id,
            "task_id": task_id,
            "prompt": "add divide with zero guard and cover it",
            "grounded_submission": _submission(),
        }
    ).get(propagate=False)
    interrupted_elapsed = time.monotonic() - started

    assert first["status"] == "cancelled", first
    assert mutation_attempts["count"] == 1
    before = _state_snapshot(db_session, session_id, task_id, task_root)
    _record("interruption_type", "session pause observed at safe step boundary")
    _record("state_before_resume", before)
    _record("first_leg_result", first)
    assert "def divide" in before["ops_py"]
    assert "test_divide" not in before["test_py"]
    assert before["current_step_index"] == 1
    assert before["step_results"] and len(before["step_results"]) == 1
    assert before["changed_files"] == ["calc/ops.py"]
    assert before["accepted_plan_identity"]
    assert before["apa_identity"]

    resumed = db_session.get(SessionModel, session_id)
    resumed.status = "running"
    resumed.is_active = True
    resumed_task = db_session.get(Task, task_id)
    resumed_task.status = TaskStatus.PENDING
    resumed_task.error_message = None
    db_session.get(SessionTask, link_id).status = TaskStatus.PENDING
    db_session.commit()

    started = time.monotonic()
    second = worker.execute_orchestration_task.apply(
        kwargs={
            "session_id": session_id,
            "task_id": task_id,
            "prompt": "add divide with zero guard and cover it",
            "resume_checkpoint_name": "autosave_latest",
        }
    ).get(propagate=False)
    resume_elapsed = time.monotonic() - started

    assert second["public_state"] == "SUCCEEDED", second
    after = _state_snapshot(db_session, session_id, task_id, task_root)
    _record("state_after_resume", after)
    _record("resume_result", second)
    _record(
        "orchestrator_elapsed_seconds",
        {
            "leg_1_until_interruption": round(interrupted_elapsed, 3),
            "leg_2_resume_to_candidate": round(resume_elapsed, 3),
            "total": round(interrupted_elapsed + resume_elapsed, 3),
        },
    )
    _record("bypass_counters", dict(counters))
    _record("mutation_attempts_orchestrator", mutation_attempts["count"])

    # replay prevention: exactly one mutation attempt per operation, ever
    assert mutation_attempts["count"] == 2
    # accepted Plan identity and APA survive the interruption unchanged
    assert after["accepted_plan_identity"] == before["accepted_plan_identity"]
    assert after["apa_identity"] == before["apa_identity"]
    # operation 1's StepResult is durable and was not recomputed
    assert after["step_results"][0] == before["step_results"][0]
    assert len(after["step_results"]) == 2
    # cross-TaskExecution lineage
    assert len(after["task_execution_ids"]) == 2
    assert before["envelope_task_execution_id"] == after["task_execution_ids"][0]
    assert second["task_execution_id"] == after["task_execution_ids"][1]
    # final changed scope observed by Candidate
    assert sorted(after["changed_files"]) == ["calc/ops.py", "tests/test_ops.py"]
    assert second["candidate_identity"]
    assert second["publication_status"] == "not_published"
    assert second["canonical_baseline_mutated"] is False
    assert second["verification"]["status"] == "passed"
    assert second["verification"]["kind"] == "focused_tests"
    _record("verification_evidence", second["verification"])
    # every provider/planning/repair entry point stayed at zero
    assert set(counters.values()) == {0}, counters
    # canonical baseline untouched
    assert baseline_marker.read_text(encoding="utf-8") == "unchanged\n"
    assert (project_root / "calc").exists() is False


def test_certification_orchestrator_source_race_after_checkpoint(
    db_session, isolated_workspace_root, monkeypatch
):
    """Op1 succeeds, op2 grounding goes stale, resume must fail closed."""

    import app.services.orchestration.phases.execution_loop as execution_loop
    import app.tasks.worker as worker

    project_root = isolated_workspace_root / "cert-source-race"
    project, session, task, link, task_root = _seed(db_session, project_root)
    session_id, task_id, link_id = session.id, task.id, link.id
    baseline_marker = project_root / "CANONICAL_BASELINE.txt"
    monkeypatch.setattr(worker, "get_db_session", lambda: db_session)
    counters = _install_bypass_spies(monkeypatch)

    original_ops = execution_loop.ExecutorService.execute_file_ops
    mutation_attempts = {"count": 0}

    def pause_after_first(*args, **kwargs):
        result = original_ops(*args, **kwargs)
        mutation_attempts["count"] += 1
        if mutation_attempts["count"] == 1:
            paused = db_session.get(SessionModel, session_id)
            paused.status = "paused"
            db_session.commit()
        return result

    monkeypatch.setattr(
        execution_loop.ExecutorService, "execute_file_ops", pause_after_first
    )
    first = worker.execute_orchestration_task.apply(
        kwargs={
            "session_id": session_id,
            "task_id": task_id,
            "prompt": "add divide with zero guard and cover it",
            "grounded_submission": _submission(),
        }
    ).get(propagate=False)
    assert first["status"] == "cancelled"

    # concurrent third party rewrites operation 2's grounded region
    raced = (task_root / "tests/test_ops.py").read_text(encoding="utf-8")
    raced = raced.replace(
        "    assert ops.subtract(3, 2) == 1",
        "    assert ops.subtract(10, 4) == 6",
    )
    (task_root / "tests/test_ops.py").write_text(raced, encoding="utf-8")

    resumed = db_session.get(SessionModel, session_id)
    resumed.status = "running"
    resumed.is_active = True
    resumed_task = db_session.get(Task, task_id)
    resumed_task.status = TaskStatus.PENDING
    resumed_task.error_message = None
    db_session.get(SessionTask, link_id).status = TaskStatus.PENDING
    db_session.commit()

    second = worker.execute_orchestration_task.apply(
        kwargs={
            "session_id": session_id,
            "task_id": task_id,
            "prompt": "add divide with zero guard and cover it",
            "resume_checkpoint_name": "autosave_latest",
        }
    ).get(propagate=False)

    _record("source_race_result", second)
    _record("source_race_mutation_attempts", mutation_attempts["count"])
    _record("source_race_bypass_counters", dict(counters))

    assert second["status"] == "failed", second
    assert second["failure"]["family"] == "STALE_GROUNDING"
    assert second["partial_work"] is True
    assert second.get("publication_status") in (None, "not_published")
    assert second.get("candidate_identity") is None
    # operation 1 stays applied in the isolated workspace and is not replayed
    ops_py = (task_root / "calc/ops.py").read_text(encoding="utf-8")
    assert ops_py.count("def divide") == 1
    assert mutation_attempts["count"] == 1
    # operation 2 did not mutate the raced source
    current_test = (task_root / "tests/test_ops.py").read_text(encoding="utf-8")
    assert "test_divide" not in current_test
    assert "ops.subtract(10, 4) == 6" in current_test
    _record(
        "source_race_workspace",
        {"ops_py_divide_count": 1, "test_py_mutated": False},
    )
    # no regrounding, no planning, no repair, no provider
    assert set(counters.values()) == {0}, counters
    assert baseline_marker.read_text(encoding="utf-8") == "unchanged\n"


def _run_pytest(workspace: Path) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_ops.py"],
        cwd=workspace,
        shell=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return completed.returncode, (completed.stdout + completed.stderr)[-1500:]


def test_certification_direct_baseline_same_job(tmp_path: Path):
    """Baseline: identical grounded operations applied directly, same tests."""

    def seed(name: str) -> Path:
        workspace = tmp_path / name
        for path, content in PROJECT_FILES.items():
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return workspace

    # --- baseline happy path -------------------------------------------------
    happy = seed("baseline-happy")
    started = time.monotonic()
    attempts = 0
    for rel, quote, new in (
        ("calc/ops.py", OPS_QUOTE, OPS_NEW),
        ("tests/test_ops.py", TEST_QUOTE, TEST_NEW),
    ):
        target = happy / rel
        target.write_text(
            target.read_text(encoding="utf-8").replace(quote, new), encoding="utf-8"
        )
        attempts += 1
    code, output = _run_pytest(happy)
    baseline_elapsed = time.monotonic() - started
    assert code == 0, output
    _record(
        "baseline_happy_path",
        {
            "outcome": "SUCCEEDED",
            "mutation_attempts": attempts,
            "pytest_returncode": code,
            "elapsed_seconds": round(baseline_elapsed, 3),
        },
    )

    # --- baseline under the same source race ---------------------------------
    raced = seed("baseline-raced")
    target = raced / "calc/ops.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace(OPS_QUOTE, OPS_NEW),
        encoding="utf-8",
    )
    test_file = raced / "tests/test_ops.py"
    test_file.write_text(
        test_file.read_text(encoding="utf-8").replace(
            "    assert ops.subtract(3, 2) == 1",
            "    assert ops.subtract(10, 4) == 6",
        ),
        encoding="utf-8",
    )
    before_race = test_file.read_text(encoding="utf-8")
    test_file.write_text(before_race.replace(TEST_QUOTE, TEST_NEW), encoding="utf-8")
    after_race = test_file.read_text(encoding="utf-8")
    race_code, race_output = _run_pytest(raced)
    silent_noop = after_race == before_race
    _record(
        "baseline_source_race",
        {
            "operation_2_silently_no_op": silent_noop,
            "failure_signalled_to_caller": not silent_noop,
            "pytest_returncode": race_code,
            "pytest_passed_despite_missing_operation": race_code == 0,
            "partial_work_flag_available": False,
            "checkpoint_available": False,
            "replay_prevention": "none",
            "output_tail": race_output[-400:],
        },
    )
    # the direct baseline applies a stale replacement as a silent no-op and
    # still reports a green test run for an incomplete job
    assert silent_noop is True
    assert race_code == 0


def test_certification_candidate_independent_value_probe(
    db_session, isolated_workspace_root, monkeypatch
):
    """Does Candidate catch scope the executor itself accepted?

    An unrelated file is mutated inside the isolated workspace after the last
    grounded operation, i.e. change that no grounded operation authorized. If
    Candidate is redundant with executor success it will still accept.
    """

    import app.services.orchestration.phases.execution_loop as execution_loop
    import app.tasks.worker as worker

    project_root = isolated_workspace_root / "cert-candidate-probe"
    project, session, task, link, task_root = _seed(db_session, project_root)
    session_id, task_id = session.id, task.id
    monkeypatch.setattr(worker, "get_db_session", lambda: db_session)
    counters = _install_bypass_spies(monkeypatch)

    original_ops = execution_loop.ExecutorService.execute_file_ops
    calls = {"count": 0}

    def stray_write_after_last_op(*args, **kwargs):
        result = original_ops(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 2:
            (task_root / "calc" / "stray.py").write_text(
                "STRAY = True\n", encoding="utf-8"
            )
        return result

    monkeypatch.setattr(
        execution_loop.ExecutorService, "execute_file_ops", stray_write_after_last_op
    )
    result = worker.execute_orchestration_task.apply(
        kwargs={
            "session_id": session_id,
            "task_id": task_id,
            "prompt": "add divide with zero guard and cover it",
            "grounded_submission": _submission(),
        }
    ).get(propagate=False)

    _record(
        "candidate_independent_value_probe",
        {
            "unauthorized_workspace_change": "calc/stray.py",
            "executor_reported_success_for_both_operations": calls["count"] == 2,
            "terminal_status": result.get("status"),
            "public_state": result.get("public_state"),
            "failure": result.get("failure"),
            "candidate_identity": result.get("candidate_identity"),
            "verification": result.get("verification"),
            "raw_result": dict(result),
            "bypass_counters": dict(counters),
        },
    )
    assert calls["count"] == 2
    assert set(counters.values()) == {0}
