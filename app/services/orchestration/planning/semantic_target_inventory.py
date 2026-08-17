"""Bounded provider-facing semantic target handles for Planning.

Target handles are reconstructed from one accepted source materialization.  The
provider sees only opaque IDs and small descriptions; selector internals remain
an Orchestrator-owned construction result and are never accepted from provider
output.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.services.orchestration.planning.semantic_selector_construction import (
    CONSTRUCTED_UNIQUE,
    MaterializedRegionReference,
    SemanticTargetIntent,
    construct_source_region_identity,
)
from app.services.orchestration.planning.source_materialization import (
    HINT_TYPE_EXACT_CALL,
    HINT_TYPE_QUOTED_SNIPPET,
    SOURCE_STATUS_EXISTING,
    SPAN_PRIMARY_TARGET,
    MaterializedSourceSpan,
    PlannerSourceMaterialization,
)
from app.services.orchestration.validation.path_authority import (
    EntryType,
    PathAuthorityError,
    CanonicalPath,
    declare,
    observe,
)

PROVIDER_SEMANTIC_FIELDS = frozenset({"op", "path", "target_id", "new"})
FORBIDDEN_PROVIDER_FIELDS = frozenset(
    {
        "selector",
        "start_byte",
        "end_byte",
        "expected_source_version",
        "selected_region_sha256",
        "derivation_kind",
        "version",
        "hash",
        "offsets",
        "region_hash",
        "source_region_identity",
    }
)


class SemanticTargetInventoryError(ValueError):
    """The bounded target inventory could not be constructed safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class SemanticTargetContractError(ValueError):
    """Provider semantic intent is invalid or cannot be constructed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class SemanticTargetHandle:
    """Internal target handle with a deliberately narrow provider projection."""

    target_id: str
    path: str
    label: str
    context: str
    semantic_target: SemanticTargetIntent

    def to_provider_dict(self) -> dict[str, str]:
        return {
            "target_id": self.target_id,
            "path": self.path,
            "label": self.label,
            "context": self.context,
        }


@dataclass(frozen=True)
class SemanticTargetInventory:
    """Deterministic, request-local handles; not an authority or registry."""

    handles: tuple[SemanticTargetHandle, ...]
    eligible_existing_mutable_paths: tuple[str, ...]

    @property
    def by_id(self) -> dict[str, SemanticTargetHandle]:
        return {handle.target_id: handle for handle in self.handles}

    def to_provider_dict(self) -> list[dict[str, str]]:
        return [handle.to_provider_dict() for handle in self.handles]

    def resolve(self, target_id: Any, path: Any) -> SemanticTargetHandle:
        normalized_id = str(target_id or "").strip()
        handle = self.by_id.get(normalized_id)
        if handle is None:
            raise SemanticTargetContractError(
                "unknown_target_id",
                "target_id is not present in the current Orchestrator-issued inventory",
            )
        try:
            canonical_path = declare(path)
        except PathAuthorityError as exc:
            raise SemanticTargetContractError(
                "target_path_invalid", "target path is not safely declared"
            ) from exc
        if canonical_path.value != handle.path:
            raise SemanticTargetContractError(
                "target_id_path_mismatch",
                "target_id is bound to a different canonical path",
            )
        return handle


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _primary_spans(item: Any) -> tuple[MaterializedSourceSpan, ...]:
    return tuple(
        span
        for span in tuple(getattr(item, "spans", ()) or ())
        if isinstance(span, MaterializedSourceSpan) and span.kind == SPAN_PRIMARY_TARGET
    )


def _scope_paths(task_scope: Iterable[Any], materialization: Any) -> set[str]:
    values = list(task_scope or ())
    if not values:
        values = [
            getattr(item, "relative_path", "")
            for item in getattr(materialization, "files", ()) or ()
            if bool(getattr(item, "expected", False))
        ]
    result: set[str] = set()
    for value in values:
        try:
            result.add(declare(value).value)
        except (PathAuthorityError, TypeError, ValueError):
            continue
    return result


def _record_is_eligible(
    item: Any,
    *,
    scope_paths: set[str],
    explicit_scope: bool,
    workspace_root: Path,
) -> CanonicalPath | None:
    if getattr(item, "status", None) != SOURCE_STATUS_EXISTING:
        return None
    try:
        path = declare(getattr(item, "relative_path", ""))
    except (PathAuthorityError, TypeError, ValueError):
        return None
    if path.value not in scope_paths:
        return None
    if not explicit_scope and not bool(getattr(item, "expected", False)):
        return None
    if not isinstance(getattr(item, "version_identity", None), str) or not getattr(
        item, "version_identity", ""
    ):
        return None
    if not isinstance(getattr(item, "content_hash", None), str) or not getattr(
        item, "content_hash", ""
    ):
        return None
    if getattr(item, "target_match_count", None) != 1 or not bool(
        getattr(item, "target_included", False)
    ):
        return None
    if getattr(item, "target_region_eligibility_reason", None):
        return None
    if getattr(item, "target_hint_type", None) not in {
        HINT_TYPE_EXACT_CALL,
        HINT_TYPE_QUOTED_SNIPPET,
    }:
        # A definition/symbol is a locator. Current materialization records no
        # structural body end, so it cannot safely become a replacement region.
        return None
    primary_spans = _primary_spans(item)
    if len(primary_spans) > 1:
        return None
    if not primary_spans:
        start_byte = getattr(item, "start_byte", None)
        end_byte = getattr(item, "end_byte", None)
        if (
            not isinstance(start_byte, int)
            or isinstance(start_byte, bool)
            or not isinstance(end_byte, int)
            or isinstance(end_byte, bool)
            or start_byte < 0
            or end_byte <= start_byte
        ):
            return None
    target_start = getattr(item, "target_match_start", None)
    target_end = getattr(item, "target_match_end", None)
    if (
        isinstance(target_start, bool)
        or not isinstance(target_start, int)
        or isinstance(target_end, bool)
        or not isinstance(target_end, int)
        or target_start < 0
        or target_end <= target_start
    ):
        return None
    try:
        observation = observe(workspace_root, path)
    except PathAuthorityError:
        return None
    if (
        observation.symlink_segment
        or not observation.exists
        or observation.entry_type is not EntryType.REGULAR_FILE
    ):
        return None
    return path


def _target_id_for_record(item: Any, path: CanonicalPath) -> str:
    primary_spans = _primary_spans(item)
    span_payload = [span.to_dict() for span in primary_spans]
    payload = {
        "schema_version": "semantic-target/1",
        "operation": "replace_in_file",
        "workspace_identity": getattr(item, "workspace_identity", ""),
        "canonical_path": path.value,
        "region_kind": SPAN_PRIMARY_TARGET,
        "version_lineage": getattr(item, "version_identity", None),
        "content_lineage": getattr(item, "content_hash", None),
        "start_line": getattr(item, "start_line", None),
        "end_line": getattr(item, "end_line", None),
        "target_match_start": getattr(item, "target_match_start", None),
        "target_match_end": getattr(item, "target_match_end", None),
        "primary_spans": span_payload,
    }
    return "tgt_" + hashlib.sha256(_canonical_json(payload)).hexdigest()[:24]


def build_semantic_target_inventory(
    source_materialization: PlannerSourceMaterialization,
    task_scope: Iterable[Any] = (),
) -> SemanticTargetInventory:
    """Build provider-safe handles from one bounded materialization only."""

    if not isinstance(source_materialization, PlannerSourceMaterialization):
        raise SemanticTargetInventoryError(
            "source_materialization_invalid", "semantic inventory needs materialization"
        )
    workspace_root = Path(source_materialization.workspace_identity)
    scope_values = list(task_scope or ())
    scope_paths = _scope_paths(scope_values, source_materialization)
    handles: list[SemanticTargetHandle] = []
    seen_ids: dict[str, str] = {}
    for item in sorted(
        source_materialization.files,
        key=lambda record: str(getattr(record, "relative_path", "")),
    ):
        path = _record_is_eligible(
            item,
            scope_paths=scope_paths,
            explicit_scope=bool(scope_values),
            workspace_root=workspace_root,
        )
        if path is None:
            continue
        target_id = _target_id_for_record(item, path)
        if target_id in seen_ids:
            raise SemanticTargetInventoryError(
                "target_id_collision",
                f"deterministic target ID collides for {seen_ids[target_id]} and {path.value}",
            )
        seen_ids[target_id] = path.value
        hint = str(getattr(item, "target_hint", "") or "").strip()
        strategy = str(getattr(item, "selection_strategy", "") or "").strip()
        label = "primary target region"
        if hint:
            label += f" ({hint[:120]})"
        context = (
            "Orchestrator-issued handle for the one supported current source "
            f"region in {path.value}; use it only for replace_in_file."
        )
        if strategy:
            context += f" Materialization context: {strategy}."
        handles.append(
            SemanticTargetHandle(
                target_id=target_id,
                path=path.value,
                label=label,
                context=context,
                semantic_target=SemanticTargetIntent(MaterializedRegionReference()),
            )
        )
    handles.sort(key=lambda handle: (handle.path, handle.target_id))
    return SemanticTargetInventory(
        handles=tuple(handles),
        eligible_existing_mutable_paths=tuple(
            sorted({handle.path for handle in handles})
        ),
    )


def render_semantic_target_inventory(
    source_materialization: PlannerSourceMaterialization,
) -> list[str]:
    inventory = build_semantic_target_inventory(source_materialization)
    if not inventory.handles:
        return []
    lines = [
        "semantic target handles (Orchestrator-issued; choose only listed IDs):",
    ]
    for handle in inventory.handles:
        lines.extend(
            [
                f"- target_id: {handle.target_id}",
                f"  path: {handle.path}",
                f"  label: {handle.label}",
                f"  context: {handle.context}",
            ]
        )
    return lines


def _nested_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        keys = {str(key) for key in value}
        for nested in value.values():
            keys.update(_nested_keys(nested))
        return keys
    if isinstance(value, (list, tuple)):
        result: set[str] = set()
        for nested in value:
            result.update(_nested_keys(nested))
        return result
    return set()


def _provider_contract_error(code: str, operation: Mapping[str, Any]) -> None:
    path = str(operation.get("path") or "")
    raise SemanticTargetContractError(
        code, f"provider replace operation is not an accepted semantic intent: {path}"
    )


def normalize_provider_semantic_intents(
    plan: Any,
    *,
    inventory: SemanticTargetInventory,
    project_dir: Path,
    source_materialization: PlannerSourceMaterialization,
) -> list[dict[str, Any]]:
    """Convert provider ``target_id`` operations before Plan validation.

    Legacy operations pass through unchanged.  A provider-authored selector or
    selector fact is always rejected, including when a valid target ID is also
    present.  Construction failures are not reinterpreted as legacy output.
    """

    if not isinstance(plan, list):
        raise SemanticTargetContractError(
            "provider_plan_shape_invalid", "provider plan must be a list"
        )
    normalized_plan = copy.deepcopy(plan)
    for step in normalized_plan:
        if not isinstance(step, Mapping):
            continue
        operations = step.get("ops") or []
        if not isinstance(operations, list):
            continue
        for operation_index, operation in enumerate(operations, start=1):
            if not isinstance(operation, dict):
                continue
            nested_keys = _nested_keys(operation)
            forbidden = nested_keys.intersection(FORBIDDEN_PROVIDER_FIELDS)
            if forbidden:
                _provider_contract_error(
                    "provider_selector_internals_forbidden", operation
                )
            has_target_id = "target_id" in operation
            if "target_id" in nested_keys and not has_target_id:
                _provider_contract_error("provider_semantic_shape_invalid", operation)
            if not has_target_id:
                continue
            if str(operation.get("op") or "").strip() != "replace_in_file":
                _provider_contract_error("target_id_operation_forbidden", operation)
            if any(
                key in operation
                for key in (
                    "old",
                    "old_text",
                    "search",
                    "match",
                    "pattern",
                    "target",
                    "old_string",
                    "old_str",
                    "oldText",
                )
            ):
                _provider_contract_error("provider_mixed_old_target_id", operation)
            if set(operation) != PROVIDER_SEMANTIC_FIELDS:
                _provider_contract_error("provider_semantic_shape_invalid", operation)
            if not isinstance(operation.get("new"), str):
                _provider_contract_error("provider_semantic_new_invalid", operation)
            handle = inventory.resolve(
                operation.get("target_id"), operation.get("path")
            )
            construction = construct_source_region_identity(
                root=Path(project_dir),
                canonical_path=handle.path,
                semantic_target=handle.semantic_target,
                accepted_source_materialization=source_materialization,
                accepted_path_authority=None,
                eligible_existing_mutable_paths=inventory.eligible_existing_mutable_paths,
                operation_intent="replace_in_file",
            )
            if (
                construction.status != CONSTRUCTED_UNIQUE
                or construction.selector is None
            ):
                raise SemanticTargetContractError(
                    f"semantic_target_construction_{construction.status.lower()}",
                    construction.diagnostic_message
                    or "Orchestrator could not construct the semantic selector",
                )
            operations[operation_index - 1] = {
                "op": "replace_in_file",
                "path": handle.path,
                "selector": construction.selector.to_dict(),
                "new": operation["new"],
            }
    return normalized_plan


__all__ = [
    "FORBIDDEN_PROVIDER_FIELDS",
    "PROVIDER_SEMANTIC_FIELDS",
    "SemanticTargetContractError",
    "SemanticTargetHandle",
    "SemanticTargetInventory",
    "SemanticTargetInventoryError",
    "build_semantic_target_inventory",
    "normalize_provider_semantic_intents",
    "render_semantic_target_inventory",
]
