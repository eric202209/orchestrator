import logging
import json
from unittest.mock import MagicMock

import pytest
from app.models import TaskStatus

from app.services.orchestration.phases.planning_flow import execute_planning_phase

from app.services.orchestration.planning.planner import PlannerService

from app.services.orchestration.types import OrchestrationRunContext

from app.services.orchestration.validation.validator import ValidatorService

from app.services.orchestration.validation.parsing import extract_structured_text

from app.tests.planner_timeout_test_helpers import _patch_planning_flow_external_writes


def test_post_repair_weak_verification_gets_one_targeted_second_repair(
    tmp_path, monkeypatch
):
    initial_plan = [
        {
            "step_number": 1,
            "description": "Create text stats implementation",
            "commands": [
                "printf 'def analyze_text(text): return {}\\n' > text_stats.py"
            ],
            "verification": "echo ok",
            "rollback": "rm -f text_stats.py",
            "expected_files": ["text_stats.py"],
        }
    ]
    first_repair_plan = [
        {
            "step_number": 1,
            "description": "Inspect files",
            "commands": ["ls"],
            "verification": "python -m pytest --version",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Create text stats implementation",
            "commands": [
                "printf 'def analyze_text(text): return {}\\n' > text_stats.py"
            ],
            "verification": "echo ok",
            "rollback": "rm -f text_stats.py",
            "expected_files": ["text_stats.py"],
        },
        {
            "step_number": 3,
            "description": "Create text stats tests",
            "commands": [
                "printf 'from text_stats import analyze_text\\n' > test_text_stats.py"
            ],
            "verification": "test -f test_text_stats.py",
            "rollback": "rm -f test_text_stats.py",
            "expected_files": ["test_text_stats.py"],
        },
    ]
    second_repair_plan = [
        {
            "step_number": 1,
            "description": "Inspect files",
            "commands": ["ls"],
            "verification": "python -m pytest --version",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Create text stats implementation",
            "commands": [
                "printf 'def analyze_text(text): return {}\\n' > text_stats.py"
            ],
            "verification": "python -m pytest test_text_stats.py -q",
            "rollback": "rm -f text_stats.py",
            "expected_files": ["text_stats.py"],
        },
        {
            "step_number": 3,
            "description": "Create text stats tests",
            "commands": [
                "printf 'from text_stats import analyze_text\\n' > test_text_stats.py"
            ],
            "verification": "python -m pytest test_text_stats.py -q",
            "rollback": "rm -f test_text_stats.py",
            "expected_files": ["test_text_stats.py"],
        },
    ]

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Repair weak verification"
    task.description = "Repair weak verification"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()
    events = []

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=64,
        task_id=15,
        prompt="Repair weak verification",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_weak_verification_second_pass"),
        emit_live=lambda *args, **kwargs: events.append((args, kwargs)),
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Create text utility",
            "workspace_facts": [],
            "planned_actions": [],
            "verification_plan": ["Run pytest"],
        },
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        staticmethod(
            lambda *args, **kwargs: type(
                "Verdict",
                (),
                {"accepted": True, "status": "accepted", "reasons": []},
            )()
        ),
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_plan",
        staticmethod(
            lambda *args, **kwargs: type(
                "Verdict",
                (),
                {
                    "accepted": True,
                    "warning": False,
                    "status": "accepted",
                    "reasons": [],
                    "details": {},
                    "verdict": {"status": "accepted"},
                },
            )()
        ),
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )
    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        if len(repair_calls) == 1:
            return {"output": json.dumps(first_repair_plan)}
        return {"output": json.dumps(second_repair_plan)}

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "completed"}
    assert len(repair_calls) == 2
    assert repair_calls[1]["reason"].startswith("post_repair_weak_verification_steps")
    assert "steps [2]" in repair_calls[1]["rejection_reasons"][0]
    assert ctx.orchestration_state.plan == second_repair_plan


