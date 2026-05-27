from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.error_handler import error_handler
from app.services.orchestration.diagnostics.debug_feedback import (
    build_bounded_debug_repair_prompt,
    build_debug_feedback_envelope,
    classify_debug_failure,
    normalize_bounded_debug_repair_payload,
    persist_debug_feedback_envelope,
)
from app.services.orchestration.diagnostics.diff_capsule import build_diff_capsule
from app.services.orchestration.diagnostics.evidence_capsule import (
    collect_workspace_evidence,
    infer_missing_python_module_target,
)
from app.services.orchestration.reporting.decision_timeline import (
    get_session_decision_timeline_payload,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.phases.execution_loop import (
    _debug_repair_materially_changes_source_or_tests,
    _execute_simple_verification_step,
    _is_simple_verification_command,
    _is_low_value_weak_verifier_command_fix,
    _is_weak_completion_verifier_failure,
    _is_read_only_inspection_command,
    _same_simple_verification_command,
    execute_step_loop,
)
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.orchestration.types import OrchestrationRunContext
from app.services.prompt_templates import OrchestrationState


def _seed_execution(db_session, tmp_path):
    project_dir = tmp_path / "phase7f-project"
    project_dir.mkdir()
    project = Project(name="Phase 7F Project", workspace_path=str(project_dir))
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Phase 7F Session",
        description="debug feedback test",
        status="stopped",
        is_active=False,
        execution_mode="manual",
    )
    task = Task(
        project_id=project.id,
        title="Debug feedback task",
        status=TaskStatus.FAILED,
        task_subfolder="task-debug",
    )
    db_session.add_all([session, task])
    db_session.flush()
    db_session.add(
        SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.FAILED,
        )
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(project)
    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(execution)
    return project_dir, project, session, task, execution


class _FakeRuntime:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def execute_task(self, prompt, timeout_seconds=None):
        self.prompts.append(str(prompt))
        if not self.responses:
            raise AssertionError("Fake runtime received unexpected prompt")
        response = self.responses.pop(0)
        if callable(response):
            response = response()
        return dict(response)

    def reports_context_overflow(self, result):
        return False

    def get_backend_metadata(self):
        return {"backend": "fake", "model_family": "test"}


def _extract_structured_text(value):
    if isinstance(value, dict):
        return value.get("output", json.dumps(value))
    return str(value or "")


def _normalize_step(raw_step, project_dir, logger_obj, step_number):
    return dict(raw_step)


def _make_run_context(
    db_session,
    tmp_path,
    *,
    runtime,
    used_debug_repair=False,
    expected_files=None,
    step_overrides=None,
):
    project_dir, project, session, task, execution = _seed_execution(
        db_session, tmp_path
    )
    session.status = "running"
    session.is_active = True
    task.status = TaskStatus.RUNNING
    task.current_step = 0
    link = (
        db_session.query(SessionTask)
        .filter(SessionTask.session_id == session.id, SessionTask.task_id == task.id)
        .one()
    )
    link.status = TaskStatus.RUNNING
    db_session.commit()
    step = {
        "step_number": 1,
        "description": "Run tests",
        "commands": ["pytest tests/test_demo.py"],
        "verification": "",
        "rollback": None,
        "expected_files": list(expected_files or []),
    }
    if step_overrides:
        step.update(step_overrides)
    state = OrchestrationState(
        session_id=str(session.id),
        task_description="Run a pytest-backed repair task",
        project_name=project.name,
        project_context="",
        task_id=task.id,
        plan=[step],
        reasoning_artifact={
            "intent": "Run tests and repair a simple runtime failure",
            "workspace_facts": [f"project_dir={project_dir}"],
            "planned_actions": ["Run tests"],
            "verification_plan": ["Verify the repaired command passes"],
        },
    )
    state._project_dir_override = str(project_dir)
    if used_debug_repair:
        state.debug_repair_task_execution_ids = [execution.id]
    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="Run the tests and repair the failing import.",
        timeout_seconds=120,
        execution_profile="test_only",
        validation_profile="verification",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=runtime,
        task_service=SimpleNamespace(),
        logger=logging.getLogger("phase7f-test"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=error_handler,
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )
    return ctx, execution


