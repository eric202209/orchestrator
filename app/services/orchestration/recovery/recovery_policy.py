"""Phase 17A/17B: Versioned recovery policy table.

Maps failure_class strings to deterministic recovery strategies.
Day-one routing mirrors Phase 16 exactly — no new strategies, no policy changes.
The only new routing is wrapper_timeout_noise → annotate_and_continue,
a validated maintenance finding.
"""

from __future__ import annotations

from dataclasses import dataclass

POLICY_VERSION = "v2"

# Strategy names
STRATEGY_EXISTING_RECOVERY = "existing_recovery"
STRATEGY_ANNOTATE_AND_CONTINUE = "annotate_and_continue"
STRATEGY_RETRY_WITH_REFLECTION = "retry_with_reflection"
STRATEGY_TERMINAL = "terminal"


@dataclass(frozen=True)
class PolicyRule:
    failure_class: str
    strategy: str
    consumes_budget: bool
    note: str


# Mirrors ELIGIBLE_RECOVERY_FAILURE_CLASSES in execution_recovery_service.py.
# These failure classes are routed to the existing ExecutionRecoveryService pipeline.
_EXISTING_RECOVERY_CLASSES = (
    "pytest_failure",
    "import_error",
    "module_not_found",
    "runtime_assertion_failure",
    "completion_validation_failed",
    "missing_dependency",
    "syntax_error",
    "source_step_validation",
    "missing_requested_symbol",
)

RECOVERY_POLICY_V1: dict[str, PolicyRule] = {
    # ── Existing recovery-eligible classes ───────────────────────────────────
    **{
        fc: PolicyRule(
            failure_class=fc,
            strategy=STRATEGY_EXISTING_RECOVERY,
            consumes_budget=True,
            note="eligible for ExecutionRecoveryService (Phase 13B)",
        )
        for fc in _EXISTING_RECOVERY_CLASSES
    },
    # ── New in 17A: wrapper timeout noise ────────────────────────────────────
    "wrapper_timeout_noise": PolicyRule(
        failure_class="wrapper_timeout_noise",
        strategy=STRATEGY_ANNOTATE_AND_CONTINUE,
        consumes_budget=False,
        note="validated maintenance finding: timeout after task completion is noise",
    ),
    # ── Terminal classes ──────────────────────────────────────────────────────
    "bounded_debug_repair_timeout": PolicyRule(
        failure_class="bounded_debug_repair_timeout",
        strategy=STRATEGY_TERMINAL,
        consumes_budget=False,
        note="bounded debug repair timeout — no recovery",
    ),
    "planning_lock_contention": PolicyRule(
        failure_class="planning_lock_contention",
        strategy=STRATEGY_TERMINAL,
        consumes_budget=False,
        note="planning lock contention — no recovery",
    ),
    "project_mutation_lock_conflict": PolicyRule(
        failure_class="project_mutation_lock_conflict",
        strategy=STRATEGY_TERMINAL,
        consumes_budget=False,
        note="project mutation lock conflict — no recovery",
    ),
    "planning_timeout": PolicyRule(
        failure_class="planning_timeout",
        strategy=STRATEGY_TERMINAL,
        consumes_budget=False,
        note="planning timeout — no recovery",
    ),
    "execution_timeout": PolicyRule(
        failure_class="execution_timeout",
        strategy=STRATEGY_TERMINAL,
        consumes_budget=False,
        note="execution timeout — no recovery",
    ),
    # ── New in 17B: reflection retry ─────────────────────────────────────────
    "debug_parse_error": PolicyRule(
        failure_class="debug_parse_error",
        strategy=STRATEGY_RETRY_WITH_REFLECTION,
        consumes_budget=False,
        note="17B: one reflection retry before terminal (disabled on low_resource)",
    ),
    "unknown_failure": PolicyRule(
        failure_class="unknown_failure",
        strategy=STRATEGY_RETRY_WITH_REFLECTION,
        consumes_budget=False,
        note="17B: one reflection retry before terminal (disabled on low_resource)",
    ),
}

_DEFAULT_TERMINAL_RULE = PolicyRule(
    failure_class="__default__",
    strategy=STRATEGY_TERMINAL,
    consumes_budget=False,
    note="default: unrecognised failure class",
)


class PolicyTable:
    """Versioned policy table lookup."""

    VERSION: str = POLICY_VERSION

    @staticmethod
    def lookup(failure_class: str) -> PolicyRule:
        """Return the PolicyRule for a failure_class, defaulting to terminal."""
        return RECOVERY_POLICY_V1.get(failure_class, _DEFAULT_TERMINAL_RULE)
