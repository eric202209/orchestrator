"""Provider-free GEC1 tests: grounding events reach the canonical journal.

ORD1 reported that no grounding event journal was persisted for its live run.
That report was wrong: the journal existed at the canonical control-state root,
``<runtime_root>/control/projects/<id>/events``, and the diagnosis had looked in
the legacy ``<workspace>/.agent`` location instead.

These tests pin the behavior that was already correct, so the same misdiagnosis
cannot recur silently, and pin the one thing that was genuinely weak: a journal
failure used to be swallowed into a debug line.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.services.orchestration.phases import planning_grounding_integration
from app.services.orchestration.phases.planning_grounding_integration import (
    run_typed_grounding_for_planning,
)
from app.services.orchestration.planning.grounding.coordinator_contracts import (
    GroundingAssessmentKind,
    GroundingProposal,
)
from app.services.orchestration.state.persistence import (
    append_orchestration_event,
    read_orchestration_events,
)
from app.services.workspace.control_state_paths import (
    ControlStateLocation,
    control_state_family_dir,
)


TASK = "Present long-held work as dormant rather than current."


def _git_repo(root: Path) -> Path:
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "app" / "states.py").write_text(
        "PAUSED = 'paused'\n\n\ndef pause(item):\n    item.status = PAUSED\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=root, check=True, shell=False)
    return root


class _ScriptedProvider:
    """Two-action run: search_text -> inspect_file -> SUFFICIENT."""

    def __init__(self) -> None:
        self.calls = 0
        self.observation_ids: list[str] = []

    def decide(self, context):
        self.calls += 1
        history = context.state.observation_history
        for item in history:
            if item.observation_id not in self.observation_ids:
                self.observation_ids.append(item.observation_id)
        if self.calls == 1:
            return GroundingProposal(
                action_payload={
                    "action": "search_text",
                    "query": "paused",
                    "scopes": ["app"],
                }
            )
        if self.calls == 2:
            return GroundingProposal(
                assessment_kind=GroundingAssessmentKind.NEED_MORE_EVIDENCE,
                action_payload={"action": "inspect_file", "path": "app/states.py"},
                rationale="read the candidate",
            )
        substantive = [
            item.observation_id
            for item in history
            if item.action_identity != "search_text"
        ]
        return GroundingProposal(
            assessment_kind=GroundingAssessmentKind.SUFFICIENT,
            cited_observation_ids=tuple(substantive),
            rationale="grounded",
        )


def _ctx(workspace: Path, control_root: Path, *, logger=None):
    """A context shaped like production: runtime workspace, separate control root."""

    location = ControlStateLocation(
        legacy_root=workspace, project_id=136
    ).with_control_root(control_root)
    return SimpleNamespace(
        session_id=206,
        task_id=258,
        task_execution_id=347,
        prompt=TASK,
        grounding_decision_provider=_ScriptedProvider(),
        grounding_max_steps=4,
        grounding_max_provider_requests=3,
        grounding_mechanical_skip=False,
        grounding_snapshot_identity=None,
        control_state_location=location,
        timeout_seconds=30,
        orchestration_state=SimpleNamespace(project_dir=str(workspace)),
        logger=logger or logging.getLogger("gec1-test"),
        db=None,
    )


# --------------------------------------------------------------------------
# T1 — the canonical journal receives grounding events end to end
# --------------------------------------------------------------------------


def test_t1_grounding_events_reach_the_canonical_control_state(tmp_path):
    workspace = _git_repo(tmp_path / "runtime-workspace")
    control_root = tmp_path / "control" / "projects" / "136"
    ctx = _ctx(workspace, control_root)

    result = run_typed_grounding_for_planning(ctx)

    journal_dir = control_state_family_dir(ctx.control_state_location, "events")
    assert journal_dir.parent == control_root
    assert journal_dir.exists(), "canonical events directory was not created"

    # The legacy workspace location must NOT be the write target.
    assert not (workspace / ".agent" / "events").exists()

    events = read_orchestration_events(ctx.control_state_location, 206, 258)
    grounding = [e for e in events if e["event_type"].startswith("grounding")]
    assert grounding, "no grounding events were persisted"
    assert result.terminal_state.value == "SUFFICIENT"


# --------------------------------------------------------------------------
# T2 — write and read paths agree (read-back proof)
# --------------------------------------------------------------------------


def test_t2_read_back_matches_what_was_written(tmp_path):
    workspace = _git_repo(tmp_path / "runtime-workspace")
    control_root = tmp_path / "control" / "projects" / "136"
    ctx = _ctx(workspace, control_root)
    run_typed_grounding_for_planning(ctx)

    journal = (
        control_state_family_dir(ctx.control_state_location, "events")
        / "session_206_task_258.jsonl"
    )
    written = [json.loads(line) for line in journal.read_text().splitlines() if line]
    read_back = read_orchestration_events(ctx.control_state_location, 206, 258)

    assert len(read_back) == len(written)
    assert [e["event_id"] for e in read_back] == [e["event_id"] for e in written]


# --------------------------------------------------------------------------
# T3 — the event completeness contract for one two-action run
# --------------------------------------------------------------------------


def test_t3_two_action_run_is_reconstructable(tmp_path):
    workspace = _git_repo(tmp_path / "runtime-workspace")
    ctx = _ctx(workspace, tmp_path / "control" / "projects" / "136")
    run_typed_grounding_for_planning(ctx)

    events = read_orchestration_events(ctx.control_state_location, 206, 258)
    kinds = [e["event_type"] for e in events]

    assert kinds.count("grounding_started") == 1
    assert kinds.count("grounding_terminal") == 1
    assert kinds.count("grounding_observation") == 2
    # One request event per provider turn, including the terminal assessment.
    assert kinds.count("grounding_request") >= 3
    assert kinds.count("grounding_assessment") >= 1
    # grounding_provider_turn is emitted by the provider adapter, not the
    # coordinator, so an injected provider produces none here by design.
    assert kinds.count("grounding_provider_turn") == 0

    observations = [e for e in events if e["event_type"] == "grounding_observation"]
    actions = [o["details"]["action"] for o in observations]
    assert actions == ["search_text", "inspect_file"]


# --------------------------------------------------------------------------
# T4 — the fields needed to answer the ORD1 questions are present
# --------------------------------------------------------------------------


def test_t4_required_diagnostic_fields_are_captured(tmp_path):
    workspace = _git_repo(tmp_path / "runtime-workspace")
    ctx = _ctx(workspace, tmp_path / "control" / "projects" / "136")
    run_typed_grounding_for_planning(ctx)

    events = read_orchestration_events(ctx.control_state_location, 206, 258)
    by_kind: dict[str, list] = {}
    for event in events:
        by_kind.setdefault(event["event_type"], []).append(event["details"])

    search = next(
        d for d in by_kind["grounding_observation"] if d["action"] == "search_text"
    )
    assert search["normalized_request"]["query"] == "paused"
    assert search["normalized_request"]["scopes"] == ["app"]
    assert search["outcome"] in {"FOUND", "NOT_FOUND"}
    assert "evidence_bytes" in search
    assert search["budget"]["distinct_files"] == 0
    assert search["budget"]["positive_regions"] == 0

    inspect = next(
        d for d in by_kind["grounding_observation"] if d["action"] == "inspect_file"
    )
    assert inspect["normalized_request"]["path"] == "app/states.py"

    request = by_kind["grounding_request"][0]
    assert request["grounding_run_id"] == "planning-grounding-206-258-347"
    assert "budget" in request

    terminal = by_kind["grounding_terminal"][0]
    assert terminal["terminal_state"] == "SUFFICIENT"
    assert terminal["cited_observation_ids"]


# --------------------------------------------------------------------------
# T5 — run identity is carried on every grounding event
# --------------------------------------------------------------------------


def test_t5_run_and_execution_identity_are_preserved(tmp_path):
    workspace = _git_repo(tmp_path / "runtime-workspace")
    ctx = _ctx(workspace, tmp_path / "control" / "projects" / "136")
    run_typed_grounding_for_planning(ctx)

    events = read_orchestration_events(ctx.control_state_location, 206, 258)
    grounding = [e for e in events if e["event_type"].startswith("grounding")]

    assert grounding
    for event in grounding:
        assert event["session_id"] == 206
        assert event["task_id"] == 258
        assert event["details"]["grounding_run_id"] == "planning-grounding-206-258-347"


# --------------------------------------------------------------------------
# T6 — repeated runs append and stay segmentable, they never overwrite
# --------------------------------------------------------------------------


def test_t6_retry_runs_append_and_remain_segmentable(tmp_path):
    workspace = _git_repo(tmp_path / "runtime-workspace")
    control_root = tmp_path / "control" / "projects" / "136"

    for _ in range(3):
        run_typed_grounding_for_planning(_ctx(workspace, control_root))

    location = ControlStateLocation(
        legacy_root=workspace, project_id=136
    ).with_control_root(control_root)
    events = read_orchestration_events(location, 206, 258)
    kinds = [e["event_type"] for e in events]

    # Nothing was overwritten: three complete runs are present in order.
    assert kinds.count("grounding_started") == 3
    assert kinds.count("grounding_terminal") == 3
    assert kinds.count("grounding_observation") == 6

    # Each run is segmentable by its start/terminal boundary even though the
    # retry chain reuses one grounding_run_id.
    segments, current = [], None
    for event in events:
        if event["event_type"] == "grounding_started":
            current = []
        elif event["event_type"] == "grounding_terminal" and current is not None:
            segments.append(current)
            current = None
        elif current is not None and event["event_type"].startswith("grounding"):
            current.append(event)
    assert len(segments) == 3
    assert all(segment for segment in segments)


# --------------------------------------------------------------------------
# T7 — a journal failure is loud, bounded, and never fails the run
# --------------------------------------------------------------------------


def test_t7_persistence_failure_is_observable_and_non_fatal(tmp_path, monkeypatch):
    workspace = _git_repo(tmp_path / "runtime-workspace")
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("gec1-failure-test")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(_Capture())

    def _raising(**_kwargs):
        raise OSError("journal write refused (token=super-secret-value)")

    ctx = _ctx(workspace, tmp_path / "control" / "projects" / "136", logger=logger)

    result = run_typed_grounding_for_planning(ctx, append_event=_raising)

    # The run still completes: observability never acquires grounding authority.
    assert result.terminal_state.value == "SUFFICIENT"

    errors = [r for r in records if r.levelno >= logging.ERROR]
    assert errors, "a journal failure must not be silent"
    rendered = errors[0].getMessage()
    assert "Grounding event persistence failed" in rendered
    assert "exception_type=OSError" in rendered
    assert "event_type=" in rendered
    # The resolved write location is named, which is what ORD1 needed:
    # project identity and the control root, not just a workspace path.
    assert "control_state=" in rendered
    assert "project_id=136" in rendered
    assert "control_root=" in rendered
    # Bounded and redacted: a credential-shaped value is not echoed.
    assert "super-secret-value" not in rendered
    assert "<redacted>" in rendered


# --------------------------------------------------------------------------
# T8 — no grounding semantics moved
# --------------------------------------------------------------------------


def test_t8_event_capture_does_not_change_grounding_outcome(tmp_path):
    workspace = _git_repo(tmp_path / "runtime-workspace")

    with_journal = run_typed_grounding_for_planning(
        _ctx(workspace, tmp_path / "control" / "a")
    )

    def _raising(**_kwargs):
        raise OSError("journal unavailable")

    without_journal = run_typed_grounding_for_planning(
        _ctx(workspace, tmp_path / "control" / "b"), append_event=_raising
    )

    assert with_journal.terminal_state == without_journal.terminal_state
    assert len(with_journal.observations) == len(without_journal.observations)
    assert [o.action_identity for o in with_journal.observations] == [
        o.action_identity for o in without_journal.observations
    ]