def test_simple_node_verification_matches_equivalent_quote_escaping():
    command = (
        'node -e "const fs=require(\'fs\'); const files=[\\"README.md\\"]; '
        'for (const p of files) { if (!fs.existsSync(p)) process.exit(1); }"'
    )
    verification = (
        'node -e "const fs=require(\'fs\'); const files=[\\"README.md\\"]; '
        'for (const p of files) { if (!fs.existsSync(p)) process.exit(1); }"'
    )

    assert _same_simple_verification_command(command, verification)


def test_simple_node_verification_allows_javascript_boolean_operators():
    verification = (
        "node -e \"const fs=require('fs'); const c=fs.readFileSync('styles.css','utf8'); "
        "if(!c.includes('margin: 0') || !c.includes('color: #333')) process.exit(1)\""
    )

    assert _is_simple_verification_command(verification)


def test_read_only_inspection_allows_sorted_rg_file_listing():
    assert _is_read_only_inspection_command("rg --files . | sort")


def test_simple_verification_executes_stronger_single_command(tmp_path):
    (tmp_path / "styles.css").write_text(
        "body {\n  margin: 0;\n}\n\nh1 {\n  color: #333;\n}\n",
        encoding="utf-8",
    )
    command = (
        "node -e \"const fs=require('fs'); const content=fs.readFileSync('styles.css','utf8'); "
        "if(!content.includes('body') || !content.includes('margin: 0') || "
        "!content.includes('h1') || !content.includes('color: #333')) process.exit(1);\""
    )
    verification = "node -e \"const fs=require('fs'); if(!fs.existsSync('styles.css')) process.exit(1)\""

    result = _execute_simple_verification_step(
        project_dir=tmp_path,
        commands=[command],
        verification_command=verification,
    )

    assert result is not None
    assert result["status"] == "completed", result


def test_phase7f_classifies_runtime_failures():
    assert (
        classify_debug_failure(stderr="ModuleNotFoundError: No module named 'main'")
        == "module_not_found"
    )
    assert (
        classify_debug_failure(stderr="SyntaxError: invalid syntax") == "syntax_error"
    )
    assert (
        classify_debug_failure(
            failed_command="pytest tests/test_api.py",
            stdout="FAILED tests/test_api.py::test_status - AssertionError",
        )
        == "pytest_failure"
    )
    assert (
        classify_debug_failure(
            stderr=(
                "============================= test session starts "
                "==============================\n"
                "platform linux -- Python 3.12.3, pytest-9.0.3, pluggy-1.6.0\n"
                "rootdir: /tmp/project\n"
                "plugins: asyncio-1.3.0"
            )
        )
        == "pytest_failure"
    )
    assert classify_debug_failure(stderr="AssertionError: boom") == (
        "runtime_assertion_failure"
    )
    assert (
        classify_debug_failure(
            failed_command="pnpm test",
            stderr="sh: 1: vitest: not found",
        )
        == "missing_dependency"
    )
    assert (
        classify_debug_failure(
            stderr=(
                "Step verification command failed (`node -e \"readFileSync('index')\"`): "
                "Error: ENOENT: no such file or directory, open 'index'"
            )
        )
        == "completion_validation_failed"
    )


def test_debug_feedback_envelope_persists_log_metadata_and_event(db_session, tmp_path):
    project_dir, _project, session, task, execution = _seed_execution(
        db_session, tmp_path
    )
    envelope = build_debug_feedback_envelope(
        task_execution_id=execution.id,
        task_id=task.id,
        step_index=2,
        failure_phase="execution",
        failed_command="pytest tests/test_api.py",
        return_code=1,
        stdout="FAILED tests/test_api.py::test_status",
        stderr="AssertionError: expected 200",
        validator_reasons=["completion_validation_failed"],
        changed_files=["app/api.py"],
        workspace_path=project_dir,
    )

    event = persist_debug_feedback_envelope(
        db=db_session,
        session_id=session.id,
        task_id=task.id,
        session_instance_id=session.instance_id,
        project_dir=project_dir,
        envelope=envelope,
    )
    db_session.commit()

    assert event is not None
    assert event["event_type"] == EventType.DEBUG_FEEDBACK_CAPTURED
    assert event["details"]["task_execution_id"] == execution.id
    assert event["details"]["debug_feedback_captured"] is True
    assert event["details"]["eligible_for_debug_repair"] is True

    row = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_execution_id == execution.id)
        .one()
    )
    metadata = json.loads(row.log_metadata)
    assert metadata["event_type"] == EventType.DEBUG_FEEDBACK_CAPTURED
    assert metadata["debug_feedback_envelope"]["task_execution_id"] == execution.id
    assert metadata["debug_failure_class"] == "pytest_failure"

    journal_events = read_orchestration_events(project_dir, session.id, task.id)
    assert [item["event_type"] for item in journal_events] == [
        EventType.DEBUG_FEEDBACK_CAPTURED,
        EventType.HEALTH_SCORE_UPDATED,
    ]


