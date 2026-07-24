"""Phase 29D-4 immutable post-apply file-state validation.

This module verifies, read-only, that one already-`applied` Controlled Apply
result's workspace mutation matches its ChangeSet exactly: created/replaced
paths carry the expected new hash, deleted paths are absent, no path was
substituted with a symlink or a directory, and the Apply Result / Pre-Apply
Snapshot authorities it depends on still verify.  It never mutates the
workspace, never re-runs the apply, and never performs a lifecycle
transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionTaskApplyAttempt,
    ExecutionTaskApplyResult,
    ExecutionTaskChangeSet,
    ExecutionTaskChangeSetOperation,
    ExecutionTaskPostApplyValidation,
    ExecutionTaskPreApplySnapshot,
    ExecutionWorkspaceBaseState,
    ExecutionWorkspaceTarget,
)
from app.services.execution.apply_execution import verify_apply_result_integrity
from app.services.execution.candidate_content import (
    CandidateContentStore,
    LocalContentAddressedStore,
)
from app.services.execution.changeset import ChangeSetError, validate_changeset_path
from app.services.execution.pre_apply_snapshot import (
    verify_pre_apply_snapshot_integrity,
)
from app.services.planning.operator_review import canonical_json_hash


POST_APPLY_VALIDATION_SCHEMA_VERSION = "execution-task-post-apply-validation/1.0"
POST_APPLY_VALIDATION_POLICY_ID = "post-apply-file-state"
POST_APPLY_VALIDATION_POLICY_VERSION = 1
POST_APPLY_VALIDATION_STATUSES = frozenset(
    {"passed", "failed", "blocked", "validation_error"}
)
MAX_FAILURE_DETAIL_LENGTH = 1024


class PostApplyValidationError(RuntimeError):
    """A bounded validation-authority failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ValidatePostApplyCommand:
    apply_result_id: int


@dataclass(frozen=True)
class PostApplyValidationOutcome:
    validation: ExecutionTaskPostApplyValidation
    replayed: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _short(value: object) -> str:
    return str(value)[:MAX_FAILURE_DETAIL_LENGTH]


def _hash_path(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    total = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PostApplyValidationError(
                "non_regular_file", "path is not a regular file"
            )
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), total


