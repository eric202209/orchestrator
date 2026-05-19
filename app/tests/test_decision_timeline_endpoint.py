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
    TaskExecution,
    TaskStatus,
)
from app.services.task_service import TASK_CHANGE_SET_LOG_MESSAGE


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


def test_decision_timeline_includes_workspace_change_set_logs(
    authenticated_client,
    db_session,
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project, status="pending")
        task = Task(
            project_id=project.id,
            title="Capture governance change set",
            status=TaskStatus.DONE,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.DONE,
        )
        db_session.add(execution)
        db_session.commit()
        db_session.refresh(execution)
        db_session.add(
            LogEntry(
                session_id=session.id,
                task_id=task.id,
                task_execution_id=execution.id,
                level="INFO",
                message=TASK_CHANGE_SET_LOG_MESSAGE,
                log_metadata=json.dumps(
                    {
                        "task_execution_id": execution.id,
                        "task_id": task.id,
                        "changed_count": 3,
                        "added_files": ["src/app.py"],
                        "modified_files": ["README.md"],
                        "deleted_files": ["old.md"],
                        "warning_flags": ["deleted_files"],
                    }
                ),
            )
        )
        db_session.commit()

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"]["completion"] == 1
        event = body["events"][0]
        assert event["source"] == "workspace_change_set_log"
        assert event["phase"] == "completion"
        assert event["event_type"] == "workspace_change_set_captured"
        assert event["task_id"] == task.id
        assert event["details"]["changed_count"] == 3
        assert event["details"]["warning_flags"] == ["deleted_files"]


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


def test_decision_timeline_resolves_relative_project_workspace_path(
    authenticated_client, db_session, tmp_path, monkeypatch
):
    workspace_root = tmp_path / "vault" / "projects"
    project_dir = workspace_root / "microsite"
    monkeypatch.setattr(
        "app.services.workspace.project_isolation_service.get_effective_workspace_root",
        lambda db=None: workspace_root,
    )

    project = _make_project(db_session, workspace_path="microsite")
    session = _make_session(db_session, project)
    task = _make_task(db_session, project, session)
    _write_events(
        str(project_dir),
        session.id,
        task.id,
        [
            _event(
                event_id="relative-path-event",
                session_id=session.id,
                task_id=task.id,
                event_type="task_started",
                timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
            )
        ],
    )

    resp = authenticated_client.get(f"/api/v1/sessions/{session.id}/decision-timeline")

    assert resp.status_code == 200
    body = resp.json()
    assert [event["id"] for event in body["events"]] == ["relative-path-event"]


def test_decision_timeline_reads_legacy_task_subfolder_event_journal(
    authenticated_client, db_session, tmp_path
):
    project_dir = tmp_path / "legacy-project"
    legacy_task_dir = project_dir / "task-legacy-folder"
    project = _make_project(db_session, workspace_path=str(project_dir))
    session = _make_session(db_session, project)
    task = _make_task(db_session, project, session)
    task.task_subfolder = "task-legacy-folder"
    db_session.commit()

    _write_events(
        str(legacy_task_dir),
        session.id,
        task.id,
        [
            _event(
                event_id="legacy-task-folder-event",
                session_id=session.id,
                task_id=task.id,
                event_type="phase_started",
                timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
                details={"phase": "planning"},
            )
        ],
    )

    resp = authenticated_client.get(f"/api/v1/sessions/{session.id}/decision-timeline")

    assert resp.status_code == 200
    body = resp.json()
    assert [event["id"] for event in body["events"]] == ["legacy-task-folder-event"]


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


def test_decision_timeline_preserves_validation_diagnostics(
    authenticated_client, db_session
):
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
                    event_id="diagnostic-validation",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="validation_result",
                    timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
                    details={
                        "stage": "plan",
                        "status": "repair_required",
                        "validation_reasons": [
                            "Plan contains brittle heredoc-heavy or malformed commands"
                        ],
                        "brittle_command_subcodes": ["oversized_command_length"],
                        "brittle_command_step_details": {
                            "2": ["oversized_command_length"]
                        },
                        "shadow_warnings": [
                            {
                                "rule_id": "model_behavior.command_length_prompt_patch",
                                "category": "model_behavior_patch",
                                "shadow_candidate": True,
                            }
                        ],
                        "max_command_length": 1456,
                    },
                )
            ],
        )

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        details = resp.json()["events"][0]["details"]
        assert details["validation_reasons"] == [
            "Plan contains brittle heredoc-heavy or malformed commands"
        ]
        assert details["brittle_command_subcodes"] == ["oversized_command_length"]
        assert details["brittle_command_step_details"] == {
            "2": ["oversized_command_length"]
        }
        assert details["shadow_warnings"] == [
            {
                "rule_id": "model_behavior.command_length_prompt_patch",
                "category": "model_behavior_patch",
                "shadow_candidate": True,
            }
        ]
        assert details["max_command_length"] == 1456


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


