"""Tests for GET /sessions/{session_id}/decision-timeline."""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models import (
    InterventionRequest,
    KnowledgeItem,
    KnowledgeUsageLog,
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskCheckpoint,
    TaskStatus,
)


def _make_project(db, *, workspace_path: str):
    project = Project(
        name=f"Timeline Project {uuid.uuid4()}",
        workspace_path=workspace_path,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_session(db, project, *, status: str = "stopped", deleted_at=None):
    existing_count = (
        db.query(SessionModel).filter(SessionModel.project_id == project.id).count()
    )
    session = SessionModel(
        project_id=project.id,
        name=f"Timeline Session {existing_count + 1}",
        description="test",
        status=status,
        is_active=False,
        execution_mode="manual",
        deleted_at=deleted_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_task(db, project, session, *, title: str = "Timeline Task"):
    task = Task(
        project_id=project.id,
        title=f"{title} {uuid.uuid4()}",
        status=TaskStatus.PENDING,
        task_subfolder=f"task-{uuid.uuid4()}",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    db.add(
        SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.PENDING,
        )
    )
    db.commit()
    return task


def _write_events(
    workspace_path: str,
    session_id: int,
    task_id: int,
    events: list[dict],
    *,
    malformed: bool = False,
) -> None:
    events_dir = Path(workspace_path) / ".openclaw" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    log_path = events_dir / f"session_{session_id}_task_{task_id}.jsonl"
    with log_path.open("w", encoding="utf-8") as handle:
        if malformed:
            handle.write("{not-valid-json\n")
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _event(
    *,
    event_id: str,
    session_id: int,
    task_id: int,
    event_type: str,
    timestamp: datetime,
    details: dict | None = None,
    parent_event_id: str | None = None,
):
    return {
        "event_id": event_id,
        "timestamp": timestamp.isoformat(),
        "event_type": event_type,
        "session_id": session_id,
        "task_id": task_id,
        "parent_event_id": parent_event_id,
        "details": details or {},
    }


def _make_knowledge_item(
    db,
    *,
    title="Timeline Knowledge",
    knowledge_type="debug_case",
):
    content = f"{title} content"
    item = KnowledgeItem(
        id=str(uuid.uuid4()),
        title=title,
        content=content,
        knowledge_type=knowledge_type,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_usage_log(db, session, item, *, task_id: int | None, phase: str):
    usage = KnowledgeUsageLog(
        session_id=session.id,
        task_id=task_id,
        knowledge_item_id=item.id,
        trigger_phase=phase,
        retrieval_reason="phase_test_retrieval",
        retrieval_query="test query",
        confidence=0.75,
        rank=0,
        used_in_prompt=True,
        was_effective=None,
    )
    db.add(usage)
    db.commit()
    db.refresh(usage)
    return usage


def test_decision_timeline_empty_session(authenticated_client, db_session):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == session.id
        assert body["events"] == []
        assert body["counts"] == {
            "planning": 0,
            "validation": 0,
            "execution": 0,
            "failure": 0,
            "completion": 0,
        }
        assert body["truncated"] is False


def test_decision_timeline_unknown_and_deleted_session(
    authenticated_client, db_session
):
    resp = authenticated_client.get("/api/v1/sessions/99999/decision-timeline")
    assert resp.status_code == 404

    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(
            db_session,
            project,
            deleted_at=datetime.now(UTC),
        )

        deleted_resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )
        assert deleted_resp.status_code == 404


def test_decision_timeline_merges_multi_task_events_sorted_by_timestamp(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task_late = _make_task(db_session, project, session, title="Late")
        task_early = _make_task(db_session, project, session, title="Early")
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)

        _write_events(
            tmpdir,
            session.id,
            task_late.id,
            [
                _event(
                    event_id="late",
                    session_id=session.id,
                    task_id=task_late.id,
                    event_type="task_completed",
                    timestamp=base + timedelta(minutes=2),
                )
            ],
        )
        _write_events(
            tmpdir,
            session.id,
            task_early.id,
            [
                _event(
                    event_id="early",
                    session_id=session.id,
                    task_id=task_early.id,
                    event_type="task_started",
                    timestamp=base,
                )
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert [event["id"] for event in body["events"]] == ["early", "late"]
        assert [event["task_id"] for event in body["events"]] == [
            task_early.id,
            task_late.id,
        ]


def test_decision_timeline_ignores_malformed_jsonl(authenticated_client, db_session):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)

        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="valid",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="validation_result",
                    timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
                    details={"stage": "plan", "status": "accepted"},
                )
            ],
            malformed=True,
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["id"] == "valid"


def test_decision_timeline_phase_filter(authenticated_client, db_session):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)

        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="planning",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="phase_started",
                    timestamp=base,
                    details={"phase": "planning"},
                ),
                _event(
                    event_id="validation",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="validation_result",
                    timestamp=base + timedelta(seconds=1),
                    details={"stage": "plan", "status": "rejected"},
                ),
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline",
            params={"phase": "validation"},
        )

        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["id"] == "validation"
        assert events[0]["phase"] == "validation"


