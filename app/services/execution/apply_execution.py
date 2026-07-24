"""Phase 29D-3 atomic Controlled Apply execution.

This module is the first mutating Controlled Apply boundary.  It accepts only
the immutable D-1/D-2 authorities, performs a filesystem-only final
precondition check while holding the existing project mutation lock, and
persists one immutable result.  It never runs Git, a shell, a command, or a
task lifecycle transition.
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
    ExecutionEvidence,
    ExecutionTaskApplyAttempt,
    ExecutionTaskApplyAuthorization,
    ExecutionTaskApplyResult,
    ExecutionTaskApplyApproval,
    ExecutionTaskCandidateContent,
    ExecutionTaskChangeSet,
    ExecutionTaskChangeSetOperation,
    ExecutionWorkspaceBaseState,
    ExecutionWorkspaceTarget,
)
from app.services.execution.candidate_content import (
    CandidateContentError,
    CandidateContentStore,
    LocalContentAddressedStore,
    verify_candidate_content_integrity,
)
from app.services.execution.changeset import (
    ChangeSetError,
    validate_changeset_path,
    verify_change_set_integrity,
)
from app.services.execution.execution_evidence import (
    ExecutionEvidenceError,
    parse_execution_evidence_reference,
    resolve_execution_evidence_reference,
)
from app.services.planning.operator_review import canonical_json_hash
from app.services.workspace.project_mutation_lock import (
    ProjectMutationLockError,
    project_mutation_lock,
)


APPLY_RESULT_SCHEMA_VERSION = "execution-task-apply-result/1.0"
APPLY_RESULT_STATUSES = frozenset({"applied", "blocked", "failed"})
MAX_FAILURE_DETAIL_LENGTH = 1024


class ApplyExecutionError(RuntimeError):
    """A bounded execution failure that must become an immutable result."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ApplyPreconditionBlocked(ApplyExecutionError):
    """Final verification failed before any workspace mutation."""


@dataclass(frozen=True)
class ExecuteApplyCommand:
    apply_attempt_id: int
    owner: str = "controlled-apply"
    execution_actor_type: str = "system"
    execution_actor_id: str = "controlled-apply-v1"
    lock_wait_timeout_seconds: float = 2.0


@dataclass(frozen=True)
class ApplyExecutionResult:
    result: ExecutionTaskApplyResult
    replayed: bool = False


@dataclass
class _PreparedOperation:
    operation: str
    path: Path
    canonical_path: str
    expected_previous_sha256: str | None
    content_reference: str | None
    content_sha256: str | None
    content: bytes | None
    temporary_path: Path | None = None
    backup_path: Path | None = None
    installed: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _short_detail(value: str | None) -> str | None:
    if value is None:
        return None
    return str(value)[:MAX_FAILURE_DETAIL_LENGTH]


def _hash_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ApplyExecutionError(
            "io_failure", "file could not be opened for final verification"
        ) from exc
    digest = hashlib.sha256()
    total = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ApplyPreconditionBlocked(
                "verification_drift", "operation target is not a regular file"
            )
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            total,
        ):
            raise ApplyPreconditionBlocked(
                "verification_drift", "file changed while final hash was computed"
            )
    except ApplyExecutionError:
        raise
    except OSError as exc:
        raise ApplyExecutionError(
            "io_failure", "file could not be hashed for final verification"
        ) from exc
    finally:
        os.close(descriptor)
    return digest.hexdigest(), total


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


