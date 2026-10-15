"""Deterministic candidate repair attempted before LLM completion repair.

Phase 34 DCR1: one proven repair class only.  ``candidate_black_failed`` is a
deterministic, semantics-preserving finding — the formatter that produced the
failure is the same one that resolves it — so escalating it to a model is both
unnecessary and, as Task 230 proved, unreliable.

This is a pre-escalation stage, not a replacement.  Findings that are not on the
allowlist, and findings that survive the formatter, continue through the
existing completion-repair path unchanged.  Nothing here decides completion:
the caller must re-run the normal candidate validation afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import shlex
from pathlib import Path
from typing import Any, Optional, Sequence

from app.services.orchestration.execution.python_resolution import (
    ProjectPythonResolutionError,
)
from app.services.orchestration.validation.candidate_checks import (
    _candidate_python,
    _run_command,
    discover_candidate_static_policy,
)
from app.services.orchestration.validation.path_authority import (
    PathDeclarationError,
    declare,
)
from app.services.orchestration.validation.workspace_guard import (
    TaskWorkspaceViolationError,
    normalize_path_reference,
)

# The only finding class DCR1 repairs deterministically.  Membership is by typed
# rule_id — never by matching finding text.
DETERMINISTIC_REPAIR_RULE_IDS = frozenset({"candidate_black_failed"})

# Control surfaces a formatter may never be pointed at, even if a finding named
# one.  Product paths only.
_FORBIDDEN_PATH_ROOTS = (".agent", ".openclaw", ".git")

DETERMINISTIC_REPAIR_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class DeterministicRepairOutcome:
    """Result of one deterministic pre-escalation repair attempt."""

    status: str  # "skipped" | "applied" | "failed"
    reason: str
    rule_ids: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    command: str = ""
    returncode: Optional[int] = None
    output: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return self.status == "applied"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "rule_ids": list(self.rule_ids),
            "paths": list(self.paths),
            "command": self.command,
            "returncode": self.returncode,
            "output": self.output[:2000],
            **dict(self.details),
        }


def _authorized_mutation_scope(accepted_path_authority: Any) -> Optional[set[str]]:
    """Mutation-class grants from the APA, or None when it cannot be projected."""

    # Deferred import: validator imports this module's caller, not the reverse.
    from app.services.orchestration.validation.validator import _apa_mutation_scope

    if accepted_path_authority is None:
        return None
    try:
        return set(_apa_mutation_scope(accepted_path_authority))
    except Exception:
        return None


def _path_is_product_safe(path_text: str, project_dir: Path) -> bool:
    """Reject anything that is not a plain, in-workspace, Product ``.py`` file."""

    try:
        canonical = str(declare(path_text))
    except PathDeclarationError:
        return False
    if not canonical.endswith(".py"):
        return False
    if Path(canonical).parts and Path(canonical).parts[0] in _FORBIDDEN_PATH_ROOTS:
        return False
    try:
        normalized = normalize_path_reference(canonical, project_dir)
    except TaskWorkspaceViolationError:
        return False
    target = project_dir / normalized
    # is_file() follows symlinks; is_symlink() is checked first so a link out of
    # the workspace cannot be formatted through.
    return not target.is_symlink() and target.is_file()


def eligible_black_repair_paths(
    *,
    findings: Sequence[Any],
    project_dir: Path,
    accepted_path_authority: Any,
) -> tuple[tuple[str, ...], str]:
    """Exact paths a deterministic black repair may touch, and why not if empty.

    Paths come from the finding's own typed evidence — the same list the check
    ran against — and must already sit inside the accepted mutation authority.
    No new path authority is minted here.
    """

    authorized = _authorized_mutation_scope(accepted_path_authority)
    if authorized is None:
        return (), "accepted_path_authority_unavailable"

    declared: list[str] = []
    for finding in findings:
        if getattr(finding, "rule_id", None) not in DETERMINISTIC_REPAIR_RULE_IDS:
            continue
        evidence = getattr(finding, "evidence", None) or {}
        for raw_path in evidence.get("paths") or []:
            path_text = str(raw_path or "").strip()
            if path_text and path_text not in declared:
                declared.append(path_text)

    if not declared:
        return (), "no_repair_paths_in_finding_evidence"

    for path_text in declared:
        if not _path_is_product_safe(path_text, project_dir):
            return (), "repair_path_not_product_safe"
        if path_text not in authorized:
            return (), "repair_path_outside_accepted_authority"

    return tuple(sorted(declared)), ""


def attempt_deterministic_candidate_repair(
    *,
    completion_validation: Any,
    project_dir: Path,
    accepted_path_authority: Any,
    timeout_seconds: int = DETERMINISTIC_REPAIR_TIMEOUT_SECONDS,
) -> DeterministicRepairOutcome:
    """Run the allowlisted deterministic repair once, if it is eligible.

    Returns ``applied`` only when the formatter itself exited zero.  The caller
    must still re-run candidate validation: this function never decides that
    completion succeeded.
    """

    repairable = list(getattr(completion_validation, "repairable_findings", []) or [])
    eligible = [
        finding
        for finding in repairable
        if getattr(finding, "rule_id", None) in DETERMINISTIC_REPAIR_RULE_IDS
    ]
    if not eligible:
        return DeterministicRepairOutcome(
            status="skipped", reason="no_deterministic_repairable_finding"
        )

    rule_ids = tuple(dict.fromkeys(finding.rule_id for finding in eligible))
    paths, ineligible_reason = eligible_black_repair_paths(
        findings=eligible,
        project_dir=project_dir,
        accepted_path_authority=accepted_path_authority,
    )
    if not paths:
        return DeterministicRepairOutcome(
            status="skipped", reason=ineligible_reason, rule_ids=rule_ids
        )

    if not discover_candidate_static_policy(project_dir).black_admitted:
        return DeterministicRepairOutcome(
            status="skipped", reason="black_gate_not_admitted", rule_ids=rule_ids
        )

    try:
        python = _candidate_python(project_dir)
    except ProjectPythonResolutionError as exc:
        return DeterministicRepairOutcome(
            status="failed",
            reason="deterministic_repair_command_unavailable",
            rule_ids=rule_ids,
            paths=paths,
            output=str(exc),
        )
    command = shlex.join([python, "-m", "black", "--", *paths])
    returncode, output = _run_command(
        project_dir=project_dir,
        command=command,
        timeout_seconds=timeout_seconds,
    )
    if returncode != 0:
        # Includes a missing formatter and a timeout (returncode None).  The
        # finding stays unresolved and the existing repair path still owns it.
        return DeterministicRepairOutcome(
            status="failed",
            reason=(
                "deterministic_repair_command_unavailable"
                if returncode is None or "No module named" in output
                else "deterministic_repair_command_failed"
            ),
            rule_ids=rule_ids,
            paths=paths,
            command=command,
            returncode=returncode,
            output=output,
        )

    return DeterministicRepairOutcome(
        status="applied",
        reason="deterministic_black_repair_applied",
        rule_ids=rule_ids,
        paths=paths,
        command=command,
        returncode=returncode,
        output=output,
    )
