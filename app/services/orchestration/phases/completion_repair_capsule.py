"""Phase 7H bounded completion repair capsule helpers."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
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
_REPAIR_EVIDENCE_KEYS = (
    "command",
    "returncode",
    "output",
    "paths",
    "path",
    "expected",
    "actual",
    "line",
    "line_number",
    "delta_evidence",
)
_MAX_REPAIR_EVIDENCE_OUTPUT_CHARS = 1200


class CompletionRepairProgress(StrEnum):
    """Same-validator truth for an applied Candidate Repair response."""

    RESOLVED = "RESOLVED"
    PARTIAL_PROGRESS = "PARTIAL_PROGRESS"
    NO_PROGRESS_OR_REGRESSION = "NO_PROGRESS_OR_REGRESSION"


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
    verification_failure: str = ""
    schema_version: int = 1
    source_file_contents: dict[str, str] = field(default_factory=dict)
    repair_objectives: list[dict[str, Any]] = field(default_factory=list)


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


def _candidate_paths_for_finding(finding: Any) -> list[str]:
    """Keep only candidate-relative paths supplied by typed finding evidence."""

    evidence = getattr(finding, "evidence", {}) or {}
    raw_paths = list(evidence.get("paths", []) or [])
    if evidence.get("path"):
        raw_paths.append(evidence["path"])
    raw_paths.extend(
        _extract_reason_paths(
            [
                str(getattr(finding, "message", "") or ""),
                str(evidence.get("command", "") or ""),
                str(evidence.get("output", "") or ""),
            ]
        )
    )
    paths: list[str] = []
    for raw_path in raw_paths:
        path = str(raw_path or "").strip().lstrip("./")
        if _is_plausible_relative_file(path) and path not in paths:
            paths.append(path)
    return paths


def _repair_objective_evidence(finding: Any) -> dict[str, Any]:
    """Render only the existing, repair-relevant typed evidence fields."""

    raw_evidence = getattr(finding, "evidence", {}) or {}
    evidence: dict[str, Any] = {}
    for key in _REPAIR_EVIDENCE_KEYS:
        value = raw_evidence.get(key)
        if value is None or value == "":
            continue
        if key == "paths":
            paths = _candidate_paths_for_finding(finding)
            if paths:
                evidence[key] = paths
        elif key == "path":
            paths = _candidate_paths_for_finding(finding)
            if paths:
                evidence[key] = paths[0]
        elif key == "output":
            evidence[key] = str(value)[:_MAX_REPAIR_EVIDENCE_OUTPUT_CHARS]
        elif isinstance(value, (str, int, float, bool)):
            evidence[key] = value
    return evidence


def _repair_objective_from_finding(finding: Any) -> dict[str, Any]:
    """Serialize CandidateFinding's existing contract without a parallel schema."""

    return {
        "rule_id": str(getattr(finding, "rule_id", "") or ""),
        "message": str(getattr(finding, "message", "") or ""),
        "candidate_paths": _candidate_paths_for_finding(finding),
        "source": str(getattr(finding, "source", "") or ""),
        "category": str(getattr(finding, "category", "") or ""),
        "attribution": str(getattr(finding, "attribution", "") or ""),
        "repairable": bool(getattr(finding, "repairable", False)),
        "evidence": _repair_objective_evidence(finding),
    }


def _repair_finding_identity(finding: Any) -> tuple[str, str, str, str, str, bool]:
    """Stable identity excludes diagnostic text that naturally changes per run."""

    def value(key: str, default: Any = "") -> Any:
        if isinstance(finding, dict):
            return finding.get(key, default)
        return getattr(finding, key, default)

    return (
        str(value("rule_id") or ""),
        str(value("source") or ""),
        str(value("category") or ""),
        str(value("severity") or ""),
        str(value("attribution") or ""),
        bool(value("repairable", False)),
    )


