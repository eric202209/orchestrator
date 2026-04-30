"""Task service - Business logic for tasks"""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import Project, Task, TaskStatus
from app.services.workspace.project_isolation_service import (
    _slugify_workspace_name,
    resolve_project_workspace_path,
)


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
        raw_workspace_path = str(project.workspace_path or "").strip()
        if raw_workspace_path.startswith("/"):
            explicit_path = Path(raw_workspace_path).expanduser().resolve()
            project_slug = _slugify_workspace_name(project.name or "")
            if explicit_path.name == project_slug:
                return explicit_path
            nested_candidate = explicit_path / project_slug
            if nested_candidate.exists():
                return nested_candidate.resolve()
            return explicit_path
        return resolve_project_workspace_path(project.workspace_path, project.name)

    def analyze_workspace_consistency(
        self,
        target_dir: Path,
        *,
        ignored_top_level_dirs: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        """Inspect a workspace for mixed stacks or nested duplicate implementations."""
        target_dir = target_dir.resolve()
        ignored_top_level_dirs = set(ignored_top_level_dirs or set())

        if not target_dir.exists():
            return {
                "exists": False,
                "dominant_stack": "none",
                "mixed_stack": False,
                "python_source_count": 0,
                "node_source_count": 0,
                "python_markers": [],
                "node_markers": [],
                "nested_duplicate_dirs": [],
                "issues": [],
            }

        python_files: list[str] = []
        node_files: list[str] = []
        python_markers: list[str] = []
        node_markers: list[str] = []
        nested_duplicate_dirs: list[str] = []

        marker_files = {
            "requirements.txt": "python",
            "pyproject.toml": "python",
            "setup.py": "python",
            "package.json": "node",
            "tsconfig.json": "node",
            "pnpm-lock.yaml": "node",
            "package-lock.json": "node",
        }

        for path in target_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(target_dir)
            if not relative.parts:
                continue
            if any(part in HYDRATION_EXCLUDED_NAMES for part in relative.parts):
                continue
            if TASK_REPORT_RE.match(path.name):
                continue
            if relative.parts[0] in ignored_top_level_dirs:
                continue

            relative_text = str(relative)
            suffix = path.suffix.lower()

            marker_stack = marker_files.get(path.name.lower())
            if marker_stack == "python" and len(python_markers) < 10:
                python_markers.append(relative_text)
            elif marker_stack == "node" and len(node_markers) < 10:
                node_markers.append(relative_text)

            if suffix == ".py":
                python_files.append(relative_text)
            elif suffix in {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}:
                node_files.append(relative_text)

        for child in target_dir.iterdir():
            if not child.is_dir():
                continue
            if (
                child.name in HYDRATION_EXCLUDED_NAMES
                or child.name in ignored_top_level_dirs
            ):
                continue
            if child.name != target_dir.name:
                continue
            nested_source_files = [
                path
                for path in child.rglob("*")
                if path.is_file()
                and path.suffix.lower()
                in {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}
            ]
            if nested_source_files:
                nested_duplicate_dirs.append(child.name)

        mixed_stack = bool(python_files and node_files)
        if mixed_stack:
            dominant_stack = "mixed"
        elif node_files or node_markers:
            dominant_stack = "node"
        elif python_files or python_markers:
            dominant_stack = "python"
        else:
            dominant_stack = "none"

        issues: list[str] = []
        if mixed_stack:
            issues.append(
                "Workspace contains both Python and Node/JS implementation artifacts"
            )
        if nested_duplicate_dirs:
            issues.append(
                "Workspace contains a nested duplicate task directory: "
                + ", ".join(nested_duplicate_dirs[:4])
            )

        return {
            "exists": True,
            "dominant_stack": dominant_stack,
            "mixed_stack": mixed_stack,
            "python_source_count": len(python_files),
            "node_source_count": len(node_files),
            "python_markers": python_markers[:10],
            "node_markers": node_markers[:10],
            "python_files": python_files[:20],
            "node_files": node_files[:20],
            "nested_duplicate_dirs": nested_duplicate_dirs[:10],
            "issues": issues,
        }

    def _parse_task_steps(self, task: Task) -> list[dict]:
        raw_steps = getattr(task, "steps", None)
        if not raw_steps:
            return []
        try:
            parsed = json.loads(raw_steps) if isinstance(raw_steps, str) else raw_steps
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    def get_task_expected_files(
        self,
        task: Task,
        *,
        existing_root: Optional[Path] = None,
        prefer_existing_for_completed: bool = False,
    ) -> list[str]:
        expected_files: list[str] = []
        seen = set()
        for step in self._parse_task_steps(task):
            raw_files = step.get("expected_files", [])
            if not isinstance(raw_files, list):
                continue
            for raw_path in raw_files:
                normalized = str(raw_path or "").strip().strip("\"'")
                if (
                    not normalized
                    or normalized in seen
                    or normalized.startswith("/")
                    or normalized.startswith("..")
                ):
                    continue
                seen.add(normalized)
                expected_files.append(normalized)
        if (
            existing_root is not None
            and prefer_existing_for_completed
            and getattr(task, "status", None) == TaskStatus.DONE
        ):
            root = existing_root.resolve()
            existing_expected_files = [
                relative_path
                for relative_path in expected_files
                if (root / relative_path).exists()
            ]
            if existing_expected_files:
                return existing_expected_files
        return expected_files

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
        snapshot_files = [
            path
            for path in snapshot_dir.rglob("*")
            if path.is_file()
            and not any(
                part in HYDRATION_EXCLUDED_NAMES
                for part in path.relative_to(snapshot_dir).parts
            )
        ]
        current_workspace_files = [
            path
            for path in target_dir.rglob("*")
            if path.is_file()
            and not any(
                part in HYDRATION_EXCLUDED_NAMES
                for part in path.relative_to(target_dir).parts
            )
        ]
        if not snapshot_files and current_workspace_files:
            return {
                "restored": False,
                "reason": "empty_snapshot_preserved_existing_workspace",
                "snapshot_path": str(snapshot_dir),
                "target_path": str(target_dir),
                "files_restored": 0,
                "current_workspace_files": len(current_workspace_files),
            }
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
        for snapshot_path in snapshot_files:
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
        if project.project_rules:
            lines.append(f"Project rules: {project.project_rules}")
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

        if current_task and getattr(current_task, "plan_position", None) is not None:
            lines.append(
                "Important: execute directly in the canonical project root. Treat the "
                "current project folder as the source of truth and do not create a "
                "parallel top-level task workspace."
            )
        else:
            lines.append(
                "Important: treat hydrated files in the current task workspace as existing project baseline; extend them instead of recreating parallel copies."
            )

        context = "\n".join(lines)
        return context[:max_chars]

    def review_existing_workspace(
        self,
        project: Optional[Project],
        current_task: Optional[Task],
        target_dir: Path,
        max_chars: int = 2500,
    ) -> dict:
        """Summarize existing task workspace content and obvious implementation risks."""
        target_dir = target_dir.resolve()
        if not target_dir.exists():
            return {
                "has_existing_files": False,
                "file_count": 0,
                "summary": "Existing workspace review: task workspace is empty.",
            }

        files: list[Path] = []
        for path in target_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(target_dir)
            if any(part in HYDRATION_EXCLUDED_NAMES for part in relative.parts):
                continue
            files.append(path)

        files.sort(key=lambda item: str(item.relative_to(target_dir)))
        if not files:
            return {
                "has_existing_files": False,
                "file_count": 0,
                "summary": "Existing workspace review: no materialized source files yet.",
            }

        key_files: list[str] = []
        issues: list[str] = []
        source_count = 0
        placeholder_count = 0

        for path in files:
            relative_text = str(path.relative_to(target_dir))
            suffix = path.suffix.lower()
            if suffix in {".py", ".js", ".ts", ".tsx", ".jsx"}:
                source_count += 1
            if len(key_files) < 12 and (
                suffix
                in {
                    ".py",
                    ".js",
                    ".ts",
                    ".tsx",
                    ".jsx",
                    ".json",
                    ".yaml",
                    ".yml",
                    ".toml",
                }
                or path.name.lower()
                in {"package.json", "requirements.txt", "pyproject.toml", "main.py"}
            ):
                key_files.append(relative_text)
            if suffix not in {".py", ".js", ".ts", ".tsx", ".jsx"}:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                continue
            lowered = content.lower()
            if (
                "todo" in lowered
                or "placeholder" in lowered
                or "notimplemented" in lowered
            ):
                placeholder_count += 1
                if len(issues) < 6:
                    issues.append(f"{relative_text} contains TODO/placeholder markers")
            if "\npass\n" in content or content.strip().endswith("pass"):
                placeholder_count += 1
                if len(issues) < 6:
                    issues.append(f"{relative_text} still contains `pass` placeholders")
            if "__main__" in content and "if __name__ == __main__" in content:
                if len(issues) < 6:
                    issues.append(f"{relative_text} has a broken Python __main__ check")

        consistency = self.analyze_workspace_consistency(target_dir)

        workspace_label = (
            "canonical project root"
            if current_task and getattr(current_task, "plan_position", None) is not None
            else "task workspace"
        )
        lines = [
            "Existing workspace review:",
            f"- {workspace_label}: {target_dir}",
            f"- file count: {len(files)}",
            f"- source file count: {source_count}",
        ]
        if (
            current_task
            and getattr(current_task, "plan_position", None) is None
            and getattr(current_task, "task_subfolder", None)
            and project
        ):
            lines.append(
                "- note: the project root is the canonical merged baseline and "
                f"`{current_task.task_subfolder}` is the isolated task workspace; overlapping files are expected after publish"
            )
        if key_files:
            lines.append("- key existing files: " + ", ".join(key_files[:12]))
        if issues:
            lines.append("- existing implementation risks: " + "; ".join(issues[:6]))
        elif source_count > 0:
            lines.append(
                "- existing implementation detected; extend or fix current files instead of creating parallel replacements"
            )
        else:
            lines.append(
                "- workspace currently looks scaffold-heavy; create missing implementation carefully"
            )
        if consistency.get("mixed_stack"):
            lines.append(
                "- workspace currently mixes Python and Node/JS implementation files; unify around the dominant intended stack instead of leaving both"
            )
        if consistency.get("nested_duplicate_dirs"):
            lines.append(
                "- nested duplicate task directories detected: "
                + ", ".join(consistency.get("nested_duplicate_dirs", [])[:4])
            )

        summary = "\n".join(lines)
        return {
            "has_existing_files": True,
            "file_count": len(files),
            "source_file_count": source_count,
            "placeholder_issue_count": placeholder_count,
            "consistency": consistency,
            "summary": summary[:max_chars],
        }

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

    def validate_task_baseline_materialization(
        self, project: Project, task: Task
    ) -> dict:
        baseline_dir = self.get_project_baseline_dir(project)
        expected_files = self.get_task_expected_files(
            task,
            existing_root=baseline_dir,
            prefer_existing_for_completed=True,
        )
        missing_expected_files = [
            relative_path
            for relative_path in expected_files
            if not (baseline_dir / relative_path).exists()
        ]
        overview = self.get_project_baseline_overview(project)
        ignored_top_level_dirs = {
            existing_task.task_subfolder
            for existing_task in self.get_project_tasks(project.id)
            if getattr(existing_task, "task_subfolder", None)
        }
        ignored_top_level_dirs.update(HYDRATION_EXCLUDED_NAMES)
        ignored_top_level_dirs.add(LEGACY_BASELINE_DIR_NAME)
        consistency = self.analyze_workspace_consistency(
            baseline_dir,
            ignored_top_level_dirs=ignored_top_level_dirs,
        )
        return {
            "baseline_path": overview["path"],
            "baseline_file_count": overview["file_count"],
            "expected_files": expected_files,
            "missing_expected_files": missing_expected_files,
            "consistency": consistency,
            "consistency_issues": consistency.get("issues", []),
        }

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

    def validate_project_baseline(
        self, project: Project, current_task: Optional[Task] = None
    ) -> dict:
        baseline_dir = self.get_project_baseline_dir(project)
        baseline_overview = self.get_project_baseline_overview(project)
        tasks = self.get_project_tasks(project.id)

        if current_task and getattr(current_task, "plan_position", None) is not None:
            cutoff = current_task.plan_position
            candidate_tasks = [
                task
                for task in tasks
                if task.id != current_task.id
                and task.plan_position is not None
                and task.plan_position < cutoff
                and task.status == TaskStatus.DONE
            ]
        else:
            candidate_tasks = [task for task in tasks if task.status == TaskStatus.DONE]

        missing_expected_files = []
        tasks_with_expected_files = []
        for task in candidate_tasks:
            expected_files = self.get_task_expected_files(
                task,
                existing_root=baseline_dir,
                prefer_existing_for_completed=True,
            )
            if not expected_files:
                continue
            tasks_with_expected_files.append(task.id)
            for relative_path in expected_files:
                if not (baseline_dir / relative_path).exists():
                    missing_expected_files.append(
                        {
                            "task_id": task.id,
                            "title": task.title,
                            "plan_position": task.plan_position,
                            "path": relative_path,
                        }
                    )

        return {
            "baseline_exists": baseline_overview["exists"],
            "baseline_path": baseline_overview["path"],
            "baseline_file_count": baseline_overview["file_count"],
            "validated_task_count": len(candidate_tasks),
            "tasks_with_expected_files": tasks_with_expected_files,
            "missing_expected_files": missing_expected_files,
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