def test_decision_timeline_limit_is_capped(authenticated_client, db_session):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)

        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id=f"event-{idx}",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="step_started",
                    timestamp=base + timedelta(seconds=idx),
                    details={"step_number": idx + 1},
                )
                for idx in range(305)
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline",
            params={"limit": 9999},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 300
        assert len(body["events"]) == 300
        assert body["truncated"] is True


def test_decision_timeline_attaches_knowledge_conservatively(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        item = _make_knowledge_item(db_session)
        usage = _make_usage_log(
            db_session,
            session,
            item,
            task_id=task.id,
            phase="validation",
        )

        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="validation",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="validation_result",
                    timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
                    details={"stage": "plan", "status": "accepted"},
                )
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        event = resp.json()["events"][0]
        assert event["knowledge_usage_ids"] == [usage.id]
        assert (
            event["details"]["knowledge_association_label"]
            == "knowledge used during this phase"
        )
        knowledge = event["details"]["knowledge_used_during_phase"][0]
        assert knowledge["usage_log_id"] == usage.id
        assert knowledge["causal"] is False
        assert knowledge["association"] == "knowledge used during this phase"


def test_decision_timeline_includes_intervention_metadata(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project, status="awaiting_input")
        task = _make_task(db_session, project, session)
        intervention = InterventionRequest(
            session_id=session.id,
            task_id=task.id,
            project_id=project.id,
            intervention_type="guidance",
            initiated_by="system",
            prompt="Need operator input",
            status="pending",
        )
        db_session.add(intervention)
        db_session.commit()
        db_session.refresh(intervention)

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 1
        event = events[0]
        assert event["source"] == "intervention_request"
        assert event["event_type"] == "human_intervention_requested"
        assert event["intervention_id"] == intervention.id
        assert event["task_id"] == task.id
        assert event["details"]["intervention_type"] == "guidance"
        assert event["details"]["status"] == "pending"


def test_decision_timeline_endpoint_is_read_only(authenticated_client, db_session):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        item = _make_knowledge_item(db_session)
        _make_usage_log(
            db_session,
            session,
            item,
            task_id=task.id,
            phase="execution",
        )
        db_session.add(
            LogEntry(
                session_id=session.id,
                task_id=task.id,
                level="INFO",
                message="existing log",
            )
        )
        db_session.commit()
        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="step",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="step_started",
                    timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
                )
            ],
        )

        before = {
            "logs": db_session.query(LogEntry).count(),
            "checkpoints": db_session.query(TaskCheckpoint).count(),
            "interventions": db_session.query(InterventionRequest).count(),
            "knowledge_usage": db_session.query(KnowledgeUsageLog).count(),
            "session_tasks": db_session.query(SessionTask).count(),
        }

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        after = {
            "logs": db_session.query(LogEntry).count(),
            "checkpoints": db_session.query(TaskCheckpoint).count(),
            "interventions": db_session.query(InterventionRequest).count(),
            "knowledge_usage": db_session.query(KnowledgeUsageLog).count(),
            "session_tasks": db_session.query(SessionTask).count(),
        }
        assert after == before


def test_decision_timeline_surfaces_explicit_parent_links(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)

        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="phase",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="phase_started",
                    timestamp=base,
                    details={"phase": "execution"},
                ),
                _event(
                    event_id="step",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="step_started",
                    timestamp=base + timedelta(seconds=1),
                    parent_event_id="phase",
                ),
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        step_event = resp.json()["events"][1]
        assert step_event["parent_event_id"] == "phase"
        assert step_event["related_event_ids"] == ["phase"]
        assert step_event["details"]["causal_links"] == [
            {
                "relation": "explicit_parent",
                "event_id": "phase",
                "inferred": False,
                "confidence": "exact",
            }
        ]


