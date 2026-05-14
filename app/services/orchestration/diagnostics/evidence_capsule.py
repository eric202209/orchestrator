"""Phase 7L workspace evidence capsule.

Orchestrator-owned, deterministic workspace search before repair.
Qwen receives evidence output; it does not choose or run search commands.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_TOTAL_BUDGET = 1500
_PER_CMD_CHARS = 350
_PER_CMD_TIMEOUT = 5
_TOTAL_TIMEOUT = 15

_MODULE_RE = re.compile(r"No module named '([A-Za-z0-9_. ]+)'")
_IMPORT_RE = re.compile(r"(?:from|import)\s+([A-Za-z0-9_.]+)")
_SENSITIVE_MARKERS = (
    ".env",
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "private_key",
)


@dataclass
class WorkspaceEvidenceCapsule:
    failure_class: str
    commands_run: list[str] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)
    files_inspected: list[str] = field(default_factory=list)
    matched_line_count: int = 0
    total_chars: int = 0
    schema_version: int = 1

    def is_empty(self) -> bool:
        return not self.results or self.total_chars == 0


def _truncate(text: str, max_chars: int = _PER_CMD_CHARS) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _run_cmd(args: list[str], cwd: Path, timeout: int = _PER_CMD_TIMEOUT) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        raw = (result.stdout or "") + (result.stderr or "")
        return _truncate(_sanitize_output(raw))
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return ""


def _sanitize_output(text: str) -> str:
    """Drop lines likely to expose secrets or secret-bearing filenames."""
    safe_lines: list[str] = []
    for line in str(text or "").splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in _SENSITIVE_MARKERS):
            continue
        safe_lines.append(line)
    return "\n".join(safe_lines).strip()


def _extract_module_name(failure_context: str) -> Optional[str]:
    m = _MODULE_RE.search(failure_context)
    if m:
        return m.group(1).split(".")[0].strip()
    m = _IMPORT_RE.search(failure_context)
    if m:
        return m.group(1).split(".")[0].strip()
    return None


def _commands_for_failure_class(
    failure_class: str,
    project_dir: Path,
    failure_context: str,
) -> list[list[str]]:
    """Return ordered list of command arg-lists for given failure class."""
    cmds: list[list[str]] = []

    if failure_class in ("module_not_found", "import_error"):
        mod = _extract_module_name(failure_context)
        cmds.append(["find", ".", "-maxdepth", "4", "-name", "*.py", "-type", "f"])
        if mod:
            cmds.append(["grep", "-rn", f"import {mod}", ".", "--include=*.py", "-l"])
            cmds.append(["grep", "-rn", f"from {mod}", ".", "--include=*.py", "-l"])
        else:
            cmds.append(["find", ".", "-maxdepth", "2", "-name", "requirements*.txt"])

    elif failure_class == "pytest_failure":
        cmds.append(
            [
                "find",
                ".",
                "-maxdepth",
                "4",
                "-type",
                "f",
                "(",
                "-name",
                "test_*.py",
                "-o",
                "-name",
                "*_test.py",
                ")",
            ]
        )
        cmds.append(["grep", "-rn", "assert ", ".", "--include=*.py"])
        cmds.append(["find", ".", "-maxdepth", "3", "-name", "conftest.py"])

    elif failure_class == "syntax_error":
        # Locate Python files recently touched; run py_compile on primary file if found
        cmds.append(["find", ".", "-maxdepth", "3", "-name", "*.py", "-newer", "."])
        # Generic syntax scan fallback
        cmds.append(["grep", "-rn", "SyntaxError", ".", "--include=*.py", "-l"])

    elif failure_class == "missing_dependency":
        cmds.append(["find", ".", "-maxdepth", "2", "-name", "requirements*.txt"])
        cmds.append(["find", ".", "-maxdepth", "2", "-name", "package.json"])

    else:
        # Unknown / completion_validation_failed / runtime_assertion_failure
        cmds.append(["find", ".", "-maxdepth", "2", "-name", "*.py", "-type", "f"])

    return cmds


def collect_workspace_evidence(
    failure_class: str,
    project_dir: Path,
    *,
    failure_context: str = "",
) -> WorkspaceEvidenceCapsule:
    """Run bounded workspace search and return compact evidence capsule.

    All commands are chosen by the orchestrator; Qwen never sees or picks them.
    Degrades gracefully: any subprocess failure yields empty result for that command.
    """
    capsule = WorkspaceEvidenceCapsule(failure_class=failure_class)
    cmds = _commands_for_failure_class(failure_class, project_dir, failure_context)
    total_chars = 0

    for args in cmds:
        if total_chars >= _TOTAL_BUDGET:
            break
        cmd_str = " ".join(args)
        capsule.commands_run.append(cmd_str)
        output = _run_cmd(args, cwd=project_dir)
        if not output:
            capsule.results[cmd_str] = ""
            continue

        remaining = _TOTAL_BUDGET - total_chars
        capped = _truncate(output, min(_PER_CMD_CHARS, remaining))
        capsule.results[cmd_str] = capped
        total_chars += len(capped)

        # Collect file paths from output lines
        for line in capped.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                p = Path(stripped.lstrip("./"))
                if p.suffix in (".py", ".txt", ".json", ".toml", ".cfg", ".ini"):
                    capsule.files_inspected.append(stripped)
                    capsule.matched_line_count += 1

    capsule.total_chars = total_chars
    return capsule


def render_evidence_section(capsule: WorkspaceEvidenceCapsule) -> str:
    """Render compact evidence section for injection into repair prompts."""
    if capsule.is_empty():
        return ""
    lines = ["Workspace evidence:"]
    for cmd, output in capsule.results.items():
        if output:
            lines.append(f"$ {cmd}")
            lines.append(output)
    return "\n".join(lines)
