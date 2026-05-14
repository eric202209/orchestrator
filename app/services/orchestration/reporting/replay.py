"""Deterministic orchestration replay and execution reconstruction.

This module reconstructs compact control-plane state from append-only
orchestration evidence. The reducer is intentionally pure: it consumes sorted
event dictionaries only and does not read database, filesystem, checkpoint, or
workspace state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..events.event_types import EventType, is_known_event_type
from ..state.persistence import (
    _compute_workspace_hash,
    _orchestration_event_log_path,
    _orchestration_state_snapshot_log_path,
)

REDUCER_VERSION = "phase4a-v1"
COMPATIBILITY_VERSION = "phase4a-compat-v1"

AUTHORITATIVE_RECONSTRUCTED_FIELDS = (
    "session_id",
    "task_id",
    "phase",
    "status",
    "current_step_index",
    "retry_count",
    "repair_count",
    "latest_checkpoint_name",
    "latest_checkpoint_event_id",
    "latest_failure_event_id",
    "latest_divergence_event_id",
    "validation_verdict_status_history",
    "intervention_status",
)

NON_AUTHORITATIVE_ARTIFACT_FIELDS = (
    "timestamps",
    "workspace_hashes",
    "changed_files",
    "tool_events",
    "reasoning_artifacts",
    "checkpoint_payloads",
    "current_workspace_observations",
    "causal_links",
    "knowledge_usage_associations",
    "plan_metadata",
)

TRANSITION_EVENT_TYPES = {
    EventType.PHASE_STARTED,
    EventType.PHASE_FINISHED,
    EventType.TASK_QUEUED,
    EventType.TASK_CLAIMED,
    EventType.TASK_DISPATCH_REJECTED,
    EventType.TASK_STARTED,
    EventType.TASK_COMPLETED,
    EventType.TASK_FAILED,
    EventType.STEP_STARTED,
    EventType.STEP_FINISHED,
    EventType.RETRY_ENTERED,
    EventType.VALIDATION_RESULT,
    EventType.REPAIR_GENERATED,
    EventType.REPAIR_APPLIED,
    EventType.REPAIR_REJECTED,
    EventType.EVALUATOR_RESULT,
    EventType.COMPLETION_EVIDENCE_FAILED,
    EventType.CHECKPOINT_SAVED,
    EventType.CHECKPOINT_LOADED,
    EventType.CHECKPOINT_REDIRECTED,
    EventType.WORKSPACE_RESTORE_SKIPPED,
    EventType.WORKSPACE_PRESERVED,
    EventType.RESUME_WORKSPACE_DRIFT,
    EventType.WORKSPACE_CONTRACT_FAILED,
    EventType.HEALTH_SCORE_UPDATED,
    EventType.DIVERGENCE_DETECTED,
    EventType.INTENT_OUTCOME_MISMATCH,
    EventType.WAITING_FOR_INPUT,
    EventType.HUMAN_INTERVENTION_REQUESTED,
    EventType.HUMAN_INTERVENTION_REPLIED,
    EventType.COUNTERFACTUAL_REPLAY_STARTED,
}

FAILURE_LIKE_EVENT_TYPES = {
    EventType.TOOL_FAILED,
    EventType.TASK_FAILED,
    EventType.REPAIR_REJECTED,
    EventType.COMPLETION_EVIDENCE_FAILED,
    EventType.WAITING_FOR_INPUT,
    EventType.WORKSPACE_CONTRACT_FAILED,
}

PROBLEM_STATUSES = {"failed", "failure", "rejected", "repair_required", "error"}


@dataclass(frozen=True)
class ReplayRecord:
    """A readable event record plus journal position metadata."""

    event: Dict[str, Any]
    line_index: int


def reconstruct_execution_state(
    *,
    project_dir: Any,
    session_id: int,
    task_id: int,
    boundary: Optional[Dict[str, Any]] = None,
    checkpoint_dir: Any = None,
    compare_workspace: bool = False,
) -> Dict[str, Any]:
    """Build a read-only replay report for one session/task pair."""

    requested_boundary = boundary or {"mode": "full"}
    records, read_findings = _read_event_records(project_dir, session_id, task_id)
    ordered_records, ordering_findings = _sort_records(records)
    selected_records, resolved_boundary, boundary_findings = _select_boundary_records(
        ordered_records,
        requested_boundary,
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
    )

    events = [record.event for record in selected_records]
    state = reduce_replay_events(events, session_id=session_id, task_id=task_id)
    snapshot_findings = _snapshot_integrity_findings(project_dir, session_id, task_id)
    integrity = _build_integrity(
        all_records=ordered_records,
        applied_records=selected_records,
        findings=[
            *read_findings,
            *ordering_findings,
            *boundary_findings,
            *snapshot_findings,
        ],
        boundary_resolved=resolved_boundary.get("resolved", False),
        session_id=session_id,
        task_id=task_id,
    )

    checkpoint_comparison = None
    if resolved_boundary.get("mode") == "to_checkpoint_name":
        checkpoint_comparison = _compare_checkpoint_payload(
            checkpoint_dir=checkpoint_dir,
            session_id=session_id,
            checkpoint_name=resolved_boundary.get("resolved_checkpoint_name"),
            state=state,
        )
        if checkpoint_comparison.get("status") in {"conflict", "missing", "unreadable"}:
            integrity["findings"].append(
                {
                    "type": "checkpoint_comparison",
                    "severity": "warning",
                    "message": checkpoint_comparison.get("summary"),
                }
            )

    workspace_evidence = _workspace_evidence_status(
        project_dir=project_dir,
        state=state,
        compare_workspace=compare_workspace,
    )
    state["workspace_evidence_status"] = workspace_evidence["status"]

    integrity["confidence"] = _confidence_for_findings(
        integrity["findings"],
        boundary_resolved=resolved_boundary.get("resolved", False),
        event_count_applied=integrity["event_count_applied"],
    )
    drift_findings = _build_drift_findings(
        checkpoint_comparison=checkpoint_comparison,
        workspace_evidence=workspace_evidence,
        integrity=integrity,
    )
    determinism = _build_determinism(
        integrity=integrity,
        workspace_evidence=workspace_evidence,
        drift_findings=drift_findings,
    )

    return {
        "reducer_version": REDUCER_VERSION,
        "compatibility_version": COMPATIBILITY_VERSION,
        "session_id": session_id,
        "task_id": task_id,
        "boundary": resolved_boundary,
        "state": state,
        "field_classification": {
            "authoritative": list(AUTHORITATIVE_RECONSTRUCTED_FIELDS),
            "artifacts": list(NON_AUTHORITATIVE_ARTIFACT_FIELDS),
        },
        "integrity": integrity,
        "determinism": determinism,
        "drift_findings": drift_findings,
        "checkpoint_comparison": checkpoint_comparison,
        "workspace_evidence": workspace_evidence,
    }


def reduce_replay_events(
    events: Iterable[Dict[str, Any]],
    *,
    session_id: int,
    task_id: int,
) -> Dict[str, Any]:
    """Pure reducer for replay-safe orchestration events."""

    state: Dict[str, Any] = {
        "session_id": session_id,
        "task_id": task_id,
        "phase": None,
        "status": None,
        "current_step_index": 0,
        "retry_count": 0,
        "repair_count": 0,
        "latest_checkpoint_name": None,
        "latest_checkpoint_event_id": None,
        "latest_failure_event_id": None,
        "latest_divergence_event_id": None,
        "validation_verdict_status_history": [],
        "intervention_status": None,
        "plan_step_count": None,
        "changed_files": [],
        "workspace_hashes": [],
        "tool_events": [],
        "reasoning_artifacts": [],
        "timestamps": [],
    }

    changed_files: set[str] = set()
    for event in events:
        event_type = str(event.get("event_type") or "")
        if not is_known_event_type(event_type):
            continue

        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        event_id = str(event.get("event_id") or "")
        timestamp = event.get("timestamp")
        if timestamp:
            state["timestamps"].append(timestamp)

        if event_type == EventType.PHASE_STARTED:
            state["phase"] = details.get("phase") or state.get("phase")
            state["status"] = "started"
        elif event_type == EventType.PHASE_FINISHED:
            state["phase"] = details.get("phase") or state.get("phase")
            state["status"] = details.get("status") or "finished"
        elif event_type == EventType.TASK_QUEUED:
            state["phase"] = "execution"
            state["status"] = "queued"
        elif event_type == EventType.TASK_CLAIMED:
            state["phase"] = "execution"
            state["status"] = "claimed"
        elif event_type == EventType.TASK_DISPATCH_REJECTED:
            state["phase"] = "execution"
            state["status"] = "dispatch_rejected"
            state["latest_failure_event_id"] = event_id
        elif event_type == EventType.TASK_STARTED:
            state["phase"] = "execution"
            state["status"] = "running"
        elif event_type == EventType.TASK_COMPLETED:
            state["phase"] = "completion"
            state["status"] = "completed"
        elif event_type == EventType.TASK_FAILED:
            state["phase"] = "failure"
            state["status"] = "failed"
            state["latest_failure_event_id"] = event_id
        elif event_type == EventType.STEP_STARTED:
            state["phase"] = "execution"
            state["status"] = "running"
            _update_step_index(state, details, advance=False)
        elif event_type == EventType.STEP_FINISHED:
            state["phase"] = "execution"
            status = str(details.get("status") or "finished")
            state["status"] = status
            _update_step_index(state, details, advance=status.lower() == "success")
            if _is_problem_event(event_type, details):
                state["latest_failure_event_id"] = event_id
        elif event_type == EventType.RETRY_ENTERED:
            state["phase"] = "execution"
            state["status"] = "retrying"
            state["retry_count"] = max(
                int(state["retry_count"] or 0) + 1,
                int(details.get("attempt") or details.get("retry_count") or 0),
            )
        elif event_type == EventType.VALIDATION_RESULT:
            state["phase"] = "validation"
            status = str(details.get("status") or "")
            if status:
                state["status"] = status
                state["validation_verdict_status_history"].append(status)
            if _is_problem_event(event_type, details):
                state["latest_failure_event_id"] = event_id
        elif event_type in {
            EventType.REPAIR_GENERATED,
            EventType.REPAIR_APPLIED,
            EventType.REPAIR_REJECTED,
        }:
            state["phase"] = "completion"
            state["status"] = event_type
            state["repair_count"] = int(state["repair_count"] or 0) + 1
            if event_type == EventType.REPAIR_REJECTED:
                state["latest_failure_event_id"] = event_id
        elif event_type == EventType.EVALUATOR_RESULT:
            state["phase"] = "completion"
            state["status"] = details.get("status") or "evaluated"
        elif event_type == EventType.COMPLETION_EVIDENCE_FAILED:
            state["phase"] = "completion"
            state["status"] = "evidence_failed"
            state["latest_failure_event_id"] = event_id
        elif event_type in {
            EventType.CHECKPOINT_SAVED,
            EventType.CHECKPOINT_LOADED,
            EventType.CHECKPOINT_REDIRECTED,
        }:
            state["latest_checkpoint_name"] = (
                details.get("checkpoint_name")
                or details.get("resolved_checkpoint_name")
                or details.get("requested_checkpoint_name")
                or state.get("latest_checkpoint_name")
            )
            state["latest_checkpoint_event_id"] = event_id
            if details.get("current_step_index") is not None:
                state["current_step_index"] = int(
                    details.get("current_step_index") or 0
                )
            if details.get("status"):
                state["status"] = details.get("status")
        elif event_type == EventType.DIVERGENCE_DETECTED:
            state["latest_divergence_event_id"] = event_id
        elif event_type in {
            EventType.WAITING_FOR_INPUT,
            EventType.HUMAN_INTERVENTION_REQUESTED,
        }:
            state["phase"] = "failure"
            state["status"] = "awaiting_input"
            state["intervention_status"] = "pending"
            state["latest_failure_event_id"] = event_id
        elif event_type == EventType.HUMAN_INTERVENTION_REPLIED:
            state["intervention_status"] = details.get("decision") or "replied"
        elif event_type in {
            EventType.WORKSPACE_CONTRACT_FAILED,
            EventType.RESUME_WORKSPACE_DRIFT,
        }:
            state["latest_failure_event_id"] = event_id
        elif event_type == EventType.REASONING_ARTIFACT_GENERATED:
            state["reasoning_artifacts"].append(
                {
                    "event_id": event_id,
                    "timestamp": timestamp,
                    "artifact_type": details.get("artifact_type"),
                }
            )
        elif event_type in {EventType.TOOL_INVOKED, EventType.TOOL_FAILED}:
            state["tool_events"].append(
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "tool_name": details.get("tool_name"),
                }
            )
            if event_type == EventType.TOOL_FAILED:
                state["latest_failure_event_id"] = event_id

        changed_names = (
            details.get("files_changed") or details.get("changed_files") or []
        )
        for filename in changed_names:
            changed_files.add(str(filename))
        if details.get("workspace_hash"):
            state["workspace_hashes"].append(details.get("workspace_hash"))
        if details.get("plan_step_count") is not None:
            state["plan_step_count"] = int(details.get("plan_step_count") or 0)

    state["changed_files"] = sorted(changed_files)
    return state


def _read_event_records(
    project_dir: Any, session_id: int, task_id: int
) -> tuple[List[ReplayRecord], List[Dict[str, Any]]]:
    log_path = _orchestration_event_log_path(project_dir, session_id, task_id)
    findings: List[Dict[str, Any]] = []
    records: List[ReplayRecord] = []
    if not log_path.exists():
        findings.append(
            {
                "type": "missing_event_journal",
                "severity": "error",
                "message": f"No event journal found at {log_path}",
            }
        )
        return records, findings

    try:
        with log_path.open("r", encoding="utf-8") as handle:
            for line_index, line in enumerate(handle):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    findings.append(
                        {
                            "type": "malformed_jsonl",
                            "severity": "warning",
                            "line_index": line_index,
                            "message": str(exc),
                        }
                    )
                    continue
                records.append(ReplayRecord(event=event, line_index=line_index))
    except OSError as exc:
        findings.append(
            {
                "type": "event_journal_read_error",
                "severity": "error",
                "message": str(exc),
            }
        )
    return records, findings


def _sort_records(
    records: List[ReplayRecord],
) -> tuple[List[ReplayRecord], List[Dict[str, Any]]]:
    findings: List[Dict[str, Any]] = []
    previous_key = None
    for record in records:
        key = _record_sort_key(record)
        if previous_key is not None and key < previous_key:
            findings.append(
                {
                    "type": "event_order_anomaly",
                    "severity": "info",
                    "line_index": record.line_index,
                    "message": (
                        "Event journal order differs from deterministic replay order"
                    ),
                }
            )
            break
        previous_key = key
    return sorted(records, key=_record_sort_key), findings


def _record_sort_key(record: ReplayRecord) -> tuple[str, int, int, str]:
    event = record.event
    timestamp = str(event.get("timestamp") or "")
    task_id = int(event.get("task_id") or 0)
    event_id = str(event.get("event_id") or "")
    return (timestamp, task_id, record.line_index, event_id)


def _select_boundary_records(
    records: List[ReplayRecord],
    boundary: Dict[str, Any],
    *,
    project_dir: Any,
    session_id: int,
    task_id: int,
) -> tuple[List[ReplayRecord], Dict[str, Any], List[Dict[str, Any]]]:
    mode = str(boundary.get("mode") or "full")
    requested = boundary.get("value") or boundary.get("requested")
    findings: List[Dict[str, Any]] = []
    resolved: Dict[str, Any] = {
        "mode": mode,
        "requested": requested,
        "resolved": False,
    }

    if mode == "full":
        resolved["resolved"] = True
        if records:
            resolved["resolved_event_id"] = records[-1].event.get("event_id")
        return records, resolved, findings

    if mode == "to_event_id":
        for index, record in enumerate(records):
            if record.event.get("event_id") == requested:
                resolved.update(
                    {
                        "resolved": True,
                        "resolved_event_id": requested,
                        "line_index": record.line_index,
                    }
                )
                return records[: index + 1], resolved, findings
        findings.append(_boundary_not_found(mode, requested))
        return [], resolved, findings

    if mode == "to_timestamp":
        selected = [
            record
            for record in records
            if str(record.event.get("timestamp") or "") <= str(requested)
        ]
        resolved.update(
            {
                "resolved": True,
                "resolved_timestamp": requested,
                "resolved_event_id": (
                    selected[-1].event.get("event_id") if selected else None
                ),
            }
        )
        return selected, resolved, findings

    if mode == "to_checkpoint_name":
        for index, record in enumerate(records):
            event = record.event
            details = (
                event.get("details") if isinstance(event.get("details"), dict) else {}
            )
            if event.get("event_type") not in {
                EventType.CHECKPOINT_SAVED,
                EventType.CHECKPOINT_LOADED,
                EventType.CHECKPOINT_REDIRECTED,
            }:
                continue
            candidate_name = (
                details.get("checkpoint_name")
                or details.get("resolved_checkpoint_name")
                or details.get("requested_checkpoint_name")
            )
            if candidate_name == requested:
                resolved.update(
                    {
                        "resolved": True,
                        "resolved_checkpoint_name": candidate_name,
                        "resolved_event_id": event.get("event_id"),
                        "line_index": record.line_index,
                    }
                )
                return records[: index + 1], resolved, findings
        findings.append(_boundary_not_found(mode, requested))
        return [], resolved, findings

    if mode == "to_snapshot_index":
        snapshots = _read_state_snapshots(project_dir, session_id, task_id)
        try:
            snapshot_index = int(requested)
            snapshot = snapshots[snapshot_index]
        except (TypeError, ValueError, IndexError):
            findings.append(_boundary_not_found(mode, requested))
            return [], resolved, findings
        related_event_id = snapshot.get("related_event_id")
        if related_event_id:
            return _select_boundary_records(
                records,
                {"mode": "to_event_id", "requested": related_event_id},
                project_dir=project_dir,
                session_id=session_id,
                task_id=task_id,
            )
        timestamp = snapshot.get("timestamp")
        selected = [
            record
            for record in records
            if str(record.event.get("timestamp") or "") <= str(timestamp)
        ]
        resolved.update(
            {
                "resolved": True,
                "resolved_snapshot_index": snapshot_index,
                "resolved_timestamp": timestamp,
                "resolved_event_id": (
                    selected[-1].event.get("event_id") if selected else None
                ),
            }
        )
        return selected, resolved, findings

    findings.append(
        {
            "type": "unknown_boundary_mode",
            "severity": "error",
            "message": f"Unknown replay boundary mode: {mode}",
        }
    )
    return [], resolved, findings


def _read_state_snapshots(
    project_dir: Any, session_id: int, task_id: int
) -> List[Dict[str, Any]]:
    path = _orchestration_state_snapshot_log_path(project_dir, session_id, task_id)
    snapshots: List[Dict[str, Any]] = []
    if not path.exists():
        return snapshots
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    snapshots.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return snapshots


def _build_integrity(
    *,
    all_records: List[ReplayRecord],
    applied_records: List[ReplayRecord],
    findings: List[Dict[str, Any]],
    boundary_resolved: bool,
    session_id: int,
    task_id: int,
) -> Dict[str, Any]:
    event_ids: Dict[str, int] = {}
    parent_ids: set[str] = set()
    known_event_ids: set[str] = set()
    unknown_event_types: set[str] = set()
    timestamp_failures = 0
    session_task_mismatches = 0

    for record in all_records:
        event = record.event
        event_id = str(event.get("event_id") or "")
        if event_id:
            if event_id in event_ids:
                findings.append(
                    {
                        "type": "duplicate_event_id",
                        "severity": "warning",
                        "event_id": event_id,
                        "message": "Duplicate event_id found in event journal",
                    }
                )
            event_ids[event_id] = record.line_index
            known_event_ids.add(event_id)

        event_type = str(event.get("event_type") or "")
        if event_type and not is_known_event_type(event_type):
            unknown_event_types.add(event_type)
            findings.append(
                {
                    "type": "event_type_ignored_by_reducer",
                    "severity": "info",
                    "event_type": event_type,
                    "event_id": event_id or None,
                    "message": "Unknown event type was ignored by replay reducer",
                }
            )

        if event.get("timestamp") and not _parse_timestamp(event.get("timestamp")):
            timestamp_failures += 1

        if event.get("parent_event_id"):
            parent_ids.add(str(event.get("parent_event_id")))

        if event.get("session_id") is not None and _coerce_int(
            event.get("session_id")
        ) != int(session_id):
            session_task_mismatches += 1
        if event.get("task_id") is not None and _coerce_int(
            event.get("task_id")
        ) != int(task_id):
            session_task_mismatches += 1

    missing_parents = sorted(parent_ids - known_event_ids)
    for parent_id in missing_parents:
        findings.append(
            {
                "type": "missing_parent_event",
                "severity": "info",
                "event_id": parent_id,
                "message": "Parent event reference was not present in the journal",
            }
        )
    if timestamp_failures:
        findings.append(
            {
                "type": "timestamp_parse_failure",
                "severity": "warning",
                "count": timestamp_failures,
                "message": "One or more event timestamps could not be parsed",
            }
        )
    if session_task_mismatches:
        findings.append(
            {
                "type": "session_task_mismatch",
                "severity": "warning",
                "count": session_task_mismatches,
                "message": "One or more events did not match the replay identity",
            }
        )

    return {
        "confidence": _confidence_for_findings(
            findings,
            boundary_resolved=boundary_resolved,
            event_count_applied=len(applied_records),
        ),
        "event_count_read": len(all_records),
        "event_count_applied": len(applied_records),
        "malformed_line_count": sum(
            1 for finding in findings if finding.get("type") == "malformed_jsonl"
        ),
        "unknown_event_types": sorted(unknown_event_types),
        "findings": findings,
    }


def _snapshot_integrity_findings(
    project_dir: Any, session_id: int, task_id: int
) -> List[Dict[str, Any]]:
    snapshots = _read_state_snapshots(project_dir, session_id, task_id)
    findings: List[Dict[str, Any]] = []
    seen_indexes: set[int] = set()
    for expected_index, snapshot in enumerate(snapshots):
        raw_index = snapshot.get("snapshot_index")
        if raw_index is None:
            continue
        try:
            snapshot_index = int(raw_index)
        except (TypeError, ValueError):
            findings.append(
                {
                    "type": "snapshot_index_invalid",
                    "severity": "warning",
                    "message": "State snapshot index could not be parsed",
                }
            )
            continue
        if snapshot_index in seen_indexes or snapshot_index != expected_index:
            findings.append(
                {
                    "type": "snapshot_index_anomaly",
                    "severity": "info",
                    "snapshot_index": snapshot_index,
                    "expected_index": expected_index,
                    "message": "State snapshot indexes are not continuous",
                }
            )
        seen_indexes.add(snapshot_index)
    return findings


def _compare_checkpoint_payload(
    *,
    checkpoint_dir: Any,
    session_id: int,
    checkpoint_name: Optional[str],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    if not checkpoint_name:
        return {"status": "missing", "summary": "No checkpoint name was resolved"}
    if checkpoint_dir is None:
        return {
            "status": "not_requested",
            "checkpoint_name": checkpoint_name,
            "summary": "No checkpoint directory was provided for comparison",
        }
    path = Path(checkpoint_dir) / f"session_{session_id}_{checkpoint_name}.json"
    if not path.exists():
        return {
            "status": "missing",
            "checkpoint_name": checkpoint_name,
            "summary": f"Checkpoint artifact was not found: {checkpoint_name}",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unreadable",
            "checkpoint_name": checkpoint_name,
            "summary": str(exc),
        }

    mismatches: List[str] = []
    checkpoint_step = payload.get("current_step_index")
    if checkpoint_step is None:
        checkpoint_step = (payload.get("orchestration_state") or {}).get(
            "current_step_index"
        )
    if checkpoint_step is not None and int(checkpoint_step or 0) != int(
        state.get("current_step_index") or 0
    ):
        mismatches.append("current_step_index")

    checkpoint_status = (payload.get("orchestration_state") or {}).get("status")
    if (
        checkpoint_status
        and state.get("status")
        and checkpoint_status != state.get("status")
    ):
        mismatches.append("status")

    checkpoint_plan = (payload.get("orchestration_state") or {}).get("plan") or []
    if state.get("plan_step_count") is not None and len(checkpoint_plan) != int(
        state.get("plan_step_count") or 0
    ):
        mismatches.append("plan_shape")

    if not mismatches:
        status = "match"
    elif len(mismatches) <= 1:
        status = "partial_match"
    else:
        status = "conflict"
    return {
        "status": status,
        "checkpoint_name": checkpoint_name,
        "mismatches": mismatches,
        "summary": (
            "Checkpoint payload matches reconstructed state"
            if not mismatches
            else "Checkpoint payload differs from reconstructed state"
        ),
    }


def _workspace_evidence_status(
    *,
    project_dir: Any,
    state: Dict[str, Any],
    compare_workspace: bool,
) -> Dict[str, Any]:
    if not compare_workspace:
        return {"status": "not_requested"}
    current_hash = _compute_workspace_hash(project_dir)
    known_hashes = list(state.get("workspace_hashes") or [])
    if current_hash is None or not known_hashes:
        return {
            "status": "insufficient_evidence",
            "current_workspace_hash": current_hash,
            "known_workspace_hash": known_hashes[-1] if known_hashes else None,
        }
    latest_hash = known_hashes[-1]
    return {
        "status": (
            "hash_matches_snapshot"
            if current_hash == latest_hash
            else "hash_differs_from_snapshot"
        ),
        "current_workspace_hash": current_hash,
        "known_workspace_hash": latest_hash,
    }


def _build_drift_findings(
    *,
    checkpoint_comparison: Optional[Dict[str, Any]],
    workspace_evidence: Dict[str, Any],
    integrity: Dict[str, Any],
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    checkpoint_status = (
        checkpoint_comparison.get("status") if checkpoint_comparison else None
    )
    if checkpoint_status in {"partial_match", "conflict", "missing", "unreadable"}:
        findings.append(
            {
                "type": f"checkpoint_{checkpoint_status}",
                "severity": "warning",
                "summary": checkpoint_comparison.get("summary"),
            }
        )

    workspace_status = workspace_evidence.get("status")
    if workspace_status in {"hash_differs_from_snapshot", "insufficient_evidence"}:
        findings.append(
            {
                "type": f"workspace_{workspace_status}",
                "severity": "info",
                "summary": "Workspace evidence is incomplete or differs",
            }
        )

    for finding in integrity.get("findings", []):
        finding_type = finding.get("type")
        if finding_type in {
            "malformed_jsonl",
            "event_type_ignored_by_reducer",
            "duplicate_event_id",
            "session_task_mismatch",
            "boundary_not_found",
        }:
            findings.append(
                {
                    "type": finding_type,
                    "severity": finding.get("severity", "info"),
                    "summary": finding.get("message") or finding_type,
                }
            )
    return findings


def _build_determinism(
    *,
    integrity: Dict[str, Any],
    workspace_evidence: Dict[str, Any],
    drift_findings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    confidence = integrity.get("confidence")
    workspace_status = workspace_evidence.get("status")
    workspace_reconstructable = workspace_status == "hash_matches_snapshot"
    artifact_gaps = 0
    notes: List[str] = []

    if workspace_status in {"not_requested", "insufficient_evidence"}:
        artifact_gaps += 1
        notes.append("workspace content is not reconstructable from replay evidence")
    elif workspace_status == "hash_differs_from_snapshot":
        artifact_gaps += 1
        notes.append("current workspace evidence differs from replay evidence")

    if integrity.get("unknown_event_types"):
        artifact_gaps += len(integrity.get("unknown_event_types") or [])
        notes.append("unknown event types were ignored by this reducer")

    if confidence == "failed":
        level = "failed"
    elif confidence == "low" or any(
        item.get("severity") == "warning" for item in drift_findings
    ):
        level = "degraded"
    elif artifact_gaps > 0 or confidence == "medium":
        level = "bounded"
    else:
        level = "strong"

    return {
        "level": level,
        "artifact_gaps": artifact_gaps,
        "workspace_reconstructable": workspace_reconstructable,
        "notes": notes[:5],
    }


def _update_step_index(
    state: Dict[str, Any], details: Dict[str, Any], *, advance: bool
) -> None:
    raw_index = details.get("current_step_index")
    if raw_index is None:
        raw_index = details.get("step_index")
    if raw_index is None and details.get("step_number") is not None:
        raw_index = max(0, int(details.get("step_number") or 1) - 1)
    if raw_index is None:
        return
    index = int(raw_index or 0)
    state["current_step_index"] = max(
        int(state.get("current_step_index") or 0),
        index + (1 if advance else 0),
    )


def _is_problem_event(event_type: str, details: Dict[str, Any]) -> bool:
    if event_type in FAILURE_LIKE_EVENT_TYPES:
        return True
    status = str(details.get("status") or "").lower()
    return status in PROBLEM_STATUSES


def _boundary_not_found(mode: str, requested: Any) -> Dict[str, Any]:
    return {
        "type": "boundary_not_found",
        "severity": "error",
        "message": f"Replay boundary {mode}={requested!r} could not be resolved",
    }


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _confidence_for_findings(
    findings: List[Dict[str, Any]],
    *,
    boundary_resolved: bool,
    event_count_applied: int,
) -> str:
    if not boundary_resolved or event_count_applied <= 0:
        return "failed"
    severities = {str(finding.get("severity") or "") for finding in findings}
    finding_types = {str(finding.get("type") or "") for finding in findings}
    if (
        "error" in severities
        or {
            "duplicate_event_id",
            "session_task_mismatch",
            "checkpoint_comparison",
        }
        & finding_types
    ):
        return "low"
    if findings:
        return "medium"
    return "high"