class PostApplyValidationService:
    """Verify one immutable, already-applied Controlled Apply result."""

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

    def validate(self, command: ValidatePostApplyCommand) -> PostApplyValidationOutcome:
        result = self.db.get(ExecutionTaskApplyResult, int(command.apply_result_id))
        if result is None:
            raise PostApplyValidationError(
                "apply_result_missing", "apply result does not exist"
            )
        if result.status != "applied":
            raise PostApplyValidationError(
                "post_apply_validation_not_applicable",
                "post-apply validation only applies to an applied Apply Result",
            )
        existing = self._existing(result.id)
        if existing is not None:
            return PostApplyValidationOutcome(existing, replayed=True)

        attempt = self.db.get(ExecutionTaskApplyAttempt, result.apply_attempt_id)
        change_set = self.db.get(ExecutionTaskChangeSet, result.change_set_id)
        target = self.db.get(ExecutionWorkspaceTarget, result.workspace_target_id)
        base_state = self.db.get(ExecutionWorkspaceBaseState, result.base_state_id)
        snapshot = (
            self.db.get(ExecutionTaskPreApplySnapshot, result.pre_apply_snapshot_id)
            if result.pre_apply_snapshot_id is not None
            else None
        )
        if any(item is None for item in (attempt, change_set, target, base_state)):
            return self._persist(
                result,
                status="blocked",
                failure_reason="validation_scope_missing",
                failure_detail="apply result authority scope is incomplete",
                checks=[],
                snapshot=snapshot,
            )

        apply_integrity = verify_apply_result_integrity(
            self.db, result.id, store=self.store
        )
        if not apply_integrity.verified:
            return self._persist(
                result,
                status="blocked",
                failure_reason="apply_result_integrity_failure",
                failure_detail=",".join(apply_integrity.issues),
                checks=[],
                snapshot=snapshot,
            )
        if snapshot is None:
            return self._persist(
                result,
                status="blocked",
                failure_reason="pre_apply_snapshot_missing",
                failure_detail="applied result has no bound pre-apply snapshot",
                checks=[],
                snapshot=None,
            )
        snapshot_integrity = verify_pre_apply_snapshot_integrity(
            self.db, snapshot.id, store=self.store
        )
        if not snapshot_integrity.verified:
            return self._persist(
                result,
                status="blocked",
                failure_reason="pre_apply_snapshot_integrity_failure",
                failure_detail=",".join(snapshot_integrity.issues),
                checks=[],
                snapshot=snapshot,
            )

        assert target is not None
        try:
            root_metadata = Path(target.normalized_realpath).lstat()
        except OSError as exc:
            return self._persist(
                result,
                status="blocked",
                failure_reason="workspace_root_unavailable",
                failure_detail=_short(exc),
                checks=[],
                snapshot=snapshot,
            )
        root = Path(target.normalized_realpath)
        identity_issue = None
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(
            root_metadata.st_mode
        ):
            identity_issue = "workspace root is not the authorized directory"
        elif target.filesystem_device is not None and str(root_metadata.st_dev) != str(
            target.filesystem_device
        ):
            identity_issue = "workspace device identity changed"
        elif target.filesystem_inode is not None and str(root_metadata.st_ino) != str(
            target.filesystem_inode
        ):
            identity_issue = "workspace inode identity changed"
        elif root.resolve() != Path(target.normalized_realpath):
            identity_issue = "workspace realpath identity changed"
        if identity_issue is not None:
            return self._persist(
                result,
                status="blocked",
                failure_reason="workspace_target_identity_mismatch",
                failure_detail=identity_issue,
                checks=[],
                snapshot=snapshot,
            )

        operations = (
            self.db.query(ExecutionTaskChangeSetOperation)
            .filter(ExecutionTaskChangeSetOperation.change_set_id == change_set.id)
            .order_by(ExecutionTaskChangeSetOperation.operation_index)
            .all()
        )
        checks: list[dict[str, Any]] = []
        has_failure = False
        has_error = False
        for row in operations:
            check = self._check_operation(root, row)
            checks.append(check)
            if check["outcome"] == "io_error":
                has_error = True
            elif check["outcome"] != "ok":
                has_failure = True

        if has_error:
            status = "validation_error"
        elif has_failure:
            status = "failed"
        else:
            status = "passed"
        failure_reason = None
        failure_detail = None
        if status != "passed":
            failing = [item for item in checks if item["outcome"] != "ok"]
            failure_reason = failing[0]["outcome"] if failing else "unknown"
            failure_detail = _short(
                "; ".join(
                    f"{item['canonical_path']}:{item['outcome']}" for item in failing
                )
            )
        return self._persist(
            result,
            status=status,
            failure_reason=failure_reason,
            failure_detail=failure_detail,
            checks=checks,
            snapshot=snapshot,
        )

    def _check_operation(
        self, root: Path, row: ExecutionTaskChangeSetOperation
    ) -> dict[str, Any]:
        # ``observed_*`` fields are the actual on-disk state seen at
        # validation time, independent of the ``outcome`` verdict.  Recovery
        # (``apply_recovery.py``) uses these -- not the ChangeSet's promised
        # post-apply hash -- as the pre-rollback drift-check baseline, since
        # the whole point of validation failing is that the two may differ.
        try:
            canonical_path = validate_changeset_path(row.canonical_path)
        except ChangeSetError as exc:
            return {
                "canonical_path": row.canonical_path,
                "operation": row.operation,
                "outcome": "invalid_path",
                "detail": _short(exc),
                "observed_exists": None,
                "observed_entry_type": None,
                "observed_sha256": None,
            }
        path = root / canonical_path
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            metadata = None
        except OSError as exc:
            return {
                "canonical_path": canonical_path,
                "operation": row.operation,
                "outcome": "io_error",
                "detail": _short(exc),
                "observed_exists": None,
                "observed_entry_type": None,
                "observed_sha256": None,
            }

        if metadata is None:
            observed_exists, observed_type, observed_sha256 = False, "absent", None
        elif stat.S_ISLNK(metadata.st_mode):
            observed_exists, observed_type, observed_sha256 = True, "other", None
        elif not stat.S_ISREG(metadata.st_mode):
            observed_exists, observed_type, observed_sha256 = True, "other", None
        else:
            try:
                observed_sha256, _length = _hash_path(path)
            except (PostApplyValidationError, OSError) as exc:
                return {
                    "canonical_path": canonical_path,
                    "operation": row.operation,
                    "outcome": "io_error",
                    "detail": _short(exc),
                    "observed_exists": True,
                    "observed_entry_type": "other",
                    "observed_sha256": None,
                }
            observed_exists, observed_type = True, "regular_file"

        base = {
            "canonical_path": canonical_path,
            "operation": row.operation,
            "observed_exists": observed_exists,
            "observed_entry_type": observed_type,
            "observed_sha256": observed_sha256,
        }

        if row.operation == "delete_file":
            if observed_exists:
                return {
                    **base,
                    "outcome": "delete_path_present",
                    "detail": "expected path to be absent",
                }
            return {**base, "outcome": "ok", "detail": None}

        if not observed_exists:
            return {
                **base,
                "outcome": "expected_path_missing",
                "detail": "expected file is missing",
            }
        if observed_type == "other":
            return {
                **base,
                "outcome": "symlink_substitution",
                "detail": "path is not a regular file",
            }
        if observed_sha256 != row.content_sha256:
            return {
                **base,
                "outcome": "content_hash_mismatch",
                "detail": f"observed sha256 {observed_sha256}",
            }
        return {**base, "outcome": "ok", "detail": None}

    def _existing(
        self, apply_result_id: int
    ) -> ExecutionTaskPostApplyValidation | None:
        return (
            self.db.query(ExecutionTaskPostApplyValidation)
            .filter(
                ExecutionTaskPostApplyValidation.apply_result_id == apply_result_id,
                ExecutionTaskPostApplyValidation.validation_policy_version
                == POST_APPLY_VALIDATION_POLICY_VERSION,
            )
            .one_or_none()
        )

    def _persist(
        self,
        result: ExecutionTaskApplyResult,
        *,
        status: str,
        failure_reason: str | None,
        failure_detail: str | None,
        checks: list[dict[str, Any]],
        snapshot: ExecutionTaskPreApplySnapshot | None,
    ) -> PostApplyValidationOutcome:
        if status not in POST_APPLY_VALIDATION_STATUSES:
            raise ValueError(f"unsupported post-apply validation status: {status}")
        payload = {
            "schema_version": POST_APPLY_VALIDATION_SCHEMA_VERSION,
            "execution_plan_id": result.execution_plan_id,
            "execution_task_id": result.execution_task_id,
            "execution_task_attempt_id": result.execution_task_attempt_id,
            "attempt_generation": result.attempt_generation,
            "apply_result_id": result.id,
            "apply_result_hash": result.canonical_sha256,
            "apply_attempt_id": result.apply_attempt_id,
            "apply_attempt_hash": result.apply_attempt_hash,
            "change_set_id": result.change_set_id,
            "change_set_hash": result.change_set_hash,
            "pre_apply_snapshot_id": snapshot.id if snapshot is not None else None,
            "pre_apply_snapshot_hash": (
                snapshot.canonical_sha256 if snapshot is not None else None
            ),
            "workspace_target_id": result.workspace_target_id,
            "workspace_target_hash": result.workspace_target_hash,
            "base_state_id": result.base_state_id,
            "base_state_hash": result.base_state_hash,
            "validation_policy_id": POST_APPLY_VALIDATION_POLICY_ID,
            "validation_policy_version": POST_APPLY_VALIDATION_POLICY_VERSION,
            "status": status,
            "failure_reason": failure_reason,
            "failure_detail": failure_detail,
            "checks": checks,
        }
        payload_hash = canonical_json_hash(payload)
        row = ExecutionTaskPostApplyValidation(
            execution_plan_id=result.execution_plan_id,
            execution_task_id=result.execution_task_id,
            execution_task_attempt_id=result.execution_task_attempt_id,
            attempt_generation=result.attempt_generation,
            apply_result_id=result.id,
            apply_result_hash=result.canonical_sha256,
            apply_attempt_id=result.apply_attempt_id,
            apply_attempt_hash=result.apply_attempt_hash,
            change_set_id=result.change_set_id,
            change_set_hash=result.change_set_hash,
            pre_apply_snapshot_id=snapshot.id if snapshot is not None else None,
            pre_apply_snapshot_hash=(
                snapshot.canonical_sha256 if snapshot is not None else None
            ),
            workspace_target_id=result.workspace_target_id,
            workspace_target_hash=result.workspace_target_hash,
            base_state_id=result.base_state_id,
            base_state_hash=result.base_state_hash,
            validation_policy_id=POST_APPLY_VALIDATION_POLICY_ID,
            validation_policy_version=POST_APPLY_VALIDATION_POLICY_VERSION,
            status=status,
            failure_reason=failure_reason,
            failure_detail=failure_detail,
            checked_operation_count=len(checks),
            canonical_payload=payload,
            canonical_sha256=payload_hash,
            validation_idempotency_key=(
                f"post-apply-validation:{result.id}:"
                f"{POST_APPLY_VALIDATION_POLICY_VERSION}"
            ),
            created_at=self._now(),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = self._existing(result.id)
            if replay is not None and replay.canonical_sha256 == payload_hash:
                return PostApplyValidationOutcome(replay, replayed=True)
            raise PostApplyValidationError(
                "post_apply_validation_insert_conflict",
                "post-apply validation conflicts with canonical authority",
            ) from exc
        return PostApplyValidationOutcome(row)


__all__ = [
    "POST_APPLY_VALIDATION_POLICY_ID",
    "POST_APPLY_VALIDATION_POLICY_VERSION",
    "POST_APPLY_VALIDATION_SCHEMA_VERSION",
    "POST_APPLY_VALIDATION_STATUSES",
    "PostApplyValidationError",
    "PostApplyValidationOutcome",
    "PostApplyValidationService",
    "ValidatePostApplyCommand",
]
