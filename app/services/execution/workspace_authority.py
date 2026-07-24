"""Read-only workspace target and base-state authorities for Phase 29D-2.

This module is deliberately narrower than the existing workspace services.  It
does not allocate sandboxes, restore checkpoints, acquire write locks, invoke
candidate commands, or modify a repository.  It resolves one configured
project target, observes a bounded Git worktree, hashes only ChangeSet paths,
and persists immutable observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Any, Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionTaskChangeSet,
    ExecutionTaskChangeSetOperation,
    ExecutionWorkspaceBaseState,
    ExecutionWorkspacePathObservation,
    ExecutionWorkspaceTarget,
    Project,
)
from app.services.planning.operator_review import canonical_json_hash
from app.services.workspace.system_settings import get_effective_workspace_root


WORKSPACE_TARGET_SCHEMA_VERSION = "execution-workspace-target/1.0"
BASE_STATE_SCHEMA_VERSION = "execution-workspace-base-state/1.0"
INSPECTION_POLICY_ID = "controlled_apply_read_only_inspection"
INSPECTION_POLICY_VERSION = 1
INSPECTOR_TOOL_ID = "orchestrator-read-only-workspace-inspector"
INSPECTOR_TOOL_VERSION = "1"
REPOSITORY_KIND_GIT_WORKTREE = "git_worktree"
REPOSITORY_KIND_NON_GIT = "non_git"
MAX_GIT_OUTPUT_BYTES = 512 * 1024
MAX_DIRTY_PATHS = 512
MAX_DIRTY_PATH_LENGTH = 1024
MAX_RELEVANT_PATHS = 200
MAX_FILE_HASH_BYTES = 8 * 1024 * 1024
MAX_GIT_TIMEOUT_SECONDS = 5
MAX_GIT_HASH_LENGTH = 128
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_HEAD_RE = re.compile(r"^[0-9a-f]{40,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PROTECTED_REPOSITORY_STATE_NAMES = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "BISECT_START",
)
_ALLOWED_GIT_COMMANDS = frozenset(
    {
        ("rev-parse", "--show-toplevel"),
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "-z"),
    }
)


class WorkspaceAuthorityError(RuntimeError):
    """Fail-closed bounded workspace authority failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class WorkspaceTargetObservation:
    configured_path: str
    normalized_realpath: str
    filesystem_device: str | None
    filesystem_inode: str | None
    repository_kind: str
    repository_identity: str
    repository_root_realpath: str
    repository_root_identity: str
    target_payload: dict[str, Any]
    target_hash: str
    target_identity: str


@dataclass(frozen=True)
class PathObservation:
    operation: str
    path: str
    exists: bool
    entry_type: str
    content_sha256: str | None
    byte_length: int | None
    mode_classification: str | None
    symlink_status: str

    def payload(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "path": self.path,
            "exists": self.exists,
            "entry_type": self.entry_type,
            "content_sha256": self.content_sha256,
            "byte_length": self.byte_length,
            "mode_classification": self.mode_classification,
            "symlink_status": self.symlink_status,
        }


@dataclass(frozen=True)
class WorkspaceObservation:
    target: WorkspaceTargetObservation
    repository_head: str
    workspace_clean: bool
    dirty_state: str
    dirty_paths: tuple[str, ...]
    dirty_path_summary_hash: str
    repository_operation_state: dict[str, bool]
    path_observations: tuple[PathObservation, ...]
    canonical_payload: dict[str, Any]
    canonical_hash: str


@dataclass(frozen=True)
class WorkspaceTargetRegistrationResult:
    target: ExecutionWorkspaceTarget
    replayed: bool = False


@dataclass(frozen=True)
class BaseStateObservationResult:
    base_state: ExecutionWorkspaceBaseState
    replayed: bool = False


def _hash(value: Any) -> str:
    return canonical_json_hash(value)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_text(
    value: bytes, code: str, *, max_bytes: int = MAX_GIT_OUTPUT_BYTES
) -> str:
    if len(value) > max_bytes:
        raise WorkspaceAuthorityError(code, "bounded command output was exceeded")
    try:
        result = value.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise WorkspaceAuthorityError(
            code, "command output was not valid UTF-8"
        ) from exc
    if _CONTROL_RE.search(result):
        raise WorkspaceAuthorityError(code, "command output contained control data")
    return result


