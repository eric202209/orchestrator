"""Focused regressions for the Phase 22B-1W1 reliability boundaries."""

from __future__ import annotations

import json

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
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
from app.services.session.execution_policy import classify_failure
from app.services.session.session_inspection_service import (
    _extract_stop_reasons,
    derive_orchestration_state_block,
    get_session_timeline_payload,
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
