"""Canonical baseline, archive, and cleanup ownership service."""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import Project, Task, TaskStatus
from app.services.workspace.canonical_mutation_service import CanonicalMutationService
from app.services.workspace.workspace_paths import (
    HYDRATION_EXCLUDED_NAMES,
    LEGACY_BASELINE_DIR_NAME,
    PROJECT_GITIGNORE_GUARD_END,
    PROJECT_GITIGNORE_GUARD_LINES,
    PROJECT_GITIGNORE_GUARD_START,
    PROMOTED_WORKSPACE_ARCHIVE_ROOT,
    REQUESTED_CHANGES_ARCHIVE_ROOT,
    RETAINED_WORKSPACE_ARCHIVE_ROOT,
    TASK_REPORT_RE,
    is_hydration_excluded_path,
    resolve_project_root,
)


class BaselinePromotionService:
    """Own canonical baseline mutations and workspace archive operations."""

    def __init__(
        self,
        db: Session,
        *,
        canonical_mutations: CanonicalMutationService | None = None,
    ):
        self.db = db
        self.canonical_mutations = canonical_mutations or CanonicalMutationService()

    def get_project_root(self, project: Project) -> Path:
        return resolve_project_root(project, self.db)

    def get_project_tasks(self, project_id: int) -> list[Task]:
        return (
            self.db.query(Task)
            .filter(Task.project_id == project_id)
            .order_by(
                Task.plan_position.asc().nullslast(),
                Task.priority.desc(),
                Task.created_at.asc().nullslast(),
                Task.id.asc(),
            )
            .all()
        )

    def get_project_baseline_dir(self, project: Project) -> Path:
        return self.get_project_root(project)

    def get_legacy_project_baseline_dir(self, project: Project) -> Path:
        return self.get_project_root(project) / LEGACY_BASELINE_DIR_NAME

    def get_existing_project_baseline_dirs(self, project: Project) -> list[Path]:
        baseline_dirs: list[Path] = []
        canonical_dir = self.get_project_baseline_dir(project)
        legacy_dir = self.get_legacy_project_baseline_dir(project)
        for candidate in (canonical_dir, legacy_dir):
            if candidate.exists() and candidate not in baseline_dirs:
                baseline_dirs.append(candidate)
        return baseline_dirs

    def ensure_project_gitignore_guard(self, project: Project) -> dict[str, Any]:
        project_root = self.get_project_root(project).resolve()
        project_root.mkdir(parents=True, exist_ok=True)
        gitignore_path = project_root / ".gitignore"
        existing = (
            gitignore_path.read_text(encoding="utf-8")
            if gitignore_path.exists()
            else ""
        )
        guard_block = "\n".join(
            [
                PROJECT_GITIGNORE_GUARD_START,
                *PROJECT_GITIGNORE_GUARD_LINES,
                PROJECT_GITIGNORE_GUARD_END,
            ]
        )
        pattern = re.compile(
            rf"{re.escape(PROJECT_GITIGNORE_GUARD_START)}.*?{re.escape(PROJECT_GITIGNORE_GUARD_END)}",
            re.DOTALL,
        )
        if pattern.search(existing):
            updated = pattern.sub(guard_block, existing)
        else:
            normalized_existing = existing.rstrip()
            updated = (
                f"{normalized_existing}\n\n{guard_block}\n"
                if normalized_existing
                else f"{guard_block}\n"
            )

        if updated == existing:
            return {
                "changed": False,
                "path": str(gitignore_path),
                "entries": PROJECT_GITIGNORE_GUARD_LINES,
            }

        gitignore_path.write_text(updated, encoding="utf-8")
        gitignore_path.chmod(0o666)
        return {
            "changed": True,
            "path": str(gitignore_path),
            "entries": PROJECT_GITIGNORE_GUARD_LINES,
        }

    def _copy_tree_into_target(
        self,
        project: Project,
        source_dir: Path,
        target_dir: Path,
        overwrite: bool,
    ) -> int:
        copied = 0
        project_root = self.get_project_root(project).resolve()
        task_subfolders = {
            task.task_subfolder
            for task in self.get_project_tasks(project.id)
            if getattr(task, "task_subfolder", None)
        }
        for source_path in source_dir.rglob("*"):
            if source_path.is_dir():
                continue
            relative = source_path.relative_to(source_dir)
            if source_dir.resolve() == project_root and relative.parts:
                first_part = relative.parts[0]
                if (
                    first_part in task_subfolders
                    or first_part in HYDRATION_EXCLUDED_NAMES
                    or first_part == LEGACY_BASELINE_DIR_NAME
                ):
                    continue
            if is_hydration_excluded_path(relative):
                continue
            if TASK_REPORT_RE.match(source_path.name):
                continue
            destination = target_dir / relative
            if destination.exists() and not overwrite:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            copied += 1
        return copied

    def promote_task_into_baseline(
        self, project: Project, task: Task
    ) -> dict[str, Any]:
        project_root = self.get_project_root(project)
        return self.canonical_mutations.run_locked(
            project,
            project_root=project_root,
            operation="promote_task",
            owner=f"task:{task.id}",
            fn=lambda: self.promote_task_into_baseline_unlocked(project, task),
        )

    def promote_task_into_baseline_unlocked(
        self, project: Project, task: Task
    ) -> dict[str, Any]:
        baseline_dir = self.get_project_baseline_dir(project)
        baseline_dir.mkdir(parents=True, exist_ok=True)
        if not task.task_subfolder:
            return {"baseline_path": str(baseline_dir), "files_copied": 0}

        project_root = self.get_project_root(project)
        source_dir = (project_root / task.task_subfolder).resolve()
        if not source_dir.exists():
            return {"baseline_path": str(baseline_dir), "files_copied": 0}

        files_copied = self._copy_tree_into_target(
            project=project,
            source_dir=source_dir,
            target_dir=baseline_dir,
            overwrite=True,
        )
        return {"baseline_path": str(baseline_dir), "files_copied": files_copied}

    def rebuild_project_baseline(self, project: Project) -> dict[str, Any]:
        project_root = self.get_project_root(project)
        return self.canonical_mutations.run_locked(
            project,
            project_root=project_root,
            operation="rebuild_baseline",
            owner=f"project:{project.id}",
            fn=lambda: self.rebuild_project_baseline_unlocked(project),
        )

    def rebuild_project_baseline_unlocked(self, project: Project) -> dict[str, Any]:
        baseline_dir = self.get_project_baseline_dir(project)
        baseline_dir.mkdir(parents=True, exist_ok=True)
        self.ensure_project_gitignore_guard(project)
        self.clear_project_root_baseline_contents(project)

        merged_tasks = [
            task
            for task in self.get_project_tasks(project.id)
            if getattr(task, "task_subfolder", None)
            and getattr(task, "workspace_status", None) == "promoted"
        ]

        applied_tasks = []
        total_files = 0
        for task in merged_tasks:
            result = self.promote_task_into_baseline_unlocked(project, task)
            applied_tasks.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "files_copied": result["files_copied"],
                }
            )
            total_files += result["files_copied"]
        self.ensure_project_gitignore_guard(project)

        return {
            "baseline_path": str(baseline_dir),
            "promoted_task_count": len(
                [
                    task
                    for task in merged_tasks
                    if getattr(task, "workspace_status", None) == "promoted"
                ]
            ),
            "merged_task_count": len(merged_tasks),
            "files_copied": total_files,
            "applied_tasks": applied_tasks,
        }

    def clear_project_root_baseline_contents(self, project: Project) -> None:
        project_root = self.get_project_root(project)
        task_subfolders = {
            task.task_subfolder
            for task in self.get_project_tasks(project.id)
            if getattr(task, "task_subfolder", None)
        }
        preserved_names = set(HYDRATION_EXCLUDED_NAMES)
        preserved_names.add(LEGACY_BASELINE_DIR_NAME)
        preserved_names.add(".gitignore")
        preserved_names.update(task_subfolders)

        for child in project_root.iterdir():
            if child.name in preserved_names:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

    def count_baseline_files(self, project: Project, baseline_dir: Path) -> int:
        if not baseline_dir.exists():
            return 0

        project_root = self.get_project_root(project).resolve()
        task_subfolders = {
            task.task_subfolder
            for task in self.get_project_tasks(project.id)
            if getattr(task, "task_subfolder", None)
        }
        count = 0
        for path in baseline_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(baseline_dir)
            if baseline_dir.resolve() == project_root and relative.parts:
                first_part = relative.parts[0]
                if (
                    first_part in task_subfolders
                    or first_part in HYDRATION_EXCLUDED_NAMES
                    or first_part == LEGACY_BASELINE_DIR_NAME
                ):
                    continue
            count += 1
        return count

    def get_project_baseline_overview(
        self, project: Optional[Project]
    ) -> dict[str, Any]:
        if not project:
            return {
                "exists": False,
                "path": None,
                "file_count": 0,
                "promoted_task_count": 0,
            }

        baseline_dir = self.get_project_baseline_dir(project)
        legacy_dir = self.get_legacy_project_baseline_dir(project)
        file_count = self.count_baseline_files(project, baseline_dir)
        if file_count == 0 and legacy_dir.exists():
            file_count = self.count_baseline_files(project, legacy_dir)
        promoted_task_count = (
            self.db.query(Task)
            .filter(
                Task.project_id == project.id,
                Task.workspace_status == "promoted",
            )
            .count()
        )
        return {
            "exists": file_count > 0,
            "path": str(
                baseline_dir
                if baseline_dir.exists() or not legacy_dir.exists()
                else legacy_dir
            ),
            "file_count": file_count,
            "promoted_task_count": promoted_task_count,
        }

    def cleanup_retained_task_workspaces(
        self,
        project: Project,
        *,
        dry_run: bool = True,
        include_ready: bool = False,
        include_changes_requested: bool = False,
        include_blocked: bool = True,
    ) -> dict[str, Any]:
        project_root = self.get_project_root(project).resolve()
        if dry_run:
            return self.cleanup_retained_task_workspaces_unlocked(
                project,
                dry_run=dry_run,
                include_ready=include_ready,
                include_changes_requested=include_changes_requested,
                include_blocked=include_blocked,
                project_root=project_root,
            )
        return self.canonical_mutations.run_locked(
            project,
            project_root=project_root,
            operation="cleanup_retained_workspaces",
            owner=f"project:{project.id}",
            fn=lambda: self.cleanup_retained_task_workspaces_unlocked(
                project,
                dry_run=dry_run,
                include_ready=include_ready,
                include_changes_requested=include_changes_requested,
                include_blocked=include_blocked,
                project_root=project_root,
            ),
        )

    def cleanup_retained_task_workspaces_unlocked(
        self,
        project: Project,
        *,
        dry_run: bool = True,
        include_ready: bool = False,
        include_changes_requested: bool = False,
        include_blocked: bool = True,
        project_root: Path | None = None,
    ) -> dict[str, Any]:
        project_root = project_root or self.get_project_root(project).resolve()
        archived_at = datetime.now(UTC)
        archive_root = (
            project_root
            / RETAINED_WORKSPACE_ARCHIVE_ROOT
            / archived_at.strftime("%Y%m%d-%H%M%S")
        )
        eligible_statuses: set[str] = set()
        if include_ready:
            eligible_statuses.add("ready")
        if include_changes_requested:
            eligible_statuses.add("changes_requested")
        if include_blocked:
            eligible_statuses.add("blocked")

        candidates: list[dict[str, Any]] = []
        deleted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for task in self.get_project_tasks(project.id):
            task_subfolder = getattr(task, "task_subfolder", None)
            workspace_status = getattr(task, "workspace_status", None) or "unknown"
            task_status = getattr(task, "status", None)
            if not task_subfolder:
                continue
            workspace_dir = (project_root / task_subfolder).resolve()
            record = {
                "task_id": task.id,
                "title": task.title,
                "workspace_status": workspace_status,
                "task_status": getattr(task_status, "value", str(task_status)),
                "task_subfolder": task_subfolder,
                "path": str(workspace_dir),
                "exists": workspace_dir.exists(),
            }
            archive_dir = (
                archive_root / f"task-{task.id}-{workspace_dir.name}"
            ).resolve()
            record["archive_path"] = str(archive_dir)
            if workspace_status == "promoted":
                skipped.append({**record, "reason": "promoted_workspace"})
                continue
            if task_status == TaskStatus.RUNNING:
                skipped.append({**record, "reason": "running_task"})
                continue
            if workspace_status not in eligible_statuses:
                skipped.append({**record, "reason": "status_not_selected"})
                continue
            if not workspace_dir.exists():
                skipped.append({**record, "reason": "workspace_missing"})
                continue
            if workspace_dir.parent != project_root:
                skipped.append({**record, "reason": "not_direct_project_child"})
                continue
            if (
                workspace_dir.name in HYDRATION_EXCLUDED_NAMES
                or workspace_dir.name == LEGACY_BASELINE_DIR_NAME
            ):
                skipped.append({**record, "reason": "reserved_workspace_name"})
                continue
            candidates.append(record)
            if not dry_run:
                archive_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(workspace_dir), str(archive_dir))
                task.task_subfolder = None
                task.workspace_status = "not_created"
                task.promoted_at = None
                task.promotion_note = f"Archived retained workspace at {archive_dir}"
                task.updated_at = archived_at
                deleted.append(record)

        if not dry_run and deleted:
            self.db.commit()

        return {
            "project_id": project.id,
            "project_root": str(project_root),
            "dry_run": dry_run,
            "archive_root": str(archive_root),
            "selected_statuses": sorted(eligible_statuses),
            "candidate_count": len(candidates),
            "deleted_count": len(deleted),
            "candidates": candidates,
            "deleted": deleted,
            "skipped": skipped,
        }

    def archive_promoted_task_workspace(
        self,
        project: Project,
        task: Task,
        *,
        reason: str = "auto_published_to_baseline",
    ) -> dict[str, Any]:
        project_root = self.get_project_root(project).resolve()
        return self.canonical_mutations.run_locked(
            project,
            project_root=project_root,
            operation="archive_promoted_workspace",
            owner=f"task:{task.id}",
            fn=lambda: self.archive_promoted_task_workspace_unlocked(
                project,
                task,
                reason=reason,
                project_root=project_root,
            ),
        )

    def archive_promoted_task_workspace_unlocked(
        self,
        project: Project,
        task: Task,
        *,
        reason: str = "auto_published_to_baseline",
        project_root: Path | None = None,
    ) -> dict[str, Any]:
        project_root = project_root or self.get_project_root(project).resolve()
        task_subfolder = getattr(task, "task_subfolder", None)
        if not task_subfolder:
            return {"archived": False, "reason": "task_has_no_workspace"}

        workspace_dir = (project_root / task_subfolder).resolve()
        archive_root = (project_root / PROMOTED_WORKSPACE_ARCHIVE_ROOT).resolve()
        if workspace_dir == archive_root or workspace_dir.is_relative_to(archive_root):
            task.workspace_status = "promoted"
            task.promoted_at = getattr(task, "promoted_at", None) or datetime.now(UTC)
            return {
                "archived": False,
                "reason": "already_archived",
                "path": str(workspace_dir),
            }
        if not workspace_dir.exists():
            task.workspace_status = "promoted"
            task.promoted_at = getattr(task, "promoted_at", None) or datetime.now(UTC)
            return {
                "archived": False,
                "reason": "workspace_missing",
                "path": str(workspace_dir),
            }
        if workspace_dir.parent != project_root:
            task.workspace_status = "promoted"
            task.promoted_at = getattr(task, "promoted_at", None) or datetime.now(UTC)
            return {
                "archived": False,
                "reason": "not_direct_project_child",
                "path": str(workspace_dir),
            }
        if (
            workspace_dir.name in HYDRATION_EXCLUDED_NAMES
            or workspace_dir.name == LEGACY_BASELINE_DIR_NAME
        ):
            return {
                "archived": False,
                "reason": "reserved_workspace_name",
                "path": str(workspace_dir),
            }

        archived_at = datetime.now(UTC)
        archive_dir = (
            archive_root
            / archived_at.strftime("%Y%m%d-%H%M%S")
            / f"task-{task.id}-{workspace_dir.name}"
        ).resolve()
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(workspace_dir), str(archive_dir))

        archive_subfolder = archive_dir.relative_to(project_root).as_posix()
        existing_note = (getattr(task, "promotion_note", None) or "").strip()
        archive_note = f"Archived promoted workspace at {archive_dir} after {reason}"
        task.task_subfolder = archive_subfolder
        task.workspace_status = "promoted"
        task.promoted_at = archived_at
        task.promotion_note = (
            f"{existing_note}\n{archive_note}" if existing_note else archive_note
        )
        task.updated_at = archived_at
        return {
            "archived": True,
            "reason": reason,
            "path": str(workspace_dir),
            "archive_path": str(archive_dir),
            "task_subfolder": archive_subfolder,
        }

    def archive_task_workspace_for_repair_rerun(
        self,
        project: Project,
        task: Task,
        *,
        reason: str = "changes_requested_repair_rerun",
    ) -> dict[str, Any]:
        project_root = self.get_project_root(project).resolve()
        return self.canonical_mutations.run_locked(
            project,
            project_root=project_root,
            operation="archive_repair_workspace",
            owner=f"task:{task.id}",
            fn=lambda: self.archive_task_workspace_for_repair_rerun_unlocked(
                project,
                task,
                reason=reason,
                project_root=project_root,
            ),
        )

    def archive_task_workspace_for_repair_rerun_unlocked(
        self,
        project: Project,
        task: Task,
        *,
        reason: str = "changes_requested_repair_rerun",
        project_root: Path | None = None,
    ) -> dict[str, Any]:
        project_root = project_root or self.get_project_root(project).resolve()
        task_subfolder = getattr(task, "task_subfolder", None)
        if not task_subfolder:
            return {"archived": False, "reason": "task_has_no_workspace"}

        workspace_dir = (project_root / task_subfolder).resolve()
        if not workspace_dir.exists():
            task.task_subfolder = None
            task.workspace_status = "not_created"
            return {
                "archived": False,
                "reason": "workspace_missing",
                "path": str(workspace_dir),
            }
        if workspace_dir.parent != project_root:
            return {
                "archived": False,
                "reason": "not_direct_project_child",
                "path": str(workspace_dir),
            }
        if (
            workspace_dir.name in HYDRATION_EXCLUDED_NAMES
            or workspace_dir.name == LEGACY_BASELINE_DIR_NAME
        ):
            return {
                "archived": False,
                "reason": "reserved_workspace_name",
                "path": str(workspace_dir),
            }

        archived_at = datetime.now(UTC)
        archive_dir = (
            project_root
            / REQUESTED_CHANGES_ARCHIVE_ROOT
            / archived_at.strftime("%Y%m%d-%H%M%S")
            / f"task-{task.id}-{workspace_dir.name}"
        ).resolve()
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(workspace_dir), str(archive_dir))

        existing_note = (getattr(task, "promotion_note", None) or "").strip()
        archive_note = f"Archived previous workspace for repair rerun at {archive_dir}"
        task.task_subfolder = None
        task.workspace_status = "not_created"
        task.promoted_at = None
        task.promotion_note = (
            f"{existing_note}\n{archive_note}" if existing_note else archive_note
        )
        task.updated_at = archived_at
        return {
            "archived": True,
            "reason": reason,
            "path": str(workspace_dir),
            "archive_path": str(archive_dir),
        }

    def restore_archived_task_workspace(
        self,
        project: Project,
        task: Task,
        *,
        archive_path: str,
    ) -> dict[str, Any]:
        project_root = self.get_project_root(project).resolve()
        return self.canonical_mutations.run_locked(
            project,
            project_root=project_root,
            operation="restore_archived_workspace",
            owner=f"task:{task.id}",
            fn=lambda: self.restore_archived_task_workspace_unlocked(
                project,
                task,
                archive_path=archive_path,
                project_root=project_root,
            ),
        )

    def restore_archived_task_workspace_unlocked(
        self,
        project: Project,
        task: Task,
        *,
        archive_path: str,
        project_root: Path | None = None,
    ) -> dict[str, Any]:
        project_root = project_root or self.get_project_root(project).resolve()
        archive_dir = Path(archive_path).expanduser().resolve()
        allowed_roots = [
            (project_root / RETAINED_WORKSPACE_ARCHIVE_ROOT).resolve(),
            (project_root / REQUESTED_CHANGES_ARCHIVE_ROOT).resolve(),
        ]
        if not any(
            archive_dir == root or archive_dir.is_relative_to(root)
            for root in allowed_roots
        ):
            raise ValueError("archive path is outside this project's workspace archive")
        if not archive_dir.exists() or not archive_dir.is_dir():
            raise ValueError("archive path does not exist")
        if getattr(task, "task_subfolder", None):
            raise ValueError("task already has an active workspace")

        raw_name = archive_dir.name
        prefix = f"task-{task.id}-"
        restored_name = (
            raw_name[len(prefix) :] if raw_name.startswith(prefix) else raw_name
        )
        restored_name = restored_name.strip() or f"task-{task.id}-restored"
        target_dir = (project_root / restored_name).resolve()
        if target_dir.parent != project_root:
            raise ValueError("restored workspace name would escape project root")
        if target_dir.exists():
            suffix = int(datetime.now(UTC).timestamp())
            target_dir = (project_root / f"{restored_name}-restored-{suffix}").resolve()

        shutil.move(str(archive_dir), str(target_dir))
        task.task_subfolder = target_dir.name
        task.workspace_status = self.infer_workspace_status(task)
        task.updated_at = datetime.now(UTC)
        db_note = (getattr(task, "promotion_note", None) or "").strip()
        task.promotion_note = (
            f"{db_note}\nRestored archived workspace from {archive_dir}"
            if db_note
            else f"Restored archived workspace from {archive_dir}"
        )
        self.db.commit()
        self.db.refresh(task)
        return {
            "restored": True,
            "task_id": task.id,
            "archive_path": str(archive_dir),
            "workspace_path": str(target_dir),
            "task_subfolder": task.task_subfolder,
            "workspace_status": task.workspace_status,
        }

    def infer_workspace_status(self, task: Task) -> str:
        current_status = getattr(task, "workspace_status", None)
        if current_status == "changes_requested":
            return "changes_requested"
        if getattr(task, "promoted_at", None) or current_status == "promoted":
            return "promoted"
        if not getattr(task, "task_subfolder", None):
            return "not_created"
        if task.status == TaskStatus.DONE:
            return "ready"
        if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return "blocked"
        if task.status == TaskStatus.RUNNING:
            return "in_progress"
        return "isolated"