def completion_repair_finding_signature(validation: Any) -> str:
    """Return one stable signature for the normalized blocking finding set."""

    findings = (
        validation.get("findings", [])
        if isinstance(validation, dict)
        else getattr(validation, "findings", [])
    )
    identities = sorted(
        _repair_finding_identity(finding)
        for finding in list(findings or [])
        if (
            finding.get("severity", "")
            if isinstance(finding, dict)
            else getattr(finding, "severity", "")
        )
        == "error"
    )
    if not identities:
        return ""
    payload = json.dumps(identities, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def classify_completion_repair_progress(
    before_validation: Any,
    after_validation: Any,
) -> CompletionRepairProgress:
    """Classify a repair using the existing Candidate Validator as truth authority."""

    before_identity = getattr(before_validation, "candidate_identity", None)
    after_identity = getattr(after_validation, "candidate_identity", None)
    identity_changed = bool(
        before_identity and after_identity and before_identity != after_identity
    )
    before_findings = list(getattr(before_validation, "findings", []) or [])
    after_findings = list(getattr(after_validation, "findings", []) or [])
    before_repairable = {
        _repair_finding_identity(finding)
        for finding in before_findings
        if getattr(finding, "severity", "") == "error"
        and bool(getattr(finding, "repairable", False))
    }
    before_blocking = {
        _repair_finding_identity(finding)
        for finding in before_findings
        if getattr(finding, "severity", "") == "error"
    }
    after_blocking = {
        _repair_finding_identity(finding)
        for finding in after_findings
        if getattr(finding, "severity", "") == "error"
    }
    after_repairable = {
        _repair_finding_identity(finding)
        for finding in after_findings
        if getattr(finding, "severity", "") == "error"
        and bool(getattr(finding, "repairable", False))
    }

    if (
        identity_changed
        and getattr(after_validation, "status", "") == "accepted"
        and not after_findings
    ):
        return CompletionRepairProgress.RESOLVED
    if (
        identity_changed
        and bool(before_repairable - after_blocking)
        and not (after_blocking - before_blocking)
        and after_repairable
    ):
        return CompletionRepairProgress.PARTIAL_PROGRESS
    return CompletionRepairProgress.NO_PROGRESS_OR_REGRESSION


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


def source_visibility_label(content: str) -> str:
    """Label rendered source evidence as complete or truncated.

    A byte-exact ``replace_in_file`` anchor is only constructible from a file
    rendered in full, so the prompt labels each block and the operation rules
    offer exact-anchor operations for nothing else.
    """

    if content.endswith(_SOURCE_TRUNCATED_MARKER):
        shown = len(content) - len(_SOURCE_TRUNCATED_MARKER)
        return f"[TRUNCATED: only the first {shown} characters are shown]"
    return "[COMPLETE]"


def build_completion_repair_capsule(
    *,
    task_prompt: str,
    completion_validation: Any,
    orchestration_state: Any,
) -> CompletionRepairCapsule:
    repair_objectives = [
        _repair_objective_from_finding(finding)
        for finding in list(
            getattr(completion_validation, "repairable_findings", []) or []
        )[:10]
    ]
    reasons = [
        str(reason)
        for reason in list(getattr(completion_validation, "reasons", []) or [])[:10]
        if str(reason)
    ]
    details = getattr(completion_validation, "details", {}) or {}
    verification_failure = str(details.get("verification_output_preview") or "")[:1200]
    candidates: list[str] = []
    # Finding-aware ordering: the bounded source budget is spent first on the
    # files the validation reasons actually implicate, so the repair objective
    # is not the file most likely to be truncated out of provider visibility.
    for objective in repair_objectives:
        candidates.extend(objective["candidate_paths"])
    candidates.extend(_extract_reason_paths(reasons))
    candidates.extend(
        str(path) for path in details.get("expected_core_files", []) or []
    )
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
        verification_failure=verification_failure,
        source_file_contents=_read_bounded_source_contents(project_dir, relevant_files),
        repair_objectives=repair_objectives,
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
    objectives = json.dumps(capsule.repair_objectives, indent=2, sort_keys=True)
    if not capsule.repair_objectives:
        objectives = "[]"

    verification_failure_section = ""
    if capsule.verification_failure:
        verification_failure_section = (
            "\n\nReported verification failure (use this exact evidence):\n"
            + capsule.verification_failure
        )

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
            blocks.append(
                f"--- {rel_path} --- {source_visibility_label(content)}\n{content}"
            )
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
{reasons}{verification_failure_section}

Actionable typed repair objectives (repair all actionable repairable findings represented below):
{objectives}

Relevant existing files:
{relevant_files}

Last execution step:
{capsule.last_step_summary or "No execution results recorded."}{evidence_section}{source_content_section}{api_contract_section}

Rules:
1. Return exactly {{"repair_step": {{...}}}}. The repair_step object is the only executable object.
2. Inside repair_step, set repair_type to "ops_fix" and step_number to {next_step_number}.
3. description must be non-empty. "ops" must be a non-empty JSON array of structured file operations.
4. Each op must have "op" (write_file, append_file, or replace_in_file), "path" (relative to workspace root), and op-specific fields: "content" for write_file/append_file; "old" and "new" for replace_in_file.
5. "verification" must be safe: `python[3] -m compileall <.py/dir>`, `pytest`/`python[3] -m pytest <test>`, or `npm run build`. Relative; forbid `..`, `~`, absolute paths, flags, and metacharacters. Avoid `black`/`flake8`; Candidate reruns.
6. Do not use a "commands" key. Use ops only. The repair_step wrapper is canonical.
7. Prefer replace_in_file for targeted in-place edits in files marked [COMPLETE]; use write_file only to create a new file or fully overwrite a file marked [COMPLETE].
8. Use relative paths only; no absolute paths, "..", or "~". Do not return prose, markdown, comments, lists, plans, or fenced code.
9. Touch only relevant existing files, unless explicitly creating a required file. expected_files must list every file written.
10. For replace_in_file, copy old character-for-character from a file marked [COMPLETE] in CURRENT FILE CONTENT. Do not invent or guess old text. Never emit replace_in_file or write_file for an existing file marked [TRUNCATED] or missing from CURRENT FILE CONTENT: its full content is not visible, so any edit would be a guess.
11. Use only methods, attributes (including no invented attributes such as .tasks), and signatures in SOURCE API CONTRACT/CURRENT FILE CONTENT; match argument shapes. Implement any shown NotImplementedError with the same signature.
12. Resolve every actionable repairable finding represented above. One operation may resolve multiple findings; do not omit an independent objective merely because another objective was repaired.
13. directly address the reported expected/actual mismatch; do not make a cosmetic-only change.
"""