def test_debug_feedback_is_visible_in_decision_timeline(db_session, tmp_path):
    project_dir, _project, session, task, execution = _seed_execution(
        db_session, tmp_path
    )
    envelope = build_debug_feedback_envelope(
        task_execution_id=execution.id,
        task_id=task.id,
        step_index=1,
        failure_phase="completion_verification",
        failed_command="pytest",
        return_code=1,
        stderr="ModuleNotFoundError: No module named 'main'",
        validator_reasons=["Completion verification failed"],
        workspace_path=project_dir,
    )
    persist_debug_feedback_envelope(
        db=db_session,
        session_id=session.id,
        task_id=task.id,
        session_instance_id=session.instance_id,
        project_dir=project_dir,
        envelope=envelope,
    )
    db_session.commit()

    payload = get_session_decision_timeline_payload(db_session, session.id)
    debug_events = [
        event
        for event in payload["events"]
        if event["event_type"] == EventType.DEBUG_FEEDBACK_CAPTURED
    ]

    assert len(debug_events) == 1
    assert debug_events[0]["phase"] == "completion"
    assert debug_events[0]["decision_type"] == "failure"
    assert "module_not_found" in debug_events[0]["summary"]
    assert debug_events[0]["details"]["task_execution_id"] == execution.id


def test_bounded_debug_repair_prompt_requires_json_array(tmp_path):
    envelope = build_debug_feedback_envelope(
        task_execution_id=123,
        task_id=45,
        step_index=2,
        failure_phase="execution",
        failed_command="pytest tests/test_api.py",
        stderr="ModuleNotFoundError: No module named 'main'",
        validator_reasons=["completion_validation_failed"],
        workspace_path=tmp_path,
    )

    prompt = build_bounded_debug_repair_prompt(envelope)

    assert "bare JSON array" in prompt
    assert "one minimal debug repair step" in prompt
    assert "Do not return prose" in prompt
    assert "No module named" in prompt
    assert "Commands execute from the workspace root" in prompt
    assert (
        "Do not cd into the workspace root or any path containing vault/projects"
        in prompt
    )
    assert "full session history" not in prompt.lower()
    assert "task_execution_id" not in prompt


def test_phase11b_debug_prompt_names_cli_source_target_for_uppercase_failure(
    tmp_path,
):
    source_dir = tmp_path / "src" / "small_cli"
    source_dir.mkdir(parents=True)
    (source_dir / "__init__.py").write_text("", encoding="utf-8")
    (source_dir / "cli.py").write_text(
        "import argparse\n"
        "\n"
        "def build_parser():\n"
        "    parser = argparse.ArgumentParser(description='Print a message.')\n"
        "    parser.add_argument('message')\n"
        "    return parser\n"
        "\n"
        "def main(argv=None):\n"
        "    args = build_parser().parse_args(argv)\n"
        "    print(args.message)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_cli.py").write_text(
        "from small_cli.cli import build_parser, main\n"
        "\n"
        "def test_uppercase_option_prints_uppercase_message(capsys):\n"
        "    assert main(['--uppercase', 'hello']) == 0\n"
        "    assert capsys.readouterr().out.strip() == 'HELLO'\n",
        encoding="utf-8",
    )
    failure = (
        "FAILED tests/test_cli.py::test_uppercase_option_prints_uppercase_message\n"
        'assert main(["--uppercase", "hello"]) == 0\n'
        "src/small_cli/cli.py:20: in main\n"
        "args = parser.parse_args(argv)\n"
        "SystemExit: 2\n"
        "usage: __main__.py [-h] message\n"
        "__main__.py: error: unrecognized arguments: --uppercase\n"
    )
    envelope = build_debug_feedback_envelope(
        task_execution_id=123,
        task_id=45,
        step_index=2,
        failure_phase="execution",
        failed_command="python -m pytest -q",
        stdout=failure,
        workspace_path=tmp_path,
    )
    capsule = collect_workspace_evidence(
        envelope.failure_class,
        tmp_path,
        failure_context=failure,
    )

    prompt = build_bounded_debug_repair_prompt(envelope, capsule)

    assert "Debug source contract:" in prompt
    assert "Existing tests are the failing contract." in prompt
    assert "Do not edit tests or verifier commands." in prompt
    assert "Repair source code under the required target." in prompt
    assert "src/small_cli/cli.py" in prompt
    assert "Required argparse wiring:" in prompt
    assert (
        'In build_parser, add parser.add_argument("--uppercase", action="store_true", ...).'
        in prompt
    )
    assert "In main(argv), read args.uppercase after parse_args(argv)." in prompt
    assert 'Preserve default behavior: format_message("hello") == "hello".' in prompt
    assert "Uppercase only when the --uppercase flag is set." in prompt
    assert (
        "Do not satisfy this by changing tests or making all output uppercase."
        in prompt
    )
    assert 'main(["--uppercase", "hello"]) exits 0 and prints HELLO.' in prompt
    assert "No placeholder/pass/TODO/export-only fixes." in prompt


