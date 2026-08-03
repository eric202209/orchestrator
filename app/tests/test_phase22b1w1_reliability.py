"""Focused regressions for the Phase 22B-1W1 reliability boundaries."""

from __future__ import annotations

import json
import logging

import pytest

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.config import settings
from app.services.agents.agent_runtime import BackendRole, create_agent_runtime
from app.services.orchestration.execution.step_support import (
    repair_step_commands_with_self_correction,
)
from app.services.orchestration.execution.executor_workspace_binding import (
    bind_openclaw_workspace,
)
from app.services.orchestration.execution.runtime_context import (
    RuntimeExecutorContext,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.validation.runtime_pollution_guard import (
    detect_runtime_pollution,
    snapshot_workspace_entry_evidence,
    snapshot_top_level_entries,
)
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.agents.openclaw_service import OpenClawWorkspaceBindingError
from app.services.session.execution_policy import classify_failure
from app.services.session.session_inspection_service import (
    _extract_stop_reasons,
    derive_orchestration_state_block,
    get_session_timeline_payload,
)
from app.services.workspace.task_sandbox_allocator import (
    TaskSandbox,
    dispose_task_sandbox,
)


def _write_config(path, project_workspace):
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {"workspace": str(project_workspace)},
                    "list": [
                        {
                            "id": "orchestrator",
                            "workspace": str(project_workspace),
                            "agentDir": "/root/.openclaw/agents/orchestrator/agent",
                        }
                    ],
                },
                "session": {"maintenance": {"mode": "warn"}},
            }
        ),
        encoding="utf-8",
    )


def test_runtime_binding_moves_all_provider_state_out_of_canonical_root(tmp_path):
    project_workspace = tmp_path / "canonical"
    project_workspace.mkdir()
    runtime_workspace = tmp_path / "runtime"
    runtime_workspace.mkdir()
    config_path = tmp_path / "openclaw.json"
    _write_config(config_path, project_workspace)
    context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime_workspace,
        project_workspace=project_workspace,
        project_id=12,
        task_execution_id=245,
        runtime_root=tmp_path,
        sandbox=object(),
    )

    binding = bind_openclaw_workspace(context, real_config_path=config_path)
    try:
        bound = json.loads(binding.config_path.read_text(encoding="utf-8"))
        agent = bound["agents"]["list"][0]
        assert agent["workspace"] == str(runtime_workspace)
        assert bound["agents"]["defaults"]["workspace"] == str(runtime_workspace)
        assert agent["agentDir"] != "/root/.openclaw/agents/orchestrator/agent"
        assert str(project_workspace) not in json.dumps(bound)
        assert binding.environment["OPENCLAW_CONFIG_PATH"] == str(binding.config_path)
        assert binding.environment["OPENCLAW_STATE_DIR"] != str(project_workspace)
    finally:
        binding.release()


def test_runtime_pollution_evidence_names_boundary_hash_and_cleanup(tmp_path):
    canonical = tmp_path / "canonical"
    runtime = tmp_path / "runtime"
    canonical.mkdir()
    runtime.mkdir()
    before = snapshot_top_level_entries(canonical)
    scaffold = canonical / "SOUL.md"
    scaffold.write_text("provider scaffold", encoding="utf-8")
    after = snapshot_top_level_entries(canonical)

    result = detect_runtime_pollution(
        before=before,
        after=after,
        canonical_root=canonical,
        runtime_workspace=runtime,
    )

    assert result["category"] == "provider_scaffold_outside_runtime_workspace"
    evidence = result["entries"][0]
    assert evidence["path"] == str(scaffold)
    assert evidence["creator_boundary"] == "canonical_project_root"
    assert evidence["canonical_or_sandbox"] == "canonical"
    assert evidence["after_sha256"]
    assert evidence["ignored"] in {True, False}
    assert evidence["cleanup_safe"] is False
    assert result["execution_must_stop"] is True


def test_canonical_scaffold_fails_closed_while_runtime_scaffold_is_contained(
    tmp_path,
):
    canonical = tmp_path / "canonical"
    runtime = tmp_path / "runtime"
    canonical.mkdir()
    runtime.mkdir()
    service = object.__new__(OpenClawSessionService)
    service.execution_cwd_override = str(runtime)
    service._log_entry = lambda *args, **kwargs: None

    canonical_before = snapshot_workspace_entry_evidence(canonical)
    runtime_before = snapshot_workspace_entry_evidence(runtime)
    (canonical / "SOUL.md").write_text("escaped", encoding="utf-8")
    (runtime / "HEARTBEAT.md").write_text("contained", encoding="utf-8")

    result = {}
    service._record_runtime_pollution(
        result,
        expected_project_root=str(canonical),
        pre_execution_top_level=canonical_before,
        runtime_workspace=str(runtime),
        runtime_pre_execution_top_level=runtime_before,
    )

    pollution = result["runtime_pollution"]
    assert result["status"] == "failed"
    assert result["failure_category"] == "runtime_safety_stop"
    assert pollution["category"] == "provider_scaffold_outside_runtime_workspace"
    boundaries = {entry["creator_boundary"]: entry for entry in pollution["entries"]}
    assert boundaries["canonical_project_root"]["cleanup_safe"] is False
    assert boundaries["runtime_workspace"]["cleanup_safe"] is True


