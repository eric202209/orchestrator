"""10K-c — Requested symbol completion verification.

Lightweight deterministic check that symbols explicitly named in a task
description are actually present in the final changed Python files.

Activates only when the task description contains explicit typed function/class
signatures or ``def``/``class`` keywords AND changed files include Python files
AND the execution profile is not review-only.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List

from app.services.orchestration.planning.repair_faithfulness import (
    extract_required_symbols,
)

_REVIEW_ONLY_PROFILES = frozenset({"review_only"})


def _extract_top_level_symbol_names(file_path: Path) -> List[str]:
    """Return all top-level function and class names from a Python source file.

    Returns an empty list on any parse error — never raises.
    """
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except Exception:
        return []
    names: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return names


def check_completion_symbol_presence(
    task_description: str,
    reported_changed_files: List[str],
    project_dir: Path,
    execution_profile: str = "full_lifecycle",
) -> Dict[str, Any]:
    """Check that symbols required by the task are present in changed Python files.

    Returns a dict with:
    - ``applicable``:  True when the check is meaningful for this task.
    - ``passed``:      True when all required symbols found (or check not applicable).
    - ``missing``:     Symbol names absent from all changed Python files.
    - ``found``:       Symbol names confirmed present in changed Python files.
    - ``required``:    Symbol names extracted from the task description.
    """
    result: Dict[str, Any] = {
        "applicable": False,
        "passed": True,
        "missing": [],
        "found": [],
        "required": [],
    }

    if execution_profile in _REVIEW_ONLY_PROFILES:
        return result

    required = extract_required_symbols(str(task_description or ""))
    if not required:
        return result

    python_files = [f for f in (reported_changed_files or []) if str(f).endswith(".py")]
    if not python_files:
        return result

    result["applicable"] = True
    result["required"] = list(required)

    found_names: set[str] = set()
    for rel_path in python_files:
        p = Path(rel_path)
        abs_path = p if p.is_absolute() else project_dir / p
        found_names.update(_extract_top_level_symbol_names(abs_path))

    missing = [s for s in required if s not in found_names]
    found = [s for s in required if s in found_names]

    result["found"] = found
    result["missing"] = missing
    result["passed"] = len(missing) == 0
    return result