def test_decision_timeline_surfaces_terminal_planning_failure_metadata(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        completed_at = datetime(2026, 5, 8, 3, 47, tzinfo=UTC)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.FAILED,
            completed_at=completed_at,
        )
        db_session.add(execution)
        db_session.commit()
        db_session.refresh(execution)

        db_session.add(
            LogEntry(
                session_id=session.id,
                task_id=task.id,
                task_execution_id=execution.id,
                level="ERROR",
                message="[ORCHESTRATION] Plan validation failed after repair",
                created_at=completed_at,
                log_metadata=json.dumps(
                    {
                        "reason": "planning_validation_failed_after_repair",
                        "validation_reasons": [
                            "Plan contains brittle heredoc-heavy or malformed commands"
                        ],
                        "brittle_command_subcodes": ["oversized_command_length"],
                        "brittle_command_step_details": {
                            "2": ["oversized_command_length"]
                        },
                        "brittle_command_step_command_lengths": {"2": [1684]},
                        "shadow_warnings": [
                            {
                                "rule_id": "model_behavior.command_length_prompt_patch",
                                "category": "model_behavior_patch",
                                "shadow_candidate": True,
                            }
                        ],
                    }
                ),
            )
        )
        db_session.commit()

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        payload = resp.json()
        failure_events = [
            event
            for event in payload["events"]
            if event["source"] == "terminal_log_metadata"
        ]
        assert len(failure_events) == 1
        failure = failure_events[0]
        assert failure["phase"] == "failure"
        assert failure["status"] == "failed"
        assert failure["details"]["task_execution_id"] == execution.id
        assert failure["details"]["reason"] == "planning_validation_failed_after_repair"
        assert failure["details"]["brittle_command_subcodes"] == [
            "oversized_command_length"
        ]
        assert failure["details"]["brittle_command_step_details"] == {
            "2": ["oversized_command_length"]
        }
        assert failure["details"]["shadow_warnings"] == [
            {
                "rule_id": "model_behavior.command_length_prompt_patch",
                "category": "model_behavior_patch",
                "shadow_candidate": True,
            }
        ]
        assert failure["details"]["repair_attempted"] is True
        assert failure["details"]["targeted_second_repair_attempted"] is False
        assert "Brittle-command" in failure["details"]["no_further_repair_reason"]
        assert "operator_next_action" in failure["details"]
        assert payload["counts"]["failure"] == 1