def test_post_repair_weak_verification_second_repair_is_capped(tmp_path, monkeypatch):
    weak_plan = [
        {
            "step_number": 1,
            "description": "Create text stats implementation",
            "commands": [
                "printf 'def analyze_text(text): return {}\\n' > text_stats.py"
            ],
            "verification": "echo ok",
            "rollback": "rm -f text_stats.py",
            "expected_files": ["text_stats.py"],
        }
    ]

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(weak_plan)}

    task = MagicMock()
    task.title = "Repair weak verification cap"
    task.description = "Repair weak verification cap"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=65,
        task_id=16,
        prompt="Repair weak verification cap",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_weak_verification_cap"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        return {"output": json.dumps(weak_plan)}

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert len(repair_calls) == 2
    assert result == {
        "status": "failed",
        "reason": "planning_invalid_commands_after_repair",
    }
    assert task.status == TaskStatus.FAILED
    assert session_task_link.status == TaskStatus.FAILED
    assert session.status == "paused"
    assert session.is_active is False


def test_post_repair_python_source_syntax_gets_one_targeted_second_repair(
    tmp_path, monkeypatch
):
    initial_plan = [
        {
            "step_number": 1,
            "description": "Write broken source",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "def broken(:\n    pass\n",
                }
            ],
            "verification": "python3 -m py_compile src/app.py",
            "rollback": None,
            "expected_files": ["src/app.py"],
        }
    ]
    first_repair_plan = [
        {
            "step_number": 1,
            "description": "Write still broken source",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": '"""unterminated\n\ndef main():\n    return 0\n',
                }
            ],
            "verification": "python3 -m py_compile src/app.py",
            "rollback": None,
            "expected_files": ["src/app.py"],
        }
    ]
    second_repair_plan = [
        {
            "step_number": 1,
            "description": "Write valid source",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "def main():\n    return 0\n",
                }
            ],
            "verification": "python3 -m py_compile src/app.py",
            "rollback": None,
            "expected_files": ["src/app.py"],
        }
    ]

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Repair Python syntax"
    task.description = "Repair Python syntax"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()
    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=66,
        task_id=17,
        prompt="Repair Python syntax",
        timeout_seconds=300,
        execution_profile="implementation",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_python_source_syntax_second_pass"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    persisted_events = []
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control.append_orchestration_event",
        lambda *args, **kwargs: persisted_events.append(kwargs) or {},
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Repair Python syntax",
            "workspace_facts": [],
            "planned_actions": [],
            "verification_plan": ["Run py_compile"],
        },
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        staticmethod(
            lambda *args, **kwargs: type(
                "Verdict",
                (),
                {"accepted": True, "status": "accepted", "reasons": []},
            )()
        ),
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        if len(repair_calls) == 1:
            return {"output": json.dumps(first_repair_plan)}
        return {"output": json.dumps(second_repair_plan)}

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "completed"}
    assert len(repair_calls) == 2
    assert repair_calls[1]["reason"].startswith(
        "post_repair_python_source_syntax_invalid"
    )
    assert "src/app.py" in repair_calls[1]["rejection_reasons"][0]
    assert "compile(content, path, 'exec')" in repair_calls[1]["rejection_reasons"][0]
    assert ctx.orchestration_state.plan == second_repair_plan
    arbitration_events = [
        event
        for event in persisted_events
        if str(event.get("event_type")) == "planning_repair_arbitration"
    ]
    assert [event["details"]["arbitration_action"] for event in arbitration_events] == [
        "syntax_retry",
        "none",
    ]
    assert arbitration_events[0]["details"]["invalid_output"] is True
    assert arbitration_events[0]["details"]["reason"] == (
        "invalid_python_repair_candidate"
    )


