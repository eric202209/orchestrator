"""Focused Phase 26C-5 Stage B worker and consumer ownership tests."""

from __future__ import annotations

import ast
import inspect

import pytest

from app.services.agents import agent_runtime
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.orchestration.context import assembly
from app.services.orchestration.lifecycle.worker_bootstrap import (
    should_use_configured_planning_runtime,
)


def _configuration(
    *,
    role: BackendRole,
    backend: str = "direct_ollama",
    model: str = "qwen3.6:27B",
    profile: str = "ollama_default",
) -> RoleRuntimeConfiguration:
    return RoleRuntimeConfiguration(
        role=role,
        backend_name=backend,
        model_family=model,
        adaptation_profile=profile,
    )


@pytest.mark.parametrize(
    ("planning", "execution", "needs_separate"),
    [
        (
            _configuration(role=BackendRole.PLANNING),
            _configuration(role=BackendRole.EXECUTION),
            False,
        ),
        (
            _configuration(role=BackendRole.PLANNING, model="Qwen-Coder"),
            _configuration(role=BackendRole.EXECUTION),
            True,
        ),
        (
            _configuration(
                role=BackendRole.PLANNING,
                profile="planning_default",
            ),
            _configuration(role=BackendRole.EXECUTION),
            True,
        ),
        (
            _configuration(role=BackendRole.PLANNING, backend="direct_ollama"),
            _configuration(role=BackendRole.EXECUTION, backend="local_openclaw"),
            True,
        ),
        (
            _configuration(role=BackendRole.PLANNING, backend="direct_ollama"),
            _configuration(role=BackendRole.EXECUTION, backend="openai_responses_api"),
            True,
        ),
    ],
)
def test_worker_compares_complete_role_configuration(
    planning, execution, needs_separate
):
    assert (
        should_use_configured_planning_runtime(
            planning_backend_override=None,
            planning_config=planning,
            execution_config=execution,
        )
        is needs_separate
    )


def test_operator_override_still_forces_a_planning_runtime():
    assert should_use_configured_planning_runtime(
        planning_backend_override="direct_ollama",
        planning_config=_configuration(role=BackendRole.PLANNING),
        execution_config=_configuration(role=BackendRole.EXECUTION),
    )


def test_planning_prompt_uses_the_resolved_profile_without_re_resolving_settings(
    monkeypatch,
):
    captured = {}

    def fake_render(profile_name, envelope):
        captured["profile"] = profile_name
        captured["envelope"] = envelope
        return "rendered"

    monkeypatch.setattr(assembly, "render_prompt_for_profile", fake_render)

    result = assembly.render_adapted_runtime_prompt(
        None,
        objective="plan",
        execution_mode="planning",
        prompt_body="body",
        adaptation_profile="planning_default",
    )

    assert result == "rendered"
    assert captured["profile"] == "planning_default"


def test_roleless_prompt_compatibility_path_remains_available(monkeypatch):
    monkeypatch.setattr(
        assembly,
        "get_effective_adaptation_profile",
        lambda db=None: "openclaw_default",
    )
    monkeypatch.setattr(
        assembly,
        "render_prompt_for_profile",
        lambda profile_name, envelope: profile_name,
    )

    assert (
        assembly.render_adapted_runtime_prompt(
            None,
            objective="utility",
            execution_mode="utility",
            prompt_body="body",
        )
        == "openclaw_default"
    )


def test_one_shot_runtime_forwards_role_and_historical_backend(monkeypatch):
    captured = {}

    class FakeRuntime:
        async def invoke_prompt(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return {"output": "ok"}

    def fake_create(db, session_id, task_id=None, **kwargs):
        captured["kwargs"] = kwargs
        return FakeRuntime()

    monkeypatch.setattr(agent_runtime, "create_agent_runtime", fake_create)

    result = agent_runtime.invoke_runtime_prompt(
        object(),
        "inspect execution",
        session_id=7,
        task_id=8,
        role=BackendRole.EXECUTION,
        backend_override="local_openclaw",
    )

    assert result == {"output": "ok"}
    assert captured["prompt"] == "inspect execution"
    assert captured["kwargs"] == {
        "role": BackendRole.EXECUTION,
        "backend_override": "local_openclaw",
    }


def _calls_missing_role(source: str, function_names: set[str]) -> list[str]:
    tree = ast.parse(source)
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in function_names:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            callee = call.func
            if isinstance(callee, ast.Name):
                name = callee.id
            elif isinstance(callee, ast.Attribute):
                name = callee.attr
            else:
                continue
            if name not in {"create_agent_runtime", "invoke_runtime_prompt"}:
                continue
            if not any(keyword.arg == "role" for keyword in call.keywords):
                missing.append(f"{node.name}:{name}")
    return missing


@pytest.mark.parametrize(
    ("module_name", "function_names"),
    [
        (
            "app.services.session.session_lifecycle_service",
            {
                "start_session_lifecycle",
                "stop_session_lifecycle",
                "pause_session_lifecycle",
            },
        ),
        (
            "app.services.session.session_execution_service",
            {"start_session_payload"},
        ),
        (
            "app.services.session.session_inspection_service",
            {
                "save_session_checkpoint_payload",
                "load_session_checkpoint_payload",
                "_generate_enriched_digest",
            },
        ),
        ("app.services.session.replan_service", {"_generate_summary_via_llm"}),
        ("app.tasks.worker", {"answer_human_intervention_query"}),
    ],
)
def test_migrated_consumers_pass_an_explicit_role(module_name, function_names):
    module = __import__(module_name, fromlist=["*"])
    assert _calls_missing_role(inspect.getsource(module), function_names) == []


def test_replan_synthesis_remains_planning_owned():
    from app.services.planning.planning_session_service import PlanningSessionService

    source = inspect.getsource(PlanningSessionService._run_openclaw)
    assert "role=BackendRole.PLANNING" in source
