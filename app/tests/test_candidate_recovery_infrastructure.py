"""Phase 17F: registry placeholder and event vocabulary tests."""

from __future__ import annotations

from app.services.orchestration.events.event_types import EventType, is_known_event_type
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_outcome import RecoveryOutcome
from app.services.orchestration.recovery.recovery_policy import (
    STRATEGY_CANDIDATE_PLANNING,
    PolicyTable,
)
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.orchestration.prompt_templates import OrchestrationState


def _make_evidence() -> ExecutionRecoveryEvidence:
    return ExecutionRecoveryEvidence(
        task_title="Task",
        task_description="Do work",
        failed_command="planner",
        exit_code=1,
        stdout_excerpt="",
        stderr_excerpt="validation failed",
        traceback_excerpt="",
        failure_class="planning_validation_failed",
    )


def _make_context(tmp_path) -> RecoveryContext:
    return RecoveryContext(
        project_dir=tmp_path,
        session_id=17,
        task_id=19,
        evidence=_make_evidence(),
        orchestration_state=OrchestrationState(
            session_id="candidate", task_description="candidate infra"
        ),
        scope="planning",
    )


def test_candidate_planning_strategy_policy_entry_is_placeholder():
    rule = PolicyTable.lookup("planning_validation_failed")

    assert rule.strategy == STRATEGY_CANDIDATE_PLANNING
    assert rule.consumes_budget is False


def test_candidate_planning_event_types_are_known():
    expected = [
        EventType.PLAN_CANDIDATE_CREATED,
        EventType.PLAN_CANDIDATE_VALIDATED,
        EventType.PLAN_CANDIDATE_SELECTED,
        EventType.PLAN_CANDIDATE_REJECTED,
        EventType.PLAN_SLOT_MERGED,
        EventType.PLAN_CANDIDATE_EXHAUSTED,
    ]

    assert expected == [
        "plan_candidate_created",
        "plan_candidate_validated",
        "plan_candidate_selected",
        "plan_candidate_rejected",
        "plan_slot_merged",
        "plan_candidate_exhausted",
    ]
    for event_type in expected:
        assert is_known_event_type(event_type)


def test_registry_route_recognizes_candidate_planning_strategy(tmp_path):
    from app.services.orchestration.recovery.failure_event import make_failure_event

    decision = RecoveryStrategyRegistry.route(
        make_failure_event(
            failure_class="planning_validation_failed",
            source="planning",
            error_message="validator rejected plan",
            session_id=17,
            task_id=19,
        ),
        project_dir=tmp_path,
        session_id=17,
        task_id=19,
    )

    assert decision.strategy == STRATEGY_CANDIDATE_PLANNING


def test_registry_candidate_planning_placeholder_returns_skipped(tmp_path):
    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=_make_context(tmp_path)
    )

    assert isinstance(outcome, RecoveryOutcome)
    assert outcome.succeeded is False
    assert outcome.resumed_execution is False
    assert outcome.strategy_name == STRATEGY_CANDIDATE_PLANNING
    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "not_enabled"
    assert outcome["candidate_outcome"]["outcome"] == "skipped"
    assert outcome["candidate_outcome"]["candidate_count"] == 0


def test_registry_candidate_planning_placeholder_emits_routing_event(tmp_path):
    RecoveryStrategyRegistry.execute_candidate_planning(context=_make_context(tmp_path))

    events = read_orchestration_events(
        tmp_path,
        session_id=17,
        task_id=19,
        event_type_filter=EventType.RECOVERY_DECISION_ROUTED,
    )

    assert len(events) == 1
    assert events[0]["details"]["strategy"] == STRATEGY_CANDIDATE_PLANNING
    assert events[0]["details"]["status"] == "skipped"
    assert events[0]["details"]["reason"] == "not_enabled"
