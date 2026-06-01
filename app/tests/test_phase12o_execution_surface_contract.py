"""Phase 12O: Execution Surface Contract.

Adapter tests proving that file-op, shell-command, portable-posix, and
verification-command execution surfaces can all be represented through the
shared ExecutionSurfaceContract, and that surface differences in mutation
policy, workspace scope, shell mode, and review requirements are classifiable.

No production behavior changes are introduced here.
"""

from __future__ import annotations

from app.services.orchestration.execution_surface_contract import (
    ExecutionMismatchType,
    ExecutionShellMode,
    ExecutionSurface,
    ExecutionSurfaceContract,
    WorkspaceScope,
    build_file_op_contract,
    build_portable_posix_contract,
    build_shell_command_contract,
    build_verification_command_contract,
    compare_execution_surface_contracts,
    count_execution_surface_mismatches,
)
from app.services.orchestration.operations.file_ops_contract import SUPPORTED_FILE_OPS


# ---------------------------------------------------------------------------
# Contract shape tests — one per surface
# ---------------------------------------------------------------------------


def test_file_op_contract_write_file():
    contract = build_file_op_contract("write_file", source="structured_file_op")

    assert contract.surface == ExecutionSurface.FILE_OP
    assert contract.operation == "write_file"
    assert contract.mutation_allowed is True
    assert contract.workspace_scope == WorkspaceScope.PROJECT_DIR_ONLY
    assert contract.shell_mode == ExecutionShellMode.NOT_APPLICABLE
    assert contract.requires_review is False
    assert set(contract.allowed_ops) == SUPPORTED_FILE_OPS
    assert "path" in contract.expected_output_fields
    assert "content" in contract.expected_output_fields
    assert contract.normalized is True
    assert contract.divergence_reason is None


def test_file_op_contract_replace_in_file():
    contract = build_file_op_contract("replace_in_file")

    assert contract.operation == "replace_in_file"
    assert "old" in contract.expected_output_fields
    assert "new" in contract.expected_output_fields
    assert contract.mutation_allowed is True


def test_file_op_contract_mkdir():
    contract = build_file_op_contract("mkdir")
    assert contract.operation == "mkdir"
    assert "path" in contract.expected_output_fields


def test_shell_command_contract():
    contract = build_shell_command_contract("npm run test", source="shell_command")

    assert contract.surface == ExecutionSurface.SHELL_COMMAND
    assert contract.operation == "npm run test"
    assert contract.mutation_allowed is True
    assert contract.workspace_scope == WorkspaceScope.PROJECT_DIR_ONLY
    assert contract.shell_mode == ExecutionShellMode.SHELL_TRUE
    assert contract.requires_review is False
    assert "success" in contract.expected_output_fields
    assert contract.normalized is True


def test_portable_posix_contract():
    contract = build_portable_posix_contract("test -f output.txt")

    assert contract.surface == ExecutionSurface.PORTABLE_POSIX
    assert contract.mutation_allowed is False
    assert contract.workspace_scope == WorkspaceScope.PROJECT_DIR_ONLY
    assert contract.shell_mode == ExecutionShellMode.NOT_APPLICABLE
    assert contract.requires_review is False
    assert "success" in contract.expected_output_fields


def test_verification_command_contract():
    contract = build_verification_command_contract("pytest", source="verification")

    assert contract.surface == ExecutionSurface.VERIFICATION_COMMAND
    assert contract.operation == "pytest"
    assert contract.mutation_allowed is False
    assert contract.workspace_scope == WorkspaceScope.PROJECT_DIR_ONLY
    assert contract.shell_mode == ExecutionShellMode.SHELL_TRUE
    assert "success" in contract.expected_output_fields
    assert contract.normalized is True


def test_contract_to_dict_has_all_fields():
    contract = build_file_op_contract("write_file")
    d = contract.to_dict()
    required_keys = {
        "surface",
        "operation",
        "allowed_ops",
        "mutation_allowed",
        "workspace_scope",
        "shell_mode",
        "requires_review",
        "expected_output_fields",
        "source",
        "normalized",
        "divergence_reason",
    }
    assert required_keys.issubset(d.keys())


# ---------------------------------------------------------------------------
# Mutation policy mismatch
# ---------------------------------------------------------------------------


def test_mutation_mismatch_between_file_op_and_verification():
    file_op = build_file_op_contract("write_file")
    verification = build_verification_command_contract("pytest")

    assert file_op.mutation_allowed is True
    assert verification.mutation_allowed is False

    mismatches = compare_execution_surface_contracts([file_op, verification])
    mismatch_types = {m["type"] for m in mismatches}
    assert ExecutionMismatchType.MUTATION_POLICY_MISMATCH in mismatch_types


def test_mutation_mismatch_between_shell_and_portable_posix():
    shell = build_shell_command_contract("echo hello")
    posix = build_portable_posix_contract("echo hello")

    assert shell.mutation_allowed is True
    assert posix.mutation_allowed is False

    mismatches = compare_execution_surface_contracts([shell, posix])
    mismatch_types = {m["type"] for m in mismatches}
    assert ExecutionMismatchType.MUTATION_POLICY_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# Shell mode mismatch