def test_phase11b_debug_prompt_names_import_repair_target_and_symbol(tmp_path):
    source_dir = tmp_path / "src" / "import_repair"
    source_dir.mkdir(parents=True)
    (source_dir / "__init__.py").write_text(
        "from import_repair.formatters import normalize_greeting\n",
        encoding="utf-8",
    )
    (source_dir / "formatters.py").write_text("# TODO\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_formatter.py").write_text(
        "from import_repair import normalize_greeting\n"
        "\n"
        "def test_normalize_greeting_trims_and_title_cases_name():\n"
        "    assert normalize_greeting('  ada   lovelace ') == 'Hello, Ada Lovelace!'\n",
        encoding="utf-8",
    )
    failure = (
        "ImportError while importing test module 'tests/test_formatter.py'.\n"
        "from import_repair import normalize_greeting\n"
        "src/import_repair/__init__.py:3: in <module>\n"
        "from import_repair.formatters import normalize_greeting\n"
        "ImportError: cannot import name 'normalize_greeting' from "
        f"'import_repair.formatters' ({source_dir / 'formatters.py'})\n"
    )
    envelope = build_debug_feedback_envelope(
        task_execution_id=123,
        task_id=45,
        step_index=2,
        failure_phase="execution",
        failed_command="python -m pytest -q",
        stderr=failure,
        workspace_path=tmp_path,
    )
    capsule = collect_workspace_evidence(
        envelope.failure_class,
        tmp_path,
        failure_context=failure,
    )

    prompt = build_bounded_debug_repair_prompt(envelope, capsule)

    assert "Debug source contract:" in prompt
    assert "src/import_repair/formatters.py" in prompt
    assert "normalize_greeting" in prompt
    assert "Define normalize_greeting in the target module." in prompt
    assert "Hello, Ada Lovelace!" in prompt
    assert "Do not edit tests or verifier commands." in prompt
    assert "No placeholder/pass/TODO/export-only fixes." in prompt


def test_import_error_evidence_infers_missing_submodule_target(tmp_path):
    package_dir = tmp_path / "src" / "math_tools"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("__all__ = ['calculator']\n")
    (package_dir / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_operations_import.py").write_text(
        "from math_tools.operations import add\n",
        encoding="utf-8",
    )
    failure_context = (
        "Step verification command failed (`python -c 'import sys; "
        "from math_tools import operations'`):\n"
        "ImportError: cannot import name 'operations' from 'math_tools' "
        f"({package_dir / '__init__.py'})"
    )

    assert (
        infer_missing_python_module_target(failure_context, tmp_path)
        == "src/math_tools/operations.py"
    )

    capsule = collect_workspace_evidence(
        "import_error",
        tmp_path,
        failure_context=failure_context,
    )

    assert "src/math_tools/operations.py" in "\n".join(capsule.results.values())
    assert any("from math_tools" in command for command in capsule.commands_run)
    assert not any("from sys" in command for command in capsule.commands_run)