class ApplyExecutionService:
    """Execute one immutable D-2 apply attempt under the workspace lock."""

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

    def execute(self, command: ExecuteApplyCommand) -> ApplyExecutionResult:
        attempt = self.db.get(ExecutionTaskApplyAttempt, int(command.apply_attempt_id))
        if attempt is None:
            raise ApplyExecutionError(
                "apply_attempt_missing", "apply attempt does not exist"
            )
        existing = self._existing_result(attempt.id)
        if existing is not None:
            return ApplyExecutionResult(existing, replayed=True)

        started_at = self._now()
        target = self.db.get(ExecutionWorkspaceTarget, attempt.workspace_target_id)
        if target is None:
            return ApplyExecutionResult(
                self._persist_result(
                    attempt,
                    status="blocked",
                    failure_reason="verification_drift",
                    failure_detail="workspace target authority is missing",
                    applied_operations=[],
                    started_at=started_at,
                    ended_at=self._now(),
                    lock_acquired=False,
                )
            )

        root = Path(target.normalized_realpath)
        try:
            with project_mutation_lock(
                project_id=target.project_id,
                project_root=root,
                operation="controlled_apply",
                owner=command.owner,
                wait_timeout_seconds=max(0.0, command.lock_wait_timeout_seconds),
            ):
                replay = self._existing_result(attempt.id)
                if replay is not None:
                    return ApplyExecutionResult(replay, replayed=True)
                try:
                    prepared = self._final_precondition_verification(attempt)
                    applied_operations = self._apply_atomically(prepared)
                    result = self._persist_result(
                        attempt,
                        status="applied",
                        failure_reason=None,
                        failure_detail=None,
                        applied_operations=applied_operations,
                        started_at=started_at,
                        ended_at=self._now(),
                        lock_acquired=True,
                    )
                except ApplyPreconditionBlocked as exc:
                    result = self._persist_result(
                        attempt,
                        status="blocked",
                        failure_reason=exc.code,
                        failure_detail=exc.message,
                        applied_operations=[],
                        started_at=started_at,
                        ended_at=self._now(),
                        lock_acquired=True,
                    )
                except ApplyExecutionError as exc:
                    result = self._persist_result(
                        attempt,
                        status="failed",
                        failure_reason=exc.code,
                        failure_detail=exc.message,
                        applied_operations=[],
                        started_at=started_at,
                        ended_at=self._now(),
                        lock_acquired=True,
                    )
                except Exception as exc:  # pragma: no cover - defensive authority path
                    result = self._persist_result(
                        attempt,
                        status="failed",
                        failure_reason="io_failure",
                        failure_detail=str(exc),
                        applied_operations=[],
                        started_at=started_at,
                        ended_at=self._now(),
                        lock_acquired=True,
                    )
                return ApplyExecutionResult(result)
        except ProjectMutationLockError:
            replay = self._existing_result(attempt.id)
            if replay is not None:
                return ApplyExecutionResult(replay, replayed=True)
            result = self._persist_result(
                attempt,
                status="blocked",
                failure_reason="lock_timeout",
                failure_detail="workspace mutation lock was not acquired",
                applied_operations=[],
                started_at=started_at,
                ended_at=self._now(),
                lock_acquired=False,
            )
            return ApplyExecutionResult(result)

    def _existing_result(
        self, apply_attempt_id: int
    ) -> ExecutionTaskApplyResult | None:
        return (
            self.db.query(ExecutionTaskApplyResult)
            .filter(ExecutionTaskApplyResult.apply_attempt_id == int(apply_attempt_id))
            .one_or_none()
        )

    def _final_precondition_verification(
        self, attempt: ExecutionTaskApplyAttempt
    ) -> list[_PreparedOperation]:
        change_set = self.db.get(ExecutionTaskChangeSet, attempt.change_set_id)
        authorization = self.db.get(
            ExecutionTaskApplyAuthorization, attempt.authorization_id
        )
        approval = self.db.get(ExecutionTaskApplyApproval, attempt.approval_id)
        target = self.db.get(ExecutionWorkspaceTarget, attempt.workspace_target_id)
        base_state = self.db.get(ExecutionWorkspaceBaseState, attempt.base_state_id)
        if any(
            item is None
            for item in (change_set, authorization, approval, target, base_state)
        ):
            raise ApplyPreconditionBlocked(
                "verification_drift", "apply authority is incomplete"
            )
        assert change_set is not None
        assert authorization is not None
        assert approval is not None
        assert target is not None
        assert base_state is not None
        if (
            authorization.authorization_status != "authorized"
            or approval.decision != "approved"
            or attempt.status in {"blocked", "cancelled"}
        ):
            raise ApplyPreconditionBlocked(
                "verification_drift", "authorization or apply attempt is not executable"
            )
        if (
            attempt.change_set_hash != change_set.changeset_sha256
            or attempt.authorization_hash != authorization.canonical_decision_hash
            or attempt.approval_hash != approval.canonical_approval_hash
            or attempt.base_state_hash != base_state.canonical_observation_hash
            or target.target_identity != base_state.target_identity
        ):
            raise ApplyPreconditionBlocked(
                "verification_drift", "immutable apply authority hash or scope changed"
            )
        if not verify_change_set_integrity(
            self.db, change_set.id, store=self.store
        ).verified:
            raise ApplyPreconditionBlocked(
                "verification_drift", "ChangeSet integrity verification failed"
            )

        root = Path(target.normalized_realpath)
        try:
            root_metadata = root.lstat()
        except OSError as exc:
            raise ApplyPreconditionBlocked(
                "verification_drift", "workspace root is unavailable"
            ) from exc
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(
            root_metadata.st_mode
        ):
            raise ApplyPreconditionBlocked(
                "verification_drift", "workspace root is not the authorized directory"
            )
        if target.filesystem_device is not None and str(root_metadata.st_dev) != str(
            target.filesystem_device
        ):
            raise ApplyPreconditionBlocked(
                "verification_drift", "workspace device identity changed"
            )
        if target.filesystem_inode is not None and str(root_metadata.st_ino) != str(
            target.filesystem_inode
        ):
            raise ApplyPreconditionBlocked(
                "verification_drift", "workspace inode identity changed"
            )
        if root.resolve() != Path(target.normalized_realpath):
            raise ApplyPreconditionBlocked(
                "verification_drift", "workspace realpath identity changed"
            )

        operations = (
            self.db.query(ExecutionTaskChangeSetOperation)
            .filter(ExecutionTaskChangeSetOperation.change_set_id == change_set.id)
            .order_by(ExecutionTaskChangeSetOperation.operation_index)
            .all()
        )
        if not operations:
            raise ApplyPreconditionBlocked(
                "verification_drift", "ChangeSet has no executable operations"
            )
        prepared: list[_PreparedOperation] = []
        for row in operations:
            try:
                canonical_path = validate_changeset_path(row.canonical_path)
            except ChangeSetError as exc:
                raise ApplyPreconditionBlocked(
                    "verification_drift", exc.message
                ) from exc
            path = root / canonical_path
            self._verify_parent_directories(root, path)
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                metadata = None
            except OSError as exc:
                raise ApplyExecutionError(
                    "io_failure", "operation target could not be inspected"
                ) from exc
            if metadata is not None and stat.S_ISLNK(metadata.st_mode):
                raise ApplyPreconditionBlocked(
                    "verification_drift", f"symlink operation target: {canonical_path}"
                )
            if row.operation == "create_file":
                if metadata is not None:
                    raise ApplyPreconditionBlocked(
                        "verification_drift",
                        f"create target already exists: {canonical_path}",
                    )
            elif row.operation in {"replace_file", "delete_file"}:
                if metadata is None:
                    raise ApplyPreconditionBlocked(
                        "missing_file", f"expected file is missing: {canonical_path}"
                    )
                if not stat.S_ISREG(metadata.st_mode):
                    raise ApplyPreconditionBlocked(
                        "verification_drift",
                        f"target is not a regular file: {canonical_path}",
                    )
                current_hash, _ = _hash_file(path)
                if current_hash != row.expected_previous_sha256:
                    raise ApplyPreconditionBlocked(
                        "hash_mismatch", f"expected file hash differs: {canonical_path}"
                    )
            else:
                raise ApplyPreconditionBlocked(
                    "verification_drift", f"unsupported operation: {row.operation}"
                )

            content = None
            if row.operation in {"create_file", "replace_file"}:
                content = self._read_content(row.content_reference)
                content_sha256 = hashlib.sha256(content).hexdigest()
                if row.content_sha256 != content_sha256:
                    raise ApplyPreconditionBlocked(
                        "hash_mismatch", f"new content hash differs: {canonical_path}"
                    )
            else:
                content_sha256 = None
            prepared.append(
                _PreparedOperation(
                    operation=row.operation,
                    path=path,
                    canonical_path=canonical_path,
                    expected_previous_sha256=row.expected_previous_sha256,
                    content_reference=row.content_reference,
                    content_sha256=content_sha256,
                    content=content,
                )
            )
        return prepared

    @staticmethod
    def _verify_parent_directories(root: Path, path: Path) -> None:
        current = root
        for part in path.relative_to(root).parts[:-1]:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError as exc:
                raise ApplyPreconditionBlocked(
                    "missing_file", f"parent directory is missing: {current}"
                ) from exc
            except OSError as exc:
                raise ApplyExecutionError(
                    "io_failure", "parent directory could not be inspected"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ApplyPreconditionBlocked(
                    "verification_drift", f"parent path is not a directory: {current}"
                )

    def _read_content(self, reference: str | None) -> bytes:
        if not reference:
            raise ApplyPreconditionBlocked(
                "verification_drift", "file operation has no content reference"
            )
        try:
            if reference.startswith("candidate-content://"):
                identifier = int(reference.rsplit("//", 1)[1])
                row = self.db.get(ExecutionTaskCandidateContent, identifier)
                if row is None:
                    raise ApplyPreconditionBlocked(
                        "missing_file", "candidate content authority is missing"
                    )
                integrity = verify_candidate_content_integrity(
                    self.db, row.id, store=self.store
                )
                if not integrity.verified:
                    raise ApplyPreconditionBlocked(
                        "hash_mismatch", "candidate content integrity failed"
                    )
                data = self.store.read(row.storage_key)
                if hashlib.sha256(data).hexdigest() != row.content_sha256:
                    raise ApplyPreconditionBlocked(
                        "hash_mismatch", "candidate content hash failed"
                    )
                return data
            parsed = parse_execution_evidence_reference(reference)
            if parsed.scheme != "execution-evidence":
                raise ApplyPreconditionBlocked(
                    "verification_drift", "content reference scheme is unsupported"
                )
            resolution = resolve_execution_evidence_reference(
                self.db, reference, store=self.store
            )
            if not resolution.verified or resolution.evidence_id is None:
                raise ApplyPreconditionBlocked(
                    "hash_mismatch", "execution evidence integrity failed"
                )
            row = self.db.get(ExecutionEvidence, resolution.evidence_id)
            if row is None:
                raise ApplyPreconditionBlocked(
                    "missing_file", "execution evidence authority is missing"
                )
            data = self.store.read(row.storage_key)
            if hashlib.sha256(data).hexdigest() != row.content_sha256:
                raise ApplyPreconditionBlocked(
                    "hash_mismatch", "execution evidence hash failed"
                )
            return data
        except ApplyExecutionError:
            raise
        except (CandidateContentError, ExecutionEvidenceError, ValueError) as exc:
            raise ApplyExecutionError(
                "io_failure", "content reference could not be read"
            ) from exc

    def _apply_atomically(
        self, operations: list[_PreparedOperation]
    ) -> list[dict[str, Any]]:
        """Stage all bytes, then commit with compensating cleanup on IO failure."""

        try:
            for item in operations:
                if item.content is None:
                    continue
                descriptor, name = tempfile.mkstemp(
                    dir=item.path.parent, prefix=".orchestrator-apply-"
                )
                item.temporary_path = Path(name)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(item.content)
                    handle.flush()
                    os.fsync(handle.fileno())
            for item in operations:
                if item.operation not in {"replace_file", "delete_file"}:
                    continue
                item.backup_path = _reserve_sibling_path(
                    item.path.parent, ".orchestrator-apply-backup-"
                )
                os.replace(item.path, item.backup_path)
                _fsync_directory(item.path.parent)
                item.installed = True
            for item in operations:
                if item.operation in {"create_file", "replace_file"}:
                    assert item.temporary_path is not None
                    os.replace(item.temporary_path, item.path)
                    item.temporary_path = None
                    _fsync_directory(item.path.parent)
                item.installed = True
            for item in operations:
                if item.backup_path is not None:
                    item.backup_path.unlink(missing_ok=True)
                    item.backup_path = None
                    _fsync_directory(item.path.parent)
        except OSError as exc:
            self._rollback(operations)
            raise ApplyExecutionError(
                "io_failure", "atomic workspace mutation failed"
            ) from exc
        finally:
            for item in operations:
                if item.temporary_path is not None:
                    item.temporary_path.unlink(missing_ok=True)
                    item.temporary_path = None
        return [
            {
                "operation": item.operation,
                "path": item.canonical_path,
                "content_reference": item.content_reference,
                "content_sha256": item.content_sha256,
            }
            for item in operations
        ]

    @staticmethod
    def _rollback(operations: list[_PreparedOperation]) -> None:
        for item in reversed(operations):
            try:
                if item.operation == "create_file" and item.installed:
                    item.path.unlink(missing_ok=True)
                    _fsync_directory(item.path.parent)
                elif item.operation == "replace_file":
                    if item.installed:
                        item.path.unlink(missing_ok=True)
                    if item.backup_path is not None and item.backup_path.exists():
                        os.replace(item.backup_path, item.path)
                    _fsync_directory(item.path.parent)
                elif item.operation == "delete_file":
                    if item.backup_path is not None and item.backup_path.exists():
                        os.replace(item.backup_path, item.path)
                    _fsync_directory(item.path.parent)
            except OSError:
                # The immutable result still records the failed execution.  A
                # rollback/recovery authority is deliberately deferred to D-4.
                continue

    def _persist_result(
        self,
        attempt: ExecutionTaskApplyAttempt,
        *,
        status: str,
        failure_reason: str | None,
        failure_detail: str | None,
        applied_operations: list[dict[str, Any]],
        started_at: datetime,
        ended_at: datetime,
        lock_acquired: bool,
    ) -> ExecutionTaskApplyResult:
        if status not in APPLY_RESULT_STATUSES:
            raise ValueError(f"unsupported apply result status: {status}")
        payload = {
            "schema_version": APPLY_RESULT_SCHEMA_VERSION,
            "execution_plan_id": attempt.execution_plan_id,
            "execution_task_id": attempt.execution_task_id,
            "execution_task_attempt_id": attempt.execution_task_attempt_id,
            "attempt_generation": attempt.attempt_generation,
            "apply_attempt_id": attempt.id,
            "apply_attempt_hash": attempt.canonical_command_hash,
            "change_set_id": attempt.change_set_id,
            "change_set_hash": attempt.change_set_hash,
            "authorization_id": attempt.authorization_id,
            "authorization_hash": attempt.authorization_hash,
            "approval_id": attempt.approval_id,
            "approval_hash": attempt.approval_hash,
            "workspace_target_id": attempt.workspace_target_id,
            "workspace_target_hash": attempt.workspace_target_hash,
            "base_state_id": attempt.base_state_id,
            "base_state_hash": attempt.base_state_hash,
            "status": status,
            "failure_reason": failure_reason,
            "failure_detail": _short_detail(failure_detail),
            "applied_operations": applied_operations,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "lock_acquired": bool(lock_acquired),
        }
        result_hash = canonical_json_hash(payload)
        row = ExecutionTaskApplyResult(
            execution_plan_id=attempt.execution_plan_id,
            execution_task_id=attempt.execution_task_id,
            execution_task_attempt_id=attempt.execution_task_attempt_id,
            attempt_generation=attempt.attempt_generation,
            apply_attempt_id=attempt.id,
            apply_attempt_hash=attempt.canonical_command_hash,
            change_set_id=attempt.change_set_id,
            change_set_hash=attempt.change_set_hash,
            authorization_id=attempt.authorization_id,
            authorization_hash=attempt.authorization_hash,
            approval_id=attempt.approval_id,
            approval_hash=attempt.approval_hash,
            workspace_target_id=attempt.workspace_target_id,
            workspace_target_hash=attempt.workspace_target_hash,
            base_state_id=attempt.base_state_id,
            base_state_hash=attempt.base_state_hash,
            status=status,
            failure_reason=failure_reason,
            failure_detail=_short_detail(failure_detail),
            applied_operations=applied_operations,
            canonical_payload=payload,
            canonical_sha256=result_hash,
            result_idempotency_key=f"apply-result:{attempt.id}",
            started_at=started_at,
            ended_at=ended_at,
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = self._existing_result(attempt.id)
            if replay is not None and replay.canonical_sha256 == result_hash:
                return replay
            raise ApplyExecutionError(
                "result_insert_conflict", "apply result conflicts with authority"
            ) from exc
        return row


@dataclass(frozen=True)
class ApplyResultIntegrity:
    authority_id: int | None
    verified: bool
    issues: tuple[str, ...] = ()


def verify_apply_result_integrity(db: Session, result_id: int) -> ApplyResultIntegrity:
    row = db.get(ExecutionTaskApplyResult, int(result_id))
    if row is None:
        return ApplyResultIntegrity(None, False, ("apply_result_missing",))
    issues: list[str] = []
    if canonical_json_hash(row.canonical_payload) != row.canonical_sha256:
        issues.append("apply_result_canonical_hash_mismatch")
    if row.canonical_payload.get("status") != row.status:
        issues.append("apply_result_status_mismatch")
    if row.canonical_payload.get("applied_operations") != row.applied_operations:
        issues.append("apply_result_operations_mismatch")
    if row.canonical_payload.get("apply_attempt_id") != row.apply_attempt_id:
        issues.append("apply_result_attempt_mismatch")
    if row.canonical_payload.get("apply_attempt_hash") != row.apply_attempt_hash:
        issues.append("apply_result_attempt_hash_mismatch")
    if row.canonical_payload.get("change_set_hash") != row.change_set_hash:
        issues.append("apply_result_changeset_hash_mismatch")
    if row.canonical_payload.get("authorization_hash") != row.authorization_hash:
        issues.append("apply_result_authorization_hash_mismatch")
    if row.canonical_payload.get("base_state_hash") != row.base_state_hash:
        issues.append("apply_result_base_state_hash_mismatch")
    if row.status == "applied" and row.failure_reason is not None:
        issues.append("apply_result_applied_failure_reason")
    if row.status in {"blocked", "failed"} and row.failure_reason is None:
        issues.append("apply_result_failure_reason_missing")
    attempt = db.get(ExecutionTaskApplyAttempt, row.apply_attempt_id)
    if attempt is None:
        issues.append("apply_result_attempt_missing")
    else:
        if attempt.canonical_command_hash != row.apply_attempt_hash:
            issues.append("apply_result_attempt_linkage_mismatch")
        if attempt.change_set_id != row.change_set_id:
            issues.append("apply_result_changeset_linkage_mismatch")
        if attempt.authorization_id != row.authorization_id:
            issues.append("apply_result_authorization_linkage_mismatch")
        if attempt.base_state_id != row.base_state_id:
            issues.append("apply_result_base_state_linkage_mismatch")
    return ApplyResultIntegrity(row.id, not issues, tuple(sorted(set(issues))))


__all__ = [
    "APPLY_RESULT_SCHEMA_VERSION",
    "ApplyExecutionError",
    "ApplyExecutionResult",
    "ApplyExecutionService",
    "ApplyPreconditionBlocked",
    "ApplyResultIntegrity",
    "ExecuteApplyCommand",
    "verify_apply_result_integrity",
]
