"""Project Isolation Service

Provides path validation and safety checks for project isolation.
Ensures all operations stay within project boundaries.
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from app.models import Project, LogEntry
from app.services.system_settings import get_effective_workspace_root

logger = logging.getLogger(__name__)


def _slugify_workspace_name(name: str) -> str:
    value = (name or "").strip().lower()
    value = re.sub(r"[\s_]+", "-", value)
    value = re.sub(r"[^a-z0-9-]", "", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "project"


def normalize_project_workspace_path(
    workspace_path: Optional[str], project_name: Optional[str] = None
) -> str:
    """Store workspace paths as project-root-relative values under OPENCLAW_WORKSPACE_ROOT."""
    if not workspace_path:
        return _slugify_workspace_name(project_name or "")

    raw = workspace_path.strip()
    if not raw:
        return _slugify_workspace_name(project_name or "")

    workspace_root = get_effective_workspace_root()
    root_str = str(workspace_root)
    if raw.startswith(root_str):
        raw = raw[len(root_str) :].lstrip("/")
    elif raw.startswith("/"):
        raw = Path(raw).name

    raw = raw.replace("\\", "/").strip("/")
    if raw.startswith("projects/"):
        raw = raw[len("projects/") :]

    return raw or _slugify_workspace_name(project_name or "")


def resolve_project_workspace_path(
    workspace_path: Optional[str], project_name: Optional[str] = None
) -> Path:
    relative = normalize_project_workspace_path(workspace_path, project_name)
    return (get_effective_workspace_root() / relative).resolve()


class ProjectIsolationError(Exception):
    """Custom exception for project isolation violations"""

    pass


class ProjectIsolationService:
    """Service for enforcing project isolation boundaries"""

    def __init__(self, db: Session):
        self.db = db

    def get_project_root(self, project_id: int) -> Path:
        """
        Get the workspace root path for a project

        Args:
            project_id: Project ID

        Returns:
            Path object for project workspace

        Raises:
            ProjectIsolationError: If project not found
        """
        project = self.db.query(Project).filter(Project.id == project_id).first()

        if not project:
            raise ProjectIsolationError(f"Project {project_id} not found")

        return resolve_project_workspace_path(project.workspace_path, project.name)

    def validate_path(
        self, project_id: int, requested_path: str, allow_relative: bool = True
    ) -> Dict[str, Any]:
        """
        Validate that a path is within project boundaries

        Args:
            project_id: Project ID
            requested_path: Path to validate (can be relative or absolute)
            allow_relative: If True, resolve relative to project root

        Returns:
            Validation result with:
            - valid: bool
            - resolved_path: Path object
            - is_within_bounds: bool
            - message: Status message

        Raises:
            ProjectIsolationError: If path is outside project bounds
        """
        try:
            project_root = self.get_project_root(project_id)

            # Resolve the path
            if Path(requested_path).is_absolute():
                resolved_path = Path(requested_path).resolve()
            else:
                if allow_relative:
                    resolved_path = (project_root / requested_path).resolve()
                else:
                    resolved_path = Path(requested_path).resolve()

            # Check if within bounds
            is_within_bounds = resolved_path.is_relative_to(project_root)

            result = {
                "valid": is_within_bounds,
                "requested_path": requested_path,
                "resolved_path": str(resolved_path),
                "project_root": str(project_root),
                "is_within_bounds": is_within_bounds,
            }

            if not is_within_bounds:
                result["message"] = (
                    f"Path '{requested_path}' is outside project boundaries. "
                    f"Project root: {project_root}"
                )
                logger.warning(
                    f"Project isolation violation: project={project_id}, "
                    f"path={requested_path}, resolved={resolved_path}"
                )
                raise ProjectIsolationError(result["message"])

            result["message"] = f"Path is within project bounds: {project_root}"
            return result

        except ProjectIsolationError:
            raise
        except Exception as e:
            error_msg = f"Path validation failed: {str(e)}"
            logger.error(error_msg)
            raise ProjectIsolationError(error_msg)

    def validate_operation(
        self,
        project_id: int,
        operation_type: str,
        target_path: str,
        action: str = "execute",
    ) -> Dict[str, Any]:
        """
        Validate an operation before execution

        Args:
            project_id: Project ID
            operation_type: Type of operation (file_read, file_write, shell_command, etc.)
            target_path: Target path for the operation
            action: Action to take on violation ("warn" or "block")

        Returns:
            Validation result with warning if outside bounds

        Raises:
            ProjectIsolationError: If action="block" and path is outside bounds
        """
        try:
            validation = self.validate_path(project_id, target_path)

            result = {
                "operation_type": operation_type,
                "target_path": target_path,
                "validation": validation,
                "allowed": validation["is_within_bounds"],
            }

            if not validation["is_within_bounds"]:
                if action == "block":
                    raise ProjectIsolationError(
                        f"Operation '{operation_type}' on '{target_path}' blocked: "
                        f"outside project boundaries"
                    )
                else:
                    # Soft isolation: warn but allow
                    result["warning"] = (
                        f"⚠️  Operation '{operation_type}' on '{target_path}' "
                        f"is outside project '{validation['project_root']}' "
                        f"boundaries. This may affect other projects."
                    )
                    logger.warning(result["warning"])

            return result

        except ProjectIsolationError:
            raise
        except Exception as e:
            error_msg = f"Operation validation failed: {str(e)}"
            logger.error(error_msg)
            raise ProjectIsolationError(error_msg)

    def get_safety_prompt(self, project_id: int) -> str:
        """
        Generate a safety prompt to inject into LLM tasks

        Args:
            project_id: Project ID

        Returns:
            Safety prompt string
        """
        try:
            project_root = self.get_project_root(project_id)

            prompt = f"""