def test_nested_step_debug_repair_keeps_runtime_binding_until_provider_returns(
    db_session, tmp_path, monkeypatch
):
    """Reproduce E3 through the role-runtime construction seam.

    The primary execution runtime returns malformed provider prose, causing
    the existing step-repair fallback to construct a fresh debug runtime.
    That runtime must use the exact parent RuntimeExecutorContext for cwd and
    ephemeral OpenClaw state for the whole nested provider invocation.
    """

    canonical = tmp_path / "canonical"
    runtime = tmp_path / "runtime"
    canonical.mkdir()
    runtime.mkdir()
    (canonical / "tracked-source.py").write_text("sentinel\n", encoding="utf-8")
    config_path = tmp_path / "openclaw.json"
    _write_config(config_path, canonical)

    project = Project(
        name="W1 nested repair project",
        workspace_path=str(canonical),
    )
    db_session.add(project)
    db_session.flush()
    session = SessionModel(name="W1 nested repair session", project_id=project.id)
    task = Task(project_id=project.id, title="W1 nested repair task")
    db_session.add_all([session, task])
    db_session.commit()
    db_session.refresh(session)
    db_session.refresh(task)

    monkeypatch.setattr(settings, "DEBUG_REPAIR_BACKEND", "local_openclaw")
    monkeypatch.setattr(
        OpenClawSessionService,
        "_openclaw_config_path",
        lambda self: config_path,
    )
    primary = create_agent_runtime(
        db_session,
        session.id,
        task.id,
        role=BackendRole.EXECUTION,
        backend_override="local_openclaw",
    )
    primary.project_id = project.id
    primary.task_execution_id = 249
    context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime,
        project_workspace=canonical,
        project_id=project.id,
        task_execution_id=249,
        runtime_root=tmp_path,
        sandbox=object(),
    )
    primary.execution_cwd_override = str(runtime)
    primary.bind_runtime_workspace(context)

    canonical_before = snapshot_workspace_entry_evidence(canonical)
    observed = {}

    async def primary_response(prompt, timeout_seconds=120):
        del prompt, timeout_seconds
        return {"output": "provider prose, not JSON", "error": "non_json_response"}

    async def debug_response(self, prompt, timeout_seconds=120):
        del prompt, timeout_seconds
        observed.update(
            {
                "runtime": self,
                "context": self.runtime_executor_context,
                "task_execution_id": self.task_execution_id,
                "cwd": self._resolve_execution_cwd(),
                "binding": self._workspace_binding,
                "config_path": self._openclaw_config_path_override,
                "config_exists_during": self._openclaw_config_path_override.exists(),
                "environment": dict(self._workspace_binding.environment),
            }
        )
        selected = json.loads(observed["config_path"].read_text(encoding="utf-8"))
        observed["selected_agent_workspace"] = selected["agents"]["list"][0][
            "workspace"
        ]
        observed["selected_agent_dir"] = selected["agents"]["list"][0]["agentDir"]
        observed["session_store"] = selected["session"]["store"]
        self._last_selected_openclaw_agent_id = selected["agents"]["list"][0]["id"]
        self._validate_runtime_invocation_boundary(observed["cwd"])
        (runtime / "provider-scaffold").mkdir()
        (runtime / "provider-scaffold" / "state.json").write_text(
            "{}\n", encoding="utf-8"
        )
        return {
            "output": json.dumps(
                {
                    "description": "repair the malformed step",
                    "commands": ["python -m pytest -q"],
                    "verification": "python -m pytest -q",
                }
            )
        }

    primary.execute_task = primary_response
    monkeypatch.setattr(OpenClawSessionService, "execute_task", debug_response)
    monkeypatch.setattr(
        "app.services.orchestration.execution.step_support.render_adapted_runtime_prompt",
        lambda *args, **kwargs: kwargs.get("prompt_body") or args[0],
    )

    repaired = repair_step_commands_with_self_correction(
        runtime_service=primary,
        db=db_session,
        session_id=session.id,
        task_id=task.id,
        session_instance_id=session.instance_id,
        task_prompt="repair the malformed execution step",
        step={"description": "execute", "commands": []},
        step_index=1,
        project_dir=runtime,
        prior_results_summary="step 1 succeeded",
        project_context="",
        logger_obj=logging.getLogger("phase22b1w1-nested-repair-test"),
        extract_structured_text=lambda value: str(value or ""),
        normalize_step=lambda data, *_args: data,
        record_live_log=lambda *args, **kwargs: None,
    )

    assert repaired["commands"] == ["python -m pytest -q"]
    assert isinstance(observed["runtime"], OpenClawSessionService)
    assert observed["runtime"] is not primary
    assert observed["context"] is context
    assert observed["task_execution_id"] == 249
    assert observed["cwd"] == str(runtime)
    assert observed["selected_agent_workspace"] == str(runtime)
    assert observed["selected_agent_dir"] != str(canonical)
    assert observed["session_store"].startswith(
        observed["environment"]["OPENCLAW_STATE_DIR"]
    )
    assert observed["binding"] is not None
    assert observed["config_exists_during"] is True
    assert observed["environment"]["OPENCLAW_CONFIG_PATH"] == str(
        observed["config_path"]
    )
    assert observed["environment"]["OPENCLAW_STATE_DIR"] != str(canonical)
    assert snapshot_workspace_entry_evidence(canonical) == canonical_before
    assert (runtime / "provider-scaffold" / "state.json").exists()
    assert observed["runtime"]._workspace_binding is None
    assert observed["runtime"]._openclaw_config_path_override is None
    assert observed["runtime"].runtime_executor_context is None
    assert not observed["config_path"].exists()
    disposal = dispose_task_sandbox(
        TaskSandbox(
            path=runtime,
            project_id=project.id,
            task_execution_id=249,
            executor="openclaw",
            is_git=False,
        )
    )
    assert disposal.cleanup_complete is True
    assert not runtime.exists()


