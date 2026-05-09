"""Phase 7G minimal diff repair capsule helpers."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.debug_feedback import DebugFeedbackEnvelope
from app.services.workspace.path_display import render_workspace_path_for_prompt

DIFF_LINE_LIMIT = 120
DIFF_REPAIR_FAILURE_CLASSES = frozenset(
    {"pytest_failure", "runtime_assertion_failure", "syntax_error", "import_error"}
)


@dataclass
class DiffCapsule:
    primary_file: str
    diff_text: str
    failure_line: str
    failure_class: str
    changed_file_count: int
    workspace_path: str = ""
    schema_version: int = 1

    @property
    def diff_line_count(self) -> int:
        return len(self.diff_text.splitlines())


def snapshot_file_contents(
    project_dir: Path,
    files: List[str],
    max_bytes_per_file: int = 32_000,
) -> Dict[str, str]:
    """Snapshot existing UTF-8 file contents for declared expected files only."""

    snapshots: Dict[str, str] = {}
    for raw_path in files or []:
        rel_path = str(raw_path or "").strip().lstrip("./")
        if not rel_path or rel_path.startswith("../"):
            continue
        path = (project_dir / rel_path).resolve()
        try:
            if not path.is_relative_to(project_dir.resolve()):
                continue
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if len(raw) > max_bytes_per_file:
                raw = raw[:max_bytes_per_file]
            snapshots[rel_path] = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return snapshots


def _failure_line(envelope: DebugFeedbackEnvelope) -> str:
    combined = "\n".join(
        part
        for part in [
            envelope.stderr_excerpt,
            envelope.pytest_excerpt,
            *envelope.validator_reasons,
        ]
        if part
    )
    for line in combined.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if not stripped:
            continue
        if (
            "error" in lowered
            or "assert" in lowered
            or "failed" in lowered
            or re.search(r"(?:line\s+\d+|:\d+:)", stripped)
        ):
            return stripped[:500]
    return combined.splitlines()[0].strip()[:500] if combined.splitlines() else ""


def _select_primary_file(changed_files: List[str], failure_line: str) -> Optional[str]:
    normalized = [str(path or "").strip().lstrip("./") for path in changed_files]
    normalized = [path for path in normalized if path and " (deleted)" not in path]
    if not normalized:
        return None
    lowered_failure = failure_line.lower()
    for path in normalized:
        if (
            path.lower() in lowered_failure
            or Path(path).name.lower() in lowered_failure
        ):
            return path
    return normalized[0]


def _read_post_content(project_dir: Path, rel_path: str) -> Optional[str]:
    path = (project_dir / rel_path).resolve()
    try:
        if not path.is_relative_to(project_dir.resolve()) or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def build_diff_capsule(
    *,
    pre_checksum: Dict[str, str],
    project_dir: Path,
    changed_files: List[str],
    envelope: DebugFeedbackEnvelope,
) -> Optional[DiffCapsule]:
    """Build a minimal diff capsule from a pre-step content snapshot.

    The ``pre_checksum`` name is retained for the Phase 7G roadmap contract. In
    this implementation the caller passes the local pre-step file-content
    snapshot produced by ``snapshot_file_contents``.
    """

    if envelope.failure_class not in DIFF_REPAIR_FAILURE_CLASSES:
        return None
    if not changed_files:
        return None

    failure_line = _failure_line(envelope)
    primary_file = _select_primary_file(changed_files, failure_line)
    if not primary_file:
        return None

    post_content = _read_post_content(project_dir, primary_file)
    if post_content is None:
        return None

    pre_content = pre_checksum.get(primary_file, "")
    diff_lines = list(
        difflib.unified_diff(
            pre_content.splitlines(),
            post_content.splitlines(),
            fromfile=f"a/{primary_file}",
            tofile=f"b/{primary_file}",
            lineterm="",
        )
    )
    if not diff_lines:
        return None

    capped_diff = "\n".join(diff_lines[:DIFF_LINE_LIMIT])
    return DiffCapsule(
        primary_file=primary_file,
        diff_text=capped_diff,
        failure_line=failure_line,
        failure_class=envelope.failure_class,
        changed_file_count=len(changed_files),
        workspace_path=str(project_dir),
    )


def build_bounded_diff_repair_prompt(
    capsule: DiffCapsule,
    evidence_capsule: Optional[Any] = None,
) -> str:
    workspace = render_workspace_path_for_prompt(Path(capsule.workspace_path or "."))
    evidence_section = ""
    if evidence_capsule is not None:
        from app.services.orchestration.evidence_capsule import render_evidence_section

        rendered = render_evidence_section(evidence_capsule)
        if rendered:
            evidence_section = f"\n{rendered}\n"
    return (
        "Return a bare JSON array of one minimal debug repair step. "
        "Do not return prose, markdown, comments, explanations, or fenced code.\n\n"
        f"Workspace scope: {workspace}\n"
        f"Failure class: {capsule.failure_class}\n"
        f"Primary file: {capsule.primary_file}\n"
        f"Failure line: {capsule.failure_line}\n\n"
        "Unified diff capsule:\n"
        f"{capsule.diff_text}\n"
        f"{evidence_section}\n"
        "Rules:\n"
        "1. Output exactly one JSON array containing one step object.\n"
        "2. The step object must include title, command, and verification_command.\n"
        "3. Touch only files shown in the diff capsule.\n"
        "4. Do not rewrite full files unless the diff shows the whole file.\n"
        "5. Use relative paths only; no absolute paths, `..`, or `~`.\n"
        "6. Do not include full stdout/stderr, planning context, or session history.\n"
    )
