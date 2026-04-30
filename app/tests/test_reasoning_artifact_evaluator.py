from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.phases.completion_flow import _run_evaluator


class _Runtime:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def execute_task(self, prompt: str, timeout_seconds: int = 120):
        self.prompts.append(prompt)
        return {"output": "VERDICT: PASS\nNOTES: looks good"}


def test_evaluator_prompt_includes_reasoning_artifact(monkeypatch):
    captured = {}

    def _append_event(**kwargs):
        captured["details"] = kwargs["details"]
        return {"event_id": "evt-1"}

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.append_orchestration_event",
        _append_event,
    )
    runtime = _Runtime()
    orchestration_state = SimpleNamespace(
        execution_results=[{"step_title": "Implement API", "status": "success"}],
        changed_files=["app/main.py"],
        reasoning_artifact={
            "intent": "Implement API changes",
            "planned_actions": ["Update app/main.py"],
            "verification_plan": ["Run tests"],
        },
        project_dir="/tmp/project",
        session_id=5,
        task_id=9,
    )

    _run_evaluator(
        runtime_service=runtime,
        orchestration_state=orchestration_state,
        prompt="Implement API changes",
        summary="Finished work",
        emit_live=lambda *_args, **_kwargs: None,
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    )

    assert "Control-plane reasoning artifact" in runtime.prompts[0]
    assert captured["details"]["reasoning_artifact_used"] is True