def test_sandboxed_context_without_cwd_binding_fails_before_provider_init(tmp_path):
    canonical = tmp_path / "canonical"
    runtime = tmp_path / "runtime"
    canonical.mkdir()
    runtime.mkdir()
    context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime,
        project_workspace=canonical,
        project_id=12,
        task_execution_id=249,
        sandbox=object(),
    )
    service = object.__new__(OpenClawSessionService)
    service.execution_cwd_override = None
    service._runtime_executor_context = context
    service._workspace_binding = None
    service._openclaw_config_path_override = None
    service.backend_role = "debug_repair"

    with pytest.raises(OpenClawWorkspaceBindingError) as raised:
        service._validate_runtime_invocation_boundary(str(canonical))

    diagnostics = raised.value.runtime_diagnostics
    assert diagnostics["failure_category"] == "runtime_workspace_binding_mismatch"
    assert diagnostics["runtime_role"] == "debug_repair"
    assert diagnostics["invocation_stage"] == "provider_initialization"
    assert diagnostics["expected_workspace"] == str(runtime.resolve())
    assert diagnostics["effective_workspace"] == str(canonical.resolve())
    assert diagnostics["binding_lifecycle_state"] == "context_present_binding_missing"


def test_planning_repair_timeout_gets_typed_terminal_cause():
    assert (
        classify_failure(
            "Planning repair timed out after 120s",
            "local_openclaw",
            {
                "failure_phase": "planning",
                "timeout_boundary": "planner_wait_for",
            },
        )
        == "planning_repair_timeout"
    )


def test_provider_timeout_is_not_malformed_planning_output():
    assert (
        classify_failure(
            "Prompt invocation timed out after 120s",
            "local_openclaw",
            {
                "failure_phase": "planning",
                "provider_failure_classification": "provider_timeout",
            },
        )
        == "provider_timeout"
    )


def test_repair_timeout_diagnostics_retain_provider_endpoint_and_context():
    class Runtime:
        def get_backend_metadata(self):
            return {
                "backend": "openai_chat_completions",
                "model_family": "qwen-local",
                "capabilities": {"max_context_tokens": 200000},
            }

    diagnostics = PlannerService._repair_invocation_diagnostics(
        Runtime(), "grounded repair prompt", 120
    )

    assert diagnostics["provider_endpoint"].endswith("/v1/chat/completions")
    assert diagnostics["provider_model"] == "qwen-local"
    assert diagnostics["provider_context_window_tokens"] == 200000
    assert diagnostics["repair_context_estimated_tokens"] > 0


def test_operator_pause_and_natural_failure_have_distinct_projection_causes(
    db_session,
):
    project = Project(name="W1 cause projection")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        name="W1 cause session", project_id=project.id, status="paused"
    )
    task = Task(project_id=project.id, title="W1 cause task")
    db_session.add_all([session, task])
    db_session.flush()
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
        failure_category="planning_repair_timeout",
    )
    db_session.add(execution)
    db_session.add(
        LogEntry(
            session_id=session.id,
            level="INFO",
            message="Session paused by operator",
            log_metadata=json.dumps(
                {
                    "event_type": "session_paused",
                    "failure_cause": "operator_requested_pause",
                }
            ),
        )
    )
    db_session.commit()

    reasons, category = _extract_stop_reasons(db_session, session)
    state = derive_orchestration_state_block(
        db_session, session, latest_task_execution=execution
    )
    timeline = get_session_timeline_payload(db_session, session.id)

    assert category == "operator_paused"
    assert "Session paused by operator." in reasons
    assert state["terminal_reason"] == "planning_repair_timeout"
    timeline_events = [
        event for phase in timeline["phases"] for event in phase["events"]
    ]
    assert any(
        event.get("cause") == "operator_requested_pause" for event in timeline_events
    )
    assert any(
        event.get("cause") == "planning_repair_timeout" for event in timeline_events
    )
