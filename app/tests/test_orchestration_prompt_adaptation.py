from __future__ import annotations

import json
from types import SimpleNamespace

from app.models import SystemSetting
from app.services.orchestration.context_assembly import (
    assemble_debugging_prompt,
    assemble_execution_prompt,
    assemble_task_summary_prompt,
)


def _build_ctx(tmp_path, *, db=None):
    class _State:
        def __init__(self):
            self.project_dir = tmp_path
            self.project_context = "Backend service with auth and queue workers."
            self.phase_history = []
            self.validation_history = []
            self.execution_results = []
            self.current_step_index = 0
            self.debug_attempts = [{"attempt": 1, "error": "previous failure"}]
            self.project_name = "Adapted Runtime"
            self.workspace_root = tmp_path
            self.plan = [
                {
                    "step_number": 1,
                    "description": "Inspect auth entrypoints",
                    "commands": ["rg -n auth src"],
                    "expected_files": [],
                }
            ]
            self.changed_files = ["src/auth.py", "tests/test_auth.py"]

        def prior_results_summary(self) -> str:
            return "Step 1: SUCCESS - inspected auth flow."

        @property
        def completed_steps(self):
            return self.plan[: self.current_step_index]

    return SimpleNamespace(
        db=db,
        prompt="Improve auth retries and session recovery.",
        execution_profile="full_lifecycle",
        orchestration_state=_State(),
    )


def test_assemble_execution_prompt_uses_active_adaptation_profile(
    monkeypatch, tmp_path
):
    ctx = _build_ctx(tmp_path)
    step = {
        "step_number": 2,
        "description": "Implement auth retry handling",
        "commands": ["apply_patch ..."],
        "verification": "pytest -q tests/test_auth.py",
        "rollback": "git checkout -- src/auth.py",
        "expected_files": ["src/auth.py"],
    }

    monkeypatch.setattr(
        "app.services.orchestration.context_assembly.get_effective_adaptation_profile",
        lambda db=None: "openai_responses_default",
    )

    payload = json.loads(assemble_execution_prompt(ctx, step))

    assert payload["execution_mode"] == "step_execution"
    assert payload["context"]["Project Directory"] == str(tmp_path)
    assert payload["context"]["Expected Files"] == ["src/auth.py"]
    assert "Implement auth retry handling" in payload["prompt_body"]


def test_assemble_debugging_prompt_uses_db_selected_adaptation_profile(
    db_session, tmp_path
):
    db_session.add(
        SystemSetting(
            key="orchestrator_adaptation_profile",
            value="openai_responses_default",
        )
    )
    db_session.commit()

    ctx = _build_ctx(tmp_path, db=db_session)
    payload = json.loads(
        assemble_debugging_prompt(
            ctx,
            step_description="Implement auth retry handling",
            error_message="pytest failed with import error",
            command_output="Traceback: import error",
            verification_output="FAILED tests/test_auth.py",
            attempt_number=2,
            max_attempts=3,
        )
    )

    assert payload["execution_mode"] == "debugging"
    assert payload["context"]["Attempt Number"] == 2
    assert payload["context"]["Max Attempts"] == 3
    assert "pytest failed with import error" in payload["prompt_body"]


def test_assemble_task_summary_prompt_uses_active_adaptation_profile(
    monkeypatch, tmp_path
):
    ctx = _build_ctx(tmp_path)

    monkeypatch.setattr(
        "app.services.orchestration.context_assembly.get_effective_adaptation_profile",
        lambda db=None: "openai_responses_default",
    )

    payload = json.loads(assemble_task_summary_prompt(ctx))

    assert payload["execution_mode"] == "task_summary"
    assert payload["context"]["Changed Files Count"] == 2
    assert "src/auth.py" in payload["prompt_body"]