### 🛡️ PROJECT ISOLATION SAFETY NOTICE

**Current Project:** Project ID {project_id}
**Workspace Root:** {project_root}

**RULES:**
1. ✅ You MAY read/write files within: {project_root}
2. ❌ You MUST NOT access files outside this directory
3. ❌ You MUST NOT modify system files or other projects
4. ⚠️  If you need to access external resources, ask the user for permission

**PATH VALIDATION:**
- All file operations will be checked against this boundary
- Attempts to escape will be logged and may be blocked
- Use relative paths when possible (e.g., "src/main.py" not "/root/...")
- ❌ DO NOT attempt to write to paths like "../../../etc/passwd" or "shell://command"
- ❌ DO NOT generate code that tries to escape the project boundary

**VERIFICATION TESTS:**
If you need to verify isolation is working, use the API endpoint:
- GET /api/v1/projects/{{project_id}}/isolation/validate
- POST /api/v1/projects/{{project_id}}/isolation/validate with {{"path": "test.txt"}}

**SAFETY FIRST:**
If you're unsure whether a path is safe, ASK before executing.
Do NOT attempt to break the isolation - it's already enforced.
"""
            return prompt.strip()

        except Exception as e:
            logger.error(f"Failed to generate safety prompt: {str(e)}")
            return "Project isolation safety prompt generation failed"

    def log_isolation_attempt(
        self,
        project_id: int,
        session_id: int,
        session_instance_id: str,
        path: str,
        operation: str,
        result: str,
        is_violation: bool = False,
    ) -> LogEntry:
        """
        Log an isolation check attempt with instance tracking

        Args:
            project_id: Project ID
            session_id: Session ID
            session_instance_id: Instance UUID (new parameter for isolation)
            path: Path that was validated
            operation: Operation type
            result: Result message
            is_violation: Whether this was a violation

        Returns:
            Created LogEntry
        """
        level = "WARNING" if is_violation else "INFO"

        log_entry = LogEntry(
            session_id=session_id,
            session_instance_id=session_instance_id,  # ✅ Critical for isolation
            level=level,
            message=f"Project isolation check: {operation} on '{path}' -> {result}",
            log_metadata=json.dumps(
                {
                    "project_id": project_id,
                    "operation": operation,
                    "path": path,
                    "is_violation": is_violation,
                    "session_instance_id": session_instance_id,
                }
            ),
        )
        self.db.add(log_entry)
        self.db.commit()

        return log_entry

    def sanitize_path(self, path: str) -> str:
        """
        Sanitize a path to prevent directory traversal attacks

        Args:
            path: Path to sanitize

        Returns:
            Sanitized path
        """
        # Remove any null bytes
        path = path.replace("\x00", "")

        # Normalize path separators
        path = path.replace("\\", "/")

        # Remove multiple slashes
        while "//" in path:
            path = path.replace("//", "/")

        return path

    def safe_test_path(self, project_id: int, test_path: str) -> Dict[str, Any]:
        """
        Safely test path validation without executing code

        This is a helper for AI agents to verify isolation is working
        without generating potentially malformed code.

        Args:
            project_id: Project ID
            test_path: Path to test (will be validated, not executed)

        Returns:
            Test result with validation status
        """
        try:
            project_root = self.get_project_root(project_id)

            # Resolve the path for display
            if Path(test_path).is_absolute():
                resolved = Path(test_path).resolve()
            else:
                resolved = (project_root / test_path).resolve()

            is_within_bounds = resolved.is_relative_to(project_root)

            return {
                "test_path": test_path,
                "resolved_path": str(resolved),
                "project_root": str(project_root),
                "is_within_bounds": is_within_bounds,
                "would_be_blocked": not is_within_bounds,
                "safe_to_use": is_within_bounds,
            }

        except ProjectIsolationError as e:
            return {
                "test_path": test_path,
                "error": str(e),
                "would_be_blocked": True,
                "safe_to_use": False,
            }
        except Exception as e:
            return {
                "test_path": test_path,
                "error": f"Test failed: {str(e)}",
                "would_be_blocked": False,
                "safe_to_use": False,
            }
