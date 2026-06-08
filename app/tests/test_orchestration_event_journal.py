from __future__ import annotations

import json
import os

import pytest

from app.models import Project, Session as SessionModel, Task, TaskStatus
from app.services.orchestration.state.persistence import (
    _write_json_payload_atomic,
    append_orchestration_event,
    diff_orchestration_state_snapshots,
    emit_intent_outcome_mismatch,
    maybe_emit_divergence_detected,
    record_validation_verdict,
    read_orchestration_state_snapshots,
    save_orchestration_checkpoint,
)
from app.services.orchestration.types import ValidationVerdict
from app.services.prompt_templates import OrchestrationState
from app.services.tool_tracking_service import ToolTrackingService


def test_append_orchestration_event_writes_append_only_jsonl(tmp_path):
    project_dir = tmp_path / "journal-project"
    project_dir.mkdir()

    append_orchestration_event(
        project_dir=project_dir,
        session_id=7,
        task_id=13,
        event_type="phase_started",
        details={"phase": "planning"},
    )
    append_orchestration_event(
        project_dir=project_dir,
        session_id=7,
        task_id=13,
        event_type="phase_finished",
        details={"phase": "planning", "status": "accepted"},
    )

    log_path = project_dir / ".agent" / "events" / "session_7_task_13.jsonl"
    lines = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [line["event_type"] for line in lines] == [
        "phase_started",
        "health_score_updated",
        "phase_finished",
    ]
    assert lines[0]["details"]["phase"] == "planning"
    assert lines[1]["details"]["score"] == 100


@pytest.mark.skipif(not hasattr(os, "chown"), reason="os.chown is POSIX-only")
def test_atomic_json_write_normalizes_ownership_to_parent(monkeypatch, tmp_path):
    parent = tmp_path / "owned-parent"
    parent.mkdir()
    target = parent / "payload.json"
    expected_uid = parent.stat().st_uid
    expected_gid = parent.stat().st_gid
    chown_calls = []

    def fake_chown(path, uid, gid):
        chown_calls.append((str(path), uid, gid))

    monkeypatch.setattr(
        "app.services.orchestration.state.persistence.os.chown", fake_chown
    )

    _write_json_payload_atomic(target, {"ok": True})

    assert target.read_text(encoding="utf-8") == '{"ok": true}'
    assert chown_calls
    assert all(
        uid == expected_uid and gid == expected_gid for _, uid, gid in chown_calls
    )
    assert any(path == str(target) for path, _, _ in chown_calls)


@pytest.mark.skipif(not hasattr(os, "chown"), reason="os.chown is POSIX-only")
def test_event_journal_writes_normalize_log_and_lock_ownership(monkeypatch, tmp_path):
    project_dir = tmp_path / "journal-project"
    project_dir.mkdir()
    chown_calls = []

    def fake_chown(path, uid, gid):
        chown_calls.append((str(path), uid, gid))

    monkeypatch.setattr(
        "app.services.orchestration.state.persistence.os.chown", fake_chown
    )

    append_orchestration_event(
        project_dir=project_dir,
        session_id=7,
        task_id=13,
        event_type="phase_started",
        details={"phase": "planning"},
    )

    event_dir = project_dir / ".agent" / "events"
    expected_uid = event_dir.stat().st_uid
    expected_gid = event_dir.stat().st_gid
    log_path = event_dir / "session_7_task_13.jsonl"
    lock_path = event_dir / "session_7_task_13.jsonl.lock"

    assert log_path.exists()
    assert lock_path.exists()
    assert any(path == str(log_path) for path, _, _ in chown_calls)
    assert any(path == str(lock_path) for path, _, _ in chown_calls)
    assert all(
        uid == expected_uid and gid == expected_gid for _, uid, gid in chown_calls
    )


def test_validation_verdict_also_persists_event(db_session, tmp_path):
    project_dir = tmp_path / "validation-journal-project"
    project_dir.mkdir()
    state = OrchestrationState(
        session_id="9",
        task_description="Validate plan",
        project_name="Validation Journal",
        task_id=5,
    )
    state._project_dir_override = str(project_dir)

    verdict = ValidationVerdict(
        stage="plan",
        status="warning",
        profile="implementation",
        reasons=["Naming mismatch"],
    )

    record_validation_verdict(
        db_session,
        session_id=9,
        task_id=5,
        orchestration_state=state,
        verdict=verdict,
    )

    log_path = project_dir / ".agent" / "events" / "session_9_task_5.jsonl"
    lines = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]

    validation_event = next(
        line for line in lines if line["event_type"] == "validation_result"
    )
    assert validation_event["details"]["stage"] == "plan"
    assert validation_event["details"]["status"] == "warning"

    snapshots = read_orchestration_state_snapshots(project_dir, 9, 5)
    assert snapshots[-1]["trigger"] == "validation_plan"
    assert snapshots[-1]["validation_verdicts"][-1]["status"] == "warning"


