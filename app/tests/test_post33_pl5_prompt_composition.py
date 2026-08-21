from types import SimpleNamespace

from app.services.orchestration.context.assembly import assemble_planning_prompt
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryObservation,
    SearchHit,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)


def _planning_context(tmp_path):
    source_path = tmp_path / "app/tasks/maintenance.py"
    test_path = tmp_path / "app/tests/test_maintenance.py"
    source_path.parent.mkdir(parents=True)
    test_path.parent.mkdir(parents=True)
    source_path.write_text(
        "def scheduled_task_execution(value):\n    return value\n",
        encoding="utf-8",
    )
    test_path.write_text(
        "def test_scheduled_task_execution():\n    assert True\n",
        encoding="utf-8",
    )
    task = (
        "Change the unique `scheduled_task_execution(value)` target in "
        "app/tasks/maintenance.py and preserve focused coverage in "
        "app/tests/test_maintenance.py."
    )
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description=task,
        expected_paths=("app/tasks/maintenance.py", "app/tests/test_maintenance.py"),
        workspace_identity=tmp_path,
    )
    state = SimpleNamespace(
        project_dir=str(tmp_path),
        project_workspace_path=str(tmp_path),
        project_context=(
            "Architecture: maintenance tasks live under app/tasks.\n"
            "Operator guidance\n"
            "  - Preserve focused coverage and existing behavior.\n"
        ),
        phase_history=[{"phase": "previous", "status": "completed"}],
        validation_history=[{"phase": "validation", "status": "accepted"}],
        session_id=None,
        task_id=None,
        project_name="orchestrator",
    )
    ctx = SimpleNamespace(
        orchestration_state=state,
        db=None,
        execution_profile="full_lifecycle",
        prompt=task,
        workflow_profile="default",
        planning_adaptation_profile="openclaw_default",
        planner_source_materialization=materialization,
        read_only_observation=DiscoveryObservation(
            action="search_text",
            status="completed",
            hits=(
                SearchHit(
                    "app/tasks/maintenance.py",
                    1,
                    "def scheduled_task_execution(value):",
                ),
            ),
        ),
    )
    return ctx, task, materialization


def test_actual_assembled_full_prompt_keeps_evidence_and_uses_pl3_fields(tmp_path):
    ctx, _, _ = _planning_context(tmp_path)
    prompt = assemble_planning_prompt(
        ctx,
        {"has_existing_files": True, "file_count": 2, "source_file_count": 2},
    )
    prompt = PlannerService.apply_prompt_profile(prompt, "local_qwen_json_array")

    assert "CURRENT SOURCE MATERIALIZATION" in prompt
    assert "app/tasks/maintenance.py" in prompt
    assert "target_id:" in prompt
    assert "READ-ONLY OBSERVATION" in prompt
    assert "Preserve focused coverage" in prompt
    assert '"step_number": 1' not in prompt
    assert '"rollback":' not in prompt
    assert "step_number values must be unique" not in prompt
    assert "rollback must always be present" not in prompt
    assert "optional `step_number`, `rollback`, and `ops`" in prompt


def test_minimal_and_ultra_retries_keep_the_same_pl3_field_contract(tmp_path):
    _, task, materialization = _planning_context(tmp_path)
    kwargs = {
        "project_dir": tmp_path,
        "prompt_profile": "local_qwen_json_array",
        "workspace_has_existing_files": True,
        "project_context": "Operator guidance\n  - Preserve focused coverage.\n",
        "source_materialization": materialization,
    }
    minimal = PlannerService.build_minimal_planning_prompt(task, **kwargs)
    ultra = PlannerService.build_ultra_minimal_planning_prompt(task, **kwargs)

    for prompt in (minimal, ultra):
        assert "CURRENT SOURCE MATERIALIZATION" in prompt
        assert "optional keys are step_number, rollback, and ops" in prompt
        assert '"step_number": 1' not in prompt
        assert '"rollback":' not in prompt
        assert "rollback must always be present" not in prompt
