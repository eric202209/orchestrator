"""Checkpoint Service for OpenClaw Session State Management

Provides save/restore functionality for session state to enable true resume capability.
Implements:
- Save current execution state to disk
- Restore from previous checkpoint
- List available checkpoints
- Delete old checkpoints
"""

import json
import os
import re
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy.orm import Session
from app.models import LogEntry, Session as SessionModel
from app.config import settings


class CheckpointError(Exception):
    """Custom exception for checkpoint errors"""

    pass


class CheckpointService:
    """Service for managing OpenClaw session checkpoints"""

    LEGACY_CHECKPOINT_DIR = (
        "/root/.openclaw/workspace/vault/projects/orchestrator/checkpoints"
    )

    def __init__(self, db: Session):
        self.db = db
        configured_dir = Path(settings.CHECKPOINT_DIR).expanduser()
        if not configured_dir.is_absolute():
            configured_dir = Path.cwd() / configured_dir
        self.checkpoint_dir = configured_dir.resolve()
        # Ensure checkpoint directory exists
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def _candidate_checkpoint_roots(self) -> List[Path]:
        roots = [self.checkpoint_dir]
        legacy_dir = Path(self.LEGACY_CHECKPOINT_DIR)
        if legacy_dir != self.checkpoint_dir and legacy_dir.exists():
            roots.append(legacy_dir)
        return roots

    def _session_checkpoint_dirs(self, session_id: int) -> List[Path]:
        dirs: List[Path] = []
        seen: set[Path] = set()
        for checkpoint_root in self._candidate_checkpoint_roots():
            session_dir = checkpoint_root / f"session_{session_id}"
            if session_dir not in seen:
                dirs.append(session_dir)
                seen.add(session_dir)
        return dirs

    def _extract_session_id(self, path: Path) -> Optional[int]:
        match = re.match(r"session_(\d+)(?:_|$)", path.name)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _get_checkpoint_path(self, session_id: int, checkpoint_name: str) -> str:
        """Get the file path for a specific checkpoint"""
        return str(self.checkpoint_dir / f"session_{session_id}_{checkpoint_name}.json")

    def _get_session_checkpoint_dir(self, session_id: int, create: bool = False) -> str:
        """Get the directory for all checkpoints of a session"""
        dir_path = self.checkpoint_dir / f"session_{session_id}"
        if create:
            Path(dir_path).mkdir(parents=True, exist_ok=True)
        return str(dir_path)

    def _checkpoint_progress_score(self, data: Dict[str, Any]) -> int:
        orchestration_state = data.get("orchestration_state", {}) or {}
        step_results = data.get("step_results", []) or []
        execution_results = orchestration_state.get("execution_results", []) or []
        plan = orchestration_state.get("plan", []) or []
        current_step_index = (
            orchestration_state.get("current_step_index")
            or data.get("current_step_index")
            or 0
        )
        completed_steps = max(len(step_results), len(execution_results))
        return (completed_steps * 1000) + (int(current_step_index) * 10) + len(plan)

    def _checkpoint_name_priority(self, checkpoint_name: str) -> int:
        lowered = str(checkpoint_name or "").lower()
        if lowered == "autosave_latest":
            return 50
        if lowered == "autosave_error":
            return 20
        if lowered.startswith("paused_") or lowered.startswith("stopped_"):
            return 10
        return 0

    def _collect_checkpoint_entries(self, session_id: int) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        seen_names: set[str] = set()

        def append_entry(filepath: str, fallback_name: str) -> None:
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                checkpoint_name = data.get("checkpoint_name", fallback_name)
                if checkpoint_name in seen_names:
                    return
                created_at = datetime.fromisoformat(
                    data.get("created_at", "1970-01-01")
                )
                entries.append(
                    {
                        "name": checkpoint_name,
                        "created_at": created_at,
                        "path": filepath,
                        "data": data,
                        "progress_score": self._checkpoint_progress_score(data),
                        "name_priority": self._checkpoint_name_priority(
                            checkpoint_name
                        ),
                    }
                )
                seen_names.add(checkpoint_name)
            except Exception:
                return

        session_dir = self._get_session_checkpoint_dir(session_id, create=False)
        if os.path.exists(session_dir):
            for filename in os.listdir(session_dir):
                if filename.endswith(".json"):
                    append_entry(
                        os.path.join(session_dir, filename),
                        filename.replace(".json", ""),
                    )

        for checkpoint_root in self._candidate_checkpoint_roots():
            if not checkpoint_root.exists():
                continue
            for filename in os.listdir(checkpoint_root):
                if filename.endswith(".json") and filename.startswith(
                    f"session_{session_id}_"
                ):
                    append_entry(
                        os.path.join(checkpoint_root, filename),
                        filename.replace(".json", ""),
                    )

        entries.sort(
            key=lambda item: (
                item["progress_score"],
                item["name_priority"],
                item["created_at"],
            ),
            reverse=True,
        )
        return entries

    def resolve_resume_checkpoint_name(
        self, session_id: int, requested_checkpoint_name: Optional[str] = None
    ) -> Optional[str]:
        entries = self._collect_checkpoint_entries(session_id)
        if not entries:
            return None

        best_entry = entries[0]
        if not requested_checkpoint_name:
            return best_entry["name"]

        requested_entry = next(
            (entry for entry in entries if entry["name"] == requested_checkpoint_name),
            None,
        )
        if requested_entry is None:
            return best_entry["name"]

        requested_score = requested_entry["progress_score"]
        best_score = best_entry["progress_score"]
        if requested_entry["name"] == best_entry["name"]:
            return requested_entry["name"]

        if requested_score <= 0 and best_score > requested_score:
            return best_entry["name"]

        if best_score - requested_score >= 1000:
            return best_entry["name"]

        return requested_entry["name"]

    def _remove_tree(self, path: Path) -> tuple[int, int]:
        """Remove a directory tree or a single file. Returns (files, dirs) removed."""
        deleted_files = 0
        deleted_dirs = 0

        if not path.exists():
            return deleted_files, deleted_dirs

        if path.is_file():
            path.unlink(missing_ok=True)
            return 1, 0

        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
                deleted_files += 1
            elif child.is_dir():
                child.rmdir()
                deleted_dirs += 1

        if path.exists():
            path.rmdir()
            deleted_dirs += 1

        return deleted_files, deleted_dirs

    def _log_checkpoint(self, session_id: int, level: str, message: str):
        """Log checkpoint operation"""
        try:
            log_entry = LogEntry(
                session_id=session_id,
                level=level,
                message=f"[CHECKPOINT] {message}",
                log_metadata=json.dumps({}),
            )
            self.db.add(log_entry)
            # Don't commit here - let caller handle it for performance
        except Exception as e:
            print(f"Failed to log checkpoint: {e}")

    def save_checkpoint(
        self,
        session_id: int,
        checkpoint_name: str = "manual",
        context_data: Dict[str, Any] = None,
        orchestration_state: Optional[Dict[str, Any]] = None,
        current_step_index: Optional[int] = None,
        step_results: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Save session state to a checkpoint file

        Args:
            session_id: Session ID
            checkpoint_name: Name of the checkpoint (e.g., 'paused', 'error_recovery')
            context_data: Current session context (task description, project info, etc.)
            orchestration_state: Orchestration workflow state (PLANNING, EXECUTING, etc.)
            current_step_index: Index of current step in multi-step execution
            step_results: Results from completed steps

        Returns:
            Checkpoint metadata including path and timestamp
        """
        try:
            checkpoint_path = self._get_checkpoint_path(session_id, checkpoint_name)

            # Create checkpoint data structure
            checkpoint_data = {
                "session_id": session_id,
                "checkpoint_name": checkpoint_name,
                "created_at": datetime.utcnow().isoformat(),
                "context": context_data or {},
                "orchestration_state": orchestration_state or {},
                "current_step_index": current_step_index,
                "step_results": step_results or [],
                "metadata": {
                    "total_steps": len(step_results) + (1 if current_step_index else 0),
                    "completed_steps": len(step_results),
                },
            }

            # Write to file
            with open(checkpoint_path, "w") as f:
                json.dump(checkpoint_data, f, indent=2, default=str)

            # Log checkpoint creation
            self._log_checkpoint(
                session_id, "INFO", f"Checkpoint saved: {checkpoint_name}"
            )

            return {
                "success": True,
                "path": checkpoint_path,
                "session_id": session_id,
                "checkpoint_name": checkpoint_name,
                "created_at": checkpoint_data["created_at"],
                "metadata": checkpoint_data["metadata"],
            }

        except Exception as e:
            error_msg = f"Failed to save checkpoint: {str(e)}"
            self._log_checkpoint(session_id, "ERROR", error_msg)
            raise CheckpointError(error_msg)

    def load_checkpoint(
        self, session_id: int, checkpoint_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Load a checkpoint from disk

        Args:
            session_id: Session ID
            checkpoint_name: Specific checkpoint name (optional - loads latest if not specified)

        Returns:
            Checkpoint data including context and state
        """
        try:
            # If no specific checkpoint name, find the latest one
            if not checkpoint_name:
                checkpoint_name = self._find_latest_checkpoint(session_id)
                if not checkpoint_name:
                    raise CheckpointError(
                        f"No checkpoints found for session {session_id}"
                    )

            checkpoint_path = self._get_checkpoint_path(session_id, checkpoint_name)

            if not os.path.exists(checkpoint_path):
                checkpoint_path = None
                for checkpoint_root in self._candidate_checkpoint_roots():
                    candidate = (
                        checkpoint_root / f"session_{session_id}_{checkpoint_name}.json"
                    )
                    if candidate.exists():
                        checkpoint_path = str(candidate)
                        break

            if not checkpoint_path or not os.path.exists(checkpoint_path):
                raise CheckpointError(
                    f"Checkpoint not found for session {session_id}: {checkpoint_name}"
                )

            # Read checkpoint data
            with open(checkpoint_path, "r") as f:
                checkpoint_data = json.load(f)

            self._log_checkpoint(
                session_id, "INFO", f"Checkpoint loaded: {checkpoint_name}"
            )

            return checkpoint_data

        except CheckpointError:
            raise
        except Exception as e:
            error_msg = f"Failed to load checkpoint: {str(e)}"
            self._log_checkpoint(session_id, "ERROR", error_msg)
            raise CheckpointError(error_msg)

    def load_resume_checkpoint(
        self, session_id: int, checkpoint_name: Optional[str] = None
    ) -> Dict[str, Any]:
        resolved_name = self.resolve_resume_checkpoint_name(
            session_id, requested_checkpoint_name=checkpoint_name
        )
        if not resolved_name:
            raise CheckpointError(f"No checkpoints found for session {session_id}")
        checkpoint_data = self.load_checkpoint(session_id, resolved_name)
        checkpoint_data["_resolved_checkpoint_name"] = resolved_name
        checkpoint_data["_requested_checkpoint_name"] = checkpoint_name
        return checkpoint_data

    def list_checkpoints(self, session_id: int) -> List[Dict[str, Any]]:
        """
        List all checkpoints for a session

        Args:
            session_id: Session ID

        Returns:
            List of checkpoint metadata (oldest first)
        """
        try:
            entries = self._collect_checkpoint_entries(session_id)
            recommended_name = self.resolve_resume_checkpoint_name(session_id)
            checkpoints = []
            for entry in entries:
                data = entry["data"]
                checkpoints.append(
                    {
                        "name": entry["name"],
                        "created_at": data.get("created_at"),
                        "step_index": data.get("current_step_index"),
                        "completed_steps": len(data.get("step_results", [])),
                        "progress_score": entry["progress_score"],
                        "recommended": entry["name"] == recommended_name,
                    }
                )

            # Sort by creation time (oldest first)
            checkpoints.sort(key=lambda x: x.get("created_at", ""))

            return checkpoints

        except Exception as e:
            self._log_checkpoint(
                session_id, "ERROR", f"Failed to list checkpoints: {str(e)}"
            )
            return []

    def delete_checkpoint(self, session_id: int, checkpoint_name: str) -> bool:
        """
        Delete a specific checkpoint

        Args:
            session_id: Session ID
            checkpoint_name: Checkpoint name to delete

        Returns:
            True if deleted successfully
        """
        try:
            deleted = False

            for checkpoint_root in self._candidate_checkpoint_roots():
                candidate = (
                    checkpoint_root / f"session_{session_id}_{checkpoint_name}.json"
                )
                if candidate.exists():
                    os.remove(candidate)
                    deleted = True

            session_dir = Path(
                self._get_session_checkpoint_dir(session_id, create=False)
            )
            if session_dir.exists():
                for candidate in session_dir.glob("*.json"):
                    try:
                        with open(candidate, "r") as f:
                            data = json.load(f)
                        candidate_name = data.get(
                            "checkpoint_name", candidate.stem.replace(".json", "")
                        )
                    except Exception:
                        candidate_name = candidate.stem

                    if candidate_name == checkpoint_name:
                        candidate.unlink(missing_ok=True)
                        deleted = True

                if not any(session_dir.iterdir()):
                    session_dir.rmdir()

            if deleted:
                self._log_checkpoint(
                    session_id, "INFO", f"Checkpoint deleted: {checkpoint_name}"
                )

            return deleted

        except Exception as e:
            self._log_checkpoint(
                session_id, "ERROR", f"Failed to delete checkpoint: {str(e)}"
            )
            return False

    def delete_all_checkpoints(self, session_id: int) -> int:
        """Delete every checkpoint file for a session across all supported roots."""
        deleted_count = 0

        try:
            for checkpoint_root in self._candidate_checkpoint_roots():
                if not checkpoint_root.exists():
                    continue
                for candidate in checkpoint_root.glob(f"session_{session_id}_*.json"):
                    candidate.unlink(missing_ok=True)
                    deleted_count += 1

            for session_dir in self._session_checkpoint_dirs(session_id):
                if not session_dir.exists():
                    continue
                for candidate in session_dir.glob("*.json"):
                    candidate.unlink(missing_ok=True)
                    deleted_count += 1

                # Remove any leftover nested artifacts, then the directory itself.
                removed_files, _ = self._remove_tree(session_dir)
                deleted_count += removed_files

            if deleted_count:
                self._log_checkpoint(
                    session_id,
                    "INFO",
                    f"Deleted all checkpoints for session {session_id} ({deleted_count} files)",
                )

            return deleted_count
        except Exception as e:
            self._log_checkpoint(
                session_id,
                "ERROR",
                f"Failed to delete all checkpoints: {str(e)}",
            )
            return deleted_count

    def cleanup_old_checkpoints(
        self, session_id: int, keep_latest: int = 3, max_age_hours: int = 24
    ) -> Dict[str, Any]:
        """
        Clean up old checkpoints, keeping only the latest N

        Args:
            session_id: Session ID
            keep_latest: Number of most recent checkpoints to keep
            max_age_hours: Delete checkpoints older than this (hours)

        Returns:
            Cleanup statistics
        """
        try:
            checkpoints = self.list_checkpoints(session_id)

            if len(checkpoints) <= keep_latest:
                return {"deleted": 0, "kept": len(checkpoints)}

            # Calculate cutoff time
            cutoff_time = datetime.utcnow() - timedelta(hours=max_age_hours)

            deleted_count = 0

            for checkpoint in checkpoints:
                created_at = datetime.fromisoformat(checkpoint["created_at"])

                # Delete if older than max_age_hours OR if we've kept enough recent ones
                if (
                    created_at < cutoff_time
                    or len(checkpoints) - deleted_count > keep_latest
                ):
                    self.delete_checkpoint(session_id, checkpoint["name"])
                    deleted_count += 1

            return {"deleted": deleted_count, "kept": len(checkpoints) - deleted_count}

        except Exception as e:
            self._log_checkpoint(
                session_id, "ERROR", f"Failed to cleanup checkpoints: {str(e)}"
            )
            return {"error": str(e), "deleted": 0}

    def cleanup_orphaned_checkpoints(self) -> Dict[str, Any]:
        """
        Delete checkpoint artifacts for sessions that are missing or soft-deleted.

        Returns:
            Summary including orphaned session ids and deleted artifact count.
        """
        deleted_files = 0
        deleted_dirs = 0
        orphaned_session_ids: set[int] = set()

        try:
            known_sessions = {
                session_id: deleted_at is not None
                for session_id, deleted_at in self.db.query(
                    SessionModel.id, SessionModel.deleted_at
                ).all()
            }

            def is_orphaned(session_id: int) -> bool:
                return session_id not in known_sessions or known_sessions[session_id]

            for checkpoint_root in self._candidate_checkpoint_roots():
                if not checkpoint_root.exists():
                    continue

                for candidate in checkpoint_root.iterdir():
                    session_id = self._extract_session_id(candidate)
                    if session_id is None or not is_orphaned(session_id):
                        continue

                    orphaned_session_ids.add(session_id)

                    if candidate.is_file():
                        candidate.unlink(missing_ok=True)
                        deleted_files += 1
                    elif candidate.is_dir():
                        removed_files, removed_dirs = self._remove_tree(candidate)
                        deleted_files += removed_files
                        deleted_dirs += removed_dirs

            if orphaned_session_ids:
                self.db.add(
                    LogEntry(
                        session_id=None,
                        level="INFO",
                        message=(
                            "[CHECKPOINT] Cleaned orphaned checkpoint artifacts for "
                            f"sessions: {', '.join(str(sid) for sid in sorted(orphaned_session_ids))}"
                        ),
                        log_metadata=json.dumps(
                            {
                                "orphaned_session_ids": sorted(orphaned_session_ids),
                                "deleted_files": deleted_files,
                                "deleted_dirs": deleted_dirs,
                            }
                        ),
                    )
                )

            return {
                "deleted_files": deleted_files,
                "deleted_dirs": deleted_dirs,
                "orphaned_session_ids": sorted(orphaned_session_ids),
            }
        except Exception as e:
            return {
                "deleted_files": deleted_files,
                "deleted_dirs": deleted_dirs,
                "orphaned_session_ids": sorted(orphaned_session_ids),
                "error": str(e),
            }

    def _find_latest_checkpoint(self, session_id: int) -> Optional[str]:
        """Find the best checkpoint name for a session

        Searches both the legacy flat format (root directory) and new subdirectory format.
        Prefers the most complete usable checkpoint, then falls back to the most recent.
        """
        try:
            all_checkpoints = self._collect_checkpoint_entries(session_id)
            if not all_checkpoints:
                return None

            return all_checkpoints[0]["name"]

        except Exception as e:
            print(f"Failed to find latest checkpoint: {e}")
            return None


# Global instance for dependency injection
_checkpoint_service_instance = None


def get_checkpoint_service(db: Session) -> CheckpointService:
    """Get or create checkpoint service instance"""
    global _checkpoint_service_instance

    if _checkpoint_service_instance is None or _checkpoint_service_instance.db != db:
        _checkpoint_service_instance = CheckpointService(db)

    return _checkpoint_service_instance
