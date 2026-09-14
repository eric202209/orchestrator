"""Deterministic source grounding for explicit post-Plan mutation targets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.services.orchestration.operations.file_ops_contract import (
    normalize_file_op_shape,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_NEW,
    MaterializedSourceFile,
    PlannerSourceMaterialization,
    materialize_planner_source_context,
    materialized_source_file,
)
from app.services.orchestration.planning.source_operation_verification import (
    FAILURE_VERSION_CHANGED,
    resolve_version_fenced_source,
)
from app.services.orchestration.validation.path_authority import (
    CanonicalPath,
    EntryType,
    PathObservationError,
    TrustClass,
    classify_trust,
    declare,
    observe,
)


POST_PLAN_GROUNDING_PATH_REJECTED = "POST_PLAN_GROUNDING_PATH_REJECTED"
POST_PLAN_GROUNDING_MISSING = "POST_PLAN_GROUNDING_MISSING"
POST_PLAN_GROUNDING_PROTECTED = "POST_PLAN_GROUNDING_PROTECTED"
POST_PLAN_GROUNDING_SYMLINK = "POST_PLAN_GROUNDING_SYMLINK"
POST_PLAN_GROUNDING_NOT_REGULAR = "POST_PLAN_GROUNDING_NOT_REGULAR"
POST_PLAN_GROUNDING_CAPACITY_EXCEEDED = "POST_PLAN_GROUNDING_CAPACITY_EXCEEDED"
POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE = "POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE"
POST_PLAN_GROUNDING_VERSION_STALE = "POST_PLAN_GROUNDING_VERSION_STALE"

EXISTING_MUTATION_OPS = frozenset(
    {"write_file", "append_file", "replace_in_file", "delete_file"}
)


@dataclass(frozen=True)
class PostPlanSourceGroundingResult:
    """Bounded outcome of grounding and preflighting one candidate Plan."""

    materialization: PlannerSourceMaterialization | None
    grounded_paths: tuple[str, ...] = ()
    preflighted_paths: tuple[str, ...] = ()
    failure_code: str | None = None
    failure_path: str | None = None
    failure_detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure_code is None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"ok": self.ok}


@dataclass(frozen=True)
class _MutationTarget:
    path: CanonicalPath
    operation_names: tuple[str, ...]


def _failure(
    materialization: PlannerSourceMaterialization | None,
    code: str,
    *,
    path: str | None = None,
    detail: str | None = None,
    grounded_paths: Iterable[str] = (),
    preflighted_paths: Iterable[str] = (),
) -> PostPlanSourceGroundingResult:
    return PostPlanSourceGroundingResult(
        materialization=materialization,
        grounded_paths=tuple(grounded_paths),
        preflighted_paths=tuple(preflighted_paths),
        failure_code=code,
        failure_path=path,
        failure_detail=detail,
    )


def _plan_mutation_targets(
    plan: Any,
) -> tuple[tuple[_MutationTarget, ...], PostPlanSourceGroundingResult | None]:
    """Extract only explicit supported file-operation paths from the Plan."""

    by_path: dict[str, tuple[CanonicalPath, list[str]]] = {}
    if not isinstance(plan, list):
        return (), None

    for step in plan:
        if not isinstance(step, Mapping):
            continue
        operations = step.get("ops")
        if not isinstance(operations, list):
            continue
        for operation in operations:
            if not isinstance(operation, Mapping):
                continue
            normalized = normalize_file_op_shape(operation)
            operation_name = str(normalized.get("op") or "").strip()
            if operation_name not in EXISTING_MUTATION_OPS:
                continue
            raw_path = operation.get("path")
            try:
                canonical = declare(raw_path)
            except Exception as exc:
                code = getattr(exc, "code", "path_rejected")
                failure_code = (
                    POST_PLAN_GROUNDING_PROTECTED
                    if code == "path_protected_root"
                    else POST_PLAN_GROUNDING_PATH_REJECTED
                )
                return (), _failure(
                    None,
                    failure_code,
                    path=str(raw_path) if raw_path is not None else None,
                    detail=str(exc),
                )
            existing = by_path.get(canonical.value)
            if existing is None:
                by_path[canonical.value] = (canonical, [operation_name])
            elif operation_name not in existing[1]:
                existing[1].append(operation_name)

    return (
        tuple(
            _MutationTarget(path=canonical, operation_names=tuple(operation_names))
            for canonical, operation_names in by_path.values()
        ),
        None,
    )


def _runtime_root_and_identity(
    project_dir: Path, workspace_identity: Any
) -> tuple[Path, str] | PostPlanSourceGroundingResult:
    root = Path(project_dir).resolve()
    physical_root = getattr(workspace_identity, "physical_runtime_root", None)
    if physical_root is not None and Path(physical_root).resolve() != root:
        return _failure(
            None,
            POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
            detail="workspace identity does not name the supplied Runtime Workspace",
        )
    if isinstance(workspace_identity, str) and workspace_identity.strip():
        if Path(workspace_identity).resolve() != root:
            return _failure(
                None,
                POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
                detail="workspace identity does not name the supplied Runtime Workspace",
            )
    return root, str(root)


def _observe_target(
    root: Path, target: _MutationTarget, materialization: PlannerSourceMaterialization
) -> tuple[Any, PostPlanSourceGroundingResult | None]:
    try:
        trust = classify_trust(target.path)
        if trust is not TrustClass.PRODUCT:
            return None, _failure(
                materialization,
                POST_PLAN_GROUNDING_PROTECTED,
                path=target.path.value,
                detail=f"path ownership is {trust.value}",
            )
        observation = observe(root, target.path)
    except PathObservationError as exc:
        return None, _failure(
            materialization,
            POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
            path=target.path.value,
            detail=str(exc),
        )
    if observation.symlink_segment:
        return None, _failure(
            materialization,
            POST_PLAN_GROUNDING_SYMLINK,
            path=target.path.value,
            detail="symlink targets are not authoritative source files",
        )
    if observation.exists and observation.entry_type is not EntryType.REGULAR_FILE:
        return None, _failure(
            materialization,
            POST_PLAN_GROUNDING_NOT_REGULAR,
            path=target.path.value,
            detail=f"observed entry type is {observation.entry_type.value}",
        )
    if observation.exists and observation.content_sha256 is None:
        return None, _failure(
            materialization,
            POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
            path=target.path.value,
            detail="source observation could not capture an exact content hash",
        )
    return observation, None


def _requires_existing_target(
    target: _MutationTarget,
    observation: Any,
    record: MaterializedSourceFile | None,
) -> tuple[bool, PostPlanSourceGroundingResult | None]:
    if observation.exists:
        return True, None
    if record is not None and record.status == SOURCE_STATUS_EXISTING:
        return True, _failure(
            None,
            POST_PLAN_GROUNDING_VERSION_STALE,
            path=target.path.value,
            detail="materialized existing source is no longer present",
        )
    if (
        "replace_in_file" in target.operation_names
        or "delete_file" in target.operation_names
    ):
        return True, _failure(
            None,
            POST_PLAN_GROUNDING_MISSING,
            path=target.path.value,
            detail="existing mutation operation requires an existing regular file",
        )
    if "append_file" in target.operation_names and not (
        record is not None
        and record.status == SOURCE_STATUS_NEW
        and record.creation_authorized
    ):
        return True, _failure(
            None,
            POST_PLAN_GROUNDING_MISSING,
            path=target.path.value,
            detail="append_file target is absent and has no creation authority",
        )
    return False, None


def _complete_grounded_record(
    record: MaterializedSourceFile | None,
    *,
    path: str,
    workspace_identity: str,
    observation: Any,
) -> bool:
    if record is None or record.relative_path != path:
        return False
    if record.workspace_identity != workspace_identity:
        return False
    if record.status != SOURCE_STATUS_EXISTING:
        return False
    if record.content is None or record.content_hash is None:
        return False
    if record.truncated or record.full_source_bytes is None:
        return False
    if record.version_identity is None:
        return False
    if record.full_source_bytes != observation.byte_length:
        return False
    if record.content_hash != observation.content_sha256:
        return False
    if record.full_source_bytes != len(record.content.encode("utf-8")):
        return False
    return True


def _merge_materialization(
    materialization: PlannerSourceMaterialization,
    additions: tuple[MaterializedSourceFile, ...],
) -> PlannerSourceMaterialization:
    return PlannerSourceMaterialization(
        workspace_identity=materialization.workspace_identity,
        files=(*materialization.files, *additions),
        maximum_files=materialization.maximum_files,
        maximum_bytes_per_file=materialization.maximum_bytes_per_file,
        maximum_total_source_bytes=materialization.maximum_total_source_bytes,
        materialized_source_bytes=materialization.materialized_source_bytes
        + sum(
            len(item.content.encode("utf-8"))
            for item in additions
            if item.content is not None
        ),
        unavailable_reasons=materialization.unavailable_reasons,
    )


def _preflight_existing_mutations(
    plan: Any,
    *,
    root: Path,
    materialization: PlannerSourceMaterialization,
    targets: tuple[_MutationTarget, ...],
    grounded_paths: tuple[str, ...],
) -> PostPlanSourceGroundingResult:
    preflighted: list[str] = []
    for target in targets:
        record = materialized_source_file(materialization, target.path.value)
        observation, observation_failure = _observe_target(
            root, target, materialization
        )
        if observation_failure is not None:
            return _failure(
                materialization,
                observation_failure.failure_code
                or POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
                path=observation_failure.failure_path,
                detail=observation_failure.failure_detail,
                grounded_paths=grounded_paths,
                preflighted_paths=preflighted,
            )
        if not observation.exists:
            if (
                record is not None
                and record.status == SOURCE_STATUS_NEW
                and record.creation_authorized
            ):
                continue
            continue
        resolved = resolve_version_fenced_source(
            materialization, target.path.value, root
        )
        preflighted.append(target.path.value)
        if resolved.failure_code is not None:
            code = (
                POST_PLAN_GROUNDING_VERSION_STALE
                if resolved.failure_code == FAILURE_VERSION_CHANGED
                else POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE
            )
            return _failure(
                materialization,
                code,
                path=target.path.value,
                detail=resolved.failure_code,
                grounded_paths=grounded_paths,
                preflighted_paths=preflighted,
            )
    return PostPlanSourceGroundingResult(
        materialization=materialization,
        grounded_paths=grounded_paths,
        preflighted_paths=tuple(preflighted),
    )


def ground_post_plan_source_materialization(
    plan: Any,
    *,
    project_dir: Path,
    source_materialization: PlannerSourceMaterialization | None,
    workspace_identity: Any = None,
) -> PostPlanSourceGroundingResult:
    """Ground missing explicit existing mutation targets from Runtime Workspace.

    The candidate Plan nominates paths only.  Authority is independently built
    from the already-isolated Runtime Workspace and then passed through the
    existing version-fenced source resolver.  This function never consults a
    provider, ProductRoot, prompt-local content, or expected-file metadata.
    """

    targets, extraction_failure = _plan_mutation_targets(plan)
    if extraction_failure is not None:
        return extraction_failure
    if not targets:
        return PostPlanSourceGroundingResult(materialization=source_materialization)
    if source_materialization is None:
        return _failure(
            None,
            POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
            detail="candidate has explicit mutation targets but no typed materialization",
        )

    runtime = _runtime_root_and_identity(project_dir, workspace_identity)
    if isinstance(runtime, PostPlanSourceGroundingResult):
        return _failure(
            source_materialization,
            runtime.failure_code or POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
            path=runtime.failure_path,
            detail=runtime.failure_detail,
        )
    root, runtime_identity = runtime
    if source_materialization.workspace_identity != runtime_identity:
        return _failure(
            source_materialization,
            POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
            detail="typed materialization belongs to a different Runtime Workspace",
        )

    file_map = source_materialization.file_map()
    missing_targets: list[tuple[_MutationTarget, Any]] = []
    for target in targets:
        observation, observation_failure = _observe_target(
            root, target, source_materialization
        )
        if observation_failure is not None:
            return observation_failure
        record = file_map.get(target.path.value)
        requires_existing, existence_failure = _requires_existing_target(
            target, observation, record
        )
        if existence_failure is not None:
            return _failure(
                source_materialization,
                existence_failure.failure_code
                or POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
                path=existence_failure.failure_path,
                detail=existence_failure.failure_detail,
            )
        if not requires_existing:
            continue
        if record is None:
            missing_targets.append((target, observation))
            continue
        if record.status != SOURCE_STATUS_EXISTING:
            if record.status == SOURCE_STATUS_NEW and record.creation_authorized:
                continue
            return _failure(
                source_materialization,
                POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
                path=target.path.value,
                detail="existing mutation target has a non-authoritative materialization record",
            )

    if (
        len(source_materialization.files) + len(missing_targets)
        > source_materialization.maximum_files
    ):
        return _failure(
            source_materialization,
            POST_PLAN_GROUNDING_CAPACITY_EXCEEDED,
            detail="grounding would exceed the existing total source-record bound",
        )

    additions: list[MaterializedSourceFile] = []
    if missing_targets:
        grounded = materialize_planner_source_context(
            root,
            task_description="",
            planner_contract=None,
            expected_paths=[target.path.value for target, _ in missing_targets],
            supporting_paths=(),
            workspace_identity=runtime_identity,
            maximum_files=source_materialization.maximum_files,
            maximum_bytes_per_file=source_materialization.maximum_bytes_per_file,
            maximum_total_source_bytes=source_materialization.maximum_total_source_bytes,
            source_cache={},
        )
        grounded_map = grounded.file_map()
        for target, observation in missing_targets:
            record = grounded_map.get(target.path.value)
            if not _complete_grounded_record(
                record,
                path=target.path.value,
                workspace_identity=runtime_identity,
                observation=observation,
            ):
                return _failure(
                    source_materialization,
                    POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
                    path=target.path.value,
                    detail="deterministic source builder did not return complete evidence",
                )
            additions.append(record)

    merged = _merge_materialization(source_materialization, tuple(additions))
    # PHASE36-SB1: the producer declares the budget it used and the fence
    # verifies it.  The total bound was already checked here; the per-file bound
    # was only ever propagated into re-materialization, so an inbound record
    # could exceed its own declared cap unnoticed.  Both declared caps are now
    # verified against the evidence actually carried.
    for item in merged.files:
        if item.included_source_bytes > merged.maximum_bytes_per_file:
            return _failure(
                source_materialization,
                POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
                path=item.relative_path,
                detail="materialized source exceeds the declared per-file byte bound",
            )
    if merged.materialized_source_bytes > merged.maximum_total_source_bytes:
        return _failure(
            source_materialization,
            POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
            detail="grounding would exceed the existing total source-byte bound",
        )
    return _preflight_existing_mutations(
        plan,
        root=root,
        materialization=merged,
        targets=targets,
        grounded_paths=tuple(target.path.value for target, _ in missing_targets),
    )


__all__ = [
    "EXISTING_MUTATION_OPS",
    "POST_PLAN_GROUNDING_CAPACITY_EXCEEDED",
    "POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE",
    "POST_PLAN_GROUNDING_MISSING",
    "POST_PLAN_GROUNDING_NOT_REGULAR",
    "POST_PLAN_GROUNDING_PATH_REJECTED",
    "POST_PLAN_GROUNDING_PROTECTED",
    "POST_PLAN_GROUNDING_SYMLINK",
    "POST_PLAN_GROUNDING_VERSION_STALE",
    "PostPlanSourceGroundingResult",
    "ground_post_plan_source_materialization",
]