def test_diff_capsule_skips_changed_init_when_missing_submodule_target_is_elsewhere(
    tmp_path,
):
    package_dir = tmp_path / "src" / "math_tools"
    package_dir.mkdir(parents=True)
    init_path = package_dir / "__init__.py"
    init_path.write_text("__all__ = ['calculator', 'operations']\n", encoding="utf-8")
    envelope = build_debug_feedback_envelope(
        task_execution_id=123,
        task_id=45,
        step_index=2,
        failure_phase="execution",
        failed_command='python -c "from math_tools import operations"',
        stderr=(
            "ImportError: cannot import name 'operations' from 'math_tools' "
            f"({init_path})"
        ),
        changed_files=["src/math_tools/__init__.py"],
        workspace_path=tmp_path,
    )

    capsule = build_diff_capsule(
        pre_checksum={"src/math_tools/__init__.py": "__all__ = ['calculator']\n"},
        project_dir=tmp_path,
        changed_files=["src/math_tools/__init__.py"],
        envelope=envelope,
    )

    assert capsule is None


def test_bounded_debug_repair_payload_requires_single_json_array():
    parsed = [
        {
            "title": "Fix missing import",
            "command": "python3 -c \"print('fixed')\"",
            "verification_command": "python3 -c \"print('ok')\"",
        }
    ]

    normalized = normalize_bounded_debug_repair_payload(parsed)

    assert normalized == {
        "fix_type": "command_fix",
        "fix": "python3 -c \"print('fixed')\"",
        "analysis": "Fix missing import",
        "confidence": "MEDIUM",
        "verification": "python3 -c \"print('ok')\"",
        "expected_files": [],
    }
    assert normalize_bounded_debug_repair_payload({"command": "echo bad"}) is None
    assert normalize_bounded_debug_repair_payload([{"command": "echo bad"}]) is None


def test_phase7f_valid_bounded_repair_is_retried_and_succeeds(db_session, tmp_path):
    runtime = _FakeRuntime(
        [
            {
                "status": "failed",
                "output": "FAILED tests/test_demo.py::test_import - AssertionError",
                "error": "AssertionError: missing import",
                "returncode": 1,
            },
            {
                "output": json.dumps(
                    [
                        {
                            "title": "Fix test command",
                            "command": "python3 -c \"print('fixed')\"",
                            "verification_command": "python3 -c \"print('ok')\"",
                        }
                    ]
                )
            },
            {
                "status": "success",
                "output": "fixed",
                "files_changed": [],
            },
        ]
    )
    ctx, execution = _make_run_context(db_session, tmp_path, runtime=runtime)

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed", result
    assert len(runtime.prompts) == 3
    assert "Return a bare JSON array" in runtime.prompts[1]
    assert ctx.orchestration_state.debug_repair_task_execution_ids == [execution.id]
    assert ctx.orchestration_state.plan[0]["commands"] == [
        "python3 -c \"print('fixed')\""
    ]
    assert ctx.orchestration_state.current_step_index == 1


def test_weak_verifier_command_fix_is_low_value_marker_repair():
    envelope = build_debug_feedback_envelope(
        task_execution_id=1,
        task_id=1,
        step_index=1,
        failure_phase="execution",
        failed_command=(
            'python -c "import sys; '
            "sys.exit(0 if '--uppercase' in sys.argv else 1)\""
        ),
        stderr="Step verification command failed",
        changed_files=["src/small_cli/cli.py"],
        workspace_path=".",
    )
    debug_data = {
        "fix_type": "command_fix",
        "fix": "echo '--uppercase' >> validate_seed.py",
        "verification": (
            'python -c "import sys; '
            "sys.exit(0 if '--uppercase' in sys.argv else 1)\" --uppercase"
        ),
    }

    assert envelope.failure_class == "completion_validation_failed"
    assert _is_low_value_weak_verifier_command_fix(envelope, debug_data)


def test_weak_verifier_repair_preserves_budget_for_later_pytest_failure():
    weak_envelope = build_debug_feedback_envelope(
        task_execution_id=1,
        task_id=1,
        step_index=1,
        failure_phase="execution",
        failed_command=(
            'python -c "import sys; '
            "sys.exit(0 if '--uppercase' in sys.argv else 1)\""
        ),
        stderr="Step verification command failed",
        changed_files=["src/small_cli/cli.py"],
        workspace_path=".",
    )
    weak_debug_data = {
        "fix_type": "command_fix",
        "fix": "echo '--uppercase' >> validate_seed.py",
        "verification": (
            'python -c "import sys; '
            "sys.exit(0 if '--uppercase' in sys.argv else 1)\" --uppercase"
        ),
    }
    pytest_envelope = build_debug_feedback_envelope(
        task_execution_id=1,
        task_id=1,
        step_index=2,
        failure_phase="execution",
        failed_command="python -m pytest tests/test_cli.py -q",
        stdout="FAILED tests/test_cli.py::test_uppercase - AssertionError",
        changed_files=["src/small_cli/cli.py"],
        workspace_path=".",
    )

    assert _is_weak_completion_verifier_failure(weak_envelope)
    assert _is_low_value_weak_verifier_command_fix(weak_envelope, weak_debug_data)
    assert not _debug_repair_materially_changes_source_or_tests(weak_debug_data)
    assert pytest_envelope.failure_class == "pytest_failure"
    assert pytest_envelope.eligible_for_debug_repair


