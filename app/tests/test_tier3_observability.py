"""Tier 3 observability: event fingerprint index + counterfactual replay."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.services.orchestration.events.event_types import (
    EventType,
    is_known_event_type,
)
from app.services.orchestration.events.observability import build_trace_export
from app.services.orchestration.persistence import (
    _apply_counterfactual_overrides_to_checkpoint,
    append_orchestration_event,
    read_session_fingerprint_index,
    write_session_fingerprint_index,
)


# ── Fingerprint index ────────────────────────────────────────────────────────


def test_write_and_read_fingerprint_index_roundtrip(tmp_path):
    fp = {
        "session_id": 1,
        "anomaly_tags": ["tool_failed", "retry_entered"],
        "retry_count": 3,
        "min_health_score": 45,
    }
    write_session_fingerprint_index(tmp_path, 1, fp)
    cached = read_session_fingerprint_index(tmp_path, 1)
    assert cached is not None
    assert cached["session_id"] == 1
    assert cached["anomaly_tags"] == ["tool_failed", "retry_entered"]
    assert cached["retry_count"] == 3
    assert "indexed_at" in cached


def test_read_fingerprint_index_returns_none_when_missing(tmp_path):
    result = read_session_fingerprint_index(tmp_path, 999)
    assert result is None


def test_read_fingerprint_index_returns_none_when_expired(tmp_path):
    fp = {"session_id": 5, "anomaly_tags": []}
    write_session_fingerprint_index(tmp_path, 5, fp)

    index_path = tmp_path / ".openclaw" / "fingerprints" / "session_5.json"
    data = json.loads(index_path.read_text())
    # Backdate by 10 minutes — beyond any reasonable TTL.
    old_ts = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    data["indexed_at"] = old_ts
    index_path.write_text(json.dumps(data))

    result = read_session_fingerprint_index(tmp_path, 5, max_age_seconds=300)
    assert result is None


def test_read_fingerprint_index_max_age_zero_skips_ttl_check(tmp_path):
    fp = {"session_id": 7, "anomaly_tags": ["tool_failed"]}
    write_session_fingerprint_index(tmp_path, 7, fp)

    index_path = tmp_path / ".openclaw" / "fingerprints" / "session_7.json"
    data = json.loads(index_path.read_text())
    old_ts = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    data["indexed_at"] = old_ts
    index_path.write_text(json.dumps(data))

    # max_age_seconds=0 means skip TTL check.
    result = read_session_fingerprint_index(tmp_path, 7, max_age_seconds=0)
    assert result is not None
    assert result["anomaly_tags"] == ["tool_failed"]


def test_write_fingerprint_index_overwrites_stale_entry(tmp_path):
    write_session_fingerprint_index(
        tmp_path, 2, {"session_id": 2, "anomaly_tags": ["old"]}
    )
    write_session_fingerprint_index(
        tmp_path, 2, {"session_id": 2, "anomaly_tags": ["new"]}
    )
    cached = read_session_fingerprint_index(tmp_path, 2)
    assert cached["anomaly_tags"] == ["new"]


def test_fingerprint_index_stored_in_correct_location(tmp_path):
    write_session_fingerprint_index(
        tmp_path, 42, {"session_id": 42, "anomaly_tags": []}
    )
    expected = tmp_path / ".openclaw" / "fingerprints" / "session_42.json"
    assert expected.exists()


# ── Counterfactual event type ────────────────────────────────────────────────


def test_counterfactual_replay_started_is_known_event_type():
    assert is_known_event_type(EventType.COUNTERFACTUAL_REPLAY_STARTED)


def test_counterfactual_replay_started_constant_value():
    assert EventType.COUNTERFACTUAL_REPLAY_STARTED == "counterfactual_replay_started"


# ── _apply_counterfactual_overrides_to_checkpoint ────────────────────────────


def _base_checkpoint(step_index: int = 2, plan_size: int = 4) -> Dict[str, Any]:
    plan = [f"Step {i + 1}" for i in range(plan_size)]
    execution_results = [
        {"step_number": i + 1, "status": "success"} for i in range(step_index)
    ]
    step_results = [
        {"step_number": i + 1, "status": "success"} for i in range(step_index)
    ]
    return {
        "checkpoint_name": "autosave_latest",
        "orchestration_state": {
            "status": "executing",
            "plan": plan,
            "current_step_index": step_index,
            "debug_attempts": [{"attempt": 1}],
            "execution_results": execution_results,
            "validation_history": [],
        },
        "current_step_index": step_index,
        "step_results": step_results,
        "context": {"task_id": 1, "task_description": "do stuff"},
    }


def test_no_overrides_returns_deep_copy_identity():
    cp = _base_checkpoint()
    result, applied, deferred = _apply_counterfactual_overrides_to_checkpoint(
        cp, overrides={}
    )
    assert applied == {}
    assert deferred == {}
    assert "replay_overrides" not in result.get("context", {})
    # Deep copy — mutating result does not mutate original.
    result["orchestration_state"]["current_step_index"] = 99
    assert cp["orchestration_state"]["current_step_index"] == 2


def test_step_from_index_rewinds_checkpoint(tmp_path):
    cp = _base_checkpoint(step_index=3, plan_size=5)
    result, applied, deferred = _apply_counterfactual_overrides_to_checkpoint(
        cp, overrides={"step_from_index": 1}
    )
    assert result["orchestration_state"]["current_step_index"] == 1
    assert result["current_step_index"] == 1
    assert result["orchestration_state"]["debug_attempts"] == []
    # Only step 1 (step_number=1, index 0 < 1) is kept.
    assert len(result["orchestration_state"]["execution_results"]) == 1
    assert len(result["step_results"]) == 1
    assert applied == {"step_from_index": 1}
    assert deferred == {}


def test_step_from_index_zero_clears_all_results():
    cp = _base_checkpoint(step_index=3, plan_size=4)
    result, applied, _ = _apply_counterfactual_overrides_to_checkpoint(
        cp, overrides={"step_from_index": 0}
    )
    assert result["orchestration_state"]["current_step_index"] == 0
    assert result["orchestration_state"]["execution_results"] == []
    assert result["step_results"] == []
    assert applied["step_from_index"] == 0


def test_step_from_index_clamped_to_plan_upper_bound():
    cp = _base_checkpoint(step_index=1, plan_size=3)
    result, applied, _ = _apply_counterfactual_overrides_to_checkpoint(
        cp, overrides={"step_from_index": 999}
    )
    # Clamped to len(plan) - 1 = 2.
    assert result["orchestration_state"]["current_step_index"] == 2
    assert applied["step_from_index"] == 2


def test_step_from_index_negative_clamped_to_zero():
    cp = _base_checkpoint(step_index=2, plan_size=3)
    result, applied, _ = _apply_counterfactual_overrides_to_checkpoint(
        cp, overrides={"step_from_index": -5}
    )
    assert result["orchestration_state"]["current_step_index"] == 0
    assert applied["step_from_index"] == 0


def test_policy_profile_stored_as_deferred():
    cp = _base_checkpoint()
    result, applied, deferred = _apply_counterfactual_overrides_to_checkpoint(
        cp, overrides={"policy_profile": "strict"}
    )
    assert "policy_profile" not in applied
    assert deferred == {"policy_profile": "strict"}
    assert result["context"]["replay_overrides"]["policy_profile"] == "strict"


def test_model_family_stored_as_deferred():
    cp = _base_checkpoint()
    _, applied, deferred = _apply_counterfactual_overrides_to_checkpoint(
        cp, overrides={"model_family": "claude-3-7-sonnet"}
    )
    assert "model_family" not in applied
    assert deferred["model_family"] == "claude-3-7-sonnet"


def test_adaptation_profile_stored_as_deferred():
    cp = _base_checkpoint()
    result, applied, deferred = _apply_counterfactual_overrides_to_checkpoint(
        cp, overrides={"adaptation_profile": "verbose"}
    )
    assert deferred["adaptation_profile"] == "verbose"
    assert result["context"]["replay_overrides"]["adaptation_profile"] == "verbose"


def test_combined_overrides_applied_and_deferred_are_disjoint():
    cp = _base_checkpoint(step_index=3, plan_size=5)
    result, applied, deferred = _apply_counterfactual_overrides_to_checkpoint(
        cp,
        overrides={
            "step_from_index": 1,
            "policy_profile": "strict",
            "model_family": "claude-opus-4",
        },
    )
    assert "step_from_index" in applied
    assert "step_from_index" not in deferred
    assert "policy_profile" in deferred
    assert "model_family" in deferred
    assert "policy_profile" not in applied
    assert result["context"]["replay_overrides"]["policy_profile"] == "strict"
    assert result["context"]["replay_overrides"]["model_family"] == "claude-opus-4"
    assert result["orchestration_state"]["current_step_index"] == 1


def test_existing_context_fields_preserved_after_replay_overrides():
    cp = _base_checkpoint()
    cp["context"]["task_subfolder"] = "my-task"
    result, _, _ = _apply_counterfactual_overrides_to_checkpoint(
        cp, overrides={"policy_profile": "balanced"}
    )
    assert result["context"]["task_subfolder"] == "my-task"
    assert result["context"]["replay_overrides"]["policy_profile"] == "balanced"


def test_unknown_override_keys_are_ignored():
    cp = _base_checkpoint()
    result, applied, deferred = _apply_counterfactual_overrides_to_checkpoint(
        cp, overrides={"totally_unknown_key": "value"}
    )
    assert applied == {}
    assert deferred == {}


# ── Cross-session divergence compare ────────────────────────────────────────


def test_divergence_compare_basic_structure(db_session, monkeypatch):
    from app.models import Project, Session as SessionModel
    from app.services.session.session_inspection_service import (
        get_session_divergence_compare_payload,
    )

    project = Project(name="tier3-compare-proj", workspace_path="/tmp/tier3test")
    db_session.add(project)
    db_session.flush()

    session_a = SessionModel(
        name="tier3-alpha", project_id=project.id, status="stopped"
    )
    session_b = SessionModel(name="tier3-beta", project_id=project.id, status="stopped")
    db_session.add(session_a)
    db_session.add(session_b)
    db_session.flush()
    db_session.commit()

    TAGS_A = sorted(["tool_failed", "retry_entered"])
    TAGS_B = sorted(["tool_failed", "retry_entered"])

    def _mock_fp(db, session):
        return {
            "session_id": session.id,
            "session_name": session.name,
            "status": session.status,
            "created_at": None,
            "task_count": 0,
            "event_count": 0,
            "retry_count": 2,
            "tool_failure_count": 1,
            "intent_gap_count": 0,
            "divergence_count": 0,
            "divergence_reasons": [],
            "validation_statuses": [],
            "min_health_score": None,
            "anomaly_tags": TAGS_A if session.id == session_a.id else TAGS_B,
        }

    monkeypatch.setattr(
        "app.services.session.session_inspection_service._build_session_divergence_fingerprint",
        _mock_fp,
    )

    result = get_session_divergence_compare_payload(db_session, session_a.id, limit=5)
    assert result["session_id"] == session_a.id
    assert result["project_id"] == project.id
    assert "current" in result
    assert "matches" in result
    assert isinstance(result["matches"], list)
    # session_b has same tags → similarity > 0
    match = next(
        (m for m in result["matches"] if m["session_id"] == session_b.id), None
    )
    assert match is not None
    assert match["similarity_score"] > 0
    assert sorted(match["shared_tags"]) == TAGS_A


def test_divergence_compare_no_match_when_no_siblings(db_session, monkeypatch):
    from app.models import Project, Session as SessionModel
    from app.services.session.session_inspection_service import (
        get_session_divergence_compare_payload,
    )

    project = Project(name="tier3-solo", workspace_path="/tmp/tier3solo")
    db_session.add(project)
    db_session.flush()

    session_only = SessionModel(
        name="only-session", project_id=project.id, status="stopped"
    )
    db_session.add(session_only)
    db_session.flush()
    db_session.commit()

    monkeypatch.setattr(
        "app.services.session.session_inspection_service._build_session_divergence_fingerprint",
        lambda db, session: {
            "session_id": session.id,
            "session_name": session.name,
            "status": session.status,
            "created_at": None,
            "task_count": 0,
            "event_count": 0,
            "retry_count": 0,
            "tool_failure_count": 0,
            "intent_gap_count": 0,
            "divergence_count": 0,
            "divergence_reasons": [],
            "validation_statuses": [],
            "min_health_score": None,
            "anomaly_tags": [],
        },
    )

    result = get_session_divergence_compare_payload(
        db_session, session_only.id, limit=5
    )
    assert result["matches"] == []


def test_divergence_compare_higher_tag_overlap_scores_higher(db_session, monkeypatch):
    from app.models import Project, Session as SessionModel
    from app.services.session.session_inspection_service import (
        get_session_divergence_compare_payload,
    )

    project = Project(name="tier3-rank", workspace_path="/tmp/tier3rank")
    db_session.add(project)
    db_session.flush()

    session_current = SessionModel(
        name="current", project_id=project.id, status="stopped"
    )
    session_close = SessionModel(name="close", project_id=project.id, status="stopped")
    session_distant = SessionModel(
        name="distant", project_id=project.id, status="stopped"
    )
    for s in (session_current, session_close, session_distant):
        db_session.add(s)
    db_session.flush()
    db_session.commit()

    CURRENT_TAGS = ["tool_failed", "retry_entered", "divergence:retry_cluster"]
    CLOSE_TAGS = ["tool_failed", "retry_entered", "divergence:retry_cluster"]
    DISTANT_TAGS = []

    def _mock_fp(db, session):
        tags = (
            CURRENT_TAGS
            if session.id == session_current.id
            else CLOSE_TAGS if session.id == session_close.id else DISTANT_TAGS
        )
        return {
            "session_id": session.id,
            "session_name": session.name,
            "status": session.status,
            "created_at": None,
            "task_count": 0,
            "event_count": 0,
            "retry_count": 1,
            "tool_failure_count": 1,
            "intent_gap_count": 0,
            "divergence_count": 0,
            "divergence_reasons": [],
            "validation_statuses": [],
            "min_health_score": None,
            "anomaly_tags": sorted(tags),
        }

    monkeypatch.setattr(
        "app.services.session.session_inspection_service._build_session_divergence_fingerprint",
        _mock_fp,
    )

    result = get_session_divergence_compare_payload(
        db_session, session_current.id, limit=5
    )
    close_m = next(m for m in result["matches"] if m["session_id"] == session_close.id)
    distant_m = next(
        m for m in result["matches"] if m["session_id"] == session_distant.id
    )
    assert close_m["similarity_score"] > distant_m["similarity_score"]


# ── Counterfactual replay API endpoint ───────────────────────────────────────


def test_counterfactual_replay_endpoint_exists(authenticated_client):
    # GET should 405 (method not allowed), confirming route registered.
    resp = authenticated_client.get(
        "/api/v1/sessions/9999/checkpoints/no-such/counterfactual-replay"
    )
    assert resp.status_code == 405


def test_counterfactual_replay_returns_404_for_unknown_session(authenticated_client):
    resp = authenticated_client.post(
        "/api/v1/sessions/9999/checkpoints/autosave_latest/counterfactual-replay",
        json={},
    )
    assert resp.status_code == 404


def test_trace_export_sorts_timestamps_by_actual_instant():
    trace = build_trace_export(
        session_id=1,
        task_id=2,
        events=[
            {
                "event_id": "late",
                "event_type": EventType.PHASE_STARTED,
                "timestamp": "2026-01-01T10:00:00+05:30",
                "details": {"phase": "planning"},
            },
            {
                "event_id": "early",
                "event_type": EventType.PHASE_STARTED,
                "timestamp": "2026-01-01T05:00:00Z",
                "details": {"phase": "execution"},
            },
        ],
        snapshots=[],
    )

    assert [span["span_id"] for span in trace["spans"]] == ["late", "early"]
