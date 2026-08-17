"""Shared prompt contract snippets for planning prompts."""

from __future__ import annotations

from app.services.orchestration.operations.file_ops_contract import (
    render_supported_file_ops,
)

OPERATOR_GUIDANCE_PRECEDENCE_LINE = (
    "Operator Guidance is project-level instruction. If it conflicts with the "
    "task description, follow Operator Guidance unless a validator/safety rule "
    "forbids it."
)


def extract_operator_guidance_block(project_context: str | None) -> str:
    """Return the rendered Operator Guidance block from project context, if any."""

    lines = str(project_context or "").splitlines()
    for idx, line in enumerate(lines):
        if line.strip().lower() != "operator guidance":
            continue
        block = ["Operator Guidance"]
        for candidate in lines[idx + 1 :]:
            stripped = candidate.strip()
            if not stripped:
                break
            if stripped.startswith(("  - ", "- ")):
                block.append(candidate)
                continue
            break
        return "\n".join(block) if len(block) > 1 else ""
    return ""


def render_operator_guidance_precedence(project_context: str | None) -> str:
    if extract_operator_guidance_block(project_context):
        return OPERATOR_GUIDANCE_PRECEDENCE_LINE
    return ""


def render_ops_first_contract(
    *,
    semantic_mode_available: bool = True,
    legacy_replace_available: bool = True,
) -> str:
    if semantic_mode_available:
        replace_shapes = (
            "replace legacy {op,path,old,new} or semantic {op,path,target_id,new}; "
            "replace forms are exclusive."
        )
    elif legacy_replace_available:
        replace_shapes = (
            "replace legacy {op,path,old,new}; semantic target mode is unavailable "
            "and must not be emitted."
        )
    else:
        replace_shapes = (
            "no replace operation; semantic target mode and legacy replace mode are "
            "unavailable without grounded current source."
        )
    return (
        "Use `ops` for file writes; put source in write_file/append_file/replace_in_file, not shell. "
        f"Supported ops: {render_supported_file_ops()}. "
        "Shapes: write/append {op,path,content}; "
        f"{replace_shapes} mkdir/delete {{op,path}}."
    )


def render_operation_choice_contract(
    *,
    semantic_mode_available: bool = True,
    legacy_replace_available: bool = True,
) -> str:
    """Render only the replace modes available to the current Planning task.

    The default preserves the existing contract for callers that do not yet
    have a provider-safe source projection.  Production Planning passes the
    filtered materialization capabilities so unavailable replace syntax is not
    advertised to the provider.
    """

    if semantic_mode_available:
        return (
            "Accepted replace shapes are `{op,path,old,new}` or `{op,path,target_id,new}`. "
            "For replace_in_file, use a listed Orchestrator `target_id` for the exact path; "
            "otherwise use exact `old` plus `new` from current evidence. Never mix `old` "
            "with `target_id`, invent IDs, or emit selector internals (offsets, versions, "
            "hashes, or derivation data)."
        )

    if legacy_replace_available:
        return (
            "Semantic target mode is unavailable for this task. Do not emit `target_id`. "
            "The only supported replace shape is `{op,path,old,new}`; use exact `old` "
            "plus `new` from the supplied current source evidence. Do not emit selector "
            "internals or fabricate replacement text."
        )

    return (
        "Semantic target mode is unavailable for this task. Do not emit `target_id`. "
        "Legacy replace mode is unavailable because no exact current source evidence "
        "is supplied. Do not fabricate `old` text; use only non-replace operations "
        "such as authorized `write_file`, `append_file`, `mkdir`, or `delete_file`."
    )


def render_shell_fallback_limits() -> str:
    return (
        "Shell is only for installs, builds, tests, inspection, and small commands; "
        "keep under 900 chars, relative, runnable. "
        "No heredocs, background processes, absolute helpers, parent traversal, pseudo-commands. "
        "If content needs quoting, move that content into `ops`."
    )


def render_test_scaffold_contract() -> str:
    return (
        "For new/changed tests: inspect nearby tests first; match their imports, "
        "fixtures, factories, and domain constructors. Do not replace project "
        "objects with raw dicts unless existing tests do. Compile changed Python "
        "tests before or with the final suite run."
    )


def render_verification_contract() -> str:
    return (
        "Verification must be a real project check with a nonzero failure mode, "
        "such as a test command, build command, compile command, import check, "
        "or content assertion grounded in current workspace files."
    )
