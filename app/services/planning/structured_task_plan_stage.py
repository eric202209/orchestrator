"""Protocol v2 Structured Task Plan generation stage.

The provider boundary in this module is intentionally semantic-only.  The
accepted Brief and Input Manifest are persisted authorities; application code
resolves references, assigns IDs, builds the graph, validates coverage, and
persists the immutable canonical Task Plan through the existing stage engine.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
import json
import re
from types import MappingProxyType
from typing import Any, Protocol

from app.services.agents.agent_runtime import BackendRole, invoke_runtime_prompt
from app.services.orchestration.stage_engine import (
    StageAcceptance,
    StageContext,
    StageDefinition,
    StageExecutionPolicy,
    StageValidation,
)
from app.services.planning.input_manifest import InputManifest
from app.services.planning.planning_brief import PlanningBrief
from app.services.planning.planning_brief_stage import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    PlanningBriefProvider,
    RuntimePlanningBriefProvider,
)
from app.services.planning.provider_contract import (
    STRUCTURED_TASK_PLAN_CANDIDATE_FIELDS,
    build_structured_task_plan_schema_contract,
    render_schema_contract,
)
from app.services.planning.structured_task_plan import (
    BLOCKING_STATES,
    CONFIDENCE_VALUES,
    DEFAULT_TASK_PLAN_POLICY,
    DEPENDENCY_TYPES,
    EFFORT_UNITS,
    EXECUTION_OWNER_ROLES,
    GROUP_KINDS,
    GROUP_SKIP_POLICIES,
    ISOLATION_MODES,
    NETWORK_MODES,
    PARALLELISM_MODES,
    REVIEW_MODES,
    TASK_CATEGORIES,
    TASK_COMPLEXITIES,
    TASK_PRIORITIES,
    TRACEABILITY_ROLES,
    TRACEABILITY_TARGET_KINDS,
    BriefReference,
    Dependency,
    EffortEstimate,
    ExecutionGroup,
    ExecutionProfile,
    InputManifestReference,
    IntentionalOmission,
    StructuredTaskPlan,
    StructuredTaskPlanError,
    StructuredTaskPlanGraphError,
    StructuredTaskPlanSchemaError,
    Task,
    Traceability,
    WorkItem,
    canonical_json_bytes,
    validate_structured_task_plan,
)


DEFAULT_TASK_PLAN_SOURCE_CHAR_LIMIT = 20_000
DEFAULT_TASK_PLAN_TOTAL_SOURCE_CHAR_LIMIT = 100_000
DEFAULT_TASK_PLAN_PROVIDER_INPUT_BYTES = 512 * 1024
DEFAULT_TASK_PLAN_CANDIDATE_BYTES = 512 * 1024
DEFAULT_TASK_PLAN_TEXT_CHAR_LIMIT = 2_000
DEFAULT_TASK_PLAN_TARGET_CHAR_LIMIT = 512
_TASK_POSITION_RE = re.compile(r"^(?:task|tasks)\[(\d+)\]$|^#(\d+)$")
_CANONICAL_TASK_ID_RE = re.compile(r"^TASK-[0-9]{3}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNSAFE_MARKUP_RE = re.compile(r"<\/?[A-Za-z][^>]*>|\b(?:javascript|data)\s*:", re.I)
_UNSAFE_TARGET_RE = re.compile(
    r"(?:^/|^[A-Za-z]:[\\/]|(?:^|[/\\])\.\.(?:[/\\]|$)|[*?\[\]{}])"
)
_SECRET_KEY_RE = re.compile(
    r"(?:password|token|secret|api[_-]?key|private[_-]?key|credential)", re.I
)
_SECRET_VALUE_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{12,})"
)


class StructuredTaskPlanStageError(RuntimeError):
    """A bounded, classified failure at the Task Plan stage boundary."""

    classification = "application_error"

    def __init__(self, message: str):
        self.detail = str(message or self.classification)[:500]
        super().__init__(f"{self.classification}: {self.detail}")


class StructuredTaskPlanTransportError(StructuredTaskPlanStageError):
    classification = "transport_failure"


class StructuredTaskPlanProviderOutputError(StructuredTaskPlanStageError):
    classification = "provider_output_failure"


class StructuredTaskPlanReferenceResolutionError(StructuredTaskPlanStageError):
    classification = "reference_resolution_failure"


class StructuredTaskPlanGraphValidationError(StructuredTaskPlanStageError):
    classification = "graph_validation_failure"


class StructuredTaskPlanCoverageValidationError(StructuredTaskPlanStageError):
    classification = "coverage_validation_failure"


class StructuredTaskPlanAcceptanceError(StructuredTaskPlanStageError):
    classification = "protocol_acceptance_failure"


class StructuredTaskPlanIntegrityError(StructuredTaskPlanStageError):
    classification = "integrity_failure"


class StructuredTaskPlanApplicationError(StructuredTaskPlanStageError):
    classification = "application_error"


class StructuredTaskPlanProviderRuntimeError(StructuredTaskPlanStageError):
    """A provider-boundary failure with a stable non-semantic class."""

    _ALLOWED_CLASSIFICATIONS = frozenset(
        {
            "provider_timeout",
            "provider_process_failure",
            "provider_result_missing",
            "provider_result_ambiguous",
        }
    )

    def __init__(self, classification: str, message: str):
        self.classification = (
            classification
            if classification in self._ALLOWED_CLASSIFICATIONS
            else "transport_failure"
        )
        super().__init__(message)


class StructuredTaskPlanProvider(Protocol):
    """Provider-independent semantic candidate contract."""

    def generate(self, provider_input: "StructuredTaskPlanProviderInput") -> Any:
        """Return semantic candidate JSON, never an authoritative plan."""


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class StructuredTaskPlanProviderInput:
    """Bounded immutable input assembled from accepted persisted authorities."""

    brief_checkpoint_id: str
    brief_hash: str
    brief_content: str
    manifest_id: str
    manifest_hash: str
    manifest_schema_version: str
    sources: tuple[Mapping[str, Any], ...]
    schema_instructions: Mapping[str, Any]
    stage_configuration: Mapping[str, Any]
    capacity_limits: Mapping[str, Any]
    rules: Mapping[str, Any]
    formatting_instructions: Mapping[str, Any]
    # Runtime-only routing metadata; it is not part of the provider contract.
    project_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sources",
            tuple(_freeze(source) for source in self.sources),
        )
        for field_name in (
            "schema_instructions",
            "stage_configuration",
            "capacity_limits",
            "rules",
            "formatting_instructions",
        ):
            object.__setattr__(
                self,
                field_name,
                _freeze(getattr(self, field_name)),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_planning_brief": {
                "checkpoint_id": self.brief_checkpoint_id,
                "content_hash": self.brief_hash,
                "canonical_content": self.brief_content,
            },
            "input_manifest": {
                "id": self.manifest_id,
                "hash": self.manifest_hash,
                "schema_version": self.manifest_schema_version,
            },
            "bounded_manifest_sources": [_thaw(source) for source in self.sources],
            "schema_instructions": _thaw(self.schema_instructions),
            "stage_configuration": _thaw(self.stage_configuration),
            "capacity_limits": _thaw(self.capacity_limits),
            "rules": _thaw(self.rules),
            "formatting_instructions": _thaw(self.formatting_instructions),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True)
class StructuredTaskPlanCandidate:
    """Semantic candidate records before lineage, IDs, and topology exist."""

    tasks: tuple[Task, ...]
    dependencies: tuple[Dependency, ...]
    execution_groups: tuple[ExecutionGroup, ...]
    intentional_omissions: tuple[IntentionalOmission, ...]


def _candidate_mapping(raw: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise StructuredTaskPlanProviderOutputError(f"{path} must be an object")
    if any(_SECRET_KEY_RE.search(str(key)) for key in raw):
        raise StructuredTaskPlanProviderOutputError(
            f"{path} contains credential-shaped fields"
        )
    return raw


def _strict_record(
    raw: Any,
    *,
    record_type: type[Any],
    path: str,
    required: set[str] | None = None,
) -> Mapping[str, Any]:
    record = _candidate_mapping(raw, path)
    allowed = {item.name for item in fields(record_type)} - {"id"}
    unknown = sorted(set(record) - allowed)
    if "id" in record:
        raise StructuredTaskPlanProviderOutputError(f"{path}.id is application-owned")
    missing = sorted((required or allowed) - set(record))
    if unknown:
        raise StructuredTaskPlanProviderOutputError(
            f"{path} contains unknown fields: {', '.join(unknown)}"
        )
    if missing:
        raise StructuredTaskPlanProviderOutputError(
            f"{path} is missing fields: {', '.join(missing)}"
        )
    return record


def _validate_text(value: str, path: str, *, target: bool = False) -> None:
    if len(value) > (
        DEFAULT_TASK_PLAN_TARGET_CHAR_LIMIT
        if target
        else DEFAULT_TASK_PLAN_TEXT_CHAR_LIMIT
    ):
        raise StructuredTaskPlanProviderOutputError(f"{path} exceeds its text bound")
    if _CONTROL_RE.search(value) or _UNSAFE_MARKUP_RE.search(value):
        raise StructuredTaskPlanProviderOutputError(f"{path} contains unsafe text")
    if target and _UNSAFE_TARGET_RE.search(value):
        raise StructuredTaskPlanProviderOutputError(
            f"{path} is not a safe project-relative target"
        )
    if _SECRET_VALUE_RE.search(value):
        raise StructuredTaskPlanProviderOutputError(
            f"{path} contains credential material"
        )


def _validate_record_texts(value: Any, path: str) -> None:
    if isinstance(value, str):
        _validate_text(value, path, target=path.endswith(".target"))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                raise StructuredTaskPlanProviderOutputError(
                    f"{path}.{key} is not permitted"
                )
            _validate_record_texts(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_record_texts(item, f"{path}[{index}]")


def _parse_task(raw: Any, index: int) -> Task:
    path = f"tasks[{index}]"
    record = _strict_record(raw, record_type=Task, path=path)
    effort_raw = _strict_record(
        record["estimated_effort"],
        record_type=EffortEstimate,
        path=f"{path}.estimated_effort",
    )
    profile_raw = _strict_record(
        record["execution_profile"],
        record_type=ExecutionProfile,
        path=f"{path}.execution_profile",
    )
    work_items_raw = record["work_items"]
    traceability_raw = record["traceability"]
    if not isinstance(work_items_raw, list) or not isinstance(traceability_raw, list):
        raise StructuredTaskPlanProviderOutputError(
            f"{path}.work_items and {path}.traceability must be arrays"
        )
    work_items = tuple(
        WorkItem(
            **dict(
                _strict_record(
                    item, record_type=WorkItem, path=f"{path}.work_items[{item_index}]"
                )
            )
        )
        for item_index, item in enumerate(work_items_raw)
    )
    traceability = tuple(
        Traceability(
            **dict(
                _strict_record(
                    item,
                    record_type=Traceability,
                    path=f"{path}.traceability[{item_index}]",
                )
            )
        )
        for item_index, item in enumerate(traceability_raw)
    )
    try:
        task = Task(
            id="",
            title=record["title"],
            objective=record["objective"],
            implementation_description=record["implementation_description"],
            rationale=record["rationale"],
            priority=record["priority"],
            complexity=record["complexity"],
            estimated_effort=EffortEstimate(**dict(effort_raw)),
            category=record["category"],
            execution_profile=ExecutionProfile(**dict(profile_raw)),
            blocking_state=record["blocking_state"],
            work_items=work_items,
            traceability=traceability,
        )
    except (TypeError, ValueError, StructuredTaskPlanError) as exc:
        raise StructuredTaskPlanProviderOutputError(f"malformed {path}") from exc
    _validate_record_texts(raw, path)
    if task.priority not in TASK_PRIORITIES:
        raise StructuredTaskPlanProviderOutputError(f"{path}.priority is unknown")
    if task.complexity not in TASK_COMPLEXITIES:
        raise StructuredTaskPlanProviderOutputError(f"{path}.complexity is unknown")
    if task.category not in TASK_CATEGORIES:
        raise StructuredTaskPlanProviderOutputError(f"{path}.category is unknown")
    if task.blocking_state not in BLOCKING_STATES:
        raise StructuredTaskPlanProviderOutputError(f"{path}.blocking_state is unknown")
    if task.estimated_effort.unit not in EFFORT_UNITS or (
        task.estimated_effort.confidence not in CONFIDENCE_VALUES
    ):
        raise StructuredTaskPlanProviderOutputError(
            f"{path}.estimated_effort enum is unknown"
        )
    for name, allowed in (
        ("owner_role", EXECUTION_OWNER_ROLES),
        ("isolation", ISOLATION_MODES),
        ("network", NETWORK_MODES),
        ("parallelism", PARALLELISM_MODES),
        ("review", REVIEW_MODES),
    ):
        if getattr(task.execution_profile, name) not in allowed:
            raise StructuredTaskPlanProviderOutputError(
                f"{path}.execution_profile.{name} is unknown"
            )
    for item_index, item in enumerate(task.traceability):
        if item.target_kind not in TRACEABILITY_TARGET_KINDS:
            raise StructuredTaskPlanProviderOutputError(
                f"{path}.traceability[{item_index}].target_kind is unknown"
            )
        if item.role not in TRACEABILITY_ROLES:
            raise StructuredTaskPlanProviderOutputError(
                f"{path}.traceability[{item_index}].role is unknown"
            )
    return task


def _parse_dependency(raw: Any, index: int) -> Dependency:
    path = f"dependencies[{index}]"
    record = _strict_record(raw, record_type=Dependency, path=path)
    try:
        dependency = Dependency(id="", **dict(record))
    except (TypeError, ValueError, StructuredTaskPlanError) as exc:
        raise StructuredTaskPlanProviderOutputError(f"malformed {path}") from exc
    _validate_record_texts(raw, path)
    if dependency.type not in DEPENDENCY_TYPES:
        raise StructuredTaskPlanProviderOutputError(f"{path}.type is unknown")
    return dependency


def _parse_group(raw: Any, index: int) -> ExecutionGroup:
    path = f"execution_groups[{index}]"
    record = _strict_record(raw, record_type=ExecutionGroup, path=path)
    if not isinstance(record["task_ids"], list):
        raise StructuredTaskPlanProviderOutputError(f"{path}.task_ids must be an array")
    try:
        group = ExecutionGroup(id="", **dict(record))
    except (TypeError, ValueError, StructuredTaskPlanError) as exc:
        raise StructuredTaskPlanProviderOutputError(f"malformed {path}") from exc
    _validate_record_texts(raw, path)
    if group.kind not in GROUP_KINDS:
        raise StructuredTaskPlanProviderOutputError(f"{path}.kind is unknown")
    if group.skip_policy not in GROUP_SKIP_POLICIES:
        raise StructuredTaskPlanProviderOutputError(f"{path}.skip_policy is unknown")
    return group


def _parse_omission(raw: Any, index: int) -> IntentionalOmission:
    path = f"intentional_omissions[{index}]"
    record = _strict_record(raw, record_type=IntentionalOmission, path=path)
    try:
        omission = IntentionalOmission(**dict(record))
    except (TypeError, ValueError, StructuredTaskPlanError) as exc:
        raise StructuredTaskPlanProviderOutputError(f"malformed {path}") from exc
    _validate_record_texts(raw, path)
    return omission


def _parse_candidate_json(raw: Any, max_bytes: int) -> Mapping[str, Any]:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StructuredTaskPlanProviderOutputError(
                "candidate is not valid UTF-8 JSON"
            ) from exc
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > max_bytes:
            raise StructuredTaskPlanProviderOutputError(
                "candidate output exceeds bound"
            )
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise StructuredTaskPlanProviderOutputError(
                "candidate is not valid JSON"
            ) from exc
    else:
        parsed = raw
    if not isinstance(parsed, Mapping):
        raise StructuredTaskPlanProviderOutputError("candidate must be a JSON object")
    try:
        if len(canonical_json_bytes(parsed)) > max_bytes:
            raise StructuredTaskPlanProviderOutputError(
                "candidate output exceeds bound"
            )
    except StructuredTaskPlanProviderOutputError:
        raise
    except Exception as exc:
        raise StructuredTaskPlanProviderOutputError(
            "candidate is not canonically JSON-shaped"
        ) from exc
    return parsed


def parse_structured_task_plan_candidate(
    raw: Any, *, max_bytes: int = DEFAULT_TASK_PLAN_CANDIDATE_BYTES
) -> StructuredTaskPlanCandidate:
    """Strictly parse semantic candidate content before authority exists."""

    if isinstance(max_bytes, bool) or int(max_bytes) < 1:
        raise StructuredTaskPlanApplicationError("candidate bound must be positive")
    parsed = _parse_candidate_json(raw, int(max_bytes))
    expected = set(STRUCTURED_TASK_PLAN_CANDIDATE_FIELDS)
    unknown = sorted(set(parsed) - expected)
    missing = sorted(expected - set(parsed))
    if unknown:
        raise StructuredTaskPlanProviderOutputError(
            f"candidate contains unknown fields: {', '.join(unknown)}"
        )
    if missing:
        raise StructuredTaskPlanProviderOutputError(
            f"candidate is missing fields: {', '.join(missing)}"
        )
    if any(
        key in parsed
        for key in ("schema_version", "brief_ref", "topology", "content_hash")
    ):
        raise StructuredTaskPlanProviderOutputError(
            "candidate contains application-owned metadata"
        )
    tasks_raw = parsed["tasks"]
    dependencies_raw = parsed["dependencies"]
    groups_raw = parsed["execution_groups"]
    omissions_raw = parsed["intentional_omissions"]
    if not all(
        isinstance(value, list)
        for value in (tasks_raw, dependencies_raw, groups_raw, omissions_raw)
    ):
        raise StructuredTaskPlanProviderOutputError(
            "candidate collections must be arrays"
        )
    if not tasks_raw:
        raise StructuredTaskPlanProviderOutputError(
            "candidate requires at least one Task"
        )
    tasks = tuple(_parse_task(item, index) for index, item in enumerate(tasks_raw))
    dependencies = tuple(
        _parse_dependency(item, index) for index, item in enumerate(dependencies_raw)
    )
    groups = tuple(_parse_group(item, index) for index, item in enumerate(groups_raw))
    omissions = tuple(
        _parse_omission(item, index) for index, item in enumerate(omissions_raw)
    )
    return StructuredTaskPlanCandidate(tasks, dependencies, groups, omissions)


def _candidate_task_position(reference: Any, count: int, path: str) -> str:
    if isinstance(reference, bool) or isinstance(reference, int):
        raise StructuredTaskPlanReferenceResolutionError(
            f"{path} must use an explicit tasks[N] or #N reference"
        )
    if not isinstance(reference, str):
        raise StructuredTaskPlanReferenceResolutionError(f"{path} reference is invalid")
    if _CANONICAL_TASK_ID_RE.fullmatch(reference):
        raise StructuredTaskPlanReferenceResolutionError(
            f"{path} uses a provider-owned Task ID"
        )
    match = _TASK_POSITION_RE.fullmatch(reference)
    if match is None:
        raise StructuredTaskPlanReferenceResolutionError(
            f"{path} is not an unambiguous candidate Task position"
        )
    position = int(next(value for value in match.groups() if value is not None))
    if position < 0 or position >= count:
        raise StructuredTaskPlanReferenceResolutionError(
            f"{path} candidate Task position does not resolve"
        )
    return f"#{position}"


def _brief_targets(brief: PlanningBrief) -> set[tuple[str, str]]:
    targets = {("goal", brief.objective.id)}
    for collection_name, kind in (
        ("requirements", "requirement"),
        ("constraints", "constraint"),
        ("acceptance_criteria", "acceptance_criterion"),
        ("architecture_context", "architecture_context"),
        ("interface_contracts", "interface_contract"),
        ("scope", "scope"),
    ):
        targets.update((kind, item.id) for item in getattr(brief, collection_name))
    return targets


def canonicalize_structured_task_plan_candidate(
    candidate: StructuredTaskPlanCandidate,
    context: StageContext,
) -> StructuredTaskPlan:
    """Resolve persisted Brief references and construct canonical plan authority."""

    brief = context.planning_brief
    if brief is None:
        raise StructuredTaskPlanReferenceResolutionError(
            "accepted Planning Brief is not available in StageContext"
        )
    brief_checkpoint = context.predecessor_checkpoints.get("planning_brief")
    if brief_checkpoint is None or brief_checkpoint.status != "accepted":
        raise StructuredTaskPlanIntegrityError("accepted Brief checkpoint is missing")
    if brief_checkpoint.content_hash != brief.content_hash:
        raise StructuredTaskPlanIntegrityError(
            "accepted Brief checkpoint hash mismatch"
        )
    manifest = context.input_manifest
    if (
        brief.input_manifest_ref.id != manifest.manifest_id
        or brief.input_manifest_ref.hash != manifest.manifest_hash
    ):
        raise StructuredTaskPlanIntegrityError("Brief/Input Manifest lineage mismatch")
    targets = _brief_targets(brief)
    for task_index, task in enumerate(candidate.tasks):
        for ref_index, reference in enumerate(task.traceability):
            if (reference.target_kind, reference.target_id) not in targets:
                raise StructuredTaskPlanReferenceResolutionError(
                    f"tasks[{task_index}].traceability[{ref_index}] does not resolve"
                )
    for omission_index, omission in enumerate(candidate.intentional_omissions):
        if (omission.target_kind, omission.target_id) not in targets:
            raise StructuredTaskPlanReferenceResolutionError(
                f"intentional_omissions[{omission_index}] does not resolve"
            )
    fingerprints: set[bytes] = set()
    for task in candidate.tasks:
        fingerprint = canonical_json_bytes(task.to_dict())
        if fingerprint in fingerprints:
            raise StructuredTaskPlanProviderOutputError(
                "candidate contains an exact semantic duplicate Task"
            )
        fingerprints.add(fingerprint)
    task_count = len(candidate.tasks)
    dependencies = tuple(
        replace(
            dependency,
            prerequisite_task_id=_candidate_task_position(
                dependency.prerequisite_task_id,
                task_count,
                f"dependencies[{index}].prerequisite_task_id",
            ),
            dependent_task_id=_candidate_task_position(
                dependency.dependent_task_id,
                task_count,
                f"dependencies[{index}].dependent_task_id",
            ),
        )
        for index, dependency in enumerate(candidate.dependencies)
    )
    groups = tuple(
        replace(
            group,
            task_ids=tuple(
                _candidate_task_position(
                    reference,
                    task_count,
                    f"execution_groups[{index}].task_ids[{member_index}]",
                )
                for member_index, reference in enumerate(group.task_ids)
            ),
        )
        for index, group in enumerate(candidate.execution_groups)
    )
    try:
        return StructuredTaskPlan.create(
            brief_ref=BriefReference(str(brief_checkpoint.id), brief.content_hash),
            input_manifest_ref=InputManifestReference(
                manifest.manifest_id, manifest.manifest_hash
            ),
            tasks=candidate.tasks,
            dependencies=dependencies,
            execution_groups=groups,
            intentional_omissions=candidate.intentional_omissions,
        )
    except StructuredTaskPlanGraphError as exc:
        raise StructuredTaskPlanGraphValidationError(str(exc)) from exc
    except StructuredTaskPlanError as exc:
        raise StructuredTaskPlanProviderOutputError(
            f"candidate canonicalization failed: {exc}"
        ) from exc


def _policy(configuration: Mapping[str, Any]) -> dict[str, Any]:
    nested = configuration.get("structured_task_plan", {})
    nested = nested if isinstance(nested, Mapping) else {}
    result = dict(DEFAULT_TASK_PLAN_POLICY)
    result["auto_accept"] = True
    for key in result:
        if key in nested:
            result[key] = nested[key]
        elif key in configuration:
            result[key] = configuration[key]
    return result


def _configuration_value(
    configuration: Mapping[str, Any], key: str, default: int
) -> int:
    nested = configuration.get("structured_task_plan", {})
    nested = nested if isinstance(nested, Mapping) else {}
    value = nested.get(key, configuration.get(key, default))
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise StructuredTaskPlanApplicationError(f"{key} must be an integer") from exc
    if normalized < 1:
        raise StructuredTaskPlanApplicationError(f"{key} must be positive")
    return normalized


def build_structured_task_plan_provider_input(
    context: StageContext,
) -> StructuredTaskPlanProviderInput:
    """Build a bounded request from the accepted Brief and persisted Manifest."""

    brief = context.planning_brief
    if brief is None:
        raise StructuredTaskPlanReferenceResolutionError("accepted Brief is required")
    brief_checkpoint = context.predecessor_checkpoints.get("planning_brief")
    if brief_checkpoint is None or brief_checkpoint.status != "accepted":
        raise StructuredTaskPlanIntegrityError("accepted Brief checkpoint is required")
    if brief_checkpoint.content_hash != brief.content_hash:
        raise StructuredTaskPlanIntegrityError("Brief checkpoint content hash mismatch")
    configuration = dict(context.configuration)
    source_limit = _configuration_value(
        configuration, "max_source_chars", DEFAULT_TASK_PLAN_SOURCE_CHAR_LIMIT
    )
    total_source_limit = _configuration_value(
        configuration,
        "max_total_source_chars",
        DEFAULT_TASK_PLAN_TOTAL_SOURCE_CHAR_LIMIT,
    )
    input_limit = _configuration_value(
        configuration,
        "max_provider_input_bytes",
        DEFAULT_TASK_PLAN_PROVIDER_INPUT_BYTES,
    )
    sources: list[Mapping[str, Any]] = []
    total_chars = 0
    for source in context.input_manifest.ordered_sources:
        payload = source.to_dict()
        material = json.dumps(
            payload.get("content"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(material) > source_limit:
            raise StructuredTaskPlanApplicationError(
                f"source {source.source_id} exceeds bounded provider input"
            )
        total_chars += len(material)
        if total_chars > total_source_limit:
            raise StructuredTaskPlanApplicationError(
                "manifest source material exceeds bounded provider input"
            )
        sources.append(
            {
                "source_id": payload["source_id"],
                "source_type": payload["source_type"],
                "ordinal": payload["ordinal"],
                "content_hash": payload["content_hash"],
                "included": payload["included"],
                "omission_reason": payload["omission_reason"],
                "content": payload["content"] if payload["included"] else None,
            }
        )
    plan_policy = _policy(configuration)
    request = StructuredTaskPlanProviderInput(
        brief_checkpoint_id=str(brief_checkpoint.id),
        brief_hash=brief.content_hash,
        brief_content=brief.canonical_json(),
        manifest_id=context.input_manifest.manifest_id,
        manifest_hash=context.input_manifest.manifest_hash,
        manifest_schema_version=context.input_manifest.schema_version,
        sources=tuple(sources),
        schema_instructions=build_structured_task_plan_schema_contract(),
        stage_configuration=configuration,
        capacity_limits=plan_policy,
        rules={
            "coverage": "Every required Brief goal, requirement, acceptance criterion, and must constraint must be covered or have a Brief-authorized omission.",
            "dependencies": "All dependency references must resolve and the application builds the DAG.",
            "groups": "Sequential group edges are materialized by the application; parallel targets must not conflict.",
            "traceability": "Do not invent Brief requirements, criteria, constraints, assumptions, decisions, or scope exceptions.",
        },
        formatting_instructions={
            "output": "Return one JSON object only; no Markdown fences or commentary.",
            "ordering": "Candidate emission order is non-authoritative.",
        },
        project_id=getattr(context.session, "project_id", None),
    )
    if len(request.canonical_bytes()) > input_limit:
        raise StructuredTaskPlanApplicationError(
            "accepted Brief and bounded manifest material exceed provider input bound"
        )
    return request


class RuntimeStructuredTaskPlanProvider:
    """Production adapter over the existing runtime invocation seam."""

    def __init__(self, db: Any):
        self.db = db

    def generate(self, provider_input: StructuredTaskPlanProviderInput) -> Any:
        candidate_fields = ", ".join(STRUCTURED_TASK_PLAN_CANDIDATE_FIELDS)
        schema = render_schema_contract(
            getattr(provider_input, "schema_instructions", None)
            or build_structured_task_plan_schema_contract()
        )
        prompt = (
            "Generate one Protocol v2 Structured Task Plan semantic candidate from INPUT.\n"
            "Return exactly one JSON object. Return no Markdown fences, explanation, "
            "commentary, or reasoning.\n"
            f"The only allowed top-level fields are: {candidate_fields}.\n"
            "Use only the specified Task, Dependency, ExecutionGroup, IntentionalOmission, "
            "and nested record fields. Do not invent aliases or legacy title, description, "
            "objectives, deliverables, or timeline keys. Do not emit canonical IDs, hashes, "
            "topology, schema versions, lifecycle, acceptance, checkpoint, session, lease, "
            "timestamp, Commit Manifest, Runtime Task, credential, or new Brief-intent fields; "
            "all such fields are application-owned or forbidden. Represent graph and coverage "
            "semantics explicitly, use accepted Brief IDs for traceability, and use tasks[N] or #N "
            "for candidate task references. Preserve uncertainty rather than inventing facts.\n\n"
            "COMPLETE RECORD-LEVEL SCHEMA CONTRACT:\n"
            + schema
            + "\n\nINPUT:\n"
            + provider_input.canonical_bytes().decode("utf-8")
        )
        try:
            timeout_seconds = int(
                provider_input.stage_configuration.get(
                    "provider_timeout_seconds", DEFAULT_PROVIDER_TIMEOUT_SECONDS
                )
            )
            result = invoke_runtime_prompt(
                self.db,
                prompt,
                project_id=getattr(provider_input, "project_id", None),
                source_brain="local",
                timeout_seconds=timeout_seconds,
                session_prefix="structured-task-plan",
                role=BackendRole.PLANNING,
            )
        except Exception as exc:
            classification = getattr(
                exc, "provider_failure_classification", None
            ) or getattr(exc, "classification", None)
            if (
                classification
                in StructuredTaskPlanProviderRuntimeError._ALLOWED_CLASSIFICATIONS
            ):
                raise StructuredTaskPlanProviderRuntimeError(
                    classification, str(exc)
                ) from exc
            raise StructuredTaskPlanTransportError(
                "provider invocation failed"
            ) from exc
        if not isinstance(result, Mapping) or result.get("status") == "failed":
            classification = (
                result.get("failure_classification")
                if isinstance(result, Mapping)
                else None
            )
            if (
                classification
                in StructuredTaskPlanProviderRuntimeError._ALLOWED_CLASSIFICATIONS
            ):
                raise StructuredTaskPlanProviderRuntimeError(
                    classification,
                    str(result.get("error") or classification),
                )
            raise StructuredTaskPlanTransportError("provider returned a failed result")
        output = result.get("output")
        if not isinstance(output, (str, bytes, Mapping)):
            raise StructuredTaskPlanProviderOutputError(
                "provider returned no candidate output"
            )
        return output


def _validation_reason(validation: Any) -> str:
    issues = tuple(validation.errors) + tuple(validation.warnings)
    if any(
        item.code
        in {
            "dependency_cycle",
            "self_edge",
            "duplicate_dependency",
            "parallel_dependency",
            "parallel_target_conflict",
            "topology_mismatch",
            "critical_path_mismatch",
            "dependency_fan_limit",
        }
        for item in issues
    ):
        classification = "graph_validation_failure"
    elif any(
        item.code
        in {
            "missing_coverage",
            "invalid_omission",
            "orphan_task",
            "duplicate_implementation",
            "brief_not_acceptable",
        }
        for item in issues
    ):
        classification = "coverage_validation_failure"
    elif any(
        item.code in {"brief_hash_mismatch", "manifest_hash_mismatch", "invalid_hash"}
        for item in issues
    ):
        classification = "integrity_failure"
    elif any(item.severity == "review_required" for item in issues):
        classification = "protocol_acceptance_failure"
    else:
        classification = "provider_output_failure"
    detail = ",".join(f"{item.code}:{item.path}" for item in issues[:8])
    return f"{classification}: {detail or 'Task Plan validation failed'}"


class StructuredTaskPlanStage(StageDefinition):
    """Generate and accept one canonical Task Plan from an accepted Brief."""

    def __init__(self, provider: StructuredTaskPlanProvider):
        self.provider = provider
        super().__init__(
            "structured_task_plan",
            version=1,
            prerequisites=("planning_brief",),
            execution_policy=StageExecutionPolicy(retryable=True, max_attempts=1),
        )

    def execute(self, context: StageContext) -> StructuredTaskPlan:
        try:
            provider_input = build_structured_task_plan_provider_input(context)
        except StructuredTaskPlanStageError:
            raise
        except Exception as exc:
            raise StructuredTaskPlanApplicationError(
                "provider input construction failed"
            ) from exc
        try:
            raw = self.provider.generate(provider_input)
        except StructuredTaskPlanStageError:
            raise
        except Exception as exc:
            raise StructuredTaskPlanTransportError(
                "provider invocation failed"
            ) from exc
        try:
            candidate = parse_structured_task_plan_candidate(
                raw,
                max_bytes=_configuration_value(
                    context.configuration,
                    "max_candidate_bytes",
                    DEFAULT_TASK_PLAN_CANDIDATE_BYTES,
                ),
            )
            return canonicalize_structured_task_plan_candidate(candidate, context)
        except StructuredTaskPlanStageError:
            raise
        except Exception as exc:
            raise StructuredTaskPlanProviderOutputError(
                f"candidate canonicalization failed: {str(exc)[:300]}"
            ) from exc

    def validate(self, output: Any, context: StageContext) -> StageValidation:
        if not isinstance(output, StructuredTaskPlan):
            return StageValidation(
                False, "provider_output_failure: output is not a Task Plan"
            )
        validation = validate_structured_task_plan(
            output,
            brief=context.planning_brief,
            input_manifest=context.input_manifest,
            policy=_policy(context.configuration),
        )
        if not validation.schema_valid or not validation.semantically_valid:
            return StageValidation(False, _validation_reason(validation))
        return StageValidation(True)

    def accept(self, output: Any, context: StageContext) -> StageAcceptance:
        if not isinstance(output, StructuredTaskPlan):
            return StageAcceptance(
                False, "provider_output_failure: output is not a Task Plan"
            )
        validation = validate_structured_task_plan(
            output,
            brief=context.planning_brief,
            input_manifest=context.input_manifest,
            policy=_policy(context.configuration),
        )
        if not validation.protocol_acceptable:
            return StageAcceptance(False, _validation_reason(validation))
        return StageAcceptance(True)


def build_protocol_v2_stage_configuration(
    definitions: Sequence[StageDefinition] | None = None,
) -> dict[str, Any]:
    """Build the deterministic default stage configuration/fingerprint input."""

    definitions = tuple(definitions or ())
    return {
        "stages": [
            {
                "identifier": definition.identifier,
                "version": definition.version,
                "prerequisites": list(definition.prerequisites),
            }
            for definition in definitions
        ],
        "structured_task_plan": {
            **dict(DEFAULT_TASK_PLAN_POLICY),
            "auto_accept": True,
            "max_source_chars": DEFAULT_TASK_PLAN_SOURCE_CHAR_LIMIT,
            "max_total_source_chars": DEFAULT_TASK_PLAN_TOTAL_SOURCE_CHAR_LIMIT,
            "max_provider_input_bytes": DEFAULT_TASK_PLAN_PROVIDER_INPUT_BYTES,
            "max_candidate_bytes": DEFAULT_TASK_PLAN_CANDIDATE_BYTES,
        },
    }


def build_protocol_v2_stage_definitions(
    db: Any,
    *,
    provider: PlanningBriefProvider | None = None,
    task_plan_provider: StructuredTaskPlanProvider | None = None,
) -> tuple[StageDefinition, ...]:
    """Return the default v2 graph while preserving explicit custom registries."""

    brief_stage = RuntimePlanningBriefProvider(db) if provider is None else provider
    plan_stage = (
        RuntimeStructuredTaskPlanProvider(db)
        if task_plan_provider is None
        else task_plan_provider
    )
    from app.services.planning.planning_brief_stage import PlanningBriefStage

    return (PlanningBriefStage(brief_stage), StructuredTaskPlanStage(plan_stage))


__all__ = [
    "DEFAULT_TASK_PLAN_CANDIDATE_BYTES",
    "DEFAULT_TASK_PLAN_PROVIDER_INPUT_BYTES",
    "DEFAULT_TASK_PLAN_SOURCE_CHAR_LIMIT",
    "DEFAULT_TASK_PLAN_TOTAL_SOURCE_CHAR_LIMIT",
    "STRUCTURED_TASK_PLAN_CANDIDATE_FIELDS",
    "RuntimeStructuredTaskPlanProvider",
    "StructuredTaskPlanProviderRuntimeError",
    "StructuredTaskPlanAcceptanceError",
    "StructuredTaskPlanApplicationError",
    "StructuredTaskPlanCandidate",
    "StructuredTaskPlanCoverageValidationError",
    "StructuredTaskPlanGraphValidationError",
    "StructuredTaskPlanIntegrityError",
    "StructuredTaskPlanProvider",
    "StructuredTaskPlanProviderInput",
    "StructuredTaskPlanProviderOutputError",
    "StructuredTaskPlanReferenceResolutionError",
    "StructuredTaskPlanStage",
    "StructuredTaskPlanStageError",
    "StructuredTaskPlanTransportError",
    "build_protocol_v2_stage_configuration",
    "build_protocol_v2_stage_definitions",
    "build_structured_task_plan_provider_input",
    "canonicalize_structured_task_plan_candidate",
    "parse_structured_task_plan_candidate",
]
