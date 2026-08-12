"""Provider-free construction of semantic source-region selectors.

The Planning provider does not author this intent yet.  This module is the
bounded internal seam for a future producer: it accepts one Orchestrator-owned
materialization region reference, verifies the accepted source evidence, and
constructs the existing :class:`SourceRegionIdentity` from the authoritative
full source bytes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    SPAN_PRIMARY_TARGET,
    MaterializedSourceSpan,
    PlannerSourceMaterialization,
    current_source_version_identity,
    materialized_source_file,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    CanonicalPath,
    EntryType,
    GrantClass,
    PathAuthorityError,
    declare,
    observe,
)

CONSTRUCTED_UNIQUE = "CONSTRUCTED_UNIQUE"
NOT_FOUND = "NOT_FOUND"
AMBIGUOUS = "AMBIGUOUS"
VERSION_MISMATCH = "VERSION_MISMATCH"
UNSUPPORTED_INTENT = "UNSUPPORTED_INTENT"
INVALID_AUTHORITY = "INVALID_AUTHORITY"
UNSAFE_SOURCE = "UNSAFE_SOURCE"


@dataclass(frozen=True)
class MaterializedRegionReference:
    """A closed reference to one Orchestrator-issued materialization region."""

    region_kind: Literal["primary_target_region"] = SPAN_PRIMARY_TARGET


@dataclass(frozen=True)
class SemanticTargetIntent:
    """The minimum internal semantic target contract.

    The operation path is deliberately not repeated here.  The caller binds
    the intent to the already-authorized operation path, so this object cannot
    redirect construction to another file.
    """

    region_reference: MaterializedRegionReference


@dataclass(frozen=True)
class SelectorConstructionResult:
    """Fail-closed result of deterministic selector construction."""

    status: str
    selector: SourceRegionIdentity | None = None
    canonical_path: CanonicalPath | None = None
    target_path: Path | None = None
    diagnostic_code: str | None = None
    diagnostic_message: str | None = None


def _failure(
    status: str,
    *,
    canonical_path: CanonicalPath | None = None,
    target_path: Path | None = None,
    diagnostic_code: str,
    diagnostic_message: str,
) -> SelectorConstructionResult:
    return SelectorConstructionResult(
        status=status,
        canonical_path=canonical_path,
        target_path=target_path,
        diagnostic_code=diagnostic_code,
        diagnostic_message=diagnostic_message,
    )


def _canonical_path(value: Any) -> CanonicalPath | None:
    if isinstance(value, CanonicalPath):
        return value
    if not isinstance(value, str):
        return None
    try:
        return declare(value)
    except PathAuthorityError:
        return None


def _span_bounds(
    span: MaterializedSourceSpan | None,
    *,
    record: Any,
) -> tuple[int, int] | None:
    if span is not None:
        start_byte = span.start_byte
        end_byte = span.end_byte
    else:
        # MaterializedSourceFile keeps the primary span in these fields for
        # compatibility with records created before the multi-span metadata.
        start_byte = getattr(record, "start_byte", None)
        end_byte = getattr(record, "end_byte", None)
    if (
        isinstance(start_byte, bool)
        or not isinstance(start_byte, int)
        or isinstance(end_byte, bool)
        or not isinstance(end_byte, int)
        or start_byte < 0
        or end_byte <= start_byte
        or start_byte > end_byte
    ):
        return None
    return start_byte, end_byte


def _primary_target_spans(record: Any) -> tuple[MaterializedSourceSpan, ...]:
    spans = tuple(getattr(record, "spans", ()) or ())
    return tuple(
        span
        for span in spans
        if isinstance(span, MaterializedSourceSpan) and span.kind == SPAN_PRIMARY_TARGET
    )


def _raw_line_spans(source_bytes: bytes) -> tuple[tuple[int, int], ...]:
    """Return line byte spans without normalizing CRLF or UTF-8 bytes."""

    spans: list[tuple[int, int]] = []
    start = 0
    for index, value in enumerate(source_bytes):
        if value == 0x0A:
            spans.append((start, index + 1))
            start = index + 1
    if start < len(source_bytes) or not spans:
        spans.append((start, len(source_bytes)))
    return tuple(spans)


def _authoritative_line_bounds(
    record: Any, source_bytes: bytes
) -> tuple[int, int] | None:
    start_line = getattr(record, "start_line", None)
    end_line = getattr(record, "end_line", None)
    if (
        isinstance(start_line, bool)
        or not isinstance(start_line, int)
        or isinstance(end_line, bool)
        or not isinstance(end_line, int)
        or start_line < 1
        or end_line < start_line
    ):
        return None
    lines = _raw_line_spans(source_bytes)
    if end_line > len(lines):
        return None
    return lines[start_line - 1][0], lines[end_line - 1][1]


def construct_source_region_identity(
    *,
    root: Path,
    canonical_path: CanonicalPath | str,
    semantic_target: SemanticTargetIntent,
    accepted_source_materialization: PlannerSourceMaterialization,
    accepted_path_authority: AcceptedPathAuthority,
    operation_intent: Literal["replace_in_file"] = "replace_in_file",
) -> SelectorConstructionResult:
    """Construct one exact selector from accepted materialization evidence.

    The materialized region reference supplies no trusted bytes or offsets.
    Its Orchestrator-created metadata identifies a candidate span; this
    function rereads the authoritative full source under the record's exact
    version identity and derives the final offsets and hash from those bytes.
    """

    if operation_intent != "replace_in_file":
        return _failure(
            UNSUPPORTED_INTENT,
            diagnostic_code="operation_intent_unsupported",
            diagnostic_message="selector construction only supports replace_in_file",
        )
    if not isinstance(semantic_target, SemanticTargetIntent):
        return _failure(
            UNSUPPORTED_INTENT,
            diagnostic_code="semantic_target_intent_invalid",
            diagnostic_message="semantic target must be a MaterializedRegionReference",
        )
    if not isinstance(semantic_target.region_reference, MaterializedRegionReference):
        return _failure(
            UNSUPPORTED_INTENT,
            diagnostic_code="materialized_region_reference_invalid",
            diagnostic_message="semantic target must contain one closed region reference",
        )
    if not isinstance(accepted_source_materialization, PlannerSourceMaterialization):
        return _failure(
            INVALID_AUTHORITY,
            diagnostic_code="source_materialization_invalid",
            diagnostic_message="accepted source evidence is malformed",
        )
    if not isinstance(accepted_path_authority, AcceptedPathAuthority):
        return _failure(
            INVALID_AUTHORITY,
            diagnostic_code="accepted_path_authority_invalid",
            diagnostic_message="accepted path authority is malformed",
        )

    declared_path = _canonical_path(canonical_path)
    if declared_path is None:
        return _failure(
            INVALID_AUTHORITY,
            diagnostic_code="canonical_path_invalid",
            diagnostic_message="operation path is not a safe canonical path",
        )

    if semantic_target.region_reference.region_kind != SPAN_PRIMARY_TARGET:
        return _failure(
            UNSUPPORTED_INTENT,
            canonical_path=declared_path,
            diagnostic_code="materialized_region_kind_unsupported",
            diagnostic_message="only the Orchestrator primary target region is supported",
        )

    try:
        grant = accepted_path_authority.grant_for(declared_path)
    except (AttributeError, PathAuthorityError) as exc:
        return _failure(
            INVALID_AUTHORITY,
            canonical_path=declared_path,
            diagnostic_code="accepted_path_authority_invalid",
            diagnostic_message=str(exc),
        )
    if grant is None or grant.grant_class is not GrantClass.EXISTING_MUTABLE:
        return _failure(
            INVALID_AUTHORITY,
            canonical_path=declared_path,
            diagnostic_code="existing_mutable_grant_required",
            diagnostic_message="replace construction requires an exact existing_mutable grant",
        )

    workspace_root = Path(root).resolve()
    target_path = workspace_root / declared_path.value
    record = materialized_source_file(
        accepted_source_materialization, declared_path.value
    )
    if record is None:
        return _failure(
            NOT_FOUND,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="materialized_region_not_found",
            diagnostic_message="accepted materialization has no record for the operation path",
        )
    if getattr(record, "workspace_identity", None) != str(workspace_root):
        return _failure(
            INVALID_AUTHORITY,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="workspace_identity_mismatch",
            diagnostic_message="source evidence belongs to another runtime workspace",
        )
    if getattr(record, "status", None) != SOURCE_STATUS_EXISTING:
        return _failure(
            UNSAFE_SOURCE,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="source_is_not_existing_materialized_file",
            diagnostic_message="semantic replacement requires an existing source file",
        )
    expected_version = getattr(record, "version_identity", None)
    if not isinstance(expected_version, str) or not expected_version:
        return _failure(
            UNSAFE_SOURCE,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="source_version_missing",
            diagnostic_message="accepted source evidence has no version identity",
        )

    target_match_count = getattr(record, "target_match_count", 0)
    if isinstance(target_match_count, bool) or not isinstance(target_match_count, int):
        return _failure(
            UNSAFE_SOURCE,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="target_match_count_invalid",
            diagnostic_message="materialization target metadata is malformed",
        )
    if target_match_count > 1:
        return _failure(
            AMBIGUOUS,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="materialized_target_ambiguous",
            diagnostic_message="authoritative materialization reports multiple target matches",
        )
    if target_match_count != 1 or not bool(getattr(record, "target_included", False)):
        return _failure(
            NOT_FOUND,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="materialized_target_insufficient",
            diagnostic_message="materialization does not prove one visible target region",
        )

    matching_spans = _primary_target_spans(record)
    if len(matching_spans) > 1:
        return _failure(
            AMBIGUOUS,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="materialized_region_ambiguous",
            diagnostic_message="multiple primary target spans match the intent",
        )
    bounds = _span_bounds(
        matching_spans[0] if matching_spans else None,
        record=record,
    )
    if bounds is None:
        return _failure(
            NOT_FOUND,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="materialized_region_bounds_missing",
            diagnostic_message="materialization has insufficient authoritative region bounds",
        )
    start_byte, end_byte = bounds

    try:
        observation = observe(workspace_root, declared_path)
    except PathAuthorityError as exc:
        return _failure(
            UNSAFE_SOURCE,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code=exc.code,
            diagnostic_message=exc.message,
        )
    if observation.symlink_segment:
        return _failure(
            UNSAFE_SOURCE,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="path_symlink_rejected",
            diagnostic_message="source target contains a symlink segment",
        )
    if not observation.exists or observation.entry_type is not EntryType.REGULAR_FILE:
        return _failure(
            UNSAFE_SOURCE,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="source_target_not_regular_file",
            diagnostic_message="source target is not an existing regular file",
        )

    current_version = current_source_version_identity(target_path)
    if current_version != expected_version:
        return _failure(
            VERSION_MISMATCH,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="source_version_mismatch",
            diagnostic_message="current source version differs from accepted materialization",
        )
    try:
        source_bytes = target_path.read_bytes()
        source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _failure(
            UNSAFE_SOURCE,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="source_not_safe_utf8",
            diagnostic_message=f"authoritative source cannot be safely read: {exc}",
        )
    current_after_read = current_source_version_identity(target_path)
    if current_after_read != expected_version:
        return _failure(
            VERSION_MISMATCH,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="source_changed_during_construction",
            diagnostic_message="source changed while the authoritative region was read",
        )
    line_bounds = _authoritative_line_bounds(record, source_bytes)
    if line_bounds is not None:
        start_byte, end_byte = line_bounds
    if end_byte > len(source_bytes) or start_byte >= end_byte:
        return _failure(
            NOT_FOUND,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="materialized_region_out_of_bounds",
            diagnostic_message="authoritative region is outside the current full source",
        )
    if (start_byte > 0 and source_bytes[start_byte - 1 : start_byte] != b"\n") or (
        end_byte < len(source_bytes) and source_bytes[end_byte - 1 : end_byte] != b"\n"
    ):
        return _failure(
            NOT_FOUND,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="materialized_region_not_line_aligned",
            diagnostic_message="authoritative region boundaries are not source line boundaries",
        )
    try:
        source_bytes[:start_byte].decode("utf-8")
        source_bytes[:end_byte].decode("utf-8")
    except UnicodeDecodeError as exc:
        return _failure(
            UNSAFE_SOURCE,
            canonical_path=declared_path,
            target_path=target_path,
            diagnostic_code="materialized_region_not_utf8_boundary",
            diagnostic_message=f"authoritative region is not a UTF-8 boundary: {exc}",
        )

    selected_region = source_bytes[start_byte:end_byte]
    selector = SourceRegionIdentity.from_region(
        canonical_path=declared_path,
        expected_source_version=expected_version,
        start_byte=start_byte,
        end_byte=end_byte,
        selected_region_sha256=hashlib.sha256(selected_region).hexdigest(),
    )
    return SelectorConstructionResult(
        status=CONSTRUCTED_UNIQUE,
        selector=selector,
        canonical_path=declared_path,
        target_path=target_path,
    )
