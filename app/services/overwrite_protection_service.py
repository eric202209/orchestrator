"""Overwrite Protection Service

Prevents accidental overwriting of existing work when executing tasks.
Provides warnings and optional blocking for duplicate task execution.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Dict, List, Any
from sqlalchemy.orm import Session
from app.models import Project
from app.services.project_isolation_service import (
    ProjectIsolationService,
    ProjectIsolationError,
)

logger = logging.getLogger(__name__)


class OverwriteProtectionError(Exception):
    """Custom exception for overwrite protection violations"""

    pass


class OverwriteProtectionService:
    """Service for preventing accidental file overwrites"""

    def __init__(self, db: Session):
        self.db = db
        self.isolation_service = ProjectIsolationService(db)

    def check_workspace_exists(
        self, project_id: int, task_subfolder: str
    ) -> Dict[str, Any]:
        """
        Check if a workspace path already exists and contains files

        Args:
            project_id: Project ID
            task_subfolder: Task subfolder name (e.g., "task_123")

        Returns:
            Dictionary with existence check results:
            - exists: bool
            - file_count: int
            - recent_files: List[str]
            - last_modified: str or None
        """
        try:
            project_root = self.isolation_service.get_project_root(project_id)
            workspace_path = (project_root / task_subfolder).resolve()

            if not workspace_path.exists():
                return {
                    "exists": False,
                    "path": str(workspace_path),
                    "file_count": 0,
                    "recent_files": [],
                    "last_modified": None,
                    "would_overwrite": False,
                }

            # Count files and get recent ones
            file_count = 0
            recent_files = []
            last_modified = None
            max_recent_files = 10

            for root, dirs, files in os.walk(workspace_path):
                for filename in files:
                    file_count += 1
                    if len(recent_files) < max_recent_files:
                        filepath = Path(root) / filename
                        stat = filepath.stat()
                        recent_files.append(
                            {
                                "path": str(filepath.relative_to(project_root)),
                                "size": stat.st_size,
                                "modified": datetime.fromtimestamp(
                                    stat.st_mtime
                                ).isoformat(),
                            }
                        )
                        if not last_modified or stat.st_mtime > (
                            last_modified.timestamp()
                            if isinstance(last_modified, datetime)
                            else 0
                        ):
                            last_modified = datetime.fromtimestamp(stat.st_mtime)

            return {
                "exists": True,
                "path": str(workspace_path),
                "file_count": file_count,
                "recent_files": recent_files,
                "last_modified": last_modified.isoformat() if last_modified else None,
                "would_overwrite": file_count > 0,
            }

        except Exception as e:
            logger.error(f"Workspace check failed: {str(e)}")
            return {
                "exists": False,
                "error": str(e),
                "would_overwrite": False,
            }

    def scan_for_conflicts(
        self, project_id: int, planned_files: List[str]
    ) -> Dict[str, Any]:
        """
        Scan for potential file conflicts between planned files and existing files

        Args:
            project_id: Project ID
            planned_files: List of files the task plans to create/modify

        Returns:
            Dictionary with conflict analysis:
            - has_conflicts: bool
            - conflicting_files: List[str]
            - safe_to_proceed: bool
        """
        try:
            project_root = self.isolation_service.get_project_root(project_id)
            conflicts = []

            for planned_file in planned_files:
                # Resolve the file path
                if Path(planned_file).is_absolute():
                    resolved_path = Path(planned_file).resolve()
                else:
                    resolved_path = (project_root / planned_file).resolve()

                # Check if file exists
                if resolved_path.exists() and resolved_path.is_file():
                    conflicts.append(
                        {
                            "file": str(resolved_path.relative_to(project_root)),
                            "type": "existing_file",
                            "would_modify": True,
                        }
                    )

            return {
                "has_conflicts": len(conflicts) > 0,
                "conflicting_files": [c["file"] for c in conflicts],
                "conflict_details": conflicts,
                "safe_to_proceed": len(conflicts) == 0,
            }

        except Exception as e:
            logger.error(f"Conflict scan failed: {str(e)}")
            return {
                "has_conflicts": False,
                "error": str(e),
                "safe_to_proceed": True,
            }

    def generate_overwrite_warning(
        self, workspace_info: Dict[str, Any], conflict_info: Dict[str, Any]
    ) -> str:
        """
        Generate a human-readable warning message for potential overwrites

        Args:
            workspace_info: Result from check_workspace_exists()
            conflict_info: Result from scan_for_conflicts()

        Returns:
            Warning message string
        """
        lines = []

        if not workspace_info.get("exists", False):
            return "No existing workspace found. Safe to proceed."

        # Workspace exists warning
        lines.append(
            f"⚠️  **EXISTING WORKSPACE DETECTED**\n\n"
            f"**Location:** {workspace_info['path']}\n"
            f"**Files already present:** {workspace_info['file_count']}\n"
            f"**Last modified:** {workspace_info.get('last_modified', 'Unknown')}"
        )

        # Recent files
        if workspace_info.get("recent_files"):
            lines.append("\n**Recent files in workspace:**")
            for file_info in workspace_info["recent_files"][:5]:
                lines.append(
                    f"- `{file_info['path']}` ({file_info['size']:,} bytes, "
                    f"modified {file_info['modified']})"
                )

        # Conflict details
        if conflict_info.get("has_conflicts"):
            lines.append("\n\n⚠️  **POTENTIAL FILE CONFLICTS:**")
            for file in conflict_info["conflicting_files"]:
                lines.append(
                    f"- `{file}` - This file already exists and may be modified!"
                )

            lines.append(
                "\n**Recommendation:** Review existing files before proceeding. "
                "Consider if this task should run in a different workspace."
            )

        return "\n".join(lines)

    def check_and_warn(
        self,
        project_id: int,
        task_subfolder: str,
        planned_files: Optional[List[str]] = None,
        action: str = "warn",  # "warn" or "block"
    ) -> Dict[str, Any]:
        """
        Check for potential overwrites and warn/block accordingly

        Args:
            project_id: Project ID
            task_subfolder: Task subfolder name
            planned_files: List of files to check (optional)
            action: Action on conflict ("warn" or "block")

        Returns:
            Result dictionary with:
            - safe_to_proceed: bool
            - warning_message: str (if any)
            - workspace_info: Dict from check_workspace_exists()
            - conflict_info: Dict from scan_for_conflicts()

        Raises:
            OverwriteProtectionError: If action="block" and conflicts detected
        """
        # Check if workspace exists
        workspace_info = self.check_workspace_exists(project_id, task_subfolder)

        # Scan for file conflicts if planned files provided
        conflict_info = {"has_conflicts": False}
        if planned_files:
            conflict_info = self.scan_for_conflicts(project_id, planned_files)

        # Generate warning message
        warning_message = self.generate_overwrite_warning(workspace_info, conflict_info)

        result = {
            "safe_to_proceed": not workspace_info.get("would_overwrite", False),
            "workspace_exists": workspace_info.get("exists", False),
            "file_count": workspace_info.get("file_count", 0),
            "warning_message": (
                warning_message if workspace_info.get("would_overwrite") else None
            ),
            "has_conflicts": conflict_info.get("has_conflicts", False),
            "workspace_info": workspace_info,
            "conflict_info": conflict_info,
        }

        # Block if requested and conflicts exist
        if action == "block" and (
            workspace_info.get("would_overwrite") or conflict_info.get("has_conflicts")
        ):
            raise OverwriteProtectionError(
                f"Overwrite protection triggered. Existing files detected at {workspace_info['path']}. "
                f"{warning_message}"
            )

        return result

    def create_backup_of_existing(
        self, project_id: int, task_subfolder: str, backup_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a backup of existing workspace before proceeding

        Args:
            project_id: Project ID
            task_subfolder: Task subfolder to backup
            backup_name: Custom backup name (optional)

        Returns:
            Backup result with:
            - success: bool
            - backup_path: str
            - files_backed_up: int
        """
        try:
            import shutil
            from datetime import datetime

            project_root = self.isolation_service.get_project_root(project_id)
            workspace_path = (project_root / task_subfolder).resolve()

            if not workspace_path.exists():
                return {
                    "success": False,
                    "error": "Workspace does not exist",
                }

            # Create backup directory
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            backup_name = backup_name or f"backup_{timestamp}"
            backup_path = (
                project_root / f"{task_subfolder}_backup_{backup_name}"
            ).resolve()

            # Copy workspace to backup location
            shutil.copytree(workspace_path, backup_path)

            # Count files backed up
            file_count = sum(len(files) for _, _, files in os.walk(backup_path))

            logger.info(f"Created backup of {task_subfolder} at {backup_path}")

            return {
                "success": True,
                "backup_path": str(backup_path),
                "files_backed_up": file_count,
                "timestamp": timestamp,
            }

        except Exception as e:
            logger.error(f"Backup creation failed: {str(e)}")
            return {
                "success": False,
                "error": str(e),
            }


# Import here to avoid circular dependency
import os
from datetime import datetime
