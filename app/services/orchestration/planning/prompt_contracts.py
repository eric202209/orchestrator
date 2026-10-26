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
        if legacy_replace_available:
            replace_shapes = (
                "replace legacy {op,path,old,new} or semantic "
                "{op,path,target_id,new}; replace forms are exclusive."
            )
        else:
            replace_shapes = "replace semantic {op,path,target_id,new} only."
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

    if semantic_mode_available and legacy_replace_available:
        return (
            "Accepted replace shapes are `{op,path,old,new}` or `{op,path,target_id,new}`. "
            "For replace_in_file, use a listed Orchestrator `target_id` for the exact path; "
            "otherwise use exact `old` plus `new` from current evidence. Never mix `old` "
            "with `target_id`, invent IDs, or emit selector internals (offsets, versions, "
            "hashes, or derivation data)."
        )
    if semantic_mode_available:
        return (
            "Semantic target mode is available. Use only a listed Orchestrator "
            "`target_id` for `replace_in_file`; do not emit `old` or selector internals."
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


def render_source_edit_decision_contract() -> str:
    """Give Planning a short, explicit create/edit/rewrite decision rule."""

    return (
        "`write_file` replaces the whole file. New files require creation "
        "authorization and `expected_files`. For a localized existing-file "
        "change, prefer an available precise `replace_in_file`; exact `old` "
        "must be unique in supplied same-path source, and stale, missing, or "
        "ambiguous text must not be guessed. Existing-file `write_file` is only "
        "for task-required full rewrites with complete GR2-grounded source and "
        "replacement content; preserve imports, helpers, definitions, and "
        "behavior unless authorized to change them, and state whole-file intent "
        "in the description."
    )


def render_existing_file_mutation_contract(
    *, legacy_replace_available: bool = True
) -> str:
    """Render the legal operation shapes for creating vs. mutating a file.

    The validator authorizes a whole-file ``write_file`` against an already
    existing path only when the step states that replacement intent, and
    otherwise expects a grounded ``replace_in_file``.  Planning previously
    received neither fact, so a correct edit could be rejected purely on the
    verb chosen for the step description.
    """

    if not legacy_replace_available:
        return ""
    return (
        "Existing file: no bare `write_file`. It replaces the entire file, so "
        "prefer `replace_in_file` with exact current `old` text for a localized "
        "change. Use `write_file` only for a task-required complete rewrite with "
        "full grounded source and complete replacement content; state the "
        "whole-file intent (replace/rewrite/overwrite/rebuild/preserve) in `description` "
        "as the validator requires. New file: complete `write_file` content "
        "only when creation is authorized, listed in that step's `expected_files`."
    )


def render_existing_file_write_contract() -> str:
    """Render the grounded whole-file route without advertising replace syntax."""

    return (
        "Existing file: no bare `write_file`. It replaces the entire file. "
        "Use it only for a task-required complete rewrite with the full current "
        "source grounded for GR2, complete replacement content, and explicit "
        "whole-file intent (replace/rewrite/overwrite/rebuild/preserve) in "
        "`description`. New file: complete `write_file` content only when "
        "creation is authorized and listed in that step's `expected_files`."
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
        "Tests: inspect tests; compile Python. Explicit new/regression intent "
        "require a supported test mutation (append_file, replace_in_file, or authorized "
        "write_file) per target; inspection, verification, or expected_files alone "
        "do not satisfy it. Never invent paths."
    )


def render_verification_contract() -> str:
    return (
        "Verification must be a real project check with a nonzero failure mode, "
        "such as a test command, build command, compile command, import check, "
        "or content assertion grounded in current workspace files."
    )
