from __future__ import annotations

from app.models import SystemSetting
from app.models import PlanningMessage, PlanningSession
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.planning.planning_session_service import PlanningSessionService


def test_context_overflow_detection_matches_embedded_agent_variant():
    assert OpenClawSessionService._is_context_overflow_result(
        {"error": "Context size has been exceeded."}
    )
    assert OpenClawSessionService._is_context_overflow_result(
        {"output": "Prompt is too long for the model."}
    )
    assert not OpenClawSessionService._is_context_overflow_result(
        {"error": "Connection refused"}
    )


def test_synthesis_prompt_is_compacted_for_long_transcripts():
    service = PlanningSessionService(db=None)  # type: ignore[arg-type]
    session = PlanningSession(
        id=1,
        project_id=1,
        title="Long planning session",
        prompt="Build an execution-ready plan for a large dashboard rewrite with auth, background jobs, logs, websockets, reporting, settings, and migration safety.",
        status="active",
        source_brain="local",
    )
    session.messages = [
        PlanningMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=("very long planning detail " * 40) + f" message {index}",
        )
        for index in range(8)
    ]
    project = type(
        "ProjectStub",
        (),
        {"name": "Big Project", "description": "existing project " * 80},
    )()

    prompt = service._build_synthesis_prompt(session, project)

    assert len(prompt) <= service.SYNTHESIS_PROMPT_CHAR_BUDGET + 200
    assert "Conversation transcript:" in prompt
    assert prompt.count("Planner:") + prompt.count("User:") <= 6


def test_synthesis_prompt_uses_active_adaptation_profile(monkeypatch):
    service = PlanningSessionService(db=None)  # type: ignore[arg-type]
    session = PlanningSession(
        id=1,
        project_id=1,
        title="Adapted planning session",
        prompt="Add a resilient auth flow with better session recovery.",
        status="active",
        source_brain="local",
    )
    session.messages = [PlanningMessage(role="user", content=session.prompt)]
    project = type(
        "ProjectStub",
        (),
        {"name": "Adapted Project", "description": "Backend and frontend auth work."},
    )()

    monkeypatch.setattr(
        "app.services.planning.planning_session_service.get_effective_adaptation_profile",
        lambda db=None: "openai_responses_default",
    )

    prompt = service._build_synthesis_prompt(session, project)

    assert prompt.startswith('{"objective":"Create implementation-planning artifacts')
    assert '"execution_mode":"planning_synthesis"' in prompt
    assert '"context":{"Project":"Adapted Project"' in prompt


def test_clarification_payload_parser_uses_model_decision():
    service = PlanningSessionService(db=None)  # type: ignore[arg-type]

    parsed = service._parse_clarification_payload(
        {
            "status": "completed",
            "output": '{"needs_clarification": true, "question": "Which rollout constraints matter most?"}',
        },
        fallback_needs=False,
        fallback_question="Fallback question",
    )

    assert parsed == {
        "needs_clarification": True,
        "question": "Which rollout constraints matter most?",
    }


def test_clarification_payload_parser_falls_back_on_invalid_json():
    service = PlanningSessionService(db=None)  # type: ignore[arg-type]

    parsed = service._parse_clarification_payload(
        {"status": "completed", "output": "not-json"},
        fallback_needs=True,
        fallback_question="Fallback question",
    )

    assert parsed == {
        "needs_clarification": True,
        "question": "Fallback question",
    }


def test_openclaw_command_honors_source_brain(monkeypatch):
    class _FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return None

    class _FakeDb:
        def query(self, *args, **kwargs):
            return _FakeQuery()

    service = PlanningSessionService(db=_FakeDb())  # type: ignore[arg-type]

    monkeypatch.setattr(
        OpenClawSessionService,
        "_resolve_openclaw_command",
        lambda self: ["/usr/bin/openclaw"],
    )

    local_cmd = service._build_openclaw_command("plan locally", source_brain="local")
    cloud_cmd = service._build_openclaw_command("plan in cloud", source_brain="cloud")

    assert local_cmd[:3] == ["/usr/bin/openclaw", "agent", "--local"]
    assert "--local" not in cloud_cmd
    assert cloud_cmd[:2] == ["/usr/bin/openclaw", "agent"]


def test_synthesis_prompt_uses_db_selected_adaptation_profile(db_session):
    db_session.add(
        SystemSetting(
            key="orchestrator_adaptation_profile",
            value="openai_responses_default",
        )
    )
    db_session.commit()

    service = PlanningSessionService(db=db_session)
    session = PlanningSession(
        id=2,
        project_id=1,
        title="DB adapted planning session",
        prompt="Improve retries and diagnostics.",
        status="active",
        source_brain="local",
    )
    session.messages = [PlanningMessage(role="user", content=session.prompt)]
    project = type(
        "ProjectStub",
        (),
        {"name": "DB Adapted Project", "description": "Queue and worker cleanup."},
    )()

    prompt = service._build_synthesis_prompt(session, project)

    assert prompt.startswith('{"objective":"Create implementation-planning artifacts')
