"""Provider-free exact source-region resolution for semantic replacement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
)
from app.services.orchestration.planning.source_materialization import (
    current_source_version_identity,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    CanonicalPath,
    EntryType,
    GrantClass,
    PathAuthorityError,
    observe,
)

RESOLVED_UNIQUE = "RESOLVED_UNIQUE"
NOT_FOUND = "NOT_FOUND"
AMBIGUOUS = "AMBIGUOUS"
VERSION_MISMATCH = "VERSION_MISMATCH"
UNSUPPORTED_SELECTOR = "UNSUPPORTED_SELECTOR"
UNSAFE_TARGET = "UNSAFE_TARGET"
INVALID_AUTHORITY = "INVALID_AUTHORITY"


@dataclass(frozen=True)
class ResolutionResult:
    status: str
    selector_identity: str | None = None
    region: SourceRegionIdentity | None = None
    source_version_before: str | None = None
    source_version_after: str | None = None
    source_bytes: bytes | None = None
    target_path: Path | None = None
    diagnostic_code: str | None = None
    diagnostic_message: str | None = None

    @property
    def selected_region_bytes(self) -> bytes | None:
        if self.source_bytes is None or self.region is None:
            return None
        return self.source_bytes[self.region.start_byte : self.region.end_byte]


@dataclass(frozen=True)
class ExecutionMutationArtifact:
    """Execution-local semantic mutation evidence; not a persistence model."""

    plan_identity: str
    authority_identity: str
    canonical_path: str
    operation: str
    selector_identity: str
    source_version_before: str
    source_version_after: str | None
    selected_region_start_byte: int
    selected_region_end_byte: int
    selected_region_hash: str
    replacement_hash: str
    resolver_kind: str
    execution_mutation_identity: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_identity": self.plan_identity,
            "authority_identity": self.authority_identity,
            "canonical_path": self.canonical_path,
            "operation": self.operation,
            "selector_identity": self.selector_identity,
            "source_version_before": self.source_version_before,
            "source_version_after": self.source_version_after,
            "selected_region_start_byte": self.selected_region_start_byte,
            "selected_region_end_byte": self.selected_region_end_byte,
            "selected_region_hash": self.selected_region_hash,
            "replacement_hash": self.replacement_hash,
            "resolver_kind": self.resolver_kind,
            "execution_mutation_identity": self.execution_mutation_identity,
        }


def _failure(
    status: str,
    *,
    selector_identity: str | None = None,
    region: SourceRegionIdentity | None = None,
    source_version_before: str | None = None,
    source_version_after: str | None = None,
    target_path: Path | None = None,
    diagnostic_code: str | None = None,
    diagnostic_message: str | None = None,
) -> ResolutionResult:
    return ResolutionResult(
        status=status,
        selector_identity=selector_identity,
        region=region,
        source_version_before=source_version_before,
        source_version_after=source_version_after,
        target_path=target_path,
        diagnostic_code=diagnostic_code,
        diagnostic_message=diagnostic_message,
    )


def resolve_mutation_target(
    *,
    root: Path,
    canonical_path: CanonicalPath,
    operation_intent: Literal["replace_in_file"],
    selector: SourceRegionIdentity,
    expected_source_version: str,
    accepted_path_authority: AcceptedPathAuthority,
) -> ResolutionResult:
    """Resolve one exact current UTF-8 region under an existing APA grant."""

    if operation_intent != "replace_in_file":
        return _failure(
            UNSUPPORTED_SELECTOR,
            diagnostic_code="operation_intent_unsupported",
            diagnostic_message="semantic region resolution only supports replace_in_file",
        )
    if not isinstance(canonical_path, CanonicalPath) or not isinstance(
        selector, SourceRegionIdentity
    ):
        return _failure(
            INVALID_AUTHORITY,
            diagnostic_code="resolver_argument_invalid",
            diagnostic_message="canonical_path and selector must be typed authority inputs",
        )
    if not isinstance(accepted_path_authority, AcceptedPathAuthority):
        return _failure(
            INVALID_AUTHORITY,
            selector_identity=selector.selector_identity,
            diagnostic_code="authority_type_invalid",
            diagnostic_message="accepted path authority is missing or malformed",
        )
    if selector.canonical_path != canonical_path:
        return _failure(
            INVALID_AUTHORITY,
            selector_identity=selector.selector_identity,
            diagnostic_code="selector_path_mismatch",
            diagnostic_message="selector path does not equal the accepted operation path",
        )
    if expected_source_version != selector.expected_source_version:
        return _failure(
            VERSION_MISMATCH,
            selector_identity=selector.selector_identity,
            diagnostic_code="selector_expected_version_mismatch",
            diagnostic_message="resolver expected version does not equal selector version",
        )
    if not expected_source_version:
        return _failure(
            UNSUPPORTED_SELECTOR,
            selector_identity=selector.selector_identity,
            diagnostic_code="expected_source_version_missing",
            diagnostic_message="semantic replacement requires expected_source_version",
        )

    try:
        grant = accepted_path_authority.grant_for(canonical_path)
    except (AttributeError, PathAuthorityError) as exc:
        return _failure(
            INVALID_AUTHORITY,
            selector_identity=selector.selector_identity,
            diagnostic_code="authority_invalid",
            diagnostic_message=str(exc),
        )
    if grant is None or grant.grant_class is not GrantClass.EXISTING_MUTABLE:
        return _failure(
            INVALID_AUTHORITY,
            selector_identity=selector.selector_identity,
            diagnostic_code="existing_mutable_grant_required",
            diagnostic_message="semantic replace requires an exact existing_mutable grant",
        )

    workspace_root = Path(root).resolve()
    target = workspace_root / canonical_path.value
    try:
        observation = observe(workspace_root, canonical_path)
    except PathAuthorityError as exc:
        return _failure(
            UNSAFE_TARGET,
            selector_identity=selector.selector_identity,
            target_path=target,
            diagnostic_code=exc.code,
            diagnostic_message=exc.message,
        )
    if observation.symlink_segment:
        return _failure(
            UNSAFE_TARGET,
            selector_identity=selector.selector_identity,
            target_path=target,
            diagnostic_code="path_symlink_rejected",
            diagnostic_message="semantic target contains a symlink segment",
        )
    if not observation.exists or observation.entry_type is not EntryType.REGULAR_FILE:
        return _failure(
            UNSAFE_TARGET,
            selector_identity=selector.selector_identity,
            target_path=target,
            diagnostic_code="target_not_regular_file",
            diagnostic_message="semantic target is not an existing regular file",
        )

    current_version = current_source_version_identity(target)
    if current_version != expected_source_version:
        return _failure(
            VERSION_MISMATCH,
            selector_identity=selector.selector_identity,
            source_version_before=current_version,
            target_path=target,
            diagnostic_code="source_version_mismatch",
            diagnostic_message="current source version differs from accepted evidence",
        )

    try:
        source_bytes = target.read_bytes()
        source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _failure(
            UNSUPPORTED_SELECTOR,
            selector_identity=selector.selector_identity,
            source_version_before=current_version,
            target_path=target,
            diagnostic_code="source_not_utf8",
            diagnostic_message=f"semantic replacement requires UTF-8 source: {exc}",
        )

    if current_source_version_identity(target) != expected_source_version:
        current_after_read = current_source_version_identity(target)
        return _failure(
            VERSION_MISMATCH,
            selector_identity=selector.selector_identity,
            source_version_before=current_version,
            source_version_after=current_after_read,
            target_path=target,
            diagnostic_code="source_changed_during_resolution",
            diagnostic_message="source changed while semantic target was read",
        )
    if selector.end_byte > len(source_bytes):
        return _failure(
            NOT_FOUND,
            selector_identity=selector.selector_identity,
            source_version_before=current_version,
            target_path=target,
            diagnostic_code="region_out_of_bounds",
            diagnostic_message="selected region is outside current source bytes",
        )
    try:
        source_bytes[: selector.start_byte].decode("utf-8")
        source_bytes[: selector.end_byte].decode("utf-8")
    except UnicodeDecodeError as exc:
        return _failure(
            UNSUPPORTED_SELECTOR,
            selector_identity=selector.selector_identity,
            source_version_before=current_version,
            target_path=target,
            diagnostic_code="offset_not_utf8_boundary",
            diagnostic_message=f"region offset is not a UTF-8 boundary: {exc}",
        )

    selected = source_bytes[selector.start_byte : selector.end_byte]
    if hashlib.sha256(selected).hexdigest() != selector.selected_region_sha256:
        return _failure(
            NOT_FOUND,
            selector_identity=selector.selector_identity,
            source_version_before=current_version,
            target_path=target,
            diagnostic_code="selected_region_hash_mismatch",
            diagnostic_message="current bytes do not match selected_region_sha256",
        )
    candidates = exact_region_candidates(source_bytes, selector)
    if not candidates:
        return _failure(
            NOT_FOUND,
            selector_identity=selector.selector_identity,
            source_version_before=current_version,
            target_path=target,
            diagnostic_code="exact_region_not_found",
            diagnostic_message="no exact current region matched the selector",
        )
    if len(candidates) != 1:
        return _failure(
            AMBIGUOUS,
            selector_identity=selector.selector_identity,
            source_version_before=current_version,
            target_path=target,
            diagnostic_code="exact_region_ambiguous",
            diagnostic_message="more than one exact region candidate was produced",
        )
    return ResolutionResult(
        status=RESOLVED_UNIQUE,
        selector_identity=selector.selector_identity,
        region=selector,
        source_version_before=current_version,
        source_version_after=current_version,
        source_bytes=source_bytes,
        target_path=target,
        diagnostic_code=None,
        diagnostic_message=None,
    )


def exact_region_candidates(
    source_bytes: bytes, selector: SourceRegionIdentity
) -> tuple[SourceRegionIdentity, ...]:
    """Return the exact selector candidate set without search or ranking.

    The D3 selector is already an immutable region identity, so this set can
    contain only zero or one candidate.  Future derivation inputs must reduce
    their candidates to this same exact form before mutation.
    """

    if selector.end_byte > len(source_bytes):
        return ()
    try:
        source_bytes[: selector.start_byte].decode("utf-8")
        source_bytes[: selector.end_byte].decode("utf-8")
    except UnicodeDecodeError:
        return ()
    selected = source_bytes[selector.start_byte : selector.end_byte]
    if hashlib.sha256(selected).hexdigest() != selector.selected_region_sha256:
        return ()
    return (selector,)


def build_execution_mutation_artifact(
    *,
    accepted_path_authority: AcceptedPathAuthority,
    resolution: ResolutionResult,
    replacement: str,
    source_version_after: str | None,
) -> ExecutionMutationArtifact:
    if resolution.status != RESOLVED_UNIQUE or resolution.region is None:
        raise ValueError("mutation artifact requires a unique resolution")
    region = resolution.region
    replacement_hash = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
    payload = {
        "plan_identity": accepted_path_authority.accepted_plan_identity,
        "authority_identity": accepted_path_authority.authority_identity,
        "canonical_path": region.canonical_path.value,
        "operation": "replace_in_file",
        "selector_identity": resolution.selector_identity,
        "source_version": resolution.source_version_before,
        "selected_region_start_byte": region.start_byte,
        "selected_region_end_byte": region.end_byte,
        "selected_region_hash": region.selected_region_sha256,
        "replacement_hash": replacement_hash,
    }
    mutation_identity = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return ExecutionMutationArtifact(
        plan_identity=accepted_path_authority.accepted_plan_identity,
        authority_identity=accepted_path_authority.authority_identity,
        canonical_path=region.canonical_path.value,
        operation="replace_in_file",
        selector_identity=str(resolution.selector_identity),
        source_version_before=str(resolution.source_version_before),
        source_version_after=source_version_after,
        selected_region_start_byte=region.start_byte,
        selected_region_end_byte=region.end_byte,
        selected_region_hash=region.selected_region_sha256,
        replacement_hash=replacement_hash,
        resolver_kind=region.derivation_kind,
        execution_mutation_identity=mutation_identity,
    )
