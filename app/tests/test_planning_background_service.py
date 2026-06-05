from __future__ import annotations

import json

from app.models import PlanningArtifact, PlanningSession, Project
from app.services.planning.planning_session_service import PlanningSessionService
from app.services.planning.planner_service import PlannerService


def _create_project(db_session, name: str = "Planning Background Project") -> Project:
    project = Project(
        name=name,
        description="Project with API, frontend, auth, and tests",
        workspace_path="planning-background-project",
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def test_start_session_processes_inline_when_force_inline_enabled(
    db_session, monkeypatch
):
    project = _create_project(db_session)

    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt, source_brain="local": {
            "status": "completed",
            "output": json.dumps(
                {
                    "requirements": "# Requirements",
                    "design": "# Design",
                    "implementation_plan": "# Implementation Plan",
                    "planner_markdown": "\n".join(
                        [
                            "# Project: Planning Background Project",
                            "",
                            "## Task List",
                            "- [ ] TASK_START: Add planning background worker | Queue planning in Celery | order=1 | P1 | effort=medium | profile=full_lifecycle",
                            "- [ ] TASK_START: Recover active sessions | Requeue unfinished planning runs | order=2 | P1 | effort=small | profile=full_lifecycle",
                            "- [ ] TASK_START: Add tests | Cover background planning flow | order=3 | P1 | effort=small | profile=test_only",
                        ]
                    ),
                }
            ),
        },
    )
    monkeypatch.setattr(
        PlanningSessionService,
        "_decide_clarification",
        lambda self, current_session, current_project: {
            "needs_clarification": False,
            "question": None,
        },
    )

    service = PlanningSessionService(db_session)
    session = service.start_session(
        project,
        "Add JWT authentication to the API and frontend with tests and rollout notes.",
    )

    assert session.status == "completed"
    assert session.processing_token is None
    assert session.processing_started_at is None
    assert len(session.artifacts) == 4


