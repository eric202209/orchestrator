"""Phase 13B-S1: Bounded execution recovery service — skeleton.

LLM patch generation is DISABLED in this phase. The service provides:
  - Eligibility gating (budget, failure class, evidence, repeated-signature checks)
  - Audit event emission for every decision
  - A safe noop attempt_recovery() that never modifies workspace files

Production behavior change in S1: none beyond additional audit events emitted
near terminal failure paths.

When Phase 13B-full enables LLM patch generation, set _LLM_PATCH_GENERATION_ENABLED
to True and implement the patch-and-rerun logic inside attempt_recovery().
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional, Tuple

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)

logger = logging.getLogger(__name__)

# Hard budget: max recovery attempts per task run.
RECOVERY_BUDGET: int = 2

# Failure classes eligible for execution recovery.
# Mirrors ELIGIBLE_DEBUG_FAILURE_CLASSES from diagnostics/debug_feedback.py
# and adds missing_requested_symbol (surfaced by 10K-c, not covered by Phase 7F).
ELIGIBLE_RECOVERY_FAILURE_CLASSES: frozenset = frozenset(
    {
        "pytest_failure",
        "import_error",
        "module_not_found",
        "runtime_assertion_failure",
        "completion_validation_failed",
        "missing_dependency",
        "syntax_error",
        "source_step_validation",
        "missing_requested_symbol",
    }
)

# Phase 13B-S1: LLM patch generation stays False until Phase 13B-full.
# Changing this flag requires a separate approved implementation phase.
_LLM_PATCH_GENERATION_ENABLED: bool = False


def _failure_signature_hash(evidence: ExecutionRecoveryEvidence) -> str:
    """Stable 16-char SHA-256 prefix of the failure signature.

    Used to detect when the same failure recurs after a recovery attempt.
    """
    payload = "|".join(
        [
            evidence.failure_class,
            evidence.failed_command[:200],
            evidence.traceback_excerpt[:400],
            evidence.stderr_excerpt[:400],
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


def _prior_recovery_signature_hashes(orchestration_state: Any) -> list:
    return list(
        getattr(orchestration_state, "execution_recovery_signature_hashes", []) or []
    )


class ExecutionRecoveryService:
    """Bounded execution recovery — skeleton (Phase 13B-S1).

    All methods are static so the service is stateless and trivially mockable.
    """

    @staticmethod
    def should_attempt(
        evidence: ExecutionRecoveryEvidence,
        orchestration_state: Any,
    ) -> Tuple[bool, str]:
        """Eligibility gate. Returns (should_attempt, skip_reason).

        Does NOT consume budget or emit events.
        """
        attempts_used = int(
            getattr(orchestration_state, "execution_recovery_attempts", 0) or 0
        )

        if attempts_used >= RECOVERY_BUDGET:
            return False, "budget_exhausted"

        if evidence.is_empty:
            return False, "evidence_empty"

        if evidence.failure_class not in ELIGIBLE_RECOVERY_FAILURE_CLASSES:
            return False, "ineligible_failure_class"

        sig = _failure_signature_hash(evidence)
        if sig in _prior_recovery_signature_hashes(orchestration_state):
            return False, "repeated_failure_signature"

        return True, ""

    @staticmethod
    def attempt_recovery(
        *,
        project_dir: Any,
        session_id: int,
        task_id: int,
        evidence: ExecutionRecoveryEvidence,
        orchestration_state: Any,
        scope: str,
        step_index: Optional[int] = None,
        parent_event_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Attempt to recover from a terminal execution failure.

        Phase 13B-S1 behaviour:
          1. Calls should_attempt() — emits RECOVERY_SKIPPED and returns if ineligible.
          2. If eligible: consumes one budget slot, emits RECOVERY_ATTEMPTED,
             then immediately emits RECOVERY_FAILED (no patch applied).
          3. Never returns status="success".
          4. Never modifies any file in the workspace.

        Return dict always has "status" key: "skipped" | "failed".
        "success" is reserved for Phase 13B-full.
        """
        should, skip_reason = ExecutionRecoveryService.should_attempt(
            evidence, orchestration_state
        )
        attempts_used = int(
            getattr(orchestration_state, "execution_recovery_attempts", 0) or 0
        )

        if not should:
            try:
                append_orchestration_event(
                    project_dir=project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.EXECUTION_RECOVERY_SKIPPED,
                    parent_event_id=parent_event_id,
                    details={
                        "scope": scope,
                        "step_index": step_index,
                        "skip_reason": skip_reason,
                        "failure_class": evidence.failure_class,
                        "total_recovery_attempts_used": attempts_used,
                        "llm_patch_generation_enabled": _LLM_PATCH_GENERATION_ENABLED,
                    },
                )
            except Exception as exc:
                logger.debug("[RECOVERY] SKIPPED event emit failed: %s", exc)
            return {"status": "skipped", "reason": skip_reason}

        # Eligible — consume one budget slot.
        sig = _failure_signature_hash(evidence)
        new_attempts = attempts_used + 1

        prior_sigs = _prior_recovery_signature_hashes(orchestration_state)
        if sig not in prior_sigs:
            prior_sigs = prior_sigs + [sig]
        try:
            setattr(
                orchestration_state,
                "execution_recovery_signature_hashes",
                prior_sigs,
            )
        except Exception:
            pass

        try:
            orchestration_state.execution_recovery_attempts = new_attempts
        except Exception:
            pass

        budget_exhausted = new_attempts >= RECOVERY_BUDGET

        try:
            append_orchestration_event(
                project_dir=project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.EXECUTION_RECOVERY_ATTEMPTED,
                parent_event_id=parent_event_id,
                details={
                    "scope": scope,
                    "step_index": step_index,
                    "attempt": new_attempts,
                    "failure_class": evidence.failure_class,
                    "failed_command": evidence.failed_command[:200],
                    "exit_code": evidence.exit_code,
                    "evidence_chars": evidence.total_chars,
                    "changed_files_count": len(evidence.changed_files),
                    "requested_symbols": evidence.requested_symbols[:10],
                    "patch_type": "noop",
                    "llm_patch_generation_enabled": _LLM_PATCH_GENERATION_ENABLED,
                },
            )
        except Exception as exc:
            logger.debug("[RECOVERY] ATTEMPTED event emit failed: %s", exc)

        # Phase 13B-S1: LLM patch generation disabled — always FAILED.
        stop_reason = "llm_patch_generation_disabled"

        try:
            append_orchestration_event(
                project_dir=project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.EXECUTION_RECOVERY_FAILED,
                parent_event_id=parent_event_id,
                details={
                    "scope": scope,
                    "step_index": step_index,
                    "attempt": new_attempts,
                    "failure_class": evidence.failure_class,
                    "stop_reason": stop_reason,
                    "rerun_exit_code": None,
                    "total_recovery_attempts_used": new_attempts,
                    "budget_exhausted": budget_exhausted,
                    "llm_patch_generation_enabled": _LLM_PATCH_GENERATION_ENABLED,
                },
            )
        except Exception as exc:
            logger.debug("[RECOVERY] FAILED event emit failed: %s", exc)

        logger.info(
            "[RECOVERY] Attempt %s/%s (%s scope) — noop (LLM patch generation disabled in S1)",
            new_attempts,
            RECOVERY_BUDGET,
            scope,
        )
        return {"status": "failed", "reason": stop_reason}
