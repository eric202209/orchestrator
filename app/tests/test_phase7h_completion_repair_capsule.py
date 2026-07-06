from __future__ import annotations

import json
import logging
import subprocess
from types import SimpleNamespace

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.phases.completion_repair_capsule import (
    MAX_RELEVANT_FILES,
    build_bounded_completion_repair_prompt,
    build_completion_repair_capsule,
)
from app.services.orchestration.phases.completion_repair import (
    _extract_completion_repair_step,
    _salvage_completion_repair_json_text,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.orchestration.phases.completion_flow import (
    _attempt_completion_repair,
    _extract_completion_repair_json_text,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.integrity import scan_python_test_text
from app.services.orchestration.prompt_templates import OrchestrationState, StepResult


class _FakeRuntime:
    def __init__(self, output="not json"):
        self.prompts = []
        self.outputs = list(output) if isinstance(output, list) else [output]

    async def execute_task(self, prompt, timeout_seconds=None):
        self.prompts.append(str(prompt))
        if len(self.outputs) > 1:
            return {"output": self.outputs.pop(0)}
        return {"output": self.outputs[0]}

    def get_backend_metadata(self):
        return {"backend": "fake", "model_family": "test"}


def _make_state(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "src" / "utils").mkdir(parents=True)
    (project_dir / "tests").mkdir()
    (project_dir / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project_dir / "src" / "utils" / "format.py").write_text(
        "def format_value(v):\n    return str(v)\n", encoding="utf-8"
    )
    (project_dir / "tests" / "test_format.py").write_text(
        "def test_format():\n    assert True\n", encoding="utf-8"
    )
    state = OrchestrationState(
        session_id="1",
        task_description="Build formatter",
        project_name="Phase 7H",
        task_id=2,
        plan=[
            {"description": "Create formatter", "expected_files": ["src/main.py"]},
            {
                "description": "Add tests",
                "expected_files": ["tests/test_format.py"],
            },
        ],
    )
    state._project_dir_override = str(project_dir)
    state.execution_results = [
        StepResult(
            step_number=1,
            status="success",
            output="Created main",
            files_changed=["src/main.py"],
        ),
        StepResult(
            step_number=2,
            status="failed",
            output="Failed tests",
            files_changed=["tests/test_format.py", "src/utils/format.py"],
        ),
    ]
    return state


def _completion_validation():
    return SimpleNamespace(
        stage="task_completion",
        status="repair_required",
        repairable=True,
        profile="implementation",
        reasons=[
            "Core implementation file src/main.py is present but import in tests/test_format.py failed",
            "Expected helper src/utils/format.py to load",
        ],
        details={
            "expected_core_files": ["src/main.py", "missing.py"],
            "verification_output_preview": "ImportError in tests/test_format.py",
        },
    )


def test_completion_capsule_selects_expected_reason_and_last_step_files(tmp_path):
    state = _make_state(tmp_path)

    capsule = build_completion_repair_capsule(
        task_prompt="Build a formatter with tests",
        completion_validation=_completion_validation(),
        orchestration_state=state,
    )

    assert capsule.relevant_files == [
        "src/main.py",
        "tests/test_format.py",
        "src/utils/format.py",
    ]
    assert "missing.py" not in capsule.relevant_files
    assert "Step 2: Add tests - failed" in capsule.last_step_summary
    assert "tests/test_format.py" in capsule.last_step_summary


def test_completion_capsule_caps_relevant_files_and_handles_empty_results(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    files = []
    for index in range(MAX_RELEVANT_FILES + 5):
        path = project_dir / f"file_{index}.py"
        path.write_text("x = 1\n", encoding="utf-8")
        files.append(path.name)
    state = OrchestrationState(session_id="1", task_description="cap files")
    state._project_dir_override = str(project_dir)
    validation = SimpleNamespace(
        reasons=["Missing many files"],
        details={"expected_core_files": files},
    )

    capsule = build_completion_repair_capsule(
        task_prompt="x" * 1200,
        completion_validation=validation,
        orchestration_state=state,
    )

    assert len(capsule.relevant_files) == MAX_RELEVANT_FILES
    assert capsule.last_step_summary == ""
    assert capsule.task_prompt_excerpt == "x" * 800


def test_bounded_completion_repair_prompt_excludes_broad_context(tmp_path):
    state = _make_state(tmp_path)
    capsule = build_completion_repair_capsule(
        task_prompt="Build a formatter with tests",
        completion_validation=_completion_validation(),
        orchestration_state=state,
    )

    prompt = build_bounded_completion_repair_prompt(capsule, 3)

    assert "Current workspace inventory:" not in prompt
    assert "Very long context" not in prompt
    assert "step=1 verdict=success" not in prompt
    assert "src/main.py" in prompt
    assert "tests/test_format.py" in prompt
    assert '"path" (relative to workspace root)' in prompt
    assert 'Do not use a "commands" key. Use ops only.' in prompt
    assert 'Use relative paths only; no absolute paths, "..", or "~".' in prompt
    assert len(prompt) < 4000


def _seed_ctx(db_session, tmp_path, runtime_output="not json"):
    state = _make_state(tmp_path)
    project = Project(name="Phase 7H Project", workspace_path=str(state.project_dir))
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Phase 7H Session",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    task = Task(
        project_id=project.id,
        title="Phase 7H Task",
        status=TaskStatus.RUNNING,
        task_subfolder="task-7h",
    )
    db_session.add_all([session, task])
    db_session.flush()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add_all([link, execution])
    db_session.commit()
    runtime = _FakeRuntime(runtime_output)
    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="Build a formatter with tests",
        timeout_seconds=120,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=runtime,
        task_service=SimpleNamespace(),
        logger=logging.getLogger("phase7h-test"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=SimpleNamespace(),
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )
    return ctx, runtime


def test_completion_repair_branch_uses_phase7h_capsule_prompt(db_session, tmp_path):
    ctx, runtime = _seed_ctx(db_session, tmp_path)

    result = _attempt_completion_repair(
        ctx=ctx,
        completion_validation=_completion_validation(),
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "failed"
    assert runtime.prompts
    assert "Relevant existing files:" in runtime.prompts[0]
    assert "Current workspace inventory:" not in runtime.prompts[0]
    assert "Objective:" not in runtime.prompts[0]
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    generated = [
        event for event in events if event["event_type"] == EventType.REPAIR_GENERATED
    ]
    assert generated[-1]["details"]["completion_repair_prompt_mode"] == (
        "phase7h_capsule"
    )
    assert generated[-1]["details"]["capsule_relevant_file_count"] == 3
    assert generated[-1]["details"]["capsule_last_step_present"] is True
    assert generated[-1]["details"]["envelope_mode"] == "direct_capsule"
    assert generated[-1]["details"]["compliance_retry_attempted"] is True
    assert generated[-1]["details"]["compliance_retry_succeeded"] is False


def test_completion_repair_compliance_retry_recovers_valid_json(db_session, tmp_path):
    valid_step = (
        '{"step_number": 3, "description": "Retry JSON only", '
        '"commands": [], "verification": null, "rollback": null, '
        '"expected_files": ["src/main.py"]}'
    )
    ctx, runtime = _seed_ctx(
        db_session,
        tmp_path,
        runtime_output=["The file is {broken}.", valid_step],
    )

    result = _attempt_completion_repair(
        ctx=ctx,
        completion_validation=_completion_validation(),
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result == {
        "status": "failed",
        "reason": "repair_step_missing_commands_or_ops",
    }
    assert len(runtime.prompts) == 2
    assert runtime.prompts[1].startswith("Your previous response was not valid JSON.")
    assert "Relevant existing files:" not in runtime.prompts[1]
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    generated = [
        event for event in events if event["event_type"] == EventType.REPAIR_GENERATED
    ]
    assert generated[-1]["details"]["compliance_retry_attempted"] is True
    assert generated[-1]["details"]["compliance_retry_succeeded"] is True


def test_completion_repair_preserves_direct_non_step_json_for_classification():
    ready_json = (
        "{\n"
        '  "status": "ready",\n'
        '  "message": "I am here and ready to help. What do you need?"\n'
        "}"
    )

    assert _extract_completion_repair_json_text(ready_json) == ready_json


def test_completion_repair_generic_json_response_classifies_as_non_step(
    db_session, tmp_path
):
    ready_json = (
        "{\n"
        '  "status": "ready",\n'
        '  "message": "I am here and ready to help. What do you need?"\n'
        "}"
    )
    ctx, runtime = _seed_ctx(
        db_session,
        tmp_path,
        runtime_output=ready_json,
    )

    result = _attempt_completion_repair(
        ctx=ctx,
        completion_validation=_completion_validation(),
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result == {"status": "failed", "reason": "repair_step_missing_step_object"}
    assert len(runtime.prompts) == 1
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    generated = [
        event for event in events if event["event_type"] == EventType.REPAIR_GENERATED
    ]
    assert generated[-1]["details"]["compliance_retry_attempted"] is False
    assert generated[-1]["details"]["compliance_retry_succeeded"] is False


def test_completion_repair_compliance_retry_wrapper_json_classifies_as_non_step(
    db_session, tmp_path
):
    wrapped_ready_json = (
        '{"finalAssistantVisibleText":"{\\n'
        '  \\"status\\": \\"ready\\",\\n'
        '  \\"message\\": \\"I am here and ready to help. What do you need?\\"\\n'
        '}"}'
    )
    ctx, runtime = _seed_ctx(
        db_session,
        tmp_path,
        runtime_output=["The repair is {not valid json}.", wrapped_ready_json],
    )

    result = _attempt_completion_repair(
        ctx=ctx,
        completion_validation=_completion_validation(),
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result == {"status": "failed", "reason": "repair_step_missing_step_object"}
    assert len(runtime.prompts) == 2
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    generated = [
        event for event in events if event["event_type"] == EventType.REPAIR_GENERATED
    ]
    assert generated[-1]["details"]["compliance_retry_attempted"] is True
    assert generated[-1]["details"]["compliance_retry_succeeded"] is True


def _task_871_malformed_repair(command: str | None = None) -> str:
    repair_command = command or (
        "cat > tests/test_strtools.py << 'EOF'\n"
        "from strtools import __version__\n\n"
        "def test_version():\n"
        '    assert __version__ == "0.1.0"\n'
        "EOF"
    )
    encoded_command = json.dumps(repair_command)
    return (
        '{"step_number":4,'
        '"description":"Add a discoverable strtools test",'
        f'"commands":[{encoded_command},'
        '"verification":".venv/bin/python3 -m pytest --collect-only",'
        '"rollback":"rm tests/test_strtools.py",'
        '"expected_files":["tests/test_strtools.py"]}'
    )


def test_completion_repair_salvages_task_871_misplaced_verification():
    malformed = _task_871_malformed_repair()

    salvaged = _salvage_completion_repair_json_text(malformed)
    parsed = json.loads(salvaged)
    step = _extract_completion_repair_step(parsed, 4)

    assert step is not None
    assert step["commands"] == [
        "cat > tests/test_strtools.py << 'EOF'\n"
        "from strtools import __version__\n\n"
        "def test_version():\n"
        '    assert __version__ == "0.1.0"\n'
        "EOF"
    ]
    assert step["verification"] == ".venv/bin/python3 -m pytest --collect-only"


def test_completion_repair_valid_schema_is_unchanged():
    valid = json.dumps(
        {
            "step_number": 4,
            "description": "Add a test",
            "commands": ["touch tests/test_app.py"],
            "verification": "python -m pytest --collect-only",
            "rollback": None,
            "expected_files": ["tests/test_app.py"],
        }
    )

    assert _salvage_completion_repair_json_text(valid) == valid


def test_completion_repair_rejects_multiple_misplaced_verifications():
    malformed = _task_871_malformed_repair().replace(
        '"rollback":',
        '"verification":"python -m pytest -q","rollback":',
    )

    assert _salvage_completion_repair_json_text(malformed) == malformed


def test_completion_repair_rejects_conflicting_top_level_verification():
    malformed = _task_871_malformed_repair().replace(
        '"description":"Add a discoverable strtools test",',
        '"description":"Add a discoverable strtools test",'
        '"verification":"python -m pytest -q",',
    )

    assert _salvage_completion_repair_json_text(malformed) == malformed


def test_completion_repair_rejects_missing_command_string():
    malformed = _task_871_malformed_repair().replace(
        '"commands":['
        + json.dumps(
            "cat > tests/test_strtools.py << 'EOF'\n"
            "from strtools import __version__\n\n"
            "def test_version():\n"
            '    assert __version__ == "0.1.0"\n'
            "EOF"
        ),
        '"commands":[{"command":"echo invalid"}',
    )

    assert _salvage_completion_repair_json_text(malformed) == malformed


def test_completion_repair_rejects_created_file_outside_expected_files():
    malformed = _task_871_malformed_repair(
        "cat > tests/test_other.py << 'EOF'\n"
        "def test_other():\n"
        "    assert True\n"
        "EOF"
    )

    assert _salvage_completion_repair_json_text(malformed) == malformed


def test_salvaged_strtools_completion_repair_creates_integrity_valid_test(
    tmp_path,
):
    project_dir = tmp_path / "project"
    (project_dir / "tests").mkdir(parents=True)
    (project_dir / "strtools").mkdir()
    (project_dir / "strtools" / "__init__.py").write_text(
        '__version__ = "0.1.0"\n',
        encoding="utf-8",
    )
    parsed = json.loads(
        _salvage_completion_repair_json_text(_task_871_malformed_repair())
    )
    step = _extract_completion_repair_step(parsed, 4)

    assert step is not None
    subprocess.run(
        step["commands"][0],
        cwd=project_dir,
        shell=True,
        check=True,
    )
    test_text = (project_dir / "tests" / "test_strtools.py").read_text(encoding="utf-8")
    findings = scan_python_test_text(test_text, "tests/test_strtools.py")

    assert "from strtools import __version__" in test_text
    assert not any(finding.code == "undefined_test_name" for finding in findings)
