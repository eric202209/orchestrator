from __future__ import annotations

import json

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.reporting.replay import (
    REDUCER_VERSION,
    reconstruct_execution_state,
)


def test_replay_reconstructs_state_with_reducer_version_and_field_classes(tmp_path):
    project_dir = tmp_path / "replay-project"
    project_dir.mkdir()
    session_id = 17
    task_id = 23

    append_orchestration_event(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.TASK_STARTED,
        details={},
    )
    append_orchestration_event(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.STEP_STARTED,
        details={"step_index": 0},
    )
    tool_failed = append_orchestration_event(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.TOOL_FAILED,
        details={"tool_name": "pytest"},
    )
    retry = append_orchestration_event(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.RETRY_ENTERED,
        details={"attempt": 1},
    )
    append_orchestration_event(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.VALIDATION_RESULT,
        details={"stage": "task_completion", "status": "rejected"},
    )

    report = reconstruct_execution_state(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        boundary={"mode": "to_event_id", "requested": retry["event_id"]},
    )

    assert report["reducer_version"] == REDUCER_VERSION
    assert report["boundary"]["resolved_event_id"] == retry["event_id"]
    assert report["state"]["phase"] == "execution"
    assert report["state"]["status"] == "retrying"
    assert report["state"]["retry_count"] == 1
    assert report["state"]["latest_failure_event_id"] == tool_failed["event_id"]
    assert "retry_count" in report["field_classification"]["authoritative"]
    assert "workspace_hashes" in report["field_classification"]["artifacts"]
    assert report["integrity"]["confidence"] == "high"


def test_replay_reports_malformed_unknown_duplicate_and_order_findings(tmp_path):
    project_dir = tmp_path / "replay-integrity"
    event_dir = project_dir / ".agent" / "events"
    event_dir.mkdir(parents=True)
    path = event_dir / "session_1_task_2.jsonl"
    duplicate_id = "duplicate-event"
    lines = [
        {
            "event_id": duplicate_id,
            "timestamp": "2026-05-05T00:00:03+00:00",
            "event_type": EventType.TASK_STARTED,
            "session_id": 1,
            "task_id": 2,
            "parent_event_id": None,
            "details": {},
        },
        "{bad json",
        {
            "event_id": duplicate_id,
            "timestamp": "2026-05-05T00:00:01+00:00",
            "event_type": "future_event_type",
            "session_id": 1,
            "task_id": 2,
            "parent_event_id": "missing-parent",
            "details": {},
        },
    ]
    path.write_text(
        "\n".join(item if isinstance(item, str) else json.dumps(item) for item in lines)
        + "\n",
        encoding="utf-8",
    )

    report = reconstruct_execution_state(
        project_dir=project_dir,
        session_id=1,
        task_id=2,
    )

    finding_types = {finding["type"] for finding in report["integrity"]["findings"]}
    assert report["integrity"]["malformed_line_count"] == 1
    assert report["integrity"]["unknown_event_types"] == ["future_event_type"]
    assert "duplicate_event_id" in finding_types
    assert "missing_parent_event" in finding_types
    assert "event_order_anomaly" in finding_types
    assert report["integrity"]["confidence"] == "low"


def test_replay_compares_checkpoint_only_for_checkpoint_boundary(tmp_path):
    project_dir = tmp_path / "replay-checkpoint"
    checkpoint_dir = tmp_path / "checkpoints"
    project_dir.mkdir()
    checkpoint_dir.mkdir()
    session_id = 3
    task_id = 4

    append_orchestration_event(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.TASK_STARTED,
        details={},
    )
    checkpoint_event = append_orchestration_event(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.CHECKPOINT_SAVED,
        details={
            "checkpoint_name": "autosave_latest",
            "current_step_index": 2,
            "status": "running",
        },
    )
    (checkpoint_dir / "session_3_autosave_latest.json").write_text(
        json.dumps(
            {
                "checkpoint_name": "autosave_latest",
                "current_step_index": 2,
                "orchestration_state": {"status": "running"},
            }
        ),
        encoding="utf-8",
    )

    report = reconstruct_execution_state(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        boundary={"mode": "to_checkpoint_name", "requested": "autosave_latest"},
        checkpoint_dir=checkpoint_dir,
    )

    assert report["boundary"]["resolved_event_id"] == checkpoint_event["event_id"]
    assert report["checkpoint_comparison"]["status"] == "match"
    assert report["state"]["latest_checkpoint_name"] == "autosave_latest"
    assert report["state"]["current_step_index"] == 2


def test_replay_reducer_does_not_observe_workspace_unless_requested(
    tmp_path, monkeypatch
):
    project_dir = tmp_path / "replay-purity"
    project_dir.mkdir()
    append_orchestration_event(
        project_dir=project_dir,
        session_id=5,
        task_id=6,
        event_type=EventType.TASK_STARTED,
        details={},
    )

    def fail_workspace_hash(_project_dir):
        raise AssertionError("workspace comparison should not run")

    monkeypatch.setattr(
        "app.services.orchestration.reporting.replay._compute_workspace_hash",
        fail_workspace_hash,
    )

    report = reconstruct_execution_state(
        project_dir=project_dir,
        session_id=5,
        task_id=6,
    )

    assert report["workspace_evidence"]["status"] == "not_requested"
    assert report["state"]["workspace_evidence_status"] == "not_requested"
