from app.services.orchestration.phases.planning_support import (
    _PlanningRetryState,
    _record_repair_root_cause,
    _root_cause_oscillation_details,
    _verifier_failures_decreased_materially,
)


def test_oscillation_policy_does_not_trigger_when_planning_repair_accepts():
    retry_state = _PlanningRetryState()
    _record_repair_root_cause(
        retry_state,
        root_cause="invalid_python",
        stage="planning_validation",
    )

    assert _root_cause_oscillation_details(retry_state, latest_progress=True) is None


def test_oscillation_policy_triggers_for_repeated_planning_root_switches():
    retry_state = _PlanningRetryState()
    _record_repair_root_cause(
        retry_state,
        root_cause="invalid_python",
        stage="planning_validation",
    )
    _record_repair_root_cause(
        retry_state,
        root_cause="missing_verification",
        stage="post_repair_validation",
    )
    _record_repair_root_cause(
        retry_state,
        root_cause="invalid_python",
        stage="post_repair_python_source_syntax_second_pass",
    )

    details = _root_cause_oscillation_details(retry_state)

    assert details is not None
    assert details["oscillation_detected"] is True
    assert details["cross_stage_convergence_class"] == "root_cause_oscillation"
    assert details["oscillation_action"] == "stop_repair_loop"
    assert details["reason"] == "root_cause_oscillation_no_progress"
    assert details["oscillation_root_causes"] == [
        "invalid_python",
        "missing_verification",
    ]
    assert details["oscillation_stage_sequence"] == [
        "planning_validation",
        "post_repair_validation",
        "post_repair_python_source_syntax_second_pass",
    ]


def test_oscillation_policy_triggers_after_accepted_planning_then_debug_regression():
    retry_state = _PlanningRetryState()
    _record_repair_root_cause(
        retry_state,
        root_cause="invalid_python",
        stage="planning_validation",
    )
    _record_repair_root_cause(
        retry_state,
        root_cause="source_api_regression",
        stage="debug_repair_failure",
    )

    details = _root_cause_oscillation_details(retry_state)

    assert details is not None
    assert details["oscillation_root_causes"] == [
        "invalid_python",
        "source_api_regression",
    ]


def test_oscillation_policy_does_not_trigger_when_verifier_improves():
    retry_state = _PlanningRetryState()
    _record_repair_root_cause(
        retry_state,
        root_cause="invalid_python",
        stage="planning_validation",
    )
    _record_repair_root_cause(
        retry_state,
        root_cause="missing_verification",
        stage="post_repair_validation",
    )

    assert _verifier_failures_decreased_materially(
        previous_failure_count=5,
        current_failure_count=2,
    )
    assert (
        _root_cause_oscillation_details(
            retry_state,
            latest_progress=True,
        )
        is None
    )


def test_oscillation_policy_requires_distinct_root_causes():
    retry_state = _PlanningRetryState()
    _record_repair_root_cause(
        retry_state,
        root_cause="invalid_python",
        stage="planning_validation",
    )
    _record_repair_root_cause(
        retry_state,
        root_cause="invalid_python",
        stage="post_repair_validation",
    )

    assert _root_cause_oscillation_details(retry_state) is None
