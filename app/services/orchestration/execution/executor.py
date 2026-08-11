"""Executor-stage helpers for orchestration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import LogEntry
from app.services.orchestration.operations.file_ops_contract import (
    CONTENT_FILE_OPS,
    SUPPORTED_FILE_OPS,
)
from app.services.orchestration.operations.patch_python import try_deterministic_patch
from app.services.workspace.permissions import (
    ensure_shared_path_to_root,
    ensure_shared_permissions,
)
from app.services.orchestration.validation.accepted_path_authority import (
    AcceptedPathAuthority,
)
from app.services.orchestration.validation.path_authority import (
    EntryType,
    GrantClass,
    PathAuthorityError,
    declare,
    observe,
)


@dataclass(frozen=True)
class ResolvedWorkspaceProductPath:
    """One canonical product-path identity and its internal filesystem path."""

    relative_path: str
    resolved_path: Path


class WorkspaceProductPathError(ValueError):
    """Fail-closed product-path rejection with a stable diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExecutorService:
    """Step execution support helpers."""

    TOOL_FAILURE_PATTERNS = (
        "read failed: ENOENT",
        "read failed: EISDIR",
        "exec failed: exec preflight",
        "complex interpreter invocation detected",
        "no such file or directory, access",
        "illegal operation on a directory, read",
    )

    @classmethod
    def recent_step_tool_failures(
        cls,
        db: Session,
        session_id: int,
        task_id: int,
        started_at: datetime,
    ) -> List[str]:
        recent_logs = (
            db.query(LogEntry)
            .filter(
                LogEntry.session_id == session_id,
                LogEntry.task_id == task_id,
                LogEntry.created_at >= started_at,
            )
            .order_by(LogEntry.created_at.asc(), LogEntry.id.asc())
            .all()
        )
        matches: List[str] = []
        for log in recent_logs:
            message = str(log.message or "")
            lowered = message.lower()
            if any(pattern.lower() in lowered for pattern in cls.TOOL_FAILURE_PATTERNS):
                matches.append(message[:500])
        return matches

    @staticmethod
    def tool_failure_correction_hints(
        tool_failures: List[str], project_dir: Path
    ) -> List[str]:
        hints: List[str] = []

        for failure in tool_failures:
            message = str(failure or "")

            raw_params = ExecutorService._extract_tool_failure_raw_params(message)

            raw_path = str(raw_params.get("path") or "").strip()
            path_diagnostic = ExecutorService.tool_failure_path_diagnostic(
                message, project_dir
            )
            requested_relative_path = path_diagnostic.get("requested_relative_path")
            if requested_relative_path:
                hints.append(
                    "File-tool paths are workspace-relative. Retry the read/write "
                    f"using `{requested_relative_path}` from the current Runtime Workspace."
                )
            elif raw_path and Path(raw_path).is_absolute():
                if not Path(raw_path).exists():
                    hints.append(
                        "The agent guessed a file path that does not exist inside the task workspace. "
                        "Before reading guessed files, enumerate the current Runtime Workspace with "
                        "`rg --files . | head -200` or `find . -maxdepth 4 -type f | sort | head -200`, "
                        "then read only confirmed files."
                    )
                    if re.search(r"/step-\d+.*\.md$", raw_path, re.IGNORECASE):
                        hints.append(
                            "Do not treat step descriptions as markdown files. "
                            "A path like `step-03-...md` is probably a guessed artifact; enumerate the workspace first "
                            "and only read it if it is actually present."
                        )
                elif Path(raw_path).is_dir():
                    hints.extend(
                        ExecutorService._directory_read_recovery_hints(
                            raw_path=Path(raw_path),
                            project_dir=project_dir,
                        )
                    )

            raw_command = str(raw_params.get("command") or "").strip()
            if raw_command.startswith("cd ") and "&&" in raw_command:
                hints.append(
                    "The execution tool rejected a wrapped shell command. "
                    "Retry with a direct command such as `node dist/server.js` and rely "
                    "on the current Runtime Workspace instead of `cd ... &&`."
                )

            if "read failed: eisd" in message.lower():
                if raw_path and Path(raw_path).is_dir():
                    hints.extend(
                        ExecutorService._directory_read_recovery_hints(
                            raw_path=Path(raw_path),
                            project_dir=project_dir,
                        )
                    )
                else:
                    hints.append(
                        "A directory path was passed to the file-read tool. Retry by reading "
                        "an actual file path inside the task workspace, not the folder itself."
                    )
            elif raw_path and Path(raw_path).is_dir():
                hints.extend(
                    ExecutorService._directory_read_recovery_hints(
                        raw_path=Path(raw_path),
                        project_dir=project_dir,
                    )
                )
            elif raw_path and re.search(r"/task-[^/]+/?$", raw_path):
                hints.append(
                    "A task workspace directory was passed to the file-read tool. "
                    "Read a specific file inside that directory, not the directory path itself."
                )

        deduped: List[str] = []
        seen = set()
        for hint in hints:
            if hint not in seen:
                seen.add(hint)
                deduped.append(hint)
        return deduped

    @staticmethod
    def tool_failure_path_diagnostic(failure: str, project_dir: Path) -> Dict[str, Any]:
        """Separate model-safe identity from raw provider path evidence."""

        message = str(failure or "")
        raw_params = ExecutorService._extract_tool_failure_raw_params(message)
        raw_path = str(raw_params.get("path") or "").strip()
        provider_match = re.search(r"access '([^']+)'", message)
        provider_reported_path = (
            provider_match.group(1) if provider_match else raw_path or None
        )
        requested_relative_path: Optional[str] = None
        resolved_internal_path: Optional[str] = None
        failure_code = "provider_tool_failure"

        if raw_path:
            try:
                resolution = ExecutorService.resolve_workspace_product_path(
                    project_dir, raw_path
                )
            except WorkspaceProductPathError as exc:
                failure_code = exc.code
                normalized_raw = raw_path.replace("\\", "/")
                root = project_dir.resolve()
                prefix = f"{root.name}/"
                if (
                    exc.code == "duplicated_task_execution_segment"
                    and normalized_raw.startswith(prefix)
                ):
                    try:
                        resolution = ExecutorService.resolve_workspace_product_path(
                            root, normalized_raw[len(prefix) :]
                        )
                    except WorkspaceProductPathError:
                        resolution = None
                elif Path(raw_path).is_absolute():
                    try:
                        relative = Path(raw_path).resolve().relative_to(root).as_posix()
                        resolution = ExecutorService.resolve_workspace_product_path(
                            root, relative
                        )
                    except (OSError, ValueError, WorkspaceProductPathError):
                        resolution = None
                else:
                    resolution = None
            if resolution is not None:
                requested_relative_path = resolution.relative_path
                resolved_internal_path = str(resolution.resolved_path)

        return {
            "requested_relative_path": requested_relative_path,
            "resolved_internal_path": resolved_internal_path,
            "provider_reported_path": provider_reported_path,
            "path_resolution_failure_code": failure_code,
        }

    @staticmethod
    def tool_failure_path_diagnostics(
        tool_failures: List[str], project_dir: Path
    ) -> List[Dict[str, Any]]:
        return [
            ExecutorService.tool_failure_path_diagnostic(failure, project_dir)
            for failure in tool_failures
            if ExecutorService._extract_tool_failure_raw_params(failure).get("path")
        ]

    @staticmethod
    def stub_file_repair_hints(
        project_dir: Path,
        stub_files: List[str],
        verification_command: Optional[str] = None,
    ) -> List[str]:
        normalized_files = [
            str(path or "").strip()
            for path in (stub_files or [])
            if str(path or "").strip()
        ]
        if not normalized_files:
            return []

        preview = ", ".join(normalized_files[:4])
        hints = [
            "These expected files already exist in the workspace but are still empty or stubbed: "
            f"{preview}. Edit their bodies directly instead of rerunning mkdir/touch commands.",
            "Replace placeholder-only commands with a real content-writing or file-editing command for each deliverable file.",
        ]
        lowered_verification = str(verification_command or "").strip().lower()
        if not lowered_verification or any(
            marker in lowered_verification
            for marker in ("test -f", "test -d", "ls ", "echo ", "grep -q")
        ):
            hints.append(
                "Use a content-aware verification command after writing the files. "
                "Do not rely only on file-existence checks once the paths already exist."
            )
        hints.append(
            "Before retrying, read the current stub file from the canonical workspace and overwrite it with real content, "
            f"for example `{project_dir / normalized_files[0]}`."
        )
        return hints

    @staticmethod
    def _extract_tool_failure_raw_params(message: str) -> Dict[str, Any]:
        raw_params_match = re.search(r"raw_params=(\{.*\})", str(message or ""))
        if not raw_params_match:
            return {}
        try:
            return json.loads(raw_params_match.group(1))
        except json.JSONDecodeError:
            path_match = re.search(r'"path"\s*:\s*"([^"]+)"', raw_params_match.group(1))
            if path_match:
                return {"path": path_match.group(1)}
            return {}

    @staticmethod
    def should_short_circuit_to_workspace_discovery(
        tool_failures: List[str], project_dir: Path
    ) -> bool:
        normalized_project_dir = project_dir.resolve()

        for failure in tool_failures:
            message = str(failure or "")
            lowered = message.lower()
            if (
                "read failed: eisdir" not in lowered
                and "illegal operation on a directory, read" not in lowered
            ):
                continue

            raw_params = ExecutorService._extract_tool_failure_raw_params(message)
            raw_path = str(raw_params.get("path") or "").strip()
            if not raw_path:
                continue

            try:
                candidate = Path(raw_path).resolve()
            except OSError:
                continue

            if not candidate.is_dir():
                continue

            if candidate == normalized_project_dir:
                return True

            if normalized_project_dir in candidate.parents:
                return True

        return False

    @staticmethod
    def _directory_read_recovery_hints(raw_path: Path, project_dir: Path) -> List[str]:
        normalized_raw_path = raw_path.resolve()
        normalized_project_dir = project_dir.resolve()
        inventory_command = "`rg --files . | head -200`"

        if normalized_raw_path == normalized_project_dir:
            return [
                "The file-read tool was pointed at the project root directory itself. "
                f"Do not read `.` as a file. First inventory the workspace with {inventory_command}, "
                "then read one confirmed workspace-relative file.",
                "For example: run `rg --files . | head -200`, choose a returned file such as "
                "`src/index.ts`, then call the file-read tool on `src/index.ts`.",
            ]

        if normalized_project_dir in normalized_raw_path.parents:
            relative_dir = normalized_raw_path.relative_to(normalized_project_dir)
            relative_dir_for_shell = relative_dir.as_posix()
            return [
                "A directory inside the task workspace was passed to the file-read tool. "
                f"Do not read `{relative_dir}` directly. First inventory files under `{relative_dir}` with "
                f"`find ./{relative_dir_for_shell} -maxdepth 4 -type f | sort | head -200`, then read one confirmed file.",
                "Use the file-read tool only on a concrete file path returned by that listing, not on the directory.",
            ]

        return [
            "A directory path was passed to the file-read tool. First inventory the workspace with "
            f"{inventory_command}, then read a concrete file path rather than the directory itself."
        ]

    @staticmethod
    def is_repeated_tool_path_failure(
        debug_attempts: List[Dict[str, Any]], error_message: str
    ) -> bool:
        combined = str(error_message or "").lower()
        if not any(
            marker in combined
            for marker in (
                "raw_params",
                "wrong root",
                "absolute task-workspace path",
                "read failed: enoent",
                "read failed: eisdir",
                "exec failed: exec preflight",
            )
        ):
            return False

        prior_related = 0
        for attempt in debug_attempts:
            prior_text = " ".join(
                [
                    str(attempt.get("error", "")),
                    str(attempt.get("analysis", "")),
                    str(attempt.get("fix", "")),
                ]
            ).lower()
            if any(
                marker in prior_text
                for marker in (
                    "raw_params",
                    "absolute task-workspace path",
                    "read failed: enoent",
                    "read failed: eisdir",
                    "exec failed: exec preflight",
                )
            ):
                prior_related += 1
        return prior_related >= 2

    _MIN_MEANINGFUL_BYTES = 4  # shared with patch_04

    @staticmethod
    def resolve_workspace_product_path(
        project_dir: Path, raw_path: str
    ) -> ResolvedWorkspaceProductPath:
        """Resolve one workspace-relative product path exactly once."""

        path_text = str(raw_path or "").strip().strip("'\"")
        if not path_text:
            raise WorkspaceProductPathError("empty_path", "product path is empty")
        path_text = path_text.replace("\\", "/")
        if path_text.startswith("~"):
            raise WorkspaceProductPathError(
                "home_path_rejected", f"product path uses home directory: {path_text}"
            )
        candidate = PurePosixPath(path_text)
        if candidate.is_absolute() or re.match(r"^[A-Za-z]:/", path_text):
            raise WorkspaceProductPathError(
                "absolute_path_rejected", f"absolute product path rejected: {path_text}"
            )
        if ".." in candidate.parts:
            raise WorkspaceProductPathError(
                "traversal_rejected", f"product path traversal rejected: {path_text}"
            )
        parts = tuple(part for part in candidate.parts if part not in {"", "."})
        if not parts:
            raise WorkspaceProductPathError("empty_path", "product path is empty")

        normalized_project_dir = project_dir.resolve()
        if (
            normalized_project_dir.parent.parent.name == "tasks"
            and parts[0] == normalized_project_dir.name
        ):
            raise WorkspaceProductPathError(
                "duplicated_task_execution_segment",
                "product path repeats the bound TaskExecution segment: " + path_text,
            )
        relative_path = PurePosixPath(*parts).as_posix()
        resolved = (normalized_project_dir / relative_path).resolve()
        if not resolved.is_relative_to(normalized_project_dir):
            raise WorkspaceProductPathError(
                "workspace_escape_rejected",
                f"product path escapes Runtime Workspace: {path_text} -> {resolved}",
            )
        return ResolvedWorkspaceProductPath(relative_path, resolved)

    @staticmethod
    def _resolve_op_path(project_dir: Path, raw_path: str, op_name: str) -> Path:
        try:
            resolution = ExecutorService.resolve_workspace_product_path(
                project_dir, raw_path
            )
        except WorkspaceProductPathError as exc:
            raise ValueError(f"{op_name} {exc}") from exc
        resolved = resolution.resolved_path
        return resolved

    @staticmethod
    def _resolve_write_file_path(project_dir: Path, raw_path: str) -> Path:
        return ExecutorService._resolve_op_path(project_dir, raw_path, "write_file")

    @staticmethod
    def _authorize_file_op(
        project_dir: Path,
        operation: Dict[str, Any],
        accepted_path_authority: AcceptedPathAuthority,
    ) -> str:
        """Authorize one structured mutation immediately before resolution."""

        op_name = str(operation.get("op") or "")
        declared = declare(operation.get("path"))
        observation = observe(project_dir, declared)
        if observation.symlink_segment:
            raise PathAuthorityError(
                "path_symlink_rejected",
                f"{op_name} target contains a symlink segment: {declared.value}",
            )

        if op_name == "mkdir":
            if (
                declared.value
                not in accepted_path_authority.creation_parent_directories()
            ):
                raise PathAuthorityError(
                    "path_not_authorized",
                    f"mkdir target is not an implied parent of a creation grant: {declared.value}",
                )
            if observation.exists and observation.entry_type is not EntryType.DIRECTORY:
                raise PathAuthorityError(
                    "path_target_type_invalid",
                    f"mkdir target is not a directory: {declared.value}",
                )
            return declared.value

        if op_name == "delete_file":
            required_class = GrantClass.DELETION_AUTHORIZED
        elif op_name == "replace_in_file":
            required_class = GrantClass.EXISTING_MUTABLE
        elif op_name in {"write_file", "append_file"}:
            if observation.entry_type is EntryType.REGULAR_FILE:
                required_class = GrantClass.EXISTING_MUTABLE
            elif not observation.exists and observation.entry_type is EntryType.MISSING:
                required_class = GrantClass.CREATION_AUTHORIZED
            else:
                raise PathAuthorityError(
                    "path_target_type_invalid",
                    f"{op_name} target is not a regular file or missing path: {declared.value}",
                )
        else:
            raise PathAuthorityError(
                "structured_mutation_unsupported",
                f"no authority rule exists for structured operation: {op_name}",
            )

        grant = accepted_path_authority.grant_for(declared)
        if grant is None:
            raise PathAuthorityError(
                "authority_missing",
                f"no {required_class.value} grant exists for {declared.value}",
            )
        if grant.grant_class is not required_class:
            raise PathAuthorityError(
                "grant_class_mismatch",
                f"{declared.value} has {grant.grant_class.value}, not {required_class.value}",
            )
        if op_name == "replace_in_file" and not observation.exists:
            raise PathAuthorityError(
                "path_target_missing",
                f"replace_in_file target does not exist: {declared.value}",
            )
        if (
            op_name == "delete_file"
            and observation.exists
            and observation.entry_type is not EntryType.REGULAR_FILE
        ):
            raise PathAuthorityError(
                "path_target_type_invalid",
                f"delete_file target is not a file: {declared.value}",
            )
        return declared.value

    @staticmethod
    def execute_file_ops(
        project_dir: Path,
        ops: Any,
        *,
        accepted_path_authority: AcceptedPathAuthority | None = None,
    ) -> Dict[str, Any]:
        """Execute structured file operations without shell quoting."""

        if not ops:
            return {
                "success": True,
                "files_changed": [],
                "output": "",
            }
        if not isinstance(ops, list):
            return {
                "success": False,
                "files_changed": [],
                "output": "ops must be a JSON array",
            }

        files_changed: List[str] = []
        output_lines: List[str] = []
        normalized_project_dir = project_dir.resolve()

        for index, operation in enumerate(ops, start=1):
            if not isinstance(operation, dict):
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": f"op {index} must be an object",
                }
            op_name = operation.get("op")
            if op_name not in SUPPORTED_FILE_OPS:
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": f"op {index} unsupported op: {op_name}",
                }
            declared_relative = None
            try:
                if accepted_path_authority is None:
                    raise PathAuthorityError(
                        "authority_record_missing",
                        "structured mutations require an accepted path authority",
                    )
                declared_relative = ExecutorService._authorize_file_op(
                    normalized_project_dir,
                    operation,
                    accepted_path_authority,
                )
                target = ExecutorService._resolve_op_path(
                    normalized_project_dir,
                    str(operation.get("path") or ""),
                    str(op_name),
                )
                # Re-observe after the existing containment resolution and
                # immediately before the operation branch.  This closes the
                # important creation-collision and final-symlink window
                # without treating a source-region digest as a whole-file lock.
                ExecutorService._authorize_file_op(
                    normalized_project_dir,
                    operation,
                    accepted_path_authority,
                )
            except PathAuthorityError as exc:
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": str(exc),
                    "failure_category": "validation_failure",
                    "authority_error": {
                        "code": exc.code,
                        "message": exc.message,
                        "path": locals().get("declared_relative"),
                    },
                    "authority_identity": (
                        accepted_path_authority.authority_identity
                        if accepted_path_authority is not None
                        else None
                    ),
                }
            except ValueError as exc:
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": str(exc),
                }
            relative = target.relative_to(normalized_project_dir).as_posix()

            if op_name == "mkdir":
                target.mkdir(parents=True, exist_ok=True)
                ensure_shared_path_to_root(target, normalized_project_dir)
                output_lines.append(f"mkdir {relative}")
                continue

            if op_name == "delete_file":
                if not target.exists():
                    output_lines.append(f"delete_file {relative} (already absent)")
                    continue
                if not target.is_file():
                    return {
                        "success": False,
                        "files_changed": files_changed,
                        "output": f"delete_file target is not a file: {relative}",
                    }
                ExecutorService._authorize_file_op(
                    normalized_project_dir,
                    operation,
                    accepted_path_authority,
                )
                target.unlink()
                files_changed.append(relative)
                output_lines.append(f"delete_file {relative}")
                ensure_shared_permissions(target.parent)
                continue

            if op_name in CONTENT_FILE_OPS:
                content = operation.get("content")
                if not isinstance(content, str):
                    return {
                        "success": False,
                        "files_changed": files_changed,
                        "output": f"op {index} content must be a string",
                    }
                if op_name == "write_file":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    ensure_shared_path_to_root(target.parent, normalized_project_dir)
                    ExecutorService._authorize_file_op(
                        normalized_project_dir,
                        operation,
                        accepted_path_authority,
                    )
                    target.write_text(content, encoding="utf-8")
                    ensure_shared_permissions(target)
                    output_lines.append(f"write_file {relative} ({len(content)} chars)")
                else:
                    if not target.parent.exists():
                        return {
                            "success": False,
                            "files_changed": files_changed,
                            "output": f"append_file parent directory does not exist: {target.parent.relative_to(normalized_project_dir).as_posix()}",
                        }
                    if not target.parent.is_dir():
                        return {
                            "success": False,
                            "files_changed": files_changed,
                            "output": f"append_file parent is not a directory: {target.parent.relative_to(normalized_project_dir).as_posix()}",
                        }
                    ExecutorService._authorize_file_op(
                        normalized_project_dir,
                        operation,
                        accepted_path_authority,
                    )
                    with target.open("a", encoding="utf-8") as handle:
                        handle.write(content)
                    ensure_shared_permissions(target)
                    output_lines.append(
                        f"append_file {relative} ({len(content)} chars)"
                    )
                files_changed.append(relative)
                continue

            old = operation.get("old")
            new = operation.get("new")
            if not isinstance(old, str):
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": f"op {index} old must be a string",
                }
            if not isinstance(new, str):
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": f"op {index} new must be a string",
                }
            if old == "":
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": f"replace_in_file old text is empty: {relative}",
                }
            if not target.exists():
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": f"replace_in_file target does not exist: {relative}",
                }
            if not target.is_file():
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": f"replace_in_file target is not a file: {relative}",
                }
            original = target.read_text(encoding="utf-8")
            occurrence_count = original.count(old)
            if occurrence_count == 0:
                already_applied_count = original.count(new) if new else 0
                if already_applied_count == 1:
                    output_lines.append(f"replace_in_file {relative} (already applied)")
                    continue
                if already_applied_count > 1:
                    return {
                        "success": False,
                        "files_changed": files_changed,
                        "output": f"replace_in_file old text not found and new text is ambiguous in {relative}: {already_applied_count} occurrences",
                    }
                try:
                    regex_matches = list(re.finditer(old, original))
                except re.error:
                    regex_matches = []
                if len(regex_matches) == 1:
                    ExecutorService._authorize_file_op(
                        normalized_project_dir,
                        operation,
                        accepted_path_authority,
                    )
                    target.write_text(
                        re.sub(old, lambda _match: new, original, count=1),
                        encoding="utf-8",
                    )
                    ensure_shared_permissions(target)
                    files_changed.append(relative)
                    output_lines.append(
                        f"replace_in_file {relative} (1 regex replacement)"
                    )
                    continue
                if len(regex_matches) > 1:
                    return {
                        "success": False,
                        "files_changed": files_changed,
                        "output": f"replace_in_file regex old text is ambiguous in {relative}: {len(regex_matches)} occurrences",
                    }
                ExecutorService._authorize_file_op(
                    normalized_project_dir,
                    operation,
                    accepted_path_authority,
                )
                patch_result = try_deterministic_patch(
                    target, old, new, normalized_project_dir
                )
                if patch_result is not None:
                    if patch_result.success:
                        ensure_shared_permissions(target)
                        files_changed.append(relative)
                        output_lines.append(
                            f"replace_in_file {relative} (patch_helper: {patch_result.evidence})"
                        )
                        continue
                    return {
                        "success": False,
                        "files_changed": files_changed,
                        "output": (
                            f"replace_in_file old text not found in {relative}; "
                            f"patch_helper: {patch_result.evidence}"
                        ),
                    }
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": f"replace_in_file old text not found in {relative}",
                }
            if occurrence_count > 1:
                return {
                    "success": False,
                    "files_changed": files_changed,
                    "output": f"replace_in_file old text is ambiguous in {relative}: {occurrence_count} occurrences",
                }
            ExecutorService._authorize_file_op(
                normalized_project_dir,
                operation,
                accepted_path_authority,
            )
            target.write_text(original.replace(old, new, 1), encoding="utf-8")
            ensure_shared_permissions(target)
            files_changed.append(relative)
            output_lines.append(f"replace_in_file {relative} (1 replacement)")

        return {
            "success": True,
            "files_changed": files_changed,
            "output": "\n".join(output_lines),
        }

    @staticmethod
    def cleanup_failed_step_artefacts(
        project_dir: Path,
        step: Dict[str, Any],
        logger,
        emit_live,
        *,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Remove empty directories created by a failed step so they do not
        pollute subsequent planning or resume runs.

        Stub/empty FILES are intentionally preserved so the debug agent can
        inspect them and understand that the file was created but needs content
        written into it, rather than concluding the file was never created.

        Returns a summary dict with lists of removed dirs and skipped paths.
        """
        removed_dirs: List[str] = []
        skipped: List[str] = []

        expected_files = step.get("expected_files", []) or []

        for raw_path in expected_files:
            path_text = str(raw_path or "").strip().strip("'\"\\")
            if not path_text:
                continue

            full_path = project_dir / path_text
            if not full_path.exists():
                continue
            if full_path.is_dir():
                # Only remove if the dir is empty.
                if not any(full_path.iterdir()):
                    if not dry_run:
                        full_path.rmdir()
                    removed_dirs.append(path_text)
                else:
                    skipped.append(path_text)
            else:
                # Preserve stub/empty files so the debug agent can inspect them.
                skipped.append(path_text)

        summary = {
            "removed_files": [],
            "removed_dirs": removed_dirs,
            "skipped": skipped,
        }

        if removed_dirs:
            msg = (
                f"[ORCHESTRATION] Pre-debug cleanup removed "
                f"0 empty file(s) and "
                f"{len(removed_dirs)} empty dir(s) from the failed step workspace"
            )
            logger.info(msg)
            emit_live(
                "INFO",
                msg,
                metadata={
                    "phase": "debug_cleanup",
                    "removed_files": [],
                    "removed_dirs": removed_dirs[:10],
                },
            )

        return summary
