"""Canonical baseline, archive, and cleanup ownership service."""

from __future__ import annotations

import logging
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import Project, Task, TaskCheckpoint, TaskExecution, TaskStatus
from app.services.orchestration.state.persistence import load_accepted_path_authority
from app.services.orchestration.validation.candidate_checks import (
    candidate_delta_identity,
)
from app.services.orchestration.validation.path_authority import (
    PathAuthorityError,
    publication_scope_violations,
)
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
    is_executor_runtime_scaffold,
    is_hydration_excluded_path,
    resolve_project_root,
)

logger = logging.getLogger(__name__)


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
        pattern = re.compile(
            rf"{re.escape(PROJECT_GITIGNORE_GUARD_START)}.*?{re.escape(PROJECT_GITIGNORE_GUARD_END)}",
            re.DOTALL,
        )
        guard_block = self._gitignore_guard_block(PROJECT_GITIGNORE_GUARD_LINES)
        if pattern.search(existing):
            if self._gitignore_already_covers_guard_entries(existing):
                updated = existing
            else:
                updated = pattern.sub(guard_block, existing)
        elif self._gitignore_already_covers_guard_entries(existing):
            return {
                "changed": False,
                "path": str(gitignore_path),
                "entries": PROJECT_GITIGNORE_GUARD_LINES,
                "reason": "entries_already_present",
            }
        else:
            missing_entries = self._missing_gitignore_guard_entries(existing)
            guard_block = self._gitignore_guard_block(missing_entries)
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

    @staticmethod
    def _gitignore_guard_block(entries: list[str]) -> str:
        return "\n".join(
            [
                PROJECT_GITIGNORE_GUARD_START,
                *entries,
                PROJECT_GITIGNORE_GUARD_END,
            ]
        )

    @staticmethod
    def _gitignore_already_covers_guard_entries(existing: str) -> bool:
        existing_rules = BaselinePromotionService._gitignore_rules(existing)
        return all(entry in existing_rules for entry in PROJECT_GITIGNORE_GUARD_LINES)

    @staticmethod
    def _missing_gitignore_guard_entries(existing: str) -> list[str]:
        existing_rules = BaselinePromotionService._gitignore_rules(existing)
        return [
            entry
            for entry in PROJECT_GITIGNORE_GUARD_LINES
            if entry not in existing_rules
        ]

    @staticmethod
    def _gitignore_rules(existing: str) -> set[str]:
        existing_rules = {
            line.strip()
            for line in existing.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        return existing_rules

    def _iter_copy_candidates(
        self, project: Project, source_dir: Path
    ) -> list[tuple[Path, Path]]:
        project_root = self.get_project_root(project).resolve()
        task_subfolders = {
            task.task_subfolder
            for task in self.get_project_tasks(project.id)
            if getattr(task, "task_subfolder", None)
        }
        candidates: list[tuple[Path, Path]] = []
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
            candidates.append((relative, source_path))
        return candidates

    def _copy_tree_into_target(
        self,
        project: Project,
        source_dir: Path,
        target_dir: Path,
        overwrite: bool,
    ) -> int:
        candidates = self._iter_copy_candidates(project, source_dir)
        for relative, _source_path in candidates:
            if not self._destination_is_containable(target_dir, relative):
                raise PathAuthorityError(
                    "publication_destination_symlink",
                    f"unsafe destination contains a symlink: {relative.as_posix()}",
                )

        copied = 0
        for relative, source_path in candidates:
            destination = target_dir / relative
            if destination.exists() and not overwrite:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            copied += 1
        return copied

    def _change_set_artifact_files_root(
        self, project: Project, task_execution_id: int
    ) -> Path:
        return (
            self.get_project_root(project)
            / ".agent"
            / "change-sets"
            / str(task_execution_id)
            / "files"
        )

    @staticmethod
    def _safe_change_set_relative_path(relative_path: str) -> Optional[Path]:
        path = Path(relative_path)
        if (
            not relative_path
            or path.is_absolute()
            or ".." in path.parts
            or is_hydration_excluded_path(path)
            or TASK_REPORT_RE.match(path.name)
        ):
            return None
        return path

    @staticmethod
    def _destination_is_containable(baseline_dir: Path, relative: Path) -> bool:
        """Reject a baseline write destination reachable only through a symlink.

        Phase 33C-1: `_safe_change_set_relative_path` is a *lexical* declaration
        check and provably cannot see a symlink, so a `some/link -> /etc` segment
        already present in the canonical baseline would make `shutil.copy2` write
        through it and mutate a target outside the baseline.  Each segment is
        observed with `lstat` semantics (`Path.is_symlink`), never resolved, so
        the check is symmetric with the trusted-inventory rule: a symlink is
        observed as a symlink, and an untrusted write through one fails closed.
        """

        current = baseline_dir
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
        return True

    def _load_publication_authority(
        self,
        *,
        project: Project,
        task: Task,
        change_set: dict[str, Any],
        source_dir: Path,
        require_candidate_identity: bool = True,
    ) -> dict[str, Any] | None:
        """Bind a Change Set to the persisted APA before any baseline write.

        Publication consumes the generic task/session/TaskExecution/Plan-fenced
        loader.  It does not compare the physical staging path to
        ``workspace_identity``: the Change Set artifact is the existing durable
        snapshot-transfer boundary, and candidate identity plus the exact
        execution lineage are the continuity proof after Runtime Workspace
        disposal.

        ``db is None`` is retained only for provider-free lower-level service
        tests that exercise copy mechanics without an orchestration graph.  All
        production TaskService instances have a database session and fail closed
        when the authority or validation lineage is absent.
        """

        if self.db is None:
            return None
        task_execution_id = change_set.get("task_execution_id")
        try:
            task_execution_id = int(task_execution_id)
        except (TypeError, ValueError) as exc:
            raise PathAuthorityError(
                "publication_execution_identity_missing",
                "publication Change Set has no valid task_execution_id",
            ) from exc
        execution = (
            self.db.query(TaskExecution)
            .filter(TaskExecution.id == task_execution_id)
            .one_or_none()
        )
        if execution is None:
            raise PathAuthorityError(
                "publication_execution_missing",
                f"TaskExecution {task_execution_id} does not exist",
            )
        if execution.task_id != task.id:
            raise PathAuthorityError(
                "publication_task_identity_mismatch",
                f"TaskExecution {task_execution_id} belongs to task {execution.task_id}",
            )
        if change_set.get("task_id") not in {None, task.id}:
            raise PathAuthorityError(
                "publication_change_set_task_mismatch",
                f"Change Set belongs to task {change_set.get('task_id')}",
            )
        if change_set.get("project_id") not in {None, project.id}:
            raise PathAuthorityError(
                "publication_change_set_project_mismatch",
                f"Change Set belongs to project {change_set.get('project_id')}",
            )
        session_id = change_set.get("session_id")
        if session_id is None:
            session_id = execution.session_id
        if int(session_id) != int(execution.session_id):
            raise PathAuthorityError(
                "publication_session_identity_mismatch",
                f"Change Set session {session_id} does not match execution session {execution.session_id}",
            )
        raw_plan = getattr(task, "steps", None)
        try:
            plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
        except (TypeError, ValueError) as exc:
            raise PathAuthorityError(
                "publication_plan_invalid",
                "Task plan cannot be decoded for publication binding",
            ) from exc
        if not isinstance(plan, list):
            raise PathAuthorityError(
                "publication_plan_missing",
                "Task has no accepted executable Plan for publication binding",
            )
        authority = load_accepted_path_authority(
            self.db,
            task_id=task.id,
            session_id=int(execution.session_id),
            task_execution_id=task_execution_id,
            plan=plan,
            workspace_identity=None,
        )
        violations = publication_scope_violations(
            authority,
            added_paths=change_set.get("added_files") or [],
            modified_paths=change_set.get("modified_files") or [],
            deleted_paths=change_set.get("deleted_files") or [],
        )
        if violations:
            raise PathAuthorityError(
                "publication_scope_violation",
                json.dumps(
                    {
                        "authority_identity": authority.authority_identity,
                        "violations": list(violations)[:20],
                    },
                    sort_keys=True,
                ),
            )

        if not require_candidate_identity:
            return {
                "_authority": authority,
                "authority_identity": authority.authority_identity,
                "accepted_plan_identity": authority.accepted_plan_identity,
                "task_execution_id": task_execution_id,
                "session_id": int(execution.session_id),
            }

        candidate_identity = candidate_delta_identity(
            change_set, project_dir=source_dir
        )
        checkpoints = (
            self.db.query(TaskCheckpoint)
            .filter(
                TaskCheckpoint.task_id == task.id,
                TaskCheckpoint.session_id == int(execution.session_id),
                TaskCheckpoint.checkpoint_type == "validation_task_completion",
            )
            .order_by(TaskCheckpoint.id.asc())
            .all()
        )
        validated = False
        for checkpoint in checkpoints:
            if (
                execution.created_at is not None
                and checkpoint.created_at is not None
                and checkpoint.created_at < execution.created_at
            ):
                continue
            try:
                verdict = json.loads(checkpoint.state_snapshot or "")
            except (TypeError, ValueError):
                continue
            if (
                isinstance(verdict, dict)
                and verdict.get("status") in {"accepted", "warning"}
                and verdict.get("candidate_identity") == candidate_identity
            ):
                validated = True
                break
        if not validated:
            raise PathAuthorityError(
                "publication_candidate_identity_unvalidated",
                json.dumps(
                    {
                        "authority_identity": authority.authority_identity,
                        "candidate_delta_identity": candidate_identity,
                        "stage": "candidate_validation",
                    },
                    sort_keys=True,
                ),
            )
        return {
            "_authority": authority,
            "authority_identity": authority.authority_identity,
            "accepted_plan_identity": authority.accepted_plan_identity,
            "candidate_delta_identity": candidate_identity,
            "task_execution_id": task_execution_id,
            "session_id": int(execution.session_id),
        }

    def _load_task_workspace_publication_authority(
        self,
        *,
        project: Project,
        task: Task,
        source_dir: Path,
        target_dir: Path,
    ) -> dict[str, Any] | None:
        """Bind legacy whole-task publication to one unambiguous execution.

        Whole-task promotion predates the captured Change Set route and has no
        candidate-delta identity.  Its durable continuity proof is therefore
        the sole TaskExecution for the task, its accepted Plan, and the APA;
        multiple or missing executions fail closed rather than selecting an
        unfenced latest record.  Baseline rebuilds intentionally bypass this
        candidate gate because they reconstitute already-promoted workspaces.
        """

        if self.db is None:
            return None
        executions = (
            self.db.query(TaskExecution)
            .filter(TaskExecution.task_id == task.id)
            .order_by(TaskExecution.id.asc())
            .all()
        )
        if len(executions) != 1:
            raise PathAuthorityError(
                "publication_execution_ambiguous",
                f"whole-task publication requires exactly one TaskExecution; found {len(executions)}",
            )
        execution = executions[0]
        authority_metadata = self._load_publication_authority(
            project=project,
            task=task,
            change_set={
                "project_id": project.id,
                "task_id": task.id,
                "session_id": execution.session_id,
                "task_execution_id": execution.id,
                "added_files": [],
                "modified_files": [],
                "deleted_files": [],
            },
            source_dir=source_dir,
            require_candidate_identity=False,
        )
        authority = authority_metadata["_authority"]
        added: list[str] = []
        modified: list[str] = []
        for relative, _source_path in self._iter_copy_candidates(project, source_dir):
            if not self._destination_is_containable(target_dir, relative):
                raise PathAuthorityError(
                    "publication_destination_symlink",
                    f"unsafe destination contains a symlink: {relative.as_posix()}",
                )
            if (target_dir / relative).exists():
                modified.append(relative.as_posix())
            else:
                added.append(relative.as_posix())
        violations = publication_scope_violations(
            # The loader always returns a publication authority for production
            # calls; the conditional keeps the db-free unit seam typed.
            authority=authority,
            added_paths=added,
            modified_paths=modified,
        )
        if violations:
            raise PathAuthorityError(
                "publication_scope_violation",
                json.dumps(
                    {
                        "authority_identity": authority.authority_identity,
                        "violations": list(violations)[:20],
                    },
                    sort_keys=True,
                ),
            )
        return authority_metadata

    def promote_change_set_into_baseline(
        self,
        project: Project,
        task: Task,
        change_set: dict[str, Any],
        *,
        lock_already_held: bool = False,
    ) -> dict[str, Any]:
        project_root = self.get_project_root(project)
        task_execution_id = int(change_set["task_execution_id"])
        if lock_already_held:
            # The calling dispatch already owns the canonical-root mutation
            # lock for this project (worker canonical-baseline execution,
            # operation=execute_canonical_root_task). The lock is not
            # reentrant, so re-acquiring here would self-conflict and fail
            # the promotion.
            result = self.promote_change_set_into_baseline_unlocked(
                project, task, change_set
            )
        else:
            result = self.canonical_mutations.run_locked(
                project,
                project_root=project_root,
                operation="promote_change_set",
                owner=f"task:{task.id}:execution:{task_execution_id}",
                fn=lambda: self.promote_change_set_into_baseline_unlocked(
                    project, task, change_set
                ),
            )
        self._trigger_engineering_context_generation(project)
        return result

    def promote_change_set_into_baseline_unlocked(
        self,
        project: Project,
        task: Task,
        change_set: dict[str, Any],
    ) -> dict[str, Any]:
        task_execution_id = int(change_set["task_execution_id"])
        source_dir = self._change_set_artifact_files_root(project, task_execution_id)
        if not source_dir.exists():
            target_path = change_set.get("target_path")
            if target_path:
                candidate = Path(str(target_path)).expanduser().resolve()
                if candidate.exists():
                    source_dir = candidate
        if not source_dir.exists():
            raise FileNotFoundError(
                f"No durable change-set artifact found for task_execution_id={task_execution_id}"
            )
        publication_authority = self._load_publication_authority(
            project=project,
            task=task,
            change_set=change_set,
            source_dir=source_dir,
        )
        baseline_dir = self.get_project_baseline_dir(project)

        # Preflight every destination before the first copy/delete so one
        # unsafe path cannot leave a partially applied publication behind.
        for relative_path in sorted(
            set(change_set.get("added_files") or [])
            | set(change_set.get("modified_files") or [])
            | set(change_set.get("deleted_files") or [])
        ):
            relative = self._safe_change_set_relative_path(relative_path)
            if relative is not None and not self._destination_is_containable(
                baseline_dir, relative
            ):
                if self.db is None:
                    continue
                raise PathAuthorityError(
                    "publication_destination_symlink",
                    f"unsafe destination contains a symlink: {relative.as_posix()}",
                )
        baseline_dir.mkdir(parents=True, exist_ok=True)

        files_copied = 0
        files_deleted = 0
        for relative_path in sorted(
            set(change_set.get("added_files") or [])
            | set(change_set.get("modified_files") or [])
        ):
            relative = self._safe_change_set_relative_path(relative_path)
            if relative is None:
                continue
            source_path = (source_dir / relative).resolve()
            try:
                source_path.relative_to(source_dir.resolve())
            except ValueError:
                continue
            if not source_path.is_file():
                continue
            # Defense in depth: capture should already remove executor
            # scaffolding, but promotion must never apply it if an older or
            # hand-built artifact contains the generated AGENTS.md.
            if is_executor_runtime_scaffold(source_path):
                continue
            if not self._destination_is_containable(baseline_dir, relative):
                if self.db is None:
                    continue
                raise PathAuthorityError(
                    "publication_destination_symlink",
                    f"unsafe destination contains a symlink: {relative.as_posix()}",
                )
            destination = baseline_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            files_copied += 1

        for relative_path in sorted(set(change_set.get("deleted_files") or [])):
            relative = self._safe_change_set_relative_path(relative_path)
            if relative is None:
                continue
            if not self._destination_is_containable(baseline_dir, relative):
                if self.db is None:
                    continue
                raise PathAuthorityError(
                    "publication_destination_symlink",
                    f"unsafe destination contains a symlink: {relative.as_posix()}",
                )
            destination = baseline_dir / relative
            if destination.exists() and destination.is_file():
                destination.unlink()
                files_deleted += 1

        return {
            "baseline_path": str(baseline_dir),
            "files_copied": files_copied,
            "files_deleted": files_deleted,
            "source": "change_set_artifact",
            "artifact_path": str(source_dir),
            "task_execution_id": task_execution_id,
            **(
                {
                    "publication_authority": {
                        key: value
                        for key, value in publication_authority.items()
                        if key != "_authority"
                    }
                }
                if publication_authority is not None
                else {}
            ),
        }

    def promote_task_into_baseline(
        self, project: Project, task: Task
    ) -> dict[str, Any]:
        project_root = self.get_project_root(project)
        result = self.canonical_mutations.run_locked(
            project,
            project_root=project_root,
            operation="promote_task",
            owner=f"task:{task.id}",
            fn=lambda: self.promote_task_into_baseline_unlocked(project, task),
        )
        self._trigger_engineering_context_generation(project)
        return result

    def _trigger_engineering_context_generation(self, project: Project) -> None:
        """Run the post-Promotion hook without changing Promotion success."""

        try:
            from app.services.engineering_context import EngineeringContextService

            result = EngineeringContextService().generate_for_promotion(
                self.get_project_root(project)
            )
            logger.info(
                "[ENGINEERING_CONTEXT] promotion_hook_result project_id=%s result=%s",
                project.id,
                {key: value for key, value in result.items() if key != "source"},
            )
        except Exception as exc:
            # Context generation is a separate lifecycle concern. A successful
            # canonical Promotion must remain successful if the hook fails.
            logger.exception(
                "[ENGINEERING_CONTEXT] promotion_hook_failed project_id=%s reason=%s",
                project.id,
                str(exc)[:240],
            )

    def promote_task_into_baseline_unlocked(
        self,
        project: Project,
        task: Task,
        *,
        enforce_publication_authority: bool = True,
    ) -> dict[str, Any]:
        baseline_dir = self.get_project_baseline_dir(project)
        if not task.task_subfolder:
            return {"baseline_path": str(baseline_dir), "files_copied": 0}

        project_root = self.get_project_root(project)
        source_dir = (project_root / task.task_subfolder).resolve()
        if not source_dir.exists():
            return {"baseline_path": str(baseline_dir), "files_copied": 0}

        publication_authority = None
        if enforce_publication_authority:
            publication_authority = self._load_task_workspace_publication_authority(
                project=project,
                task=task,
                source_dir=source_dir,
                target_dir=baseline_dir,
            )
        baseline_dir.mkdir(parents=True, exist_ok=True)

        files_copied = self._copy_tree_into_target(
            project=project,
            source_dir=source_dir,
            target_dir=baseline_dir,
            overwrite=True,
        )
        result = {"baseline_path": str(baseline_dir), "files_copied": files_copied}
        if publication_authority is not None:
            result["publication_authority"] = {
                key: value
                for key, value in publication_authority.items()
                if key != "_authority"
            }
        return result

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
            result = self.promote_task_into_baseline_unlocked(
                project, task, enforce_publication_authority=False
            )
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
