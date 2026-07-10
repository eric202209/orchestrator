"""Task execution change-set ownership service."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import LogEntry, Project, Task, TaskExecutionChangeSet
from app.services.orchestration.review_policy import decide_change_set_review
from app.services.workspace.permissions import ensure_shared_tree
from app.services.workspace.workspace_paths import (
    AUTO_SNAPSHOT_ROOT,
    HYDRATION_EXCLUDED_NAMES,
    LEGACY_BASELINE_DIR_NAME,
    TASK_REPORT_RE,
    is_hydration_excluded_path,
    resolve_project_root,
)

TASK_CHANGE_SET_LOG_MESSAGE = (
    "[WORKSPACE_CHANGE_SET] Task execution change set captured"
)
DEPENDENCY_FILE_NAMES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
}
CONFIG_FILE_NAMES = {
    ".env",
    ".env.example",
    "alembic.ini",
    "tsconfig.json",
    "vite.config.js",
    "vite.config.ts",
    "pytest.ini",
    "ruff.toml",
    "mypy.ini",
}


class ChangesetService:
    """Own build, persistence, lookup, and disposition for task change sets."""

    def __init__(self, db: Session):
        self.db = db

    def get_project_root(self, project: Project) -> Path:
        return resolve_project_root(project, self.db)

    def _reserved_project_names(self, project: Project) -> set[str]:
        reserved = {
            task.task_subfolder
            for task in project.tasks or []
            if getattr(task, "task_subfolder", None)
        }
        reserved.add(LEGACY_BASELINE_DIR_NAME)
        return reserved | HYDRATION_EXCLUDED_NAMES

    def _tracked_workspace_file_map(
        self,
        root: Path,
        *,
        project: Optional[Project] = None,
        preserve_project_root_rules: bool = False,
    ) -> dict[str, Path]:
        if not root.exists():
            return {}

        reserved_names = (
            self._reserved_project_names(project)
            if project is not None and preserve_project_root_rules
            else set()
        )
        tracked: dict[str, Path] = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if preserve_project_root_rules and relative.parts:
                if relative.parts[0] in reserved_names:
                    continue
            if is_hydration_excluded_path(relative):
                continue
            if TASK_REPORT_RE.match(path.name):
                continue
            tracked[relative.as_posix()] = path
        return tracked

    def _file_digest(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _artifact_files_root(self, project: Project, task_execution_id: int) -> Path:
        return (
            self.get_project_root(project)
            / ".agent"
            / "change-sets"
            / str(task_execution_id)
            / "files"
        )

    def _artifact_manifest_path(self, project: Project, task_execution_id: int) -> Path:
        return (
            self.get_project_root(project)
            / ".agent"
            / "change-sets"
            / str(task_execution_id)
            / "manifest.json"
        )

    def _path_is_safe_relative(self, relative_path: str) -> bool:
        path = Path(relative_path)
        return (
            relative_path
            and not path.is_absolute()
            and ".." not in path.parts
            and not is_hydration_excluded_path(path)
            and not TASK_REPORT_RE.match(path.name)
        )

    def persist_change_set_artifact(
        self,
        project: Project,
        change_set: dict[str, Any],
        *,
        target_root: Path,
    ) -> dict[str, Any]:
        """Persist approved-file candidates outside the disposable runtime tree."""

        task_execution_id = int(change_set["task_execution_id"])
        files_root = self._artifact_files_root(project, task_execution_id)
        manifest_path = self._artifact_manifest_path(project, task_execution_id)
        if files_root.exists():
            shutil.rmtree(files_root)
        files_root.mkdir(parents=True, exist_ok=True)

        copied_files: list[str] = []
        for relative_path in sorted(
            set(change_set.get("added_files") or [])
            | set(change_set.get("modified_files") or [])
        ):
            if not self._path_is_safe_relative(relative_path):
                continue
            source_path = (target_root / relative_path).resolve()
            try:
                source_path.relative_to(target_root)
            except ValueError:
                continue
            if not source_path.is_file():
                continue
            destination = files_root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            copied_files.append(relative_path)

        manifest = {
            "schema": "openclaw.task_execution_change_set_artifact.v1",
            "task_execution_id": task_execution_id,
            "project_id": change_set.get("project_id"),
            "task_id": change_set.get("task_id"),
            "source_target_path": str(target_root),
            "artifact_path": str(files_root),
            "copied_files": copied_files,
            "deleted_files": list(change_set.get("deleted_files") or []),
            "created_at": datetime.now(UTC).isoformat(),
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        ensure_shared_tree(manifest_path.parent)
        change_set["artifact_path"] = str(files_root)
        change_set["artifact_manifest_path"] = str(manifest_path)
        change_set["artifact_file_count"] = len(copied_files)
        return change_set

    def change_set_warning_flags(
        self,
        *,
        added_files: list[str],
        modified_files: list[str],
        deleted_files: list[str],
    ) -> list[str]:
        changed_files = added_files + modified_files + deleted_files
        flags: list[str] = []
        if deleted_files:
            flags.append("deleted_files")
        if len(changed_files) > 10:
            flags.append("more_than_10_changed_files")
        if any(Path(path).name in DEPENDENCY_FILE_NAMES for path in changed_files):
            flags.append("dependency_files_changed")
        if any(
            Path(path).name in CONFIG_FILE_NAMES or Path(path).name.startswith(".env.")
            for path in changed_files
        ):
            flags.append("config_files_changed")
        scaffold_names = {"README.md", "readme.md", "package.json", "tests"}
        if any(
            Path(path).parts and Path(path).parts[0] in scaffold_names
            for path in changed_files
        ):
            flags.append("scaffold_or_test_surface_changed")
        return sorted(set(flags))

    def change_set_review_decision(
        self,
        change_set: Optional[dict[str, Any]],
        *,
        workspace_review_policy: str,
        workflow_profile: Optional[str] = None,
        evaluator_evidence: Optional[dict[str, Any]] = None,
        template_review_policy: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return decide_change_set_review(
            change_set,
            workspace_review_policy=workspace_review_policy,
            workflow_profile=workflow_profile,
            evaluator_evidence=evaluator_evidence,
            template_review_policy=template_review_policy,
        )

    def build_task_execution_change_set(
        self,
        project: Project,
        task: Task,
        *,
        task_execution_id: int,
        snapshot_key: str,
        target_dir: Optional[Path] = None,
        preserve_project_root_rules: bool = True,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        project_root = self.get_project_root(project).resolve()
        target_root = (target_dir or project_root).resolve()
        snapshot_dir = (target_root / AUTO_SNAPSHOT_ROOT / snapshot_key).resolve()

        before = self._tracked_workspace_file_map(
            snapshot_dir,
            project=project,
            preserve_project_root_rules=False,
        )
        after = self._tracked_workspace_file_map(
            target_root,
            project=project,
            preserve_project_root_rules=preserve_project_root_rules,
        )

        before_paths = set(before)
        after_paths = set(after)
        added_files = sorted(after_paths - before_paths)
        deleted_files = sorted(before_paths - after_paths)
        modified_files = sorted(
            relative
            for relative in before_paths & after_paths
            if self._file_digest(before[relative]) != self._file_digest(after[relative])
        )

        return {
            "schema": "openclaw.task_execution_change_set.v1",
            "project_id": project.id,
            "task_id": task.id,
            "task_execution_id": task_execution_id,
            "snapshot_key": snapshot_key,
            "snapshot_path": str(snapshot_dir),
            "snapshot_exists": snapshot_dir.exists(),
            "target_path": str(target_root),
            "status": status,
            "captured_at": datetime.now(UTC).isoformat(),
            "added_files": added_files,
            "modified_files": modified_files,
            "deleted_files": deleted_files,
            "added_count": len(added_files),
            "modified_count": len(modified_files),
            "deleted_count": len(deleted_files),
            "changed_count": len(added_files)
            + len(modified_files)
            + len(deleted_files),
            "warning_flags": self.change_set_warning_flags(
                added_files=added_files,
                modified_files=modified_files,
                deleted_files=deleted_files,
            ),
        }

    def record_payload(self, record: TaskExecutionChangeSet) -> dict[str, Any]:
        added_files = list(record.added_files or [])
        modified_files = list(record.modified_files or [])
        deleted_files = list(record.deleted_files or [])
        warning_flags = list(record.warning_flags or [])
        payload = {
            "schema": "openclaw.task_execution_change_set.v1",
            "change_set_id": record.id,
            "project_id": record.project_id,
            "task_id": record.task_id,
            "task_execution_id": record.task_execution_id,
            "snapshot_key": record.base_snapshot_key,
            "snapshot_path": record.snapshot_path,
            "snapshot_exists": bool(record.snapshot_exists),
            "target_path": record.target_path,
            "status": record.status,
            "captured_at": (
                record.captured_at.isoformat() if record.captured_at else None
            ),
            "added_files": added_files,
            "modified_files": modified_files,
            "deleted_files": deleted_files,
            "added_count": len(added_files),
            "modified_count": len(modified_files),
            "deleted_count": len(deleted_files),
            "changed_count": len(added_files)
            + len(modified_files)
            + len(deleted_files),
            "warning_flags": warning_flags,
            "review_decision": record.review_decision,
            "review_reason": record.review_reason,
            "disposition": record.disposition,
            "disposition_reason": record.disposition_reason,
            "disposition_at": (
                record.disposition_at.isoformat() if record.disposition_at else None
            ),
            "disposition_metadata": record.disposition_metadata,
        }
        project = self.db.query(Project).filter(Project.id == record.project_id).first()
        if project is not None:
            files_root = self._artifact_files_root(project, record.task_execution_id)
            manifest_path = self._artifact_manifest_path(
                project, record.task_execution_id
            )
            payload["artifact_path"] = str(files_root)
            payload["artifact_exists"] = files_root.exists()
            payload["artifact_manifest_path"] = str(manifest_path)
        return payload

    def parse_change_set_captured_at(
        self, change_set: dict[str, Any]
    ) -> Optional[datetime]:
        captured_at = change_set.get("captured_at")
        if not captured_at:
            return None
        try:
            return datetime.fromisoformat(str(captured_at))
        except ValueError:
            return None

    def upsert_record(
        self,
        *,
        change_set: dict[str, Any],
        session_id: Optional[int],
        workspace_review_policy: Optional[str] = None,
        review_decision: Optional[dict[str, Any]] = None,
        workflow_profile: Optional[str] = None,
        evaluator_evidence: Optional[dict[str, Any]] = None,
    ) -> TaskExecutionChangeSet:
        task_execution_id = int(change_set["task_execution_id"])
        record = (
            self.db.query(TaskExecutionChangeSet)
            .filter(TaskExecutionChangeSet.task_execution_id == task_execution_id)
            .first()
        )
        if record is None:
            record = TaskExecutionChangeSet(task_execution_id=task_execution_id)
            self.db.add(record)

        record.project_id = int(change_set["project_id"])
        record.task_id = int(change_set["task_id"])
        record.session_id = session_id
        record.base_snapshot_key = str(change_set["snapshot_key"])
        record.snapshot_path = change_set.get("snapshot_path")
        record.target_path = change_set.get("target_path")
        record.snapshot_exists = bool(change_set.get("snapshot_exists"))
        record.added_files = list(change_set.get("added_files") or [])
        record.modified_files = list(change_set.get("modified_files") or [])
        record.deleted_files = list(change_set.get("deleted_files") or [])
        record.warning_flags = list(change_set.get("warning_flags") or [])
        record.status = change_set.get("status")
        record.captured_at = self.parse_change_set_captured_at(change_set)
        if review_decision is None:
            if workspace_review_policy is None:
                try:
                    from app.config import settings
                    from app.services.workspace.system_settings import (
                        get_effective_workspace_review_policy,
                    )

                    workspace_review_policy = get_effective_workspace_review_policy(
                        settings.WORKSPACE_REVIEW_POLICY,
                        db=self.db,
                    )
                except Exception:
                    workspace_review_policy = "hold_nontrivial"
            review_decision = self.change_set_review_decision(
                change_set,
                workspace_review_policy=workspace_review_policy,
                workflow_profile=workflow_profile,
                evaluator_evidence=evaluator_evidence,
            )
        record.review_decision = review_decision
        record.review_reason = (
            review_decision.get("reason") if review_decision else None
        )
        if not record.disposition:
            record.disposition = "captured"
        return record

    def mark_task_execution_change_set_disposition(
        self,
        *,
        task_execution_id: int,
        disposition: str,
        reason: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        commit: bool = True,
    ) -> Optional[TaskExecutionChangeSet]:
        record = (
            self.db.query(TaskExecutionChangeSet)
            .filter(TaskExecutionChangeSet.task_execution_id == task_execution_id)
            .first()
        )
        if not record:
            return None
        record.disposition = disposition
        record.disposition_reason = reason
        record.disposition_at = datetime.now(UTC)
        record.disposition_metadata = metadata or {}
        if commit:
            self.db.commit()
        return record

    def persist_task_execution_change_set(
        self,
        project: Project,
        task: Task,
        *,
        session_id: Optional[int],
        task_execution_id: int,
        snapshot_key: str,
        target_dir: Optional[Path] = None,
        preserve_project_root_rules: bool = True,
        status: Optional[str] = None,
        workspace_review_policy: Optional[str] = None,
        review_decision: Optional[dict[str, Any]] = None,
        workflow_profile: Optional[str] = None,
        evaluator_evidence: Optional[dict[str, Any]] = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        change_set = self.build_task_execution_change_set(
            project,
            task,
            task_execution_id=task_execution_id,
            snapshot_key=snapshot_key,
            target_dir=target_dir,
            preserve_project_root_rules=preserve_project_root_rules,
            status=status,
        )
        target_root = Path(change_set["target_path"]).resolve()
        if target_root.exists():
            self.persist_change_set_artifact(
                project,
                change_set,
                target_root=target_root,
            )
        self.upsert_record(
            change_set=change_set,
            session_id=session_id,
            workspace_review_policy=workspace_review_policy,
            review_decision=review_decision,
            workflow_profile=workflow_profile,
            evaluator_evidence=evaluator_evidence,
        )
        existing = (
            self.db.query(LogEntry)
            .filter(
                LogEntry.task_execution_id == task_execution_id,
                LogEntry.message == TASK_CHANGE_SET_LOG_MESSAGE,
            )
            .order_by(LogEntry.id.desc())
            .first()
        )
        if existing:
            existing.level = "INFO"
            existing.session_id = session_id
            existing.task_id = task.id
            existing.log_metadata = json.dumps(change_set)
        else:
            self.db.add(
                LogEntry(
                    session_id=session_id,
                    task_id=task.id,
                    task_execution_id=task_execution_id,
                    level="INFO",
                    message=TASK_CHANGE_SET_LOG_MESSAGE,
                    log_metadata=json.dumps(change_set),
                )
            )
        if commit:
            self.db.commit()
        return change_set

    def get_task_execution_change_set(
        self,
        *,
        task_execution_id: int,
    ) -> Optional[dict[str, Any]]:
        record = (
            self.db.query(TaskExecutionChangeSet)
            .filter(TaskExecutionChangeSet.task_execution_id == task_execution_id)
            .first()
        )
        if record:
            return self.record_payload(record)
        return None

    def get_latest_task_change_set_for_task(
        self,
        task_id: int,
    ) -> Optional[dict[str, Any]]:
        record = (
            self.db.query(TaskExecutionChangeSet)
            .filter(TaskExecutionChangeSet.task_id == task_id)
            .order_by(
                TaskExecutionChangeSet.created_at.desc(),
                TaskExecutionChangeSet.id.desc(),
            )
            .first()
        )
        if record:
            return {
                "change_set_id": record.id,
                "task_execution_id": record.task_execution_id,
                "recorded_at": (
                    record.created_at.isoformat() if record.created_at else None
                ),
                "change_set": self.record_payload(record),
                "review_decision": record.review_decision,
            }
        return None
