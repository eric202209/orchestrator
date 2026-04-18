"""Task service - Business logic for tasks"""

import re
import shutil
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session
from datetime import datetime
from app.models import Task, TaskStatus, Project, SessionTask
from app.schemas import TaskUpdate
from app.services.project_isolation_service import resolve_project_workspace_path


HYDRATION_EXCLUDED_NAMES = {
    ".openclaw",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
}
TASK_REPORT_RE = re.compile(r"^task_report_\d+\.md$", re.IGNORECASE)
LEGACY_BASELINE_DIR_NAME = ".project-baseline"
AUTO_SNAPSHOT_ROOT = ".openclaw/auto-snapshots"


class TaskService:
    """Service for task operations"""

    def __init__(self, db: Session):
        self.db = db

    def get_task(self, task_id: int):
        """Get a task by ID"""
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if task:
            self.sync_workspace_status(task, commit=False)
        return task

    def get_project_tasks(self, project_id: int):
        """Get all tasks for a project"""
        tasks = (
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
        changed = False
        for task in tasks:
            changed = self.sync_workspace_status(task, commit=False) or changed
        if changed:
            self.db.commit()
        return tasks

    def get_project_root(self, project: Project) -> Path:
        return resolve_project_workspace_path(project.workspace_path, project.name)

    def _reserved_project_names(self, project: Project) -> set[str]:
        task_subfolders = {
            task.task_subfolder
            for task in self.get_project_tasks(project.id)
            if getattr(task, "task_subfolder", None)
        }
        reserved = set(HYDRATION_EXCLUDED_NAMES)
        reserved.add(LEGACY_BASELINE_DIR_NAME)
        reserved.update(task_subfolders)
        return reserved

    def create_workspace_snapshot(
        self,
        project: Project,
        source_dir: Path,
        *,
        snapshot_key: str,
        preserve_project_root_rules: bool = False,
    ) -> dict:
        source_dir = source_dir.resolve()
        project_root = self.get_project_root(project).resolve()
        snapshot_dir = (project_root / AUTO_SNAPSHOT_ROOT / snapshot_key).resolve()

        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        if not source_dir.exists():
            return {
                "snapshot_path": str(snapshot_dir),
                "source_path": str(source_dir),
                "files_copied": 0,
                "source_exists": False,
                "preserve_project_root_rules": preserve_project_root_rules,
            }

        files_copied = 0
        reserved_names = (
            self._reserved_project_names(project)
            if preserve_project_root_rules
            else set()
        )
        for source_path in source_dir.rglob("*"):
            if source_path.is_dir():
                continue
            relative = source_path.relative_to(source_dir)
            if preserve_project_root_rules and relative.parts:
                first_part = relative.parts[0]
                if first_part in reserved_names:
                    continue
            if any(part in HYDRATION_EXCLUDED_NAMES for part in relative.parts):
                continue
            destination = snapshot_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            files_copied += 1

        return {
            "snapshot_path": str(snapshot_dir),
            "source_path": str(source_dir),
            "files_copied": files_copied,
            "source_exists": True,
            "preserve_project_root_rules": preserve_project_root_rules,
        }

    def restore_workspace_snapshot(
        self,
        project: Project,
        target_dir: Path,
        *,
        snapshot_key: str,
        preserve_project_root_rules: bool = False,
    ) -> dict:
        target_dir = target_dir.resolve()
        project_root = self.get_project_root(project).resolve()
        snapshot_dir = (project_root / AUTO_SNAPSHOT_ROOT / snapshot_key).resolve()

        if not snapshot_dir.exists():
            return {
                "restored": False,
                "reason": "snapshot_missing",
                "snapshot_path": str(snapshot_dir),
                "target_path": str(target_dir),
                "files_restored": 0,
            }

        target_dir.mkdir(parents=True, exist_ok=True)
        reserved_names = (
            self._reserved_project_names(project)
            if preserve_project_root_rules
            else set()
        )

        for child in list(target_dir.iterdir()):
            if preserve_project_root_rules and child.name in reserved_names:
                continue
            if child.name in HYDRATION_EXCLUDED_NAMES:
                continue
            if (
                preserve_project_root_rules
                and child.name == AUTO_SNAPSHOT_ROOT.split("/")[-1]
            ):
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

        files_restored = 0
        for snapshot_path in snapshot_dir.rglob("*"):
            if snapshot_path.is_dir():
                continue
            relative = snapshot_path.relative_to(snapshot_dir)
            destination = target_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot_path, destination)
            files_restored += 1

        return {
            "restored": True,
            "snapshot_path": str(snapshot_dir),
            "target_path": str(target_dir),
            "files_restored": files_restored,
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

    def sync_workspace_status(self, task: Task, commit: bool = True) -> bool:
        if not task:
            return False

        inferred_status = self.infer_workspace_status(task)
        if getattr(task, "workspace_status", None) == inferred_status:
            return False

        task.workspace_status = inferred_status
        if inferred_status != "promoted" and getattr(task, "promoted_at", None):
            task.promoted_at = None
        if commit:
            self.db.commit()
            self.db.refresh(task)
        return True

    def build_project_execution_context(
        self,
        project: Optional[Project],
        current_task: Optional[Task],
        max_chars: int = 4000,
    ) -> str:
        """Summarize project progress and available prior work for planning/execution."""
        if not project:
            return "No project context available."

        tasks = self.get_project_tasks(project.id)
        current_order = getattr(current_task, "plan_position", None)

        promoted = []
        prior_done = []
        blocked = []
        lines = [
            f"Project: {project.name}",
            f"Project description: {project.description or 'None provided'}",
        ]
        baseline = self.get_project_baseline_overview(project)
        if baseline["exists"]:
            lines.append(
                f"Canonical baseline available: {baseline['file_count']} files at {baseline['path']}"
            )

        if current_task:
            lines.append(
                "Current task: "
                f"#{current_order if current_order is not None else 'manual'} "
                f"{current_task.title} ({current_task.status.value})"
            )

        for task in tasks:
            entry = (
                f"- #{task.plan_position if task.plan_position is not None else 'manual'} "
                f"{task.title} :: status={task.status.value} :: workspace={getattr(task, 'workspace_status', None) or 'unknown'}"
            )
            if getattr(task, "task_subfolder", None):
                entry += f" :: subfolder={task.task_subfolder}"

            if getattr(task, "workspace_status", None) == "promoted":
                promoted.append(entry)
            elif (
                current_task
                and current_order is not None
                and task.id != current_task.id
                and task.plan_position is not None
                and task.plan_position < current_order
                and task.status == TaskStatus.DONE
            ):
                prior_done.append(entry)
            elif (
                current_task
                and current_order is not None
                and task.id != current_task.id
                and task.plan_position is not None
                and task.plan_position < current_order
                and task.status != TaskStatus.DONE
            ):
                blocked.append(entry)

        if promoted:
            lines.append(
                "Promoted workspaces already accepted into the project baseline:"
            )
            lines.extend(promoted[:6])
        if prior_done:
            lines.append("Earlier ordered tasks already completed and can be reused:")
            lines.extend(prior_done[:6])
        if blocked:
            lines.append(
                "Earlier ordered tasks still incomplete and should not be ignored:"
            )
            lines.extend(blocked[:6])

        lines.append(
            "Important: treat hydrated files in the current task workspace as existing project baseline; extend them instead of recreating parallel copies."
        )

        context = "\n".join(lines)
        return context[:max_chars]

    def hydrate_task_workspace(
        self,
        project: Optional[Project],
        current_task: Optional[Task],
        target_dir: Path,
    ) -> dict:
        """Copy approved prior task artifacts into the current task workspace without overwriting."""
        if not project or not current_task:
            return {"hydrated": False, "source_tasks": [], "files_copied": 0}

        project_root = self.get_project_root(project)
        current_order = getattr(current_task, "plan_position", None)
        if current_order is None:
            return {"hydrated": False, "source_tasks": [], "files_copied": 0}

        target_dir.mkdir(parents=True, exist_ok=True)
        source_tasks = []
        files_copied = 0

        baseline_dirs = self.get_existing_project_baseline_dirs(project)
        for baseline_dir in baseline_dirs:
            copied = self._copy_tree_into_target(
                project=project,
                source_dir=baseline_dir,
                target_dir=target_dir,
                overwrite=False,
            )
            if copied:
                source_tasks.append(
                    {
                        "task_id": None,
                        "title": "project baseline",
                        "task_subfolder": baseline_dir.name,
                        "files_copied": copied,
                    }
                )
                files_copied += copied

        if files_copied > 0:
            return {
                "hydrated": bool(source_tasks),
                "source_tasks": source_tasks,
                "files_copied": files_copied,
            }

        candidate_tasks = []
        for task in self.get_project_tasks(project.id):
            if task.id == current_task.id or not getattr(task, "task_subfolder", None):
                continue
            if getattr(task, "workspace_status", None) == "promoted":
                candidate_tasks.append(task)
                continue
            if (
                task.plan_position is not None
                and task.plan_position < current_order
                and task.status == TaskStatus.DONE
            ):
                candidate_tasks.append(task)

        candidate_tasks.sort(
            key=lambda item: (
                item.plan_position if item.plan_position is not None else 10**9,
                item.created_at or datetime.min,
                item.id,
            )
        )

        seen_ids = set()

        for task in candidate_tasks:
            if task.id in seen_ids:
                continue
            seen_ids.add(task.id)
            source_dir = (project_root / task.task_subfolder).resolve()
            if not source_dir.exists() or source_dir == target_dir.resolve():
                continue

            copied_for_task = self._copy_tree_into_target(
                project=project,
                source_dir=source_dir,
                target_dir=target_dir,
                overwrite=True,
            )
            files_copied += copied_for_task

            if copied_for_task:
                source_tasks.append(
                    {
                        "task_id": task.id,
                        "title": task.title,
                        "task_subfolder": task.task_subfolder,
                        "files_copied": copied_for_task,
                    }
                )

        return {
            "hydrated": bool(source_tasks),
            "source_tasks": source_tasks,
            "files_copied": files_copied,
        }

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
            if source_dir.resolve() == project_root:
                if relative.parts:
                    first_part = relative.parts[0]
                    if (
                        first_part in task_subfolders
                        or first_part in HYDRATION_EXCLUDED_NAMES
                        or first_part == LEGACY_BASELINE_DIR_NAME
                    ):
                        continue
            if any(part in HYDRATION_EXCLUDED_NAMES for part in relative.parts):
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

    def promote_task_into_baseline(self, project: Project, task: Task) -> dict:
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

    def auto_publish_task_into_baseline(self, project: Project, task: Task) -> dict:
        """Publish a completed task into the canonical merged project workspace."""
        return self.promote_task_into_baseline(project, task)

    def rebuild_project_baseline(self, project: Project) -> dict:
        baseline_dir = self.get_project_baseline_dir(project)
        baseline_dir.mkdir(parents=True, exist_ok=True)
        self._clear_project_root_baseline_contents(project)

        merged_tasks = [
            task
            for task in self.get_project_tasks(project.id)
            if getattr(task, "task_subfolder", None)
            and (
                getattr(task, "workspace_status", None) == "promoted"
                or task.status == TaskStatus.DONE
            )
        ]

        applied_tasks = []
        total_files = 0
        for task in merged_tasks:
            result = self.promote_task_into_baseline(project, task)
            applied_tasks.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "files_copied": result["files_copied"],
                }
            )
            total_files += result["files_copied"]

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

    def _clear_project_root_baseline_contents(self, project: Project) -> None:
        project_root = self.get_project_root(project)
        task_subfolders = {
            task.task_subfolder
            for task in self.get_project_tasks(project.id)
            if getattr(task, "task_subfolder", None)
        }
        preserved_names = set(HYDRATION_EXCLUDED_NAMES)
        preserved_names.add(LEGACY_BASELINE_DIR_NAME)
        preserved_names.update(task_subfolders)

        for child in project_root.iterdir():
            if child.name in preserved_names:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

    def _count_baseline_files(self, project: Project, baseline_dir: Path) -> int:
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

    def get_project_baseline_overview(self, project: Optional[Project]) -> dict:
        if not project:
            return {
                "exists": False,
                "path": None,
                "file_count": 0,
                "promoted_task_count": 0,
            }

        baseline_dir = self.get_project_baseline_dir(project)
        legacy_dir = self.get_legacy_project_baseline_dir(project)
        file_count = self._count_baseline_files(project, baseline_dir)
        if file_count == 0 and legacy_dir.exists():
            file_count = self._count_baseline_files(project, legacy_dir)
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

    def update_task_status(
        self, task_id: int, new_status: TaskStatus, error_message: str = None
    ):
        """Update task status with validation"""
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        # Status transition validation
        valid_transitions = {
            TaskStatus.PENDING: [TaskStatus.RUNNING, TaskStatus.CANCELLED],
            TaskStatus.RUNNING: [TaskStatus.DONE, TaskStatus.FAILED],
            TaskStatus.FAILED: [TaskStatus.PENDING],
        }

        if new_status not in valid_transitions.get(task.status, []):
            raise ValueError(
                f"Invalid status transition from {task.status} to {new_status}"
            )

        task.status = new_status
        if new_status == TaskStatus.RUNNING:
            task.started_at = datetime.utcnow()
        elif new_status in [TaskStatus.DONE, TaskStatus.FAILED]:
            task.completed_at = datetime.utcnow()

        if error_message:
            task.error_message = error_message

        self.db.commit()
        self.db.refresh(task)
        return task

    def get_next_pending_task(self, project_id: int):
        """Get the next pending task whose earlier ordered tasks are already done."""
        tasks = (
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

        for task in tasks:
            if task.status != TaskStatus.PENDING:
                continue
            if not self.get_blocking_prior_tasks(task):
                return task
        return None

    def get_blocking_prior_tasks(self, task: Task):
        """Return earlier ordered tasks that must complete before this one can run."""
        if not task or task.plan_position is None:
            return []

        return (
            self.db.query(Task)
            .filter(
                Task.project_id == task.project_id,
                Task.plan_position.isnot(None),
                Task.plan_position < task.plan_position,
                Task.status != TaskStatus.DONE,
            )
            .order_by(
                Task.plan_position.asc(),
                Task.priority.desc(),
                Task.created_at.asc().nullslast(),
                Task.id.asc(),
            )
            .all()
        )

    def mark_step_complete(self, task_id: int, step_num: int):
        """Mark a step as complete and update current_step"""
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        task.current_step = step_num
        self.db.commit()
        self.db.refresh(task)
        return task

    def log_task_event(
        self,
        task_id: int,
        session_id: int,
        session_instance_id: str,
        level: str,
        message: str,
        metadata: dict = None,
    ):
        """Log an event for a task with proper instance isolation

        Args:
            task_id: Task ID
            session_id: Session ID (new parameter for proper isolation)
            session_instance_id: Instance UUID (new parameter for proper isolation)
            level: Log level
            message: Log message
            metadata: Optional metadata dict
        """
        from app.models import LogEntry

        # Insert log entry with instance tracking
        log = LogEntry(
            session_id=session_id,
            session_instance_id=session_instance_id,  # ✅ Critical for isolation
            task_id=task_id,
            level=level,
            message=message,
            metadata=metadata,
        )
        self.db.add(log)
        self.db.commit()
        return log
