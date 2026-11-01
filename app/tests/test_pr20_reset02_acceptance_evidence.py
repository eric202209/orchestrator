"""PR20-RESET-02: bounded, attempt-bound acceptance evidence projection.

The TaskCheckpoint stays the authoritative verdict record; the validation
event carries a bounded, versioned projection of the acceptance result that
names its TaskExecution and the checkpoint it was projected from.  All tests
are offline: temporary workspaces, in-memory SQLite, monkeypatched runners.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from app.models import Project, Session as SessionModel, TaskCheckpoint
from app.services.orchestration.events.observability import build_trace_export
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.state import persistence
from app.services.orchestration.state.persistence import (
    read_orchestration_events,
    record_validation_verdict,
)
from app.services.orchestration.types import ValidationVerdict
from app.services.orchestration.validation import candidate_checks
from app.tests.test_pr20_a1_task_acceptance_verification import (
    _apply_candidate_c,
    _apply_incomplete_candidate,
    _complete,
    _task31_project,
)

SCHEMA = "task_acceptance_evidence/v1"
LEGACY_EVENT_KEYS = {
    "stage",
    "status",
    "profile",
    "step_number",
    "reasons",
    "confidence",
}
ACCEPTANCE_PROMPT = "Fix it.\n\nAcceptance criteria:\n- `python -m pytest -q` passes.\n"


def _state(project_dir: Path, *, session_id: int, task_id: int) -> OrchestrationState:
    state = OrchestrationState(
        session_id=str(session_id),
        task_description="acceptance evidence",
        project_name="reset02",
        task_id=task_id,
    )
    state._project_dir_override = str(project_dir)
    return state


def _acceptance_verdict(*, status="accepted", results=None) -> ValidationVerdict:
    return ValidationVerdict(
        stage="task_completion",
        status=status,
        profile="mutation",
        reasons=[] if status == "accepted" else ["Task acceptance pytest failed"],
        details={
            "task_acceptance_verification": {
                "section_found": True,
                "extracted": ["python -m pytest -q"],
                "admitted": ["python -m pytest -q"],
                "refused": [],
                "ignored": [],
                "results": (
                    results
                    if results is not None
                    else [
                        {
                            "command": "python -m pytest -q",
                            "executed_command": "/venv/bin/python -m pytest -q",
                            "executed": True,
                            "returncode": 0,
                            "outcome": "passed",
                        }
                    ]
                ),
            }
        },
    )


def _record(db, project_dir, verdict, *, session_id=9, task_id=5, **kwargs):
    record_validation_verdict(
        db,
        session_id=session_id,
        task_id=task_id,
        orchestration_state=_state(project_dir, session_id=session_id, task_id=task_id),
        verdict=verdict,
        **kwargs,
    )


def _validation_events(project_dir, session_id=9, task_id=5):
    return [
        event
        for event in read_orchestration_events(project_dir, session_id, task_id)
        if event["event_type"] == "validation_result"
    ]


def _checkpoints(db):
    return db.query(TaskCheckpoint).order_by(TaskCheckpoint.id).all()


# --- A/B/C: real admitted pytest commands through validator and persistence ---


def test_positive_acceptance_evidence_is_durable_and_attempt_bound(
    tmp_path, db_session
):
    root = _task31_project(tmp_path)
    _apply_candidate_c(root)
    verdict = _complete(root, flag=True)
    result = verdict.details["task_acceptance_verification"]["results"][0]
    assert result["outcome"] == "passed"
    assert result["output"]["chars"] > 0
    assert result["output"]["retained_chars"] == 0

    _record(db_session, tmp_path, verdict, task_execution_id=101)
    db_session.commit()

    stored = json.loads(_checkpoints(db_session)[0].state_snapshot)
    evidence = stored["details"]["task_acceptance_evidence"]
    assert evidence["schema"] == SCHEMA
    assert (evidence["session_id"], evidence["task_id"]) == (9, 5)
    assert evidence["task_execution_id"] == 101
    projected = evidence["results"][0]
    assert projected["command"] == "python -m pytest -q"
    assert (projected["admitted"], projected["executed"]) == (True, True)
    assert (projected["outcome"], projected["returncode"]) == ("passed", 0)
    assert projected["output"]["sha256"] == result["output"]["sha256"]

    event = _validation_events(tmp_path)[-1]["details"]["task_acceptance_evidence"]
    assert event["task_execution_id"] == 101
    assert event["results"] == evidence["results"]


def test_negative_acceptance_evidence_retains_bounded_failure(tmp_path, db_session):
    root = _task31_project(tmp_path)
    _apply_incomplete_candidate(root)
    verdict = _complete(root, flag=True)
    assert verdict.status == "rejected"

    _record(db_session, tmp_path, verdict, task_execution_id=102)
    db_session.commit()

    stored = json.loads(_checkpoints(db_session)[0].state_snapshot)
    assert stored["status"] == "rejected"
    finding = next(
        f for f in stored["findings"] if f["rule_id"] == "task_acceptance_pytest_failed"
    )
    assert "test_distinct_ids_are_all_kept" in finding["evidence"]["output"]
    projected = stored["details"]["task_acceptance_evidence"]["results"][0]
    assert projected["outcome"] == "failed"
    assert projected["returncode"] not in (0, None)
    assert projected["output"]["retained_chars"] == len(finding["evidence"]["output"])

    details = _validation_events(tmp_path)[-1]["details"]
    assert details["status"] == "rejected"
    event = details["task_acceptance_evidence"]
    assert event["task_execution_id"] == 102
    assert event["results"][0]["outcome"] == "failed"
    # The public event carries metadata only, never the raw test output.
    assert "test_distinct_ids_are_all_kept" not in json.dumps(details)


# --- D: existing authenticated API --------------------------------------------


def test_session_task_events_api_exposes_evidence_without_cross_task_leak(
    tmp_path, db_session, authenticated_client
):
    project = Project(name="Reset02", workspace_path=str(tmp_path))
    db_session.add(project)
    db_session.commit()
    session = SessionModel(
        project_id=project.id,
        name="reset02",
        description="t",
        status="stopped",
        is_active=False,
        execution_mode="manual",
    )
    db_session.add(session)
    db_session.commit()

    _record(
        db_session,
        tmp_path,
        _acceptance_verdict(),
        session_id=session.id,
        task_id=5,
        task_execution_id=301,
    )
    _record(
        db_session,
        tmp_path,
        _acceptance_verdict(
            status="rejected",
            results=[
                {
                    "command": "python -m pytest -q",
                    "executed": True,
                    "returncode": 1,
                    "outcome": "failed",
                }
            ],
        ),
        session_id=session.id,
        task_id=6,
        task_execution_id=302,
    )
    db_session.commit()

    response = authenticated_client.get(
        f"/api/v1/sessions/{session.id}/tasks/5/events",
        params={"event_type": "validation_result"},
    )
    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1
    evidence = events[0]["details"]["task_acceptance_evidence"]
    assert (evidence["task_id"], evidence["task_execution_id"]) == (5, 301)
    assert evidence["results"][0]["outcome"] == "passed"
    assert "302" not in json.dumps(events)


# --- E: existing trace transformation -----------------------------------------


def test_trace_export_projects_acceptance_evidence(tmp_path, db_session):
    _record(db_session, tmp_path, _acceptance_verdict(), task_execution_id=401)
    events = read_orchestration_events(tmp_path, 9, 5)

    trace = build_trace_export(session_id=9, task_id=5, events=events, snapshots=[])

    spans = [s for s in trace["spans"] if s["name"] == "task_acceptance_verification"]
    assert len(spans) == 1
    attributes = spans[0]["attributes"]
    assert attributes["schema"] == SCHEMA
    assert attributes["task_execution_id"] == 401
    assert attributes["outcomes"] == ["passed"]
    assert attributes["returncodes"] == [0]
    assert attributes["checkpoint_projected_before_commit"] is True


# --- F: attempt isolation -----------------------------------------------------


def test_two_attempts_for_same_task_keep_distinct_evidence_identity(
    tmp_path, db_session
):
    verdict = _acceptance_verdict()
    _record(db_session, tmp_path, verdict, task_execution_id=501)
    _record(db_session, tmp_path, verdict, task_execution_id=502)
    db_session.commit()

    stored = [
        json.loads(c.state_snapshot)["details"]["task_acceptance_evidence"]
        for c in _checkpoints(db_session)
    ]
    assert [e["task_execution_id"] for e in stored] == [501, 502]
    events = [
        e["details"]["task_acceptance_evidence"] for e in _validation_events(tmp_path)
    ]
    assert [e["task_execution_id"] for e in events] == [501, 502]
    # The shared verdict object is never stamped with an attempt identity.
    assert "task_acceptance_evidence" not in verdict.details


def test_completion_binds_its_own_task_execution_to_validation_record(
    tmp_path, monkeypatch
):
    from app.services.orchestration.coordinators import completion_coordinator
    from app.services.orchestration.coordinators.completion_coordinator import (
        CompletionCoordinator,
    )
    from app.tests.test_phase14b1_completion_coordinator import (
        _NOOP_FN,
        _make_ctx,
        _patch_coordinator_delegates,
    )

    recorded: list[object] = []
    verdict = _acceptance_verdict(status="rejected")
    _patch_coordinator_delegates(monkeypatch, validation_verdict=verdict)
    monkeypatch.setattr(
        completion_coordinator,
        "record_validation_verdict",
        lambda *args, **kwargs: recorded.append(kwargs.get("task_execution_id")),
    )
    for name in (
        "persist_debug_feedback_envelope",
        "mark_task_attempt_failed",
        "mark_session_paused",
    ):
        monkeypatch.setattr(completion_coordinator, name, _NOOP_FN)
    monkeypatch.setattr(
        completion_coordinator,
        "build_debug_feedback_envelope",
        lambda **_kwargs: type(
            "Envelope",
            (),
            {
                "failure_class": "acceptance",
                "eligible_for_debug_repair": False,
                "stderr_excerpt": "",
                "return_code": None,
            },
        )(),
    )

    for attempt in (77, 78):
        ctx = _make_ctx(tmp_path)
        ctx.task_execution_id = attempt
        result = CompletionCoordinator().complete_task(
            ctx=ctx,
            write_project_state_snapshot_fn=_NOOP_FN,
            save_orchestration_checkpoint_fn=_NOOP_FN,
        )
        assert result["status"] == "failed"

    assert recorded == [77, 78]


# --- G/J: legacy and non-acceptance compatibility ------------------------------


def test_non_acceptance_validation_is_unchanged(tmp_path, db_session):
    verdict = ValidationVerdict(
        stage="plan", status="warning", profile="implementation", reasons=["x"]
    )
    _record(db_session, tmp_path, verdict, task_execution_id=601)
    db_session.commit()

    assert json.loads(_checkpoints(db_session)[0].state_snapshot) == verdict.to_dict()
    details = _validation_events(tmp_path)[-1]["details"]
    assert set(details) == LEGACY_EVENT_KEYS


def test_legacy_events_checkpoints_and_results_remain_readable(tmp_path, db_session):
    legacy_event = {
        "event_id": "legacy",
        "event_type": "validation_result",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "details": {key: None for key in LEGACY_EVENT_KEYS},
    }
    trace = build_trace_export(
        session_id=9, task_id=5, events=[legacy_event], snapshots=[]
    )
    assert trace["span_count"] == 0

    # Pre-RESET-02 result shape (no output metadata) and no attempt context.
    _record(db_session, tmp_path, _acceptance_verdict())
    db_session.commit()
    evidence = json.loads(_checkpoints(db_session)[0].state_snapshot)["details"][
        "task_acceptance_evidence"
    ]
    assert evidence["task_execution_id"] is None
    assert "output" not in evidence["results"][0]


# --- H: output bounds and privacy ----------------------------------------------


def _fake_completed(stdout: str, returncode: int):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


@pytest.mark.parametrize("mode", ["failed", "timeout"])
def test_large_sensitive_output_is_bounded(tmp_path, db_session, monkeypatch, mode):
    secret = "PROVIDER_KEY=sk-reset02-secret"
    noisy = (secret + "\n") + ("x" * 200_000)

    def _fake_run(*_args, **kwargs):
        if mode == "timeout":
            # Real POSIX timeouts carry raw bytes even under text=True.
            raise subprocess.TimeoutExpired(
                cmd="pytest", timeout=kwargs["timeout"], output=noisy.encode()
            )
        return _fake_completed(noisy, 1)

    monkeypatch.setattr(candidate_checks.subprocess, "run", _fake_run)
    findings: list = []
    acceptance = candidate_checks._run_task_acceptance(
        project_dir=tmp_path,
        task_prompt=ACCEPTANCE_PROMPT,
        focused_command="",
        focused_returncode=None,
        timeout_seconds=5,
        findings=findings,
        commands_run=[],
    )

    result = acceptance["results"][0]
    output = result["output"]
    assert output["chars"] == len(noisy)
    assert output["sha256"] == hashlib.sha256(noisy.encode()).hexdigest()
    assert output["truncated"] is True
    assert output["retained_chars"] == 12_000
    excerpt = findings[0].evidence["output"]
    assert excerpt.startswith(noisy[:12_000])
    assert "b'" not in excerpt
    if mode == "timeout":
        assert result["outcome"] == "infrastructure_failure"
        assert result["infrastructure_failure"] == "timeout"
        # The runner's marker follows the excerpt; it is not part of the digest.
        assert excerpt == noisy[:12_000] + "\nTimed out after 5s"
    else:
        assert excerpt == noisy[:12_000]

    verdict = _acceptance_verdict(status="rejected", results=acceptance["results"])
    _record(db_session, tmp_path, verdict, task_execution_id=701)
    details = _validation_events(tmp_path)[-1]["details"]
    serialized = json.dumps(details)
    assert secret not in serialized
    assert len(serialized) < 8_000


# --- R2: output metadata semantics (F-1, F-2, F-6) ------------------------------

_LONG = "y" * 12_001


@pytest.mark.parametrize(
    "mode, text, retained_chars, truncated",
    [
        ("passed", "1 passed", 0, False),
        ("failed", "1 failed", 8, False),
        ("failed", _LONG, 12_000, True),
        ("timeout", "partial \u00e9", 9, False),
        ("timeout", _LONG, 12_000, True),
        ("failed", "", 0, False),
        ("timeout", "", 0, False),
    ],
    ids=[
        "passed-omitted",
        "failed-short",
        "failed-capped",
        "timeout-short",
        "timeout-capped",
        "failed-empty",
        "timeout-empty",
    ],
)
def test_output_metadata_truncation_semantics(
    tmp_path, monkeypatch, mode, text, retained_chars, truncated
):
    def _fake_run(*_args, **kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired(
                cmd="pytest", timeout=kwargs["timeout"], output=text.encode()
            )
        return _fake_completed(text, 0 if mode == "passed" else 1)

    monkeypatch.setattr(candidate_checks.subprocess, "run", _fake_run)
    findings: list = []
    acceptance = candidate_checks._run_task_acceptance(
        project_dir=tmp_path,
        task_prompt=ACCEPTANCE_PROMPT,
        focused_command="",
        focused_returncode=None,
        timeout_seconds=5,
        findings=findings,
        commands_run=[],
    )

    output = acceptance["results"][0]["output"]
    assert output == {
        "scope": "combined_stdout_stderr_text",
        "chars": len(text),
        "bytes": len(text.encode()),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "retained_chars": retained_chars,
        "truncated": truncated,
    }
    if mode == "passed":
        assert findings == []
    else:
        assert findings[0].evidence["output"].startswith(text[:retained_chars])


def test_real_subprocess_timeout_output_is_decoded_text(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdout.buffer.write(b'h\\xc3\\xa9llo-timeout\\n'); sys.stdout.flush()\n"
        "sys.stderr.buffer.write(b'raw \\xff\\n'); sys.stderr.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"

    returncode, captured = candidate_checks._execute_command(
        project_dir=tmp_path, command=command, timeout_seconds=2
    )
    assert returncode is None
    assert isinstance(captured, str)
    assert "b'" not in captured and "\\x" not in captured
    assert captured.startswith("h\u00e9llo-timeout")
    assert "raw \ufffd" in captured
    assert "Timed out" not in captured

    returncode, legacy = candidate_checks._run_command(
        project_dir=tmp_path, command=command, timeout_seconds=2
    )
    assert returncode is None
    assert legacy == captured + "\nTimed out after 2s"


# --- R2: commit metadata and host-path exposure (F-3, F-5) ----------------------


def test_projection_states_pre_commit_and_omits_interpreter_path(tmp_path, db_session):
    _record(db_session, tmp_path, _acceptance_verdict(), task_execution_id=1001)
    db_session.commit()

    event = _validation_events(tmp_path)[-1]["details"]["task_acceptance_evidence"]
    ref = event["checkpoint_ref"]
    assert ref["projected_before_commit"] is True
    assert "commit_confirmed" not in ref
    assert "executed_command" not in event["results"][0]
    assert "/venv/bin/python" not in json.dumps(event)

    stored = json.loads(_checkpoints(db_session)[0].state_snapshot)
    raw = stored["details"]["task_acceptance_verification"]["results"][0]
    assert raw["executed_command"] == "/venv/bin/python -m pytest -q"
    assert "executed_command" not in (
        stored["details"]["task_acceptance_evidence"]["results"][0]
    )

    trace = build_trace_export(
        session_id=9, task_id=5, events=_validation_events(tmp_path), snapshots=[]
    )
    span = next(
        s for s in trace["spans"] if s["name"] == "task_acceptance_verification"
    )
    assert span["attributes"]["checkpoint_projected_before_commit"] is True
    assert "checkpoint_commit_confirmed" not in span["attributes"]


def test_projected_strings_and_lists_are_bounded(tmp_path, db_session):
    results = [
        {
            "command": "python -m pytest " + "a" * 5_000,
            "executed": True,
            "returncode": 1,
            "outcome": "failed",
        }
        for _ in range(50)
    ]
    _record(
        db_session,
        tmp_path,
        _acceptance_verdict(status="rejected", results=results),
        task_execution_id=702,
    )
    evidence = _validation_events(tmp_path)[-1]["details"]["task_acceptance_evidence"]
    assert len(evidence["results"]) == 20
    assert evidence["results_omitted"] == 30
    assert all(len(r["command"]) <= 500 for r in evidence["results"])


# --- I: checkpoint commit failure ----------------------------------------------


def test_commit_failure_leaves_projection_marked_pre_commit(
    tmp_path, db_session, monkeypatch
):
    _record(db_session, tmp_path, _acceptance_verdict(), task_execution_id=801)

    def _fail_commit():
        raise RuntimeError("simulated checkpoint commit failure")

    monkeypatch.setattr(db_session, "commit", _fail_commit)
    with pytest.raises(RuntimeError):
        db_session.commit()
    db_session.rollback()
    monkeypatch.undo()

    # The journal append preceded the failed commit (existing ordering): the
    # projection exists, but it is explicitly not a durable-success claim and
    # its checkpoint reference resolves to no committed checkpoint.
    assert _checkpoints(db_session) == []
    evidence = _validation_events(tmp_path)[-1]["details"]["task_acceptance_evidence"]
    ref = evidence["checkpoint_ref"]
    assert ref["authority"] == "task_checkpoint"
    assert ref["projected_before_commit"] is True

    # A committed checkpoint is resolvable from its projection's reference.
    _record(db_session, tmp_path, _acceptance_verdict(), task_execution_id=802)
    db_session.commit()
    committed = _checkpoints(db_session)
    assert len(committed) == 1
    committed_sha = hashlib.sha256(
        committed[0].state_snapshot.encode("utf-8")
    ).hexdigest()
    refs = [
        e["details"]["task_acceptance_evidence"]["checkpoint_ref"]
        for e in _validation_events(tmp_path)
    ]
    assert refs[0]["state_snapshot_sha256"] != committed_sha
    assert refs[1]["state_snapshot_sha256"] == committed_sha
    assert refs[1]["checkpoint_type"] == committed[0].checkpoint_type


def test_journal_failure_does_not_block_checkpoint(tmp_path, db_session, monkeypatch):
    def _boom(**_kwargs):
        raise OSError("journal unavailable")

    monkeypatch.setattr(persistence, "append_orchestration_event", _boom)
    _record(db_session, tmp_path, _acceptance_verdict(), task_execution_id=901)
    db_session.commit()

    stored = json.loads(_checkpoints(db_session)[0].state_snapshot)
    assert stored["details"]["task_acceptance_evidence"]["task_execution_id"] == 901
