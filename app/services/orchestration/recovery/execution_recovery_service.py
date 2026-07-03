"""Phase 13B-S3: Bounded execution recovery service.

Step-scope patch generation is ENABLED. Completion-scope recovery is ENABLED
for failure_class == "missing_requested_symbol" only.

Phase 13B-S1 behaviour is preserved when llm_callable is not provided (noop path).
Phase 13B-S2 behaviour: when scope=="step" and llm_callable is provided, a real
recovery patch is generated, validated, applied, and the rerun_command is executed.
Phase 13B-S3 behaviour: scope=="completion" is also routed to _step_recovery when
failure_class is "missing_requested_symbol" and llm_callable is provided.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.reflection_evidence import ReflectionEvidence

logger = logging.getLogger(__name__)

# Hard budget: max recovery attempts per task run.
RECOVERY_BUDGET: int = 2

# Failure classes eligible for execution recovery.
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

# Phase 13B-S2: step-scope patch generation is enabled.
_LLM_PATCH_GENERATION_ENABLED: bool = True
_STEP_SCOPE_RECOVERY_ENABLED: bool = True
# Phase 13B-S3: generic completion-scope recovery remains disabled.
# Narrow completion recovery is enabled only for missing_requested_symbol.
_COMPLETION_SCOPE_RECOVERY_ENABLED: bool = False
_COMPLETION_MISSING_SYMBOL_RECOVERY_ENABLED: bool = True
_COMPLETION_SCOPE_RECOVERY_ELIGIBLE_CLASSES: frozenset = frozenset(
    {"missing_requested_symbol"}
)


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


def _prior_hashes(orchestration_state: Any) -> list:
    return list(
        getattr(orchestration_state, "execution_recovery_signature_hashes", []) or []
    )


class ExecutionRecoveryService:
    """Bounded execution recovery — Phase 13B-S3.

    Step-scope: full patch-and-rerun pipeline.
    Completion-scope: patch-and-rerun ONLY for missing_requested_symbol.
    Generic completion-scope recovery remains disabled.
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
        if sig in _prior_hashes(orchestration_state):
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
        llm_callable: Optional[Callable[[str], str]] = None,
        command_runner: Optional[Callable[[str], Tuple[int, str, str]]] = None,
        validator_callable: Optional[Callable[[str], Tuple[bool, str]]] = None,
        reflection_evidence: Optional[ReflectionEvidence] = None,
    ) -> Dict[str, Any]:
        """Attempt to recover from a terminal execution failure.

        Phase 13B-S2 behaviour for scope=="step" when llm_callable is provided:
          1. Calls should_attempt() — emits RECOVERY_SKIPPED and returns if ineligible.
          2. If eligible: consumes one budget slot.
          3. Calls llm_callable(prompt) → parse → validate → apply → rerun.
          4. On rerun exit 0: calls validator_callable(patch_path) if provided.
          5. On rerun exit 0 + validator accepts: emits RECOVERY_SUCCEEDED.
          6. On any failure after budget consumed: emits RECOVERY_FAILED.

        validator_callable(patch_path) -> (accepted: bool, reason: str).
        When None, validation is skipped (S2 backward-compat for tests without validator).

        Phase 13B-S3 completion-scope behaviour (scope=="completion"):
          - Routes to _step_recovery ONLY when failure_class=="missing_requested_symbol"
            AND _COMPLETION_MISSING_SYMBOL_RECOVERY_ENABLED AND llm_callable is provided.
          - All other completion failures remain noop (scope_disabled).

        Phase 13B-S1/noop behaviour (scope=="completion" ineligible OR llm_callable is None):
          - Emits RECOVERY_ATTEMPTED + RECOVERY_FAILED(llm_disabled or scope_disabled).
          - Never returns status="success".

        Return dict always has "status": "skipped" | "failed" | "success".
        """
        from pathlib import Path as _Path

        project_dir = _Path(str(project_dir))

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

        # Consume one budget slot.
        failure_sig = _failure_signature_hash(evidence)
        new_attempts = attempts_used + 1

        prior = _prior_hashes(orchestration_state)
        if failure_sig not in prior:
            prior = prior + [failure_sig]
        try:
            setattr(orchestration_state, "execution_recovery_signature_hashes", prior)
            orchestration_state.execution_recovery_attempts = new_attempts
        except Exception:
            pass

        budget_exhausted_after = new_attempts >= RECOVERY_BUDGET

        # --- Scope routing ---
        if scope == "completion":
            # S3: allow recovery only for missing_requested_symbol when enabled.
            _completion_eligible = (
                _COMPLETION_MISSING_SYMBOL_RECOVERY_ENABLED
                and evidence.failure_class
                in _COMPLETION_SCOPE_RECOVERY_ELIGIBLE_CLASSES
                and llm_callable is not None
            )
            if not _completion_eligible:
                stop_reason = "completion_scope_disabled"
                return ExecutionRecoveryService._noop_attempt(
                    project_dir=project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    evidence=evidence,
                    scope=scope,
                    step_index=step_index,
                    parent_event_id=parent_event_id,
                    new_attempts=new_attempts,
                    budget_exhausted=budget_exhausted_after,
                    stop_reason=stop_reason,
                )
            # Fall through to _step_recovery for eligible completion-scope recovery.
        elif llm_callable is None:
            # Keep S1-compatible stop_reason so existing tests pass.
            stop_reason = "llm_patch_generation_disabled"
            return ExecutionRecoveryService._noop_attempt(
                project_dir=project_dir,
                session_id=session_id,
                task_id=task_id,
                evidence=evidence,
                scope=scope,
                step_index=step_index,
                parent_event_id=parent_event_id,
                new_attempts=new_attempts,
                budget_exhausted=budget_exhausted_after,
                stop_reason=stop_reason,
            )

        # --- Step scope OR eligible completion scope: real recovery. ---
        return ExecutionRecoveryService._step_recovery(
            project_dir=project_dir,
            session_id=session_id,
            task_id=task_id,
            evidence=evidence,
            orchestration_state=orchestration_state,
            scope=scope,
            step_index=step_index,
            parent_event_id=parent_event_id,
            new_attempts=new_attempts,
            budget_exhausted=budget_exhausted_after,
            llm_callable=llm_callable,
            command_runner=command_runner,
            validator_callable=validator_callable,
            reflection_evidence=reflection_evidence,
        )

    @staticmethod
    def _noop_attempt(
        *,
        project_dir: Any,
        session_id: int,
        task_id: int,
        evidence: ExecutionRecoveryEvidence,
        scope: str,
        step_index: Optional[int],
        parent_event_id: Optional[str],
        new_attempts: int,
        budget_exhausted: bool,
        stop_reason: str,
    ) -> Dict[str, Any]:
        """S1-compatible noop: emit ATTEMPTED + FAILED, never succeed."""
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
            "[RECOVERY] Noop attempt %s/%s (%s): %s",
            new_attempts,
            RECOVERY_BUDGET,
            scope,
            stop_reason,
        )
        return {"status": "failed", "reason": stop_reason}

    @staticmethod
    def _step_recovery(
        *,
        project_dir: Any,
        session_id: int,
        task_id: int,
        evidence: ExecutionRecoveryEvidence,
        orchestration_state: Any,
        scope: str,
        step_index: Optional[int],
        parent_event_id: Optional[str],
        new_attempts: int,
        budget_exhausted: bool,
        llm_callable: Callable[[str], str],
        command_runner: Optional[Callable[[str], Tuple[int, str, str]]],
        validator_callable: Optional[Callable[[str], Tuple[bool, str]]] = None,
        reflection_evidence: Optional[ReflectionEvidence] = None,
    ) -> Dict[str, Any]:
        """Full step-scope recovery: LLM patch → validate → apply → rerun → validator."""
        from app.services.orchestration.recovery.recovery_patch import (
            RecoveryPatch,
            apply_recovery_patch,
            build_recovery_prompt,
            parse_recovery_patch,
            post_apply_test_preservation_check,
            validate_recovery_patch,
        )

        # Tracks whether patch was applied to disk (determines rollback_performed in events).
        _patch_applied = False

        def _emit_failed(
            stop_reason: str,
            rerun_exit_code: Optional[int] = None,
            extra: Optional[dict] = None,
            rollback_performed: bool = False,
        ) -> None:
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
                        "rerun_exit_code": rerun_exit_code,
                        "rollback_performed": rollback_performed,
                        "total_recovery_attempts_used": new_attempts,
                        "budget_exhausted": budget_exhausted,
                        "llm_patch_generation_enabled": _LLM_PATCH_GENERATION_ENABLED,
                        **(extra or {}),
                    },
                )
            except Exception as exc:
                logger.debug("[RECOVERY] FAILED event emit failed: %s", exc)

        # Step 1: Call LLM.
        try:
            prompt = build_recovery_prompt(
                evidence,
                reflection_evidence=reflection_evidence,
            )
            raw_text = llm_callable(prompt)
        except Exception as exc:
            logger.warning("[RECOVERY] LLM call failed: %s", exc)
            _emit_failed("llm_call_failed")
            return {"status": "failed", "reason": "llm_call_failed"}

        # Step 2: Parse response.
        patch, parse_error = parse_recovery_patch(raw_text)
        if patch is None:
            logger.info("[RECOVERY] Patch parse failed: %s", parse_error)
            _emit_failed("prose_response", extra={"parse_error": parse_error})
            return {"status": "failed", "reason": "prose_response"}

        # Step 3: Validate patch (scope, safety, test-preservation pre-check).
        valid, validation_reason = validate_recovery_patch(patch, evidence, project_dir)
        if not valid:
            logger.info("[RECOVERY] Patch validation failed: %s", validation_reason)
            _emit_failed(validation_reason)
            return {"status": "failed", "reason": validation_reason}

        # Step 4: Check for repeated patch hash.
        patch_hash = patch.content_hash()
        prior = _prior_hashes(orchestration_state)
        if patch_hash in prior:
            logger.info("[RECOVERY] Repeated patch hash rejected: %s", patch_hash)
            _emit_failed("repeated_patch")
            return {"status": "failed", "reason": "repeated_patch"}

        # Store patch hash to prevent future repeats.
        try:
            setattr(
                orchestration_state,
                "execution_recovery_signature_hashes",
                prior + [patch_hash],
            )
        except Exception:
            pass

        # Step 5: Emit ATTEMPTED (valid patch about to be applied).
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
                    "patch_type": patch.patch_type,
                    "patch_path": patch.path,
                    "rerun_command": patch.rerun_command[:200],
                    "llm_patch_generation_enabled": _LLM_PATCH_GENERATION_ENABLED,
                },
            )
        except Exception as exc:
            logger.debug("[RECOVERY] ATTEMPTED event emit failed: %s", exc)

        # Step 6: Apply the patch.
        apply_ok, apply_error, rollback = apply_recovery_patch(patch, project_dir)
        if not apply_ok:
            logger.info("[RECOVERY] Patch apply failed: %s", apply_error)
            _emit_failed(
                "apply_failed",
                extra={"apply_error": apply_error},
                rollback_performed=False,
            )
            return {"status": "failed", "reason": "apply_failed"}

        # Patch is now on disk — all subsequent failures must call rollback().
        _patch_applied = True

        # Step 7: Post-apply test preservation check (reads from disk).
        tp_violation = post_apply_test_preservation_check(patch, project_dir)
        if tp_violation:
            rollback()
            logger.info("[RECOVERY] Test preservation violated after apply")
            _emit_failed("test_preservation_violated", rollback_performed=True)
            return {"status": "failed", "reason": "test_preservation_violated"}

        # Step 8: Run rerun command.
        rerun_exit_code = -1
        rerun_stdout = ""
        rerun_stderr = ""

        if command_runner is None:
            rollback()
            _emit_failed("command_runner_not_provided", rollback_performed=True)
            return {"status": "failed", "reason": "command_runner_not_provided"}

        try:
            rerun_exit_code, rerun_stdout, rerun_stderr = command_runner(
                patch.rerun_command
            )
        except Exception as exc:
            rollback()
            logger.warning("[RECOVERY] Rerun command raised: %s", exc)
            _emit_failed(
                "rerun_command_raised", rerun_exit_code=-1, rollback_performed=True
            )
            return {"status": "failed", "reason": "rerun_command_raised"}

        if rerun_exit_code != 0:
            rollback()
            logger.info(
                "[RECOVERY] Rerun command failed (exit %s): %s",
                rerun_exit_code,
                patch.rerun_command,
            )
            _emit_failed(
                "rerun_still_failing",
                rerun_exit_code=rerun_exit_code,
                rollback_performed=True,
            )
            return {"status": "failed", "reason": "rerun_still_failing"}

        # Step 9: Post-recovery validation gate.
        # Calls validator_callable(patch.path) when provided; None means skip (S2 compat).
        _validator_accepted = True
        _validation_reason = ""
        if validator_callable is not None:
            try:
                _validator_accepted, _validation_reason = validator_callable(patch.path)
            except Exception as exc:
                _validator_accepted = False
                _validation_reason = f"validator_exception:{exc}"
                logger.warning("[RECOVERY] Validator raised: %s", exc)

            if not _validator_accepted:
                rollback()
                logger.info(
                    "[RECOVERY] Post-recovery validator rejected: %s",
                    _validation_reason,
                )
                _emit_failed(
                    "validator_rejected",
                    rerun_exit_code=rerun_exit_code,
                    rollback_performed=True,
                    extra={"post_recovery_validation_reason": _validation_reason},
                )
                return {"status": "failed", "reason": "validator_rejected"}

        # Step 10: Recovery succeeded.
        try:
            append_orchestration_event(
                project_dir=project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.EXECUTION_RECOVERY_SUCCEEDED,
                parent_event_id=parent_event_id,
                details={
                    "scope": scope,
                    "step_index": step_index,
                    "attempt": new_attempts,
                    "patch_type": patch.patch_type,
                    "patch_path": patch.path,
                    "rerun_command": patch.rerun_command[:200],
                    "rerun_exit_code": rerun_exit_code,
                    "validator_accepted": _validator_accepted,
                    "total_recovery_attempts_used": new_attempts,
                    "llm_patch_generation_enabled": _LLM_PATCH_GENERATION_ENABLED,
                },
            )
        except Exception as exc:
            logger.debug("[RECOVERY] SUCCEEDED event emit failed: %s", exc)

        logger.info(
            "[RECOVERY] Step recovery succeeded — attempt %s/%s, patch_type=%s, path=%s",
            new_attempts,
            RECOVERY_BUDGET,
            patch.patch_type,
            patch.path,
        )
        return {
            "status": "success",
            "patch_type": patch.patch_type,
            "patch_path": patch.path,
            "rerun_stdout": rerun_stdout[:1000],
            "rerun_exit_code": rerun_exit_code,
        }
