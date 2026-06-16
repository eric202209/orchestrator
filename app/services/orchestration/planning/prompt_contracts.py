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


def render_ops_first_contract() -> str:
    return (
        "Use `ops` for file writes; put source in write_file/append_file/replace_in_file, not shell. "
        f"Supported ops: {render_supported_file_ops()}. "
        "Shapes: write/append {op,path,content}; replace {op,path,old,new}; mkdir/delete {op,path}."
    )


def render_operation_choice_contract() -> str:
    return (
        "`replace_in_file` is only for exact old text already confirmed from "
        "current workspace evidence. If the old text is guessed, stale, or not "
        "shown in the current excerpt, inspect first or use another supported "
        "operation; do not invent helper identifiers or functions."
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
