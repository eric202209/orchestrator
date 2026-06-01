"""Declarative execution surface contract boundary.

Phase 12O: Normalized representation that file-op, shell-command,
portable-posix, and verification execution surfaces can all be compared
through.

This is NOT a new runtime.  It is a shared schema so that execution-surface
differences in allowed operations, mutation semantics, workspace scope, and
review requirements can be represented and classified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.services.orchestration.operations.file_ops_contract import (
    SUPPORTED_FILE_OPS,
    FILE_OP_FIELD_SETS,
)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ExecutionSurface(StrEnum):
    FILE_OP = "file_op"
    SHELL_COMMAND = "shell_command"
    PORTABLE_POSIX = "portable_posix"
    VERIFICATION_COMMAND = "verification_command"


class WorkspaceScope(StrEnum):
    PROJECT_DIR_ONLY = "project_dir_only"
    ANY = "any"


class ExecutionShellMode(StrEnum):
    SHELL_TRUE = "shell=True"
    SHELL_FALSE = "shell=False"
    NOT_APPLICABLE = "not_applicable"


class ExecutionMismatchType(StrEnum):
    MUTATION_POLICY_MISMATCH = "MUTATION_POLICY_MISMATCH"
    WORKSPACE_SCOPE_MISMATCH = "WORKSPACE_SCOPE_MISMATCH"
    SHELL_MODE_MISMATCH = "SHELL_MODE_MISMATCH"
    REVIEW_POLICY_MISMATCH = "REVIEW_POLICY_MISMATCH"
    ALLOWED_OPS_MISMATCH = "ALLOWED_OPS_MISMATCH"


# ---------------------------------------------------------------------------
# Contract dataclass
# ---------------------------------------------------------------------------

# Portable POSIX supports a safe subset — cat, test -f, echo, && chaining.
_PORTABLE_POSIX_OPS = frozenset({"cat", "test", "echo"})

# Verification command output fields
_VERIFICATION_OUTPUT_FIELDS = ["success", "returncode", "output"]

# File op output fields per op (what the structured result contains)
_FILE_OP_OUTPUT_FIELDS: dict[str, list[str]] = {
    "write_file": ["op", "path", "content"],
    "append_file": ["op", "path", "content"],
    "replace_in_file": ["op", "path", "old", "new"],
    "mkdir": ["op", "path"],
    "delete_file": ["op", "path"],
}


@dataclass(frozen=True)
class ExecutionSurfaceContract:
    """Normalized representation of one execution surface's semantics.

    All execution surfaces (file ops, shell commands, portable posix,
    verification commands) can be represented and compared through this shape.
    """

    surface: str
    operation: str
    allowed_ops: list[str]
    mutation_allowed: bool
    workspace_scope: str
    shell_mode: str
    requires_review: bool
    expected_output_fields: list[str]
    source: str
    normalized: bool = True
    divergence_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "operation": self.operation,
            "allowed_ops": list(self.allowed_ops),
            "mutation_allowed": self.mutation_allowed,
            "workspace_scope": self.workspace_scope,
            "shell_mode": self.shell_mode,
            "requires_review": self.requires_review,
            "expected_output_fields": list(self.expected_output_fields),
            "source": self.source,
            "normalized": self.normalized,
            "divergence_reason": self.divergence_reason,
        }


# ---------------------------------------------------------------------------
# Surface adapters
# ---------------------------------------------------------------------------


def build_file_op_contract(
    op_name: str,
    *,
    requires_review: bool = False,
    source: str = "structured_file_op",
    divergence_reason: str | None = None,
) -> ExecutionSurfaceContract:
    """Build a normalized contract for a structured file operation."""
    raw_op = str(op_name or "").strip()
    expected_fields = list(FILE_OP_FIELD_SETS.get(raw_op, {"op", "path"}))
    return ExecutionSurfaceContract(
        surface=ExecutionSurface.FILE_OP,
        operation=raw_op,
        allowed_ops=sorted(SUPPORTED_FILE_OPS),
        mutation_allowed=True,
        workspace_scope=WorkspaceScope.PROJECT_DIR_ONLY,
        shell_mode=ExecutionShellMode.NOT_APPLICABLE,
        requires_review=requires_review,
        expected_output_fields=expected_fields,
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
    )


def build_shell_command_contract(
    command: str,
    *,
    requires_review: bool = False,
    source: str = "shell_command",
    divergence_reason: str | None = None,
) -> ExecutionSurfaceContract:
    """Build a normalized contract for shell command execution (shell=True)."""
    return ExecutionSurfaceContract(
        surface=ExecutionSurface.SHELL_COMMAND,
        operation=str(command or "").strip(),
        allowed_ops=[],
        mutation_allowed=True,
        workspace_scope=WorkspaceScope.PROJECT_DIR_ONLY,
        shell_mode=ExecutionShellMode.SHELL_TRUE,
        requires_review=requires_review,
        expected_output_fields=_VERIFICATION_OUTPUT_FIELDS,
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
    )


def build_portable_posix_contract(
    command: str,
    *,
    source: str = "portable_posix",
    divergence_reason: str | None = None,
) -> ExecutionSurfaceContract:
    """Build a normalized contract for portable POSIX execution.

    Portable POSIX handles a safe subset of commands (cat, test -f, echo, &&)
    without delegating to a full shell.  Mutation is not permitted.
    """
    return ExecutionSurfaceContract(
        surface=ExecutionSurface.PORTABLE_POSIX,
        operation=str(command or "").strip(),
        allowed_ops=sorted(_PORTABLE_POSIX_OPS),
        mutation_allowed=False,
        workspace_scope=WorkspaceScope.PROJECT_DIR_ONLY,
        shell_mode=ExecutionShellMode.NOT_APPLICABLE,
        requires_review=False,
        expected_output_fields=_VERIFICATION_OUTPUT_FIELDS,
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
    )


def build_verification_command_contract(
    command: str,
    *,
    requires_review: bool = False,
    source: str = "verification_command",
    divergence_reason: str | None = None,
) -> ExecutionSurfaceContract:
    """Build a normalized contract for a verification command subprocess.

    Verification commands run with shell=True but are scoped to the project dir
    and do not mutate workspace state.
    """
    return ExecutionSurfaceContract(
        surface=ExecutionSurface.VERIFICATION_COMMAND,
        operation=str(command or "").strip(),
        allowed_ops=[],
        mutation_allowed=False,
        workspace_scope=WorkspaceScope.PROJECT_DIR_ONLY,
        shell_mode=ExecutionShellMode.SHELL_TRUE,
        requires_review=requires_review,
        expected_output_fields=_VERIFICATION_OUTPUT_FIELDS,
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
    )


# ---------------------------------------------------------------------------
# Contract comparison
# ---------------------------------------------------------------------------


def compare_execution_surface_contracts(
    contracts: list[ExecutionSurfaceContract],
) -> list[dict[str, Any]]:
    """Compare execution surface contracts and return classified mismatches.

    Contracts with a non-None divergence_reason are excluded from comparison.
    """
    if len(contracts) < 2:
        return []

    normalized = [c for c in contracts if c.divergence_reason is None]
    if len(normalized) < 2:
        return []

    reference = normalized[0]
    mismatches: list[dict[str, Any]] = []

    for other in normalized[1:]:

        def _record(mtype: ExecutionMismatchType, ref_val: Any, other_val: Any) -> None:
            mismatches.append(
                {
                    "type": str(mtype),
                    "reference_surface": reference.surface,
                    "other_surface": other.surface,
                    "reference_value": ref_val,
                    "other_value": other_val,
                }
            )

        if reference.mutation_allowed != other.mutation_allowed:
            _record(
                ExecutionMismatchType.MUTATION_POLICY_MISMATCH,
                reference.mutation_allowed,
                other.mutation_allowed,
            )

        if reference.workspace_scope != other.workspace_scope:
            _record(
                ExecutionMismatchType.WORKSPACE_SCOPE_MISMATCH,
                reference.workspace_scope,
                other.workspace_scope,
            )

        if reference.shell_mode != other.shell_mode:
            _record(
                ExecutionMismatchType.SHELL_MODE_MISMATCH,
                reference.shell_mode,
                other.shell_mode,
            )

        if reference.requires_review != other.requires_review:
            _record(
                ExecutionMismatchType.REVIEW_POLICY_MISMATCH,
                reference.requires_review,
                other.requires_review,
            )

        if set(reference.allowed_ops) != set(other.allowed_ops):
            _record(
                ExecutionMismatchType.ALLOWED_OPS_MISMATCH,
                sorted(reference.allowed_ops),
                sorted(other.allowed_ops),
            )

    return mismatches


def count_execution_surface_mismatches(
    contracts: list[ExecutionSurfaceContract],
) -> dict[str, Any]:
    """Return structured mismatch count for metric and report fields."""
    mismatches = compare_execution_surface_contracts(contracts)
    by_type: dict[str, int] = {}
    for m in mismatches:
        mtype = str(m.get("type") or "UNKNOWN")
        by_type[mtype] = by_type.get(mtype, 0) + 1

    diverged = [c.surface for c in contracts if c.divergence_reason is not None]

    return {
        "total_mismatch_count": len(mismatches),
        "mismatch_types": by_type,
        "surfaces_compared": [c.surface for c in contracts],
        "intentionally_diverged_surfaces": diverged,
        "mismatches": mismatches,
    }