def test_command_fix_replaces_failed_structured_ops_before_retry(db_session, tmp_path):
    runtime = _FakeRuntime(
        [
            {
                "output": json.dumps(
                    {
                        "fix_type": "command_fix",
                        "analysis": "Run a direct JSON update instead of retrying the stale replace op.",
                        "fix": "node -e \"console.log('patched')\"",
                        "confidence": "HIGH",
                    }
                )
            },
            {
                "status": "success",
                "output": "patched",
                "files_changed": ["package.json"],
            },
        ]
    )
    ctx, _execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        step_overrides={
            "description": "Update package.json",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "package.json",
                    "old": '"version": "1.0.0"',
                    "new": '"version": "1.1.0"',
                }
            ],
            "commands": [],
            "expected_files": ["package.json"],
        },
    )
    (ctx.orchestration_state.project_dir / "package.json").write_text(
        '{"name":"demo","version":"1.1.0"}\n',
        encoding="utf-8",
    )

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    repaired_step = ctx.orchestration_state.plan[0]
    assert repaired_step["commands"] == ["node -e \"console.log('patched')\""]
    assert repaired_step.get("ops") == []


def test_code_fix_verification_replaces_stale_commands_before_retry(
    db_session, tmp_path
):
    runtime = _FakeRuntime(
        [
            {
                "status": "success",
                "output": "index present",
            },
            {
                "output": json.dumps(
                    {
                        "fix_type": "code_fix",
                        "analysis": "Use the existing nested stylesheet path.",
                        "fix": "Verify css/style.css instead of style.css.",
                        "verification": "test -f index.html && test -f css/style.css",
                        "expected_files": ["index.html", "css/style.css"],
                        "confidence": "HIGH",
                    }
                )
            },
            {
                "status": "success",
                "output": "verified",
            },
        ]
    )
    ctx, _execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        step_overrides={
            "description": "Inspect existing page",
            "commands": ["cat index.html", "cat style.css"],
            "verification": "",
            "expected_files": ["index.html", "style.css"],
        },
    )
    (ctx.orchestration_state.project_dir / "index.html").write_text(
        "<link rel='stylesheet' href='css/style.css'>",
        encoding="utf-8",
    )
    (ctx.orchestration_state.project_dir / "css").mkdir()
    (ctx.orchestration_state.project_dir / "css" / "style.css").write_text(
        "body { color: green; }",
        encoding="utf-8",
    )

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    repaired_step = ctx.orchestration_state.plan[0]
    assert repaired_step["commands"] == ["test -f index.html && test -f css/style.css"]
    assert (
        repaired_step["verification"] == "test -f index.html && test -f css/style.css"
    )
    assert repaired_step["expected_files"] == ["index.html", "css/style.css"]


def test_non_actionable_code_fix_for_structured_ops_is_rejected(db_session, tmp_path):
    runtime = _FakeRuntime(
        [
            {
                "output": json.dumps(
                    {
                        "fix_type": "code_fix",
                        "analysis": "The JSON needs a scripts field.",
                        "fix": "Edit package.json to add scripts.test.",
                        "confidence": "MEDIUM",
                    }
                )
            },
            {
                "status": "success",
                "output": "this retry should not run",
            },
        ]
    )
    ctx, _execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        step_overrides={
            "description": "Update package.json",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "package.json",
                    "old": '"version": "1.0.0"',
                    "new": '"version": "1.1.0"',
                }
            ],
            "commands": [],
            "expected_files": ["package.json"],
        },
    )
    (ctx.orchestration_state.project_dir / "package.json").write_text(
        '{"name":"demo","version":"1.1.0"}\n',
        encoding="utf-8",
    )

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result == {
        "status": "failed",
        "reason": "non_actionable_code_fix_for_structured_ops",
    }
    assert len(runtime.prompts) == 1
    assert ctx.task.status == TaskStatus.FAILED
    assert "not actionable" in ctx.task.error_message


