"""One canonical verification authority for grounded source operations.

Planner immediate inspection and ValidatorService must never reach different
conclusions about the same operation, so the rule lives here once and both call
it.

Two concepts are deliberately kept apart:

* *model-visible source evidence* — the bounded span(s) placed in the prompt;
* *system-side deterministic verification* — whether the exact ``old`` text
  really exists in the current file.

The second may consult the complete file, but only while the version identity
captured during materialization still holds.  Text outside the visible span is
never reported as model-grounded; it is reported as verified against the same
file version.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    _binary_or_unreadable,
    current_source_version_identity,
    materialized_source_file,
)

SOURCE_EVIDENCE_VISIBLE_IN_SPAN = "visible_in_materialized_span"
SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION = "verified_in_full_file_same_version"
SOURCE_EVIDENCE_PLAN_GENERATED = "verified_in_plan_generated_content"
SOURCE_EVIDENCE_UNVERIFIED = "not_verified"

FAILURE_MISSING_MATERIALIZATION = "missing_source_materialization"
FAILURE_WORKSPACE_IDENTITY_MISMATCH = "workspace_identity_mismatch"
FAILURE_UNSAFE_PATH = "path_outside_runtime_workspace"
FAILURE_VERSION_UNREADABLE = "source_version_unreadable"
FAILURE_VERSION_CHANGED = "source_version_changed_since_materialization"
FAILURE_UNREADABLE_SOURCE = "unreadable_or_binary_source"
FAILURE_STALE_OLD_TEXT = "stale_old_text_absent_from_current_source"


@dataclass(frozen=True)
class ResolvedSource:
    """A version-fenced reading of one materialized existing file."""

    relative_path: str
    recorded_version_identity: str | None = None
    current_version_identity: str | None = None
    visible_content: str | None = None
    full_content: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class SourceOperationVerdict:
    """The single verdict both the planner and the validator report from."""

    path: str
    step_index: int | None
    operation_index: int | None
    recorded_version_identity: str | None
    current_version_identity: str | None
    present_in_visible_span: bool
    present_in_full_file_same_version: bool
    visibility: str
    failure_code: str | None

    @property
    def verified(self) -> bool:
        return self.failure_code is None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_version_fenced_source(
    materialization: Any, relative_path: str, project_dir: Path
) -> ResolvedSource:
    """Read the complete current file only while its captured version holds."""

    record = materialized_source_file(materialization, relative_path)
    if record is None or record.status != SOURCE_STATUS_EXISTING:
        return ResolvedSource(
            relative_path=relative_path,
            failure_code=FAILURE_MISSING_MATERIALIZATION,
        )

    root = Path(project_dir).resolve()
    if record.workspace_identity != str(root):
        return ResolvedSource(
            relative_path=record.relative_path,
            recorded_version_identity=record.version_identity,
            visible_content=record.content,
            failure_code=FAILURE_WORKSPACE_IDENTITY_MISMATCH,
        )

    path = (root / record.relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return ResolvedSource(
            relative_path=record.relative_path,
            recorded_version_identity=record.version_identity,
            visible_content=record.content,
            failure_code=FAILURE_UNSAFE_PATH,
        )

    current_version = current_source_version_identity(path)
    if current_version is None:
        return ResolvedSource(
            relative_path=record.relative_path,
            recorded_version_identity=record.version_identity,
            visible_content=record.content,
            failure_code=FAILURE_VERSION_UNREADABLE,
        )
    if current_version != record.version_identity:
        return ResolvedSource(
            relative_path=record.relative_path,
            recorded_version_identity=record.version_identity,
            current_version_identity=current_version,
            visible_content=record.content,
            failure_code=FAILURE_VERSION_CHANGED,
        )

    if _binary_or_unreadable(path):
        return ResolvedSource(
            relative_path=record.relative_path,
            recorded_version_identity=record.version_identity,
            current_version_identity=current_version,
            visible_content=record.content,
            failure_code=FAILURE_UNREADABLE_SOURCE,
        )
    try:
        full_content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ResolvedSource(
            relative_path=record.relative_path,
            recorded_version_identity=record.version_identity,
            current_version_identity=current_version,
            visible_content=record.content,
            failure_code=FAILURE_UNREADABLE_SOURCE,
        )
    # The read must be fenced on both sides: a file rewritten during the read
    # would otherwise be accepted under the captured identity.
    if current_source_version_identity(path) != record.version_identity:
        return ResolvedSource(
            relative_path=record.relative_path,
            recorded_version_identity=record.version_identity,
            current_version_identity=current_source_version_identity(path),
            visible_content=record.content,
            failure_code=FAILURE_VERSION_CHANGED,
        )

    return ResolvedSource(
        relative_path=record.relative_path,
        recorded_version_identity=record.version_identity,
        current_version_identity=current_version,
        visible_content=record.content,
        full_content=full_content,
    )


def verify_replace_operation(
    resolved: ResolvedSource | None,
    old_text: str,
    *,
    relative_path: str = "",
    simulated_content: str | None = None,
    step_index: int | None = None,
    operation_index: int | None = None,
) -> SourceOperationVerdict:
    """Return the shared verdict for one ``replace_in_file`` operation.

    ``simulated_content`` is the caller's in-plan buffer for the file.  When it
    is supplied it is the authority on presence, so an earlier operation in the
    same plan cannot be undone by evidence captured before it; the visible span
    and the fenced full file then only classify *how* the text was grounded.
    """

    path = resolved.relative_path if resolved is not None else relative_path
    recorded_version = resolved.recorded_version_identity if resolved else None
    current_version = resolved.current_version_identity if resolved else None

    def verdict(
        *,
        present_visible: bool,
        present_full: bool,
        visibility: str,
        failure_code: str | None,
    ) -> SourceOperationVerdict:
        return SourceOperationVerdict(
            path=path,
            step_index=step_index,
            operation_index=operation_index,
            recorded_version_identity=recorded_version,
            current_version_identity=current_version,
            present_in_visible_span=present_visible,
            present_in_full_file_same_version=present_full,
            visibility=visibility,
            failure_code=failure_code,
        )

    if simulated_content is None:
        if resolved is None:
            return verdict(
                present_visible=False,
                present_full=False,
                visibility=SOURCE_EVIDENCE_UNVERIFIED,
                failure_code=FAILURE_MISSING_MATERIALIZATION,
            )
        if resolved.failure_code is not None:
            return verdict(
                present_visible=False,
                present_full=False,
                visibility=SOURCE_EVIDENCE_UNVERIFIED,
                failure_code=resolved.failure_code,
            )

    visible = resolved.visible_content if resolved is not None else None
    full = resolved.full_content if resolved is not None else None
    present_visible = bool(old_text) and bool(visible) and old_text in visible
    present_full = bool(old_text) and full is not None and old_text in full

    if simulated_content is None:
        present = present_visible or present_full
    else:
        present = bool(old_text) and old_text in simulated_content

    if not present:
        return verdict(
            present_visible=False,
            present_full=False,
            visibility=SOURCE_EVIDENCE_UNVERIFIED,
            failure_code=FAILURE_STALE_OLD_TEXT,
        )
    if present_visible:
        visibility = SOURCE_EVIDENCE_VISIBLE_IN_SPAN
    elif present_full:
        visibility = SOURCE_EVIDENCE_FULL_FILE_SAME_VERSION
    else:
        visibility = SOURCE_EVIDENCE_PLAN_GENERATED
    return verdict(
        present_visible=present_visible,
        present_full=present_full,
        visibility=visibility,
        failure_code=None,
    )


def verify_replace_in_file(
    materialization: Any,
    relative_path: str,
    old_text: str,
    project_dir: Path,
    *,
    step_index: int | None = None,
    operation_index: int | None = None,
) -> SourceOperationVerdict:
    """Resolve and verify one operation for callers without an in-plan buffer."""

    resolved = resolve_version_fenced_source(
        materialization, relative_path, Path(project_dir)
    )
    return verify_replace_operation(
        resolved,
        old_text,
        relative_path=relative_path,
        step_index=step_index,
        operation_index=operation_index,
    )