def test_post_repair_python_source_syntax_second_repair_is_capped(
    tmp_path, monkeypatch
):
    broken_plan = [
        {
            "step_number": 1,
            "description": "Write broken source",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "def broken(:\n    pass\n",
                }
            ],
            "verification": "python3 -m py_compile src/app.py",
            "rollback": None,
            "expected_files": ["src/app.py"],
        }
    ]

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(broken_plan)}

    task = MagicMock()
    task.title = "Repair Python syntax cap"
    task.description = "Repair Python syntax cap"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()
    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=67,
        task_id=18,
        prompt="Repair Python syntax cap",
        timeout_seconds=300,
        execution_profile="implementation",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_python_source_syntax_cap"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    persisted_events = []
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control.append_orchestration_event",
        lambda *args, **kwargs: persisted_events.append(kwargs) or {},
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        return {"output": json.dumps(broken_plan)}

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert len(repair_calls) == 2
    assert result == {
        "status": "failed",
        "reason": "planning_validation_failed_after_repair",
    }
    assert task.status == TaskStatus.FAILED
    assert session_task_link.status == TaskStatus.FAILED
    assert session.status == "paused"
    assert session.is_active is False
    arbitration_events = [
        event
        for event in persisted_events
        if str(event.get("event_type")) == "planning_repair_arbitration"
    ]
    assert [event["details"]["arbitration_action"] for event in arbitration_events] == [
        "syntax_retry",
        "reject_after_retry",
    ]
    assert arbitration_events[-1]["details"]["invalid_output"] is True
    assert arbitration_events[-1]["details"]["reason"] == (
        "invalid_python_repair_candidate"
    )


def test_post_repair_argparse_framework_mismatch_gets_one_targeted_second_repair(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "src" / "medium_cli"
    source_dir.mkdir(parents=True)
    (source_dir / "__init__.py").write_text("", encoding="utf-8")
    existing_cli = (
        "import argparse\n"
        "from medium_cli.formatting import format_task_line\n"
        "from medium_cli.store import TaskStore\n\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        "    parser = argparse.ArgumentParser(description='Inspect tasks')\n"
        "    subparsers = parser.add_subparsers(dest='command', required=True)\n"
        "    subparsers.add_parser('list')\n"
        "    return parser\n\n"
        "def build_store() -> TaskStore:\n"
        "    return TaskStore()\n\n"
        "def main(argv=None) -> int:\n"
        "    parser = build_parser()\n"
        "    args = parser.parse_args(argv)\n"
        "    if args.command == 'list':\n"
        "        return 0\n"
        "    return 2\n"
    )
    valid_cli = (
        "import argparse\n"
        "from medium_cli.formatting import format_summary, format_task_line\n"
        "from medium_cli.store import TaskStore\n\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        "    parser = argparse.ArgumentParser(description='Inspect tasks')\n"
        "    subparsers = parser.add_subparsers(dest='command', required=True)\n"
        "    subparsers.add_parser('list')\n"
        "    subparsers.add_parser('summary')\n"
        "    return parser\n\n"
        "def build_store() -> TaskStore:\n"
        "    return TaskStore()\n\n"
        "def main(argv=None) -> int:\n"
        "    parser = build_parser()\n"
        "    args = parser.parse_args(argv)\n"
        "    store = build_store()\n"
        "    if args.command == 'list':\n"
        "        return 0\n"
        "    if args.command == 'summary':\n"
        "        total, completed = store.summary()\n"
        "        print(format_summary(total, completed))\n"
        "        return 0\n"
        "    return 2\n"
    )
    (source_dir / "cli.py").write_text(existing_cli, encoding="utf-8")
    (source_dir / "formatting.py").write_text(
        "def format_task_line(task):\n    return str(task)\n\n"
        "def format_summary(total, completed):\n    return f'{total} tasks, {completed} complete'\n",
        encoding="utf-8",
    )
    (source_dir / "store.py").write_text(
        "class TaskStore:\n    def summary(self):\n        return (3, 2)\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_summary.py").write_text(
        "def test_existing_contract():\n    assert 1 == 1\n    assert 2 == 2\n",
        encoding="utf-8",
    )
    initial_plan = [
        {
            "step_number": 1,
            "description": "Add summary command",
            "commands": [],
            "ops": [
                {
                    "op": "append_file",
                    "path": "src/medium_cli/cli.py",
                    "content": "\n@cli.command()\ndef summary():\n    print('summary')\n",
                }
            ],
            "verification": "python3 -m py_compile src/medium_cli/cli.py",
            "rollback": None,
            "expected_files": ["src/medium_cli/cli.py"],
        }
    ]
    click_repair_plan = [
        {
            "step_number": 1,
            "description": "Add click summary command",
            "commands": [],
            "ops": [
                {
                    "op": "append_file",
                    "path": "src/medium_cli/cli.py",
                    "content": "\n@click.command()\ndef summary():\n    click.echo('summary')\n",
                }
            ],
            "verification": "python3 -m py_compile src/medium_cli/cli.py",
            "rollback": None,
            "expected_files": ["src/medium_cli/cli.py"],
        },
        {
            "step_number": 2,
            "description": "Rewrite summary tests",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "tests/test_summary.py",
                    "content": "def test_summary():\n    assert True\n",
                }
            ],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["tests/test_summary.py"],
        },
    ]
    argparse_repair_plan = [
        {
            "step_number": 1,
            "description": "Preserve argparse CLI and add summary",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/medium_cli/cli.py",
                    "content": valid_cli,
                }
            ],
            "verification": "python3 -m py_compile src/medium_cli/cli.py",
            "rollback": None,
            "expected_files": ["src/medium_cli/cli.py"],
        }
    ]

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Add summary command"
    task.description = "Add summary command"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()
    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=68,
        task_id=19,
        prompt="Add a summary command to the argparse CLI",
        timeout_seconds=300,
        execution_profile="implementation",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_framework_mismatch_second_pass"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Add summary command",
            "workspace_facts": [],
            "planned_actions": [],
            "verification_plan": ["Run py_compile"],
        },
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        staticmethod(
            lambda *args, **kwargs: type(
                "Verdict",
                (),
                {"accepted": True, "status": "accepted", "reasons": []},
            )()
        ),
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        if len(repair_calls) == 1:
            return {"output": json.dumps(click_repair_plan)}
        return {"output": json.dumps(argparse_repair_plan)}

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": True},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "completed"}
    assert len(repair_calls) == 2
    assert repair_calls[1]["reason"].startswith("post_repair_framework_mismatch")
    assert "detected framework: argparse" in repair_calls[1]["rejection_reasons"][0]
    assert "build_parser" in repair_calls[1]["rejection_reasons"][0]
    assert "main(argv=None)" in repair_calls[1]["rejection_reasons"][0]
    assert "@click.command" in repair_calls[1]["rejection_reasons"][0]
    assert any(
        operation.get("op") == "write_file"
        and operation.get("path") == "src/medium_cli/cli.py"
        and operation.get("content") == valid_cli
        for step in ctx.orchestration_state.plan
        for operation in (step.get("ops") or [])
    )


