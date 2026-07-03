"""Phase 13B-S2: Recovery patch schema, prompt, parsing, validation, and application.

Provides all per-patch logic that execution_recovery_service.py orchestrates.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.reflection_evidence import ReflectionEvidence
from app.services.orchestration.validation.integrity import (
    check_test_preservation,
    is_python_test_path,
    scan_python_test_text,
)

# Commands the rerun_command may START with.
_ALLOWED_RERUN_PREFIXES: Tuple[str, ...] = (
    "pytest ",
    "pytest\n",
    "python -m pytest",
    "python3 -m pytest",
    "python ",
    "python3 ",
    "flake8 ",
    "mypy ",
    "ruff ",
)
# Also allow the bare word (e.g. "pytest" with no arguments).
_ALLOWED_EXACT_COMMANDS: frozenset = frozenset(
    {"pytest", "python", "python3", "flake8"}
)

# Patterns that are never allowed in a rerun command (shell-injection / destructive).
_DENIED_COMMAND_PATTERNS: Tuple[str, ...] = (
    "rm ",
    "sudo ",
    "curl ",
    "wget ",
    " | ",
    "&&",
    ";",
    "$(",
    "`",
    " >> ",
    " > ",
    "pip install",
    "npm install",
    "apt",
    "chmod",
    "chown",
)

# Path segments that must never be written by recovery.
_EXCLUDED_PATH_SEGMENTS: frozenset = frozenset(
    {
        "venv",
        ".venv",
        "node_modules",
        "__pycache__",
        ".git",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".eggs",
    }
)

_GENERATED_SUFFIXES: Tuple[str, ...] = (".pyc", ".min.js", ".min.css", "_pb2.py")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass
class RecoveryPatch:
    """A single-file patch returned by the recovery LLM."""

    patch_type: str  # replace_in_file | write_file | create_file
    path: str  # relative from project root
    old: str  # exact text to find — only for replace_in_file
    new: str  # replacement / full content
    rerun_command: str

    def content_hash(self) -> str:
        """Stable 16-char sha256 of (patch_type, path, old, new) for dedup."""
        payload = "|".join([self.patch_type, self.path, self.old, self.new])
        return (
            "patch:"
            + hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]
        )


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _supplemental_reflection_section(
    reflection_evidence: Optional[ReflectionEvidence],
) -> str:
    if reflection_evidence is None:
        return ""

    lines = [
        "Supplemental Diagnostic Context",
        "Reflection may be incorrect. Validator remains authoritative.",
        f"Reflection summary: {reflection_evidence.summary}",
    ]
    if reflection_evidence.suggested_fix:
        lines.append(f"Suggested fix: {reflection_evidence.suggested_fix}")
    if reflection_evidence.confidence:
        lines.append(f"Confidence: {reflection_evidence.confidence}")
    lines.append(f"Source: {reflection_evidence.source}")
    return "\n".join(lines) + "\n\n"


def build_recovery_prompt(
    evidence: ExecutionRecoveryEvidence,
    reflection_evidence: Optional[ReflectionEvidence] = None,
) -> str:
    changed_str = ", ".join(evidence.changed_files[:10]) or "(none)"
    symbols_block = ""
    if evidence.requested_symbols:
        symbols_block = (
            f"\nRequired symbols (must be exported): "
            f"{', '.join(evidence.requested_symbols[:5])}"
        )

    return (
        "You are fixing a specific engineering failure. "
        "Return ONLY a JSON object. No prose, no markdown, no explanation.\n\n"
        f"Failure class: {evidence.failure_class}\n"
        f"Failed command: {evidence.failed_command}\n"
        f"Exit code: {evidence.exit_code}\n\n"
        f"Traceback:\n{evidence.traceback_excerpt or '(none)'}\n\n"
        f"Stderr:\n{evidence.stderr_excerpt or '(none)'}\n\n"
        f"Changed files: {changed_str}\n"
        f"Task: {evidence.task_title}: {evidence.task_description[:300]}\n"
        f"{symbols_block}\n\n"
        f"{_supplemental_reflection_section(reflection_evidence)}"
        "Return exactly one JSON object with these fields:\n"
        '  {"patch_type": "replace_in_file" | "write_file" | "create_file",\n'
        '   "path": "<path relative to project root>",\n'
        '   "old": "<exact text to replace — for replace_in_file only>",\n'
        '   "new": "<replacement content>",\n'
        '   "rerun_command": "<focused command to verify — must start with pytest or python>"}\n\n'
        "Rules:\n"
        "- Only touch one file.\n"
        "- The path must be relative to the project root.\n"
        "- For replace_in_file: old must be the exact text to find in the file.\n"
        "- For write_file or create_file: omit old.\n"
        "- rerun_command must start with pytest, python, python3, flake8, mypy, or ruff.\n"
        "- No rm, sudo, pipes, shell operators, or multi-file rewrites."
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_recovery_patch(raw_text: str) -> Tuple[Optional[RecoveryPatch], str]:
    """Parse LLM response into a RecoveryPatch.

    Returns (patch, "") on success or (None, rejection_reason) on failure.
    """
    if not raw_text or not raw_text.strip():
        return None, "empty_response"

    # Strip markdown fences if present.
    fence_match = _JSON_FENCE_RE.search(raw_text)
    text = fence_match.group(1).strip() if fence_match else raw_text.strip()

    # Find the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None, "no_json_object"

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None, "json_parse_failed"

    if not isinstance(data, dict):
        return None, "not_a_dict"

    patch_type = str(data.get("patch_type", "")).strip()
    if patch_type not in ("replace_in_file", "write_file", "create_file"):
        return None, "invalid_patch_type"

    path = str(data.get("path", "")).strip()
    if not path:
        return None, "missing_path"

    new_content = str(data.get("new", "")).strip()
    if not new_content:
        return None, "missing_new_content"

    old_content = str(data.get("old", "")).strip() if "old" in data else ""
    if patch_type == "replace_in_file" and not old_content:
        return None, "missing_old_for_replace"

    rerun_command = str(data.get("rerun_command", "")).strip()
    if not rerun_command:
        return None, "missing_rerun_command"

    return (
        RecoveryPatch(
            patch_type=patch_type,
            path=path,
            old=old_content,
            new=new_content,
            rerun_command=rerun_command,
        ),
        "",
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_recovery_patch(
    patch: RecoveryPatch,
    evidence: ExecutionRecoveryEvidence,
    project_dir: Path,
) -> Tuple[bool, str]:
    """Validate a patch before applying it. Returns (valid, rejection_reason)."""

    # 1. Rerun command safety.
    cmd = patch.rerun_command.strip()
    if not (
        any(cmd.startswith(p) for p in _ALLOWED_RERUN_PREFIXES)
        or cmd in _ALLOWED_EXACT_COMMANDS
    ):
        return False, "disallowed_rerun_command"
    if any(pat in cmd for pat in _DENIED_COMMAND_PATTERNS):
        return False, "dangerous_rerun_command"

    # 2. Resolve patch path relative to project root.
    patch_path = Path(patch.path)
    if patch_path.is_absolute():
        try:
            patch_path = patch_path.relative_to(project_dir)
        except ValueError:
            return False, "path_outside_project"

    try:
        resolved = (project_dir / patch_path).resolve()
        project_resolved = project_dir.resolve()
        if not str(resolved).startswith(str(project_resolved)):
            return False, "path_outside_project"
    except (ValueError, RuntimeError, OSError):
        return False, "path_outside_project"

    rel_str = str(patch_path).replace("\\", "/").lstrip("./")

    # 3. Check excluded path segments.
    for part in patch_path.parts:
        if part in _EXCLUDED_PATH_SEGMENTS:
            return False, "excluded_path"
    if any(rel_str.endswith(suffix) for suffix in _GENERATED_SUFFIXES):
        return False, "generated_file"

    # 4. Scope check: path must be in changed_files OR referenced in traceback/stderr.
    changed_normalized = {
        Path(f).as_posix().lstrip("./") for f in (evidence.changed_files or [])
    }
    in_changed = rel_str in changed_normalized or any(
        rel_str.endswith(c) or (c and c.endswith(rel_str))
        for c in changed_normalized
        if c
    )

    traceback_and_stderr = (
        (evidence.traceback_excerpt or "") + "\n" + (evidence.stderr_excerpt or "")
    )
    path_name = patch_path.name
    path_stem = patch_path.stem
    in_traceback = (
        rel_str in traceback_and_stderr
        or (path_name and path_name in traceback_and_stderr)
        or (len(path_stem) > 4 and path_stem in traceback_and_stderr)
    )

    # create_file is more permissive for missing-module failures.
    if patch.patch_type == "create_file":
        if (
            not in_changed
            and not in_traceback
            and evidence.failure_class
            not in (
                "missing_requested_symbol",
                "import_error",
                "module_not_found",
                "missing_dependency",
            )
        ):
            return False, "unrelated_patch"
    elif not in_changed and not in_traceback:
        return False, "unrelated_patch"

    # 5. Test-file protection: reject if new content weakens a test file.
    if is_python_test_path(rel_str) and patch.patch_type in (
        "replace_in_file",
        "write_file",
    ):
        findings = scan_python_test_text(patch.new, rel_str)
        if findings:
            codes = {f.code for f in findings}
            if codes & {"skip_added", "test_weakened_or_removed"}:
                return False, "test_preservation_violated"

    # 6. Symbol rename guard: requested symbols must not be renamed away.
    # If a requested symbol appears in old text but not in new text, that's a rename.
    if evidence.requested_symbols:
        for sym in evidence.requested_symbols[:5]:
            if patch.old and sym in patch.old and sym not in patch.new:
                return False, "symbol_rename_detected"

    return True, ""


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def apply_recovery_patch(
    patch: RecoveryPatch,
    project_dir: Path,
) -> Tuple[bool, str, Any]:
    """Apply a recovery patch to disk.

    Returns (success, error_reason, rollback_fn).
    rollback_fn() restores the file to its pre-patch state.
    Always call rollback_fn() on failure paths.
    """
    patch_path = Path(patch.path)
    if patch_path.is_absolute():
        try:
            patch_path = patch_path.relative_to(project_dir)
        except ValueError:
            return False, "path_outside_project", _noop

    abs_path = (project_dir / patch_path).resolve()

    if patch.patch_type == "replace_in_file":
        if not abs_path.exists():
            return False, "target_file_not_found", _noop
        try:
            original = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"read_failed:{exc}", _noop
        if patch.old not in original:
            return False, "old_text_not_found", _noop
        new_content = original.replace(patch.old, patch.new, 1)
        try:
            abs_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return False, f"write_failed:{exc}", _noop

        def _rollback_replace():
            try:
                abs_path.write_text(original, encoding="utf-8")
            except Exception:
                pass

        return True, "", _rollback_replace

    elif patch.patch_type in ("write_file", "create_file"):
        if (
            patch.patch_type == "create_file"
            and abs_path.exists()
            and abs_path.stat().st_size > 0
        ):
            return False, "file_already_exists", _noop

        existed_before = abs_path.exists()
        original_content: Optional[str] = None
        if existed_before:
            try:
                original_content = abs_path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                original_content = None

        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(patch.new, encoding="utf-8")
        except OSError as exc:
            return False, f"write_failed:{exc}", _noop

        def _rollback_write():
            try:
                if original_content is not None:
                    abs_path.write_text(original_content, encoding="utf-8")
                elif abs_path.exists():
                    abs_path.unlink(missing_ok=True)
            except Exception:
                pass

        return True, "", _rollback_write

    return False, f"unknown_patch_type:{patch.patch_type}", _noop


def post_apply_test_preservation_check(
    patch: RecoveryPatch,
    project_dir: Path,
) -> Optional[str]:
    """Check test preservation after applying a patch (reads from disk).

    Returns rejection_reason string if check fails, None if OK.
    """
    rel_str = Path(patch.path).as_posix().lstrip("./")
    if not is_python_test_path(rel_str):
        return None

    if patch.patch_type in ("replace_in_file", "write_file"):
        change_set: dict = {
            "modified_files": [patch.path],
            "added_files": [],
            "deleted_files": [],
        }
    else:
        change_set = {
            "added_files": [patch.path],
            "modified_files": [],
            "deleted_files": [],
        }

    findings = check_test_preservation(change_set, project_dir)
    if findings:
        return "test_preservation_violated"
    return None


def _noop() -> None:
    pass


# ---------------------------------------------------------------------------
# Post-recovery step validation gate
# ---------------------------------------------------------------------------


def post_recovery_step_validation(
    patch_path: str,
    project_dir: Path,
) -> Tuple[bool, str]:
    """Run ValidatorService.validate_step_success scoped to the recovery-patched file.

    Uses relaxed_mode=True to avoid false rejects on verification-strength heuristics.
    Checks placeholder content and test integrity of the patched file only.
    Returns (accepted, reason).
    """
    from app.services.orchestration.validation.validator import ValidatorService

    try:
        verdict = ValidatorService.validate_step_success(
            project_dir=project_dir,
            step={"expected_files": [patch_path], "verification": ""},
            step_output="",
            missing_expected_files=[],
            tool_failures=[],
            validation_profile="implementation",
            reported_changed_files=[patch_path],
            relaxed_mode=True,
            validation_severity="standard",
        )
    except Exception as exc:
        return False, f"validator_exception:{exc}"

    if not verdict.accepted:
        return False, " | ".join(verdict.reasons[:3])
    return True, ""