def _path_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _configured_workspace_candidate(
    project: Project, db: Session
) -> tuple[str, Path, Path]:
    raw = str(project.workspace_path or "").strip()
    if not raw or _CONTROL_RE.search(raw):
        raise WorkspaceAuthorityError(
            "workspace_target_missing_path",
            "project workspace path is missing or malformed",
        )
    root = get_effective_workspace_root(db=db).resolve()
    if "\\" in raw:
        raise WorkspaceAuthorityError(
            "workspace_target_path_invalid",
            "workspace path must use platform separators",
        )
    configured = Path(raw).expanduser()
    if not configured.is_absolute():
        if any(part in {"", ".", ".."} for part in configured.parts):
            raise WorkspaceAuthorityError(
                "workspace_target_path_invalid",
                "relative workspace path has unsafe segments",
            )
        configured = root / configured
    else:
        configured = configured.absolute()
    if not _path_under(root, configured):
        raise WorkspaceAuthorityError(
            "workspace_target_escapes_allowed_root",
            "configured workspace path is outside the allowed workspace root",
        )
    realpath = Path(os.path.realpath(configured))
    if not realpath.exists():
        raise WorkspaceAuthorityError(
            "workspace_target_missing", "configured workspace path does not exist"
        )
    if not realpath.is_dir():
        raise WorkspaceAuthorityError(
            "workspace_target_not_directory",
            "configured workspace path is not a directory",
        )
    if configured != realpath:
        raise WorkspaceAuthorityError(
            "workspace_target_symlink_path",
            "configured workspace path resolves through a symlink",
        )
    if not _path_under(root, realpath):
        raise WorkspaceAuthorityError(
            "workspace_target_escapes_allowed_root",
            "resolved workspace path is outside the allowed workspace root",
        )
    return raw, configured, realpath


def _stat_identity(path: Path) -> tuple[str | None, str | None]:
    try:
        value = path.stat()
    except OSError as exc:
        raise WorkspaceAuthorityError(
            "workspace_target_stat_failed",
            "workspace target metadata could not be read",
        ) from exc
    return (
        str(getattr(value, "st_dev", "")) or None,
        str(getattr(value, "st_ino", "")) or None,
    )


def _safe_git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
        "LANG": "C",
        "PATH": os.environ.get("PATH", ""),
    }