def test_decision_timeline_links_retry_chain(authenticated_client, db_session):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)

        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="tool-failed",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="tool_failed",
                    timestamp=base,
                ),
                _event(
                    event_id="retry-1",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="retry_entered",
                    timestamp=base + timedelta(seconds=1),
                ),
                _event(
                    event_id="retry-2",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="retry_entered",
                    timestamp=base + timedelta(seconds=2),
                ),
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        retry_1 = resp.json()["events"][1]
        retry_2 = resp.json()["events"][2]
        assert retry_1["related_event_ids"] == ["tool-failed"]
        assert retry_1["details"]["causal_links"] == [
            {
                "relation": "retry_after_failure",
                "event_id": "tool-failed",
                "inferred": True,
                "confidence": "high",
            }
        ]
        assert retry_2["related_event_ids"] == ["retry-1", "tool-failed"]
        assert retry_2["details"]["causal_links"] == [
            {
                "relation": "previous_retry",
                "event_id": "retry-1",
                "inferred": True,
                "confidence": "high",
            },
            {
                "relation": "retry_after_failure",
                "event_id": "tool-failed",
                "inferred": True,
                "confidence": "high",
            },
        ]


def test_decision_timeline_links_validation_repair_validation_flow(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)

        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="validation-rejected",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="validation_result",
                    timestamp=base,
                    details={"stage": "task_completion", "status": "rejected"},
                ),
                _event(
                    event_id="repair-generated",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="repair_generated",
                    timestamp=base + timedelta(seconds=1),
                ),
                _event(
                    event_id="validation-accepted",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="validation_result",
                    timestamp=base + timedelta(seconds=2),
                    details={"stage": "task_completion", "status": "accepted"},
                ),
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        repair_event = resp.json()["events"][1]
        accepted_event = resp.json()["events"][2]
        assert repair_event["related_event_ids"] == ["validation-rejected"]
        assert repair_event["details"]["causal_links"] == [
            {
                "relation": "repair_for_validation",
                "event_id": "validation-rejected",
                "inferred": True,
                "confidence": "medium",
            }
        ]
        assert accepted_event["related_event_ids"] == ["repair-generated"]
        assert accepted_event["details"]["causal_links"] == [
            {
                "relation": "validation_after_repair",
                "event_id": "repair-generated",
                "inferred": True,
                "confidence": "medium",
            }
        ]


def test_decision_timeline_links_task_failure_to_latest_cause(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)

        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="validation-rejected",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="validation_result",
                    timestamp=base,
                    details={"stage": "task_completion", "status": "rejected"},
                ),
                _event(
                    event_id="task-failed",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="task_failed",
                    timestamp=base + timedelta(seconds=1),
                ),
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        failed_event = resp.json()["events"][1]
        assert failed_event["related_event_ids"] == ["validation-rejected"]
        assert failed_event["details"]["causal_links"] == [
            {
                "relation": "task_failed_because",
                "event_id": "validation-rejected",
                "inferred": True,
                "confidence": "medium",
            }
        ]


def test_decision_timeline_links_intervention_to_latest_failure(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project, status="awaiting_input")
        task = _make_task(db_session, project, session)
        base = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)

        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="tool-failed",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="tool_failed",
                    timestamp=base,
                )
            ],
        )
        intervention = InterventionRequest(
            session_id=session.id,
            task_id=task.id,
            project_id=project.id,
            intervention_type="guidance",
            initiated_by="system",
            prompt="Need operator input",
            status="pending",
            created_at=base + timedelta(seconds=1),
        )
        db_session.add(intervention)
        db_session.commit()

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        intervention_event = resp.json()["events"][1]
        assert intervention_event["event_type"] == "human_intervention_requested"
        assert intervention_event["related_event_ids"] == ["tool-failed"]
        assert intervention_event["details"]["causal_links"] == [
            {
                "relation": "intervention_after_failure",
                "event_id": "tool-failed",
                "inferred": True,
                "confidence": "medium",
            }
        ]


def test_decision_timeline_keeps_knowledge_attachment_non_causal(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        item = _make_knowledge_item(db_session)
        usage = _make_usage_log(
            db_session,
            session,
            item,
            task_id=task.id,
            phase="validation",
        )

        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="validation-accepted",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="validation_result",
                    timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
                    details={"stage": "plan", "status": "accepted"},
                )
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        event = resp.json()["events"][0]
        assert event["knowledge_usage_ids"] == [usage.id]
        assert event["related_event_ids"] == []
        assert "causal_links" not in event["details"]
        knowledge = event["details"]["knowledge_used_during_phase"][0]
        assert knowledge["association"] == "knowledge used during this phase"
        assert knowledge["causal"] is False
