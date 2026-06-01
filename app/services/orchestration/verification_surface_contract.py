"""Declarative verification-surface contract boundary.

Phase 12N: Normalized representation that all verification surfaces can be
compared through before any behavior changes.

This is NOT a new runtime.  It is a shared schema for describing what each
verification surface does so that surface disagreements can be represented,
classified, and reported rather than hidden.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class VerificationSurface(StrEnum):
    STEP_VERIFICATION = "step_verification"
    COMPLETION_VERIFICATION = "completion_verification"
    REPAIR_VERIFICATION = "repair_verification"
    SCORER_VERIFICATION = "scorer_verification"


class VerificationShellMode(StrEnum):
    SHELL_TRUE = "shell=True"
    SHELL_FALSE = "shell=False"
    PORTABLE_POSIX = "portable_posix"


class MismatchPolicy(StrEnum):
    REJECT = "reject"
    WARN = "warn"
    DIAGNOSTIC_ONLY = "diagnostic_only"


class DivergenceReason(StrEnum):
    INTENTIONAL_SCOPE_DIFFERENCE = "INTENTIONAL_SCOPE_DIFFERENCE"


class VerificationMismatchType(StrEnum):
    COMMAND_MISMATCH = "COMMAND_MISMATCH"
    CWD_MISMATCH = "CWD_MISMATCH"
    ENV_MISMATCH = "ENV_MISMATCH"
    PYTHONPATH_MISMATCH = "PYTHONPATH_MISMATCH"
    SHELL_MODE_MISMATCH = "SHELL_MODE_MISMATCH"
    TIMEOUT_MISMATCH = "TIMEOUT_MISMATCH"
    ARTIFACT_EXPECTATION_MISMATCH = "ARTIFACT_EXPECTATION_MISMATCH"
    TERMINAL_EVENT_MISMATCH = "TERMINAL_EVENT_MISMATCH"
    SCORER_ONLY_MISMATCH = "SCORER_ONLY_MISMATCH"


# ---------------------------------------------------------------------------
# Contract dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationSurfaceContract:
    """Normalized representation of one verification surface's parameters.

    All verification surfaces (step, completion, repair, scorer) can be
    represented through this shape.  Fields map directly to the mismatch
    taxonomy established in Phase 12M.
    """

    surface: str
    command: str
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    pythonpath: list[str] = field(default_factory=list)
    shell_mode: str = VerificationShellMode.SHELL_TRUE
    timeout_seconds: float = 120.0
    expected_artifacts: list[str] = field(default_factory=list)
    required_terminal_events: list[str] = field(default_factory=list)
    source: str = ""
    normalized: bool = True
    divergence_reason: str | None = None
    mismatch_policy: str = MismatchPolicy.DIAGNOSTIC_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "normalized": self.normalized,
            "divergence_reason": self.divergence_reason,
            "command": self.command,
            "cwd": self.cwd,
            "env": dict(self.env),
            "pythonpath": list(self.pythonpath),
            "shell_mode": self.shell_mode,
            "timeout_seconds": self.timeout_seconds,
            "expected_artifacts": list(self.expected_artifacts),
            "required_terminal_events": list(self.required_terminal_events),
            "source": self.source,
            "mismatch_policy": self.mismatch_policy,
        }


# ---------------------------------------------------------------------------
# Internal helpers for adapter logic
# ---------------------------------------------------------------------------


def _normalize_pythonpath(raw: str) -> list[str]:
    return [p for p in raw.split(os.pathsep) if p]


def _step_resolved_command(raw_command: str) -> str:
    """Replicate the python-rewrite logic from execute_verification_command."""
    if raw_command in {"python", "python3"}:
        return subprocess.list2cmdline([sys.executable])
    if raw_command.startswith("python "):
        return subprocess.list2cmdline([sys.executable]) + raw_command[len("python") :]
    if raw_command.startswith("python3 "):
        return subprocess.list2cmdline([sys.executable]) + raw_command[len("python3") :]
    return raw_command


def _step_env(project_dir: Path, raw_command: str) -> dict[str, str]:
    """Derive the env dict that execute_verification_command would use."""
    from app.services.orchestration.execution.execution_flow import (
        workspace_python_command_env,
    )

    base_env = os.environ.copy()
    python_dir = str(Path(sys.executable).parent)
    base_env["PATH"] = python_dir + os.pathsep + base_env.get("PATH", "")
    env = workspace_python_command_env(project_dir, raw_command, base_env=base_env)
    return env


def _completion_env(project_dir: Path) -> dict[str, str]:
    """Derive the env dict that _execute_completion_verification would use."""
    env = dict(os.environ)
    raw_pythonpath = env.get("PYTHONPATH", "")
    if raw_pythonpath:
        caller_cwd = Path.cwd()
        absolute_entries = []
        for entry in raw_pythonpath.split(os.pathsep):
            p = Path(entry)
            resolved = (caller_cwd / p).resolve() if not p.is_absolute() else p
            if resolved.exists():
                absolute_entries.append(str(resolved))
        if absolute_entries:
            env["PYTHONPATH"] = os.pathsep.join(absolute_entries)
        else:
            env.pop("PYTHONPATH", None)

    pythonpath_entries = [str(project_dir.resolve())]
    if env.get("PYTHONPATH"):
        pythonpath_entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    return env


# ---------------------------------------------------------------------------
# Surface adapters
# ---------------------------------------------------------------------------


def build_step_verification_contract(
    *,
    project_dir: Path,
    command: str,
    timeout_seconds: float = 120.0,
    expected_artifacts: list[str] | None = None,
    source: str = "step_verification",
    divergence_reason: str | None = None,
    mismatch_policy: str = MismatchPolicy.DIAGNOSTIC_ONLY,
) -> VerificationSurfaceContract:
    """Build a normalized contract for step verification parameters."""
    raw_command = str(command or "").strip()
    resolved_command = _step_resolved_command(raw_command)
    env = _step_env(project_dir, raw_command)
    pythonpath = _normalize_pythonpath(env.get("PYTHONPATH", ""))
    return VerificationSurfaceContract(
        surface=VerificationSurface.STEP_VERIFICATION,
        command=resolved_command,
        cwd=str(project_dir.resolve()),
        env={"PYTHONPATH": env.get("PYTHONPATH", "")},
        pythonpath=pythonpath,
        shell_mode=VerificationShellMode.SHELL_TRUE,
        timeout_seconds=float(timeout_seconds),
        expected_artifacts=list(expected_artifacts or []),
        required_terminal_events=[],
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
        mismatch_policy=mismatch_policy,
    )


def build_completion_verification_contract(
    *,
    project_dir: Path,
    command: str,
    timeout_seconds: float = 180.0,
    expected_artifacts: list[str] | None = None,
    source: str = "completion_verification",
    divergence_reason: str | None = None,
    mismatch_policy: str = MismatchPolicy.DIAGNOSTIC_ONLY,
) -> VerificationSurfaceContract:
    """Build a normalized contract for completion verification parameters."""
    raw_command = str(command or "").strip()
    env = _completion_env(project_dir)
    pythonpath = _normalize_pythonpath(env.get("PYTHONPATH", ""))
    return VerificationSurfaceContract(
        surface=VerificationSurface.COMPLETION_VERIFICATION,
        command=raw_command,
        cwd=str(project_dir.resolve()),
        env={"PYTHONPATH": env.get("PYTHONPATH", "")},
        pythonpath=pythonpath,
        shell_mode=VerificationShellMode.SHELL_FALSE,
        timeout_seconds=float(timeout_seconds),
        expected_artifacts=list(expected_artifacts or []),
        required_terminal_events=[],
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
        mismatch_policy=mismatch_policy,
    )


def build_repair_verification_contract(
    *,
    project_dir: Path,
    command: str,
    timeout_seconds: float = 180.0,
    expected_artifacts: list[str] | None = None,
    source: str = "repair_verification",
    divergence_reason: str | None = None,
    mismatch_policy: str = MismatchPolicy.DIAGNOSTIC_ONLY,
) -> VerificationSurfaceContract:
    """Build a normalized contract for repair verification parameters.

    Repair verification uses the same execution path as completion verification.
    Divergence from step/scorer is expected; record it as INTENTIONAL_SCOPE_DIFFERENCE
    when the caller knows the repair surface is intentionally scoped differently.
    """
    raw_command = str(command or "").strip()
    env = _completion_env(project_dir)
    pythonpath = _normalize_pythonpath(env.get("PYTHONPATH", ""))
    return VerificationSurfaceContract(
        surface=VerificationSurface.REPAIR_VERIFICATION,
        command=raw_command,
        cwd=str(project_dir.resolve()),
        env={"PYTHONPATH": env.get("PYTHONPATH", "")},
        pythonpath=pythonpath,
        shell_mode=VerificationShellMode.SHELL_FALSE,
        timeout_seconds=float(timeout_seconds),
        expected_artifacts=list(expected_artifacts or []),
        required_terminal_events=[],
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
        mismatch_policy=mismatch_policy,
    )


def build_scorer_verification_contract(
    *,
    project_dir: Path,
    command: str,
    timeout_seconds: float = 60.0,
    expected_artifacts: list[str] | None = None,
    required_terminal_events: list[str] | None = None,
    source: str = "scorer_verification",
    divergence_reason: str | None = None,
    mismatch_policy: str = MismatchPolicy.DIAGNOSTIC_ONLY,
) -> VerificationSurfaceContract:
    """Build a normalized contract for scorer verification parameters.

    The scorer uses shell=True and rewrites leading python via its own resolver.
    It does not augment PYTHONPATH.  These are intentional differences from
    completion verification; callers may record INTENTIONAL_SCOPE_DIFFERENCE
    when the scope difference is known and accepted.
    """
    raw_command = str(command or "").strip()
    pythonpath = _normalize_pythonpath(os.environ.get("PYTHONPATH", ""))
    return VerificationSurfaceContract(
        surface=VerificationSurface.SCORER_VERIFICATION,
        command=raw_command,
        cwd=str(project_dir.resolve()),
        env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        pythonpath=pythonpath,
        shell_mode=VerificationShellMode.SHELL_TRUE,
        timeout_seconds=float(timeout_seconds),
        expected_artifacts=list(expected_artifacts or []),
        required_terminal_events=list(required_terminal_events or []),
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
        mismatch_policy=mismatch_policy,
    )


# ---------------------------------------------------------------------------
# Contract comparison
# ---------------------------------------------------------------------------


def _classify_contract_pair(
    reference: VerificationSurfaceContract,
    other: VerificationSurfaceContract,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []

    def _record(
        mismatch_type: VerificationMismatchType, ref_val: Any, other_val: Any
    ) -> None:
        mismatches.append(
            {
                "type": str(mismatch_type),
                "reference_surface": reference.surface,
                "other_surface": other.surface,
                "reference_value": ref_val,
                "other_value": other_val,
            }
        )

    if reference.command != other.command:
        _record(
            VerificationMismatchType.COMMAND_MISMATCH,
            reference.command,
            other.command,
        )

    if reference.cwd != other.cwd:
        _record(
            VerificationMismatchType.CWD_MISMATCH,
            reference.cwd,
            other.cwd,
        )

    ref_pythonpath = reference.pythonpath
    other_pythonpath = other.pythonpath
    if ref_pythonpath != other_pythonpath:
        # ENV_MISMATCH and PYTHONPATH_MISMATCH are both reported for python path differences
        _record(
            VerificationMismatchType.ENV_MISMATCH,
            reference.env,
            other.env,
        )
        _record(
            VerificationMismatchType.PYTHONPATH_MISMATCH,
            ref_pythonpath,
            other_pythonpath,
        )

    if reference.shell_mode != other.shell_mode:
        _record(
            VerificationMismatchType.SHELL_MODE_MISMATCH,
            reference.shell_mode,
            other.shell_mode,
        )

    if reference.timeout_seconds != other.timeout_seconds:
        _record(
            VerificationMismatchType.TIMEOUT_MISMATCH,
            reference.timeout_seconds,
            other.timeout_seconds,
        )

    if set(reference.expected_artifacts) != set(other.expected_artifacts):
        _record(
            VerificationMismatchType.ARTIFACT_EXPECTATION_MISMATCH,
            sorted(reference.expected_artifacts),
            sorted(other.expected_artifacts),
        )

    if set(reference.required_terminal_events) != set(other.required_terminal_events):
        _record(
            VerificationMismatchType.TERMINAL_EVENT_MISMATCH,
            sorted(reference.required_terminal_events),
            sorted(other.required_terminal_events),
        )

    return mismatches


def compare_verification_surface_contracts(
    contracts: list[VerificationSurfaceContract],
) -> list[dict[str, Any]]:
    """Compare a list of surface contracts and return classified mismatch records.

    Contracts with a non-None divergence_reason are excluded from comparison
    — their intentional divergence is already recorded in the contract.

    Returns a list of mismatch records, each with:
    - type: VerificationMismatchType string
    - reference_surface: surface name of the reference contract
    - other_surface: surface name of the compared contract
    - reference_value: value on the reference side
    - other_value: value on the other side
    """
    if len(contracts) < 2:
        return []

    normalized = [c for c in contracts if c.divergence_reason is None]
    if len(normalized) < 2:
        return []

    reference = normalized[0]
    mismatches: list[dict[str, Any]] = []
    for other in normalized[1:]:
        mismatches.extend(_classify_contract_pair(reference, other))
    return mismatches


def count_verification_surface_mismatches(
    contracts: list[VerificationSurfaceContract],
) -> dict[str, Any]:
    """Return a structured mismatch count for metric and report fields.

    This is the high-level summary field the roadmap specifies for the
    existing metrics path.
    """
    mismatches = compare_verification_surface_contracts(contracts)
    by_type: dict[str, int] = {}
    for m in mismatches:
        mtype = str(m.get("type") or "UNKNOWN")
        by_type[mtype] = by_type.get(mtype, 0) + 1

    intentionally_diverged = [
        c.surface for c in contracts if c.divergence_reason is not None
    ]

    return {
        "total_mismatch_count": len(mismatches),
        "mismatch_types": by_type,
        "surfaces_compared": [c.surface for c in contracts],
        "intentionally_diverged_surfaces": intentionally_diverged,
        "mismatches": mismatches,
    }
