"""Phase 12N: Verification Surface Contract Boundary.

Adapter tests proving that step, completion, repair, and scorer verification
surfaces can all be represented through the shared VerificationSurfaceContract.

Phase 12M characterized mismatch types deterministically.  Phase 12N proves
that those mismatches are now representable in a shared normalized schema, and
that intentional divergence can be recorded explicitly rather than hidden.

No production behavior changes are introduced here.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from app.services.orchestration.verification_surface_contract import (
    DivergenceReason,
    MismatchPolicy,
    VerificationMismatchType,
    VerificationShellMode,
    VerificationSurface,
    VerificationSurfaceContract,
    build_completion_verification_contract,
    build_repair_verification_contract,
    build_scorer_verification_contract,
    build_step_verification_contract,
    compare_verification_surface_contracts,
    count_verification_surface_mismatches,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _python_command(script: str, executable: str = "python") -> str:
    return shlex.join([executable, "-c", script])


# ---------------------------------------------------------------------------
# Contract shape tests — one per surface
# ---------------------------------------------------------------------------


def test_step_contract_normalizes_correctly(tmp_path):
    command = _python_command("print('ok')")
    contract = build_step_verification_contract(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=120.0,
        expected_artifacts=["output.txt"],
        source="step_verification",
    )

    assert contract.surface == VerificationSurface.STEP_VERIFICATION
    assert contract.normalized is True
    assert contract.divergence_reason is None
    assert contract.shell_mode == VerificationShellMode.SHELL_TRUE
    assert contract.cwd == str(tmp_path.resolve())
    assert contract.timeout_seconds == 120.0
    assert contract.expected_artifacts == ["output.txt"]
    assert contract.required_terminal_events == []
    assert contract.mismatch_policy == MismatchPolicy.DIAGNOSTIC_ONLY


def test_completion_contract_normalizes_correctly(tmp_path):
    command = _python_command("print('ok')")
    contract = build_completion_verification_contract(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=180.0,
        source="completion_verification",
    )

    assert contract.surface == VerificationSurface.COMPLETION_VERIFICATION
    assert contract.normalized is True
    assert contract.divergence_reason is None
    assert contract.shell_mode == VerificationShellMode.SHELL_FALSE
    assert contract.cwd == str(tmp_path.resolve())
    assert contract.timeout_seconds == 180.0


def test_repair_contract_normalizes_correctly(tmp_path):
    command = _python_command("print('ok')")
    contract = build_repair_verification_contract(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=180.0,
        source="repair_verification",
    )

    assert contract.surface == VerificationSurface.REPAIR_VERIFICATION
    assert contract.normalized is True
    assert contract.shell_mode == VerificationShellMode.SHELL_FALSE
    assert contract.cwd == str(tmp_path.resolve())


def test_scorer_contract_normalizes_correctly(tmp_path):
    command = _python_command("print('ok')")
    contract = build_scorer_verification_contract(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=60.0,
        expected_artifacts=["report.json"],
        required_terminal_events=["task_completed"],
        source="scorer_verification",
    )

    assert contract.surface == VerificationSurface.SCORER_VERIFICATION
    assert contract.normalized is True
    assert contract.divergence_reason is None
    assert contract.shell_mode == VerificationShellMode.SHELL_TRUE
    assert contract.cwd == str(tmp_path.resolve())
    assert contract.timeout_seconds == 60.0
    assert contract.expected_artifacts == ["report.json"]
    assert contract.required_terminal_events == ["task_completed"]


def test_contract_to_dict_envelope(tmp_path):
    contract = build_completion_verification_contract(
        project_dir=tmp_path,
        command="pytest",
        source="completion_verification",
    )
    d = contract.to_dict()
    assert d["surface"] == "completion_verification"
    assert d["normalized"] is True
    assert d["divergence_reason"] is None
    assert "command" in d
    assert "cwd" in d
    assert "env" in d
    assert "pythonpath" in d
    assert "shell_mode" in d
    assert "timeout_seconds" in d
    assert "expected_artifacts" in d
    assert "required_terminal_events" in d


# ---------------------------------------------------------------------------
# Shell mode mismatch — characterizes the 12M SHELL_MODE_MISMATCH finding
# ---------------------------------------------------------------------------


def test_shell_mode_mismatch_detected_between_step_and_completion(tmp_path):
    command = (
        _python_command("from pathlib import Path; Path('out.txt').write_text('ok')")
        + " && "
        + _python_command("assert Path('out.txt').exists()")
    )

    step = build_step_verification_contract(
        project_dir=tmp_path, command=command, source="step_verification"
    )
    completion = build_completion_verification_contract(
        project_dir=tmp_path, command=command, source="completion_verification"
    )

    assert step.shell_mode == VerificationShellMode.SHELL_TRUE
    assert completion.shell_mode == VerificationShellMode.SHELL_FALSE

    mismatches = compare_verification_surface_contracts([step, completion])
    mismatch_types = {m["type"] for m in mismatches}
    assert VerificationMismatchType.SHELL_MODE_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# Command mismatch — characterizes the 12M COMMAND_MISMATCH finding
# ---------------------------------------------------------------------------


def test_command_mismatch_detected_between_step_and_scorer(tmp_path, monkeypatch):
    command = _python_command("print('ok')")

    step = build_step_verification_contract(
        project_dir=tmp_path, command=command, source="step_verification"
    )
    scorer = build_scorer_verification_contract(
        project_dir=tmp_path, command=command, source="scorer_verification"
    )

    # Step rewrites leading "python" to sys.executable; scorer receives the
    # original command as-is (its resolver is applied at run-time, not here).
    # The contract records what each surface *would* use.
    assert step.command != command  # rewritten to sys.executable
    assert scorer.command == command  # not rewritten by the adapter

    mismatches = compare_verification_surface_contracts([step, scorer])
    mismatch_types = {m["type"] for m in mismatches}
    assert VerificationMismatchType.COMMAND_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# PYTHONPATH / ENV mismatch — characterizes the 12M ENV/PYTHONPATH finding
# ---------------------------------------------------------------------------


def test_pythonpath_mismatch_detected_between_step_and_scorer(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    package_dir = tmp_path / "src" / "mypackage"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 12\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
        encoding="utf-8",
    )
    command = _python_command(
        "import mypackage; assert mypackage.VALUE == 12", executable="python3"
    )

    step = build_step_verification_contract(
        project_dir=tmp_path, command=command, source="step_verification"
    )
    scorer = build_scorer_verification_contract(
        project_dir=tmp_path, command=command, source="scorer_verification"
    )

    # Step materializes src/ into PYTHONPATH for inline python; scorer does not.
    assert any(
        "src" in p for p in step.pythonpath
    ), "step contract should include src/ in PYTHONPATH for src-layout packages"
    assert not any(
        "src" in p for p in scorer.pythonpath
    ), "scorer contract should not inject src/ PYTHONPATH"

    mismatches = compare_verification_surface_contracts([step, scorer])
    mismatch_types = {m["type"] for m in mismatches}
    assert VerificationMismatchType.PYTHONPATH_MISMATCH in mismatch_types
    assert VerificationMismatchType.ENV_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# Timeout mismatch — characterizes the 12M TIMEOUT_MISMATCH finding
# ---------------------------------------------------------------------------


def test_timeout_mismatch_detected_across_surfaces(tmp_path):
    command = _python_command("import time; time.sleep(0.5)", executable="python3")

    step = build_step_verification_contract(
        project_dir=tmp_path, command=command, timeout_seconds=0.05
    )
    completion = build_completion_verification_contract(
        project_dir=tmp_path, command=command, timeout_seconds=3.0
    )

    assert step.timeout_seconds == 0.05
    assert completion.timeout_seconds == 3.0

    mismatches = compare_verification_surface_contracts([step, completion])
    mismatch_types = {m["type"] for m in mismatches}
    assert VerificationMismatchType.TIMEOUT_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# Artifact expectation mismatch — characterizes the 12M ARTIFACT finding
# ---------------------------------------------------------------------------


def test_artifact_mismatch_detected_between_surfaces(tmp_path):
    command = _python_command("print('ok')")

    step = build_step_verification_contract(
        project_dir=tmp_path,
        command=command,
        expected_artifacts=[],
    )
    scorer = build_scorer_verification_contract(
        project_dir=tmp_path,
        command=command,
        expected_artifacts=["deliverable.md"],
    )

    mismatches = compare_verification_surface_contracts([step, scorer])
    mismatch_types = {m["type"] for m in mismatches}
    assert VerificationMismatchType.ARTIFACT_EXPECTATION_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# Terminal event mismatch — characterizes the 12M TERMINAL_EVENT finding
# ---------------------------------------------------------------------------


def test_terminal_event_mismatch_detected_between_surfaces(tmp_path):
    command = _python_command("print('ok')")

    step = build_step_verification_contract(
        project_dir=tmp_path,
        command=command,
    )
    scorer = build_scorer_verification_contract(
        project_dir=tmp_path,
        command=command,
        required_terminal_events=["task_completed"],
    )

    mismatches = compare_verification_surface_contracts([step, scorer])
    mismatch_types = {m["type"] for m in mismatches}
    assert VerificationMismatchType.TERMINAL_EVENT_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# CWD alignment — characterizes the 12M CWD non-mismatch finding
# ---------------------------------------------------------------------------


def test_cwd_surfaces_align_on_project_dir(tmp_path):
    command = _python_command("print('ok')")

    step = build_step_verification_contract(project_dir=tmp_path, command=command)
    completion = build_completion_verification_contract(
        project_dir=tmp_path, command=command
    )
    scorer = build_scorer_verification_contract(project_dir=tmp_path, command=command)

    assert step.cwd == str(tmp_path.resolve())
    assert completion.cwd == str(tmp_path.resolve())
    assert scorer.cwd == str(tmp_path.resolve())

    mismatches = compare_verification_surface_contracts([step, completion, scorer])
    mismatch_types = {m["type"] for m in mismatches}
    assert VerificationMismatchType.CWD_MISMATCH not in mismatch_types


# ---------------------------------------------------------------------------
# Intentional divergence suppresses mismatch reporting
# ---------------------------------------------------------------------------


def test_intentional_divergence_suppresses_mismatch(tmp_path):
    command = _python_command("print('ok')")

    step = build_step_verification_contract(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=0.05,
    )
    completion = build_completion_verification_contract(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=3.0,
        divergence_reason=DivergenceReason.INTENTIONAL_SCOPE_DIFFERENCE,
    )

    # Completion surface has a recorded divergence reason — excluded from comparison.
    mismatches = compare_verification_surface_contracts([step, completion])
    assert (
        mismatches == []
    ), "intentionally diverged surface should not produce mismatch records"


def test_intentional_divergence_preserved_in_to_dict(tmp_path):
    contract = build_completion_verification_contract(
        project_dir=tmp_path,
        command="pytest",
        divergence_reason=DivergenceReason.INTENTIONAL_SCOPE_DIFFERENCE,
    )
    d = contract.to_dict()
    assert d["divergence_reason"] == "INTENTIONAL_SCOPE_DIFFERENCE"
    assert d["normalized"] is True


# ---------------------------------------------------------------------------
# All four surfaces through the shared contract — no behavior change
# ---------------------------------------------------------------------------


def test_all_four_surfaces_representable_through_shared_contract(tmp_path):
    command = _python_command("print('ok')")

    step = build_step_verification_contract(project_dir=tmp_path, command=command)
    completion = build_completion_verification_contract(
        project_dir=tmp_path, command=command
    )
    repair = build_repair_verification_contract(project_dir=tmp_path, command=command)
    scorer = build_scorer_verification_contract(project_dir=tmp_path, command=command)

    contracts = [step, completion, repair, scorer]
    for c in contracts:
        assert isinstance(c, VerificationSurfaceContract)
        assert c.normalized is True
        assert c.cwd == str(tmp_path.resolve())

    summary = count_verification_surface_mismatches(contracts)
    assert "total_mismatch_count" in summary
    assert "mismatch_types" in summary
    assert "surfaces_compared" in summary
    assert set(summary["surfaces_compared"]) == {
        VerificationSurface.STEP_VERIFICATION,
        VerificationSurface.COMPLETION_VERIFICATION,
        VerificationSurface.REPAIR_VERIFICATION,
        VerificationSurface.SCORER_VERIFICATION,
    }


# ---------------------------------------------------------------------------
# Mismatch count metric / report field
# ---------------------------------------------------------------------------


def test_count_verification_surface_mismatches_returns_structured_summary(tmp_path):
    command = _python_command("print('ok')")

    step = build_step_verification_contract(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=10.0,
        expected_artifacts=[],
    )
    completion = build_completion_verification_contract(
        project_dir=tmp_path,
        command=command,
        timeout_seconds=180.0,
        expected_artifacts=["report.md"],
    )

    summary = count_verification_surface_mismatches([step, completion])

    assert summary["total_mismatch_count"] > 0
    assert VerificationMismatchType.TIMEOUT_MISMATCH in summary["mismatch_types"]
    assert (
        VerificationMismatchType.ARTIFACT_EXPECTATION_MISMATCH
        in summary["mismatch_types"]
    )
    assert "intentionally_diverged_surfaces" in summary
    assert summary["intentionally_diverged_surfaces"] == []
    assert isinstance(summary["mismatches"], list)


def test_count_mismatches_records_intentionally_diverged_surfaces(tmp_path):
    command = _python_command("print('ok')")

    step = build_step_verification_contract(project_dir=tmp_path, command=command)
    scorer = build_scorer_verification_contract(
        project_dir=tmp_path,
        command=command,
        divergence_reason=DivergenceReason.INTENTIONAL_SCOPE_DIFFERENCE,
    )

    summary = count_verification_surface_mismatches([step, scorer])
    assert (
        VerificationSurface.SCORER_VERIFICATION
        in summary["intentionally_diverged_surfaces"]
    )
    assert summary["total_mismatch_count"] == 0


# ---------------------------------------------------------------------------
# Repair and completion share the same execution path
# ---------------------------------------------------------------------------


def test_repair_and_completion_share_same_shell_mode(tmp_path):
    command = _python_command("print('ok')")

    completion = build_completion_verification_contract(
        project_dir=tmp_path, command=command
    )
    repair = build_repair_verification_contract(project_dir=tmp_path, command=command)

    assert (
        completion.shell_mode == repair.shell_mode == VerificationShellMode.SHELL_FALSE
    )


def test_repair_and_step_shell_mode_differ(tmp_path):
    command = _python_command("print('ok')")

    step = build_step_verification_contract(project_dir=tmp_path, command=command)
    repair = build_repair_verification_contract(project_dir=tmp_path, command=command)

    assert step.shell_mode != repair.shell_mode

    mismatches = compare_verification_surface_contracts([step, repair])
    mismatch_types = {m["type"] for m in mismatches}
    assert VerificationMismatchType.SHELL_MODE_MISMATCH in mismatch_types
