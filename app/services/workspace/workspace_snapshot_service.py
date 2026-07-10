"""Workspace snapshot capture and restore ownership service."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models import Project, Task
from app.services.workspace.canonical_mutation_service import CanonicalMutationService
from app.services.workspace.permissions import (
    ensure_shared_path_to_root,
    ensure_shared_permissions,
)
from app.services.workspace.workspace_paths import (
    AUTO_SNAPSHOT_DIR_NAME,
    AUTO_SNAPSHOT_ROOT,
    HYDRATION_EXCLUDED_NAMES,
    LEGACY_BASELINE_DIR_NAME,
    is_hydration_excluded_path,
    resolve_project_root,
)


class WorkspaceSnapshotService:
    """Own snapshot key paths, capture, and restore operations."""

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

    def reserved_project_names(self, project: Project) -> set[str]:
        task_subfolders = {
            task.task_subfolder
            for task in self.db.query(Task).filter(Task.project_id == project.id).all()
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
        snapshot_root: Path | None = None,
    ) -> dict[str, Any]:
        source_dir = source_dir.resolve()
        project_root = self.get_project_root(project).resolve()
        snapshot_anchor = (snapshot_root or project_root).resolve()
        snapshot_dir = (snapshot_anchor / AUTO_SNAPSHOT_ROOT / snapshot_key).resolve()

        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        ensure_shared_path_to_root(snapshot_dir, snapshot_anchor)

        if not source_dir.exists():
            return {
                "snapshot_path": str(snapshot_dir),
                "snapshot_root": str(snapshot_anchor),
                "source_path": str(source_dir),
                "files_copied": 0,
                "source_exists": False,
                "preserve_project_root_rules": preserve_project_root_rules,
            }

        files_copied = 0
        reserved_names = (
            self.reserved_project_names(project)
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
            if is_hydration_excluded_path(relative):
                continue
            destination = snapshot_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            ensure_shared_path_to_root(destination.parent, snapshot_anchor)
            shutil.copy2(source_path, destination)
            ensure_shared_permissions(destination)
            files_copied += 1

        return {
            "snapshot_path": str(snapshot_dir),
            "snapshot_root": str(snapshot_anchor),
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
        snapshot_root: Path | None = None,
    ) -> dict[str, Any]:
        project_root = self.get_project_root(project).resolve()
        return self.canonical_mutations.run_locked(
            project,
            project_root=project_root,
            operation="restore_workspace_snapshot",
            owner=f"snapshot:{snapshot_key}",
            fn=lambda: self.restore_workspace_snapshot_unlocked(
                project,
                target_dir,
                snapshot_key=snapshot_key,
                preserve_project_root_rules=preserve_project_root_rules,
                project_root=project_root,
                snapshot_root=snapshot_root,
            ),
        )

    def restore_workspace_snapshot_unlocked(
        self,
        project: Project,
        target_dir: Path,
        *,
        snapshot_key: str,
        preserve_project_root_rules: bool = False,
        project_root: Path | None = None,
        snapshot_root: Path | None = None,
    ) -> dict[str, Any]:
        target_dir = target_dir.resolve()
        project_root = project_root or self.get_project_root(project).resolve()
        snapshot_anchor = (snapshot_root or project_root).resolve()
        snapshot_dir = (snapshot_anchor / AUTO_SNAPSHOT_ROOT / snapshot_key).resolve()

        if not snapshot_dir.exists():
            return {
                "restored": False,
                "reason": "snapshot_missing",
                "snapshot_path": str(snapshot_dir),
                "snapshot_root": str(snapshot_anchor),
                "target_path": str(target_dir),
                "files_restored": 0,
            }

        target_dir.mkdir(parents=True, exist_ok=True)
        ensure_shared_path_to_root(target_dir, snapshot_anchor)
        snapshot_files = [
            path
            for path in snapshot_dir.rglob("*")
            if path.is_file()
            and not is_hydration_excluded_path(path.relative_to(snapshot_dir))
        ]
        current_workspace_files = [
            path
            for path in target_dir.rglob("*")
            if path.is_file()
            and not is_hydration_excluded_path(path.relative_to(target_dir))
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
            self.reserved_project_names(project)
            if preserve_project_root_rules
            else set()
        )

        for child in list(target_dir.iterdir()):
            if preserve_project_root_rules and child.name in reserved_names:
                continue
            if child.name in HYDRATION_EXCLUDED_NAMES:
                continue
            if preserve_project_root_rules and child.name == AUTO_SNAPSHOT_DIR_NAME:
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
            ensure_shared_path_to_root(destination.parent, snapshot_anchor)
            shutil.copy2(snapshot_path, destination)
            ensure_shared_permissions(destination)
            files_restored += 1

        return {
            "restored": True,
            "snapshot_path": str(snapshot_dir),
            "snapshot_root": str(snapshot_anchor),
            "target_path": str(target_dir),
            "files_restored": files_restored,
        }
