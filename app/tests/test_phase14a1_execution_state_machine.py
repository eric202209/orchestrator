"""Phase 14A-1: Execution state machine enum and transition table tests.

Verifies:
- OrchestrationPhase values are unique and match expected strings
- TerminalReason values are unique
- Every non-terminal OrchestrationPhase appears in LEGAL_TRANSITIONS
- Every transition target is a valid OrchestrationPhase
- Terminal phases have no outgoing transitions in LEGAL_TRANSITIONS
- TERMINAL_PHASES matches the terminal members of OrchestrationPhase
"""

from app.services.orchestration.state.execution_states import (
    OrchestrationPhase,
    TerminalReason,
)
from app.services.orchestration.state.transition_table import (
    LEGAL_TRANSITIONS,
    TERMINAL_PHASES,
)


class TestOrchestrationPhaseEnum:
    def test_values_are_unique(self):
        values = [p.value for p in OrchestrationPhase]
        assert len(values) == len(set(values))

    def test_str_equality(self):
        assert OrchestrationPhase.PLANNING == "planning"
        assert OrchestrationPhase.PLANNING_REPAIR == "planning_repair"
        assert OrchestrationPhase.STEP_EXECUTING == "step_executing"
        assert OrchestrationPhase.DEBUG_REPAIR == "debug_repair"
        assert OrchestrationPhase.PLAN_REVISION == "plan_revision"
        assert OrchestrationPhase.AWAITING_INPUT == "awaiting_input"
        assert OrchestrationPhase.COMPLETION_VALIDATION == "completion_validation"
        assert OrchestrationPhase.VERIFICATION == "verification"
        assert OrchestrationPhase.COMPLETION_REPAIR == "completion_repair"
        assert OrchestrationPhase.DONE == "done"
        assert OrchestrationPhase.FAILED == "failed"
        assert OrchestrationPhase.CANCELLED == "cancelled"

    def test_is_str_subclass(self):
        for phase in OrchestrationPhase:
            assert isinstance(phase, str), f"{phase!r} must be str-compatible"

    def test_terminal_phases_defined(self):
        assert OrchestrationPhase.DONE in TERMINAL_PHASES
        assert OrchestrationPhase.FAILED in TERMINAL_PHASES
        assert OrchestrationPhase.CANCELLED in TERMINAL_PHASES


class TestTerminalReasonEnum:
    def test_values_are_unique(self):
        values = [r.value for r in TerminalReason]
        assert len(values) == len(set(values))

    def test_is_str_subclass(self):
        for reason in TerminalReason:
            assert isinstance(reason, str), f"{reason!r} must be str-compatible"

    def test_known_reason_values(self):
        assert TerminalReason.MAX_ATTEMPTS_REACHED == "max_attempts_reached"
        assert (
            TerminalReason.DEBUG_REPAIR_BUDGET_EXHAUSTED
            == "debug_repair_budget_exhausted"
        )
        assert TerminalReason.DEBUG_PARSE_ERROR == "debug_parse_error"
        assert TerminalReason.PLAN_REVISION_CAP_REACHED == "plan_revision_cap_reached"
        assert TerminalReason.OP_CONTRACT_VIOLATION == "op_contract_violation"
        assert (
            TerminalReason.WORKSPACE_ISOLATION_VIOLATION
            == "workspace_isolation_violation"
        )
        assert TerminalReason.MANUAL_REVIEW_REQUIRED == "manual_review_required"
        assert (
            TerminalReason.REASONING_ARTIFACT_GATE_FAILED
            == "reasoning_artifact_gate_failed"
        )
        assert (
            TerminalReason.REVISED_PLAN_VALIDATION_FAILED
            == "revised_plan_validation_failed"
        )
        assert (
            TerminalReason.COMPLETION_VALIDATION_FAILED
            == "completion_validation_failed"
        )
        assert TerminalReason.COMPLETION_REPAIR_FAILED == "completion_repair_failed"
        # Actual code values (differ from the names in the proposal):
        assert TerminalReason.COMPLETION_REPAIR_CHURN_LIMIT == "repair_churn_limit"
        assert (
            TerminalReason.COMPLETION_REPAIR_ATTEMPT_LIMIT
            == "repair_attempt_limit_reached"
        )
        assert (
            TerminalReason.COMPLETION_VERIFICATION_FAILED
            == "completion_verification_failed"
        )
        assert (
            TerminalReason.VERIFICATION_INTEGRITY_FAILED
            == "verification_integrity_failed"
        )
        assert TerminalReason.SESSION_STOPPED == "session_stopped"
        assert TerminalReason.SESSION_PAUSED == "session_paused"


class TestTransitionTable:
    def test_terminal_phases_frozenset(self):
        assert isinstance(TERMINAL_PHASES, frozenset)
        assert TERMINAL_PHASES == frozenset(
            {
                OrchestrationPhase.DONE,
                OrchestrationPhase.FAILED,
                OrchestrationPhase.CANCELLED,
            }
        )

    def test_legal_transitions_keys_are_orchestration_phases(self):
        for key in LEGAL_TRANSITIONS:
            assert isinstance(
                key, OrchestrationPhase
            ), f"Key {key!r} is not an OrchestrationPhase"

    def test_legal_transitions_values_are_orchestration_phases(self):
        for source, targets in LEGAL_TRANSITIONS.items():
            for target in targets:
                assert isinstance(
                    target, OrchestrationPhase
                ), f"Transition target {target!r} from {source!r} is not an OrchestrationPhase"

    def test_every_non_terminal_phase_has_outgoing_transitions(self):
        non_terminal = set(OrchestrationPhase) - TERMINAL_PHASES
        for phase in non_terminal:
            assert (
                phase in LEGAL_TRANSITIONS
            ), f"Non-terminal phase {phase!r} has no outgoing transitions in LEGAL_TRANSITIONS"

    def test_terminal_phases_not_in_transition_sources(self):
        for terminal in TERMINAL_PHASES:
            assert (
                terminal not in LEGAL_TRANSITIONS
            ), f"Terminal phase {terminal!r} should have no outgoing transitions"

    def test_all_targets_reachable_from_some_source(self):
        all_targets = {t for targets in LEGAL_TRANSITIONS.values() for t in targets}
        for target in all_targets:
            assert (
                target in OrchestrationPhase
            ), f"Transition target {target!r} is not a member of OrchestrationPhase"

    def test_completion_repair_can_reach_verification(self):
        assert (
            OrchestrationPhase.VERIFICATION
            in LEGAL_TRANSITIONS[OrchestrationPhase.COMPLETION_REPAIR]
        )

    def test_step_executing_can_reach_debug_repair(self):
        assert (
            OrchestrationPhase.DEBUG_REPAIR
            in LEGAL_TRANSITIONS[OrchestrationPhase.STEP_EXECUTING]
        )

    def test_planning_can_reach_step_executing(self):
        assert (
            OrchestrationPhase.STEP_EXECUTING
            in LEGAL_TRANSITIONS[OrchestrationPhase.PLANNING]
        )

    def test_all_non_terminal_phases_can_reach_failed_or_cancelled(self):
        non_terminal = set(OrchestrationPhase) - TERMINAL_PHASES
        for phase in non_terminal:
            targets = LEGAL_TRANSITIONS.get(phase, frozenset())
            can_terminate = bool(targets & TERMINAL_PHASES)
            assert (
                can_terminate
            ), f"Phase {phase!r} has no path to a terminal state in LEGAL_TRANSITIONS"
