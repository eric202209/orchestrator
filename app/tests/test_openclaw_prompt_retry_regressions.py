from __future__ import annotations

import asyncio

from app.models import Project, Session as SessionModel, Task, TaskStatus
from app.services.openclaw_service import OpenClawSessionService


def _seed_service_models(db_session):
    project = Project(name="Prompt Retry Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Prompt Retry Session",
        status="running",
        is_active=True,
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Retry overflowed prompt",
        description="Regression coverage for compact-prompt retry logic",
        status=TaskStatus.PENDING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    return session, task


def test_execute_task_retries_context_overflow_with_compact_prompt(
    db_session, monkeypatch
):
    session, task = _seed_service_models(db_session)
    service = OpenClawSessionService(
        db_session, session.id, task.id, use_demo_mode=False
    )

    seen_prompts: list[str] = []

    async def fake_execute_task_with_streaming(prompt, timeout_seconds, log_callback):
        seen_prompts.append(prompt)
        if len(seen_prompts) == 1:
            return {
                "status": "failed",
                "mode": "real",
                "output": "",
                "error": "Context window exceeded",
                "logs": [],
            }
        return {
            "status": "completed",
            "mode": "real",
            "output": '{"ok":true}',
            "error": "",
            "logs": [],
        }

    monkeypatch.setattr(
        service, "execute_task_with_streaming", fake_execute_task_with_streaming
    )
    monkeypatch.setattr(service, "_log_entry", lambda *args, **kwargs: None)

    prompt = "\n".join(
        [
            "**Step:** Create the final Vitest test suite",
            "**Context:** " + ("existing workspace details " * 600),
            "**Output:** status, output, verification_output, files_changed, error_message",
        ]
    )

    result = asyncio.run(service.execute_task(prompt, timeout_seconds=30))

    assert result["status"] == "completed"
    assert len(seen_prompts) == 2
    assert len(seen_prompts[1]) < len(seen_prompts[0])
    assert "[Content truncated for performance]" in seen_prompts[1]
