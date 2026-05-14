"""Specialized planning repair prompt strategies.

Keep narrow repair modes here so PlannerService can delegate prompt shaping
without accumulating more validation-specific branching.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

_SOURCE_EXTENSIONS = {
    ".css",
    ".html",
    ".js",
    ".jsx",
    ".md",
    ".py",
    ".scss",
    ".svg",
    ".ts",
    ".tsx",
}
_EXCLUDED_PARTS = {
    ".git",
    ".openclaw",
    ".openclaw-workspaces",
    "__pycache__",
    "dist",
    "node_modules",
}


def build_specialized_repair_prompt(
    *,
    task_description: str,
    malformed_output: str,
    project_dir: Path,
    rejection_reasons: Optional[list[str]],
    knowledge_block: str = "",
) -> Optional[str]:
    """Return a specialized repair prompt when a known narrow mode applies."""

    reasons = [str(reason or "") for reason in (rejection_reasons or [])]
    if not _is_verification_workspace_repair(reasons):
        return None

    reason_lines = "\n".join(f"- {reason[:220]}" for reason in reasons[:5])
    existing_files = _render_workspace_source_inventory(project_dir)
    invalid_plan_summary = _summarize_plan_without_content(malformed_output)
    task = " ".join(str(task_description or "").split())[:900]
    knowledge = f"\n{knowledge_block.strip()}\n" if knowledge_block.strip() else ""

    return f"""Return ONLY a valid JSON array. First character must be `[`. Last must be `]`.
No prose. No markdown fences. No plan.json. No explanation.

Verification-only repair mode.

Task:
{task}

Validation errors:
{reason_lines or "- verification plan must be grounded in existing workspace files"}
{knowledge}
Existing workspace source files:
{existing_files or "- none detected"}

Invalid plan summary, with file contents removed:
{invalid_plan_summary}

Rules:
1. Repair the plan into 2 or 3 steps.
2. Do not create, rewrite, append, replace, or delete app source assets.
3. Do not use `ops` for app files such as HTML, CSS, JS, TS, SVG, or Python.
4. Do not invent conventional paths like styles.css, style.css, app.css, logo.svg, or icon.svg.
5. Use only paths from the existing workspace source files list.
6. Use runnable inspection and verification commands only.
7. Prefer `node -e` checks that read index.html, parse actual href/src references, and then read those referenced files.
8. `commands` must be an array of short shell strings; `verification` must be one shell string or null.
9. `expected_files` should be [] unless the task explicitly asks to create a verification helper file.
10. Each step must contain only: step_number, description, commands, verification, rollback, expected_files; optional ops only for verification helper files.
11. No background processes, dev servers, absolute paths, parent traversal, prose commands, or extra keys.
"""


def _is_verification_workspace_repair(reasons: list[str]) -> bool:
    combined = "\n".join(reasons).lower()
    return any(
        marker in combined
        for marker in (
            "verification/review plan references source files",
            "verification/review plan creates new app source assets",
            "verification/review plan mutates app source assets",
        )
    )


def _render_workspace_source_inventory(project_dir: Path) -> str:
    if not project_dir or not project_dir.exists():
        return ""
    paths: list[str] = []
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(project_dir)
        except ValueError:
            continue
        if any(part in _EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() not in _SOURCE_EXTENSIONS:
            continue
        paths.append(str(relative))
        if len(paths) >= 60:
            break
    return "\n".join(f"- {path}" for path in paths)


def _summarize_plan_without_content(malformed_output: str) -> str:
    try:
        parsed = json.loads(str(malformed_output or ""))
    except Exception:
        text = " ".join(str(malformed_output or "").split())
        return text[:900]

    if not isinstance(parsed, list):
        return str(type(parsed).__name__)

    lines: list[str] = []
    for index, step in enumerate(parsed[:5], start=1):
        if not isinstance(step, dict):
            lines.append(f"- step {index}: non-object")
            continue
        step_number = step.get("step_number", index)
        description = str(step.get("description") or "")[:160]
        commands = [
            str(command or "")[:180] for command in (step.get("commands") or [])[:3]
        ]
        verification = str(step.get("verification") or "")[:220]
        expected_files = [
            str(path or "") for path in (step.get("expected_files") or [])[:8]
        ]
        op_paths: list[str] = []
        for op in (step.get("ops") or [])[:8]:
            if isinstance(op, dict):
                op_name = str(op.get("op") or "")
                op_path = str(op.get("path") or "")
                op_paths.append(f"{op_name}:{op_path}" if op_path else op_name)
        lines.append(
            "- step {step}: {description}; commands={commands}; "
            "verification={verification}; expected_files={expected}; ops={ops}".format(
                step=step_number,
                description=description,
                commands=commands,
                verification=verification,
                expected=expected_files,
                ops=op_paths,
            )
        )
    return "\n".join(lines)
