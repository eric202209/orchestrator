from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.reporting.replay import (
    COMPATIBILITY_VERSION,
    REDUCER_VERSION,
)

SESSION_ID = 900
TASK_ID = 901
BASE_TS = "2026-05-05T12:00:"


@dataclass(frozen=True)
class ReplayFixture:
    fixture_id: str
    description: str
    events: List[Dict[str, Any] | str]
    expected_state: Dict[str, Any]
    expected_confidence: str = "high"
    expected_findings: set[str] = field(default_factory=set)
    expected_unknown_event_types: List[str] = field(default_factory=list)
    boundary: Optional[Dict[str, Any]] = None
    checkpoint_payloads: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    checkpoint_status: Optional[str] = None
    compare_workspace: bool = False
    expected_workspace_status: str = "not_requested"
    expected_reducer_version: str = REDUCER_VERSION
    expected_compatibility_version: str = COMPATIBILITY_VERSION


def event(
    event_id: str,
    second: int,
    event_type: str,
    details: Optional[Dict[str, Any]] = None,
    *,
    session_id: int = SESSION_ID,
    task_id: int = TASK_ID,
    parent_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "timestamp": f"{BASE_TS}{second:02d}+00:00",
        "event_type": event_type,
        "session_id": session_id,
        "task_id": task_id,
        "parent_event_id": parent_event_id,
        "details": details or {},
    }


REPLAY_FIXTURES: tuple[ReplayFixture, ...] = (
    ReplayFixture(
        fixture_id="successful_execution_trace",
        description="Task starts, completes one step, validates, and completes.",
        events=[
            event("task-started", 1, EventType.TASK_STARTED),
            event("step-started", 2, EventType.STEP_STARTED, {"step_index": 0}),
            event(
                "step-finished",
                3,
                EventType.STEP_FINISHED,
                {"step_index": 0, "status": "success"},
            ),
            event(
                "validation-accepted",
                4,
                EventType.VALIDATION_RESULT,
                {"stage": "task_completion", "status": "accepted"},
            ),
            event("task-completed", 5, EventType.TASK_COMPLETED),
        ],
        expected_state={
            "phase": "completion",
            "status": "completed",
            "current_step_index": 1,
            "retry_count": 0,
            "repair_count": 0,
            "latest_failure_event_id": None,
            "validation_verdict_status_history": ["accepted"],
        },
    ),
    ReplayFixture(
        fixture_id="validation_rejection_trace",
        description="Validation rejects task completion and becomes latest failure.",
        events=[
            event("task-started", 1, EventType.TASK_STARTED),
            event(
                "validation-rejected",
                2,
                EventType.VALIDATION_RESULT,
                {"stage": "task_completion", "status": "rejected"},
            ),
        ],
        expected_state={
            "phase": "validation",
            "status": "rejected",
            "latest_failure_event_id": "validation-rejected",
            "validation_verdict_status_history": ["rejected"],
        },
    ),
    ReplayFixture(
        fixture_id="repair_chain_trace",
        description="Completion repair is generated, applied, then rejected.",
        events=[
            event("repair-generated", 1, EventType.REPAIR_GENERATED),
            event("repair-applied", 2, EventType.REPAIR_APPLIED),
            event("repair-rejected", 3, EventType.REPAIR_REJECTED),
        ],
        expected_state={
            "phase": "completion",
            "status": EventType.REPAIR_REJECTED,
            "repair_count": 3,
            "latest_failure_event_id": "repair-rejected",
        },
    ),
    ReplayFixture(
        fixture_id="timeout_failure_trace",
        description="A timeout is represented as a terminal task failure.",
        events=[
            event("task-started", 1, EventType.TASK_STARTED),
            event(
                "task-timeout",
                2,
                EventType.TASK_FAILED,
                {"error_type": "timeout", "phase": "execution"},
            ),
        ],
        expected_state={
            "phase": "failure",
            "status": "failed",
            "latest_failure_event_id": "task-timeout",
        },
    ),
    ReplayFixture(
        fixture_id="intervention_flow_trace",
        description="Human intervention is requested and then approved.",
        events=[
            event("waiting", 1, EventType.WAITING_FOR_INPUT),
            event("intervention-requested", 2, EventType.HUMAN_INTERVENTION_REQUESTED),
            event(
                "intervention-replied",
                3,
                EventType.HUMAN_INTERVENTION_REPLIED,
                {"decision": "approved"},
            ),
        ],
        expected_state={
            "phase": "failure",
            "status": "awaiting_input",
            "intervention_status": "approved",
            "latest_failure_event_id": "intervention-requested",
        },
    ),
    ReplayFixture(
        fixture_id="checkpoint_redirect_trace",
        description="Resume redirects from requested checkpoint to richer checkpoint.",
        events=[
            event("task-started", 1, EventType.TASK_STARTED),
            event(
                "checkpoint-redirected",
                2,
                EventType.CHECKPOINT_REDIRECTED,
                {
                    "requested_checkpoint_name": "paused_old",
                    "resolved_checkpoint_name": "autosave_latest",
                    "current_step_index": 2,
                    "status": "running",
                },
            ),
        ],
        boundary={"mode": "to_checkpoint_name", "requested": "autosave_latest"},
        checkpoint_payloads={
            "autosave_latest": {
                "checkpoint_name": "autosave_latest",
                "current_step_index": 2,
                "orchestration_state": {"status": "running"},
            }
        },
        checkpoint_status="match",
        expected_state={
            "status": "running",
            "current_step_index": 2,
            "latest_checkpoint_name": "autosave_latest",
            "latest_checkpoint_event_id": "checkpoint-redirected",
        },
    ),
    ReplayFixture(
        fixture_id="workspace_drift_evidence_trace",
        description="Workspace drift evidence is diagnostic, not control state.",
        events=[
            event("task-started", 1, EventType.TASK_STARTED),
            event(
                "workspace-drift",
                2,
                EventType.RESUME_WORKSPACE_DRIFT,
                {"workspace_hash": "known-historical-hash"},
            ),
        ],
        compare_workspace=True,
        expected_workspace_status="hash_differs_from_snapshot",
        expected_state={
            "latest_failure_event_id": "workspace-drift",
            "workspace_hashes": ["known-historical-hash"],
        },
    ),
    ReplayFixture(
        fixture_id="malformed_jsonl_trace",
        description="Malformed JSONL is reported while valid events replay.",
        events=[
            event("task-started", 1, EventType.TASK_STARTED),
            "{not json",
            event("task-completed", 2, EventType.TASK_COMPLETED),
        ],
        expected_confidence="medium",
        expected_findings={"malformed_jsonl"},
        expected_state={"phase": "completion", "status": "completed"},
    ),
    ReplayFixture(
        fixture_id="unknown_event_type_trace",
        description="Unknown event types are ignored with compatibility findings.",
        events=[
            event("task-started", 1, EventType.TASK_STARTED),
            event("future-event", 2, "future_event_type", {"future": True}),
            event("task-completed", 3, EventType.TASK_COMPLETED),
        ],
        expected_confidence="medium",
        expected_findings={"event_type_ignored_by_reducer"},
        expected_unknown_event_types=["future_event_type"],
        expected_state={"phase": "completion", "status": "completed"},
    ),
)


