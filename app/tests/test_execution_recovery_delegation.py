"""Recovery delegation boundaries after candidate-repair consolidation."""

from app.services.orchestration.coordinators import completion_coordinator
from app.services.orchestration.phases import execution_loop
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)


def test_completion_coordinator_has_no_execution_recovery_authority() -> None:
    """Candidate findings may route only through Candidate Repair."""

    assert not hasattr(completion_coordinator, "ExecutionRecoveryService")
    assert not hasattr(completion_coordinator, "RecoveryStrategyRegistry")


def test_execution_loop_retains_its_protected_execution_recovery_authority() -> None:
    """Q-2 does not alter the protected execution path authority."""

    assert not hasattr(execution_loop, "ExecutionRecoveryService")
    assert execution_loop.RecoveryStrategyRegistry is RecoveryStrategyRegistry
