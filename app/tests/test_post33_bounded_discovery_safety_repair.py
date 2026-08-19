"""Provider-free regressions for the POST33 bounded discovery safety gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app.services.agents.openclaw_service import OpenClawSessionError
from app.services.orchestration.execution.executor_workspace_binding import (
    bind_openclaw_workspace,
)
from app.services.orchestration.coordinators.failure_coordinator import (
    FailureCoordinator,
)
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryContractError,
    fail_closed_discovery,
    run_discovery_stage,
)
from app.services.orchestration.recovery.failure_classifier import FailureClassifier
from app.services.orchestration.recovery.recovery_policy import PolicyTable
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.session.execution_policy import is_retry_exempt_category

from app.tests.test_phase14b2_failure_coordinator import (
    _NOOP,
    _RetryCapableSelfTask,
    _seed_ctx,
)


def _binding_context(project_workspace: Path, runtime_workspace: Path):
    return SimpleNamespace(
        project_workspace=project_workspace,
        runtime_workspace=runtime_workspace,
        project_id=12,
        task_execution_id=302,
        is_sandboxed=True,
    )


def test_runtime_binding_blocks_provider_bootstrap_scaffold(tmp_path: Path):
    project_workspace = tmp_path / "project"
    runtime_workspace = tmp_path / "runtime"
    project_workspace.mkdir()
    runtime_workspace.mkdir()
    (runtime_workspace / "existing_task_source.py").write_text(
        "def existing_task_source():\n    return 1\n", encoding="utf-8"
    )
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {},
                    "list": [
                        {"id": "orchestrator", "workspace": str(project_workspace)}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    original_config = config_path.read_bytes()

    binding = bind_openclaw_workspace(
        _binding_context(project_workspace, runtime_workspace),
        real_config_path=config_path,
    )
    try:
        bound_config = json.loads(binding.config_path.read_text(encoding="utf-8"))
        defaults = bound_config["agents"]["defaults"]
        agent = bound_config["agents"]["list"][0]

        # Provider-free reconstruction of OpenClaw's bootstrap boundary: the
        # provider creates these files in its configured workspace unless the
        # ephemeral config explicitly disables bootstrap materialization.
        assert defaults["skipBootstrap"] is True
        assert "skipBootstrap" not in agent
        if not defaults["skipBootstrap"]:
            for name in (
                "HEARTBEAT.md",
                "IDENTITY.md",
                "SOUL.md",
                "TOOLS.md",
                "USER.md",
            ):
                (runtime_workspace / name).write_text(
                    "provider scaffold", encoding="utf-8"
                )
        assert not (runtime_workspace / "HEARTBEAT.md").exists()
        assert (runtime_workspace / "existing_task_source.py").exists()
        assert Path(binding.environment["OPENCLAW_STATE_DIR"]).is_dir()
        state_dir = Path(binding.environment["OPENCLAW_STATE_DIR"])
    finally:
        binding.release()
    assert config_path.read_bytes() == original_config
    assert not state_dir.exists()


def test_current_openclaw_parser_accepts_defaults_bootstrap_control(
    tmp_path: Path,
):
    """Exercise the installed parser without starting a provider/model call."""

    openclaw = shutil.which("openclaw")
    if openclaw is None:
        pytest.skip("OpenClaw CLI is not installed in this test environment")

    project_workspace = tmp_path / "project"
    runtime_workspace = tmp_path / "runtime"
    project_workspace.mkdir()
    runtime_workspace.mkdir()
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {},
                    "list": [
                        {"id": "orchestrator", "workspace": str(project_workspace)}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    original_config = config_path.read_bytes()
    binding = bind_openclaw_workspace(
        _binding_context(project_workspace, runtime_workspace),
        real_config_path=config_path,
    )
    try:
        env = os.environ.copy()
        env.update(binding.environment)
        result = subprocess.run(
            [openclaw, "config", "validate", "--json"],
            cwd=runtime_workspace,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["valid"] is True
        bound = json.loads(binding.config_path.read_text(encoding="utf-8"))
        assert bound["agents"]["defaults"]["skipBootstrap"] is True
        assert all("skipBootstrap" not in agent for agent in bound["agents"]["list"])
        assert not tuple(runtime_workspace.iterdir())
        assert Path(binding.environment["OPENCLAW_STATE_DIR"]).joinpath("logs").is_dir()
    finally:
        binding.release()
    assert config_path.read_bytes() == original_config


def test_discovery_failure_is_terminal_and_not_reflection_retryable():
    error = DiscoveryContractError("canonical_workspace_pollution_detected")
    event = FailureClassifier.classify(error, SimpleNamespace())
    assert event.failure_class == "discovery_terminal_failure"
    assert PolicyTable.lookup("discovery_terminal_failure").strategy == "terminal"
    assert is_retry_exempt_category("discovery_terminal_failure") is True

    reflection_calls = []
    decision = RecoveryStrategyRegistry.route(
        event,
        orchestration_state=SimpleNamespace(),
        llm_callable=lambda _prompt: reflection_calls.append(True),
    )
    assert decision.strategy == "terminal"
    assert reflection_calls == []


@pytest.mark.parametrize(
    "reason",
    [
        "discovery_output_not_json",
        "discovery_action_unsupported",
        "discovery_path_unsafe",
        "discovery_path_symlink",
        "discovery_search_execution_failed",
        "provider_timeout",
        "provider_unavailable",
        "canonical_workspace_pollution_detected",
    ],
)
def test_bounded_discovery_failure_families_are_terminal(reason: str):
    event = FailureClassifier.classify(
        DiscoveryContractError(reason), SimpleNamespace()
    )
    assert event.failure_class == "discovery_terminal_failure"
    assert PolicyTable.lookup(event.failure_class).strategy == "terminal"


def test_discovery_provider_timeout_is_wrapped_without_a_second_call(tmp_path: Path):
    calls = []

    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("provider timeout")

    ctx = SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=object(),
        prompt="inspect the existing defect",
        orchestration_state=SimpleNamespace(project_context="", project_dir=tmp_path),
        emit_live=lambda *args, **kwargs: None,
        session_id=162,
        task_id=219,
        task_execution_id=302,
    )
    with pytest.raises(DiscoveryContractError, match="discovery_provider_failed"):
        run_discovery_stage(
            ctx=ctx,
            planning_timeout_seconds=120,
            extract_structured_text=lambda value: str(value),
            planner_service=_Planner,
            emit_phase_event=lambda *args, **kwargs: None,
        )
    assert calls == [1]


def test_2r_openclaw_timeout_shape_stays_terminal_through_worker_policy(
    db_session, tmp_path: Path
):
    """Reproduce the retained OpenClaw timeout exception, not a generic timeout."""

    calls = []
    diagnostics = {
        "stage": "read_only_discovery",
        "diagnostic_label": "PLANNING_DISCOVERY",
        "timeout_seconds": 120,
        "timeout_with_cleanup_seconds": 150,
        "duration_seconds": 150.323,
        "timed_out": True,
        "timeout_boundary": "planning_wait_for",
        "activity_classification": "provider_process_timeout",
        "cleanup_status": "completed",
        "return_code": -9,
        "stdout_chars": 0,
    }
    provider_timeout = OpenClawSessionError(
        "Task execution failed: Task timed out after 120s"
    )
    provider_timeout.runtime_diagnostics = diagnostics

    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            calls.append(1)
            raise provider_timeout

    discovery_ctx = SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=object(),
        prompt="inspect the existing defect",
        orchestration_state=SimpleNamespace(project_context="", project_dir=tmp_path),
        emit_live=lambda *args, **kwargs: None,
        session_id=163,
        task_id=220,
        task_execution_id=303,
    )
    with pytest.raises(DiscoveryContractError) as caught:
        run_discovery_stage(
            ctx=discovery_ctx,
            planning_timeout_seconds=120,
            extract_structured_text=lambda value: str(value),
            planner_service=_Planner,
            emit_phase_event=lambda *args, **kwargs: None,
        )

    error = caught.value
    assert calls == [1]
    assert error.failure_category == "discovery_terminal_failure"
    assert error.runtime_diagnostics == diagnostics

    ctx, session, task, execution = _seed_ctx(
        db_session, execution_mode="automatic", plan_position=1
    )
    ctx.error_handler = SimpleNamespace(should_retry=lambda _exc, _scope: True)
    queue = []
    reflection_calls = []
    event = FailureClassifier.classify(error, SimpleNamespace())
    decision = RecoveryStrategyRegistry.route(
        event,
        orchestration_state=SimpleNamespace(),
        llm_callable=lambda _prompt: reflection_calls.append(True),
    )
    assert decision.strategy == "terminal"
    assert reflection_calls == []

    with pytest.raises(DiscoveryContractError):
        FailureCoordinator().handle_failure(
            self_task=_RetryCapableSelfTask(),
            ctx=ctx,
            exc=error,
            get_latest_session_task_link_fn=lambda *args, **kwargs: ctx.session_task_link,
            write_project_state_snapshot_fn=_NOOP,
            save_orchestration_checkpoint_fn=_NOOP,
            record_live_log_fn=_NOOP,
            queue_task_for_session_fn=lambda **kwargs: queue.append(kwargs),
        )

    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(execution)
    assert queue == []
    assert session.status == "paused"
    assert task.status.value == "failed"
    assert execution.status.value == "failed"
    assert execution.failure_category == "discovery_terminal_failure"


def test_failure_coordinator_cannot_retry_terminal_discovery_failure(db_session):
    ctx, session, task, execution = _seed_ctx(
        db_session, execution_mode="automatic", plan_position=1
    )
    ctx.error_handler = SimpleNamespace(
        should_retry=lambda _exc, _scope: True,
    )
    queue = []
    error = DiscoveryContractError("canonical_workspace_pollution_detected")

    with pytest.raises(DiscoveryContractError):
        FailureCoordinator().handle_failure(
            self_task=_RetryCapableSelfTask(),
            ctx=ctx,
            exc=error,
            get_latest_session_task_link_fn=lambda *args, **kwargs: ctx.session_task_link,
            write_project_state_snapshot_fn=_NOOP,
            save_orchestration_checkpoint_fn=_NOOP,
            record_live_log_fn=_NOOP,
            queue_task_for_session_fn=lambda **kwargs: queue.append(kwargs),
        )

    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(execution)
    assert queue == []
    assert session.status == "paused"
    assert task.status.value == "failed"
    assert execution.status.value == "failed"
    assert execution.failure_category == "discovery_terminal_failure"


def test_successful_discovery_still_completes_one_observation():
    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            return {"status": "completed", "output": '{"action":"stop"}'}

    ctx = SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=object(),
        prompt="inspect the existing defect",
        orchestration_state=SimpleNamespace(project_context="", project_dir=Path.cwd()),
        emit_live=lambda *args, **kwargs: None,
        session_id=162,
        task_id=219,
        task_execution_id=302,
    )
    observation = run_discovery_stage(
        ctx=ctx,
        planning_timeout_seconds=120,
        extract_structured_text=lambda value: str(value),
        planner_service=_Planner,
        emit_phase_event=lambda *args, **kwargs: None,
    )
    assert observation.action == "stop"
    assert ctx.read_only_observation is observation
    assert ctx.read_only_discovery_completed is True


def test_fail_closed_discovery_returns_terminal_worker_result():
    ctx = SimpleNamespace(
        orchestration_state=SimpleNamespace(),
        emit_live=lambda *args, **kwargs: None,
        restore_workspace_snapshot_if_needed=None,
    )
    result = fail_closed_discovery(
        ctx=ctx,
        reason="read_only_discovery_failed_closed",
        detail="canonical_workspace_pollution_detected",
        aborted_status="aborted",
        emit_phase_event=lambda *args, **kwargs: None,
        finalize_failure=lambda **kwargs: None,
    )
    assert result["status"] == "failed"
    assert result["terminal_failure"] is True
    assert result["failure_category"] == "discovery_terminal_failure"


def test_failed_provider_result_becomes_bounded_discovery_error(tmp_path: Path):
    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            return {
                "status": "failed",
                "error": (
                    "Provider initialization escaped the declared Runtime Workspace: "
                    "canonical_workspace_pollution_detected"
                ),
                "failure_category": "runtime_safety_stop",
            }

    ctx = SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=object(),
        prompt="inspect the existing defect",
        orchestration_state=SimpleNamespace(project_context="", project_dir=tmp_path),
        emit_live=lambda *args, **kwargs: None,
        session_id=162,
        task_id=219,
        task_execution_id=302,
    )
    with pytest.raises(
        DiscoveryContractError, match="canonical_workspace_pollution_detected"
    ):
        run_discovery_stage(
            ctx=ctx,
            planning_timeout_seconds=120,
            extract_structured_text=lambda value: str(value),
            planner_service=_Planner,
            emit_phase_event=lambda *args, **kwargs: None,
        )
