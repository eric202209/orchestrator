"""10K-a — Planning repair faithfulness guard.

Ensures guidance-violation repairs preserve the original task objective.
The repair LLM must fix the style violation (e.g. mutable default) without
substituting unrelated function/class names or file paths.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

_PYTHON_KEYWORDS = frozenset(
    {
        "if",
        "else",
        "elif",
        "for",
        "while",
        "return",
        "def",
        "class",
        "import",
        "from",
        "in",
        "is",
        "not",
        "and",
        "or",
        "with",
        "as",
        "try",
        "except",
        "finally",
        "raise",
        "pass",
        "break",
        "continue",
        "lambda",
        "yield",
        "del",
        "global",
        "nonlocal",
        "assert",
        "True",
        "False",
        "None",
    }
)

_COMMON_WORDS = frozenset(
    {
        "get",
        "set",
        "list",
        "dict",
        "str",
        "int",
        "float",
        "bool",
        "type",
        "any",
        "all",
        "len",
        "range",
        "print",
        "input",
        "open",
        "true",
        "false",
        "null",
        "none",
        "self",
        "cls",
        "args",
        "kwargs",
    }
)


def extract_required_symbols(text: str) -> List[str]:
    """Extract specific Python function/class names from a task description.

    Captures names that appear:
    - As explicit ``def NAME`` or ``class NAME`` declarations.
    - As function calls with typed parameters: ``NAME(param: Type, ...)``.

    Common Python keywords and short generic words are excluded.
    Returns an empty list when no specific named artifacts are found.
    """
    text = str(text or "")
    found: List[str] = []
    seen: set = set()

    # Pattern 1: explicit def/class NAME
    for m in re.finditer(r"\b(?:def|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b", text):
        name = m.group(1)
        if name not in _PYTHON_KEYWORDS and name not in seen:
            found.append(name)
            seen.add(name)

    # Pattern 2: FUNCNAME(param: Type ...) — typed signature in task description
    for m in re.finditer(r"\b([a-z_][a-zA-Z0-9_]*)\s*\([^)]*:\s*[a-zA-Z]", text):
        name = m.group(1)
        if (
            name not in _PYTHON_KEYWORDS
            and name not in _COMMON_WORDS
            and name not in seen
            and len(name) > 3
        ):
            found.append(name)
            seen.add(name)

    return found


def extract_required_file_paths(text: str) -> List[str]:
    """Extract file/module paths (e.g. ``looptools/__init__.py``) from text."""
    text = str(text or "")
    found: List[str] = []
    seen: set = set()
    for m in re.finditer(
        r"\b([a-zA-Z_][a-zA-Z0-9_]*/[a-zA-Z_][a-zA-Z0-9_./]*\.[a-zA-Z0-9_]+)\b",
        text,
    ):
        path = m.group(1)
        if path not in seen:
            found.append(path)
            seen.add(path)
    return found


def _plan_to_text(plan: object) -> str:
    if isinstance(plan, list):
        try:
            return json.dumps(plan)
        except Exception:
            return str(plan)
    return str(plan or "")


def check_plan_faithfulness(
    task_description: str,
    repaired_plan: object,
    original_plan: Optional[object] = None,
) -> Tuple[bool, List[str]]:
    """Check whether the repaired plan preserves required symbols from the task.

    Returns ``(is_faithful, missing_symbols)``.  When no specific symbols are
    found in the task description the check trivially passes.

    If ``original_plan`` is provided, only symbols that also appeared in the
    original plan are required (avoids false positives from type annotation
    names in the task description that were never part of the plan).
    """
    task_description = str(task_description or "")
    if not task_description:
        return True, []

    required_symbols = extract_required_symbols(task_description)
    required_paths = extract_required_file_paths(task_description)

    if not required_symbols and not required_paths:
        return True, []

    if original_plan is not None:
        original_text = _plan_to_text(original_plan).lower()
        required_symbols = [s for s in required_symbols if s.lower() in original_text]
        required_paths = [p for p in required_paths if p.lower() in original_text]

    if not required_symbols and not required_paths:
        return True, []

    repaired_text = _plan_to_text(repaired_plan).lower()
    missing: List[str] = []

    for symbol in required_symbols:
        if not re.search(r"\b" + re.escape(symbol.lower()) + r"\b", repaired_text):
            missing.append(symbol)

    for path in required_paths:
        if path.lower() not in repaired_text:
            missing.append(path)

    if missing:
        return False, missing
    return True, []


def build_faithfulness_prompt_block(
    task_description: str,
    required_symbols: Optional[List[str]] = None,
) -> str:
    """Build faithfulness instruction block for inclusion in repair prompts.

    Returns an empty string when no specific named artifacts are found,
    so no-op for tasks without typed function signatures in the description.
    """
    task_description = str(task_description or "").strip()
    if not task_description:
        return ""

    if required_symbols is None:
        required_symbols = extract_required_symbols(task_description)

    if not required_symbols:
        return ""

    names = ", ".join(f"`{s}`" for s in required_symbols[:8])
    lines = [
        "Task objective (preserve exactly):",
        "- Preserve the original task objective."
        " Do not substitute the requested function, class, or module names with different ones.",
        "- Only change the minimal parts needed to satisfy the rejection reason"
        " (e.g. fix a mutable default without renaming the function).",
        "- If the rejection is a mutable default (= [] or = {}),"
        " fix only the default value (e.g. = None, initialize inside the function)."
        " Keep the function name.",
        "- Keep verification targeted to the originally requested artifact.",
        f"- Required named artifacts (do not rename or remove): {names}",
    ]
    return "\n".join(lines)
