"""Executor-stage helpers for orchestration."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import LogEntry


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
            if raw_path and not Path(raw_path).is_absolute():
                corrected_path = (project_dir / raw_path).resolve()
                hints.append(
                    "File-tool paths are being resolved against the wrong root. "
                    f"Retry the file read/write using the absolute task-workspace path "
                    f"`{corrected_path}` instead of `{raw_path}`."
                )
            elif raw_path and Path(raw_path).is_absolute():
                corrected_path = (project_dir / raw_path.lstrip("/")).resolve()
                if not Path(raw_path).exists() and corrected_path.is_relative_to(
                    project_dir
                ):
                    hints.append(
                        "The file-tool path looks like a truncated absolute path. "
                        f"Do not shorten the workspace root. Retry with the real absolute "
                        f"task-workspace path `{project_dir}` or a file inside it, not `{raw_path}`."
                    )
                elif not Path(raw_path).exists():
                    hints.append(
                        "The agent guessed a file path that does not exist inside the task workspace. "
                        f"Before reading guessed files, enumerate the real tree from `{project_dir}` with "
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
                    f"on the task working directory `{project_dir}` instead of `cd ... &&`."
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
                f"Do not read `{normalized_project_dir}` as a file. First inventory the workspace with {inventory_command}, "
                "then read one confirmed file using its full absolute path inside the project root.",
                "For example: run `rg --files . | head -200`, choose a returned file such as "
                f"`src/index.ts`, then call the file-read tool on `{normalized_project_dir}/src/index.ts`.",
            ]

        if normalized_project_dir in normalized_raw_path.parents:
            relative_dir = normalized_raw_path.relative_to(normalized_project_dir)
            return [
                "A directory inside the task workspace was passed to the file-read tool. "
                f"Do not read `{normalized_raw_path}` directly. First inventory files under `{relative_dir}` with "
                f"`find ./{relative_dir} -maxdepth 4 -type f | sort | head -200`, then read one confirmed file.",
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