def test_post_repair_argparse_framework_mismatch_second_repair_is_capped(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "src" / "medium_cli"
    source_dir.mkdir(parents=True)
    (source_dir / "cli.py").write_text(
        "import argparse\n\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_subparsers(dest='command', required=True)\n"
        "    return parser\n\n"
        "def main(argv=None) -> int:\n"
        "    parser = build_parser()\n"
        "    parser.parse_args(argv)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    bad_plan = [
        {
            "step_number": 1,
            "description": "Add click summary command",
            "commands": [],
            "ops": [
                {
                    "op": "append_file",
                    "path": "src/medium_cli/cli.py",
                    "content": "\n@click.command()\ndef summary():\n    click.echo('summary')\n",
                }
            ],
            "verification": "python3 -m py_compile src/medium_cli/cli.py",
            "rollback": None,
            "expected_files": ["src/medium_cli/cli.py"],
        }
    ]

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(bad_plan)}

    task = MagicMock()
    task.title = "Add summary command"
    task.description = "Add summary command"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()
    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=69,
        task_id=20,
        prompt="Add a summary command to the argparse CLI",
        timeout_seconds=300,
        execution_profile="implementation",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_framework_mismatch_cap"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        return {"output": json.dumps(bad_plan)}

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": True},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert len(repair_calls) == 2
    assert repair_calls[1]["reason"].startswith("post_repair_framework_mismatch")
    assert result == {
        "status": "failed",
        "reason": "planning_validation_failed_after_repair",
    }
    assert task.status == TaskStatus.FAILED
    assert session_task_link.status == TaskStatus.FAILED
    assert session.status == "paused"
    assert session.is_active is False


