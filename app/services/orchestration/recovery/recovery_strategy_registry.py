"""Phase 17A/17B: Recovery Strategy Registry.

Routes every FailureEvent through the policy table and emits a deterministic
audit event for each recovery routing decision.

Phase 17A: emit RECOVERY_DECISION_ROUTED / RECOVERY_NOISE_ANNOTATED for every
routing decision. Handle annotate_and_continue in FailureCoordinator.

Phase 17B: retry_with_reflection routing with:
- Machine profile guard (skipped on low_resource / compact_local)
- Signature dedup (one reflection per unique failure signature per task run)
- ReflectionRetryStrategy execution before final terminal routing
- Four new reflection-specific audit event types
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Callable, Optional

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.execution_recovery_service import (
    ExecutionRecoveryService,
)
from app.services.orchestration.recovery.failure_event import FailureEvent
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.reflection_evidence import ReflectionEvidence
from app.services.orchestration.recovery.recovery_lifecycle import RecoveryLifecycle
from app.services.orchestration.recovery.recovery_outcome import RecoveryOutcome
from app.services.orchestration.recovery.recovery_policy import (
    STRATEGY_CANDIDATE_PLANNING,
    PolicyRule,
    PolicyTable,
)
from app.services.planning.candidate_operator_policy import (
    evaluate_candidate_operator_policy,
)
from app.services.planning.candidate_planning_outcome import CandidatePlanningOutcome
from app.services.orchestration.recovery.strategies.reflection_retry import (
    RecoveryResult,
    ReflectionRetryStrategy,
)

logger = logging.getLogger(__name__)

# Machine profiles where reflection retry is disabled (Machine C).
_LOW_RESOURCE_PROFILES = frozenset({"low_resource", "compact_local"})
_REFLECTION_EVIDENCE_FAILURE_CLASSES = frozenset({"unknown_failure"})
# ── Machine profile helpers ───────────────────────────────────────────────────


def _reflection_allowed() -> bool:
    """Return True when the current RUNTIME_PROFILE allows reflection retries."""
    try:
        from app.config import settings

        return settings.RUNTIME_PROFILE not in _LOW_RESOURCE_PROFILES
    except Exception:
        return True  # fail open


# ── Per-task signature dedup ──────────────────────────────────────────────────


def _get_reflection_signatures(orchestration_state: Any) -> frozenset:
    raw = getattr(orchestration_state, "_reflection_attempted_signatures", None)
    return frozenset(raw) if raw else frozenset()


def _add_reflection_signature(orchestration_state: Any, sig: str) -> None:
    existing = _get_reflection_signatures(orchestration_state)
    try:
        setattr(
            orchestration_state,
            "_reflection_attempted_signatures",
            existing | {sig},
        )
    except Exception:
        pass


def _get_candidate_signatures(orchestration_state: Any) -> frozenset:
    raw = getattr(orchestration_state, "_candidate_planning_signatures", None)
    return frozenset(raw) if raw else frozenset()


def _add_candidate_signature(orchestration_state: Any, sig: str) -> None:
    existing = _get_candidate_signatures(orchestration_state)
    try:
        setattr(
            orchestration_state,
            "_candidate_planning_signatures",
            existing | {sig},
        )
    except Exception:
        pass


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class RecoveryDecision:
    """The output of the registry: which strategy was selected and why."""

    failure_event: FailureEvent
    strategy: str  # effective strategy after all guards (may differ from policy)
    policy_rule: PolicyRule
    policy_version: str
    signature_hash: Optional[str]
    budget_state: Optional[dict]  # placeholder; unused in 17A/17B
    timestamp: str
    reflection_result: Optional[RecoveryResult] = None


# ── Internal event helpers ────────────────────────────────────────────────────


def _emit(
    event_type: str,
    details: dict,
    project_dir: Any,
    session_id: Optional[int],
    task_id: Optional[int],
) -> None:
    if project_dir is None or session_id is None or task_id is None:
        return
    try:
        from app.services.orchestration.state.persistence import (
            append_orchestration_event,
        )

        append_orchestration_event(
            project_dir=project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=event_type,
            details=details,
        )
    except Exception as exc:
        logger.debug("[17B] event emit failed (%s): %s", event_type, exc)


def _emit_decision_event(
    decision: RecoveryDecision,
    project_dir: Any,
    session_id: Optional[int],
    task_id: Optional[int],
) -> None:
    if decision.strategy == "annotate_and_continue":
        event_type = EventType.RECOVERY_NOISE_ANNOTATED
    else:
        event_type = EventType.RECOVERY_DECISION_ROUTED

    _emit(
        event_type,
        {
            "failure_class": decision.failure_event.failure_class,
            "strategy": decision.strategy,
            "policy_version": decision.policy_version,
            "signature_hash": decision.signature_hash,
            "source": decision.failure_event.source,
            "exception_type": decision.failure_event.exception_type,
            "orchestration_status": decision.failure_event.orchestration_status,
            "session_id": session_id,
            "task_id": task_id,
            "timestamp": decision.timestamp,
        },
        project_dir,
        session_id,
        task_id,
    )


# ── Reflection routing ────────────────────────────────────────────────────────


def _route_reflection(
    *,
    failure_event: FailureEvent,
    project_dir: Any,
    session_id: Optional[int],
    task_id: Optional[int],
    orchestration_state: Any,
    llm_callable: Optional[Callable[[str], str]],
) -> Optional[RecoveryResult]:
    """Execute reflection retry with guards. Returns RecoveryResult or None (skipped).

    Always results in terminal routing after — reflection is an enrichment step,
    not a recovery that prevents task failure. Emits the four 17B audit events.
    """
    failure_class = failure_event.failure_class
    sig = failure_event.signature_hash or failure_class

    base_details = {
        "failure_class": failure_class,
        "strategy": "retry_with_reflection",
        "signature_hash": sig,
        "machine_profile": _get_runtime_profile(),
        "session_id": session_id,
        "task_id": task_id,
    }

    # Guard: machine profile
    if not _reflection_allowed():
        _emit(
            EventType.RECOVERY_REFLECTION_SKIPPED,
            {**base_details, "skip_reason": "low_resource_profile"},
            project_dir,
            session_id,
            task_id,
        )
        logger.info(
            "[17B] reflection skipped: low_resource profile (failure_class=%s)",
            failure_class,
        )
        return None

    # Guard: signature dedup
    seen = _get_reflection_signatures(orchestration_state)
    dedup_key = f"{failure_class}:retry_with_reflection:{sig}"
    if dedup_key in seen:
        _emit(
            EventType.RECOVERY_REFLECTION_SKIPPED,
            {
                **base_details,
                "skip_reason": "signature_already_attempted",
                "dedup_key": dedup_key,
            },
            project_dir,
            session_id,
            task_id,
        )
        logger.info(
            "[17B] reflection skipped: signature already attempted (failure_class=%s)",
            failure_class,
        )
        return None

    # Record signature before executing (prevents re-entry even on crash)
    _add_reflection_signature(orchestration_state, dedup_key)

    # Emit STARTED
    _emit(
        EventType.RECOVERY_REFLECTION_STARTED,
        {**base_details, "retry_attempt": 1},
        project_dir,
        session_id,
        task_id,
    )

    try:
        result = ReflectionRetryStrategy.execute(
            failure_event=failure_event,
            llm_callable=llm_callable,
            orchestration_state=orchestration_state,
        )
    except Exception as exc:
        logger.warning("[17B] ReflectionRetryStrategy.execute raised: %s", exc)
        _emit(
            EventType.RECOVERY_REFLECTION_FAILED,
            {
                **base_details,
                "retry_attempt": 1,
                "outcome": "strategy_raised",
                "error": str(exc)[:400],
            },
            project_dir,
            session_id,
            task_id,
        )
        return None

    # Emit COMPLETED or FAILED
    if result.success:
        _emit(
            EventType.RECOVERY_REFLECTION_COMPLETED,
            {
                **base_details,
                "retry_attempt": 1,
                "outcome": result.outcome,
                "duration_ms": result.duration_ms,
                "llm_output_chars": len(result.llm_output or ""),
            },
            project_dir,
            session_id,
            task_id,
        )
    else:
        _emit(
            EventType.RECOVERY_REFLECTION_FAILED,
            {
                **base_details,
                "retry_attempt": 1,
                "outcome": result.outcome,
                "duration_ms": result.duration_ms,
                "error": result.error,
            },
            project_dir,
            session_id,
            task_id,
        )

    return result


def _get_runtime_profile() -> str:
    try:
        from app.config import settings

        return settings.RUNTIME_PROFILE
    except Exception:
        return "unknown"


# ── Registry ──────────────────────────────────────────────────────────────────


class RecoveryStrategyRegistry:
    """Routes a FailureEvent to a strategy via the PolicyTable."""

    @staticmethod
    def route(
        failure_event: FailureEvent,
        *,
        project_dir: Any = None,
        session_id: Optional[int] = None,
        task_id: Optional[int] = None,
        orchestration_state: Any = None,
        llm_callable: Optional[Callable[[str], str]] = None,
    ) -> RecoveryDecision:
        """Look up the policy rule, execute reflection if applicable, emit audit event.

        retry_with_reflection always results in effective strategy="terminal":
        reflection is an enrichment / annotation step, not a task-saving recovery.
        Falls back safely to terminal on any error.
        """
        rule = PolicyTable.lookup(failure_event.failure_class)
        effective_strategy = rule.strategy
        reflection_result: Optional[RecoveryResult] = None

        if rule.strategy == "retry_with_reflection":
            try:
                reflection_result = _route_reflection(
                    failure_event=failure_event,
                    project_dir=project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    orchestration_state=orchestration_state,
                    llm_callable=llm_callable,
                )
            except Exception as exc:
                logger.warning("[17B] _route_reflection raised unexpectedly: %s", exc)
            # Always fall through to terminal after reflection
            effective_strategy = "terminal"

        decision = RecoveryDecision(
            failure_event=failure_event,
            strategy=effective_strategy,
            policy_rule=rule,
            policy_version=PolicyTable.VERSION,
            signature_hash=failure_event.signature_hash,
            budget_state=None,
            timestamp=datetime.now(UTC).isoformat(),
            reflection_result=reflection_result,
        )
        _emit_decision_event(decision, project_dir, session_id, task_id)
        logger.debug(
            "[17A/17B] recovery routed: failure_class=%s policy=%s effective=%s",
            failure_event.failure_class,
            rule.strategy,
            effective_strategy,
        )
        return decision

    @staticmethod
    def execute_recovery(
        *,
        context: RecoveryContext,
    ) -> RecoveryOutcome:
        """Phase 17C-3: registry entry point for active execution recovery.

        Emits a routing audit event, then delegates to
        ExecutionRecoveryService.attempt_recovery() exactly once with context
        values unpacked unchanged. ExecutionRecoveryService owns all recovery
        behavior, budget, and its own EXECUTION_RECOVERY_* audit events; this
        method owns the canonical registry-level lifecycle.
        """
        started_at = time.perf_counter()
        strategy_name = "execution_recovery"
        context = RecoveryStrategyRegistry._attach_reflection_evidence(context)
        _emit(
            EventType.RECOVERY_DECISION_ROUTED,
            {
                "failure_class": getattr(context.evidence, "failure_class", None),
                "strategy": strategy_name,
                "scope": context.scope,
                "step_index": context.step_index,
                "session_id": context.session_id,
                "task_id": context.task_id,
                "timestamp": datetime.now(UTC).isoformat(),
            },
            context.project_dir,
            context.session_id,
            context.task_id,
        )
        lifecycle = RecoveryLifecycle(context=context, strategy_name=strategy_name)
        lifecycle.started()
        try:
            result = ExecutionRecoveryService.attempt_recovery(
                project_dir=context.project_dir,
                session_id=context.session_id,
                task_id=context.task_id,
                evidence=context.evidence,
                orchestration_state=context.orchestration_state,
                scope=context.scope,
                step_index=context.step_index,
                parent_event_id=context.parent_event_id,
                llm_callable=context.llm_callable,
                command_runner=context.command_runner,
                validator_callable=context.validator_callable,
                reflection_evidence=context.reflection_result,
            )
        except Exception as exc:
            lifecycle.failed(error=str(exc))
            raise

        succeeded = result.get("status") == "success"
        if succeeded:
            lifecycle.completed(result=result)
            lifecycle.resumed(result=result)
        else:
            lifecycle.failed(result=result)

        return RecoveryOutcome(
            succeeded=succeeded,
            resumed_execution=succeeded,
            strategy_name=strategy_name,
            duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
            failure_class=str(getattr(context.evidence, "failure_class", "") or ""),
            recovery_context=context,
            audit_event_ids=lifecycle.audit_event_ids,
            strategy_result=result,
        )

    @staticmethod
    def execute_candidate_planning(
        *,
        context: RecoveryContext,
    ) -> RecoveryOutcome:
        """Registry-owned Candidate Recovery orchestration."""
        started_at = time.perf_counter()
        strategy_name = STRATEGY_CANDIDATE_PLANNING
        failure_class = str(getattr(context.evidence, "failure_class", "") or "")
        metadata = dict(context.recovery_metadata or {})
        signature = str(
            metadata.get("planning_failure_signature")
            or getattr(context.evidence, "validator_rejection_reason", "")
            or failure_class
        )

        def _skipped(reason: str) -> RecoveryOutcome:
            _emit(
                EventType.RECOVERY_DECISION_ROUTED,
                {
                    "failure_class": failure_class,
                    "strategy": strategy_name,
                    "scope": context.scope,
                    "step_index": context.step_index,
                    "session_id": context.session_id,
                    "task_id": context.task_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "status": "skipped",
                    "reason": reason,
                    "signature_hash": signature,
                },
                context.project_dir,
                context.session_id,
                context.task_id,
            )
            outcome = CandidatePlanningOutcome.skipped(reason=reason)
            return RecoveryOutcome(
                succeeded=False,
                resumed_execution=False,
                strategy_name=strategy_name,
                duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
                failure_class=failure_class,
                recovery_context=context,
                audit_event_ids=(),
                strategy_result={
                    "status": "skipped",
                    "reason": reason,
                    "candidate_outcome": outcome.to_dict(),
                },
            )

        try:
            from app.config import settings

            enabled = bool(settings.CANDIDATE_RECOVERY_ENABLED)
        except Exception:
            enabled = False
        if context.scope != "planning" or failure_class != "planning_validation_failed":
            return _skipped("ineligible_trigger")
        candidate_operator = str(metadata.get("candidate_operator") or "")
        try:
            slot_merge_enabled = bool(settings.CANDIDATE_SLOT_MERGE_ENABLED)
        except Exception:
            slot_merge_enabled = False
        policy_decision = evaluate_candidate_operator_policy(
            runtime_profile=context.runtime_profile,
            candidate_operator=candidate_operator,
            candidate_recovery_enabled=enabled,
            slot_merge_enabled=slot_merge_enabled,
        )
        if not policy_decision.allowed:
            return _skipped(policy_decision.reason)
        dedup_operator = candidate_operator or strategy_name
        dedup_key = f"{failure_class}:{strategy_name}:{dedup_operator}:{signature}"
        if dedup_key in _get_candidate_signatures(context.orchestration_state):
            return _skipped("signature_already_attempted")
        candidate_executor = metadata.get("candidate_executor")
        if not callable(candidate_executor):
            return _skipped("candidate_executor_missing")

        _add_candidate_signature(context.orchestration_state, dedup_key)
        _emit(
            EventType.RECOVERY_DECISION_ROUTED,
            {
                "failure_class": failure_class,
                "strategy": strategy_name,
                "scope": context.scope,
                "step_index": context.step_index,
                "session_id": context.session_id,
                "task_id": context.task_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "signature_hash": signature,
            },
            context.project_dir,
            context.session_id,
            context.task_id,
        )
        lifecycle = RecoveryLifecycle(context=context, strategy_name=strategy_name)
        lifecycle.started()
        try:
            candidate_result = candidate_executor()
        except Exception as exc:
            lifecycle.failed(error=str(exc))
            raise

        outcome = candidate_result.outcome
        succeeded = bool(getattr(candidate_result, "selected", False))
        if succeeded:
            lifecycle.completed(result=outcome.to_dict())
            lifecycle.resumed(result=outcome.to_dict())
            status = "success"
            reason = ""
        else:
            lifecycle.failed(result=outcome.to_dict())
            status = "failed"
            reason = "candidate_exhausted"

        result = {
            "status": status,
            "reason": reason,
            "candidate_outcome": outcome.to_dict(),
        }
        candidate_audit_ids = tuple(getattr(candidate_result, "audit_event_ids", ()))
        return RecoveryOutcome(
            succeeded=succeeded,
            resumed_execution=succeeded,
            strategy_name=strategy_name,
            duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
            failure_class=failure_class,
            recovery_context=context,
            audit_event_ids=tuple(lifecycle.audit_event_ids) + candidate_audit_ids,
            strategy_result=result,
        )

    @staticmethod
    def _attach_reflection_evidence(context: RecoveryContext) -> RecoveryContext:
        failure_class = str(getattr(context.evidence, "failure_class", "") or "")
        if failure_class not in _REFLECTION_EVIDENCE_FAILURE_CLASSES:
            if context.reflection_result is None:
                return context
            return replace(context, reflection_result=None)
        if context.reflection_result is None:
            return context

        reflection_evidence = ReflectionEvidence.from_reflection_result(
            context.reflection_result
        )
        if reflection_evidence is context.reflection_result:
            return context
        return replace(context, reflection_result=reflection_evidence)