def test_decision_timeline_surfaces_timeout_and_repair_contract_terminals(
    authenticated_client, db_session
):
    cases = [
        {
            "reason": "planning_context_overflow",
            "message": "[ORCHESTRATION] Planning timed out or exceeded context",
            "title": "Planning Timed Out Or Exceeded Context",
            "repair_attempted": False,
        },
        {
            "reason": "repair_output_contract_violation",
            "message": "[ORCHESTRATION] Repair output contract violation",
            "title": "Repair Output Contract Violation",
            "repair_attempted": True,
        },
        {
            "reason": "planning_openclaw_lock_contention",
            "message": "[ORCHESTRATION] OpenClaw session lock contention",
            "title": "OpenClaw Session Lock Contention",
            "repair_attempted": False,
        },
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        base = datetime(2026, 5, 8, 4, 0, tzinfo=UTC)
        session_ids = []

        for index, case in enumerate(cases):
            session = _make_session(db_session, project)
            task = _make_task(db_session, project, session)
            execution = TaskExecution(
                session_id=session.id,
                task_id=task.id,
                attempt_number=1,
                status=TaskStatus.FAILED,
                completed_at=base + timedelta(minutes=index),
            )
            db_session.add(execution)
            db_session.commit()
            db_session.refresh(execution)
            db_session.add(
                LogEntry(
                    session_id=session.id,
                    task_id=task.id,
                    task_execution_id=execution.id,
                    level="ERROR",
                    message=case["message"],
                    created_at=execution.completed_at,
                    log_metadata=json.dumps(
                        {
                            "phase": "planning",
                            "reason": case["reason"],
                            "task_execution_id": execution.id,
                        }
                    ),
                )
            )
            db_session.commit()
            session_ids.append((session.id, execution.id, case))

        for session_id, execution_id, case in session_ids:
            resp = authenticated_client.get(
                f"/api/v1/sessions/{session_id}/decision-timeline"
            )

            assert resp.status_code == 200
            payload = resp.json()
            failure = [
                event
                for event in payload["events"]
                if event["source"] == "terminal_log_metadata"
            ][0]
            assert failure["title"] == case["title"]
            assert failure["status"] == "failed"
            assert failure["details"]["task_execution_id"] == execution_id
            assert failure["details"]["reason"] == case["reason"]
            assert failure["details"]["repair_attempted"] is case["repair_attempted"]
            assert failure["details"]["targeted_second_repair_attempted"] is False
            assert "operator_next_action" in failure["details"]
            assert payload["counts"]["failure"] == 1


def test_decision_timeline_surfaces_workspace_isolation_terminal(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        completed_at = datetime(2026, 5, 8, 20, 46, tzinfo=UTC)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.FAILED,
            completed_at=completed_at,
        )
        db_session.add(execution)
        db_session.commit()
        db_session.refresh(execution)

        db_session.add(
            LogEntry(
                session_id=session.id,
                task_id=task.id,
                task_execution_id=execution.id,
                level="WARN",
                message=(
                    "[ORCHESTRATION] Restored workspace to the pre-run snapshot "
                    "after workspace isolation violation"
                ),
                created_at=completed_at,
                log_metadata=json.dumps(
                    {
                        "phase": "workspace_restore",
                        "reason": "workspace isolation violation",
                        "snapshot_path": f"{tmpdir}/.openclaw/auto-snapshots/task",
                        "target_path": tmpdir,
                        "task_execution_id": execution.id,
                    }
                ),
            )
        )
        db_session.commit()

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        payload = resp.json()
        failure = [
            event
            for event in payload["events"]
            if event["source"] == "terminal_log_metadata"
        ][0]
        assert failure["title"] == "Workspace Isolation Violation"
        assert failure["status"] == "failed"
        assert failure["details"]["reason"] == "workspace isolation violation"
        assert failure["details"]["task_execution_id"] == execution.id
        assert failure["details"]["snapshot_path"].endswith(
            ".openclaw/auto-snapshots/task"
        )
        assert failure["details"]["target_path"] == tmpdir
        assert failure["details"]["repair_attempted"] is False
        assert failure["details"]["targeted_second_repair_attempted"] is False
        assert "project boundary" in failure["details"]["no_further_repair_reason"]
        assert "operator_next_action" in failure["details"]
        assert payload["counts"]["failure"] == 1


def test_decision_timeline_surfaces_cancelled_execution_without_terminal_log(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.CANCELLED,
            completed_at=datetime(2026, 5, 8, 5, 0, tzinfo=UTC),
        )
        db_session.add(execution)
        db_session.commit()
        db_session.refresh(execution)

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        payload = resp.json()
        failure = [
            event
            for event in payload["events"]
            if event["source"] == "terminal_log_metadata"
        ][0]
        assert failure["title"] == "Execution Cancelled"
        assert failure["status"] == "cancelled"
        assert failure["severity"] == "warning"
        assert failure["details"]["task_execution_id"] == execution.id
        assert failure["details"]["reason"] == "forced-stop or cancellation"
        assert failure["details"]["repair_attempted"] is False
        assert failure["details"]["targeted_second_repair_attempted"] is False
        assert "operator_next_action" in failure["details"]
        assert payload["counts"]["failure"] == 1


def test_decision_timeline_does_not_duplicate_existing_task_failed_event(
    authenticated_client, db_session
):
    with tempfile.TemporaryDirectory() as tmpdir:
        project = _make_project(db_session, workspace_path=tmpdir)
        session = _make_session(db_session, project)
        task = _make_task(db_session, project, session)
        completed_at = datetime(2026, 5, 8, 6, 0, tzinfo=UTC)
        execution = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=1,
            status=TaskStatus.FAILED,
            completed_at=completed_at,
        )
        db_session.add(execution)
        db_session.commit()
        db_session.refresh(execution)
        _write_events(
            tmpdir,
            session.id,
            task.id,
            [
                _event(
                    event_id="task-failed-existing",
                    session_id=session.id,
                    task_id=task.id,
                    event_type="task_failed",
                    timestamp=completed_at,
                    details={"task_execution_id": execution.id},
                )
            ],
        )
        db_session.add(
            LogEntry(
                session_id=session.id,
                task_id=task.id,
                task_execution_id=execution.id,
                level="ERROR",
                message="[ORCHESTRATION] Planning timed out or exceeded context",
                created_at=completed_at,
                log_metadata=json.dumps({"reason": "planning_context_overflow"}),
            )
        )
        db_session.commit()

        resp = authenticated_client.get(
            f"/api/v1/sessions/{session.id}/decision-timeline"
        )

        assert resp.status_code == 200
        payload = resp.json()
        assert [
            event
            for event in payload["events"]
            if event["source"] == "terminal_log_metadata"
        ] == []
        assert [event["id"] for event in payload["events"]] == ["task-failed-existing"]


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