def test_post_repair_background_process_gets_one_targeted_second_repair(
    tmp_path, monkeypatch
):
    initial_plan = [
        {
            "step_number": 1,
            "description": "Create text stats implementation",
            "commands": [
                "printf 'def analyze_text(text): return {}\\n' > text_stats.py"
            ],
            "verification": "echo ok",
            "rollback": "rm -f text_stats.py",
            "expected_files": ["text_stats.py"],
        }
    ]
    first_repair_plan = [
        {
            "step_number": 1,
            "description": "Start a background server",
            "commands": ["python -m http.server 8000 &"],
            "verification": "python -m pytest --version",
            "rollback": None,
            "expected_files": [],
        }
    ]
    second_repair_plan = [
        {
            "step_number": 1,
            "description": "Run a bounded foreground check",
            "commands": ["python -m pytest --version"],
            "verification": "python -m pytest --version",
            "rollback": None,
            "expected_files": [],
        }
    ]

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Repair background process"
    task.description = "Repair background process"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=66,
        task_id=15,
        prompt="Repair background process",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_background_process_second_pass"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        if len(repair_calls) == 1:
            return {"output": json.dumps(first_repair_plan)}
        return {"output": json.dumps(second_repair_plan)}

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "completed"}
    assert len(repair_calls) == 2
    assert repair_calls[1]["reason"].startswith("post_repair_background_process_steps")
    assert "steps [1]" in repair_calls[1]["rejection_reasons"][0]
    assert "bounded foreground commands" in repair_calls[1]["rejection_reasons"][0]
    assert ctx.orchestration_state.plan == second_repair_plan


def test_post_repair_missing_verification_gets_one_targeted_second_repair(
    tmp_path, monkeypatch
):
    initial_plan = [
        {
            "step_number": 1,
            "description": "Create CSV reporter",
            "commands": ["printf 'def build_report(rows): return []\\n' > reporter.py"],
            "verification": None,
            "rollback": "rm -f reporter.py",
            "expected_files": ["reporter.py"],
        }
    ]
    first_repair_plan = [
        {
            "step_number": 1,
            "description": "Create CSV reporter",
            "commands": ["printf 'def build_report(rows): return []\\n' > reporter.py"],
            "verification": None,
            "rollback": "rm -f reporter.py",
            "expected_files": ["reporter.py"],
        }
    ]
    second_repair_plan = [
        {
            "step_number": 1,
            "description": "Create CSV reporter",
            "commands": ["printf 'def build_report(rows): return []\\n' > reporter.py"],
            "verification": "python -m pytest test_reporter.py -q",
            "rollback": "rm -f reporter.py",
            "expected_files": ["reporter.py"],
        }
    ]

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Repair missing verification"
    task.description = "Repair missing verification"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=67,
        task_id=17,
        prompt="Repair missing verification",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_missing_verification_second_pass"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Create CSV reporter",
            "workspace_facts": [],
            "planned_actions": [],
            "verification_plan": ["Run pytest"],
        },
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        staticmethod(
            lambda *args, **kwargs: type(
                "Verdict",
                (),
                {"accepted": True, "status": "accepted", "reasons": []},
            )()
        ),
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "find_immediate_repair_step_issues",
        staticmethod(lambda *args, **kwargs: {}),
    )

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        if len(repair_calls) == 1:
            return {"output": json.dumps(first_repair_plan)}
        return {"output": json.dumps(second_repair_plan)}

    validate_calls = []

    def validate_plan(*args, **kwargs):
        validate_calls.append(args)
        if len(validate_calls) < 3:
            return type(
                "Verdict",
                (),
                {
                    "accepted": False,
                    "warning": False,
                    "status": "rejected",
                    "reasons": [
                        "Plan is missing verification commands for implementation-heavy work (steps: [1])"
                    ],
                    "details": {
                        "missing_verification_steps": [1],
                        "semantic_violation_codes": ["missing_verification_command"],
                    },
                    "verdict": {"status": "rejected"},
                },
            )()
        return type(
            "Verdict",
            (),
            {
                "accepted": True,
                "warning": False,
                "status": "accepted",
                "reasons": [],
                "details": {},
                "verdict": {"status": "accepted"},
            },
        )()

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))
    monkeypatch.setattr(ValidatorService, "validate_plan", staticmethod(validate_plan))

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "completed"}
    assert len(repair_calls) == 2
    assert repair_calls[1]["reason"].startswith(
        "post_repair_missing_verification_steps"
    )
    assert "steps [1]" in repair_calls[1]["rejection_reasons"][0]
    assert "implementation-heavy step" in repair_calls[1]["rejection_reasons"][0]
    assert ctx.orchestration_state.plan == second_repair_plan