def test_max_step_attempts_pauses_session(db_session, tmp_path, monkeypatch):
    import app.services.orchestration.phases.execution_loop as execution_loop

    monkeypatch.setattr(execution_loop, "MAX_STEP_ATTEMPTS", 1)
    runtime = _FakeRuntime(
        [
            {
                "status": "failed",
                "output": "still failing",
            },
        ]
    )
    ctx, _execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        step_overrides={
            "description": "Run impossible command",
            "commands": ["false"],
            "verification": "",
        },
    )

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result == {"status": "failed", "reason": "max_attempts_reached"}
    db_session.refresh(ctx.session)
    assert ctx.session.status == "paused"
    assert ctx.session.is_active is False
    assert ctx.session.last_alert_level == "error"


def test_typed_ops_fix_replaces_failed_structured_ops_before_retry(
    db_session, tmp_path
):
    runtime = _FakeRuntime(
        [
            {
                "output": json.dumps(
                    {
                        "fix_type": "replace_op",
                        "analysis": "Rewrite package.json because exact replacement is stale.",
                        "replacement_ops": [
                            {
                                "op": "write_file",
                                "path": "package.json",
                                "content": '{"name":"demo","version":"1.1.0","scripts":{"test":"echo test ok"}}\n',
                            }
                        ],
                        "verification": "node -e \"const p=require('./package.json'); if(p.version !== '1.1.0') process.exit(1)\"",
                        "expected_files": ["package.json"],
                    }
                )
            },
            {
                "status": "success",
                "output": "write_file package.json",
                "files_changed": ["package.json"],
            },
        ]
    )
    ctx, _execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        step_overrides={
            "description": "Update package.json",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "package.json",
                    "old": '"version": "1.0.0"',
                    "new": '"version": "1.1.0"',
                }
            ],
            "commands": [],
            "expected_files": ["package.json"],
        },
    )
    (ctx.orchestration_state.project_dir / "package.json").write_text(
        '{"name":"demo","version":"1.1.0"}\n',
        encoding="utf-8",
    )

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    repaired_step = ctx.orchestration_state.plan[0]
    assert repaired_step["commands"] == []
    assert repaired_step["ops"] == [
        {
            "op": "write_file",
            "path": "package.json",
            "content": '{"name":"demo","version":"1.1.0","scripts":{"test":"echo test ok"}}\n',
        }
    ]


def test_wrapped_typed_ops_fix_applies_to_repeated_structured_op_failures(
    db_session, tmp_path
):
    runtime = _FakeRuntime(
        [
            {
                "output": json.dumps(
                    {
                        "fix_type": "replace_op",
                        "analysis": "Rewrite package.json because exact replacement is stale.",
                        "replacement_ops": [
                            {
                                "op": "write_file",
                                "path": "package.json",
                                "content": '{"name":"demo","version":"1.1.0","scripts":{"test":"echo test ok"}}\n',
                            }
                        ],
                        "expected_files": ["package.json"],
                    }
                )
            },
            {
                "output": json.dumps(
                    {
                        "projectContextChars": 15365,
                        "nonProjectContextChars": 33281,
                        "finalAssistantVisibleText": (
                            "```json\n"
                            "{\n"
                            '  "fix_type": "replace_op",\n'
                            '  "analysis": "Rewrite README.md because exact replacement is stale.",\n'
                            '  "replacement_ops": [\n'
                            '    {"op": "write_file", "path": "README.md", "content": "# Demo\\n\\nPackage metadata fixture.\\n\\n## Changelog\\n- 1.1.0\\n"}\n'
                            "  ],\n"
                            '  "confidence": "HIGH"\n'
                            "}\n```"
                        ),
                    }
                )
            },
        ]
    )
    ctx, _execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        step_overrides={
            "description": "Update package.json",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "package.json",
                    "old": '"version": "1.0.0"',
                    "new": '"version": "1.1.0"',
                }
            ],
            "commands": [],
            "expected_files": ["package.json"],
        },
    )
    ctx.orchestration_state.plan.append(
        {
            "step_number": 2,
            "description": "Update README.md version",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "README.md",
                    "old": "Version: 1.0.0",
                    "new": "Version: 1.1.0",
                }
            ],
            "commands": [],
            "verification": "",
            "rollback": None,
            "expected_files": ["README.md"],
        }
    )
    ctx.orchestration_state.reasoning_artifact["planned_actions"] = [
        "Update package.json",
        "Update README.md version",
    ]
    (ctx.orchestration_state.project_dir / "package.json").write_text(
        '{"name":"demo","version":"1.1.0"}\n',
        encoding="utf-8",
    )
    (ctx.orchestration_state.project_dir / "README.md").write_text(
        "# Demo\n\nPackage metadata fixture.\n",
        encoding="utf-8",
    )

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed", result
    assert len(runtime.prompts) == 2
    assert ctx.orchestration_state.plan[0]["ops"][0]["op"] == "write_file"
    assert ctx.orchestration_state.plan[1]["ops"] == [
        {
            "op": "write_file",
            "path": "README.md",
            "content": (
                "# Demo\n\nPackage metadata fixture.\n\n## Changelog\n- 1.1.0\n"
            ),
        }
    ]
    assert "1.1.0" in (ctx.orchestration_state.project_dir / "README.md").read_text(
        encoding="utf-8"
    )


