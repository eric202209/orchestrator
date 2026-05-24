"""Shared prompt contract snippets for planning prompts."""

from __future__ import annotations

from app.services.orchestration.operations.file_ops_contract import (
    render_supported_file_ops,
)


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


def render_python_verification_contract() -> str:
    return (
        "For Python verify with `python -m py_compile`, unittest, or pytest; "
        "if pytest config/tests exist, prefer final `python -m pytest tests/ -q`. "
        "For Python app import assertions, create a tiny test file with `ops` "
        "instead of inline `python -c` snippets."
    )


def render_test_scaffold_contract() -> str:
    return (
        "For new/changed tests: inspect nearby tests first; match their imports, "
        "fixtures, factories, and domain constructors. Do not replace project "
        "objects with raw dicts unless existing tests do. Compile changed Python "
        "tests before or with the final suite run."
    )


def render_static_site_verification_contract() -> str:
    return (
        "For static HTML/CSS without package.json, prefer `python -c` "
        "file/content checks; use Node only for Node projects."
    )
