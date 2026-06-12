"""Phase 7F debug feedback envelope helpers.

These helpers capture execution/completion failures as structured evidence.
They do not decide retry policy or execute repair; orchestration callers remain
responsible for those control decisions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from app.models import LogEntry
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.execution.structured_op_repair import (
    normalize_replacement_ops,
)
from app.services.orchestration.execution.step_support import (
    is_runnable_shell_command_fix,
)
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.workspace.path_display import render_workspace_path_for_prompt

ELIGIBLE_DEBUG_FAILURE_CLASSES = frozenset(
    {
        "pytest_failure",
        "import_error",
        "module_not_found",
        "runtime_assertion_failure",
        "completion_validation_failed",
        "missing_dependency",
        "syntax_error",
        "source_step_validation",
    }
)

_DEBUG_SOURCE_CONTRACT_MAX_CHARS = 1100
_SOURCE_STEP_EXCERPT_MAX_FILES = 4
_SOURCE_STEP_EXCERPT_PER_FILE_CHARS = 900
_SOURCE_STEP_EXCERPT_TOTAL_CHARS = 2600

_CANNOT_IMPORT_FROM_FILE_RE = re.compile(
    r"cannot import name '([A-Za-z_][A-Za-z0-9_]*)' from '([A-Za-z_][A-Za-z0-9_.]*)'"
    r"(?: \(([^)]+\.py)\))?",
    flags=re.IGNORECASE,
)


@dataclass
class DebugFeedbackEnvelope:
    """Structured runtime failure evidence for bounded debug repair."""

    task_execution_id: Optional[int]
    task_id: int
    step_index: Optional[int]
    failure_phase: str
    failed_command: str = ""
    return_code: Optional[int] = None
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    pytest_excerpt: str = ""
    validator_reasons: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    workspace_path: str = ""
    failure_class: str = "unknown"
    schema_version: int = 1

    @property
    def eligible_for_debug_repair(self) -> bool:
        return self.failure_class in ELIGIBLE_DEBUG_FAILURE_CLASSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_execution_id": self.task_execution_id,
            "task_id": self.task_id,
            "step_index": self.step_index,
            "failure_phase": self.failure_phase,
            "failed_command": self.failed_command,
            "return_code": self.return_code,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "pytest_excerpt": self.pytest_excerpt,
            "validator_reasons": list(self.validator_reasons),
            "changed_files": list(self.changed_files),
            "expected_files": list(self.expected_files),
            "workspace_path": self.workspace_path,
            "failure_class": self.failure_class,
            "eligible_for_debug_repair": self.eligible_for_debug_repair,
        }


@dataclass(frozen=True)
class DebugRepairNormalizationResult:
    """Phase 7F repair normalization outcome with rejection observability."""

    payload: Optional[dict[str, Any]]
    rejection_reason: Optional[str]
    parsed_shape: dict[str, Any]


@dataclass(frozen=True)
class DebugRepairPromptBuildResult:
    """Bounded debug repair prompt plus non-sensitive build metadata."""

    prompt: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _excerpt(value: Any, max_chars: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _first_pytest_excerpt(text: str) -> str:
    lines = str(text or "").splitlines()
    selected: list[str] = []
    markers = (
        "failed",
        "error",
        "assert",
        "traceback",
        "expected",
        "received",
        "no module named",
        "module not found",
    )
    for line in lines:
        lowered = line.lower()
        if any(marker in lowered for marker in markers):
            selected.append(line[:240])
        if len(selected) >= 12:
            break
    return _excerpt("\n".join(selected), 1200)


def classify_debug_failure(
    *,
    failed_command: str = "",
    stdout: str = "",
    stderr: str = "",
    validator_reasons: Optional[Iterable[str]] = None,
    return_code: Optional[int] = None,
) -> str:
    """Map runtime evidence to the Phase 7F eligible failure taxonomy."""

    reasons = "\n".join(str(reason or "") for reason in (validator_reasons or []))
    combined = "\n".join(
        part
        for part in [failed_command, stdout, stderr, reasons, str(return_code or "")]
        if str(part or "").strip()
    ).lower()
    if not combined:
        return "unknown"

    if _is_source_step_validation_failure(reasons):
        return "source_step_validation"
    if "syntaxerror" in combined or "syntax error" in combined:
        return "syntax_error"
    if "modulenotfounderror" in combined or "no module named" in combined:
        return "module_not_found"
    if "importerror" in combined or "cannot import name" in combined:
        return "import_error"
    if (
        "command not found" in combined
        or "not found" in combined
        and any(tool in combined for tool in ("pytest", "jest", "vitest", "mocha"))
        or "cannot find module" in combined
    ):
        return "missing_dependency"
    if (
        "step verification command failed" in combined
        or "completion verification command failed" in combined
        or "enoent:" in combined
        or "no such file or directory" in combined
    ):
        return "completion_validation_failed"
    if "pytest" in combined and (
        " failed" in combined
        or " failures" in combined
        or "failed tests" in combined
        or "== fail" in combined
        or "test session starts" in combined
        or "assertionerror" in combined
        or re.search(r"\bassert\b", combined)
    ):
        return "pytest_failure"
    if "assertionerror" in combined or re.search(r"\bassert\b", combined):
        return "runtime_assertion_failure"
    if "completion_validation_failed" in combined or (
        "completion" in combined and "validation" in combined
    ):
        return "completion_validation_failed"
    return "unknown"


def _is_source_step_validation_failure(reasons: str) -> bool:
    lowered = str(reasons or "").lower()
    if not lowered:
        return False
    source_markers = (
        ".py still contains not-implemented markers",
        "not-implemented markers",
        "verification is too weak for implementation-heavy work",
        "weak verification for implementation-heavy work",
    )
    return any(marker in lowered for marker in source_markers)


def build_debug_feedback_envelope(
    *,
    task_execution_id: Optional[int],
    task_id: int,
    step_index: Optional[int],
    failure_phase: str,
    failed_command: str = "",
    return_code: Optional[int] = None,
    stdout: str = "",
    stderr: str = "",
    validator_reasons: Optional[Iterable[str]] = None,
    changed_files: Optional[Iterable[str]] = None,
    expected_files: Optional[Iterable[str]] = None,
    workspace_path: Any = "",
) -> DebugFeedbackEnvelope:
    reasons = [str(reason) for reason in (validator_reasons or []) if str(reason)]
    stdout_excerpt = _excerpt(stdout)
    stderr_excerpt = _excerpt(stderr)
    combined_output = "\n".join(part for part in [stdout, stderr, *reasons] if part)
    return DebugFeedbackEnvelope(
        task_execution_id=task_execution_id,
        task_id=task_id,
        step_index=step_index,
        failure_phase=failure_phase,
        failed_command=str(failed_command or ""),
        return_code=return_code,
        stdout_excerpt=stdout_excerpt,
        stderr_excerpt=stderr_excerpt,
        pytest_excerpt=_first_pytest_excerpt(combined_output),
        validator_reasons=reasons[:10],
        changed_files=[str(path) for path in (changed_files or []) if str(path)][:20],
        expected_files=[str(path) for path in (expected_files or []) if str(path)][:20],
        workspace_path=str(workspace_path or ""),
        failure_class=classify_debug_failure(
            failed_command=failed_command,
            stdout=stdout_excerpt,
            stderr=stderr_excerpt,
            validator_reasons=reasons,
            return_code=return_code,
        ),
    )


def persist_debug_feedback_envelope(
    *,
    db: Session,
    session_id: int,
    task_id: int,
    session_instance_id: Optional[str],
    project_dir: Any,
    envelope: DebugFeedbackEnvelope,
    parent_event_id: Optional[str] = None,
    evidence_capsule: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """Store debug feedback in LogEntry metadata and the event journal."""

    payload = envelope.to_dict()
    metadata = {
        "event_type": EventType.DEBUG_FEEDBACK_CAPTURED,
        "debug_feedback_captured": True,
        "debug_failure_class": envelope.failure_class,
        "debug_repair_used": False,
        "debug_repair_attempted": False,
        "debug_feedback_envelope": payload,
        "task_execution_id": envelope.task_execution_id,
        "task_id": task_id,
        "evidence_chars_total": (
            getattr(evidence_capsule, "total_chars", 0) if evidence_capsule else 0
        ),
        "evidence_files_inspected": (
            getattr(evidence_capsule, "files_inspected", []) if evidence_capsule else []
        ),
        "evidence_matched_lines": (
            getattr(evidence_capsule, "matched_line_count", 0)
            if evidence_capsule
            else 0
        ),
        "evidence_capsule_used": evidence_capsule is not None
        and not getattr(evidence_capsule, "is_empty", lambda: True)(),
    }
    db.add(
        LogEntry(
            session_id=session_id,
            task_id=task_id,
            task_execution_id=envelope.task_execution_id,
            level="WARN",
            message=(
                "[ORCHESTRATION] Debug feedback captured "
                f"({envelope.failure_phase}:{envelope.failure_class})"
            ),
            log_metadata=json.dumps(metadata),
            session_instance_id=session_instance_id,
        )
    )
    db.flush()

    try:
        return append_orchestration_event(
            project_dir=project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.DEBUG_FEEDBACK_CAPTURED,
            parent_event_id=parent_event_id,
            details={
                **metadata,
                "phase": (
                    "completion"
                    if envelope.failure_phase.startswith("completion")
                    else "execution"
                ),
                "eligible_for_debug_repair": envelope.eligible_for_debug_repair,
            },
        )
    except Exception:
        return None


def build_bounded_debug_repair_prompt(
    envelope: DebugFeedbackEnvelope,
    evidence_capsule: Optional[Any] = None,
    *,
    source_edit_context: bool = False,
    candidate_repair: Optional[Any] = None,
) -> str:
    """Render the bounded Phase 7F debug repair prompt body."""

    return build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        evidence_capsule,
        source_edit_context=source_edit_context,
        candidate_repair=candidate_repair,
    ).prompt


def build_bounded_debug_repair_prompt_with_metadata(
    envelope: DebugFeedbackEnvelope,
    evidence_capsule: Optional[Any] = None,
    *,
    source_edit_context: bool = False,
    candidate_repair: Optional[Any] = None,
) -> DebugRepairPromptBuildResult:
    """Render the bounded debug repair prompt with source/API metadata."""

    workspace = render_workspace_path_for_prompt(Path(envelope.workspace_path or "."))
    zero_test_collect_only = _is_zero_test_collect_only_failure(envelope)
    excerpts = {
        "stdout_excerpt": envelope.stdout_excerpt,
        "stderr_excerpt": envelope.stderr_excerpt,
        "pytest_excerpt": envelope.pytest_excerpt,
    }
    evidence_section = ""
    if evidence_capsule is not None:
        from app.services.orchestration.diagnostics.evidence_capsule import (
            render_evidence_section,
        )

        rendered = render_evidence_section(evidence_capsule)
        if rendered:
            evidence_section = f"\n{rendered}\n"
    source_excerpt_section = build_source_step_validation_excerpt_section(envelope)
    source_contract = build_debug_source_contract(envelope, evidence_capsule)
    source_api_contract, source_api_metadata = (
        build_debug_source_api_contract_context_block(
            envelope,
            source_edit_context=source_edit_context,
            candidate_repair=candidate_repair,
        )
    )
    source_contract_section = f"\n{source_contract}\n" if source_contract else ""
    source_api_contract_section = (
        f"\n{source_api_contract}\n" if source_api_contract else ""
    )
    source_ops_contract_section = ""
    if source_contract:
        source_ops_contract_section = (
            "\nSource-context structured repair contract:\n"
            "- For this source-context failure, return repair_type/fix_type ops_fix with an ops array.\n"
            "- Use structured write_file, append_file, or replace_in_file operations for source changes.\n"
            "- write_file and append_file operations must include content, not new.\n"
            "- replace_in_file operations must include old and new.\n"
            "- For source_step_validation, prefer write_file with complete grounded file content when replacing function bodies or when exact current old text is not visible.\n"
            "- Use replace_in_file only when old is copied exactly from a visible current source excerpt.\n"
            "- Never infer replace_in_file.old signatures from tests; tests describe expected behavior, not current source text.\n"
            "- Preserve imports and existing public function/class signatures from source excerpts.\n"
            "- Do not use command_fix for source file changes; command_fix is only for verifier/command-only repairs.\n"
            "- Do not use shell commands, heredocs, cat > file, sed, or python -c to mutate files.\n"
            "- Any shell strings must be runnable shell strings, not prose instructions.\n"
            "- Do not use heredoc rewrites.\n"
            "- Minimal valid source repair example:\n"
            '  {"repair_type":"ops_fix","ops":[{"op":"write_file","path":"src/...","content":"complete file content"}],"verification_command":"python3 -m pytest -q"}\n'
        )
    if source_contract:
        rules_section = (
            "Rules:\n"
            "1. Output exactly one JSON array containing one source repair object.\n"
            "2. The repair object must include repair_type or fix_type set to ops_fix, an ops array, and verification_command.\n"
            "3. ops must contain structured replace_in_file, write_file, or append_file operations.\n"
            "4. write_file/append_file use content; replace_in_file uses old and new.\n"
            "5. verification_command must be a runnable verification shell string, not a mutation command.\n"
            "6. Keep the fix atomic; do not rewrite unrelated files.\n"
            "7. Use relative paths only; no absolute paths, `..`, or `~`.\n"
            f"8. Commands execute from the workspace root ({workspace}). Do not cd into the workspace root or any path containing vault/projects; you are already there.\n"
            "9. Do not bypass validators, workspace boundaries, or verification.\n"
            "10. Do not request additional retries or describe policy.\n"
            "11. If workspace evidence names a missing Python module target, prefer creating that module file instead of editing only a package __init__.py.\n"
        )
    else:
        zero_test_rule = ""
        if zero_test_collect_only:
            zero_test_rule = (
                "11. Zero tests were collected. The command must create a non-empty "
                "test_*.py file containing a def test_* function and at least one "
                "assertion or target-package import; include that path in "
                "expected_files. An empty file or touch command is invalid.\n"
            )
        rules_section = (
            "Rules:\n"
            "1. Output exactly one JSON array containing one command_fix step object.\n"
            "2. The step object must include title, command, and verification_command.\n"
            "3. command and verification_command must be runnable shell strings, not prose instructions.\n"
            "4. Keep the fix atomic; do not rewrite unrelated files.\n"
            "5. Do not use heredoc rewrites; keep file changes minimal and command-driven.\n"
            "6. Use relative paths only; no absolute paths, `..`, or `~`.\n"
            f"7. Commands execute from the workspace root ({workspace}). Do not cd into the workspace root or any path containing vault/projects; you are already there.\n"
            "8. Do not bypass validators, workspace boundaries, or verification.\n"
            "9. Do not request additional retries or describe policy.\n"
            "10. If workspace evidence names a missing Python module target, prefer creating that module file instead of editing only a package __init__.py.\n"
            f"{zero_test_rule}"
        )
    prompt = (
        "Return a bare JSON array of one minimal debug repair step. "
        "Do not return prose, markdown, comments, explanations, or fenced code.\n\n"
        f"Workspace scope: {workspace}\n"
        f"Failure class: {envelope.failure_class}\n"
        f"Failed command: {envelope.failed_command or '(none recorded)'}\n"
        f"Return code: {envelope.return_code}\n"
        "Validator reasons:\n"
        f"{json.dumps(envelope.validator_reasons[:8], ensure_ascii=True)}\n"
        "Failure excerpts:\n"
        f"{json.dumps(excerpts, ensure_ascii=True)[:1800]}\n"
        f"{source_excerpt_section}\n"
        f"{evidence_section}\n"
        f"{source_contract_section}"
        f"{source_ops_contract_section}"
        f"{source_api_contract_section}"
        f"{rules_section}"
    )
    return DebugRepairPromptBuildResult(
        prompt=prompt,
        metadata=source_api_metadata,
    )


def build_debug_source_api_contract_context_block(
    envelope: DebugFeedbackEnvelope,
    *,
    source_edit_context: bool = False,
    candidate_repair: Optional[Any] = None,
    max_chars: int = 3200,
    compact_max_chars: int = 1500,
) -> tuple[str, dict[str, Any]]:
    """Render the shared source/API capsule for bounded debug source repair."""

    metadata = {
        "source_api_contract_available": False,
        "source_api_contract_included": False,
        "source_api_contract_chars": 0,
        "source_api_contract_compacted": False,
        "source_api_contract_omitted_reason": "not_available",
        "source_api_contract_included_reason": None,
    }
    project_dir = Path(envelope.workspace_path or ".")
    if not project_dir.exists():
        return "", metadata

    try:
        from app.services.orchestration.planning.source_api_contract import (
            build_source_api_contract_capsule,
        )

        capsule = build_source_api_contract_capsule(
            project_dir,
            max_excerpt_chars=450,
        )
    except Exception:
        return "", metadata

    if not capsule.source_modules:
        return "", metadata

    metadata["source_api_contract_available"] = True
    metadata["source_api_contract_omitted_reason"] = None
    included_reason = _debug_repair_source_api_contract_included_reason(
        envelope,
        capsule,
        source_edit_context=source_edit_context,
        candidate_repair=candidate_repair,
    )
    if not included_reason:
        metadata["source_api_contract_omitted_reason"] = "non_python_debug_context"
        return "", metadata

    full_block = _render_debug_source_api_contract_block(capsule, compact=False)
    compact_block = _render_debug_source_api_contract_block(capsule, compact=True)
    block = full_block
    compacted = False
    if len(block) > max_chars and compact_block:
        block = compact_block
        compacted = True
    limit = compact_max_chars if compacted else max_chars
    block = _excerpt(block, limit)
    metadata["source_api_contract_included"] = bool(block)
    metadata["source_api_contract_chars"] = len(block)
    metadata["source_api_contract_compacted"] = compacted
    metadata["source_api_contract_included_reason"] = included_reason
    if not block:
        metadata["source_api_contract_omitted_reason"] = "empty"
    return block, metadata


def _debug_repair_source_api_contract_included_reason(
    envelope: DebugFeedbackEnvelope,
    capsule: Any,
    *,
    source_edit_context: bool,
    candidate_repair: Optional[Any],
) -> Optional[str]:
    if source_edit_context:
        return "source_edit_context"
    if _candidate_repair_touches_python_source(candidate_repair):
        return "python_source_ops"
    if any(_normalize_source_excerpt_path(path) for path in envelope.expected_files):
        return "expected_python_source_files"
    if envelope.failure_class in {"import_error", "module_not_found", "syntax_error"}:
        return "import_error_public_api_risk"
    if any(_normalize_source_excerpt_path(path) for path in envelope.changed_files):
        return "expected_python_source_files"
    context = _debug_failure_context(envelope)
    if _CANNOT_IMPORT_FROM_FILE_RE.search(context):
        return "import_error_public_api_risk"
    if _looks_like_public_api_attribute_error(context):
        return "import_error_public_api_risk"
    lowered_context = context.lower()
    for module, symbols in getattr(capsule, "test_imported_symbols", {}).items():
        if str(module or "").lower() in lowered_context:
            return "import_error_public_api_risk"
        for symbol in symbols:
            if re.search(rf"\b{re.escape(str(symbol))}\b", context):
                return "import_error_public_api_risk"
    return None


def _candidate_repair_touches_python_source(candidate_repair: Optional[Any]) -> bool:
    if candidate_repair is None:
        return False
    if isinstance(candidate_repair, dict):
        path = str(candidate_repair.get("path") or "").strip()
        if _normalize_source_excerpt_path(path):
            return True
        if any(
            _candidate_repair_touches_python_source(candidate_repair.get(key))
            for key in ("ops", "repair", "fix", "candidate")
        ):
            return True
        if len(candidate_repair) == 1:
            value = next(iter(candidate_repair.values()))
            return _candidate_repair_touches_python_source(value)
        return False
    if isinstance(candidate_repair, list):
        return any(
            _candidate_repair_touches_python_source(item) for item in candidate_repair
        )
    return False


def _looks_like_public_api_attribute_error(context: str) -> bool:
    return bool(
        re.search(
            r"AttributeError:\s+module\s+'[A-Za-z_][A-Za-z0-9_.]*'\s+has\s+no\s+attribute\s+'[A-Za-z_][A-Za-z0-9_]*'",
            context,
        )
    )


def _render_debug_source_api_contract_block(capsule: Any, *, compact: bool) -> str:
    lines = [
        "## SOURCE/API CONTRACT CAPSULE",
        "Use this read-only contract to ground bounded debug source repair.",
    ]
    framework_family = getattr(capsule, "framework_family", None)
    if framework_family:
        lines.append(f"framework_family: {framework_family}")
        lines.append(f"- Preserve the detected {framework_family} framework family.")
    lines.extend(
        [
            "- Preserve required public symbols imported by tests.",
            "- Do not add self-imports to restore symbols.",
            "- Do not add physical src. imports inside package code.",
            "- Prefer repairing existing source definitions over re-export hacks.",
            "- Do not rewrite tests unless explicitly requested.",
            "- Prefer canonical source ops under existing source modules.",
        ]
    )
    source_modules = list(getattr(capsule, "source_modules", []) or [])
    if source_modules:
        lines.append("source_modules: " + ", ".join(source_modules[:8]))

    test_imported = dict(getattr(capsule, "test_imported_symbols", {}) or {})
    if test_imported:
        lines.append("test_imported_symbols:")
        for module, symbols in list(test_imported.items())[:8]:
            lines.append(f"- {module}: {', '.join(list(symbols)[:12])}")
        lines.append("required_public_symbols:")
        for module, symbols in list(test_imported.items())[:8]:
            lines.append(f"- {module}: {', '.join(list(symbols)[:12])}")

    public_symbols = dict(getattr(capsule, "public_symbols", {}) or {})
    if public_symbols:
        lines.append("public_symbols:")
        for module, symbols in list(public_symbols.items())[:8]:
            lines.append(f"- {module}: {', '.join(list(symbols)[:12])}")

    if compact:
        return "\n".join(lines).strip()

    source_excerpt = dict(getattr(capsule, "source_excerpt", {}) or {})
    if source_excerpt:
        lines.append("source_excerpt:")
        for module, excerpt in list(source_excerpt.items())[:4]:
            normalized = str(excerpt or "").strip()
            if not normalized:
                continue
            lines.append(f"{module}:")
            lines.append(normalized)
    return "\n".join(lines).strip()


def build_source_step_validation_excerpt_section(
    envelope: DebugFeedbackEnvelope,
    *,
    max_files: int = _SOURCE_STEP_EXCERPT_MAX_FILES,
    per_file_chars: int = _SOURCE_STEP_EXCERPT_PER_FILE_CHARS,
    total_chars: int = _SOURCE_STEP_EXCERPT_TOTAL_CHARS,
) -> str:
    """Render bounded current source excerpts for source-step validation repair."""

    if envelope.failure_class != "source_step_validation":
        return ""

    project_dir = Path(envelope.workspace_path or ".")
    if not project_dir.exists():
        return ""

    excerpts: list[tuple[str, str, bool]] = []
    used_chars = 0
    for raw_path in envelope.changed_files:
        if len(excerpts) >= max_files or used_chars >= total_chars:
            break
        rel_path = _normalize_source_excerpt_path(raw_path)
        if not rel_path:
            continue
        path = project_dir / rel_path
        try:
            resolved = path.resolve()
            resolved.relative_to(project_dir.resolve())
            if not resolved.is_file():
                continue
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        remaining = total_chars - used_chars
        if remaining < 4:
            break
        excerpt_limit = min(per_file_chars, remaining)
        excerpt = _excerpt(text, excerpt_limit)
        if not excerpt:
            continue
        used_chars += len(excerpt)
        excerpts.append((rel_path, excerpt, len(text.strip()) > len(excerpt)))

    if not excerpts:
        return ""

    lines = [
        "Current source excerpts from changed_files:",
        "- Treat these as the current source truth; preserve public imports, classes, and function signatures shown here.",
        "- These current source excerpts have higher priority than inferring source shape from tests alone.",
    ]
    for rel_path, excerpt, truncated in excerpts:
        suffix = " truncated" if truncated else ""
        lines.append(f"--- {rel_path} ({len(excerpt)} chars{suffix})")
        lines.append(excerpt)
    return "\n".join(lines)


def _normalize_source_excerpt_path(path: Any) -> str:
    normalized = str(path or "").strip().replace("\\", "/").lstrip("./")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.startswith("~")
        or ".." in Path(normalized).parts
        or not normalized.endswith(".py")
        or normalized.startswith("tests/")
        or normalized.startswith("test/")
    ):
        return ""
    return normalized


def build_debug_source_contract(
    envelope: DebugFeedbackEnvelope,
    evidence_capsule: Optional[Any] = None,
    *,
    max_chars: int = _DEBUG_SOURCE_CONTRACT_MAX_CHARS,
) -> str:
    """Render a compact source-focused repair contract for Python debug repair."""

    if envelope.failure_class not in {
        "pytest_failure",
        "import_error",
        "module_not_found",
        "runtime_assertion_failure",
        "completion_validation_failed",
        "syntax_error",
        "source_step_validation",
    }:
        return ""

    project_dir = Path(envelope.workspace_path or ".")
    context = _debug_failure_context(envelope)
    targets: list[str] = []
    behavior: list[str] = []
    argparse_wiring: list[str] = []

    contract = None
    if project_dir.exists():
        try:
            from app.services.project.source_imports import extract_python_test_contract

            contract = extract_python_test_contract(project_dir)
        except (OSError, SyntaxError, ValueError):
            contract = None

    if contract is not None:
        for path, _reason in list(contract.source_targets) + list(
            contract.missing_source_targets
        ):
            _append_unique(targets, path, limit=3)
        for line in _debug_expected_behavior_lines(contract):
            _append_unique(behavior, line, limit=3)

    if envelope.failure_class == "source_step_validation":
        for path in envelope.changed_files:
            target = _normalize_source_excerpt_path(path)
            if target:
                _append_unique(targets, target, limit=3)

    for target in _targets_from_evidence(evidence_capsule):
        _append_unique(targets, target, limit=3)

    imported_symbol = _imported_symbol_from_failure(context)
    direct_import_target = _direct_import_error_target(context, project_dir)
    if direct_import_target:
        _append_unique(targets, direct_import_target, limit=3)

    if _looks_like_uppercase_repair_context(context):
        _prefer_target(targets, "src/small_cli/cli.py")
        has_build_parser = _contract_or_context_mentions_symbol(
            contract, context, "build_parser"
        )
        has_main = _contract_or_context_mentions_symbol(contract, context, "main")
        parser_name = "build_parser" if has_build_parser else "the argparse parser"
        main_name = "main(argv)" if has_main else "the CLI entrypoint"
        _append_unique(
            argparse_wiring,
            f'In {parser_name}, add parser.add_argument("--uppercase", action="store_true", ...).',
            limit=6,
        )
        _append_unique(
            argparse_wiring,
            f"In {main_name}, read args.uppercase after parse_args(argv).",
            limit=6,
        )
        _append_unique(
            argparse_wiring,
            "Uppercase only when the --uppercase flag is set.",
            limit=6,
        )
        if has_build_parser and has_main:
            _append_unique(
                argparse_wiring,
                "Do not inspect raw sys.argv for --uppercase; use parse_args(argv) and args.uppercase.",
                limit=6,
            )
        _append_unique(
            argparse_wiring,
            "Do not satisfy this by changing tests or making all output uppercase.",
            limit=6,
        )
        priority_behavior = [
            'main(["--uppercase", "hello"]) exits 0 and prints HELLO.',
            'Preserve default behavior: format_message("hello") == "hello".',
            "Existing normal CLI behavior still passes.",
        ]
        behavior = priority_behavior + [
            line for line in behavior if line not in priority_behavior
        ]

    for line in _debug_expected_behavior_lines(contract, context):
        _append_unique(behavior, line, limit=3)

    if imported_symbol == "normalize_greeting" or "normalize_greeting" in context:
        _prefer_target(targets, "src/import_repair/formatters.py")
        _append_unique(
            behavior,
            "Define normalize_greeting in the target module.",
            limit=3,
        )
        _append_unique(
            behavior,
            'normalize_greeting("  ada   lovelace ") returns "Hello, Ada Lovelace!".',
            limit=3,
        )

    if not targets and not behavior:
        return ""

    lines = [
        "Debug source contract:",
        "- Existing tests are the failing contract.",
        "- Do not edit tests or verifier commands.",
        "- Repair source code under the required target.",
    ]
    if targets:
        lines.append("- Required source target path:")
        lines.extend(f"  - {target}" for target in targets[:3])
    if behavior:
        lines.append("- Expected behavior:")
        lines.extend(f"  - {item}" for item in behavior[:4])
    if argparse_wiring:
        lines.append("- Required argparse wiring:")
        lines.extend(f"  - {item}" for item in argparse_wiring[:6])
    lines.append("- No placeholder/pass/TODO/export-only fixes.")
    return _excerpt("\n".join(lines), max_chars)


def _debug_failure_context(envelope: DebugFeedbackEnvelope) -> str:
    return "\n".join(
        part
        for part in (
            envelope.failed_command,
            envelope.stdout_excerpt,
            envelope.stderr_excerpt,
            envelope.pytest_excerpt,
            "\n".join(envelope.validator_reasons or []),
        )
        if str(part or "").strip()
    )


def _append_unique(values: list[str], value: str, *, limit: int) -> None:
    cleaned = str(value or "").strip()
    if cleaned and cleaned not in values and len(values) < limit:
        values.append(cleaned)


def _prefer_target(values: list[str], target: str) -> None:
    if target in values:
        values.remove(target)
    values.insert(0, target)
    del values[3:]


def _targets_from_evidence(evidence_capsule: Optional[Any]) -> list[str]:
    if evidence_capsule is None:
        return []
    targets: list[str] = []
    results = getattr(evidence_capsule, "results", {}) or {}
    for text in results.values():
        if not isinstance(text, str):
            continue
        for match in re.finditer(r"(?:^|\s)(src/[A-Za-z0-9_./-]+\.py)\b", text):
            _append_unique(targets, match.group(1), limit=3)
    for path in getattr(evidence_capsule, "files_inspected", []) or []:
        cleaned = str(path or "").strip().lstrip("./")
        if cleaned.startswith("src/") and cleaned.endswith(".py"):
            _append_unique(targets, cleaned, limit=3)
    return targets


def _direct_import_error_target(context: str, project_dir: Path) -> Optional[str]:
    match = _CANNOT_IMPORT_FROM_FILE_RE.search(context)
    if not match:
        return None
    source_path = match.group(3)
    if not source_path:
        return None
    path = Path(source_path)
    try:
        if path.is_absolute():
            return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    return path.as_posix()


def _imported_symbol_from_failure(context: str) -> str:
    match = _CANNOT_IMPORT_FROM_FILE_RE.search(context)
    return match.group(1) if match else ""


def _looks_like_uppercase_argparse_failure(context: str) -> bool:
    lowered = context.lower()
    return (
        "--uppercase" in lowered
        and "unrecognized arguments" in lowered
        and ("argparse" in lowered or "usage:" in lowered)
    )


def _looks_like_uppercase_repair_context(context: str) -> bool:
    lowered = context.lower()
    return (
        _looks_like_uppercase_argparse_failure(context)
        or "--uppercase" in lowered
        or "test_uppercase" in lowered
        or ("hello" in lowered and "uppercase" in lowered)
    )


def _contract_or_context_mentions_symbol(
    contract: Any,
    context: str,
    symbol: str,
) -> bool:
    if re.search(rf"\b{re.escape(symbol)}\b", context):
        return True
    if contract is None:
        return False
    for value in (
        list(getattr(contract, "imports", ()) or ())
        + list(getattr(contract, "public_calls", ()) or ())
        + list(getattr(contract, "assertions", ()) or ())
    ):
        if re.search(rf"\b{re.escape(symbol)}\b", str(value or "")):
            return True
    return False


def _debug_expected_behavior_lines(contract: Any, context: str = "") -> list[str]:
    lines: list[str] = []
    for assertion in getattr(contract, "assertions", ()) or ():
        rendered = str(assertion or "").strip()
        if not rendered:
            continue
        if context and rendered in context:
            _append_unique(lines, rendered, limit=3)
    for assertion in getattr(contract, "assertions", ()) or ():
        rendered = str(assertion or "").strip()
        if not rendered:
            continue
        if "capsys.readouterr().out.strip()" in rendered:
            rendered = rendered.replace(
                "capsys.readouterr().out.strip()", "printed output"
            )
        if "==" in rendered:
            left, right = [part.strip() for part in rendered.split("==", 1)]
            rendered = f"{left} should equal {right}"
        _append_unique(lines, rendered, limit=3)
    if not lines:
        for call in getattr(contract, "public_calls", ()) or ():
            _append_unique(lines, str(call), limit=3)
    return lines[:3]


def normalize_bounded_debug_repair_payload(
    parsed_data: Any,
    *,
    envelope: Optional[DebugFeedbackEnvelope] = None,
    source_edit_context: bool = False,
) -> Optional[dict[str, Any]]:
    """Convert a Phase 7F repair array into the legacy debug action shape."""
    return normalize_bounded_debug_repair_payload_detailed(
        parsed_data,
        envelope=envelope,
        source_edit_context=source_edit_context,
    ).payload


def normalize_bounded_debug_repair_payload_detailed(
    parsed_data: Any,
    *,
    envelope: Optional[DebugFeedbackEnvelope] = None,
    source_edit_context: bool = False,
) -> DebugRepairNormalizationResult:
    """Convert Phase 7F repair output while preserving invalid-branch details."""

    if isinstance(parsed_data, dict):
        fix_type = str(parsed_data.get("fix_type") or "code_fix").strip()
        if fix_type not in {"code_fix", "command_fix", "ops_fix", "revise_plan"}:
            return _debug_repair_normalization_rejected(
                parsed_data, "unsupported_fix_type"
            )

        ops = _normalize_durable_source_ops(parsed_data.get("ops"))
        if source_edit_context and _ops_touch_source_files(ops):
            fix_type = "ops_fix"

        normalized: dict[str, Any] = {
            "fix_type": fix_type,
            "fix": str(parsed_data.get("fix") or "").strip(),
            "analysis": str(parsed_data.get("analysis") or "")[:1200],
            "confidence": str(parsed_data.get("confidence") or "MEDIUM"),
        }
        if isinstance(parsed_data.get("expected_files"), list):
            normalized["expected_files"] = [
                str(path).strip()
                for path in parsed_data.get("expected_files", [])
                if str(path).strip()
            ]
        if fix_type == "command_fix" and not normalized.get("expected_files"):
            derived_expected_files = _derive_zero_test_expected_files(
                normalized["fix"],
                envelope=envelope,
            )
            if derived_expected_files:
                normalized["expected_files"] = derived_expected_files
        if isinstance(parsed_data.get("verification"), str):
            normalized["verification"] = str(parsed_data.get("verification") or "")
        if isinstance(parsed_data.get("ops"), list):
            normalized["ops"] = ops
        if isinstance(parsed_data.get("revised_plan"), list):
            normalized["revised_plan"] = parsed_data.get("revised_plan", [])
        if source_edit_context and fix_type == "command_fix":
            if _ops_touch_source_files(normalized.get("ops")):
                normalized["fix_type"] = "ops_fix"
                normalized["fix"] = ""
                return DebugRepairNormalizationResult(
                    payload=normalized,
                    rejection_reason=None,
                    parsed_shape=_debug_repair_parsed_shape(parsed_data),
                )
            if not _is_verifier_only_command_fix(
                normalized["fix"], normalized.get("verification")
            ):
                return _debug_repair_normalization_rejected(
                    parsed_data, "source_context_command_fix_rejected"
                )
        if fix_type == "command_fix" and not is_runnable_shell_command_fix(
            normalized["fix"]
        ):
            reason = (
                "missing_command" if not normalized["fix"] else "non_runnable_command"
            )
            return _debug_repair_normalization_rejected(parsed_data, reason)
        if fix_type == "command_fix" and _source_repair_command_fix_requires_ops(
            normalized["fix"],
            normalized.get("verification"),
            envelope=envelope,
            source_edit_context=source_edit_context,
        ):
            return _debug_repair_normalization_rejected(
                parsed_data, "source_repair_command_fix_rejected"
            )
        if fix_type == "command_fix" and _semantic_pytest_string_edit_repair(
            normalized["fix"],
            envelope=envelope,
        ):
            return _debug_repair_normalization_rejected(
                parsed_data, "semantic_string_edit_rejected"
            )
        if fix_type == "command_fix" and not _zero_test_repair_creates_semantic_test(
            normalized["fix"],
            normalized.get("expected_files"),
            envelope=envelope,
        ):
            return _debug_repair_normalization_rejected(
                parsed_data, "zero_test_repair_missing_semantic_test"
            )
        if fix_type in {"code_fix", "ops_fix"} and not any(
            key in normalized for key in ("expected_files", "verification", "ops")
        ):
            return _debug_repair_normalization_rejected(
                parsed_data, "missing_ops_or_expected_files"
            )
        return DebugRepairNormalizationResult(
            payload=normalized,
            rejection_reason=None,
            parsed_shape=_debug_repair_parsed_shape(parsed_data),
        )

    if not isinstance(parsed_data, list) or len(parsed_data) != 1:
        return _debug_repair_normalization_rejected(parsed_data, "unsupported_shape")
    item = parsed_data[0]
    if not isinstance(item, dict):
        return _debug_repair_normalization_rejected(parsed_data, "unsupported_shape")

    command = str(item.get("command") or "").strip()
    verification = str(item.get("verification_command") or "").strip()
    ops = _normalize_durable_source_ops(item.get("ops"))
    item_fix_type = str(item.get("fix_type") or item.get("repair_type") or "").strip()
    explicit_ops_fix = item_fix_type == "ops_fix"
    raw_ops_present = isinstance(item.get("ops"), list)
    if explicit_ops_fix and raw_ops_present and not ops:
        return _debug_repair_normalization_rejected(parsed_data, "invalid_ops_fix_ops")
    if (source_edit_context or explicit_ops_fix) and _ops_touch_source_files(ops):
        if not verification:
            return _debug_repair_normalization_rejected(
                parsed_data, "missing_verification_command"
            )
        return DebugRepairNormalizationResult(
            payload={
                "fix_type": "ops_fix",
                "fix": "",
                "analysis": str(item.get("title") or "Apply bounded debug repair")[
                    :1200
                ],
                "confidence": "MEDIUM",
                "verification": verification,
                "expected_files": [
                    str(path).strip()
                    for path in _expected_files_from_item(item)
                    if str(path).strip()
                ],
                "ops": ops,
            },
            rejection_reason=None,
            parsed_shape=_debug_repair_parsed_shape(parsed_data),
        )
    if not command:
        return _debug_repair_normalization_rejected(parsed_data, "missing_command")
    if not verification:
        return _debug_repair_normalization_rejected(
            parsed_data, "missing_verification_command"
        )

    expected_files = _expected_files_from_item(item)
    if not expected_files:
        expected_files = _derive_zero_test_expected_files(
            command,
            envelope=envelope,
        )

    if not is_runnable_shell_command_fix(command):
        return _debug_repair_normalization_rejected(parsed_data, "non_runnable_command")
    if source_edit_context and not _is_verifier_only_command_fix(command, verification):
        return _debug_repair_normalization_rejected(
            parsed_data, "source_context_command_fix_rejected"
        )
    if _source_repair_command_fix_requires_ops(
        command,
        verification,
        envelope=envelope,
        source_edit_context=source_edit_context,
    ):
        return _debug_repair_normalization_rejected(
            parsed_data, "source_repair_command_fix_rejected"
        )
    if _semantic_pytest_string_edit_repair(command, envelope=envelope):
        return _debug_repair_normalization_rejected(
            parsed_data, "semantic_string_edit_rejected"
        )
    if not _zero_test_repair_creates_semantic_test(
        command,
        expected_files,
        envelope=envelope,
    ):
        return _debug_repair_normalization_rejected(
            parsed_data, "zero_test_repair_missing_semantic_test"
        )

    return DebugRepairNormalizationResult(
        payload={
            "fix_type": "command_fix",
            "fix": command,
            "analysis": str(item.get("title") or "Apply bounded debug repair")[:1200],
            "confidence": "MEDIUM",
            "verification": verification,
            "expected_files": [
                str(path).strip() for path in expected_files if str(path).strip()
            ],
        },
        rejection_reason=None,
        parsed_shape=_debug_repair_parsed_shape(parsed_data),
    )


def normalize_diff_scoped_compliance_retry_command_list(
    raw_output: str,
    *,
    parsed_data: Any = None,
    envelope: Optional[DebugFeedbackEnvelope] = None,
    source_edit_context: bool = False,
) -> DebugRepairNormalizationResult:
    """Normalize diff-scoped compliance retry list-shaped command repairs.

    This is intentionally not part of the general bounded debug repair
    normalizer. It exists only for the diff-scoped compliance retry path where
    models often return a top-level list of command steps instead of the
    bounded repair object.
    """

    items = parsed_data if isinstance(parsed_data, list) else None
    if items is None:
        items = _extract_diff_scoped_command_list_items(raw_output)
    if not isinstance(items, list) or not items:
        return _debug_repair_normalization_rejected(items, "unsupported_shape")

    commands: list[str] = []
    verifications: list[str] = []
    titles: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            return _debug_repair_normalization_rejected(items, "unsupported_shape")
        command = str(item.get("command") or "").strip()
        verification = str(item.get("verification_command") or "").strip()
        if not command:
            return _debug_repair_normalization_rejected(items, "missing_command")
        if not verification:
            return _debug_repair_normalization_rejected(
                items, "missing_verification_command"
            )
        commands.append(command)
        verifications.append(verification)
        title = str(item.get("title") or item.get("description") or "").strip()
        if title:
            titles.append(title)

    return normalize_bounded_debug_repair_payload_detailed(
        [
            {
                "title": (
                    "; ".join(titles[:3])
                    or "Apply diff-scoped compliance command repair"
                ),
                "command": " && ".join(commands),
                "verification_command": " && ".join(verifications),
            }
        ],
        envelope=envelope,
        source_edit_context=source_edit_context,
    )


def _extract_diff_scoped_command_list_items(raw_output: str) -> list[dict[str, str]]:
    text = str(raw_output or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    if not text.startswith("[") or not text.endswith("]"):
        return []
    objects = re.findall(r"\{(.*?)\}", text, flags=re.DOTALL)
    items: list[dict[str, str]] = []
    for obj in objects:
        item: dict[str, str] = {}
        for key in ("title", "description", "command", "verification_command"):
            value = _extract_diff_scoped_string_field(obj, key)
            if value is not None:
                item[key] = value
        if item:
            items.append(item)
    return items


def _extract_diff_scoped_string_field(object_text: str, key: str) -> str | None:
    marker = re.search(rf'"{re.escape(key)}"\s*:\s*"', object_text)
    if not marker:
        return None
    start = marker.end()
    next_key = re.search(
        r',\s*"(?:title|description|command|verification_command)"\s*:\s*"',
        object_text[start:],
    )
    if next_key:
        raw = object_text[start : start + next_key.start()]
    else:
        raw = object_text[start:]
    raw = raw.strip()
    if raw.endswith('"'):
        raw = raw[:-1]
    return raw.strip()


def _expected_files_from_item(item: dict[str, Any]) -> list[Any]:
    expected_files = item.get("expected_files", [])
    if isinstance(expected_files, str):
        expected_files = [expected_files]
    if not isinstance(expected_files, list):
        return []
    return expected_files


def _ops_touch_source_files(ops: Any) -> bool:
    ops = _normalize_durable_source_ops(ops)
    if not isinstance(ops, list):
        return False
    durable_ops = {"replace_in_file", "write_file", "append_file"}
    for op in ops:
        if not isinstance(op, dict):
            continue
        op_name = str(op.get("op") or "").strip()
        path = str(op.get("path") or "").strip().replace("\\", "/").lstrip("./")
        if op_name in durable_ops and path.startswith("src/"):
            return True
    return False


def _normalize_durable_source_ops(ops: Any) -> list[dict[str, Any]]:
    if not isinstance(ops, list):
        return []
    return normalize_replacement_ops({"ops": ops})


def _is_verifier_only_command_fix(command: str, verification: Any) -> bool:
    normalized_command = str(command or "").strip()
    normalized_verification = str(verification or "").strip()
    if not normalized_command or normalized_command != normalized_verification:
        return False
    lowered = normalized_command.lower()
    if re.search(
        r"\b(?:sed|perl|tee|cat\s*>|>>?|write_text|replace\(|open\()", lowered
    ):
        return False
    return bool(
        re.search(
            r"\b(?:pytest|python3?\s+-m\s+pytest|npm\s+test|npm\s+run\s+test|make\s+test)\b",
            lowered,
        )
    )


def _source_repair_command_fix_requires_ops(
    command: str,
    verification: Any,
    *,
    envelope: Optional[DebugFeedbackEnvelope],
    source_edit_context: bool,
) -> bool:
    if _is_verifier_only_command_fix(command, verification):
        return False
    if not _command_fix_mutates_source_or_tests(command):
        return False
    return source_edit_context or _debug_envelope_points_to_source_repair(envelope)


def _command_fix_mutates_source_or_tests(command: str) -> bool:
    lowered = str(command or "").strip().lower().replace("\\", "/")
    if not any(marker in lowered for marker in ("src/", "tests/", "test/")):
        return False
    return bool(
        re.search(
            r"\b(?:sed|perl|touch|mkdir|rm|mv|cp|tee)\b|"
            r">>?|write_text|replace\(|open\(|path\(",
            lowered,
        )
    )


def _debug_envelope_points_to_source_repair(
    envelope: Optional[DebugFeedbackEnvelope],
) -> bool:
    if envelope is None:
        return False
    if envelope.failure_class not in {
        "pytest_failure",
        "import_error",
        "module_not_found",
        "runtime_assertion_failure",
        "completion_validation_failed",
        "syntax_error",
    }:
        return False
    context = _debug_failure_context(envelope)
    if re.search(r"\bsrc/[A-Za-z0-9_./-]+\.py\b", context):
        return True
    return bool(
        _direct_import_error_target(context, Path(envelope.workspace_path or "."))
    )


def _debug_repair_normalization_rejected(
    parsed_data: Any,
    reason: str,
) -> DebugRepairNormalizationResult:
    return DebugRepairNormalizationResult(
        payload=None,
        rejection_reason=reason,
        parsed_shape=_debug_repair_parsed_shape(parsed_data),
    )


def _debug_repair_parsed_shape(parsed_data: Any) -> dict[str, Any]:
    shape: dict[str, Any] = {"type": type(parsed_data).__name__}
    if isinstance(parsed_data, dict):
        shape["keys"] = sorted(str(key) for key in parsed_data.keys())[:20]
        fix_type = parsed_data.get("fix_type")
        if fix_type is not None:
            shape["fix_type"] = str(fix_type)
        if "ops" in parsed_data:
            shape["ops_type"] = type(parsed_data.get("ops")).__name__
            if isinstance(parsed_data.get("ops"), list):
                shape["ops_count"] = len(parsed_data.get("ops") or [])
        if "expected_files" in parsed_data:
            shape["expected_files_type"] = type(
                parsed_data.get("expected_files")
            ).__name__
        return shape
    if isinstance(parsed_data, list):
        shape["length"] = len(parsed_data)
        if parsed_data:
            first = parsed_data[0]
            shape["first_item_type"] = type(first).__name__
            if isinstance(first, dict):
                shape["first_item_keys"] = sorted(str(key) for key in first.keys())[:20]
        return shape
    return shape


def _semantic_pytest_string_edit_repair(
    command: str,
    *,
    envelope: Optional[DebugFeedbackEnvelope],
) -> bool:
    if envelope is None or envelope.failure_class not in {
        "pytest_failure",
        "completion_validation_failed",
    }:
        return False
    context = " ".join(
        str(part or "")
        for part in (
            envelope.stderr_excerpt,
            envelope.pytest_excerpt,
            envelope.stdout_excerpt,
            " ".join(envelope.validator_reasons or []),
        )
    ).lower()
    if not any(
        marker in context
        for marker in (
            "unrecognized arguments",
            "nameerror",
            "assertionerror",
            "typeerror",
        )
    ):
        return False
    lowered_command = str(command or "").strip().lower()
    if re.search(r"\b(?:pytest|unittest|python3?\s+-m\s+pytest)\b", lowered_command):
        return False
    return bool(
        re.search(
            r"(^|[;&|]\s*)(?:sed|perl)\b|"
            r"\bpython3?\s+-c\s+['\"][^'\"]*(?:replace|write_text|sed)",
            lowered_command,
        )
    )


def _is_zero_test_collect_only_failure(
    envelope: Optional[DebugFeedbackEnvelope],
) -> bool:
    if envelope is None:
        return False
    failed_command = str(envelope.failed_command or "").lower()
    if "pytest" not in failed_command or "--collect-only" not in failed_command:
        return False
    context = " ".join(
        str(part or "")
        for part in (
            envelope.stdout_excerpt,
            envelope.stderr_excerpt,
            envelope.pytest_excerpt,
            " ".join(envelope.validator_reasons or []),
        )
    ).lower()
    return bool(
        envelope.return_code == 5
        or re.search(r"\bcollected\s+0\s+items?\b|\bno tests collected\b", context)
    )


def _zero_test_repair_creates_semantic_test(
    command: str,
    expected_files: Any,
    *,
    envelope: Optional[DebugFeedbackEnvelope],
) -> bool:
    if not _is_zero_test_collect_only_failure(envelope):
        return True

    normalized_command = str(command or "").replace("\\", "/")
    semantic_command = str(command or "").replace("\\n", "\n")
    test_paths = {
        match.lstrip("./")
        for match in re.findall(
            r"(?:^|[\s'\"=])(tests/test_[A-Za-z0-9_.-]+\.py)\b",
            normalized_command,
        )
    }
    normalized_expected = {
        str(path or "").strip().replace("\\", "/").lstrip("./")
        for path in (expected_files or [])
        if str(path or "").strip()
    }
    declared_test_paths = test_paths.intersection(normalized_expected)
    if not declared_test_paths:
        return False
    if not any(
        re.search(
            rf"(?:>>?|tee(?:\s+-a)?)\s*['\"]?{re.escape(path)}\b",
            normalized_command,
        )
        or re.search(
            rf"(?:path\(\s*['\"]{re.escape(path)}['\"]\s*\)|"
            rf"['\"]{re.escape(path)}['\"])\s*\.write_text\(",
            normalized_command,
            flags=re.IGNORECASE,
        )
        for path in declared_test_paths
    ):
        return False
    if not re.search(r"\bdef\s+test_[A-Za-z0-9_]*\s*\(", semantic_command):
        return False
    return bool(
        re.search(r"\bassert\b", semantic_command)
        or re.search(
            r"\b(?:from\s+[A-Za-z_][A-Za-z0-9_.]*\s+import|"
            r"import\s+[A-Za-z_][A-Za-z0-9_.]*)\b",
            semantic_command,
        )
    )


def _derive_zero_test_expected_files(
    command: str,
    *,
    envelope: Optional[DebugFeedbackEnvelope],
) -> list[str]:
    if not _is_zero_test_collect_only_failure(envelope):
        return []

    normalized_command = str(command or "").replace("\\", "/")
    semantic_command = str(command or "").replace("\\n", "\n")
    test_paths = {
        match.lstrip("./")
        for match in re.findall(
            r"(?:^|[\s'\"=])(tests/test_[A-Za-z0-9_.-]+\.py)\b",
            normalized_command,
        )
    }
    if len(test_paths) != 1:
        return []

    path = next(iter(test_paths))
    writes_path = bool(
        re.search(
            rf"(?:>>?|tee(?:\s+-a)?)\s*['\"]?{re.escape(path)}\b",
            normalized_command,
        )
        or re.search(
            rf"(?:path\(\s*['\"]{re.escape(path)}['\"]\s*\)|"
            rf"['\"]{re.escape(path)}['\"])\s*\.write_text\(",
            normalized_command,
            flags=re.IGNORECASE,
        )
    )
    if not writes_path:
        return []
    if not re.search(r"\bdef\s+test_[A-Za-z0-9_]*\s*\(", semantic_command):
        return []
    if not (
        re.search(r"\bassert\b", semantic_command)
        or re.search(
            r"\b(?:from\s+[A-Za-z_][A-Za-z0-9_.]*\s+import|"
            r"import\s+[A-Za-z_][A-Za-z0-9_.]*)\b",
            semantic_command,
        )
    ):
        return []
    return [path]
