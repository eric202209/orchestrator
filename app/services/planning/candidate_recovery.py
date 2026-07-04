"""Planning-owned runtime adapter for bounded Candidate Recovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.planning.candidate_planning_outcome import CandidatePlanningOutcome
from app.services.planning.candidate_selection_policy import select_candidate
from app.services.planning.plan_candidate import PlanCandidate
from app.services.planning.slot_merge_operator import SlotMergeInput, SlotMergeOperator


def stable_plan_hash(plan: Any) -> str:
    payload = json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def planning_failure_signature(reasons: list[str] | tuple[str, ...]) -> str:
    payload = " | ".join(str(reason or "").strip().lower() for reason in reasons)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CandidateRuntimeResult:
    outcome: CandidatePlanningOutcome
    selected_plan: Optional[list[dict[str, Any]]] = None
    selected_output_text: str = ""
    selected_verdict: Any = None
    audit_event_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def selected(self) -> bool:
        return self.outcome.outcome == "selected" and self.selected_plan is not None


@dataclass(frozen=True)
class CandidateRecoveryRequest:
    project_dir: Any
    session_id: int
    task_id: int
    original_plan: list[dict[str, Any]]
    original_output_text: str
    original_verdict: Any
    runtime_profile: str
    parent_event_id: Optional[str]
    generate_sibling: Callable[[], tuple[list[dict[str, Any]], str]]
    validate_candidate: Callable[[list[dict[str, Any]], str], Any]


@dataclass(frozen=True)
class SlotMergeCandidateRecoveryRequest:
    project_dir: Any
    session_id: int
    task_id: int
    parent_a_plan: list[dict[str, Any]]
    parent_a_output_text: str
    parent_a_verdict: Any
    parent_b_plan: list[dict[str, Any]]
    parent_b_output_text: str
    parent_b_verdict: Any
    runtime_profile: str
    parent_event_id: Optional[str]
    validate_candidate: Callable[[list[dict[str, Any]], str], Any]
    policy_version: str = "phase17h"


def _verdict_status(verdict: Any) -> str:
    status = str(getattr(verdict, "status", "") or "").strip()
    if status:
        return status
    if getattr(verdict, "accepted", False):
        return "accepted"
    if getattr(verdict, "warning", False):
        return "warning"
    if getattr(verdict, "repairable", False):
        return "repair_required"
    return "rejected"


def _verdict_reasons(verdict: Any) -> tuple[str, ...]:
    return tuple(str(reason) for reason in (getattr(verdict, "reasons", []) or []))


def _verdict_rule_ids(verdict: Any) -> tuple[str, ...]:
    rule_ids = getattr(verdict, "validator_rule_ids", None)
    if rule_ids is None:
        details = getattr(verdict, "details", {}) or {}
        if isinstance(details, Mapping):
            rule_ids = details.get("validator_rule_ids")
    return tuple(str(rule_id) for rule_id in (rule_ids or []) if str(rule_id).strip())


def _emit(
    *,
    request: Any,
    event_type: str,
    candidate: Optional[PlanCandidate] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> str:
    payload: dict[str, Any] = dict(details or {})
    if candidate is not None:
        payload.update(candidate.to_dict())
    event = append_orchestration_event(
        project_dir=request.project_dir,
        session_id=request.session_id,
        task_id=request.task_id,
        event_type=event_type,
        parent_event_id=request.parent_event_id,
        details=payload,
    )
    return str(event.get("event_id") or "")


def _candidate_from_verdict(
    *,
    candidate_id: str,
    parent_candidate_ids: tuple[str, ...] = (),
    operator: str,
    source_lineage: str,
    plan: list[dict[str, Any]],
    verdict: Any,
    failure_signature: str,
    runtime_profile: str,
) -> PlanCandidate:
    return PlanCandidate(
        candidate_id=candidate_id,
        parent_candidate_ids=parent_candidate_ids,
        operator=operator,
        source_lineage=source_lineage,
        artifact_hash=stable_plan_hash(plan),
        validator_status=_verdict_status(verdict),
        validator_reasons=_verdict_reasons(verdict),
        validator_rule_ids=_verdict_rule_ids(verdict),
        planning_failure_signature=failure_signature,
        runtime_profile=runtime_profile,
    )


def _select_runtime_result(
    *,
    candidates: list[PlanCandidate],
    plans_by_id: Mapping[str, list[dict[str, Any]]],
    output_text_by_id: Mapping[str, str],
    verdict_by_id: Mapping[str, Any],
    audit_event_ids: list[str],
    operator_sequence: tuple[str, ...],
) -> CandidateRuntimeResult:
    selected = select_candidate(candidates)
    if selected and selected.accepted:
        outcome = CandidatePlanningOutcome(
            selected_candidate=selected,
            candidate_count=len(candidates),
            operator_sequence=operator_sequence,
            outcome="selected",
            audit_event_ids=tuple(audit_event_ids),
        )
        return CandidateRuntimeResult(
            outcome=outcome,
            selected_plan=plans_by_id[selected.candidate_id],
            selected_output_text=output_text_by_id[selected.candidate_id],
            selected_verdict=verdict_by_id[selected.candidate_id],
            audit_event_ids=tuple(audit_event_ids),
        )

    outcome = CandidatePlanningOutcome(
        selected_candidate=None,
        candidate_count=len(candidates),
        operator_sequence=operator_sequence,
        outcome="exhausted",
        audit_event_ids=tuple(audit_event_ids),
    )
    return CandidateRuntimeResult(
        outcome=outcome, audit_event_ids=tuple(audit_event_ids)
    )


def execute_single_sibling_candidate_recovery(
    request: CandidateRecoveryRequest,
) -> CandidateRuntimeResult:
    """Generate one sibling candidate, validate both lineages, select one."""

    audit_event_ids: list[str] = []
    failure_signature = planning_failure_signature(
        _verdict_reasons(request.original_verdict)
    )
    original = _candidate_from_verdict(
        candidate_id="candidate-original",
        operator="original",
        source_lineage="original",
        plan=request.original_plan,
        verdict=request.original_verdict,
        failure_signature=failure_signature,
        runtime_profile=request.runtime_profile,
    )
    audit_event_ids.append(
        _emit(
            request=request,
            event_type=EventType.PLAN_CANDIDATE_CREATED,
            candidate=original,
        )
    )
    audit_event_ids.append(
        _emit(
            request=request,
            event_type=EventType.PLAN_CANDIDATE_VALIDATED,
            candidate=original,
        )
    )

    sibling_plan, sibling_output_text = request.generate_sibling()
    sibling_verdict = request.validate_candidate(sibling_plan, sibling_output_text)
    sibling = _candidate_from_verdict(
        candidate_id="candidate-sibling-1",
        parent_candidate_ids=(original.candidate_id,),
        operator="sibling_generation",
        source_lineage="sibling",
        plan=sibling_plan,
        verdict=sibling_verdict,
        failure_signature=failure_signature,
        runtime_profile=request.runtime_profile,
    )
    audit_event_ids.append(
        _emit(
            request=request,
            event_type=EventType.PLAN_CANDIDATE_CREATED,
            candidate=sibling,
        )
    )
    audit_event_ids.append(
        _emit(
            request=request,
            event_type=EventType.PLAN_CANDIDATE_VALIDATED,
            candidate=sibling,
        )
    )

    candidates = [original, sibling]
    selected = select_candidate(candidates)
    selected_plan = (
        request.original_plan
        if selected and selected.candidate_id == original.candidate_id
        else sibling_plan
    )
    selected_output_text = (
        request.original_output_text
        if selected and selected.candidate_id == original.candidate_id
        else sibling_output_text
    )
    selected_verdict = (
        request.original_verdict
        if selected and selected.candidate_id == original.candidate_id
        else sibling_verdict
    )

    if selected and selected.accepted:
        audit_event_ids.append(
            _emit(
                request=request,
                event_type=EventType.PLAN_CANDIDATE_SELECTED,
                candidate=selected,
            )
        )
        for candidate in candidates:
            if candidate.candidate_id != selected.candidate_id:
                audit_event_ids.append(
                    _emit(
                        request=request,
                        event_type=EventType.PLAN_CANDIDATE_REJECTED,
                        candidate=candidate,
                        details={"reason": "lower_rank_than_selected"},
                    )
                )
        outcome = CandidatePlanningOutcome(
            selected_candidate=selected,
            candidate_count=len(candidates),
            operator_sequence=("original", "sibling_generation"),
            outcome="selected",
            audit_event_ids=tuple(audit_event_ids),
        )
        return CandidateRuntimeResult(
            outcome=outcome,
            selected_plan=selected_plan,
            selected_output_text=selected_output_text,
            selected_verdict=selected_verdict,
            audit_event_ids=tuple(audit_event_ids),
        )

    for candidate in candidates:
        audit_event_ids.append(
            _emit(
                request=request,
                event_type=EventType.PLAN_CANDIDATE_REJECTED,
                candidate=candidate,
                details={"reason": "validator_rejected"},
            )
        )
    audit_event_ids.append(
        _emit(
            request=request,
            event_type=EventType.PLAN_CANDIDATE_EXHAUSTED,
            details={
                "candidate_count": len(candidates),
                "planning_failure_signature": failure_signature,
            },
        )
    )
    outcome = CandidatePlanningOutcome(
        selected_candidate=None,
        candidate_count=len(candidates),
        operator_sequence=("original", "sibling_generation"),
        outcome="exhausted",
        audit_event_ids=tuple(audit_event_ids),
    )
    return CandidateRuntimeResult(
        outcome=outcome,
        audit_event_ids=tuple(audit_event_ids),
    )


def execute_slot_merge_candidate_recovery(
    request: SlotMergeCandidateRecoveryRequest,
) -> CandidateRuntimeResult:
    """Merge two failed lineages into one candidate and select deterministically."""

    audit_event_ids: list[str] = []
    failure_signature = planning_failure_signature(
        _verdict_reasons(request.parent_a_verdict)
        + _verdict_reasons(request.parent_b_verdict)
    )
    parent_a = _candidate_from_verdict(
        candidate_id="candidate-original",
        operator="original",
        source_lineage="original",
        plan=request.parent_a_plan,
        verdict=request.parent_a_verdict,
        failure_signature=failure_signature,
        runtime_profile=request.runtime_profile,
    )
    parent_b = _candidate_from_verdict(
        candidate_id="candidate-repair",
        operator="repair_mutation",
        source_lineage="repair",
        plan=request.parent_b_plan,
        verdict=request.parent_b_verdict,
        failure_signature=failure_signature,
        runtime_profile=request.runtime_profile,
    )
    for parent in (parent_a, parent_b):
        audit_event_ids.append(
            _emit(
                request=request,
                event_type=EventType.PLAN_CANDIDATE_CREATED,
                candidate=parent,
            )
        )
        audit_event_ids.append(
            _emit(
                request=request,
                event_type=EventType.PLAN_CANDIDATE_VALIDATED,
                candidate=parent,
            )
        )

    merge_result = SlotMergeOperator().merge(
        SlotMergeInput(
            parent_a_plan=request.parent_a_plan,
            parent_b_plan=request.parent_b_plan,
            parent_a_reasons=_verdict_reasons(request.parent_a_verdict),
            parent_b_reasons=_verdict_reasons(request.parent_b_verdict),
        )
    )
    merged_output_text = json.dumps(merge_result.merged_plan)
    merged_verdict = request.validate_candidate(
        merge_result.merged_plan, merged_output_text
    )
    merged = _candidate_from_verdict(
        candidate_id=merge_result.merged_candidate_id,
        parent_candidate_ids=merge_result.parent_candidate_ids,
        operator=merge_result.operator,
        source_lineage="slot_merge",
        plan=merge_result.merged_plan,
        verdict=merged_verdict,
        failure_signature=failure_signature,
        runtime_profile=request.runtime_profile,
    )
    audit_event_ids.append(
        _emit(
            request=request,
            event_type=EventType.PLAN_SLOT_MERGED,
            candidate=merged,
            details={
                "parent_candidate_ids": list(merge_result.parent_candidate_ids),
                "merged_candidate_id": merged.candidate_id,
                "failure_signature": failure_signature,
                "policy_version": request.policy_version,
            },
        )
    )
    audit_event_ids.append(
        _emit(
            request=request,
            event_type=EventType.PLAN_CANDIDATE_CREATED,
            candidate=merged,
        )
    )
    audit_event_ids.append(
        _emit(
            request=request,
            event_type=EventType.PLAN_CANDIDATE_VALIDATED,
            candidate=merged,
        )
    )

    candidates = [parent_a, merged]
    selected = select_candidate(candidates)
    if selected and selected.accepted:
        audit_event_ids.append(
            _emit(
                request=request,
                event_type=EventType.PLAN_CANDIDATE_SELECTED,
                candidate=selected,
            )
        )
        for candidate in candidates:
            if candidate.candidate_id != selected.candidate_id:
                audit_event_ids.append(
                    _emit(
                        request=request,
                        event_type=EventType.PLAN_CANDIDATE_REJECTED,
                        candidate=candidate,
                        details={"reason": "lower_rank_than_selected"},
                    )
                )
    else:
        for candidate in candidates:
            audit_event_ids.append(
                _emit(
                    request=request,
                    event_type=EventType.PLAN_CANDIDATE_REJECTED,
                    candidate=candidate,
                    details={"reason": "validator_rejected"},
                )
            )
        audit_event_ids.append(
            _emit(
                request=request,
                event_type=EventType.PLAN_CANDIDATE_EXHAUSTED,
                details={
                    "candidate_count": len(candidates),
                    "planning_failure_signature": failure_signature,
                },
            )
        )

    return _select_runtime_result(
        candidates=candidates,
        plans_by_id={
            parent_a.candidate_id: request.parent_a_plan,
            merged.candidate_id: merge_result.merged_plan,
        },
        output_text_by_id={
            parent_a.candidate_id: request.parent_a_output_text,
            merged.candidate_id: merged_output_text,
        },
        verdict_by_id={
            parent_a.candidate_id: request.parent_a_verdict,
            merged.candidate_id: merged_verdict,
        },
        audit_event_ids=audit_event_ids,
        operator_sequence=("original", "repair_mutation", "slot_merge"),
    )
