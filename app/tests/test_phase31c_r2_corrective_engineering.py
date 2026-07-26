"""Narrow Phase 31C-R2 runtime and planning convergence regressions."""

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.agents.agent_backends import get_backend_descriptor
from app.services.agents.agent_runtime import (
    BackendRole,
    RuntimeCapabilityError,
    create_agent_runtime,
    validate_runtime_capabilities,
)
from app.services.orchestration.phases.planning_repair_arbitration_control import (
    _preserve_bootstrap_source_materialization_plan,
    _preserve_regressed_weak_verification_plan,
)
from app.services.orchestration.planning.planner import PlannerService


def test_repair_dispatch_rejects_effective_context_below_planning_requirement():
    descriptor = get_backend_descriptor("openai_chat_completions")

    with pytest.raises(RuntimeCapabilityError, match="16000"):
        validate_runtime_capabilities(
            descriptor,
            BackendRole.DEBUG_REPAIR,
            effective_context_tokens=8192,
        )


def test_repair_dispatch_accepts_declared_context_for_single_model_profile():
    descriptor = get_backend_descriptor("openai_chat_completions")

    result = validate_runtime_capabilities(
        descriptor,
        BackendRole.REPAIR,
        effective_context_tokens=16384,
    )

    assert result["effective_context_tokens"] == 16384
    assert result["role"] == "repair"


def test_explicit_debug_runtime_factory_rejects_low_context(db_session, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "REPAIR_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_CONTEXT_TOKENS", 8192)
    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", 8192)

    with pytest.raises(RuntimeCapabilityError, match="16000"):
        create_agent_runtime(
            db_session,
            session_id=None,
            role=BackendRole.DEBUG_REPAIR,
        )


def test_bootstrap_repair_restores_removed_source_materialization():
    previous = [
        {
            "step_number": 1,
            "description": "Materialize the pilot document",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "README-PILOT.md",
                    "content": "# Pilot\n",
                }
            ],
            "verification": "python -m pytest -q",
            "expected_files": ["README-PILOT.md"],
        }
    ]
    candidate = [
        {
            "step_number": 1,
            "description": "Verify the pilot document",
            "commands": ["python -m pytest -q"],
            "ops": [],
            "verification": "python -m pytest -q",
            "expected_files": [],
        }
    ]

    merged = _preserve_bootstrap_source_materialization_plan(previous, candidate)

    assert merged is not None
    assert merged[0]["ops"][0]["path"] == "README-PILOT.md"
    assert merged[0]["expected_files"] == ["README-PILOT.md"]


def test_stale_replace_repair_keeps_candidate_ops_and_restores_verification(
    monkeypatch, tmp_path
):
    previous = [
        {
            "step_number": 1,
            "description": "Patch the application",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/app.py",
                    "old": "missing text",
                    "new": "fixed text",
                }
            ],
            "verification": "test -f src/app.py",
            "expected_files": ["src/app.py"],
        }
    ]
    candidate = [
        {
            "step_number": 1,
            "description": "Patch the application",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "fixed text\n",
                }
            ],
            "verification": "",
            "expected_files": ["src/app.py"],
        }
    ]

    def immediate_issues(plan, project_dir=None):
        del project_dir
        if any(
            operation.get("op") == "replace_in_file"
            for operation in plan[0].get("ops") or []
            if isinstance(operation, dict)
        ):
            return {"stale_replace_ops_steps": [1]}
        if not plan[0].get("verification"):
            return {"weak_verification_steps": [1]}
        return {}

    monkeypatch.setattr(
        PlannerService, "find_immediate_repair_step_issues", immediate_issues
    )
    ctx = SimpleNamespace(
        task=SimpleNamespace(plan_position=2),
        orchestration_state=SimpleNamespace(
            project_dir=tmp_path,
            plan=candidate,
        ),
    )

    repaired = _preserve_regressed_weak_verification_plan(
        ctx=ctx,
        previous_plan=previous,
        arbitration={"immediate_repair_issues": {"weak_verification_steps": [1]}},
    )

    assert repaired is not None
    assert repaired[0]["ops"][0]["op"] == "write_file"
    assert repaired[0]["verification"] == "test -f src/app.py"
