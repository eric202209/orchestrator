"""Phase 7H bounded completion repair capsule helpers."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.orchestration.prompt_templates import StepResult
from app.services.workspace.path_display import render_workspace_path_for_prompt

MAX_RELEVANT_FILES = 25
MAX_LAST_STEP_CHARS = 400
MAX_TASK_PROMPT_EXCERPT_CHARS = 800
MAX_SOURCE_CONTENT_PER_FILE_CHARS = 2000
MAX_SOURCE_CONTENT_TOTAL_CHARS = 5000
_SOURCE_TRUNCATED_MARKER = "... [truncated]"
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w./:-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9_.-]+)(?![\w./:-])"
)


def _format_arg(arg: ast.arg) -> str:
    if arg.annotation is not None:
        return f"{arg.arg}: {ast.unparse(arg.annotation)}"
    return arg.arg


def _format_func_sig_from_ast(
    func_name: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    """Return 'func_name(arg: type, ...) -> return' from an AST function node."""
    a = node.args
    parts: list[str] = []
    parts.extend(_format_arg(x) for x in a.args)
    if a.vararg is not None:
        parts.append(f"*{_format_arg(a.vararg)}")
    elif a.kwonlyargs:
        parts.append("*")
    parts.extend(_format_arg(x) for x in a.kwonlyargs)
    if a.kwarg is not None:
        parts.append(f"**{_format_arg(a.kwarg)}")
    sig = f"{func_name}({', '.join(parts)})"
    if node.returns is not None:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


def _extract_source_api_contract(source_file_contents: dict[str, str]) -> str:
    """Extract compact function/method signatures from Python source_file_contents.

    Returns a formatted multi-line string listing per-file signatures.
    Non-Python files and files that fail to parse are silently skipped.
    Truncation markers are stripped before parsing so partial files still yield signatures.
    """
    sections: list[str] = []
    for rel_path, content in source_file_contents.items():
        if not rel_path.endswith(".py"):
            continue
        parse_content = content
        if parse_content.endswith(_SOURCE_TRUNCATED_MARKER):
            parse_content = parse_content[: -len(_SOURCE_TRUNCATED_MARKER)]
        try:
            tree = ast.parse(parse_content)
        except SyntaxError:
            continue
        lines: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines.append(f"  - {_format_func_sig_from_ast(node.name, node)}")
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        lines.append(
                            f"  - {_format_func_sig_from_ast(f'{node.name}.{item.name}', item)}"
                        )
        if lines:
            sections.append(f"- {rel_path}\n" + "\n".join(lines))
    return "\n".join(sections)


@dataclass
class CompletionRepairCapsule:
    validation_reasons: list[str]
    relevant_files: list[str]
    last_step_summary: str
    workspace_path: str
    task_prompt_excerpt: str
    schema_version: int = 1
    source_file_contents: dict[str, str] = field(default_factory=dict)


def _trim(text: Any, max_chars: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def _is_plausible_relative_file(path_text: str) -> bool:
    if not path_text or "://" in path_text or any(ch.isspace() for ch in path_text):
        return False
    path = Path(path_text)
    if path.is_absolute() or ".." in path.parts:
        return False
    return bool(path.suffix)


def _extract_reason_paths(reasons: list[str]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        for match in _PATH_TOKEN_RE.finditer(str(reason or "")):
            candidate = match.group(1).strip("`'\".,:;()[]{}")
            if not _is_plausible_relative_file(candidate):
                continue
            if candidate not in seen:
                seen.add(candidate)
                paths.append(candidate)
    return paths


def _step_files_changed(result: Any) -> list[str]:
    files = getattr(result, "files_changed", None)
    if files is None and isinstance(result, dict):
        files = result.get("files_changed")
    return [str(path).strip() for path in (files or []) if str(path).strip()]


def _step_status(result: Any) -> str:
    if isinstance(result, StepResult):
        return result.status
    if isinstance(result, dict):
        return str(result.get("status") or "")
    return str(getattr(result, "status", "") or "")


def _step_number(result: Any) -> int:
    if isinstance(result, StepResult):
        return int(result.step_number or 0)
    if isinstance(result, dict):
        return int(result.get("step_number") or 0)
    return int(getattr(result, "step_number", 0) or 0)


def _last_step_summary(orchestration_state: Any) -> str:
    results = list(getattr(orchestration_state, "execution_results", []) or [])
    if not results:
        return ""
    latest = results[-1]
    step_number = _step_number(latest)
    description = ""
    plan = list(getattr(orchestration_state, "plan", []) or [])
    if step_number > 0 and step_number <= len(plan):
        description = str((plan[step_number - 1] or {}).get("description") or "")
    if not description:
        description = f"Step {step_number}" if step_number else "Latest step"
    files = _step_files_changed(latest)
    files_text = ", ".join(files[:8]) if files else "none"
    return _trim(
        f"Step {step_number}: {description} - {_step_status(latest)}. Files: {files_text}.",
        MAX_LAST_STEP_CHARS,
    )


def _workspace_existing_files(project_dir: Path, candidates: list[str]) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()
    root = project_dir.resolve()
    for candidate in candidates:
        rel_path = str(candidate or "").strip().lstrip("./")
        if not _is_plausible_relative_file(rel_path) or rel_path in seen:
            continue
        path = (root / rel_path).resolve()
        try:
            if path.is_relative_to(root) and path.is_file():
                seen.add(rel_path)
                kept.append(rel_path)
        except OSError:
            continue
        if len(kept) >= MAX_RELEVANT_FILES:
            break
    return kept


def _read_bounded_source_contents(
    project_dir: Path,
    rel_paths: list[str],
) -> dict[str, str]:
    """Read bounded current content for each relevant file.

    Returns {rel_path: content} preserving rel_paths order.
    Per-file cap: MAX_SOURCE_CONTENT_PER_FILE_CHARS. Total cap: MAX_SOURCE_CONTENT_TOTAL_CHARS.
    Content exceeding the per-file cap is truncated and suffixed with _SOURCE_TRUNCATED_MARKER.
    """
    contents: dict[str, str] = {}
    total_chars = 0
    root = project_dir.resolve()
    for rel_path in rel_paths:
        if total_chars >= MAX_SOURCE_CONTENT_TOTAL_CHARS:
            break
        abs_path = (root / rel_path).resolve()
        try:
            if not abs_path.is_relative_to(root) or not abs_path.is_file():
                continue
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        remaining = MAX_SOURCE_CONTENT_TOTAL_CHARS - total_chars
        cap = min(MAX_SOURCE_CONTENT_PER_FILE_CHARS, remaining)
        if len(text) > cap:
            content = text[:cap] + _SOURCE_TRUNCATED_MARKER
        else:
            content = text
        contents[rel_path] = content
        total_chars += len(content)
    return contents


def build_completion_repair_capsule(
    *,
    task_prompt: str,
    completion_validation: Any,
    orchestration_state: Any,
) -> CompletionRepairCapsule:
    reasons = [
        str(reason)
        for reason in list(getattr(completion_validation, "reasons", []) or [])[:10]
        if str(reason)
    ]
    details = getattr(completion_validation, "details", {}) or {}
    candidates: list[str] = []
    candidates.extend(
        str(path) for path in details.get("expected_core_files", []) or []
    )
    candidates.extend(_extract_reason_paths(reasons))
    for result in list(getattr(orchestration_state, "execution_results", []) or [])[
        -2:
    ]:
        candidates.extend(_step_files_changed(result))

    project_dir = Path(getattr(orchestration_state, "project_dir"))
    relevant_files = _workspace_existing_files(project_dir, candidates)
    return CompletionRepairCapsule(
        validation_reasons=reasons,
        relevant_files=relevant_files,
        last_step_summary=_last_step_summary(orchestration_state),
        workspace_path=str(project_dir),
        task_prompt_excerpt=str(task_prompt or "")[:MAX_TASK_PROMPT_EXCERPT_CHARS],
        source_file_contents=_read_bounded_source_contents(project_dir, relevant_files),
    )


def build_bounded_completion_repair_prompt(
    capsule: CompletionRepairCapsule,
    next_step_number: int,
    evidence_capsule: Any = None,
) -> str:
    workspace = render_workspace_path_for_prompt(capsule.workspace_path)
    relevant_files = "\n".join(f"- {path}" for path in capsule.relevant_files)
    if not relevant_files:
        relevant_files = "- No existing relevant files were found; create only files required by validation."
    reasons = "\n".join(f"- {reason}" for reason in capsule.validation_reasons)
    if not reasons:
        reasons = "- Completion validation failed without detailed reasons."

    evidence_section = ""
    if evidence_capsule is not None:
        from app.services.orchestration.diagnostics.evidence_capsule import (
            render_evidence_section,
        )

        rendered = render_evidence_section(evidence_capsule)
        if rendered:
            evidence_section = f"\n{rendered}\n"

    source_content_section = ""
    if capsule.source_file_contents:
        blocks = []
        for rel_path, content in capsule.source_file_contents.items():
            blocks.append(f"--- {rel_path} ---\n{content}")
        source_content_section = "\n\nCURRENT FILE CONTENT:\n" + "\n\n".join(blocks)

    api_contract_section = ""
    if capsule.source_file_contents:
        contract = _extract_source_api_contract(capsule.source_file_contents)
        if contract:
            api_contract_section = (
                "\n\nSOURCE API CONTRACT"
                " (derived from files above — these are the ONLY valid APIs):\n"
                + contract
            )

    return f"""Return one minimal JSON completion repair envelope. Output JSON object only.

