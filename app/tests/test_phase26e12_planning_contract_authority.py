from __future__ import annotations

import pytest

from app.models import PlanningMessage, PlanningSession
from app.services.agents.providers import openai_chat_adapter, ollama_adapter
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.providers.ollama_adapter import OllamaRuntime
from app.services.planning.planning_session_service import PlanningSessionService


CANONICAL_ARTIFACT_PROMPT = (
    "Return JSON only with exactly these keys: requirements, design, "
    "implementation_plan, planner_markdown."
)


@pytest.mark.parametrize(
    "runtime_class, generic_system",
    [
        (OllamaRuntime, ollama_adapter._GENERIC_SYSTEM),
        (OpenAIChatCompletionsRuntime, openai_chat_adapter._GENERIC_SYSTEM),
    ],
)
@pytest.mark.asyncio
async def test_planning_provider_does_not_inject_execution_array_contract(
    db_session, monkeypatch, runtime_class, generic_system
):
    runtime = runtime_class(db_session, session_id=None)
    observed: dict[str, object] = {}

    async def fake_chat(*args, **kwargs):
        del args
        observed.update(kwargs)
        return "{}"

    monkeypatch.setattr(runtime, "_chat", fake_chat)

    result = await runtime.invoke_prompt(
        CANONICAL_ARTIFACT_PROMPT,
        session_prefix="planning",
    )

    assert result["status"] == "completed"
    assert observed["user"] == CANONICAL_ARTIFACT_PROMPT
    assert observed["system"] == generic_system
    assert "JSON array" not in str(observed["system"])
    assert "ops" not in str(observed["system"])


def test_planning_session_service_owns_canonical_artifact_contract():
    service = PlanningSessionService(db=None)  # type: ignore[arg-type]
    session = PlanningSession(
        id=1,
        project_id=1,
        title="Contract authority",
        prompt="Create a bounded implementation plan with tests.",
        status="active",
        source_brain="local",
    )
    session.messages = [PlanningMessage(role="user", content=session.prompt)]
    project = type(
        "ProjectStub",
        (),
        {
            "name": "Contract Project",
            "description": "A focused planning contract test.",
            "project_rules": None,
        },
    )()

    prompt = service._build_synthesis_prompt(session, project)

    assert "Return JSON only with exactly these keys" in prompt
    assert all(
        key in prompt
        for key in ("requirements", "design", "implementation_plan", "planner_markdown")
    )
    assert "A JSON object with requirements" in prompt
    assert "JSON array" not in prompt
