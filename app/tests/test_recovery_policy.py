"""Phase 17A: Tests for the recovery policy table."""

from __future__ import annotations

from app.services.orchestration.recovery.recovery_policy import (
    RECOVERY_POLICY_V1,
    STRATEGY_ANNOTATE_AND_CONTINUE,
    STRATEGY_EXISTING_RECOVERY,
    STRATEGY_RETRY_WITH_REFLECTION,
    STRATEGY_TERMINAL,
    PolicyTable,
    _DEFAULT_TERMINAL_RULE,
)
from app.services.orchestration.recovery.execution_recovery_service import (
    ELIGIBLE_RECOVERY_FAILURE_CLASSES,
)


def test_wrapper_timeout_noise_routes_to_annotate_and_continue():
    rule = PolicyTable.lookup("wrapper_timeout_noise")
    assert rule.strategy == STRATEGY_ANNOTATE_AND_CONTINUE


def test_wrapper_timeout_noise_consumes_no_budget():
    rule = PolicyTable.lookup("wrapper_timeout_noise")
    assert rule.consumes_budget is False


def test_all_eligible_recovery_classes_route_to_existing_recovery():
    for fc in ELIGIBLE_RECOVERY_FAILURE_CLASSES:
        rule = PolicyTable.lookup(fc)
        assert (
            rule.strategy == STRATEGY_EXISTING_RECOVERY
        ), f"{fc!r} should map to existing_recovery, got {rule.strategy!r}"


def test_all_eligible_recovery_classes_consume_budget():
    for fc in ELIGIBLE_RECOVERY_FAILURE_CLASSES:
        rule = PolicyTable.lookup(fc)
        assert rule.consumes_budget is True, f"{fc!r} should consume budget"


def test_unknown_failure_routes_to_retry_with_reflection():
    rule = PolicyTable.lookup("unknown_failure")
    assert rule.strategy == STRATEGY_RETRY_WITH_REFLECTION


def test_unrecognised_class_returns_default_terminal_rule():
    rule = PolicyTable.lookup("this_class_does_not_exist_in_v1")
    assert rule.strategy == STRATEGY_TERMINAL
    assert rule is _DEFAULT_TERMINAL_RULE


def test_bounded_debug_repair_timeout_is_terminal():
    rule = PolicyTable.lookup("bounded_debug_repair_timeout")
    assert rule.strategy == STRATEGY_TERMINAL


def test_planning_lock_contention_is_terminal():
    rule = PolicyTable.lookup("planning_lock_contention")
    assert rule.strategy == STRATEGY_TERMINAL


def test_project_mutation_lock_conflict_is_terminal():
    rule = PolicyTable.lookup("project_mutation_lock_conflict")
    assert rule.strategy == STRATEGY_TERMINAL


def test_policy_table_version_is_string():
    assert isinstance(PolicyTable.VERSION, str)
    assert len(PolicyTable.VERSION) > 0


def test_policy_v1_contains_all_eligible_recovery_classes():
    for fc in ELIGIBLE_RECOVERY_FAILURE_CLASSES:
        assert fc in RECOVERY_POLICY_V1, f"{fc!r} missing from RECOVERY_POLICY_V1"


def test_policy_rule_failure_class_matches_key():
    for key, rule in RECOVERY_POLICY_V1.items():
        assert (
            rule.failure_class == key
        ), f"PolicyRule.failure_class mismatch for key {key!r}"
