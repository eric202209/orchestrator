"""Shared prompt contract snippets for planning prompts."""

from __future__ import annotations

from app.services.orchestration.operations.file_ops_contract import (
    render_supported_file_ops,
)


def render_ops_first_contract() -> str:
    return (
        "Use `ops` for file writes; put source in write_file/append_file/replace_in_file, not shell. "
        f"Supported ops: {render_supported_file_ops()}."
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
        "For Python verify with `python -m py_compile`, unittest, or pytest. "
        "For Python app import assertions, create a tiny test file with `ops` "
        "instead of inline `python -c` snippets."
    )


def render_static_site_verification_contract() -> str:
    return (
        "For static HTML/CSS without package.json, prefer `python -c` "
        "file/content checks; use Node only for Node projects."
    )