# ---------------------------------------------------------------------------


def test_shell_mode_mismatch_between_file_op_and_shell_command():
    file_op = build_file_op_contract("write_file")
    shell = build_shell_command_contract("echo hello")

    assert file_op.shell_mode == ExecutionShellMode.NOT_APPLICABLE
    assert shell.shell_mode == ExecutionShellMode.SHELL_TRUE

    mismatches = compare_execution_surface_contracts([file_op, shell])
    mismatch_types = {m["type"] for m in mismatches}
    assert ExecutionMismatchType.SHELL_MODE_MISMATCH in mismatch_types


def test_shell_mode_mismatch_between_portable_posix_and_verification():
    posix = build_portable_posix_contract("cat output.txt")
    verification = build_verification_command_contract("pytest")

    assert posix.shell_mode == ExecutionShellMode.NOT_APPLICABLE
    assert verification.shell_mode == ExecutionShellMode.SHELL_TRUE

    mismatches = compare_execution_surface_contracts([posix, verification])
    mismatch_types = {m["type"] for m in mismatches}
    assert ExecutionMismatchType.SHELL_MODE_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# Allowed ops mismatch
# ---------------------------------------------------------------------------


def test_allowed_ops_mismatch_between_file_op_and_shell():
    file_op = build_file_op_contract("write_file")
    shell = build_shell_command_contract("npm test")

    assert set(file_op.allowed_ops) == SUPPORTED_FILE_OPS
    assert shell.allowed_ops == []

    mismatches = compare_execution_surface_contracts([file_op, shell])
    mismatch_types = {m["type"] for m in mismatches}
    assert ExecutionMismatchType.ALLOWED_OPS_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# Workspace scope — all surfaces share project_dir_only
# ---------------------------------------------------------------------------


def test_all_surfaces_share_project_dir_only_scope():
    file_op = build_file_op_contract("write_file")
    shell = build_shell_command_contract("npm test")
    posix = build_portable_posix_contract("cat output.txt")
    verification = build_verification_command_contract("pytest")

    for contract in [file_op, shell, posix, verification]:
        assert contract.workspace_scope == WorkspaceScope.PROJECT_DIR_ONLY

    mismatches = compare_execution_surface_contracts(
        [file_op, shell, posix, verification]
    )
    mismatch_types = {m["type"] for m in mismatches}
    assert ExecutionMismatchType.WORKSPACE_SCOPE_MISMATCH not in mismatch_types


# ---------------------------------------------------------------------------
# Review policy mismatch
# ---------------------------------------------------------------------------


def test_review_policy_mismatch_when_requires_review_differs():
    file_op_reviewed = build_file_op_contract("write_file", requires_review=True)
    file_op_plain = build_file_op_contract("write_file", requires_review=False)

    mismatches = compare_execution_surface_contracts([file_op_reviewed, file_op_plain])
    mismatch_types = {m["type"] for m in mismatches}
    assert ExecutionMismatchType.REVIEW_POLICY_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# Intentional divergence
# ---------------------------------------------------------------------------


def test_intentional_divergence_suppresses_mismatch():
    file_op = build_file_op_contract("write_file")
    shell = build_shell_command_contract(
        "npm test",
        divergence_reason="INTENTIONAL_SCOPE_DIFFERENCE",
    )

    mismatches = compare_execution_surface_contracts([file_op, shell])
    assert mismatches == []


# ---------------------------------------------------------------------------
# All four surfaces representable through shared contract
# ---------------------------------------------------------------------------


def test_all_four_surfaces_representable():
    contracts = [
        build_file_op_contract("write_file"),
        build_shell_command_contract("npm test"),
        build_portable_posix_contract("cat output.txt"),
        build_verification_command_contract("pytest"),
    ]

    for c in contracts:
        assert isinstance(c, ExecutionSurfaceContract)
        assert c.normalized is True
        assert c.workspace_scope == WorkspaceScope.PROJECT_DIR_ONLY

    assert set(c.surface for c in contracts) == {
        ExecutionSurface.FILE_OP,
        ExecutionSurface.SHELL_COMMAND,
        ExecutionSurface.PORTABLE_POSIX,
        ExecutionSurface.VERIFICATION_COMMAND,
    }


# ---------------------------------------------------------------------------
# Mismatch count metric
# ---------------------------------------------------------------------------


def test_count_mismatches_returns_structured_summary():
    file_op = build_file_op_contract("write_file")
    shell = build_shell_command_contract("npm test")

    summary = count_execution_surface_mismatches([file_op, shell])

    assert "total_mismatch_count" in summary
    assert "mismatch_types" in summary
    assert "surfaces_compared" in summary
    assert "intentionally_diverged_surfaces" in summary
    assert summary["total_mismatch_count"] > 0


def test_count_records_intentionally_diverged():
    file_op = build_file_op_contract("write_file")
    shell = build_shell_command_contract(
        "npm test",
        divergence_reason="INTENTIONAL_SCOPE_DIFFERENCE",
    )

    summary = count_execution_surface_mismatches([file_op, shell])
    assert ExecutionSurface.SHELL_COMMAND in summary["intentionally_diverged_surfaces"]
    assert summary["total_mismatch_count"] == 0
