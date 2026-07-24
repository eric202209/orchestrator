"""Phase 29D-4 immutable recovery decision and D-3A-snapshot-only rollback.

This module never runs Git, a shell, or a candidate command, and never
reconstructs bytes except from the exact Phase 29D-3A Pre-Apply Snapshot
bound to the failing Apply Result.  It performs no lifecycle transition; the
owning Execution Task's lifecycle is advanced only by
``app.services.execution.apply_lifecycle``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionTaskApplyResult,
    ExecutionTaskPostApplyValidation,
    ExecutionTaskPreApplySnapshot,
    ExecutionTaskPreApplySnapshotEntry,
    ExecutionTaskRecoveryDecision,
    ExecutionTaskRecoveryResult,
    ExecutionWorkspaceTarget,
)
from app.services.execution.candidate_content import (
    CandidateContentError,
    CandidateContentStore,
    LocalContentAddressedStore,
)
from app.services.execution.pre_apply_snapshot import (
    verify_pre_apply_snapshot_integrity,
)
from app.services.planning.operator_review import canonical_json_hash
from app.services.workspace.project_mutation_lock import (
    ProjectMutationLockError,
    project_mutation_lock,
)


RECOVERY_DECISION_SCHEMA_VERSION = "execution-task-recovery-decision/1.0"
RECOVERY_RESULT_SCHEMA_VERSION = "execution-task-recovery-result/1.0"
RECOVERY_DECISIONS = frozenset(
    {
        "rollback_required",
        "no_recovery_required",
        "recovery_blocked",
        "manual_intervention_required",
    }
)
RECOVERY_RESULT_STATUSES = frozenset(
    {"recovered", "blocked", "failed", "manual_intervention_required"}
)
MAX_FAILURE_DETAIL_LENGTH = 1024


class RecoveryError(RuntimeError):
    """A bounded recovery-authority failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _short(value: object) -> str:
    return str(value)[:MAX_FAILURE_DETAIL_LENGTH]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reserve_sibling_path(parent: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(dir=parent, prefix=prefix)
    os.close(descriptor)
    path = Path(name)
    path.unlink(missing_ok=True)
    return path


def _lstat_state(path: Path) -> tuple[str, str | None]:
    """Return (``"absent"``|``"regular_file"``|``"other"``, sha256-or-None)."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "absent", None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return "other", None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return "regular_file", digest.hexdigest()


@dataclass(frozen=True)
class DecideRecoveryCommand:
    apply_result_id: int


@dataclass(frozen=True)
class RecoveryDecisionOutcome:
    decision: ExecutionTaskRecoveryDecision
    replayed: bool = False


class RecoveryDecisionService:
    """Derive exactly one immutable recovery decision per Apply Result."""

    def __init__(self, db: Session, *, now: Any = None):
        self.db = db
        self._now = now or _utc_now

    def decide(self, command: DecideRecoveryCommand) -> RecoveryDecisionOutcome:
        result = self.db.get(ExecutionTaskApplyResult, int(command.apply_result_id))
        if result is None:
            raise RecoveryError("apply_result_missing", "apply result does not exist")
        existing = self._existing(result.id)
        if existing is not None:
            return RecoveryDecisionOutcome(existing, replayed=True)

        validation: ExecutionTaskPostApplyValidation | None = None
        if result.status == "applied":
            validation = (
                self.db.query(ExecutionTaskPostApplyValidation)
                .filter(ExecutionTaskPostApplyValidation.apply_result_id == result.id)
                .order_by(
                    ExecutionTaskPostApplyValidation.validation_policy_version.desc()
                )
                .first()
            )
            if validation is None:
                raise RecoveryError(
                    "post_apply_validation_required",
                    "an applied result requires post-apply validation before "
                    "a recovery decision can be made",
                )
            decision, reason, detail = self._decide_from_validation(validation)
        elif result.status == "blocked":
            decision, reason, detail = (
                "no_recovery_required",
                "apply_never_mutated",
                "apply attempt was blocked before any workspace mutation",
            )
        elif result.status == "failed":
            decision, reason, detail = self._decide_from_failed_apply(result)
        else:  # pragma: no cover - defensive, apply_result status is enum-bound
            raise RecoveryError(
                "apply_result_status_invalid",
                f"unsupported apply result status: {result.status!r}",
            )

        return self._persist(
            result,
            validation=validation,
            decision=decision,
            reason=reason,
            detail=detail,
        )

    @staticmethod
    def _decide_from_validation(
        validation: ExecutionTaskPostApplyValidation,
    ) -> tuple[str, str, str | None]:
        if validation.status == "passed":
            return (
                "no_recovery_required",
                "post_apply_validation_passed",
                None,
            )
        if validation.status == "failed":
            return (
                "rollback_required",
                "post_apply_validation_failed",
                validation.failure_detail,
            )
        if validation.status == "blocked":
            return (
                "recovery_blocked",
                "post_apply_validation_blocked",
                validation.failure_detail,
            )
        return (
            "manual_intervention_required",
            "post_apply_validation_error",
            validation.failure_detail,
        )

    def _decide_from_failed_apply(
        self, result: ExecutionTaskApplyResult
    ) -> tuple[str, str, str | None]:
        if result.pre_apply_snapshot_id is None:
            return (
                "manual_intervention_required",
                "apply_failed_snapshot_unavailable",
                "a failed apply attempt has no bound pre-apply snapshot",
            )
        snapshot = self.db.get(
            ExecutionTaskPreApplySnapshot, result.pre_apply_snapshot_id
        )
        if snapshot is None or snapshot.status != "captured":
            return (
                "manual_intervention_required",
                "apply_failed_snapshot_unavailable",
                "bound pre-apply snapshot is missing or was not captured",
            )
        integrity = verify_pre_apply_snapshot_integrity(self.db, snapshot.id)
        if not integrity.verified:
            return (
                "manual_intervention_required",
                "apply_failed_snapshot_integrity_failure",
                ",".join(integrity.issues),
            )
        return (
            "rollback_required",
            "apply_failed_with_snapshot",
            result.failure_detail,
        )

    def _existing(self, apply_result_id: int) -> ExecutionTaskRecoveryDecision | None:
        return (
            self.db.query(ExecutionTaskRecoveryDecision)
            .filter(ExecutionTaskRecoveryDecision.apply_result_id == apply_result_id)
            .one_or_none()
        )

    def _persist(
        self,
        result: ExecutionTaskApplyResult,
        *,
        validation: ExecutionTaskPostApplyValidation | None,
        decision: str,
        reason: str,
        detail: str | None,
    ) -> RecoveryDecisionOutcome:
        if decision not in RECOVERY_DECISIONS:
            raise ValueError(f"unsupported recovery decision: {decision}")
        payload = {
            "schema_version": RECOVERY_DECISION_SCHEMA_VERSION,
            "execution_plan_id": result.execution_plan_id,
            "execution_task_id": result.execution_task_id,
            "execution_task_attempt_id": result.execution_task_attempt_id,
            "attempt_generation": result.attempt_generation,
            "apply_result_id": result.id,
            "apply_result_hash": result.canonical_sha256,
            "post_apply_validation_id": validation.id if validation else None,
            "post_apply_validation_hash": (
                validation.canonical_sha256 if validation else None
            ),
            "decision": decision,
            "decision_reason": reason,
            "decision_detail": _short(detail) if detail else None,
        }
        payload_hash = canonical_json_hash(payload)
        row = ExecutionTaskRecoveryDecision(
            execution_plan_id=result.execution_plan_id,
            execution_task_id=result.execution_task_id,
            execution_task_attempt_id=result.execution_task_attempt_id,
            attempt_generation=result.attempt_generation,
            apply_result_id=result.id,
            apply_result_hash=result.canonical_sha256,
            post_apply_validation_id=validation.id if validation else None,
            post_apply_validation_hash=(
                validation.canonical_sha256 if validation else None
            ),
            decision=decision,
            decision_reason=reason,
            decision_detail=_short(detail) if detail else None,
            canonical_payload=payload,
            canonical_sha256=payload_hash,
            decision_idempotency_key=f"recovery-decision:{result.id}",
            created_at=self._now(),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = self._existing(result.id)
            if replay is not None and replay.canonical_sha256 == payload_hash:
                return RecoveryDecisionOutcome(replay, replayed=True)
            raise RecoveryError(
                "recovery_decision_insert_conflict",
                "recovery decision conflicts with canonical authority",
            ) from exc
        return RecoveryDecisionOutcome(row)


@dataclass(frozen=True)
class ExecuteRecoveryCommand:
    recovery_decision_id: int
    owner: str = "controlled-apply-recovery"
    lock_wait_timeout_seconds: float = 2.0


@dataclass(frozen=True)
class RecoveryExecutionOutcome:
    result: ExecutionTaskRecoveryResult
    replayed: bool = False


class RecoveryExecutionService:
    """Execute (or trivially close) exactly one recovery decision."""

    def __init__(
        self,
        db: Session,
        *,
        store: CandidateContentStore | None = None,
        now: Any = None,
    ):
        self.db = db
        self.store = store or LocalContentAddressedStore()
        self._now = now or _utc_now

    def execute(self, command: ExecuteRecoveryCommand) -> RecoveryExecutionOutcome:
        decision = self.db.get(
            ExecutionTaskRecoveryDecision, int(command.recovery_decision_id)
        )
        if decision is None:
            raise RecoveryError(
                "recovery_decision_missing", "recovery decision does not exist"
            )
        existing = self._existing(decision.id)
        if existing is not None:
            return RecoveryExecutionOutcome(existing, replayed=True)

        started_at = self._now()
        apply_result = self.db.get(ExecutionTaskApplyResult, decision.apply_result_id)
        if apply_result is None:
            return RecoveryExecutionOutcome(
                self._persist(
                    decision,
                    apply_result_id=decision.apply_result_id,
                    apply_result_hash=decision.apply_result_hash,
                    pre_apply_snapshot=None,
                    status="manual_intervention_required",
                    failure_reason="apply_result_missing",
                    failure_detail="bound apply result no longer exists",
                    rolled_back_operations=[],
                    started_at=started_at,
                    ended_at=self._now(),
                )
            )

        if decision.decision == "no_recovery_required":
            return RecoveryExecutionOutcome(
                self._persist(
                    decision,
                    apply_result_id=apply_result.id,
                    apply_result_hash=apply_result.canonical_sha256,
                    pre_apply_snapshot=None,
                    status="recovered",
                    failure_reason=None,
                    failure_detail=None,
                    rolled_back_operations=[],
                    started_at=started_at,
                    ended_at=self._now(),
                )
            )
        if decision.decision in {"recovery_blocked", "manual_intervention_required"}:
            return RecoveryExecutionOutcome(
                self._persist(
                    decision,
                    apply_result_id=apply_result.id,
                    apply_result_hash=apply_result.canonical_sha256,
                    pre_apply_snapshot=None,
                    status="manual_intervention_required",
                    failure_reason=decision.decision_reason,
                    failure_detail=decision.decision_detail,
                    rolled_back_operations=[],
                    started_at=started_at,
                    ended_at=self._now(),
                )
            )

        # decision.decision == "rollback_required"
        return self._execute_rollback(decision, apply_result, command, started_at)

    def _execute_rollback(
        self,
        decision: ExecutionTaskRecoveryDecision,
        apply_result: ExecutionTaskApplyResult,
        command: ExecuteRecoveryCommand,
        started_at: datetime,
    ) -> RecoveryExecutionOutcome:
        snapshot = (
            self.db.get(
                ExecutionTaskPreApplySnapshot, apply_result.pre_apply_snapshot_id
            )
            if apply_result.pre_apply_snapshot_id is not None
            else None
        )
        if snapshot is None or snapshot.status != "captured":
            return RecoveryExecutionOutcome(
                self._persist(
                    decision,
                    apply_result_id=apply_result.id,
                    apply_result_hash=apply_result.canonical_sha256,
                    pre_apply_snapshot=snapshot,
                    status="manual_intervention_required",
                    failure_reason="snapshot_unavailable",
                    failure_detail="rollback requires a captured pre-apply snapshot",
                    rolled_back_operations=[],
                    started_at=started_at,
                    ended_at=self._now(),
                )
            )
        snapshot_integrity = verify_pre_apply_snapshot_integrity(
            self.db, snapshot.id, store=self.store
        )
        if not snapshot_integrity.verified:
            return RecoveryExecutionOutcome(
                self._persist(
                    decision,
                    apply_result_id=apply_result.id,
                    apply_result_hash=apply_result.canonical_sha256,
                    pre_apply_snapshot=snapshot,
                    status="manual_intervention_required",
                    failure_reason="snapshot_integrity_failure",
                    failure_detail=",".join(snapshot_integrity.issues),
                    rolled_back_operations=[],
                    started_at=started_at,
                    ended_at=self._now(),
                )
            )
        target = self.db.get(ExecutionWorkspaceTarget, apply_result.workspace_target_id)
        if target is None:
            return RecoveryExecutionOutcome(
                self._persist(
                    decision,
                    apply_result_id=apply_result.id,
                    apply_result_hash=apply_result.canonical_sha256,
                    pre_apply_snapshot=snapshot,
                    status="manual_intervention_required",
                    failure_reason="workspace_target_missing",
                    failure_detail="workspace target authority is missing",
                    rolled_back_operations=[],
                    started_at=started_at,
                    ended_at=self._now(),
                )
            )
        root = Path(target.normalized_realpath)
        try:
            with project_mutation_lock(
                project_id=target.project_id,
                project_root=root,
                operation="controlled_apply_rollback",
                owner=command.owner,
                wait_timeout_seconds=max(0.0, command.lock_wait_timeout_seconds),
            ):
                replay = self._existing(decision.id)
                if replay is not None:
                    return RecoveryExecutionOutcome(replay, replayed=True)
                return self._rollback_locked(
                    decision, apply_result, snapshot, root, started_at
                )
        except ProjectMutationLockError:
            replay = self._existing(decision.id)
            if replay is not None:
                return RecoveryExecutionOutcome(replay, replayed=True)
            return RecoveryExecutionOutcome(
                self._persist(
                    decision,
                    apply_result_id=apply_result.id,
                    apply_result_hash=apply_result.canonical_sha256,
                    pre_apply_snapshot=snapshot,
                    status="blocked",
                    failure_reason="lock_timeout",
                    failure_detail="workspace mutation lock was not acquired",
                    rolled_back_operations=[],
                    started_at=started_at,
                    ended_at=self._now(),
                )
            )

    def _rollback_locked(
        self,
        decision: ExecutionTaskRecoveryDecision,
        apply_result: ExecutionTaskApplyResult,
        snapshot: ExecutionTaskPreApplySnapshot,
        root: Path,
        started_at: datetime,
    ) -> RecoveryExecutionOutcome:
        entries = (
            self.db.query(ExecutionTaskPreApplySnapshotEntry)
            .filter(ExecutionTaskPreApplySnapshotEntry.snapshot_id == snapshot.id)
            .order_by(ExecutionTaskPreApplySnapshotEntry.entry_index)
            .all()
        )
        # For an `applied` result that failed post-apply validation, the
        # baseline for "has anything changed since we decided to roll back"
        # is what validation *observed* per path -- not the ChangeSet's
        # promised post-apply hash, since a hash mismatch is exactly why
        # validation failed in the first place. A failed apply attempt (no
        # validation ever ran) falls back to the snapshot's own recorded
        # pre/post-apply expectation.
        observed_by_path: dict[str, dict[str, Any]] = {}
        if decision.post_apply_validation_id is not None:
            validation = self.db.get(
                ExecutionTaskPostApplyValidation, decision.post_apply_validation_id
            )
            if validation is not None:
                for check in validation.canonical_payload.get("checks", []):
                    observed_by_path[check["canonical_path"]] = check

        plan: list[dict[str, Any]] = []
        drift: list[str] = []
        for entry in entries:
            path = root / entry.canonical_path
            state, digest = _lstat_state(path)
            expected_pre_exists = bool(entry.previous_exists)
            observed = observed_by_path.get(entry.canonical_path)
            if observed is not None and observed.get("observed_entry_type") in (
                "absent",
                "regular_file",
            ):
                baseline_exists = bool(observed["observed_exists"])
                baseline_sha256 = observed["observed_sha256"]
            else:
                baseline_exists = bool(entry.expected_post_apply_exists)
                baseline_sha256 = entry.expected_post_apply_sha256
            if state == "other":
                drift.append(entry.canonical_path)
                continue
            matches_baseline = (state == "regular_file" and baseline_exists) and (
                digest == baseline_sha256
            )
            matches_absent_baseline = state == "absent" and not baseline_exists
            matches_pre = (state == "regular_file" and expected_pre_exists) and (
                digest == entry.previous_sha256
            )
            matches_absent_pre = state == "absent" and not expected_pre_exists
            if matches_baseline or matches_absent_baseline:
                plan.append({"entry": entry, "path": path, "action": "revert"})
            elif matches_pre or matches_absent_pre:
                plan.append({"entry": entry, "path": path, "action": "noop"})
            else:
                drift.append(entry.canonical_path)

        if drift:
            return RecoveryExecutionOutcome(
                self._persist(
                    decision,
                    apply_result_id=apply_result.id,
                    apply_result_hash=apply_result.canonical_sha256,
                    pre_apply_snapshot=snapshot,
                    status="blocked",
                    failure_reason="rollback_drift_detected",
                    failure_detail=_short(", ".join(sorted(drift))),
                    rolled_back_operations=[],
                    started_at=started_at,
                    ended_at=self._now(),
                )
            )

        rolled_back: list[dict[str, Any]] = []
        try:
            for item in plan:
                entry: ExecutionTaskPreApplySnapshotEntry = item["entry"]
                path: Path = item["path"]
                if item["action"] == "noop":
                    rolled_back.append(
                        {
                            "operation": entry.operation,
                            "path": entry.canonical_path,
                            "action": "noop",
                        }
                    )
                    continue
                if entry.previous_exists:
                    data = self.store.read(str(entry.previous_storage_key))
                    descriptor, name = tempfile.mkstemp(
                        dir=path.parent, prefix=".orchestrator-rollback-"
                    )
                    temporary_path = Path(name)
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if path.exists():
                        backup_path = _reserve_sibling_path(
                            path.parent, ".orchestrator-rollback-backup-"
                        )
                        os.replace(path, backup_path)
                        os.replace(temporary_path, path)
                        backup_path.unlink(missing_ok=True)
                    else:
                        os.replace(temporary_path, path)
                    _fsync_directory(path.parent)
                else:
                    path.unlink(missing_ok=True)
                    _fsync_directory(path.parent)
                rolled_back.append(
                    {
                        "operation": entry.operation,
                        "path": entry.canonical_path,
                        "action": "reverted",
                    }
                )
        except (OSError, CandidateContentError) as exc:
            return RecoveryExecutionOutcome(
                self._persist(
                    decision,
                    apply_result_id=apply_result.id,
                    apply_result_hash=apply_result.canonical_sha256,
                    pre_apply_snapshot=snapshot,
                    status="failed",
                    failure_reason="rollback_io_failure",
                    failure_detail=_short(exc),
                    rolled_back_operations=rolled_back,
                    started_at=started_at,
                    ended_at=self._now(),
                )
            )

        return RecoveryExecutionOutcome(
            self._persist(
                decision,
                apply_result_id=apply_result.id,
                apply_result_hash=apply_result.canonical_sha256,
                pre_apply_snapshot=snapshot,
                status="recovered",
                failure_reason=None,
                failure_detail=None,
                rolled_back_operations=rolled_back,
                started_at=started_at,
                ended_at=self._now(),
            )
        )

    def _existing(
        self, recovery_decision_id: int
    ) -> ExecutionTaskRecoveryResult | None:
        return (
            self.db.query(ExecutionTaskRecoveryResult)
            .filter(
                ExecutionTaskRecoveryResult.recovery_decision_id == recovery_decision_id
            )
            .one_or_none()
        )

    def _persist(
        self,
        decision: ExecutionTaskRecoveryDecision,
        *,
        apply_result_id: int,
        apply_result_hash: str,
        pre_apply_snapshot: ExecutionTaskPreApplySnapshot | None,
        status: str,
        failure_reason: str | None,
        failure_detail: str | None,
        rolled_back_operations: list[dict[str, Any]],
        started_at: datetime,
        ended_at: datetime,
    ) -> ExecutionTaskRecoveryResult:
        if status not in RECOVERY_RESULT_STATUSES:
            raise ValueError(f"unsupported recovery result status: {status}")
        payload = {
            "schema_version": RECOVERY_RESULT_SCHEMA_VERSION,
            "execution_plan_id": decision.execution_plan_id,
            "execution_task_id": decision.execution_task_id,
            "execution_task_attempt_id": decision.execution_task_attempt_id,
            "attempt_generation": decision.attempt_generation,
            "recovery_decision_id": decision.id,
            "recovery_decision_hash": decision.canonical_sha256,
            "apply_result_id": apply_result_id,
            "apply_result_hash": apply_result_hash,
            "pre_apply_snapshot_id": (
                pre_apply_snapshot.id if pre_apply_snapshot is not None else None
            ),
            "pre_apply_snapshot_hash": (
                pre_apply_snapshot.canonical_sha256
                if pre_apply_snapshot is not None
                else None
            ),
            "status": status,
            "failure_reason": failure_reason,
            "failure_detail": _short(failure_detail) if failure_detail else None,
            "rolled_back_operations": rolled_back_operations,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
        }
        payload_hash = canonical_json_hash(payload)
        row = ExecutionTaskRecoveryResult(
            execution_plan_id=decision.execution_plan_id,
            execution_task_id=decision.execution_task_id,
            execution_task_attempt_id=decision.execution_task_attempt_id,
            attempt_generation=decision.attempt_generation,
            recovery_decision_id=decision.id,
            recovery_decision_hash=decision.canonical_sha256,
            apply_result_id=apply_result_id,
            apply_result_hash=apply_result_hash,
            pre_apply_snapshot_id=(
                pre_apply_snapshot.id if pre_apply_snapshot is not None else None
            ),
            pre_apply_snapshot_hash=(
                pre_apply_snapshot.canonical_sha256
                if pre_apply_snapshot is not None
                else None
            ),
            status=status,
            failure_reason=failure_reason,
            failure_detail=_short(failure_detail) if failure_detail else None,
            rolled_back_operations=rolled_back_operations,
            canonical_payload=payload,
            canonical_sha256=payload_hash,
            result_idempotency_key=f"recovery-result:{decision.id}",
            started_at=started_at,
            ended_at=ended_at,
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = self._existing(decision.id)
            if replay is not None and replay.canonical_sha256 == payload_hash:
                return replay
            raise RecoveryError(
                "recovery_result_insert_conflict",
                "recovery result conflicts with canonical authority",
            ) from exc
        return row


__all__ = [
    "DecideRecoveryCommand",
    "ExecuteRecoveryCommand",
    "RECOVERY_DECISIONS",
    "RECOVERY_RESULT_STATUSES",
    "RecoveryDecisionOutcome",
    "RecoveryDecisionService",
    "RecoveryError",
    "RecoveryExecutionOutcome",
    "RecoveryExecutionService",
]