def test_checkpoint_save_also_persists_checkpoint_saved_event(db_session, tmp_path):
    project_dir = tmp_path / "checkpoint-journal-project"
    project_dir.mkdir()
    state = OrchestrationState(
        session_id="11",
        task_description="Checkpoint event",
        project_name="Checkpoint Journal",
        task_id=8,
    )
    state._project_dir_override = str(project_dir)

    save_orchestration_checkpoint(
        db_session,
        session_id=11,
        task_id=8,
        prompt="Checkpoint event prompt",
        orchestration_state=state,
        checkpoint_name="autosave_latest",
    )

    log_path = project_dir / ".agent" / "events" / "session_11_task_8.jsonl"
    lines = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]

    checkpoint_event = next(
        line for line in lines if line["event_type"] == "checkpoint_saved"
    )
    assert checkpoint_event["details"]["checkpoint_name"] == "autosave_latest"

    snapshots = read_orchestration_state_snapshots(project_dir, 11, 8)
    assert snapshots[-1]["checkpoint_name"] == "autosave_latest"
    assert snapshots[-1]["trigger"] == "checkpoint_saved"


def test_tool_tracking_also_persists_tool_events(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.workspace.project_isolation_service.get_effective_workspace_root",
        lambda db=None: tmp_path,
    )

    project = Project(name="Tool Events", workspace_path="tool-events")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Track tool events",
        status=TaskStatus.RUNNING,
        task_subfolder="task-21",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    session = SessionModel(
        project_id=project.id,
        name="Tool Session",
        description="track tools",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    tool_service = ToolTrackingService(db_session)
    tool_service.track(
        execution_id="exec-1",
        tool_name="rg",
        params={"pattern": "TODO"},
        result={"matches": 3},
        success=False,
        session_id=session.id,
        task_id=task.id,
        error_message="rg failed",
    )

    log_path = (
        tmp_path
        / "tool-events"
        / "task-21"
        / ".agent"
        / "events"
        / f"session_{session.id}_task_{task.id}.jsonl"
    )
    lines = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]

    tool_failed_event = next(
        line for line in lines if line["event_type"] == "tool_failed"
    )
    assert lines[0]["event_type"] == "tool_invoked"
    assert tool_failed_event["details"]["tool_name"] == "rg"

    health_event = lines[-1]
    assert health_event["event_type"] == "health_score_updated"
    assert health_event["details"]["score"] < 100


def test_snapshot_diff_reports_step_and_file_delta(db_session, tmp_path):
    project_dir = tmp_path / "diff-project"
    project_dir.mkdir()
    state = OrchestrationState(
        session_id="12",
        task_description="Diff event",
        project_name="Diff Journal",
        task_id=4,
    )
    state._project_dir_override = str(project_dir)
    state.plan = [{"description": "one"}, {"description": "two"}]

    save_orchestration_checkpoint(
        db_session,
        session_id=12,
        task_id=4,
        prompt="before",
        orchestration_state=state,
        checkpoint_name="before",
    )

    state.current_step_index = 1
    state.changed_files = ["src/app.py"]
    save_orchestration_checkpoint(
        db_session,
        session_id=12,
        task_id=4,
        prompt="after",
        orchestration_state=state,
        checkpoint_name="after",
    )

    diff = diff_orchestration_state_snapshots(
        project_dir,
        12,
        4,
        from_checkpoint=0,
        to_checkpoint=1,
    )

    assert diff["delta"]["current_step_index"]["change"] == 1
    assert diff["delta"]["files_touched"]["added"] == ["src/app.py"]


def test_divergence_event_emitted_after_retry_cluster(tmp_path):
    project_dir = tmp_path / "divergence-project"
    project_dir.mkdir()

    step_started = append_orchestration_event(
        project_dir=project_dir,
        session_id=21,
        task_id=2,
        event_type="step_started",
        details={"step_index": 1},
    )
    append_orchestration_event(
        project_dir=project_dir,
        session_id=21,
        task_id=2,
        event_type="retry_entered",
        parent_event_id=step_started["event_id"],
        details={"step_index": 1, "attempt": 1},
    )
    retry_two = append_orchestration_event(
        project_dir=project_dir,
        session_id=21,
        task_id=2,
        event_type="retry_entered",
        parent_event_id=step_started["event_id"],
        details={"step_index": 1, "attempt": 2},
    )

    divergence = maybe_emit_divergence_detected(
        project_dir=project_dir,
        session_id=21,
        task_id=2,
        parent_event_id=retry_two["event_id"],
    )

    assert divergence is not None
    assert divergence["event_type"] == "divergence_detected"
    assert divergence["details"]["reason"] in {"retry_cluster", "health_drop"}


def test_intent_outcome_mismatch_event_emitted_for_file_gap(tmp_path):
    project_dir = tmp_path / "intent-gap-project"
    project_dir.mkdir()

    mismatch = emit_intent_outcome_mismatch(
        project_dir=project_dir,
        session_id=22,
        task_id=3,
        step_index=1,
        step_description="Create src/app.ts and tests/app.test.ts",
        expected_files=["src/app.ts", "tests/app.test.ts"],
        actual_files=["README.md"],
        actual_tool_calls=["rg", "sed"],
    )

    assert mismatch is not None
    assert mismatch["event_type"] == "intent_outcome_mismatch"
    assert mismatch["details"]["mismatch_score"] >= 40
