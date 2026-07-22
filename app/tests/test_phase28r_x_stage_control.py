"""Focused Phase 28R-X planning stage-control tests."""

from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from app.models import Project
from app.schemas import PlanningSessionCreateRequest
from app.services.orchestration.stage_engine import StageRunResult, StageStatus
from app.services.planning.planning_session_service import PlanningSessionService


class _RecordingStageExecutor:
    configuration = {}
    graph = SimpleNamespace(definitions=())

    def __init__(self):
        self.targets: list[str | None] = []

    def advance(self, _session_id, *, target_stage=None, **_kwargs):
        self.targets.append(target_stage)
        return StageRunResult(
            StageStatus.PAUSED,
            reason="target stage reached",
            target_stage=target_stage,
            target_reached=True,
        )


def _project(db_session):
    project = Project(
        name=f"Phase 28R-X {uuid.uuid4().hex[:8]}",
        workspace_path=f"phase28r-x-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def test_target_stage_is_persisted_and_projects_target_reached_lifecycle(
    db_session, monkeypatch
):
    executor = _RecordingStageExecutor()
    service = PlanningSessionService(db_session, stage_executor=executor)
    monkeypatch.setattr(service, "schedule_processing", lambda *_args: None)
    session = service.start_session(
        _project(db_session),
        "Generate a bounded Brief.",
        protocol_version="v2",
        target_stage="planning_brief",
    )

    processed = service.process_session(session.id)
    payload = service.build_session_payload(processed)

    assert executor.targets == ["planning_brief"]
    assert processed.status == "paused"
    assert processed.messages[0].metadata_json["target_stage"] == "planning_brief"
    assert payload["target_stage"] == "planning_brief"
    assert payload["planning_completion_state"] == "target_reached"


def test_unknown_target_is_rejected_by_create_request():
    with pytest.raises(ValueError):
        PlanningSessionCreateRequest(
            project_id=1,
            prompt="Generate a Brief.",
            protocol_version="v2",
            target_stage="unknown",
        )


def test_advance_stage_requires_and_records_exact_brief_checkpoint_authority(
    db_session, monkeypatch
):
    executor = _RecordingStageExecutor()
    service = PlanningSessionService(db_session, stage_executor=executor)
    monkeypatch.setattr(service, "schedule_processing", lambda *_args: None)
    session = service.start_session(
        _project(db_session),
        "Generate a bounded Brief.",
        protocol_version="v2",
        target_stage="planning_brief",
    )
    service.process_session(session.id)
    checkpoint = SimpleNamespace(id=42, status="accepted")
    monkeypatch.setattr(
        service.protocol_persistence,
        "effective_checkpoints",
        lambda *_args, **_kwargs: {("planning_brief", 1): checkpoint},
    )

    advanced = service.advance_to_stage(
        session.id,
        "structured_task_plan",
        accepted_brief_checkpoint_id=42,
    )

    assert advanced.status == "active"
    assert advanced.messages[-1].metadata_json == {
        "kind": "stage_control",
        "target_stage": "structured_task_plan",
        "accepted_brief_checkpoint_id": 42,
        "independent_attempt": False,
    }