def test_process_session_sets_waiting_and_releases_processing_lease(
    db_session, monkeypatch
):
    project = _create_project(db_session, name="Waiting Project")
    session = PlanningSession(
        project_id=project.id,
        title="Need more detail",
        prompt="Improve planner",
        status="active",
        source_brain="local",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    service = PlanningSessionService(db_session)
    service._add_message(
        session,
        "user",
        "Improve planner",
        metadata={"kind": "prompt"},
    )
    db_session.commit()

    monkeypatch.setattr(
        PlanningSessionService,
        "_decide_clarification",
        lambda self, current_session, current_project: {
            "needs_clarification": True,
            "question": "Which rollout constraints matter most?",
        },
    )

    updated = service.process_session(session.id)

    assert updated is not None
    assert updated.status == "waiting_for_input"
    assert updated.current_prompt_id
    assert updated.processing_token is None
    assert updated.processing_started_at is None
    assert updated.messages[-1].content == "Which rollout constraints matter most?"


def test_process_session_preserves_operator_cancel_during_runtime_failure(
    db_session, monkeypatch
):
    project = _create_project(db_session, name="Cancelled During Runtime Project")
    session = PlanningSession(
        project_id=project.id,
        title="Cancel during runtime",
        prompt="Generate an intentionally overlarge plan",
        status="active",
        source_brain="local",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    service = PlanningSessionService(db_session)
    service._add_message(
        session,
        "user",
        session.prompt,
        metadata={"kind": "prompt", "skip_clarification": True},
    )
    db_session.commit()

    monkeypatch.setattr(
        PlanningSessionService,
        "_decide_clarification",
        lambda self, current_session, current_project: {
            "needs_clarification": False,
            "question": None,
        },
    )

    def fake_run_openclaw(self, prompt, *, source_brain="local", timeout_seconds=None):
        db_session.query(PlanningSession).filter(
            PlanningSession.id == session.id
        ).update(
            {
                "status": "cancelled",
                "processing_token": None,
                "processing_started_at": None,
            }
        )
        db_session.commit()
        raise RuntimeError("Ollama timed out after 90.0s")

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", fake_run_openclaw)

    updated = service.process_session(session.id)

    assert updated is not None
    assert updated.status == "cancelled"
    assert updated.last_error is None
    assert updated.processing_token is None
    assert db_session.query(PlanningArtifact).count() == 0


def test_malformed_planning_synthesis_failure_writes_diagnostic_artifact(
    db_session, monkeypatch
):
    project = _create_project(db_session, name="Malformed Synthesis Project")
    session = PlanningSession(
        project_id=project.id,
        title="Malformed synthesis",
        prompt="Plan a settings form with validation and tests.",
        status="active",
        source_brain="local",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    service = PlanningSessionService(db_session)
    service._add_message(
        session,
        "user",
        session.prompt,
        metadata={"kind": "prompt", "skip_clarification": True},
    )
    db_session.commit()

    compact_output = (
        '{"requirements": "# Requirements",\n'
        ' "design": "# Design",\n'
        ' "implementation_plan" "missing colon",\n'
        ' "planner_markdown": "## Task List"}'
    )
    outputs = [
        '{"requirements": "# Requirements", "design" "missing colon"}',
        compact_output,
    ]

    def fake_run_openclaw(
        self,
        prompt,
        *,
        source_brain="local",
        timeout_seconds=None,
    ):
        return {
            "status": "completed",
            "output": outputs.pop(0),
            "backend": "direct_ollama",
            "model_family": "qwen3:8b-hybrid",
        }

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", fake_run_openclaw)

    updated = service.process_session(session.id)

    assert updated is not None
    assert updated.status == "failed"
    assert "Expecting ':' delimiter" in (updated.last_error or "")

    diagnostic = (
        db_session.query(PlanningArtifact)
        .filter(
            PlanningArtifact.planning_session_id == updated.id,
            PlanningArtifact.artifact_type
            == "planning_synthesis_parse_failure_diagnostic",
        )
        .one()
    )
    payload = json.loads(diagnostic.content)
    assert payload["kind"] == "planning_synthesis_parse_failure"
    assert payload["attempt"] == "compact_retry"
    assert payload["backend"] == "direct_ollama"
    assert payload["model_family"] == "qwen3:8b-hybrid"
    assert payload["classification"] == "malformed_json_syntax"
    assert payload["json_error_line"] == 3
    assert payload["json_error_column"] > 0
    assert payload["response_chars"] == len(compact_output)
    assert len(payload["raw_sha256"]) == 64
    assert "first_attempt_error" in payload
    assert "missing colon" in payload["raw_excerpt_head"]


def test_replan_recovery_uses_short_timeout_and_deterministic_fallback(
    db_session, monkeypatch
):
    project = _create_project(db_session, name="Replan Timeout Project")
    session = PlanningSession(
        project_id=project.id,
        title="Recover timeout",
        prompt=(
            "## Failure Context\n\n"
            "The following execution session failed and requires replanning.\n\n"
            "### Failed Tasks\n"
            "- Add final quality check: Task timed out after 180s"
        ),
        status="active",
        source_brain="local",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    service = PlanningSessionService(db_session)
    service._add_message(
        session,
        "user",
        session.prompt,
        metadata={
            "kind": "prompt",
            "skip_clarification": True,
            "replan_recovery": True,
        },
    )
    db_session.commit()

    observed_timeouts: list[int | None] = []

    def fake_run_openclaw(
        self,
        prompt,
        *,
        source_brain="local",
        timeout_seconds=None,
    ):
        observed_timeouts.append(timeout_seconds)
        raise RuntimeError("Task execution failed: Task timed out after 180s")

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", fake_run_openclaw)

    updated = service.process_session(session.id)

    assert updated is not None
    assert updated.status == "completed", updated.last_error
    assert observed_timeouts == [
        PlanningSessionService.REPLAN_SYNTHESIS_TIMEOUT_SECONDS
    ]
    assert updated.last_error is None
    assert any(
        message.metadata_json and message.metadata_json.get("kind") == "replan_fallback"
        for message in updated.messages
    )
    planner = next(
        artifact
        for artifact in updated.artifacts
        if artifact.artifact_type == "planner_markdown"
    )
    assert "Diagnose recovered failure" in planner.content
    assert "Plan bounded recovery approach" in planner.content
    assert "Apply targeted recovery fix" in planner.content
    assert "Validate recovery path" in planner.content
    assert "Review recovery outcome" in planner.content
    parsed = PlannerService.parse_markdown(planner.content)
    assert [task.execution_profile for task in parsed] == [
        "review_only",
        "review_only",
        "debug_only",
        "test_only",
        "review_only",
    ]
    assert [task.workflow_stage for task in parsed] == [
        "diagnose",
        "plan",
        "debug",
        "validate",
        "complete",
    ]


def test_replan_recovery_replaces_full_lifecycle_model_tasks_with_scoped_tasks(
    db_session, monkeypatch
):
    project = _create_project(db_session, name="Replan Scope Project")
    session = PlanningSession(
        project_id=project.id,
        title="Recover full lifecycle model output",
        prompt=(
            "## Failure Context\n\n"
            "The following execution session failed and requires replanning.\n\n"
            "### Failed Tasks\n"
            "- Build static page: no source files were produced"
        ),
        status="active",
        source_brain="local",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    service = PlanningSessionService(db_session)
    service._add_message(
        session,
        "user",
        session.prompt,
        metadata={
            "kind": "prompt",
            "skip_clarification": True,
            "replan_recovery": True,
        },
    )
    db_session.commit()

    def fake_run_openclaw(
        self,
        prompt,
        *,
        source_brain="local",
        timeout_seconds=None,
    ):
        return {
            "status": "completed",
            "output": json.dumps(
                {
                    "requirements": "# Requirements",
                    "design": "# Design",
                    "implementation_plan": "# Implementation Plan",
                    "planner_markdown": "\n".join(
                        [
                            "# Project: Replan Scope Project",
                            "",
                            "## Task List",
                            "- [ ] TASK_START: Setup Project Environment | Rebuild the page from scratch | order=1 | profile=full_lifecycle",
                            "- [ ] TASK_START: Test the Page | Verify the page works | order=2 | profile=full_lifecycle",
                        ]
                    ),
                }
            ),
        }

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", fake_run_openclaw)

    updated = service.process_session(session.id)

    assert updated is not None
    assert updated.status == "completed", updated.last_error
    assert any(
        message.metadata_json
        and message.metadata_json.get("kind") == "replan_scope_fallback"
        for message in updated.messages
    )
    planner = next(
        artifact
        for artifact in updated.artifacts
        if artifact.artifact_type == "planner_markdown"
    )
    parsed = PlannerService.parse_markdown(planner.content)
    assert [task.execution_profile for task in parsed] == [
        "review_only",
        "review_only",
        "debug_only",
        "test_only",
        "review_only",
    ]
    assert [task.workflow_stage for task in parsed] == [
        "diagnose",
        "plan",
        "debug",
        "validate",
        "complete",
    ]


def test_recover_active_sessions_clears_processing_lease_before_rescheduling(
    db_session, monkeypatch
):
    project = _create_project(db_session, name="Recovery Lease Project")
    session = PlanningSession(
        project_id=project.id,
        title="Recover me",
        prompt="Recover me",
        status="active",
        source_brain="local",
        processing_token="stuck-token",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    queued: list[int] = []
    monkeypatch.setattr(
        PlanningSessionService,
        "schedule_processing",
        lambda self, session_id: queued.append(session_id),
    )

    recovered = PlanningSessionService(db_session).recover_active_sessions()
    db_session.refresh(session)

    assert recovered == [session.id]
    assert queued == [session.id]
    assert session.processing_token is None
    assert session.processing_started_at is None


def test_commit_preserves_artifact_history_and_exposes_latest(db_session, monkeypatch):
    project = _create_project(db_session, name="Artifact History Project")

    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt, source_brain="local": {
            "status": "completed",
            "output": json.dumps(
                {
                    "requirements": "# Requirements",
                    "design": "# Design",
                    "implementation_plan": "# Implementation Plan",
                    "planner_markdown": "\n".join(
                        [
                            "# Project: Artifact History Project",
                            "",
                            "## Task List",
                            "- [ ] TASK_START: Original task | Original artifact content | order=1 | P1 | effort=medium | profile=full_lifecycle",
                            "- [ ] TASK_START: Follow-up task | Another task | order=2 | P1 | effort=small | profile=test_only",
                            "- [ ] TASK_START: Rollout task | Rollout notes | order=3 | P2 | effort=small | profile=review_only",
                        ]
                    ),
                }
            ),
        },
    )
    monkeypatch.setattr(
        PlanningSessionService,
        "_decide_clarification",
        lambda self, current_session, current_project: {
            "needs_clarification": False,
            "question": None,
        },
    )

    service = PlanningSessionService(db_session)
    session = service.start_session(
        project,
        "Add a resumable planning worker with artifact history and tests.",
    )

    edited_markdown = "\n".join(
        [
            "# Project: Artifact History Project",
            "",
            "## Task List",
            "- [ ] TASK_START: Edited task | Edited planner artifact content | order=1 | P1 | effort=medium | profile=full_lifecycle",
        ]
    )
    updated_session, _, _ = service.commit(
        session.id,
        selected_tasks=None,
        planner_markdown=edited_markdown,
    )

    planner_artifacts = (
        db_session.query(PlanningArtifact)
        .filter(
            PlanningArtifact.planning_session_id == updated_session.id,
            PlanningArtifact.artifact_type == "planner_markdown",
        )
        .order_by(PlanningArtifact.version.asc(), PlanningArtifact.id.asc())
        .all()
    )

    assert len(planner_artifacts) == 2
    assert planner_artifacts[0].is_latest is False
    assert planner_artifacts[0].content != edited_markdown
    assert planner_artifacts[1].is_latest is True
    assert planner_artifacts[1].content == edited_markdown

    payload = service.build_session_payload(updated_session)
    assert len(payload["artifacts"]) == 4
    latest_planner = next(
        artifact
        for artifact in payload["artifacts"]
        if artifact.artifact_type == "planner_markdown"
    )
    assert latest_planner.content == edited_markdown