def test_post_repair_missing_verification_second_repair_is_capped(
    tmp_path, monkeypatch
):
    missing_plan = [
        {
            "step_number": 1,
            "description": "Create CSV reporter",
            "commands": ["printf 'def build_report(rows): return []\\n' > reporter.py"],
            "verification": None,
            "rollback": "rm -f reporter.py",
            "expected_files": ["reporter.py"],
        }
    ]

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(missing_plan)}

    task = MagicMock()
    task.title = "Repair missing verification cap"
    task.description = "Repair missing verification cap"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=68,
        task_id=17,
        prompt="Repair missing verification cap",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_missing_verification_cap"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "find_immediate_repair_step_issues",
        staticmethod(lambda *args, **kwargs: {}),
    )

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        return {"output": json.dumps(missing_plan)}

    def rejected_missing_verification(*args, **kwargs):
        return type(
            "Verdict",
            (),
            {
                "accepted": False,
                "warning": False,
                "status": "rejected",
                "reasons": [
                    "Plan is missing verification commands for implementation-heavy work (steps: [1])"
                ],
                "details": {
                    "missing_verification_steps": [1],
                    "semantic_violation_codes": ["missing_verification_command"],
                },
                "verdict": {"status": "rejected"},
            },
        )()

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))
    monkeypatch.setattr(
        ValidatorService, "validate_plan", staticmethod(rejected_missing_verification)
    )

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert len(repair_calls) == 2
    assert result == {
        "status": "failed",
        "reason": "planning_validation_failed_after_repair",
    }


def test_post_repair_missing_materialization_rejects_inspect_only_plan(
    tmp_path, monkeypatch
):
    initial_plan = [
        {
            "step_number": 1,
            "description": "Inspect CLI",
            "commands": ["rg summary src/medium_cli tests"],
            "verification": 'python3 -c "import sys; sys.exit(0)"',
            "rollback": None,
            "expected_files": [],
        }
    ]
    repaired_plan = [
        {
            "step_number": 1,
            "description": "Inspect CLI",
            "commands": ["rg summary src/medium_cli tests"],
            "verification": 'python3 -c "import sys; sys.exit(0)"',
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Run tests",
            "commands": ["python3 -m pytest -q"],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": [],
        },
    ]

    (tmp_path / "src" / "medium_cli").mkdir(parents=True)
    (tmp_path / "src" / "medium_cli" / "__init__.py").write_text("", encoding="utf-8")

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Add summary command"
    task.description = "Add a summary command to the Python CLI"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=69,
        task_id=18,
        prompt="Add a summary command to the Python CLI",
        timeout_seconds=300,
        execution_profile="implementation",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_missing_materialization_guard"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "find_immediate_repair_step_issues",
        staticmethod(lambda *args, **kwargs: {}),
    )
    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(lambda cls, *args, **kwargs: {"output": json.dumps(repaired_plan)}),
    )

    validate_calls = []

    def validate_plan(*args, **kwargs):
        validate_calls.append(args)
        return type(
            "Verdict",
            (),
            {
                "accepted": False,
                "warning": False,
                "status": "rejected",
                "reasons": [
                    "Implementation task plan does not materialize any source changes"
                ],
                "details": {
                    "missing_materialization_for_implementation": True,
                    "semantic_violation_codes": ["missing_source_materialization"],
                },
                "verdict": {"status": "rejected"},
            },
        )()

    monkeypatch.setattr(ValidatorService, "validate_plan", staticmethod(validate_plan))

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": True},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {
        "status": "failed",
        "reason": "planning_repair_missing_source_materialization",
    }
    assert len(validate_calls) == 1
    assert task.status == TaskStatus.FAILED
    assert session_task_link.status == TaskStatus.FAILED
    assert session.status == "paused"
    assert session.is_active is False