def test_phase7g_diff_repair_prompt_is_used_when_capsule_available(
    db_session, tmp_path
):
    holder = {}

    def fail_after_file_change():
        source = holder["project_dir"] / "src" / "demo.py"
        source.write_text("VALUE = 2\n", encoding="utf-8")
        return {
            "status": "failed",
            "output": "FAILED tests/test_demo.py::test_value",
            "error": "AssertionError: expected 1",
            "files_changed": ["src/demo.py"],
            "returncode": 1,
        }

    runtime = _FakeRuntime(
        [
            fail_after_file_change,
            {
                "output": json.dumps(
                    [
                        {
                            "title": "Fix value",
                            "command": "python3 -c \"from pathlib import Path; Path('src/demo.py').write_text('VALUE = 1\\\\n')\"",
                            "verification_command": "python3 -m py_compile src/demo.py",
                        }
                    ]
                )
            },
            {
                "status": "success",
                "output": "fixed",
                "files_changed": ["src/demo.py"],
            },
        ]
    )
    ctx, _execution = _make_run_context(
        db_session, tmp_path, runtime=runtime, expected_files=["src/demo.py"]
    )
    holder["project_dir"] = ctx.orchestration_state.project_dir
    (holder["project_dir"] / "src").mkdir(parents=True)
    (holder["project_dir"] / "src" / "demo.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    assert "Unified diff capsule" in runtime.prompts[1]
    assert "Return a bare JSON array" in runtime.prompts[1]
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    attempted = [
        event
        for event in events
        if event["event_type"] == EventType.DEBUG_REPAIR_ATTEMPTED
    ]
    assert attempted[-1]["details"]["debug_prompt_mode"] == "phase7g_diff_repair"
    assert attempted[-1]["details"]["diff_capsule_primary_file"] == "src/demo.py"
    assert attempted[-1]["details"]["diff_capsule_line_count"] > 0


def test_phase7f_invalid_bounded_repair_terminalizes(db_session, tmp_path):
    runtime = _FakeRuntime(
        [
            {
                "status": "failed",
                "output": "FAILED tests/test_demo.py::test_import - AssertionError",
                "error": "AssertionError: missing import",
                "returncode": 1,
            },
            {"output": "not json"},
        ]
    )
    ctx, execution = _make_run_context(db_session, tmp_path, runtime=runtime)

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "debug_parse_error"
    assert ctx.orchestration_state.debug_repair_task_execution_ids == [execution.id]
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    rejected = [
        event for event in events if event["event_type"] == EventType.REPAIR_REJECTED
    ]
    assert rejected[-1]["details"]["debug_repair_terminal_reason"] == (
        "invalid_debug_repair_output"
    )


def test_phase7f_second_debug_repair_for_task_execution_is_blocked(
    db_session, tmp_path
):
    runtime = _FakeRuntime(
        [
            {
                "status": "failed",
                "output": "FAILED tests/test_demo.py::test_import - AssertionError",
                "error": "AssertionError: missing import",
                "returncode": 1,
            },
        ]
    )
    ctx, execution = _make_run_context(
        db_session, tmp_path, runtime=runtime, used_debug_repair=True
    )

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result == {"status": "failed", "reason": "debug_repair_budget_exhausted"}
    assert len(runtime.prompts) == 1
    assert ctx.orchestration_state.debug_repair_task_execution_ids == [execution.id]