def _run_git(root: Path, args: tuple[str, ...]) -> bytes:
    if args not in _ALLOWED_GIT_COMMANDS:
        raise WorkspaceAuthorityError(
            "repository_command_not_allowed", "Git command is not allowlisted"
        )
    executable = shutil.which("git")
    if not executable:
        raise WorkspaceAuthorityError(
            "git_unavailable", "Git executable is unavailable"
        )
    try:
        completed = subprocess.run(
            [executable, *args],
            cwd=str(root),
            env=_safe_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=MAX_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceAuthorityError(
            "repository_inspection_timeout", "Git inspection timed out"
        ) from exc
    except OSError as exc:
        raise WorkspaceAuthorityError(
            "repository_inspection_failed", "Git inspection could not start"
        ) from exc
    if (
        len(completed.stdout or b"") > MAX_GIT_OUTPUT_BYTES
        or len(completed.stderr or b"") > MAX_GIT_OUTPUT_BYTES
    ):
        raise WorkspaceAuthorityError(
            "repository_inspection_output_bounded", "Git output exceeded the bound"
        )
    if completed.returncode != 0:
        if args == ("rev-parse", "--show-toplevel"):
            raise WorkspaceAuthorityError(
                "workspace_non_git_unsupported", "workspace is not a Git worktree"
            )
        if args == ("rev-parse", "HEAD"):
            raise WorkspaceAuthorityError(
                "repository_head_unavailable", "Git HEAD is unavailable"
            )
        raise WorkspaceAuthorityError(
            "repository_status_unavailable", "Git status could not be inspected"
        )
    return bytes(completed.stdout or b"")


def _repository_operation_state(root: Path) -> dict[str, bool]:
    git_entry = root / ".git"
    try:
        entry = git_entry.lstat()
    except OSError as exc:
        raise WorkspaceAuthorityError(
            "workspace_not_git_worktree", "Git metadata entry is missing"
        ) from exc
    if stat.S_ISLNK(entry.st_mode):
        raise WorkspaceAuthorityError(
            "workspace_git_metadata_symlink", "Git metadata entry is a symlink"
        )
    git_dir = git_entry
    if stat.S_ISREG(entry.st_mode):
        try:
            pointer = git_entry.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise WorkspaceAuthorityError(
                "workspace_git_metadata_invalid", "Git worktree pointer is unreadable"
            ) from exc
        if not pointer.startswith("gitdir: ") or len(pointer.encode("utf-8")) > 4096:
            raise WorkspaceAuthorityError(
                "workspace_git_metadata_invalid", "Git worktree pointer is invalid"
            )
        git_dir = Path(pointer.splitlines()[0][8:].strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
        else:
            git_dir = git_dir.resolve()
    if not git_dir.is_dir():
        raise WorkspaceAuthorityError(
            "workspace_git_metadata_invalid", "Git metadata directory is missing"
        )
    state: dict[str, bool] = {}
    for name in _PROTECTED_REPOSITORY_STATE_NAMES:
        try:
            state[name.lower()] = (git_dir / name).exists()
        except OSError as exc:
            raise WorkspaceAuthorityError(
                "repository_operation_state_unreadable",
                "Git operation state is unreadable",
            ) from exc
    if any(state.values()):
        raise WorkspaceAuthorityError(
            "repository_operation_in_progress",
            "Git repository operation is in progress",
        )
    return state


def _canonical_dirty_path(value: str) -> str:
    if not value or len(value) > MAX_DIRTY_PATH_LENGTH or _CONTROL_RE.search(value):
        raise WorkspaceAuthorityError(
            "dirty_output_invalid", "Git dirty path is malformed"
        )
    if value.startswith("/") or "\\" in value:
        raise WorkspaceAuthorityError(
            "dirty_output_invalid", "Git dirty path is not relative"
        )
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise WorkspaceAuthorityError(
            "dirty_output_invalid", "Git dirty path contains unsafe segments"
        )
    return "/".join(parts)


def _read_dirty_paths(root: Path) -> tuple[str, ...]:
    raw = _run_git(root, ("status", "--porcelain=v1", "-z"))
    tokens = raw.split(b"\0")
    paths: list[str] = []
    for token in tokens:
        if not token:
            continue
        try:
            text = token.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WorkspaceAuthorityError(
                "dirty_output_invalid", "Git dirty path is not UTF-8"
            ) from exc
        if len(text) < 4 or text[2] != " ":
            raise WorkspaceAuthorityError(
                "dirty_output_invalid", "Git porcelain output is malformed"
            )
        paths.append(_canonical_dirty_path(text[3:]))
        if len(paths) > MAX_DIRTY_PATHS:
            raise WorkspaceAuthorityError(
                "dirty_output_bounded", "Git dirty path count exceeded the bound"
            )
    return tuple(sorted(set(paths)))


def _repository_identity(root: Path) -> tuple[str, str, str]:
    device, inode = _stat_identity(root)
    git_entry = root / ".git"
    git_device, git_inode = _stat_identity(git_entry)
    root_payload = {
        "repository_root_realpath": str(root),
        "repository_root_device": device,
        "repository_root_inode": inode,
        "git_metadata_device": git_device,
        "git_metadata_inode": git_inode,
    }
    root_hash = _hash(root_payload)
    identity = f"repository-sha256:{root_hash}"
    return identity, f"repository-root-sha256:{root_hash}", root_hash


def inspect_workspace_target(
    project: Project, db: Session
) -> WorkspaceTargetObservation:
    raw, _configured, realpath = _configured_workspace_candidate(project, db)
    root_output = _safe_text(
        _run_git(realpath, ("rev-parse", "--show-toplevel")), "repository_root_invalid"
    )
    repository_root = Path(root_output).resolve()
    if repository_root != realpath:
        raise WorkspaceAuthorityError(
            "repository_root_mismatch",
            "Git root differs from authorized workspace root",
        )
    head = _safe_text(
        _run_git(realpath, ("rev-parse", "HEAD")),
        "repository_head_invalid",
        max_bytes=MAX_GIT_HASH_LENGTH,
    )
    if not _GIT_HEAD_RE.fullmatch(head):
        raise WorkspaceAuthorityError(
            "repository_head_unavailable", "Git HEAD is not a full commit hash"
        )
    _repository_operation_state(realpath)
    device, inode = _stat_identity(realpath)
    repository_identity, repository_root_identity, _ = _repository_identity(realpath)
    payload = {
        "schema_version": WORKSPACE_TARGET_SCHEMA_VERSION,
        "project_id": int(project.id),
        "configured_workspace_path": raw,
        "normalized_realpath": str(realpath),
        "filesystem_device": device,
        "filesystem_inode": inode,
        "repository_kind": REPOSITORY_KIND_GIT_WORKTREE,
        "repository_identity": repository_identity,
        "repository_root_realpath": str(repository_root),
        "repository_root_identity": repository_root_identity,
    }
    target_hash = _hash(payload)
    return WorkspaceTargetObservation(
        configured_path=raw,
        normalized_realpath=str(realpath),
        filesystem_device=device,
        filesystem_inode=inode,
        repository_kind=REPOSITORY_KIND_GIT_WORKTREE,
        repository_identity=repository_identity,
        repository_root_realpath=str(repository_root),
        repository_root_identity=repository_root_identity,
        target_payload=payload,
        target_hash=target_hash,
        target_identity=f"workspace-sha256:{target_hash}",
    )


def _collision_realpath(project: Project, db: Session) -> str | None:
    raw = str(project.workspace_path or "").strip()
    if not raw or _CONTROL_RE.search(raw):
        return None
    root = get_effective_workspace_root(db=db).resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    else:
        candidate = candidate.absolute()
    if not _path_under(root, candidate):
        return None
    resolved = Path(os.path.realpath(candidate))
    if (
        not resolved.exists()
        or not resolved.is_dir()
        or not _path_under(root, resolved)
    ):
        return None
    return str(resolved)


def _assert_no_active_project_collision(
    project: Project, db: Session, realpath: str
) -> None:
    projects = (
        db.query(Project)
        .filter(Project.deleted_at.is_(None), Project.id != int(project.id))
        .all()
    )
    for other in projects:
        other_realpath = _collision_realpath(other, db)
        if other_realpath == realpath:
            raise WorkspaceAuthorityError(
                "workspace_target_collision",
                "another active project resolves to the same workspace",
            )


class WorkspaceTargetService:
    """Register one independently inspected immutable workspace target."""

    def __init__(self, db: Session, *, now: Any = None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    def register(
        self,
        project_id: int,
        *,
        registration_idempotency_key: str,
        creation_actor_type: str = "operator",
        creation_actor_id: str = "system",
    ) -> WorkspaceTargetRegistrationResult:
        project = self.db.get(Project, int(project_id))
        if project is None or project.deleted_at is not None:
            raise WorkspaceAuthorityError(
                "workspace_target_project_missing", "project is missing or deleted"
            )
        observation = inspect_workspace_target(project, self.db)
        _assert_no_active_project_collision(
            project, self.db, observation.normalized_realpath
        )
        payload = {
            **observation.target_payload,
            "registration_idempotency_key": registration_idempotency_key,
            "creation_actor_type": creation_actor_type,
            "creation_actor_id": creation_actor_id,
        }
        command_hash = _hash(payload)
        existing = (
            self.db.query(ExecutionWorkspaceTarget)
            .filter(
                ExecutionWorkspaceTarget.registration_idempotency_key
                == registration_idempotency_key
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_target_hash != observation.target_hash:
                raise WorkspaceAuthorityError(
                    "workspace_target_idempotency_conflict",
                    "registration key is bound to a different target",
                )
            return WorkspaceTargetRegistrationResult(existing, replayed=True)
        existing_identity = (
            self.db.query(ExecutionWorkspaceTarget)
            .filter(
                ExecutionWorkspaceTarget.project_id == project.id,
                ExecutionWorkspaceTarget.target_identity == observation.target_identity,
            )
            .one_or_none()
        )
        if existing_identity is not None:
            return WorkspaceTargetRegistrationResult(existing_identity, replayed=True)
        row = ExecutionWorkspaceTarget(
            project_id=project.id,
            authority_version=1,
            target_status="active",
            configured_workspace_path=observation.configured_path,
            normalized_realpath=observation.normalized_realpath,
            filesystem_device=observation.filesystem_device,
            filesystem_inode=observation.filesystem_inode,
            target_identity=observation.target_identity,
            repository_kind=observation.repository_kind,
            repository_identity=observation.repository_identity,
            repository_root_realpath=observation.repository_root_realpath,
            repository_root_identity=observation.repository_root_identity,
            canonical_target_payload=observation.target_payload,
            canonical_target_hash=observation.target_hash,
            registration_idempotency_key=registration_idempotency_key,
            creation_actor_type=creation_actor_type,
            creation_actor_id=creation_actor_id,
            created_at=self._now(),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(ExecutionWorkspaceTarget)
                .filter(
                    ExecutionWorkspaceTarget.registration_idempotency_key
                    == registration_idempotency_key
                )
                .one_or_none()
            )
            if (
                replay is not None
                and replay.canonical_target_hash == observation.target_hash
            ):
                return WorkspaceTargetRegistrationResult(replay, replayed=True)
            raise WorkspaceAuthorityError(
                "workspace_target_insert_conflict",
                "target conflicts with canonical authority",
            ) from exc
        return WorkspaceTargetRegistrationResult(row)


def _safe_operation_path(root: Path, path: str) -> Path:
    candidate = root / path
    absolute = Path(os.path.abspath(candidate))
    if not _path_under(root, absolute):
        raise WorkspaceAuthorityError(
            "operation_path_escape", "ChangeSet path escapes workspace root"
        )
    return absolute


def _mode_classification(mode: int) -> str:
    return (
        "executable"
        if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else "regular"
    )


def _inspect_operation_path(root: Path, operation: str, path: str) -> PathObservation:
    full = _safe_operation_path(root, path)
    relative_parts = full.relative_to(root).parts
    current = root
    for part in relative_parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return PathObservation(
                operation, path, False, "missing", None, None, None, "not_symlink"
            )
        except OSError as exc:
            raise WorkspaceAuthorityError(
                "operation_path_unreadable", "operation path metadata is unreadable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceAuthorityError(
                "operation_path_symlink", "symlink operation targets are unsupported"
            )
    try:
        metadata = full.lstat()
    except FileNotFoundError:
        return PathObservation(
            operation, path, False, "missing", None, None, None, "not_symlink"
        )
    if stat.S_ISDIR(metadata.st_mode):
        return PathObservation(
            operation, path, True, "directory", None, None, None, "not_symlink"
        )
    if not stat.S_ISREG(metadata.st_mode):
        return PathObservation(
            operation, path, True, "special", None, None, None, "not_symlink"
        )
    if metadata.st_size > MAX_FILE_HASH_BYTES:
        raise WorkspaceAuthorityError(
            "operation_file_too_large", "operation file exceeds hashing bound"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(full, flags)
    except FileNotFoundError:
        return PathObservation(
            operation, path, False, "missing", None, None, None, "not_symlink"
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise WorkspaceAuthorityError(
                "operation_path_symlink", "symlink operation targets are unsupported"
            ) from exc
        raise WorkspaceAuthorityError(
            "operation_path_unreadable", "operation file cannot be opened read-only"
        ) from exc
    digest = hashlib.sha256()
    total = 0
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_HASH_BYTES:
            raise WorkspaceAuthorityError(
                "operation_file_invalid",
                "operation target changed to an unsupported file",
            )
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_HASH_BYTES:
                raise WorkspaceAuthorityError(
                    "operation_file_too_large", "operation file exceeds hashing bound"
                )
            digest.update(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            total,
        ):
            raise WorkspaceAuthorityError(
                "operation_file_changed_during_read",
                "operation file changed during hashing",
            )
    finally:
        os.close(fd)
    return PathObservation(
        operation,
        path,
        True,
        "regular_file",
        digest.hexdigest(),
        total,
        _mode_classification(metadata.st_mode),
        "not_symlink",
    )


def _paths_conflict(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _observe_workspace(
    target: ExecutionWorkspaceTarget, change_set: ExecutionTaskChangeSet, db: Session
) -> WorkspaceObservation:
    project = db.get(Project, target.project_id)
    if project is None or project.deleted_at is not None:
        raise WorkspaceAuthorityError(
            "workspace_target_project_missing", "target project is missing or deleted"
        )
    current_target = inspect_workspace_target(project, db)
    if current_target.target_identity != target.target_identity:
        raise WorkspaceAuthorityError(
            "workspace_target_identity_changed",
            "project target no longer matches authority",
        )
    if current_target.normalized_realpath != target.normalized_realpath:
        raise WorkspaceAuthorityError(
            "workspace_target_identity_changed",
            "workspace realpath no longer matches authority",
        )
    if current_target.target_hash != target.canonical_target_hash:
        raise WorkspaceAuthorityError(
            "workspace_target_integrity_failure",
            "workspace target canonical hash does not match",
        )
    root = Path(target.normalized_realpath)
    operation_rows = (
        db.query(ExecutionTaskChangeSetOperation)
        .filter(ExecutionTaskChangeSetOperation.change_set_id == change_set.id)
        .order_by(ExecutionTaskChangeSetOperation.operation_index)
        .all()
    )
    if not operation_rows or len(operation_rows) > MAX_RELEVANT_PATHS:
        raise WorkspaceAuthorityError(
            "operation_path_bound_exceeded",
            "ChangeSet path count is outside inspection bounds",
        )
    dirty_paths = _read_dirty_paths(root)
    operation_paths = tuple(str(row.canonical_path) for row in operation_rows)
    dirty_state = "clean"
    if dirty_paths:
        dirty_state = (
            "conflicting_dirty"
            if any(
                _paths_conflict(dirty, operation_path)
                for dirty in dirty_paths
                for operation_path in operation_paths
            )
            else "unrelated_dirty"
        )
    operation_state = _repository_operation_state(root)
    path_observations = tuple(
        _inspect_operation_path(root, str(row.operation), str(row.canonical_path))
        for row in operation_rows
    )
    dirty_hash = _hash({"dirty_paths": list(dirty_paths)})
    payload = {
        "schema_version": BASE_STATE_SCHEMA_VERSION,
        "project_id": int(project.id),
        "change_set_id": int(change_set.id),
        "change_set_hash": change_set.changeset_sha256,
        "target_identity": target.target_identity,
        "target_hash": target.canonical_target_hash,
        "repository_kind": target.repository_kind,
        "repository_identity": target.repository_identity,
        "repository_root_identity": target.repository_root_identity,
        "repository_head": _safe_text(
            _run_git(root, ("rev-parse", "HEAD")),
            "repository_head_invalid",
            max_bytes=MAX_GIT_HASH_LENGTH,
        ),
        "workspace_clean": not bool(dirty_paths),
        "dirty_state": dirty_state,
        "dirty_paths": list(dirty_paths),
        "dirty_path_summary_hash": dirty_hash,
        "repository_operation_state": operation_state,
        "path_observations": [item.payload() for item in path_observations],
        "inspection_policy_id": INSPECTION_POLICY_ID,
        "inspection_policy_version": INSPECTION_POLICY_VERSION,
        "tool_identity": INSPECTOR_TOOL_ID,
        "tool_version": INSPECTOR_TOOL_VERSION,
    }
    return WorkspaceObservation(
        target=current_target,
        repository_head=str(payload["repository_head"]),
        workspace_clean=not bool(dirty_paths),
        dirty_state=dirty_state,
        dirty_paths=dirty_paths,
        dirty_path_summary_hash=dirty_hash,
        repository_operation_state=operation_state,
        path_observations=path_observations,
        canonical_payload=payload,
        canonical_hash=_hash(payload),
    )


class WorkspaceBaseStateService:
    """Create or replay an immutable read-only base-state observation."""

    def __init__(self, db: Session, *, now: Any = None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    def inspect(
        self,
        *,
        workspace_target_id: int,
        change_set_id: int,
        observation_idempotency_key: str,
        creation_actor_type: str = "operator",
        creation_actor_id: str = "system",
    ) -> BaseStateObservationResult:
        target = self.db.get(ExecutionWorkspaceTarget, int(workspace_target_id))
        change_set = self.db.get(ExecutionTaskChangeSet, int(change_set_id))
        if target is None or change_set is None:
            raise WorkspaceAuthorityError(
                "workspace_base_state_authority_missing",
                "target or ChangeSet is missing",
            )
        if change_set.target_project_id != target.project_id:
            raise WorkspaceAuthorityError(
                "workspace_base_state_linkage_mismatch",
                "ChangeSet target project differs from workspace target",
            )
        observation = _observe_workspace(target, change_set, self.db)
        existing = (
            self.db.query(ExecutionWorkspaceBaseState)
            .filter(
                ExecutionWorkspaceBaseState.observation_idempotency_key
                == observation_idempotency_key
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_observation_hash != observation.canonical_hash:
                raise WorkspaceAuthorityError(
                    "workspace_base_state_idempotency_conflict",
                    "observation key is bound to a different state",
                )
            return BaseStateObservationResult(existing, replayed=True)
        equivalent = (
            self.db.query(ExecutionWorkspaceBaseState)
            .filter(
                ExecutionWorkspaceBaseState.workspace_target_id == target.id,
                ExecutionWorkspaceBaseState.change_set_id == change_set.id,
                ExecutionWorkspaceBaseState.canonical_observation_hash
                == observation.canonical_hash,
            )
            .one_or_none()
        )
        if equivalent is not None:
            return BaseStateObservationResult(equivalent, replayed=True)
        now = self._now()
        row = ExecutionWorkspaceBaseState(
            workspace_target_id=target.id,
            project_id=target.project_id,
            change_set_id=change_set.id,
            target_identity=target.target_identity,
            repository_kind=target.repository_kind,
            repository_identity=target.repository_identity,
            repository_root_identity=target.repository_root_identity,
            repository_head=observation.repository_head,
            workspace_clean=observation.workspace_clean,
            dirty_state=observation.dirty_state,
            dirty_path_count=len(observation.dirty_paths),
            dirty_paths=list(observation.dirty_paths),
            dirty_path_summary_hash=observation.dirty_path_summary_hash,
            repository_operation_state=observation.repository_operation_state,
            inspection_policy_id=INSPECTION_POLICY_ID,
            inspection_policy_version=INSPECTION_POLICY_VERSION,
            tool_identity=INSPECTOR_TOOL_ID,
            tool_version=INSPECTOR_TOOL_VERSION,
            path_observation_count=len(observation.path_observations),
            canonical_observation_payload=observation.canonical_payload,
            canonical_observation_hash=observation.canonical_hash,
            observation_idempotency_key=observation_idempotency_key,
            creation_actor_type=creation_actor_type,
            creation_actor_id=creation_actor_id,
            created_at=now,
            inspected_at=now,
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
                for index, item in enumerate(observation.path_observations):
                    payload = item.payload()
                    self.db.add(
                        ExecutionWorkspacePathObservation(
                            base_state_id=row.id,
                            observation_index=index,
                            operation=item.operation,
                            path=item.path,
                            exists=item.exists,
                            entry_type=item.entry_type,
                            content_sha256=item.content_sha256,
                            byte_length=item.byte_length,
                            mode_classification=item.mode_classification,
                            symlink_status=item.symlink_status,
                            canonical_observation_payload=payload,
                            canonical_observation_hash=_hash(payload),
                        )
                    )
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(ExecutionWorkspaceBaseState)
                .filter(
                    ExecutionWorkspaceBaseState.observation_idempotency_key
                    == observation_idempotency_key
                )
                .one_or_none()
            )
            if (
                replay is not None
                and replay.canonical_observation_hash == observation.canonical_hash
            ):
                return BaseStateObservationResult(replay, replayed=True)
            raise WorkspaceAuthorityError(
                "workspace_base_state_insert_conflict",
                "base state conflicts with canonical authority",
            ) from exc
        return BaseStateObservationResult(row)

    def observe_current(
        self, workspace_target_id: int, change_set_id: int
    ) -> WorkspaceObservation:
        target = self.db.get(ExecutionWorkspaceTarget, int(workspace_target_id))
        change_set = self.db.get(ExecutionTaskChangeSet, int(change_set_id))
        if target is None or change_set is None:
            raise WorkspaceAuthorityError(
                "workspace_base_state_authority_missing",
                "target or ChangeSet is missing",
            )
        if change_set.target_project_id != target.project_id:
            raise WorkspaceAuthorityError(
                "workspace_base_state_linkage_mismatch",
                "ChangeSet target project differs from workspace target",
            )
        return _observe_workspace(target, change_set, self.db)


@dataclass(frozen=True)
class WorkspaceIntegrityResult:
    authority_id: int | None
    verified: bool
    issues: tuple[str, ...] = ()


def verify_workspace_target_integrity(
    db: Session, target_id: int
) -> WorkspaceIntegrityResult:
    row = db.get(ExecutionWorkspaceTarget, int(target_id))
    if row is None:
        return WorkspaceIntegrityResult(None, False, ("workspace_target_missing",))
    issues: list[str] = []
    if _hash(row.canonical_target_payload) != row.canonical_target_hash:
        issues.append("workspace_target_canonical_hash_mismatch")
    if row.target_identity != f"workspace-sha256:{row.canonical_target_hash}":
        issues.append("workspace_target_identity_hash_mismatch")
    project = db.get(Project, row.project_id)
    if project is None:
        issues.append("workspace_target_project_missing")
    elif row.canonical_target_payload.get("project_id") != project.id:
        issues.append("workspace_target_project_linkage_mismatch")
    duplicate = (
        db.query(ExecutionWorkspaceTarget)
        .filter(ExecutionWorkspaceTarget.target_identity == row.target_identity)
        .count()
    )
    if duplicate != 1:
        issues.append("workspace_target_duplicate_identity")
    return WorkspaceIntegrityResult(row.id, not issues, tuple(sorted(set(issues))))


def verify_workspace_base_state_integrity(
    db: Session, base_state_id: int
) -> WorkspaceIntegrityResult:
    row = db.get(ExecutionWorkspaceBaseState, int(base_state_id))
    if row is None:
        return WorkspaceIntegrityResult(None, False, ("workspace_base_state_missing",))
    issues: list[str] = []
    target = db.get(ExecutionWorkspaceTarget, row.workspace_target_id)
    change_set = db.get(ExecutionTaskChangeSet, row.change_set_id)
    if target is None:
        issues.append("workspace_base_state_target_missing")
    elif row.target_identity != target.target_identity:
        issues.append("workspace_base_state_target_identity_mismatch")
    if change_set is None:
        issues.append("workspace_base_state_changeset_missing")
    payload = row.canonical_observation_payload
    if _hash(payload) != row.canonical_observation_hash:
        issues.append("workspace_base_state_canonical_hash_mismatch")
    paths = (
        db.query(ExecutionWorkspacePathObservation)
        .filter(ExecutionWorkspacePathObservation.base_state_id == row.id)
        .order_by(ExecutionWorkspacePathObservation.observation_index)
        .all()
    )
    if len(paths) != row.path_observation_count:
        issues.append("workspace_base_state_path_count_mismatch")
    payload_paths = (
        payload.get("path_observations", []) if isinstance(payload, dict) else []
    )
    if len(payload_paths) != len(paths):
        issues.append("workspace_base_state_path_payload_mismatch")
    for item in paths:
        if _hash(item.canonical_observation_payload) != item.canonical_observation_hash:
            issues.append(f"workspace_path_observation_hash_mismatch:{item.id}")
    return WorkspaceIntegrityResult(row.id, not issues, tuple(sorted(set(issues))))


def workspace_authority_retention_order() -> tuple[str, ...]:
    """Return authority ownership order; cleanup is intentionally separate."""

    return (
        "execution_workspace_targets",
        "execution_workspace_base_states",
        "execution_task_change_sets",
        "execution_task_apply_authorizations",
        "execution_task_apply_approvals",
        "execution_task_apply_attempts",
        "execution_task_apply_precondition_verifications",
        "execution_task_apply_results",
    )


__all__ = [
    "BASE_STATE_SCHEMA_VERSION",
    "INSPECTION_POLICY_ID",
    "INSPECTION_POLICY_VERSION",
    "INSPECTOR_TOOL_ID",
    "INSPECTOR_TOOL_VERSION",
    "MAX_DIRTY_PATHS",
    "MAX_FILE_HASH_BYTES",
    "REPOSITORY_KIND_GIT_WORKTREE",
    "WorkspaceAuthorityError",
    "WorkspaceBaseStateService",
    "WorkspaceObservation",
    "WorkspaceTargetObservation",
    "WorkspaceTargetRegistrationResult",
    "WorkspaceTargetService",
    "inspect_workspace_target",
    "verify_workspace_base_state_integrity",
    "verify_workspace_target_integrity",
    "workspace_authority_retention_order",
]
