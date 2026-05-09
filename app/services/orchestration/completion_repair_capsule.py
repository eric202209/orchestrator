"""Phase 7H bounded completion repair capsule helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.prompt_templates import StepResult
from app.services.workspace.path_display import render_workspace_path_for_prompt

MAX_RELEVANT_FILES = 25
MAX_LAST_STEP_CHARS = 400
MAX_TASK_PROMPT_EXCERPT_CHARS = 800
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w./:-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9_.-]+)(?![\w./:-])"
)


@dataclass
class CompletionRepairCapsule:
    validation_reasons: list[str]
    relevant_files: list[str]
    last_step_summary: str
    workspace_path: str
    task_prompt_excerpt: str
    schema_version: int = 1


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
    return CompletionRepairCapsule(
        validation_reasons=reasons,
        relevant_files=_workspace_existing_files(project_dir, candidates),
        last_step_summary=_last_step_summary(orchestration_state),
        workspace_path=str(project_dir),
        task_prompt_excerpt=str(task_prompt or "")[:MAX_TASK_PROMPT_EXCERPT_CHARS],
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
        from app.services.orchestration.evidence_capsule import render_evidence_section

        rendered = render_evidence_section(evidence_capsule)
        if rendered:
            evidence_section = f"\n{rendered}\n"

    return f"""Return one minimal JSON completion repair step. Output JSON object only.

Task excerpt:
{capsule.task_prompt_excerpt}

Working directory:
{workspace}

Completion validation reasons:
{reasons}

Relevant existing files:
{relevant_files}

Last execution step:
{capsule.last_step_summary or "No execution results recorded."}{evidence_section}

Rules:
1. Return a single JSON object with keys: step_number, description, commands, verification, rollback, expected_files.
2. Use step_number {next_step_number}.
3. Keep the fix atomic and minimal.
4. Do not return prose, markdown, comments, or fenced code.
5. Touch only relevant existing files unless commands explicitly create a required missing file.
6. Do not rewrite unrelated files or inspect broad workspace inventory.
7. Use relative paths only; no absolute paths, `..`, or `~`.

Output example:
{{
  "step_number": {next_step_number},
  "description": "Fix missing completion artifact and verify it loads",
  "commands": ["python3 - <<'PY'\\nfrom pathlib import Path\\nPath('src/main.py').write_text('print(1)\\\\n')\\nPY"],
  "verification": "python3 -m py_compile src/main.py",
  "rollback": null,
  "expected_files": ["src/main.py"]
}}
"""
