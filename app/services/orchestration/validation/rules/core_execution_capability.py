"""Accepted-step execution capability core invariant (Phase 34-A).

One invariant: ``ACCEPTED_STEP_EXECUTION_CAPABILITY_COMPLETE``.  Every accepted
mutating step must have at least one execution channel that can actually
perform its mutation:

* **E1** -- a structured file operation the Orchestrator applies under APA;
* **E2** -- a shell command the local command policy can execute itself;
* **E5** -- an explicitly resolved ``AGENT_RUNTIME`` topology whose runtime owns
  native workspace side effects.

A residual text-only reasoning turn (**E4**) is not an execution channel.  Under
``STRUCTURED_ORCHESTRATOR`` a step that demands mutation through a shell command
E2 will refuse therefore has no channel at all, and the plan must be repaired
before the execution loop can dispatch it to a runtime that cannot mutate
anything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.agents.agent_backends import (
    EXECUTION_TOPOLOGY_REQUIRED_CAPABILITIES,
    ExecutionTopology,
)
from app.services.orchestration.operations.file_ops_contract import CONTENT_FILE_OPS
from app.services.orchestration.validation.local_command_policy import (
    local_shell_command_is_executable,
)

# The mutating shell forms plan admission recognizes.  Deliberately identical to
# the read-only-stage detector in ``core_file_ops`` so one plan cannot be
# "mutating" for one rule and "non-mutating" for the other.
MUTATING_COMMAND_PATTERNS = (
    re.compile(r"(^|[;&|]\s*)(mkdir|touch|cp|mv|rm)\b"),
    re.compile(r"\bsed\s+-i\b"),
    re.compile(r">\s*[^&\s]"),
    re.compile(r"\btee\s+"),
)


def command_requires_mutation(command: Any) -> bool:
    """True when a planned shell command is a workspace-mutating form."""

    command_text = str(command or "").strip()
    if not command_text:
        return False
    patterns = MUTATING_COMMAND_PATTERNS
    if command_text.startswith(("python -c ", "python3 -c ")):
        # Inline python verification scripts are read-only by contract; only the
        # unambiguous mutation forms apply.
        patterns = (
            MUTATING_COMMAND_PATTERNS[0],
            MUTATING_COMMAND_PATTERNS[1],
            MUTATING_COMMAND_PATTERNS[3],
        )
    return any(pattern.search(command_text) for pattern in patterns)


def step_unexecutable_mutating_commands(
    step: Dict[str, Any], project_dir: Path
) -> List[str]:
    """Mutating commands in one step that no Orchestrator-owned channel can run."""

    findings: List[str] = []
    for command in step.get("commands", []) or []:
        command_text = str(command or "").strip()
        if not command_text or not command_requires_mutation(command_text):
            continue
        if local_shell_command_is_executable(command_text, project_dir):
            continue
        findings.append(command_text)
    return findings


def plan_steps_without_execution_channel(
    plan: List[Dict[str, Any]],
    *,
    project_dir: Optional[Path],
    execution_topology: ExecutionTopology,
) -> Dict[int, List[str]]:
    """Return accepted-step mutations with no capable execution channel.

    Keyed by step number, valued by the offending commands.  ``AGENT_RUNTIME``
    deployments return nothing: the runtime owns native workspace side effects
    (E5), so a shell form the local policy refuses is still executable there.
    """

    if execution_topology is not ExecutionTopology.STRUCTURED_ORCHESTRATOR:
        return {}
    if project_dir is None:
        return {}
    resolved_dir = Path(project_dir)
    findings: Dict[int, List[str]] = {}
    for index, step in enumerate(plan, start=1):
        if not isinstance(step, dict):
            continue
        try:
            step_number = int(step.get("step_number", index))
        except (TypeError, ValueError):
            step_number = index
        offending = step_unexecutable_mutating_commands(step, resolved_dir)
        if offending:
            findings[step_number] = offending
    return findings


def plan_steps_with_unsupported_commands(
    plan: List[Dict[str, Any]],
    *,
    project_dir: Optional[Path],
    execution_topology: ExecutionTopology,
) -> Dict[int, List[str]]:
    """Return step commands no execution channel of this topology can run.

    Phase 40C-2.  Under ``STRUCTURED_ORCHESTRATOR`` the runtime has no command
    tools, so a step command is only executable when one of the Orchestrator's
    local executors admits it.  Otherwise the execution loop would hand the
    command to a text-only runtime whose reply is self-report, not evidence.
    Steps ``plan_steps_without_execution_channel`` already reports are skipped here.
    """

    if execution_topology is not ExecutionTopology.STRUCTURED_ORCHESTRATOR:
        return {}
    if project_dir is None:
        return {}
    # Lazy: the execution phase module imports ``ValidatorService``.
    from app.services.orchestration.phases.execution_local_steps import (
        local_step_commands_are_executable,
    )

    resolved_dir = Path(project_dir)
    findings: Dict[int, List[str]] = {}
    for index, step in enumerate(plan, start=1):
        if not isinstance(step, dict):
            continue
        commands = [
            str(command or "").strip()
            for command in step.get("commands", []) or []
            if str(command or "").strip()
        ]
        if not commands:
            continue
        if step_unexecutable_mutating_commands(step, resolved_dir):
            continue
        if local_step_commands_are_executable(
            project_dir=resolved_dir,
            commands=commands,
            verification_command=step.get("verification"),
        ):
            continue
        try:
            step_number = int(step.get("step_number", index))
        except (TypeError, ValueError):
            step_number = index
        findings[step_number] = commands
    return findings


def describe_unsupported_step_commands(
    step_number: int, commands: List[str], *, project_dir: Path
) -> str:
    """Return a bounded repair reason for a step ``plan_steps_with_unsupported_commands`` reported.

    Diagnostic only: admission is decided by ``local_step_commands_are_executable``.
    The per-command classification only chooses which truthful reason to give.
    """

    from app.services.orchestration.phases.execution_local_steps import (
        _is_read_only_inspection_command,
        _is_simple_verification_command,
        local_step_commands_are_executable,
    )

    prefix = "unsupported_step_command: "
    inspections = [c for c in commands if _is_read_only_inspection_command(c)]
    verifications = [
        c
        for c in commands
        if c not in inspections
        and _is_simple_verification_command(c, project_dir=project_dir)
    ]
    if len(inspections) + len(verifications) == len(commands):
        if inspections and verifications:
            return (
                f"{prefix}step {step_number} mixes inspection and verification "
                "commands; use an inspection-only step and a separate step with "
                "exactly one verification command."
            )
        if len(verifications) > 1:
            return (
                f"{prefix}step {step_number} has {len(verifications)} verification "
                "commands; use exactly one per step (one pytest call may list "
                "several node IDs)."
            )
    rejected = next(
        (
            command
            for command in commands
            if not local_step_commands_are_executable(
                project_dir=project_dir, commands=[command], verification_command=None
            )
        ),
        None,
    )
    if rejected is None:
        return (
            f"{prefix}step {step_number} commands fit no single admitted channel "
            "(inspection only, one verification, or safe shell only); split them."
        )
    # Repair retains the rejected command and capability limit under its
    # 180-character per-reason bound. The complete command remains in the
    # malformed Plan shown to the repair.
    reason_prefix = f"{prefix}rejected "
    reason_suffix = (
        "; structured runtime has no command tools. "
        "Plan ops for edits; keep goal, scope, checks."
    )
    rejected_command = repr(" ".join(rejected.split()))
    command_limit = 180 - len(reason_prefix) - len(reason_suffix)
    if len(rejected_command) > command_limit:
        rejected_command = (
            rejected_command[: max(0, command_limit - 3)].rstrip() + "..."
        )
    return f"{reason_prefix}{rejected_command}{reason_suffix}"


def plan_steps_with_unsupported_verification(
    plan: List[Dict[str, Any]],
    *,
    project_dir: Optional[Path],
    execution_topology: ExecutionTopology,
) -> Dict[int, str]:
    """Return standalone verification fields Structured Runtime cannot execute."""

    if execution_topology is not ExecutionTopology.STRUCTURED_ORCHESTRATOR:
        return {}
    if project_dir is None:
        return {}

    # Verification is a separate execution channel from ``commands``. Reuse
    # the command-level verification predicate without requiring a matching
    # ``commands`` entry, since ops-only steps may declare verification alone.
    from app.services.orchestration.phases.execution_local_steps import (
        local_verification_command_is_executable,
    )

    resolved_dir = Path(project_dir)
    findings: Dict[int, str] = {}
    for index, step in enumerate(plan, start=1):
        if not isinstance(step, dict):
            continue
        verification = step.get("verification")
        command = str(verification or "").strip()
        if not command or local_verification_command_is_executable(
            project_dir=resolved_dir,
            verification_command=command,
        ):
            continue
        try:
            step_number = int(step.get("step_number", index))
        except (TypeError, ValueError):
            step_number = index
        findings[step_number] = command
    return findings


def local_executor_command_examples(project_dir: Path) -> tuple[list[str], list[str]]:
    """Return representative inspection and verification forms admitted locally."""

    from app.services.orchestration.phases.execution_local_steps import (
        _is_read_only_inspection_command,
        _is_simple_verification_command,
        local_step_commands_are_executable,
    )

    inspection_candidates = (
        "rg --files .",
        "ls",
        "pwd",
        "cat README.md",
        "rg TODO ./README.md",
        "grep TODO ./README.md",
    )
    verification_candidates = (
        "pytest -q tests",
        "python -m pytest -q tests",
        "python -m py_compile module.py",
        "python -m unittest tests.test_example",
        "npm run build",
    )
    inspections = [
        command
        for command in inspection_candidates
        if _is_read_only_inspection_command(command)
        and local_step_commands_are_executable(
            project_dir=Path(project_dir),
            commands=[command],
            verification_command=None,
        )
    ]
    verifications = [
        command
        for command in verification_candidates
        if _is_simple_verification_command(command, project_dir=Path(project_dir))
        and local_step_commands_are_executable(
            project_dir=Path(project_dir),
            commands=[command],
            verification_command=command,
        )
    ]
    return inspections, verifications


def render_planning_execution_capability_contract(
    *, runtime_service: Any, project_dir: Path
) -> str:
    """Render a compact Planning contract from the resolved executor policy.

    Command examples are admitted through the same predicates used at Plan
    admission and execution. They are representative forms, not a second
    allowlist and do not grant additional execution authority.
    """

    from app.services.agents.agent_runtime import execution_topology_for_runtime

    topology = execution_topology_for_runtime(runtime_service)
    descriptor = getattr(runtime_service, "backend_descriptor", None)
    capabilities = getattr(descriptor, "capabilities", None)
    if topology is ExecutionTopology.AGENT_RUNTIME:
        declared = [
            name
            for name in EXECUTION_TOPOLOGY_REQUIRED_CAPABILITIES[
                ExecutionTopology.AGENT_RUNTIME
            ]
            if bool(getattr(capabilities, name, False))
        ]
        return (
            "## EXECUTION CAPABILITY CONTRACT\n"
            "Resolved topology: agent runtime. Native execution is limited to "
            f"the configured runtime's declared capabilities ({', '.join(declared)}). "
            "Use only tools that runtime actually exposes for its bound workspace; "
            "this prompt grants no additional tools or command permissions. "
            "Source changes remain subject to Plan operation, APA, grounding, and "
            "GR2 validation. Keep genuine verification requirements in the Plan."
        )

    inspections, verifications = local_executor_command_examples(Path(project_dir))
    missing = (
        capabilities.missing_execution_capabilities(ExecutionTopology.AGENT_RUNTIME)
        if capabilities is not None
        else ["backend descriptor unavailable"]
    )
    source_ops = ", ".join(sorted(CONTENT_FILE_OPS | {"replace_in_file"}))
    inspect_forms = "; ".join(inspections[:2]) or "none admitted"
    verify_forms = "; ".join(verifications[:3]) or "none admitted"
    return (
        "## EXECUTION CAPABILITY CONTRACT\n"
        f"Resolved topology: structured orchestrator; this runtime has no native command tools ({', '.join(missing)}). "
        "Step shape: `commands` carries command work, `ops` carries deterministic file operations, and `verification` is a separate check. "
        "A `verification` value alone does not make a step runnable; a Structured step needs an admitted command or at least one operation. "
        "When `commands` carries verification work, use exactly one admitted verification command in that step. "
        "A step's full `commands` list must fit one admitted channel: admitted inspection commands may share a step, "
        "a verification command must be the only command in its step, and safe local shell commands keep their own admission rules. "
        "Do not mix inspection and verification commands in one step; put them in separate steps. "
        "Put multiple pytest node IDs in one supported pytest invocation, or use separate steps for distinct invocations. "
        f"Plan ops from {{{source_ops}}} are source edits, subject to APA, grounding, and GR2. "
        f"Local inspection forms admitted by this executor: {inspect_forms}. "
        f"Verification forms admitted by this executor: {verify_forms}. "
        "These examples still require command/path admission. Arbitrary shell and "
        "project-importing inline Python are not available. Inline Python `-c` is "
        "limited to the constrained read-only file checks admitted by this executor. "
        "Preserve the objective, "
        "source scope, and verification checks. Do not drop work or relabel an "
        "edit as verification."
    )