Task excerpt:
{capsule.task_prompt_excerpt}

Working directory:
{workspace}

Completion validation reasons:
{reasons}

Relevant existing files:
{relevant_files}

Last execution step:
{capsule.last_step_summary or "No execution results recorded."}{evidence_section}{source_content_section}{api_contract_section}

Rules:
1. Return exactly {{"repair_step": {{...}}}}. The repair_step object is the only executable object.
2. Inside repair_step, set repair_type to "ops_fix" and step_number to {next_step_number}.
3. description must be non-empty. ops must be a non-empty JSON array of structured file operations.
4. Each op must have "op" (write_file, append_file, or replace_in_file), "path" (relative to workspace root), and op-specific fields: "content" for write_file/append_file; "old" and "new" for replace_in_file.
5. verification must be one non-empty top-level shell command string. No shell metacharacters.
6. Do not use a "commands" key. Use ops only.
   The canonical producer envelope is the repair_step wrapper.
7. Prefer replace_in_file for targeted in-place edits; use write_file only to create or fully overwrite a file.
8. Use relative paths only; no absolute paths, "..", or "~".
9. Do not return prose, markdown, comments, a list, a second plan, or fenced code.
10. Touch only files that appear in the relevant existing files list, unless ops explicitly create a new required file.
11. expected_files must list every file path that ops write to.
12. For replace_in_file, copy the "old" value character-for-character from CURRENT FILE CONTENT above — same whitespace, indentation, quotes, and line breaks. Do not reconstruct from memory or training data.
13. If the exact text to replace is not shown in CURRENT FILE CONTENT, use write_file with the complete corrected file instead of replace_in_file. Do not invent or guess "old" text.
14. Generate ops for every file that must change to make the failing tests pass. The repair may include multiple ops across multiple files. Do not restrict the repair to the file named in the error traceback if another file in CURRENT FILE CONTENT contains the missing or broken implementation.
15. Use only methods, attributes, and function signatures listed in SOURCE API CONTRACT and CURRENT FILE CONTENT. Do not invent or assume the existence of attributes (such as .tasks) or methods not shown there.
16. Do not call functions or methods with argument shapes different from their signatures in SOURCE API CONTRACT. If a signature shows (total: int, completed: int), call it with two integer arguments — not with a single object argument.
17. If a function body in CURRENT FILE CONTENT raises NotImplementedError, generate an op to implement that body with the exact same signature. Do not route around an unimplemented stub by calling it with incorrect arguments or skipping its implementation.

Output example:
{{
  "step_number": {next_step_number},
  "repair_type": "ops_fix",
  "description": "Fix format_summary signature to match pre-run contract",
  "ops": [
    {{
      "op": "replace_in_file",
      "path": "src/medium_cli/formatting.py",
      "old": "def format_summary(*, total: int = 0, completed: int = 0) -> str:",
      "new": "def format_summary(total: int, completed: int) -> str:"
    }}
  ],
  "verification": "python -m pytest -q",
  "expected_files": ["src/medium_cli/formatting.py"]
}}
"""
