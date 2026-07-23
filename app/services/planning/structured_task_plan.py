"""Canonical Structured Task Plan domain for Protocol v2.

The Structured Task Plan is an immutable, provider-independent decomposition
of one accepted :class:`PlanningBrief`.  This module owns the semantic value,
application IDs, graph and coverage calculations, deterministic validation,
canonical serialization, Markdown/legacy projections, and structural diffing.

It deliberately has no provider, runtime, worker, or commit dependency.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
import hashlib
import html
import json
import math
import re
from types import MappingProxyType
from typing import Any, ClassVar
import unicodedata

from app.services.planning.planning_brief import PlanningBrief
from app.services.planning.validation_contract import (
    StructuredValidationContract,
    canonical_validation_hash,
)


STRUCTURED_TASK_PLAN_SCHEMA_VERSION = "structured-task-plan/1.0"
STRUCTURED_TASK_PLAN_STAGE_NAME = "structured_task_plan"
STRUCTURED_TASK_PLAN_STAGE_VERSION = 1
STRUCTURED_TASK_PLAN_RENDERER_VERSION = "structured-task-plan-markdown/1.0"
STRUCTURED_TASK_PLAN_VALIDATOR_VERSION = "structured-task-plan-validator/1.0"

# Phase 28H's recommended initial policy values.  They are configuration,
# not authority, and are exposed so persistence can include them in evidence.
DEFAULT_TASK_PLAN_POLICY = MappingProxyType(
    {
        "max_tasks": 200,
        "max_groups": 50,
        "max_dependencies_per_task": 8,
        "max_dependency_fan_in": 8,
        "max_dependency_fan_out": 8,
        "max_work_items_per_task": 8,
        "max_expected_effort": 8 * 60,
        "max_parallel_width": 4,
        "max_plan_bytes": 256 * 1024,
    }
)

TASK_CATEGORIES = (
    "implementation",
    "refactor",
    "test",
    "documentation",
    "migration",
    "verification",
    "research",
    "cleanup",
    "configuration",
    "operator_action",
    "review",
)
TASK_CATEGORY_RANK = {value: index for index, value in enumerate(TASK_CATEGORIES)}
TASK_PRIORITIES = frozenset({"required", "recommended", "optional"})
TASK_PRIORITY_RANK = {"required": 0, "recommended": 1, "optional": 2}
TASK_COMPLEXITIES = frozenset({"trivial", "small", "medium", "large", "very_large"})
DEPENDENCY_TYPES = frozenset(
    {
        "hard_completion",
        "artifact_ready",
        "review_gate",
        "ordering",
        "resource_serialization",
    }
)
BLOCKING_DEPENDENCY_TYPES = frozenset(
    {"hard_completion", "artifact_ready", "review_gate"}
)
CRITICAL_PATH_DEPENDENCY_TYPES = BLOCKING_DEPENDENCY_TYPES | {"ordering"}
GROUP_KINDS = frozenset(
    {"sequential", "parallel", "optional", "review_gate", "verification"}
)
GROUP_SKIP_POLICIES = frozenset({"not_skippable", "skippable"})
BLOCKING_STATES = frozenset({"blocking", "non_blocking", "review_required"})
EFFORT_UNITS = frozenset({"person_minutes"})
CONFIDENCE_VALUES = frozenset({"low", "medium", "high"})
EXECUTION_OWNER_ROLES = frozenset({"agent", "operator", "system", "reviewer"})
ISOLATION_MODES = frozenset({"isolated_workspace", "shared_workspace", "no_workspace"})
WRITE_SCOPES = frozenset({"project", "documentation", "none", "operator_only"})
NETWORK_MODES = frozenset({"none", "configured"})
PARALLELISM_MODES = frozenset({"safe", "read_only", "unsafe"})
REVIEW_MODES = frozenset({"none", "after_task", "before_dependents", "before_commit"})
TRACEABILITY_TARGET_KINDS = frozenset(
    {
        "goal",
        "requirement",
        "acceptance_criterion",
        "constraint",
        "architecture_context",
        "interface_contract",
        "scope",
    }
)
TRACEABILITY_ROLES = frozenset(
    {"implements", "enables", "verifies", "documents", "constrained_by"}
)
OMISSION_TARGET_KINDS = frozenset({"requirement", "acceptance_criterion"})
OMISSION_REASON_CODES = frozenset(
    {"optional_scope", "deferred_scope", "already_satisfied"}
)

_ID_PATTERNS = {
    "TASK": re.compile(r"^TASK-[0-9]{3}$"),
    "DEP": re.compile(r"^DEP-[0-9]{3}$"),
    "GROUP": re.compile(r"^GROUP-[0-9]{3}$"),
}
_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNSAFE_MARKUP_RE = re.compile(r"<\/?[A-Za-z][^>]*>|\b(?:javascript|data)\s*:", re.I)
_UNSAFE_TARGET_RE = re.compile(
    r"(?:^/|^[A-Za-z]:[\\/]|(?:^|[/\\])\.\.(?:[/\\]|$)|[*?\[\]{}]|\b(?:password|token|secret|api[_-]?key|private[_-]?key)\b)",
    re.I,
)


class StructuredTaskPlanError(ValueError):
    """Base error for invalid Task Plan values."""


class StructuredTaskPlanSchemaError(StructuredTaskPlanError):
    """A value does not have the supported canonical Task Plan shape."""


class StructuredTaskPlanValidationError(StructuredTaskPlanError):
    """A canonical Task Plan fails deterministic validation."""


class StructuredTaskPlanGraphError(StructuredTaskPlanValidationError):
    """A dependency graph cannot be constructed as a DAG."""


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = unicodedata.normalize("NFC", str(key))
            if normalized_key in normalized:
                raise StructuredTaskPlanSchemaError("duplicate normalized object key")
            normalized[normalized_key] = _normalize(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StructuredTaskPlanSchemaError("non-finite numbers are not canonical")
        return value
    raise StructuredTaskPlanSchemaError(
        f"Task Plan value is not JSON-compatible: {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize normalized values as compact, sorted UTF-8 JSON."""

    try:
        return json.dumps(
            _normalize(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StructuredTaskPlanSchemaError(
            "Task Plan is not canonically serializable"
        ) from exc


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _text(value: Any, field_name: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise StructuredTaskPlanSchemaError(f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if required and not normalized.strip():
        raise StructuredTaskPlanSchemaError(f"{field_name} is required")
    return normalized


def _tuple_text(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise StructuredTaskPlanSchemaError(f"{field_name} must be an array")
    return tuple(_text(item, field_name) for item in value)


def _record_dict(record: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in fields(record):
        value = getattr(record, item.name)
        if isinstance(value, tuple):
            result[item.name] = [
                _record_dict(part) if hasattr(part, "to_dict") else part
                for part in value
            ]
        elif hasattr(value, "to_dict"):
            result[item.name] = value.to_dict()
        else:
            result[item.name] = value
    return result


def _coerce(record_type: type[Any], value: Any, field_name: str) -> Any:
    if isinstance(value, record_type):
        return value
    if isinstance(value, Mapping):
        try:
            return record_type(**dict(value))
        except TypeError as exc:
            raise StructuredTaskPlanSchemaError(f"invalid {field_name}") from exc
    raise StructuredTaskPlanSchemaError(f"{field_name} must be an object")


@dataclass(frozen=True)
class BriefReference:
    checkpoint_id: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        checkpoint_id = (
            str(self.checkpoint_id)
            if isinstance(self.checkpoint_id, int)
            and not isinstance(self.checkpoint_id, bool)
            else self.checkpoint_id
        )
        object.__setattr__(
            self, "checkpoint_id", _text(checkpoint_id, "brief_ref.checkpoint_id")
        )
        object.__setattr__(
            self, "content_hash", _text(self.content_hash, "brief_ref.content_hash")
        )

    def to_dict(self) -> dict[str, str]:
        return {"checkpoint_id": self.checkpoint_id, "content_hash": self.content_hash}


@dataclass(frozen=True)
class InputManifestReference:
    id: str = ""
    hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "input_manifest_ref.id"))
        object.__setattr__(self, "hash", _text(self.hash, "input_manifest_ref.hash"))

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "hash": self.hash}


@dataclass(frozen=True)
class WorkItem:
    action: str = ""
    target: str = ""
    deliverable: str = ""
    done_when: str = ""
    validation_contract: StructuredValidationContract | None = None

    def __post_init__(self) -> None:
        for name in fields(self):
            if name.name == "validation_contract":
                value = getattr(self, name.name)
                if value is not None and not isinstance(
                    value, StructuredValidationContract
                ):
                    value = StructuredValidationContract.from_mapping(value)
                object.__setattr__(self, name.name, value)
            else:
                object.__setattr__(
                    self, name.name, _text(getattr(self, name.name), name.name)
                )

    def to_dict(self) -> dict[str, Any]:
        result = _record_dict(self)
        # Keep legacy plan hashes stable when the additive field is absent.
        # Structured validation is present only when explicitly authored.
        if self.validation_contract is None:
            result.pop("validation_contract", None)
        return result


@dataclass(frozen=True)
class Traceability:
    target_kind: str = ""
    target_id: str = ""
    role: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_kind", _text(self.target_kind, "traceability.target_kind")
        )
        object.__setattr__(
            self, "target_id", _text(self.target_id, "traceability.target_id")
        )
        object.__setattr__(self, "role", _text(self.role, "traceability.role"))

    def to_dict(self) -> dict[str, str]:
        return _record_dict(self)


@dataclass(frozen=True)
class EffortEstimate:
    unit: str = "person_minutes"
    lower: int = 0
    expected: int = 0
    upper: int = 0
    confidence: str = "medium"

    def __post_init__(self) -> None:
        for name in ("lower", "expected", "upper"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise StructuredTaskPlanSchemaError(
                    f"estimated_effort.{name} must be an integer"
                )
            object.__setattr__(self, name, value)
        object.__setattr__(self, "unit", _text(self.unit, "estimated_effort.unit"))
        object.__setattr__(
            self, "confidence", _text(self.confidence, "estimated_effort.confidence")
        )

    def to_dict(self) -> dict[str, Any]:
        return _record_dict(self)


@dataclass(frozen=True)
class ExecutionProfile:
    owner_role: str = "agent"
    isolation: str = "isolated_workspace"
    write_scope: str = "project"
    network: str = "none"
    parallelism: str = "safe"
    review: str = "after_task"

    def __post_init__(self) -> None:
        for name in fields(self):
            object.__setattr__(
                self,
                name.name,
                _text(getattr(self, name.name), f"execution_profile.{name.name}"),
            )

    def to_dict(self) -> dict[str, str]:
        return _record_dict(self)


@dataclass(frozen=True)
class Task:
    id: str = ""
    title: str = ""
    objective: str = ""
    implementation_description: str = ""
    rationale: str = ""
    priority: str = "required"
    complexity: str = "medium"
    estimated_effort: EffortEstimate = EffortEstimate()
    category: str = "implementation"
    execution_profile: ExecutionProfile = ExecutionProfile()
    blocking_state: str = "blocking"
    work_items: tuple[WorkItem, ...] = ()
    traceability: tuple[Traceability, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "id",
            "title",
            "objective",
            "implementation_description",
            "rationale",
            "priority",
            "complexity",
            "category",
            "blocking_state",
        ):
            object.__setattr__(
                self,
                name,
                _text(getattr(self, name), f"task.{name}", required=name != "id"),
            )
        object.__setattr__(
            self,
            "estimated_effort",
            _coerce(EffortEstimate, self.estimated_effort, "estimated_effort"),
        )
        object.__setattr__(
            self,
            "execution_profile",
            _coerce(ExecutionProfile, self.execution_profile, "execution_profile"),
        )
        object.__setattr__(
            self,
            "work_items",
            tuple(_coerce(WorkItem, item, "work_item") for item in self.work_items),
        )
        object.__setattr__(
            self,
            "traceability",
            tuple(
                _coerce(Traceability, item, "traceability")
                for item in self.traceability
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return _record_dict(self)


@dataclass(frozen=True)
class Dependency:
    id: str = ""
    prerequisite_task_id: str = ""
    dependent_task_id: str = ""
    type: str = "hard_completion"
    reason: str = ""

    def __post_init__(self) -> None:
        for name in fields(self):
            object.__setattr__(
                self,
                name.name,
                _text(
                    getattr(self, name.name),
                    f"dependency.{name.name}",
                    required=name.name != "id",
                ),
            )

    def to_dict(self) -> dict[str, str]:
        return _record_dict(self)


@dataclass(frozen=True)
class ExecutionGroup:
    id: str = ""
    kind: str = "parallel"
    order: int = 0
    task_ids: tuple[str, ...] = ()
    parallel_limit: int = 1
    skip_policy: str = "not_skippable"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "id", _text(self.id, "execution_group.id", required=False)
        )
        object.__setattr__(self, "kind", _text(self.kind, "execution_group.kind"))
        if isinstance(self.order, bool) or not isinstance(self.order, int):
            raise StructuredTaskPlanSchemaError(
                "execution_group.order must be an integer"
            )
        if isinstance(self.parallel_limit, bool) or not isinstance(
            self.parallel_limit, int
        ):
            raise StructuredTaskPlanSchemaError(
                "execution_group.parallel_limit must be an integer"
            )
        object.__setattr__(
            self, "task_ids", _tuple_text(self.task_ids, "execution_group.task_ids")
        )
        object.__setattr__(
            self, "skip_policy", _text(self.skip_policy, "execution_group.skip_policy")
        )

    def to_dict(self) -> dict[str, Any]:
        return _record_dict(self)


@dataclass(frozen=True)
class IntentionalOmission:
    target_kind: str = "requirement"
    target_id: str = ""
    reason_code: str = "optional_scope"
    brief_scope_or_decision_id: str = ""

    def __post_init__(self) -> None:
        for name in fields(self):
            object.__setattr__(
                self,
                name.name,
                _text(getattr(self, name.name), f"intentional_omission.{name.name}"),
            )

    def to_dict(self) -> dict[str, str]:
        return _record_dict(self)


@dataclass(frozen=True)
class Topology:
    topological_order: tuple[str, ...] = ()
    critical_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "topological_order",
            _tuple_text(self.topological_order, "topology.topological_order"),
        )
        object.__setattr__(
            self,
            "critical_path",
            _tuple_text(self.critical_path, "topology.critical_path"),
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "topological_order": list(self.topological_order),
            "critical_path": list(self.critical_path),
        }


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return _record_dict(self)


@dataclass(frozen=True)
class StructuredTaskPlanValidation:
    schema_valid: bool
    semantically_valid: bool
    protocol_acceptable: bool
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()
    validator_version: str = STRUCTURED_TASK_PLAN_VALIDATOR_VERSION
    validation_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if not self.validation_hash:
            payload = self.to_dict(include_hash=False)
            object.__setattr__(self, "validation_hash", canonical_json_hash(payload))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_valid": self.schema_valid,
            "semantically_valid": self.semantically_valid,
            "protocol_acceptable": self.protocol_acceptable,
            "errors": [item.to_dict() for item in self.errors],
            "warnings": [item.to_dict() for item in self.warnings],
            "validator_version": self.validator_version,
        }
        if include_hash:
            result["validation_hash"] = self.validation_hash
        return result


@dataclass(frozen=True)
class CoverageEntry:
    target_kind: str
    target_id: str
    implementing_task_ids: tuple[str, ...] = ()
    verifying_task_ids: tuple[str, ...] = ()
    enabling_task_ids: tuple[str, ...] = ()
    documenting_task_ids: tuple[str, ...] = ()
    constrained_task_ids: tuple[str, ...] = ()
    omission: IntentionalOmission | None = None

    def to_dict(self) -> dict[str, Any]:
        return _record_dict(self)


@dataclass(frozen=True)
class CoverageIndex:
    entries: tuple[CoverageEntry, ...] = ()
    orphan_task_ids: tuple[str, ...] = ()
    duplicate_task_ids: tuple[str, ...] = ()
    missing_targets: tuple[str, ...] = ()
    invalid_omissions: tuple[str, ...] = ()

    @property
    def by_target(self) -> Mapping[tuple[str, str], CoverageEntry]:
        return MappingProxyType(
            {(item.target_kind, item.target_id): item for item in self.entries}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [item.to_dict() for item in self.entries],
            "orphan_task_ids": list(self.orphan_task_ids),
            "duplicate_task_ids": list(self.duplicate_task_ids),
            "missing_targets": list(self.missing_targets),
            "invalid_omissions": list(self.invalid_omissions),
        }


@dataclass(frozen=True)
class DependencyGraph:
    task_ids: tuple[str, ...]
    dependencies: tuple[Dependency, ...]
    topological_order: tuple[str, ...]
    critical_path: tuple[str, ...]
    critical_path_effort: int
    _predecessors: Mapping[str, tuple[str, ...]]
    _successors: Mapping[str, tuple[str, ...]]

    @property
    def has_cycle(self) -> bool:
        return len(self.topological_order) != len(self.task_ids)

    def predecessors(self, task_id: str) -> tuple[str, ...]:
        return self._predecessors.get(task_id, ())

    def successors(self, task_id: str) -> tuple[str, ...]:
        return self._successors.get(task_id, ())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_ids": list(self.task_ids),
            "dependencies": [item.to_dict() for item in self.dependencies],
            "topological_order": list(self.topological_order),
            "critical_path": list(self.critical_path),
            "critical_path_effort": self.critical_path_effort,
        }


@dataclass(frozen=True)
class StructuredTaskPlan:
    schema_version: str
    brief_ref: BriefReference
    input_manifest_ref: InputManifestReference
    tasks: tuple[Task, ...]
    dependencies: tuple[Dependency, ...]
    execution_groups: tuple[ExecutionGroup, ...]
    topology: Topology
    intentional_omissions: tuple[IntentionalOmission, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "schema_version", _text(self.schema_version, "schema_version")
        )
        object.__setattr__(
            self, "brief_ref", _coerce(BriefReference, self.brief_ref, "brief_ref")
        )
        object.__setattr__(
            self,
            "input_manifest_ref",
            _coerce(
                InputManifestReference, self.input_manifest_ref, "input_manifest_ref"
            ),
        )
        object.__setattr__(
            self, "tasks", tuple(_coerce(Task, item, "task") for item in self.tasks)
        )
        object.__setattr__(
            self,
            "dependencies",
            tuple(
                _coerce(Dependency, item, "dependency") for item in self.dependencies
            ),
        )
        object.__setattr__(
            self,
            "execution_groups",
            tuple(
                _coerce(ExecutionGroup, item, "execution_group")
                for item in self.execution_groups
            ),
        )
        object.__setattr__(
            self, "topology", _coerce(Topology, self.topology, "topology")
        )
        object.__setattr__(
            self,
            "intentional_omissions",
            tuple(
                _coerce(IntentionalOmission, item, "intentional_omission")
                for item in self.intentional_omissions
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        brief_ref: BriefReference | Mapping[str, Any],
        input_manifest_ref: InputManifestReference | Mapping[str, Any],
        tasks: Sequence[Task | Mapping[str, Any]],
        dependencies: Sequence[Dependency | Mapping[str, Any]] = (),
        execution_groups: Sequence[ExecutionGroup | Mapping[str, Any]] = (),
        intentional_omissions: Sequence[IntentionalOmission | Mapping[str, Any]] = (),
        schema_version: str = STRUCTURED_TASK_PLAN_SCHEMA_VERSION,
    ) -> "StructuredTaskPlan":
        """Canonicalize semantic records and assign all application-owned IDs."""

        candidate_tasks = tuple(_coerce(Task, item, "task") for item in tasks)
        if not candidate_tasks:
            raise StructuredTaskPlanSchemaError(
                "a Task Plan requires at least one Task"
            )
        candidate_dependencies = tuple(
            _coerce(Dependency, item, "dependency") for item in dependencies
        )
        candidate_groups = tuple(
            _coerce(ExecutionGroup, item, "execution_group")
            for item in execution_groups
        )
        omissions = tuple(
            _coerce(IntentionalOmission, item, "intentional_omission")
            for item in intentional_omissions
        )

        indexed_tasks = list(enumerate(candidate_tasks))
        indexed_tasks.sort(key=lambda item: _task_candidate_sort_key(item[1], item[0]))
        task_id_map: dict[str, str] = {}
        assigned_tasks: list[Task] = []
        for index, (original_index, task) in enumerate(indexed_tasks, 1):
            new_id = f"TASK-{index:03d}"
            if task.id:
                if task.id in task_id_map:
                    raise StructuredTaskPlanSchemaError(
                        f"duplicate candidate Task ID: {task.id}"
                    )
                task_id_map[task.id] = new_id
            task_id_map[f"#{original_index}"] = new_id
            assigned_tasks.append(_replace_task_id(task, new_id))

        resolved_groups: list[ExecutionGroup] = []
        for group in candidate_groups:
            resolved_ids = tuple(
                _resolve_task_reference(ref, task_id_map, len(candidate_tasks))
                for ref in group.task_ids
            )
            resolved_groups.append(_replace_group(group, id="", task_ids=resolved_ids))
        resolved_groups.sort(
            key=lambda item: (
                item.order,
                item.kind,
                item.task_ids,
                item.parallel_limit,
                item.skip_policy,
            )
        )
        groups: list[ExecutionGroup] = []
        for index, group in enumerate(resolved_groups, 1):
            groups.append(_replace_group(group, id=f"GROUP-{index:03d}"))

        resolved_dependencies: list[Dependency] = []
        for dependency in candidate_dependencies:
            prerequisite = _resolve_task_reference(
                dependency.prerequisite_task_id, task_id_map, len(candidate_tasks)
            )
            dependent = _resolve_task_reference(
                dependency.dependent_task_id, task_id_map, len(candidate_tasks)
            )
            resolved_dependencies.append(
                _replace_dependency(
                    dependency,
                    id="",
                    prerequisite_task_id=prerequisite,
                    dependent_task_id=dependent,
                )
            )
        # Sequential group order is canonical graph data, not hidden runtime
        # behavior.  Add missing ordering edges before dependency numbering.
        existing_edges = {
            (item.prerequisite_task_id, item.dependent_task_id)
            for item in resolved_dependencies
        }
        for group in groups:
            if group.kind != "sequential":
                continue
            for prerequisite, dependent in zip(group.task_ids, group.task_ids[1:]):
                if (prerequisite, dependent) not in existing_edges:
                    resolved_dependencies.append(
                        Dependency(
                            id="",
                            prerequisite_task_id=prerequisite,
                            dependent_task_id=dependent,
                            type="ordering",
                            reason=f"sequential execution group {group.id}",
                        )
                    )
                    existing_edges.add((prerequisite, dependent))
        resolved_dependencies.sort(
            key=lambda item: (
                item.prerequisite_task_id,
                item.dependent_task_id,
                item.type,
                item.reason,
            )
        )
        dependencies_with_ids = tuple(
            _replace_dependency(item, id=f"DEP-{index:03d}")
            for index, item in enumerate(resolved_dependencies, 1)
        )
        provisional = cls(
            schema_version=schema_version,
            brief_ref=_coerce(BriefReference, brief_ref, "brief_ref"),
            input_manifest_ref=_coerce(
                InputManifestReference, input_manifest_ref, "input_manifest_ref"
            ),
            tasks=tuple(assigned_tasks),
            dependencies=dependencies_with_ids,
            execution_groups=tuple(groups),
            topology=Topology(),
            intentional_omissions=omissions,
        )
        try:
            graph = build_dependency_graph(provisional)
            topology = Topology(graph.topological_order, graph.critical_path)
        except StructuredTaskPlanGraphError:
            topology = Topology()
        return cls(
            schema_version=provisional.schema_version,
            brief_ref=provisional.brief_ref,
            input_manifest_ref=provisional.input_manifest_ref,
            tasks=provisional.tasks,
            dependencies=provisional.dependencies,
            execution_groups=provisional.execution_groups,
            topology=topology,
            intentional_omissions=provisional.intentional_omissions,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "brief_ref": self.brief_ref.to_dict(),
            "input_manifest_ref": self.input_manifest_ref.to_dict(),
            "tasks": [item.to_dict() for item in self.tasks],
            "dependencies": [item.to_dict() for item in self.dependencies],
            "execution_groups": [item.to_dict() for item in self.execution_groups],
            "topology": self.topology.to_dict(),
            "intentional_omissions": [
                item.to_dict() for item in self.intentional_omissions
            ],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def canonical_json(self) -> str:
        return self.canonical_bytes().decode("utf-8")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def task_plan_hash(self) -> str:
        return self.content_hash

    @property
    def total_expected_effort(self) -> int:
        return sum(item.estimated_effort.expected for item in self.tasks)

    @property
    def graph(self) -> DependencyGraph:
        return build_dependency_graph(self)

    def coverage_index(self, brief: PlanningBrief | None = None) -> CoverageIndex:
        return build_coverage_index(self, brief=brief)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StructuredTaskPlan":
        if not isinstance(raw, Mapping):
            raise StructuredTaskPlanSchemaError(
                "Structured Task Plan must be an object"
            )
        allowed = {
            "schema_version",
            "brief_ref",
            "input_manifest_ref",
            "tasks",
            "dependencies",
            "execution_groups",
            "topology",
            "intentional_omissions",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise StructuredTaskPlanSchemaError(
                f"unknown Task Plan fields: {', '.join(unknown)}"
            )
        missing = sorted(allowed - set(raw))
        if missing:
            raise StructuredTaskPlanSchemaError(
                f"missing Task Plan fields: {', '.join(missing)}"
            )
        if (
            not isinstance(raw["tasks"], list)
            or not isinstance(raw["dependencies"], list)
            or not isinstance(raw["execution_groups"], list)
            or not isinstance(raw["intentional_omissions"], list)
        ):
            raise StructuredTaskPlanSchemaError("Task Plan collections must be arrays")
        return cls(
            schema_version=_text(raw["schema_version"], "schema_version"),
            brief_ref=_coerce(BriefReference, raw["brief_ref"], "brief_ref"),
            input_manifest_ref=_coerce(
                InputManifestReference, raw["input_manifest_ref"], "input_manifest_ref"
            ),
            tasks=tuple(_coerce(Task, item, "task") for item in raw["tasks"]),
            dependencies=tuple(
                _coerce(Dependency, item, "dependency") for item in raw["dependencies"]
            ),
            execution_groups=tuple(
                _coerce(ExecutionGroup, item, "execution_group")
                for item in raw["execution_groups"]
            ),
            topology=_coerce(Topology, raw["topology"], "topology"),
            intentional_omissions=tuple(
                _coerce(IntentionalOmission, item, "intentional_omission")
                for item in raw["intentional_omissions"]
            ),
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> "StructuredTaskPlan":
        try:
            raw = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise StructuredTaskPlanSchemaError("invalid Task Plan JSON") from exc
        return cls.from_dict(raw)


def _replace_task_id(task: Task, task_id: str) -> Task:
    return Task(
        task_id,
        task.title,
        task.objective,
        task.implementation_description,
        task.rationale,
        task.priority,
        task.complexity,
        task.estimated_effort,
        task.category,
        task.execution_profile,
        task.blocking_state,
        task.work_items,
        task.traceability,
    )


def _replace_dependency(dependency: Dependency, **changes: Any) -> Dependency:
    values = dependency.to_dict()
    values.update(changes)
    return Dependency(**values)


def _replace_group(group: ExecutionGroup, **changes: Any) -> ExecutionGroup:
    values = group.to_dict()
    values.update(changes)
    return ExecutionGroup(**values)


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def _task_candidate_sort_key(task: Task, original_index: int) -> tuple[Any, ...]:
    traceability_rank = {
        "goal": 0,
        "requirement": 1,
        "acceptance_criterion": 2,
        "constraint": 3,
        "scope": 4,
        "architecture_context": 5,
        "interface_contract": 6,
    }
    refs = tuple(
        sorted(
            (
                traceability_rank.get(item.target_kind, len(traceability_rank)),
                _normalized(item.target_id),
                _normalized(item.role),
            )
            for item in task.traceability
        )
    )
    work = tuple(
        tuple(
            _normalized(getattr(item, name))
            for name in ("target", "action", "deliverable", "done_when")
        )
        for item in task.work_items
    )
    if any(item.validation_contract is not None for item in task.work_items):
        work = tuple(
            item_key
            + (
                (
                    canonical_validation_hash(
                        task.work_items[index].validation_contract.to_dict()
                    )
                    if task.work_items[index].validation_contract is not None
                    else ""
                ),
            )
            for index, item_key in enumerate(work)
        )
    semantic = canonical_json_hash(
        {
            "objective": task.objective,
            "description": task.implementation_description,
            "category": task.category,
            "work_items": work,
            "traceability": [item.to_dict() for item in task.traceability],
        }
    )
    return (
        refs,
        TASK_CATEGORY_RANK.get(task.category, len(TASK_CATEGORIES)),
        _normalized(task.title),
        _normalized(task.objective),
        canonical_json_bytes(work),
        semantic,
        original_index,
    )


def _resolve_task_reference(
    reference: Any, task_id_map: Mapping[str, str], count: int
) -> str:
    if isinstance(reference, int) and not isinstance(reference, bool):
        if 0 <= reference < count:
            return task_id_map[f"#{reference}"]
        if 1 <= reference <= count:
            return task_id_map[f"#{reference - 1}"]
    normalized = _text(reference, "task reference")
    if normalized in task_id_map:
        return task_id_map[normalized]
    if normalized.startswith("TASK-"):
        return normalized
    raise StructuredTaskPlanSchemaError(
        f"unresolved candidate Task reference: {normalized}"
    )


def _task_key(task: Task) -> tuple[Any, ...]:
    payload = task.to_dict()
    payload.pop("id", None)
    return canonical_json_bytes(payload)


def _task_tie_key(task: Task, group_orders: Mapping[str, int]) -> tuple[Any, ...]:
    return (
        group_orders.get(task.id, 0),
        TASK_PRIORITY_RANK.get(task.priority, len(TASK_PRIORITY_RANK)),
        TASK_CATEGORY_RANK.get(task.category, len(TASK_CATEGORY_RANK)),
        _normalized(task.title),
        task.id,
    )


def detect_cycles(plan: StructuredTaskPlan) -> tuple[tuple[str, ...], ...]:
    """Return deterministic cycle paths, if any."""

    task_ids = {task.id for task in plan.tasks}
    adjacency: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
    for dependency in plan.dependencies:
        if (
            dependency.prerequisite_task_id in task_ids
            and dependency.dependent_task_id in task_ids
        ):
            adjacency[dependency.prerequisite_task_id].append(
                dependency.dependent_task_id
            )
    for values in adjacency.values():
        values.sort()
    state: dict[str, int] = {}
    stack: list[str] = []
    found: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for child in adjacency[node]:
            if state.get(child, 0) == 0:
                visit(child)
            elif state.get(child) == 1 and child in stack:
                start = stack.index(child)
                found.add(tuple(stack[start:] + [child]))
        stack.pop()
        state[node] = 2

    for task_id in sorted(task_ids):
        if state.get(task_id, 0) == 0:
            visit(task_id)
    return tuple(sorted(found))


def build_dependency_graph(plan: StructuredTaskPlan) -> DependencyGraph:
    """Build a validated DAG and calculate deterministic topology metrics."""

    task_by_id = {task.id: task for task in plan.tasks}
    if len(task_by_id) != len(plan.tasks):
        raise StructuredTaskPlanGraphError("Task IDs must be unique")
    adjacency: dict[str, list[Dependency]] = {task_id: [] for task_id in task_by_id}
    predecessors: dict[str, list[str]] = {task_id: [] for task_id in task_by_id}
    for dependency in plan.dependencies:
        if (
            dependency.prerequisite_task_id not in task_by_id
            or dependency.dependent_task_id not in task_by_id
        ):
            raise StructuredTaskPlanGraphError(
                f"dependency {dependency.id} references an unknown Task"
            )
        if dependency.prerequisite_task_id == dependency.dependent_task_id:
            raise StructuredTaskPlanGraphError(
                f"dependency {dependency.id} is a self-edge"
            )
        if dependency.type not in DEPENDENCY_TYPES:
            raise StructuredTaskPlanGraphError(
                f"dependency {dependency.id} has an unknown type"
            )
        adjacency[dependency.prerequisite_task_id].append(dependency)
        predecessors[dependency.dependent_task_id].append(
            dependency.prerequisite_task_id
        )
    cycles = detect_cycles(plan)
    if cycles:
        raise StructuredTaskPlanGraphError(
            "dependency cycle: " + " -> ".join(cycles[0])
        )
    group_orders = {
        task_id: group.order
        for group in plan.execution_groups
        for task_id in group.task_ids
    }
    ready = [task_id for task_id, values in predecessors.items() if not values]
    ready.sort(key=lambda task_id: _task_tie_key(task_by_id[task_id], group_orders))
    topological: list[str] = []
    remaining = {task_id: len(values) for task_id, values in predecessors.items()}
    while ready:
        node = ready.pop(0)
        topological.append(node)
        for edge in sorted(adjacency[node], key=lambda item: item.dependent_task_id):
            remaining[edge.dependent_task_id] -= 1
            if remaining[edge.dependent_task_id] == 0:
                ready.append(edge.dependent_task_id)
        ready.sort(key=lambda task_id: _task_tie_key(task_by_id[task_id], group_orders))

    critical_predecessors: dict[str, list[str]] = {
        task_id: [] for task_id in task_by_id
    }
    for dependency in plan.dependencies:
        if dependency.type in CRITICAL_PATH_DEPENDENCY_TYPES:
            critical_predecessors[dependency.dependent_task_id].append(
                dependency.prerequisite_task_id
            )
    best: dict[str, tuple[int, tuple[str, ...]]] = {}
    for task_id in topological:
        candidates = [(0, (task_id,))]
        for parent in critical_predecessors[task_id]:
            if parent in best:
                effort, path = best[parent]
                candidates.append((effort, path + (task_id,)))
        best[task_id] = _best_path(
            candidates, task_by_id[task_id].estimated_effort.expected
        )
    if best:
        critical_effort = max(item[0] for item in best.values())
        critical_path = min(
            (path for effort, path in best.values() if effort == critical_effort),
            key=lambda item: item,
        )
    else:
        critical_effort, critical_path = 0, ()
    return DependencyGraph(
        task_ids=tuple(task.id for task in plan.tasks),
        dependencies=plan.dependencies,
        topological_order=tuple(topological),
        critical_path=tuple(critical_path),
        critical_path_effort=critical_effort,
        _predecessors=MappingProxyType(
            {key: tuple(sorted(value)) for key, value in predecessors.items()}
        ),
        _successors=MappingProxyType(
            {
                key: tuple(sorted(edge.dependent_task_id for edge in value))
                for key, value in adjacency.items()
            }
        ),
    )


def _best_path(
    candidates: Sequence[tuple[int, tuple[str, ...]]], own_effort: int
) -> tuple[int, tuple[str, ...]]:
    best_effort, best_path = candidates[0]
    best_effort += own_effort
    for effort, path in candidates[1:]:
        candidate_effort = effort + own_effort
        if candidate_effort > best_effort or (
            candidate_effort == best_effort and path < best_path
        ):
            best_effort, best_path = candidate_effort, path
    return best_effort, best_path


def _brief_target_records(brief: PlanningBrief) -> dict[tuple[str, str], Any]:
    result: dict[tuple[str, str], Any] = {("goal", brief.objective.id): brief.objective}
    for collection_name, kind in (
        ("requirements", "requirement"),
        ("constraints", "constraint"),
        ("acceptance_criteria", "acceptance_criterion"),
        ("architecture_context", "architecture_context"),
        ("interface_contracts", "interface_contract"),
        ("scope", "scope"),
    ):
        for item in getattr(brief, collection_name):
            result[(kind, item.id)] = item
    return result


def build_coverage_index(
    plan: StructuredTaskPlan, *, brief: PlanningBrief | None = None
) -> CoverageIndex:
    """Build a deterministic, read-only coverage index from Task traceability."""

    target_records = _brief_target_records(brief) if brief is not None else {}
    covered: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    orphan: set[str] = set()
    for task in plan.tasks:
        if not task.traceability:
            orphan.add(task.id)
        for reference in task.traceability:
            key = (reference.target_kind, reference.target_id)
            if reference.target_kind not in TRACEABILITY_TARGET_KINDS or (
                brief is not None and key not in target_records
            ):
                orphan.add(task.id)
            covered[key][reference.role].append(task.id)
    fingerprint_to_ids: dict[bytes, list[str]] = defaultdict(list)
    for task in plan.tasks:
        fingerprint_to_ids[_task_key(task)].append(task.id)
    duplicates = {
        task_id
        for ids in fingerprint_to_ids.values()
        if len(ids) > 1
        for task_id in ids
    }

    omissions_by_target = {
        (item.target_kind, item.target_id): item for item in plan.intentional_omissions
    }
    all_targets = set(target_records) | set(covered) | set(omissions_by_target)
    entries: list[CoverageEntry] = []
    for kind, target_id in sorted(all_targets):
        roles = covered.get((kind, target_id), {})
        entries.append(
            CoverageEntry(
                target_kind=kind,
                target_id=target_id,
                implementing_task_ids=tuple(sorted(roles.get("implements", []))),
                verifying_task_ids=tuple(sorted(roles.get("verifies", []))),
                enabling_task_ids=tuple(sorted(roles.get("enables", []))),
                documenting_task_ids=tuple(sorted(roles.get("documents", []))),
                constrained_task_ids=tuple(sorted(roles.get("constrained_by", []))),
                omission=omissions_by_target.get((kind, target_id)),
            )
        )
    missing: set[str] = set()
    invalid_omissions: set[str] = set()
    if brief is not None:
        for (kind, target_id), record in sorted(target_records.items()):
            entry = next(
                item
                for item in entries
                if (item.target_kind, item.target_id) == (kind, target_id)
            )
            if kind == "goal":
                mapped = bool(
                    entry.implementing_task_ids
                    or entry.enabling_task_ids
                    or entry.verifying_task_ids
                )
            elif kind == "requirement":
                mapped = bool(entry.implementing_task_ids or entry.enabling_task_ids)
            elif kind == "acceptance_criterion":
                mapped = bool(entry.implementing_task_ids or entry.verifying_task_ids)
            elif kind == "constraint":
                mapped = bool(entry.constrained_task_ids)
            else:
                mapped = bool(
                    entry.implementing_task_ids
                    or entry.enabling_task_ids
                    or entry.verifying_task_ids
                    or entry.documenting_task_ids
                    or entry.constrained_task_ids
                )
            omission = entry.omission
            required = (
                (
                    kind == "requirement"
                    and getattr(record, "priority", None) == "required"
                )
                or (
                    kind == "acceptance_criterion"
                    and getattr(record, "criticality", None) == "required"
                )
                or (
                    kind == "constraint" and getattr(record, "severity", None) == "must"
                )
                or kind == "goal"
            )
            requires_coverage = kind in {
                "goal",
                "requirement",
                "acceptance_criterion",
                "constraint",
            }
            if requires_coverage and not mapped and (required or omission is None):
                missing.add(f"{kind}:{target_id}")
            if omission is not None:
                if (
                    required
                    or omission.target_kind not in OMISSION_TARGET_KINDS
                    or omission.reason_code not in OMISSION_REASON_CODES
                ):
                    invalid_omissions.add(f"{kind}:{target_id}")
                if omission.brief_scope_or_decision_id not in {
                    item.id for item in brief.scope
                } | {item.id for item in brief.operator_decisions}:
                    invalid_omissions.add(f"{kind}:{target_id}")
    for omission in plan.intentional_omissions:
        if (
            omission.target_kind,
            omission.target_id,
        ) not in target_records and brief is not None:
            invalid_omissions.add(f"{omission.target_kind}:{omission.target_id}")
    return CoverageIndex(
        entries=tuple(entries),
        orphan_task_ids=tuple(sorted(orphan)),
        duplicate_task_ids=tuple(sorted(duplicates)),
        missing_targets=tuple(sorted(missing)),
        invalid_omissions=tuple(sorted(invalid_omissions)),
    )


def _issue(
    code: str, path: str, message: str, severity: str = "error"
) -> ValidationIssue:
    return ValidationIssue(code, path, message, severity)


def validate_structured_task_plan(
    plan: StructuredTaskPlan,
    *,
    brief: PlanningBrief | None = None,
    input_manifest: Any | None = None,
    policy: Mapping[str, Any] | None = None,
) -> StructuredTaskPlanValidation:
    """Run schema, reference, graph, group, effort, coverage, and hash checks."""

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    limits = dict(DEFAULT_TASK_PLAN_POLICY)
    if policy is not None:
        for key, value in policy.items():
            if key in limits:
                limits[key] = value
    if plan.schema_version != STRUCTURED_TASK_PLAN_SCHEMA_VERSION:
        errors.append(
            _issue(
                "unsupported_schema", "schema_version", "unsupported Task Plan schema"
            )
        )
    for name, value in (
        ("brief_ref.checkpoint_id", plan.brief_ref.checkpoint_id),
        ("input_manifest_ref.id", plan.input_manifest_ref.id),
    ):
        if not value.strip():
            errors.append(
                _issue("missing_reference", name, "lineage reference is required")
            )
    if not _HASH_RE.fullmatch(plan.brief_ref.content_hash):
        errors.append(
            _issue(
                "invalid_hash",
                "brief_ref.content_hash",
                "Brief reference hash must be SHA-256",
            )
        )
    if not _HASH_RE.fullmatch(plan.input_manifest_ref.hash):
        errors.append(
            _issue(
                "invalid_hash",
                "input_manifest_ref.hash",
                "Input Manifest reference hash must be SHA-256",
            )
        )
    if len(plan.tasks) < 1 or len(plan.tasks) > int(limits["max_tasks"]):
        errors.append(
            _issue(
                "task_count_limit", "tasks", "Task count is outside configured limits"
            )
        )
    if len(plan.execution_groups) > int(limits["max_groups"]):
        errors.append(
            _issue(
                "group_count_limit",
                "execution_groups",
                "execution-group count exceeds configured limit",
            )
        )
    task_ids = [task.id for task in plan.tasks]
    if any(not _ID_PATTERNS["TASK"].fullmatch(task_id) for task_id in task_ids):
        errors.append(
            _issue(
                "invalid_id", "tasks", "Tasks require application-owned TASK-NNN IDs"
            )
        )
    if len(set(task_ids)) != len(task_ids):
        errors.append(_issue("duplicate_id", "tasks", "Task IDs must be unique"))
    dependency_ids = [item.id for item in plan.dependencies]
    if any(not _ID_PATTERNS["DEP"].fullmatch(item) for item in dependency_ids) or len(
        set(dependency_ids)
    ) != len(dependency_ids):
        errors.append(
            _issue(
                "invalid_id",
                "dependencies",
                "Dependency IDs must be unique DEP-NNN values",
            )
        )
    group_ids = [item.id for item in plan.execution_groups]
    if any(not _ID_PATTERNS["GROUP"].fullmatch(item) for item in group_ids) or len(
        set(group_ids)
    ) != len(group_ids):
        errors.append(
            _issue(
                "invalid_id",
                "execution_groups",
                "Group IDs must be unique GROUP-NNN values",
            )
        )

    task_by_id = {task.id: task for task in plan.tasks}
    seen_edges: set[tuple[str, str, str, str]] = set()
    seen_edge_pairs: set[tuple[str, str]] = set()
    for index, dependency in enumerate(plan.dependencies):
        path = f"dependencies[{index}]"
        if (
            dependency.prerequisite_task_id not in task_by_id
            or dependency.dependent_task_id not in task_by_id
        ):
            errors.append(
                _issue(
                    "unresolved_reference",
                    path,
                    "dependency references an unknown Task",
                )
            )
        if dependency.prerequisite_task_id == dependency.dependent_task_id:
            errors.append(
                _issue("self_edge", path, "dependency cannot reference the same Task")
            )
        if dependency.type not in DEPENDENCY_TYPES:
            errors.append(
                _issue(
                    "invalid_dependency_type", path, "dependency type is not supported"
                )
            )
        if not dependency.reason.strip() and dependency.type in {
            "artifact_ready",
            "review_gate",
            "resource_serialization",
        }:
            errors.append(
                _issue(
                    "missing_dependency_reason",
                    path,
                    "this dependency type requires a reason",
                )
            )
        edge = (
            dependency.prerequisite_task_id,
            dependency.dependent_task_id,
            dependency.type,
            _normalized(dependency.reason),
        )
        edge_pair = (
            dependency.prerequisite_task_id,
            dependency.dependent_task_id,
        )
        if edge in seen_edges or edge_pair in seen_edge_pairs:
            errors.append(
                _issue("duplicate_dependency", path, "duplicate dependency edge")
            )
        seen_edges.add(edge)
        seen_edge_pairs.add(edge_pair)
    cycles = detect_cycles(plan)
    if cycles:
        errors.append(
            _issue(
                "dependency_cycle",
                "dependencies",
                "cycle detected: " + " -> ".join(cycles[0]),
            )
        )

    group_by_task: dict[str, ExecutionGroup] = {}
    group_orders: dict[str, int] = {}
    for index, group in enumerate(plan.execution_groups):
        path = f"execution_groups[{index}]"
        if group.kind not in GROUP_KINDS:
            errors.append(
                _issue(
                    "invalid_group_kind", path, "execution-group kind is not supported"
                )
            )
        if group.skip_policy not in GROUP_SKIP_POLICIES:
            errors.append(
                _issue(
                    "invalid_skip_policy",
                    path,
                    "execution-group skip policy is not supported",
                )
            )
        if group.order < 0 or group.parallel_limit < 1:
            errors.append(
                _issue(
                    "invalid_group_limits",
                    path,
                    "group order/parallel limit is invalid",
                )
            )
        if group.parallel_limit > int(limits["max_parallel_width"]):
            errors.append(
                _issue(
                    "parallel_width_limit",
                    path,
                    "parallel limit exceeds configured width",
                )
            )
        if not group.task_ids:
            errors.append(
                _issue("empty_group", path, "accepted groups cannot be empty")
            )
        if len(set(group.task_ids)) != len(group.task_ids):
            errors.append(
                _issue("duplicate_group_member", path, "group members must be unique")
            )
        if group.kind == "parallel" and group.parallel_limit > len(group.task_ids):
            errors.append(
                _issue("parallel_limit", path, "parallel limit exceeds member count")
            )
        for task_id in group.task_ids:
            if task_id not in task_by_id:
                errors.append(
                    _issue("unresolved_reference", path, f"unknown Task {task_id}")
                )
            elif task_id in group_by_task:
                errors.append(
                    _issue(
                        "multiple_group_membership",
                        path,
                        f"Task {task_id} belongs to multiple groups",
                    )
                )
            else:
                group_by_task[task_id] = group
                group_orders[task_id] = group.order
        if group.kind == "optional" and any(
            task_by_id.get(task_id) is not None
            and task_by_id[task_id].priority == "required"
            for task_id in group.task_ids
        ):
            errors.append(
                _issue(
                    "required_in_optional_group",
                    path,
                    "optional groups cannot contain required Tasks",
                )
            )
        if group.kind == "review_gate" and any(
            task_by_id.get(task_id) is not None
            and task_by_id[task_id].category not in {"review", "operator_action"}
            for task_id in group.task_ids
        ):
            errors.append(
                _issue(
                    "invalid_review_group",
                    path,
                    "review gates contain only review or operator_action Tasks",
                )
            )
        if group.kind == "verification" and any(
            task_by_id.get(task_id) is not None
            and task_by_id[task_id].category not in {"test", "verification", "review"}
            for task_id in group.task_ids
        ):
            errors.append(
                _issue(
                    "invalid_verification_group",
                    path,
                    "verification groups contain only verification categories",
                )
            )
    edge_pairs = {
        (item.prerequisite_task_id, item.dependent_task_id)
        for item in plan.dependencies
    }
    for group in plan.execution_groups:
        if group.kind == "sequential":
            for prerequisite, dependent in zip(group.task_ids, group.task_ids[1:]):
                matching_edges = [
                    item
                    for item in plan.dependencies
                    if item.prerequisite_task_id == prerequisite
                    and item.dependent_task_id == dependent
                ]
                if not matching_edges or not any(
                    item.type in {"ordering", "hard_completion"}
                    for item in matching_edges
                ):
                    errors.append(
                        _issue(
                            "hidden_group_dependency",
                            f"execution_groups.{group.id}",
                            "sequential members require explicit graph edges",
                        )
                    )
        if group.kind == "parallel":
            if any(
                (a, b) in edge_pairs or (b, a) in edge_pairs
                for a in group.task_ids
                for b in group.task_ids
                if a != b
            ):
                errors.append(
                    _issue(
                        "parallel_dependency",
                        f"execution_groups.{group.id}",
                        "parallel group members cannot depend on one another",
                    )
                )
            targets: dict[str, list[Task]] = defaultdict(list)
            for task_id in group.task_ids:
                task = task_by_id.get(task_id)
                if task is not None:
                    for item in task.work_items:
                        targets[_normalized(item.target)].append(task)
            for target, tasks in targets.items():
                if len(tasks) > 1 and any(
                    task.execution_profile.write_scope != "none"
                    and task.execution_profile.parallelism != "read_only"
                    for task in tasks
                ):
                    errors.append(
                        _issue(
                            "parallel_target_conflict",
                            f"execution_groups.{group.id}",
                            f"parallel Tasks conflict on target {target}",
                        )
                    )

    for index, task in enumerate(plan.tasks):
        path = f"tasks[{index}]"
        for field_name in (
            "title",
            "objective",
            "implementation_description",
            "rationale",
        ):
            value = getattr(task, field_name)
            if len(value) > 2_000:
                errors.append(
                    _issue(
                        "text_size_limit",
                        f"{path}.{field_name}",
                        "Task text exceeds the canonical field limit",
                    )
                )
            if _UNSAFE_MARKUP_RE.search(value) or _CONTROL_RE.search(value):
                errors.append(
                    _issue(
                        "unsafe_text",
                        f"{path}.{field_name}",
                        "Task text contains unsafe markup/control characters",
                    )
                )
        if task.priority not in TASK_PRIORITIES:
            errors.append(_issue("invalid_priority", path, "priority is not supported"))
        if task.complexity not in TASK_COMPLEXITIES:
            errors.append(
                _issue("invalid_complexity", path, "complexity is not supported")
            )
        if task.category not in TASK_CATEGORIES:
            errors.append(_issue("invalid_category", path, "category is not supported"))
        if task.blocking_state not in BLOCKING_STATES:
            errors.append(
                _issue(
                    "invalid_blocking_state", path, "blocking_state is not supported"
                )
            )
        profile = task.execution_profile
        for name, allowed in (
            ("owner_role", EXECUTION_OWNER_ROLES),
            ("isolation", ISOLATION_MODES),
            ("write_scope", WRITE_SCOPES),
            ("network", NETWORK_MODES),
            ("parallelism", PARALLELISM_MODES),
            ("review", REVIEW_MODES),
        ):
            if getattr(profile, name) not in allowed:
                errors.append(
                    _issue(
                        "invalid_execution_profile",
                        f"{path}.execution_profile.{name}",
                        "execution profile value is not supported",
                    )
                )
        if task.category == "operator_action" and profile.owner_role == "agent":
            errors.append(
                _issue(
                    "operator_action_owner",
                    path,
                    "operator_action cannot be auto-owned by an agent",
                )
            )
        if not task.work_items or len(task.work_items) > int(
            limits["max_work_items_per_task"]
        ):
            errors.append(
                _issue(
                    "work_item_limit",
                    path,
                    "Task requires a bounded non-empty work-item list",
                )
            )
        if task.complexity == "very_large" and not task.rationale.strip():
            errors.append(
                _issue(
                    "atomicity_rationale",
                    path,
                    "very_large Tasks require an atomicity rationale",
                )
            )
        effort = task.estimated_effort
        if (
            effort.unit not in EFFORT_UNITS
            or effort.confidence not in CONFIDENCE_VALUES
            or not (0 < effort.lower <= effort.expected <= effort.upper)
        ):
            errors.append(
                _issue(
                    "invalid_effort",
                    f"{path}.estimated_effort",
                    "effort must satisfy 0 < lower <= expected <= upper",
                )
            )
        if effort.expected > int(limits["max_expected_effort"]):
            errors.append(
                _issue(
                    "effort_cap", path, "Task expected effort exceeds configured cap"
                )
            )
        fan_in = sum(
            1
            for dependency in plan.dependencies
            if dependency.dependent_task_id == task.id
        )
        fan_out = sum(
            1
            for dependency in plan.dependencies
            if dependency.prerequisite_task_id == task.id
        )
        max_fan_in = int(
            (policy or {}).get("max_dependency_fan_in", limits["max_dependency_fan_in"])
        )
        max_fan_out = int(
            (policy or {}).get(
                "max_dependency_fan_out", limits["max_dependency_fan_out"]
            )
        )
        if fan_in > max_fan_in or fan_out > max_fan_out:
            errors.append(
                _issue(
                    "dependency_fan_limit",
                    path,
                    "Task dependency fan-in/fan-out exceeds configured limit",
                )
            )
        for item_index, item in enumerate(task.work_items):
            if (
                _UNSAFE_TARGET_RE.search(item.target)
                or _UNSAFE_MARKUP_RE.search(item.target)
                or _CONTROL_RE.search(item.target)
            ):
                errors.append(
                    _issue(
                        "unsafe_target",
                        f"{path}.work_items[{item_index}].target",
                        "work-item target is not a safe project-relative target",
                    )
                )
            for name in ("action", "deliverable", "done_when"):
                if _UNSAFE_MARKUP_RE.search(getattr(item, name)) or _CONTROL_RE.search(
                    getattr(item, name)
                ):
                    errors.append(
                        _issue(
                            "unsafe_text",
                            f"{path}.work_items[{item_index}].{name}",
                            "work-item text contains unsafe markup/control characters",
                        )
                    )
        for ref_index, reference in enumerate(task.traceability):
            if (
                reference.target_kind not in TRACEABILITY_TARGET_KINDS
                or reference.role not in TRACEABILITY_ROLES
            ):
                errors.append(
                    _issue(
                        "invalid_traceability",
                        f"{path}.traceability[{ref_index}]",
                        "traceability target or role is not supported",
                    )
                )
            if brief is None:
                expected_prefix = {
                    "goal": "GOAL-",
                    "requirement": "REQ-",
                    "acceptance_criterion": "AC-",
                    "constraint": "CON-",
                    "architecture_context": "ARCH-",
                    "interface_contract": "IFACE-",
                    "scope": "SCOPE-",
                }.get(reference.target_kind)
                if expected_prefix is not None and not reference.target_id.startswith(
                    expected_prefix
                ):
                    errors.append(
                        _issue(
                            "invalid_traceability",
                            f"{path}.traceability[{ref_index}].target_id",
                            "traceability target ID does not match its target kind",
                        )
                    )
            elif reference.target_kind == "scope":
                scope = next(
                    (item for item in brief.scope if item.id == reference.target_id),
                    None,
                )
                if (
                    scope is not None
                    and scope.classification
                    in {"prohibited", "out_of_scope", "deferred"}
                    and not (
                        task.category == "verification" and reference.role == "verifies"
                    )
                ):
                    errors.append(
                        _issue(
                            "prohibited_scope",
                            f"{path}.traceability[{ref_index}]",
                            "Task targets excluded or deferred scope without verification-only purpose",
                        )
                    )
        if task.category in {"test", "verification", "review"} and not any(
            item.role == "verifies" for item in task.traceability
        ):
            errors.append(
                _issue(
                    "verification_traceability",
                    path,
                    "verification Tasks must verify a Brief obligation",
                )
            )

    coverage = build_coverage_index(plan, brief=brief)
    for task_id in coverage.orphan_task_ids:
        errors.append(
            _issue(
                "orphan_task",
                f"tasks.{task_id}",
                "Task has no valid Brief traceability",
            )
        )
    for task_id in coverage.duplicate_task_ids:
        errors.append(
            _issue(
                "duplicate_implementation",
                f"tasks.{task_id}",
                "Task is an exact normalized duplicate",
            )
        )
    for target in coverage.missing_targets:
        errors.append(
            _issue(
                "missing_coverage",
                "coverage",
                f"required Brief target is not covered: {target}",
            )
        )
    for target in coverage.invalid_omissions:
        errors.append(
            _issue(
                "invalid_omission",
                "intentional_omissions",
                f"invalid intentional omission: {target}",
            )
        )
    if brief is not None:
        if plan.brief_ref.content_hash != brief.content_hash:
            errors.append(
                _issue(
                    "brief_hash_mismatch",
                    "brief_ref.content_hash",
                    "Task Plan is not bound to the supplied Brief",
                )
            )
        if (
            plan.input_manifest_ref.id != brief.input_manifest_ref.id
            or plan.input_manifest_ref.hash != brief.input_manifest_ref.hash
        ):
            errors.append(
                _issue(
                    "manifest_hash_mismatch",
                    "input_manifest_ref",
                    "Task Plan is not bound to the Brief's Input Manifest",
                )
            )
        blocking_questions = [
            item.id
            for item in brief.unresolved_questions
            if item.classification in {"blocking", "operator_decision_required"}
        ]
        if blocking_questions:
            errors.append(
                _issue(
                    "brief_not_acceptable",
                    "brief",
                    "accepted Task Plans require a Brief without blocking questions",
                )
            )
    if input_manifest is not None:
        if (
            getattr(input_manifest, "manifest_id", None) != plan.input_manifest_ref.id
            or getattr(input_manifest, "manifest_hash", None)
            != plan.input_manifest_ref.hash
        ):
            errors.append(
                _issue(
                    "manifest_hash_mismatch",
                    "input_manifest_ref",
                    "Input Manifest identity does not match the Task Plan",
                )
            )

    try:
        graph = build_dependency_graph(plan)
    except StructuredTaskPlanGraphError:
        graph = None
    if graph is not None:
        if plan.topology.topological_order != graph.topological_order:
            errors.append(
                _issue(
                    "topology_mismatch",
                    "topology.topological_order",
                    "stored topological order is not deterministic",
                )
            )
        if plan.topology.critical_path != graph.critical_path:
            errors.append(
                _issue(
                    "critical_path_mismatch",
                    "topology.critical_path",
                    "stored critical path is not deterministic",
                )
            )
    canonical_bytes = plan.canonical_bytes()
    if len(canonical_bytes) > int(limits["max_plan_bytes"]):
        errors.append(
            _issue(
                "size_limit",
                "plan",
                "canonical Task Plan exceeds configured size limit",
            )
        )
    if not errors and any(task.priority == "optional" for task in plan.tasks):
        warnings.append(
            _issue("optional_tasks", "tasks", "plan contains optional Tasks", "warning")
        )

    # Automatic acceptance is deliberately conservative.  These findings do
    # not mutate the immutable plan and do not make it semantically invalid;
    # they keep a validated candidate out of the accepted checkpoint path until
    # a future operator-approval mechanism exists.
    if any(task.category == "operator_action" for task in plan.tasks):
        warnings.append(
            _issue(
                "acceptance_policy_operator_action",
                "tasks",
                "operator_action Tasks require operator approval",
                "review_required",
            )
        )
    if any(
        task.blocking_state == "review_required" or task.category == "review"
        for task in plan.tasks
    ):
        warnings.append(
            _issue(
                "acceptance_policy_review_required_task",
                "tasks",
                "review_required Tasks require operator approval",
                "review_required",
            )
        )
    if any(
        group.kind == "review_gate" and group.skip_policy == "not_skippable"
        for group in plan.execution_groups
    ) or any(item.type == "review_gate" for item in plan.dependencies):
        warnings.append(
            _issue(
                "acceptance_policy_review_gate",
                "execution_groups",
                "blocking review gates require operator approval",
                "review_required",
            )
        )
    if any(task.complexity == "very_large" for task in plan.tasks):
        warnings.append(
            _issue(
                "atomicity_exception",
                "tasks",
                "very_large Tasks require atomicity review",
                "review_required",
            )
        )
    for field_name in ("objective",):
        by_value: dict[str, list[str]] = defaultdict(list)
        for task in plan.tasks:
            by_value[_normalized(getattr(task, field_name))].append(task.id)
        overlaps = [
            (value, tuple(sorted(task_ids)))
            for value, task_ids in by_value.items()
            if value and len(task_ids) > 1
        ]
        for value, task_ids in sorted(overlaps):
            warnings.append(
                _issue(
                    "semantic_overlap",
                    "tasks",
                    f"unresolved semantic overlap for {field_name}: {','.join(task_ids)}",
                    "review_required",
                )
            )
    if (policy or {}).get("auto_accept", True) is False:
        warnings.append(
            _issue(
                "acceptance_policy_disabled",
                "policy",
                "automatic acceptance is disabled by stage policy",
                "review_required",
            )
        )
    schema_error_codes = {
        "unsupported_schema",
        "invalid_hash",
        "invalid_id",
        "duplicate_id",
        "missing_reference",
        "size_limit",
    }
    schema_valid = not any(item.code in schema_error_codes for item in errors)
    semantically_valid = not errors
    result = StructuredTaskPlanValidation(
        schema_valid=schema_valid,
        semantically_valid=semantically_valid,
        protocol_acceptable=semantically_valid
        and not any(item.severity == "review_required" for item in warnings),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
    return result


def require_valid_structured_task_plan(
    plan: StructuredTaskPlan,
    *,
    brief: PlanningBrief | None = None,
    input_manifest: Any | None = None,
    policy: Mapping[str, Any] | None = None,
) -> StructuredTaskPlanValidation:
    result = validate_structured_task_plan(
        plan, brief=brief, input_manifest=input_manifest, policy=policy
    )
    if not result.semantically_valid:
        detail = "; ".join(f"{item.code}: {item.path}" for item in result.errors[:8])
        raise StructuredTaskPlanValidationError(
            detail or "Structured Task Plan is invalid"
        )
    return result


def _escape(value: Any) -> str:
    text = str(value)
    text = text.replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "#", "|"):
        text = text.replace(character, "\\" + character)
    return html.escape(text, quote=False)


def render_structured_task_plan(
    plan: StructuredTaskPlan,
    *,
    renderer_version: str = STRUCTURED_TASK_PLAN_RENDERER_VERSION,
) -> str:
    """Render canonical Markdown.  The result is never parsed as authority."""

    lines = [
        "# Structured Task Plan",
        "",
        f"- Schema: `{_escape(plan.schema_version)}`",
        f"- Source plan hash: `{_escape(plan.content_hash)}`",
        f"- Renderer: `{_escape(renderer_version)}`",
        f"- Brief checkpoint: `{_escape(plan.brief_ref.checkpoint_id)}`",
        f"- Brief hash: `{_escape(plan.brief_ref.content_hash)}`",
        f"- Input Manifest: `{_escape(plan.input_manifest_ref.id)}` (`{_escape(plan.input_manifest_ref.hash)}`)",
        "",
        "## Topology",
        "",
        f"- Topological order: {', '.join(f'`{_escape(item)}`' for item in plan.topology.topological_order) or '_None._'}",
        f"- Critical path: {', '.join(f'`{_escape(item)}`' for item in plan.topology.critical_path) or '_None._'}",
        f"- Expected effort: {plan.total_expected_effort} person-minutes",
        "",
        "## Tasks",
        "",
    ]
    ordered_ids = plan.topology.topological_order or tuple(
        item.id for item in plan.tasks
    )
    task_by_id = {item.id: item for item in plan.tasks}
    for task_id in ordered_ids:
        task = task_by_id[task_id]
        lines.extend(
            [
                f"### {_escape(task.id)} — {_escape(task.title)}",
                "",
                f"- Objective: {_escape(task.objective)}",
                f"- Category: `{_escape(task.category)}`; priority: `{_escape(task.priority)}`; complexity: `{_escape(task.complexity)}`",
                f"- Effort: {task.estimated_effort.lower}/{task.estimated_effort.expected}/{task.estimated_effort.upper} {task.estimated_effort.unit} ({_escape(task.estimated_effort.confidence)})",
                f"- Execution: `{_escape(task.execution_profile.owner_role)}` / `{_escape(task.execution_profile.isolation)}` / `{_escape(task.execution_profile.write_scope)}`",
                f"- Blocking state: `{_escape(task.blocking_state)}`",
                f"- Description: {_escape(task.implementation_description)}",
                f"- Rationale: {_escape(task.rationale)}",
                "- Work items:",
            ]
        )
        for item in task.work_items:
            lines.append(
                f"  - {_escape(item.action)} → `{_escape(item.target)}` — {_escape(item.deliverable)}; done when: {_escape(item.done_when)}"
            )
        lines.append("- Traceability:")
        for reference in task.traceability:
            lines.append(
                f"  - `{_escape(reference.target_kind)}:{_escape(reference.target_id)}` ({_escape(reference.role)})"
            )
        lines.append("")
    lines.extend(["## Dependencies", ""])
    for dependency in plan.dependencies:
        lines.append(
            f"- `{_escape(dependency.id)}`: `{_escape(dependency.prerequisite_task_id)}` → `{_escape(dependency.dependent_task_id)}` ({_escape(dependency.type)}) — {_escape(dependency.reason)}"
        )
    if not plan.dependencies:
        lines.append("_None._")
    lines.extend(["", "## Execution groups", ""])
    for group in plan.execution_groups:
        lines.append(
            f"- `{_escape(group.id)}` order {group.order}, `{_escape(group.kind)}`, members: {', '.join(f'`{_escape(item)}`' for item in group.task_ids)}; parallel limit {group.parallel_limit}; `{_escape(group.skip_policy)}`"
        )
    if not plan.execution_groups:
        lines.append("_None._")
    lines.extend(["", "## Coverage", ""])
    coverage = plan.coverage_index()
    for entry in coverage.entries:
        mapped = list(
            entry.implementing_task_ids
            + entry.enabling_task_ids
            + entry.verifying_task_ids
            + entry.documenting_task_ids
            + entry.constrained_task_ids
        )
        omission = f"; omitted: {entry.omission.reason_code}" if entry.omission else ""
        lines.append(
            f"- `{_escape(entry.target_kind)}:{_escape(entry.target_id)}` → {', '.join(f'`{_escape(item)}`' for item in sorted(set(mapped))) or '_unmapped_'}{_escape(omission)}"
        )
    if not coverage.entries:
        lines.append("_None._")
    lines.extend(["", "## Intentional omissions", ""])
    for omission in plan.intentional_omissions:
        lines.append(
            f"- `{_escape(omission.target_kind)}:{_escape(omission.target_id)}` — `{_escape(omission.reason_code)}` via `{_escape(omission.brief_scope_or_decision_id)}`"
        )
    if not plan.intentional_omissions:
        lines.append("_None._")
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class StructuredTaskPlanCompatibilityProjection:
    source_plan_hash: str
    renderer_version: str
    markdown: str
    requirements: str
    design: str
    implementation_plan: str
    planner_markdown: str

    @property
    def projection_hashes(self) -> Mapping[str, str]:
        return MappingProxyType(
            {name: canonical_json_hash(value) for name, value in self.values().items()}
        )

    def values(self) -> dict[str, str]:
        return {
            "markdown": self.markdown,
            "requirements": self.requirements,
            "design": self.design,
            "implementation_plan": self.implementation_plan,
            "planner_markdown": self.planner_markdown,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_plan_hash": self.source_plan_hash,
            "renderer_version": self.renderer_version,
            "projections": self.values(),
            "projection_hashes": dict(self.projection_hashes),
        }


def project_structured_task_plan(
    plan: StructuredTaskPlan,
    *,
    renderer_version: str = STRUCTURED_TASK_PLAN_RENDERER_VERSION,
) -> StructuredTaskPlanCompatibilityProjection:
    markdown = render_structured_task_plan(plan, renderer_version=renderer_version)
    requirements = ["# Requirements", ""]
    design = ["# Design", ""]
    implementation = ["# Implementation Plan", ""]
    task_by_id = {item.id: item for item in plan.tasks}
    for task_id in plan.topology.topological_order or tuple(
        item.id for item in plan.tasks
    ):
        task = task_by_id[task_id]
        refs = ", ".join(
            f"{item.target_kind}:{item.target_id}" for item in task.traceability
        )
        requirements.append(
            f"- **{_escape(task.id)}** — {_escape(task.objective)} ({_escape(refs)})"
        )
        design.append(
            f"- **{_escape(task.id)}** — {_escape(task.implementation_description)}"
        )
        implementation.append(
            f"{len(implementation) - 1}. {_escape(task.title)} — {_escape(task.work_items[0].done_when)}"
        )
    return StructuredTaskPlanCompatibilityProjection(
        source_plan_hash=plan.content_hash,
        renderer_version=renderer_version,
        markdown=markdown,
        requirements="\n".join(requirements).rstrip() + "\n",
        design="\n".join(design).rstrip() + "\n",
        implementation_plan="\n".join(implementation).rstrip() + "\n",
        planner_markdown=markdown,
    )


@dataclass(frozen=True)
class TaskChange:
    task_id: str
    before: Mapping[str, Any]
    after: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "before": dict(self.before),
            "after": dict(self.after),
        }


@dataclass(frozen=True)
class StructuredTaskPlanStructuralDiff:
    added_tasks: tuple[str, ...] = ()
    removed_tasks: tuple[str, ...] = ()
    changed_tasks: tuple[TaskChange, ...] = ()
    added_dependencies: tuple[str, ...] = ()
    removed_dependencies: tuple[str, ...] = ()
    changed_execution_groups: tuple[str, ...] = ()
    topology_changed: bool = False
    coverage_changes: tuple[str, ...] = ()

    @property
    def dependency_changes(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.added_dependencies + self.removed_dependencies)))

    @property
    def execution_group_changes(self) -> tuple[str, ...]:
        return self.changed_execution_groups

    @property
    def changed(self) -> bool:
        return bool(
            self.added_tasks
            or self.removed_tasks
            or self.changed_tasks
            or self.added_dependencies
            or self.removed_dependencies
            or self.changed_execution_groups
            or self.topology_changed
            or self.coverage_changes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "added_tasks": list(self.added_tasks),
            "removed_tasks": list(self.removed_tasks),
            "changed_tasks": [item.to_dict() for item in self.changed_tasks],
            "added_dependencies": list(self.added_dependencies),
            "removed_dependencies": list(self.removed_dependencies),
            "changed_execution_groups": list(self.changed_execution_groups),
            "topology_changed": self.topology_changed,
            "coverage_changes": list(self.coverage_changes),
            "changed": self.changed,
        }


def diff_structured_task_plans(
    before: StructuredTaskPlan, after: StructuredTaskPlan
) -> StructuredTaskPlanStructuralDiff:
    before_tasks = {item.id: item for item in before.tasks}
    after_tasks = {item.id: item for item in after.tasks}
    added = tuple(sorted(set(after_tasks) - set(before_tasks)))
    removed = tuple(sorted(set(before_tasks) - set(after_tasks)))
    changed = tuple(
        TaskChange(
            task_id, before_tasks[task_id].to_dict(), after_tasks[task_id].to_dict()
        )
        for task_id in sorted(set(before_tasks) & set(after_tasks))
        if before_tasks[task_id].to_dict() != after_tasks[task_id].to_dict()
    )

    def edge_key(item: Dependency) -> tuple[str, str, str, str]:
        return (
            item.prerequisite_task_id,
            item.dependent_task_id,
            item.type,
            _normalized(item.reason),
        )

    before_edges = {edge_key(item): item.id for item in before.dependencies}
    after_edges = {edge_key(item): item.id for item in after.dependencies}
    before_groups = {item.id: item.to_dict() for item in before.execution_groups}
    after_groups = {item.id: item.to_dict() for item in after.execution_groups}
    changed_groups = tuple(
        sorted(
            group_id
            for group_id in set(before_groups) | set(after_groups)
            if before_groups.get(group_id) != after_groups.get(group_id)
        )
    )
    before_coverage = before.coverage_index().to_dict()
    after_coverage = after.coverage_index().to_dict()
    coverage_changes = (
        tuple(
            sorted(
                {
                    canonical_json_hash(before_coverage),
                    canonical_json_hash(after_coverage),
                }
            )
        )
        if before_coverage != after_coverage
        else ()
    )
    return StructuredTaskPlanStructuralDiff(
        added_tasks=added,
        removed_tasks=removed,
        changed_tasks=changed,
        added_dependencies=tuple(
            sorted(after_edges[key] for key in set(after_edges) - set(before_edges))
        ),
        removed_dependencies=tuple(
            sorted(before_edges[key] for key in set(before_edges) - set(after_edges))
        ),
        changed_execution_groups=changed_groups,
        topology_changed=before.topology != after.topology,
        coverage_changes=coverage_changes,
    )


# Compatibility aliases make the application-owned domain easy to discover
# without making any projection authoritative.
render_task_plan = render_structured_task_plan
project_compatibility = project_structured_task_plan
diff_task_plans = diff_structured_task_plans
validate_task_plan = validate_structured_task_plan
TaskPlan = StructuredTaskPlan
TaskPlanValidation = StructuredTaskPlanValidation
TaskPlanError = StructuredTaskPlanError


__all__ = [
    "BriefReference",
    "InputManifestReference",
    "WorkItem",
    "Traceability",
    "EffortEstimate",
    "ExecutionProfile",
    "Task",
    "Dependency",
    "ExecutionGroup",
    "IntentionalOmission",
    "Topology",
    "StructuredTaskPlan",
    "TaskPlan",
    "TaskPlanValidation",
    "TaskPlanError",
    "ValidationIssue",
    "StructuredTaskPlanValidation",
    "CoverageEntry",
    "CoverageIndex",
    "DependencyGraph",
    "StructuredTaskPlanCompatibilityProjection",
    "TaskChange",
    "StructuredTaskPlanStructuralDiff",
    "DEFAULT_TASK_PLAN_POLICY",
    "STRUCTURED_TASK_PLAN_SCHEMA_VERSION",
    "STRUCTURED_TASK_PLAN_STAGE_NAME",
    "STRUCTURED_TASK_PLAN_STAGE_VERSION",
    "STRUCTURED_TASK_PLAN_RENDERER_VERSION",
    "STRUCTURED_TASK_PLAN_VALIDATOR_VERSION",
    "canonical_json_bytes",
    "canonical_json_hash",
    "build_dependency_graph",
    "detect_cycles",
    "build_coverage_index",
    "validate_structured_task_plan",
    "require_valid_structured_task_plan",
    "render_structured_task_plan",
    "project_structured_task_plan",
    "diff_structured_task_plans",
    "render_task_plan",
    "project_compatibility",
    "diff_task_plans",
    "validate_task_plan",
]