def materialize_replay_fixture(
    tmp_path: Path, fixture: ReplayFixture
) -> tuple[Path, Optional[Path]]:
    project_dir = tmp_path / fixture.fixture_id
    event_dir = project_dir / ".agent" / "events"
    event_dir.mkdir(parents=True)
    event_path = event_dir / f"session_{SESSION_ID}_task_{TASK_ID}.jsonl"
    event_path.write_text(
        "\n".join(
            item if isinstance(item, str) else json.dumps(item)
            for item in fixture.events
        )
        + "\n",
        encoding="utf-8",
    )
    if fixture.compare_workspace:
        (project_dir / "current.txt").write_text("current workspace", encoding="utf-8")

    checkpoint_dir = None
    if fixture.checkpoint_payloads:
        checkpoint_dir = tmp_path / f"{fixture.fixture_id}_checkpoints"
        checkpoint_dir.mkdir(parents=True)
        for checkpoint_name, payload in fixture.checkpoint_payloads.items():
            checkpoint_path = (
                checkpoint_dir / f"session_{SESSION_ID}_{checkpoint_name}.json"
            )
            checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    return project_dir, checkpoint_dir


def assert_replay_fixture_expectations(
    report: Dict[str, Any], fixture: ReplayFixture
) -> None:
    assert report["reducer_version"] == fixture.expected_reducer_version
    assert report["compatibility_version"] == fixture.expected_compatibility_version

    for key, expected in fixture.expected_state.items():
        assert report["state"].get(key) == expected, fixture.fixture_id

    assert report["integrity"]["confidence"] == fixture.expected_confidence
    assert (
        report["integrity"]["unknown_event_types"]
        == fixture.expected_unknown_event_types
    )
    finding_types = {item["type"] for item in report["integrity"]["findings"]}
    assert fixture.expected_findings <= finding_types

    assert report["workspace_evidence"]["status"] == fixture.expected_workspace_status
    if fixture.checkpoint_status:
        assert report["checkpoint_comparison"]["status"] == fixture.checkpoint_status
