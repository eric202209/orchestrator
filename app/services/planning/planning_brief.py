"""Canonical Planning Brief domain for Protocol v2.

The Brief is a strict, immutable planning-intent value.  It deliberately has
no provider, prompt, or generation dependency: callers provide structured
records, this module assigns application-owned IDs, validates the result,
serializes it canonically, renders compatibility Markdown, and computes
structural diffs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
import hashlib
import html
import json
import re
from types import MappingProxyType
from typing import Any, ClassVar, TypeVar
import unicodedata

from app.services.planning.input_manifest import InputManifest


PLANNING_BRIEF_SCHEMA_VERSION = "planning-brief/1.0"
PLANNING_BRIEF_RENDERER_VERSION = "planning-brief-markdown/1.0"
PLANNING_BRIEF_VALIDATOR_VERSION = "planning-brief-validator/1.0"
PLANNING_BRIEF_STAGE_NAME = "planning_brief"
PLANNING_BRIEF_STAGE_VERSION = 1
MAX_BRIEF_CHARACTERS = 100_000
MAX_BRIEF_BYTES = 128 * 1024
MAX_STATEMENT_CHARACTERS = 1_000
MAX_OBJECTIVE_CHARACTERS = 2_000
MAX_DETAIL_CHARACTERS = 1_500

SCOPE_CLASSIFICATIONS = frozenset(
    {
        "in_scope",
        "out_of_scope",
        "deferred",
        "prohibited",
        "assumed_existing",
        "compatibility_preserved",
    }
)
SCOPE_PRECEDENCE = {
    "in_scope": 1,
    "assumed_existing": 2,
    "compatibility_preserved": 3,
    "deferred": 4,
    "out_of_scope": 5,
    "prohibited": 6,
}
REQUIREMENT_TYPES = frozenset({"functional", "non_functional"})
REQUIREMENT_PRIORITIES = frozenset({"required", "recommended", "optional"})
QUALITY_ATTRIBUTES = frozenset(
    {
        "security",
        "performance",
        "reliability",
        "accessibility",
        "maintainability",
        "privacy",
        "operability",
    }
)
CONSTRAINT_TYPES = frozenset(
    {
        "architecture",
        "compatibility",
        "security",
        "migration",
        "operational",
        "performance",
        "data_retention",
        "provider_model",
        "operator_imposed",
        "phase_do_not_change",
    }
)
SEVERITIES = frozenset({"must", "should", "advisory"})
ENFORCEMENTS = frozenset({"deterministic", "test", "operator_review", "model_review"})
CRITICALITIES = frozenset({"required", "advisory", "informational"})
FACT_STATUSES = frozenset({"verified"})
LIKELIHOODS = frozenset({"low", "medium", "high"})
IMPACTS = frozenset({"low", "medium", "high", "critical"})
QUESTION_CLASSIFICATIONS = frozenset(
    {"blocking", "operator_decision_required", "non_blocking", "informational"}
)
RESOLVER_ROLES = frozenset(
    {"operator", "repository_verification", "system_policy", "model_review"}
)
INTERFACE_KINDS = frozenset({"api", "cli", "event", "data", "storage", "ui"})
CHANGE_PERMISSIONS = frozenset({"preserve", "additive", "breaking_authorized"})

_ID_RE = re.compile(
    r"^(GOAL|FACT|SCOPE|REQ|CON|AC|ARCH|IFACE|STRAT|VAL|ASM|RISK|Q|DEC)-[0-9]{3}$"
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_MARKUP_RE = re.compile(r"<\/?[A-Za-z][^>]*>|\bjavascript\s*:", re.I)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PlanningBriefError(ValueError):
    """Base class for invalid Brief values."""


class PlanningBriefSchemaError(PlanningBriefError):
    """A JSON object is not a supported Brief schema."""


class PlanningBriefValidationError(PlanningBriefError):
    """A Brief cannot be accepted by deterministic validation."""


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = unicodedata.normalize("NFC", str(key))
            if normalized_key in normalized:
                raise PlanningBriefSchemaError("duplicate normalized object key")
            normalized[normalized_key] = _normalize(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    raise PlanningBriefSchemaError(
        f"Brief value is not JSON-compatible: {type(value).__name__}"
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return normalized, sorted, compact UTF-8 JSON bytes."""

    try:
        return json.dumps(
            _normalize(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PlanningBriefSchemaError("Brief is not canonically serializable") from exc


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _text(value: Any, field_name: str = "text") -> str:
    if not isinstance(value, str):
        raise PlanningBriefSchemaError(f"{field_name} must be a string")
    return unicodedata.normalize("NFC", value)


def _refs(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise PlanningBriefSchemaError(f"{field_name} must be an array")
    normalized = tuple(dict.fromkeys(_text(item, field_name) for item in value))
    return tuple(sorted(normalized))


def _ids(value: Any, field_name: str) -> tuple[str, ...]:
    return _refs(value, field_name)


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def _record_to_dict(record: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in fields(record):
        value = getattr(record, field.name)
        if value is None:
            continue
        payload[field.name] = _thaw(value)
    return payload


class _RecordMixin:
    prefix: ClassVar[str]

    def to_dict(self) -> dict[str, Any]:
        return _record_to_dict(self)


@dataclass(frozen=True)
class InputManifestReference:
    id: str = ""
    hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "input_manifest_ref.id"))
        object.__setattr__(self, "hash", _text(self.hash, "input_manifest_ref.hash"))

    @classmethod
    def from_manifest(cls, manifest: InputManifest) -> "InputManifestReference":
        return cls(id=manifest.manifest_id, hash=manifest.manifest_hash)

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "hash": self.hash}


@dataclass(frozen=True)
class Goal(_RecordMixin):
    prefix: ClassVar[str] = "GOAL"
    id: str = ""
    statement: str = ""
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))


@dataclass(frozen=True)
class BackgroundFact(_RecordMixin):
    prefix: ClassVar[str] = "FACT"
    id: str = ""
    statement: str = ""
    status: str = ""
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(self, "status", _text(self.status, "status"))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))


@dataclass(frozen=True)
class ScopeItem(_RecordMixin):
    prefix: ClassVar[str] = "SCOPE"
    id: str = ""
    classification: str = ""
    statement: str = ""
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(
            self, "classification", _text(self.classification, "classification")
        )
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))


@dataclass(frozen=True)
class Requirement(_RecordMixin):
    prefix: ClassVar[str] = "REQ"
    id: str = ""
    type: str = ""
    statement: str = ""
    priority: str = ""
    source_refs: tuple[str, ...] = ()
    rationale: str | None = None
    quality_attribute: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(self, "type", _text(self.type, "type"))
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(self, "priority", _text(self.priority, "priority"))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))
        object.__setattr__(
            self, "rationale", _optional_text(self.rationale, "rationale")
        )
        object.__setattr__(
            self,
            "quality_attribute",
            _optional_text(self.quality_attribute, "quality_attribute"),
        )


@dataclass(frozen=True)
class Constraint(_RecordMixin):
    prefix: ClassVar[str] = "CON"
    id: str = ""
    type: str = ""
    statement: str = ""
    severity: str = ""
    enforcement: str = ""
    source_refs: tuple[str, ...] = ()
    applies_to_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("id", "type", "statement", "severity", "enforcement"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))
        object.__setattr__(
            self, "applies_to_refs", _ids(self.applies_to_refs, "applies_to_refs")
        )


@dataclass(frozen=True)
class AcceptanceCriterion(_RecordMixin):
    prefix: ClassVar[str] = "AC"
    id: str = ""
    statement: str = ""
    verification_method: str = ""
    source_requirement_ids: tuple[str, ...] = ()
    criticality: str = ""
    group_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(
            self,
            "verification_method",
            _text(self.verification_method, "verification_method"),
        )
        object.__setattr__(
            self,
            "source_requirement_ids",
            _ids(self.source_requirement_ids, "source_requirement_ids"),
        )
        object.__setattr__(self, "criticality", _text(self.criticality, "criticality"))
        object.__setattr__(self, "group_id", _optional_text(self.group_id, "group_id"))


@dataclass(frozen=True)
class ArchitectureContext(_RecordMixin):
    prefix: ClassVar[str] = "ARCH"
    id: str = ""
    kind: str = ""
    component: str = ""
    statement: str = ""
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("id", "kind", "component", "statement"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))


@dataclass(frozen=True)
class InterfaceContract(_RecordMixin):
    prefix: ClassVar[str] = "IFACE"
    id: str = ""
    kind: str = ""
    current_contract: str = ""
    change_permission: str = ""
    source_refs: tuple[str, ...] = ()

    @property
    def contract_summary(self) -> str:
        return self.current_contract

    def __post_init__(self) -> None:
        for name in ("id", "kind", "current_contract", "change_permission"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))


@dataclass(frozen=True)
class ImplementationStrategy(_RecordMixin):
    prefix: ClassVar[str] = "STRAT"
    id: str = ""
    statement: str = ""
    source_refs: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    constraint_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))
        object.__setattr__(
            self, "requirement_ids", _ids(self.requirement_ids, "requirement_ids")
        )
        object.__setattr__(
            self, "constraint_ids", _ids(self.constraint_ids, "constraint_ids")
        )


@dataclass(frozen=True)
class ValidationStrategy(_RecordMixin):
    prefix: ClassVar[str] = "VAL"
    id: str = ""
    statement: str = ""
    source_refs: tuple[str, ...] = ()
    acceptance_criterion_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))
        object.__setattr__(
            self,
            "acceptance_criterion_ids",
            _ids(self.acceptance_criterion_ids, "acceptance_criterion_ids"),
        )
        object.__setattr__(
            self, "requirement_ids", _ids(self.requirement_ids, "requirement_ids")
        )


@dataclass(frozen=True)
class Assumption(_RecordMixin):
    prefix: ClassVar[str] = "ASM"
    id: str = ""
    statement: str = ""
    source_refs: tuple[str, ...] = ()
    confidence: str | None = None
    impact_if_false: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))
        object.__setattr__(
            self, "confidence", _optional_text(self.confidence, "confidence")
        )
        object.__setattr__(
            self, "impact_if_false", _text(self.impact_if_false, "impact_if_false")
        )


@dataclass(frozen=True)
class Risk(_RecordMixin):
    prefix: ClassVar[str] = "RISK"
    id: str = ""
    description: str = ""
    likelihood: str = ""
    impact: str = ""
    source_refs: tuple[str, ...] = ()
    mitigation: str | None = None
    trigger: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(self, "description", _text(self.description, "description"))
        object.__setattr__(self, "likelihood", _text(self.likelihood, "likelihood"))
        object.__setattr__(self, "impact", _text(self.impact, "impact"))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))
        object.__setattr__(
            self, "mitigation", _optional_text(self.mitigation, "mitigation")
        )
        object.__setattr__(self, "trigger", _optional_text(self.trigger, "trigger"))


@dataclass(frozen=True)
class UnresolvedQuestion(_RecordMixin):
    prefix: ClassVar[str] = "Q"
    id: str = ""
    statement: str = ""
    classification: str = ""
    allowed_resolver_roles: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    temporary_assumption_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "id"))
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(
            self, "classification", _text(self.classification, "classification")
        )
        object.__setattr__(
            self,
            "allowed_resolver_roles",
            _ids(self.allowed_resolver_roles, "allowed_resolver_roles"),
        )
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))
        object.__setattr__(
            self,
            "temporary_assumption_id",
            _optional_text(self.temporary_assumption_id, "temporary_assumption_id"),
        )


@dataclass(frozen=True)
class OperatorDecision(_RecordMixin):
    prefix: ClassVar[str] = "DEC"
    id: str = ""
    statement: str = ""
    decision: str = ""
    source_refs: tuple[str, ...] = ()
    rationale: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "statement", "decision"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "source_refs", _refs(self.source_refs, "source_refs"))
        object.__setattr__(
            self, "rationale", _optional_text(self.rationale, "rationale")
        )


@dataclass(frozen=True)
class SourceReference:
    """Compact pointer to one immutable Input Manifest source."""

    source_id: str = ""
    source_type: str = ""
    content_hash: str = ""
    label: str | None = None

    @property
    def id(self) -> str:
        return self.source_id

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _text(self.source_id, "source_id"))
        object.__setattr__(self, "source_type", _text(self.source_type, "source_type"))
        object.__setattr__(
            self, "content_hash", _text(self.content_hash, "content_hash")
        )
        object.__setattr__(self, "label", _optional_text(self.label, "label"))

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "content_hash": self.content_hash,
        }
        if self.label is not None:
            payload["label"] = self.label
        return payload


Record = (
    Goal
    | BackgroundFact
    | ScopeItem
    | Requirement
    | Constraint
    | AcceptanceCriterion
    | ArchitectureContext
    | InterfaceContract
    | ImplementationStrategy
    | ValidationStrategy
    | Assumption
    | Risk
    | UnresolvedQuestion
    | OperatorDecision
)
_RecordT = TypeVar("_RecordT", bound=Record)


def _assign_records(records: Sequence[_RecordT], prefix: str) -> tuple[_RecordT, ...]:
    return tuple(
        replace(record, id=f"{prefix}-{index:03d}")
        for index, record in enumerate(records, 1)
    )


def assign_canonical_ids(
    records: Sequence[_RecordT], prefix: str | None = None
) -> tuple[_RecordT, ...]:
    """Assign category-prefixed application IDs in deterministic presentation order."""

    if prefix is None:
        if not records:
            return ()
        prefix = getattr(records[0], "prefix", "")
    normalized_prefix = _text(prefix, "prefix").upper()
    if not normalized_prefix:
        raise PlanningBriefSchemaError("record category prefix is required")
    return _assign_records(records, normalized_prefix)


@dataclass(frozen=True)
class PlanningBrief:
    """Immutable canonical Planning Brief content; lifecycle is checkpoint metadata."""

    schema_version: str
    input_manifest_ref: InputManifestReference
    objective: Goal
    background: tuple[BackgroundFact, ...]
    scope: tuple[ScopeItem, ...]
    requirements: tuple[Requirement, ...]
    constraints: tuple[Constraint, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    architecture_context: tuple[ArchitectureContext, ...]
    interface_contracts: tuple[InterfaceContract, ...]
    implementation_strategy: tuple[ImplementationStrategy, ...]
    validation_strategy: tuple[ValidationStrategy, ...]
    assumptions: tuple[Assumption, ...]
    risks: tuple[Risk, ...]
    unresolved_questions: tuple[UnresolvedQuestion, ...]
    operator_decisions: tuple[OperatorDecision, ...]
    source_references: tuple[SourceReference, ...]

    def __post_init__(self) -> None:
        if isinstance(self.input_manifest_ref, Mapping):
            object.__setattr__(
                self,
                "input_manifest_ref",
                InputManifestReference(**self.input_manifest_ref),
            )
        object.__setattr__(
            self, "schema_version", _text(self.schema_version, "schema_version")
        )
        for name in (
            "background",
            "scope",
            "requirements",
            "constraints",
            "acceptance_criteria",
            "architecture_context",
            "interface_contracts",
            "implementation_strategy",
            "validation_strategy",
            "assumptions",
            "risks",
            "unresolved_questions",
            "operator_decisions",
            "source_references",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(
            self,
            "source_references",
            tuple(sorted(self.source_references, key=lambda item: item.source_id)),
        )

    @classmethod
    def create(
        cls,
        *,
        input_manifest: InputManifest | None = None,
        input_manifest_ref: InputManifestReference | Mapping[str, Any] | None = None,
        objective: Goal,
        background: Sequence[BackgroundFact] = (),
        scope: Sequence[ScopeItem] = (),
        requirements: Sequence[Requirement] = (),
        constraints: Sequence[Constraint] = (),
        acceptance_criteria: Sequence[AcceptanceCriterion] = (),
        architecture_context: Sequence[ArchitectureContext] = (),
        interface_contracts: Sequence[InterfaceContract] = (),
        implementation_strategy: Sequence[ImplementationStrategy] = (),
        validation_strategy: Sequence[ValidationStrategy] = (),
        assumptions: Sequence[Assumption] = (),
        risks: Sequence[Risk] = (),
        unresolved_questions: Sequence[UnresolvedQuestion] = (),
        operator_decisions: Sequence[OperatorDecision] = (),
        source_references: Sequence[SourceReference] = (),
        schema_version: str = PLANNING_BRIEF_SCHEMA_VERSION,
    ) -> "PlanningBrief":
        if input_manifest is not None:
            manifest_ref = InputManifestReference.from_manifest(input_manifest)
        elif input_manifest_ref is not None:
            manifest_ref = (
                input_manifest_ref
                if isinstance(input_manifest_ref, InputManifestReference)
                else InputManifestReference(**input_manifest_ref)
            )
        else:
            raise PlanningBriefSchemaError(
                "input_manifest or input_manifest_ref is required"
            )
        return cls(
            schema_version=schema_version,
            input_manifest_ref=manifest_ref,
            objective=replace(objective, id="GOAL-001"),
            background=assign_canonical_ids(background, "FACT"),
            scope=assign_canonical_ids(scope, "SCOPE"),
            requirements=assign_canonical_ids(requirements, "REQ"),
            constraints=assign_canonical_ids(constraints, "CON"),
            acceptance_criteria=assign_canonical_ids(acceptance_criteria, "AC"),
            architecture_context=assign_canonical_ids(architecture_context, "ARCH"),
            interface_contracts=assign_canonical_ids(interface_contracts, "IFACE"),
            implementation_strategy=assign_canonical_ids(
                implementation_strategy, "STRAT"
            ),
            validation_strategy=assign_canonical_ids(validation_strategy, "VAL"),
            assumptions=assign_canonical_ids(assumptions, "ASM"),
            risks=assign_canonical_ids(risks, "RISK"),
            unresolved_questions=assign_canonical_ids(unresolved_questions, "Q"),
            operator_decisions=assign_canonical_ids(operator_decisions, "DEC"),
            source_references=tuple(source_references),
        )

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "input_manifest_ref": self.input_manifest_ref.to_dict(),
            "objective": self.objective.to_dict(),
            "background": [_record_to_dict(item) for item in self.background],
            "scope": [_record_to_dict(item) for item in self.scope],
            "requirements": [_record_to_dict(item) for item in self.requirements],
            "constraints": [_record_to_dict(item) for item in self.constraints],
            "acceptance_criteria": [
                _record_to_dict(item) for item in self.acceptance_criteria
            ],
            "architecture_context": [
                _record_to_dict(item) for item in self.architecture_context
            ],
            "interface_contracts": [
                _record_to_dict(item) for item in self.interface_contracts
            ],
            "implementation_strategy": [
                _record_to_dict(item) for item in self.implementation_strategy
            ],
            "validation_strategy": [
                _record_to_dict(item) for item in self.validation_strategy
            ],
            "assumptions": [_record_to_dict(item) for item in self.assumptions],
            "risks": [_record_to_dict(item) for item in self.risks],
            "unresolved_questions": [
                _record_to_dict(item) for item in self.unresolved_questions
            ],
            "operator_decisions": [
                _record_to_dict(item) for item in self.operator_decisions
            ],
            "source_references": [item.to_dict() for item in self.source_references],
        }

    def to_dict(self) -> dict[str, Any]:
        return self._canonical_payload()

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self._canonical_payload())

    def canonical_json(self) -> str:
        return self.canonical_bytes().decode("utf-8")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def brief_hash(self) -> str:
        return self.content_hash

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanningBrief":
        if not isinstance(raw, Mapping):
            raise PlanningBriefSchemaError("Planning Brief must be an object")
        allowed = {
            "schema_version",
            "input_manifest_ref",
            "objective",
            "background",
            "scope",
            "requirements",
            "constraints",
            "acceptance_criteria",
            "architecture_context",
            "interface_contracts",
            "implementation_strategy",
            "validation_strategy",
            "assumptions",
            "risks",
            "unresolved_questions",
            "operator_decisions",
            "source_references",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise PlanningBriefSchemaError(
                f"unknown Brief fields: {', '.join(unknown)}"
            )
        missing = sorted(allowed - set(raw))
        if missing:
            raise PlanningBriefSchemaError(
                f"missing Brief fields: {', '.join(missing)}"
            )
        manifest_ref_raw = raw["input_manifest_ref"]
        if not isinstance(manifest_ref_raw, Mapping):
            raise PlanningBriefSchemaError("input_manifest_ref must be an object")
        if set(manifest_ref_raw) != {"id", "hash"}:
            raise PlanningBriefSchemaError(
                "input_manifest_ref must contain only id and hash"
            )
        return cls(
            schema_version=_text(raw["schema_version"], "schema_version"),
            input_manifest_ref=InputManifestReference(**dict(manifest_ref_raw)),
            objective=_parse_record(raw["objective"], Goal),
            background=_parse_records(raw["background"], BackgroundFact),
            scope=_parse_records(raw["scope"], ScopeItem),
            requirements=_parse_records(raw["requirements"], Requirement),
            constraints=_parse_records(raw["constraints"], Constraint),
            acceptance_criteria=_parse_records(
                raw["acceptance_criteria"], AcceptanceCriterion
            ),
            architecture_context=_parse_records(
                raw["architecture_context"], ArchitectureContext
            ),
            interface_contracts=_parse_records(
                raw["interface_contracts"], InterfaceContract
            ),
            implementation_strategy=_parse_records(
                raw["implementation_strategy"], ImplementationStrategy
            ),
            validation_strategy=_parse_records(
                raw["validation_strategy"], ValidationStrategy
            ),
            assumptions=_parse_records(raw["assumptions"], Assumption),
            risks=_parse_records(raw["risks"], Risk),
            unresolved_questions=_parse_records(
                raw["unresolved_questions"], UnresolvedQuestion
            ),
            operator_decisions=_parse_records(
                raw["operator_decisions"], OperatorDecision
            ),
            source_references=_parse_source_references(raw["source_references"]),
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> "PlanningBrief":
        try:
            raw = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise PlanningBriefSchemaError("invalid Brief JSON") from exc
        return cls.from_dict(raw)


def _parse_record(raw: Any, record_type: type[_RecordT]) -> _RecordT:
    if not isinstance(raw, Mapping):
        raise PlanningBriefSchemaError("Brief record must be an object")
    allowed = {field.name for field in fields(record_type)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise PlanningBriefSchemaError(
            f"unknown {record_type.__name__} fields: {', '.join(unknown)}"
        )
    return record_type(**dict(raw))


def _parse_records(raw: Any, record_type: type[_RecordT]) -> tuple[_RecordT, ...]:
    if not isinstance(raw, list):
        raise PlanningBriefSchemaError(
            f"{record_type.__name__} collection must be an array"
        )
    return tuple(_parse_record(item, record_type) for item in raw)


def _parse_source_references(raw: Any) -> tuple[SourceReference, ...]:
    if not isinstance(raw, list):
        raise PlanningBriefSchemaError("source_references must be an array")
    allowed = {field.name for field in fields(SourceReference)}
    values = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise PlanningBriefSchemaError("source reference must be an object")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise PlanningBriefSchemaError(
                f"unknown SourceReference fields: {', '.join(unknown)}"
            )
        values.append(SourceReference(**dict(item)))
    return tuple(values)


_COLLECTIONS: tuple[tuple[str, type[Any]], ...] = (
    ("background", BackgroundFact),
    ("scope", ScopeItem),
    ("requirements", Requirement),
    ("constraints", Constraint),
    ("acceptance_criteria", AcceptanceCriterion),
    ("architecture_context", ArchitectureContext),
    ("interface_contracts", InterfaceContract),
    ("implementation_strategy", ImplementationStrategy),
    ("validation_strategy", ValidationStrategy),
    ("assumptions", Assumption),
    ("risks", Risk),
    ("unresolved_questions", UnresolvedQuestion),
    ("operator_decisions", OperatorDecision),
)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class PlanningBriefAcceptance:
    schema_valid: bool
    semantically_valid: bool
    protocol_acceptable: bool
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()
    operator_review_required: bool = False
    validator_version: str = PLANNING_BRIEF_VALIDATOR_VERSION

    @property
    def validation_hash(self) -> str:
        return canonical_json_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_valid": self.schema_valid,
            "semantically_valid": self.semantically_valid,
            "protocol_acceptable": self.protocol_acceptable,
            "operator_review_required": self.operator_review_required,
            "validator_version": self.validator_version,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }
        if include_hash:
            payload["validation_hash"] = self.validation_hash
        return payload

    @property
    def accepted(self) -> bool:
        return self.protocol_acceptable


def _issue(code: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, path=path, message=message)


def _all_records(brief: PlanningBrief) -> tuple[tuple[str, Record], ...]:
    values: list[tuple[str, Record]] = [("objective", brief.objective)]
    for collection_name, _record_type in _COLLECTIONS:
        values.extend(
            (collection_name, item) for item in getattr(brief, collection_name)
        )
    return tuple(values)


def _record_statement(record: Record) -> str:
    return getattr(record, "statement", getattr(record, "description", ""))


def _record_source_refs(record: Record) -> tuple[str, ...]:
    return tuple(getattr(record, "source_refs", ()))


def _normalized_scope_statement(statement: str) -> str:
    return " ".join(unicodedata.normalize("NFC", statement).casefold().split())


def _validate_string_safety(
    value: str, path: str, issues: list[ValidationIssue]
) -> None:
    if _CONTROL_RE.search(value):
        issues.append(
            _issue("control_character", path, "control characters are forbidden")
        )
    if _UNSAFE_MARKUP_RE.search(value):
        issues.append(
            _issue("unsafe_markup", path, "raw HTML and unsafe links are forbidden")
        )


def validate_planning_brief(
    brief: PlanningBrief,
    *,
    input_manifest: InputManifest | None = None,
) -> PlanningBriefAcceptance:
    """Run deterministic schema, reference, scope, and coverage validation."""

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    if brief.schema_version != PLANNING_BRIEF_SCHEMA_VERSION:
        errors.append(
            _issue(
                "unsupported_schema",
                "schema_version",
                "unsupported Brief schema version",
            )
        )
    ref = brief.input_manifest_ref
    if not ref.id or not ref.hash or not _HASH_RE.fullmatch(ref.hash):
        errors.append(
            _issue(
                "invalid_manifest_ref",
                "input_manifest_ref",
                "manifest id and lowercase SHA-256 hash are required",
            )
        )
    if input_manifest is not None:
        try:
            input_manifest.validate()
        except Exception as exc:
            errors.append(
                _issue("invalid_input_manifest", "input_manifest_ref", str(exc))
            )
        else:
            if (
                ref.id != input_manifest.manifest_id
                or ref.hash != input_manifest.manifest_hash
            ):
                errors.append(
                    _issue(
                        "manifest_mismatch",
                        "input_manifest_ref",
                        "Brief is not bound to the supplied Input Manifest",
                    )
                )
    elif ref.id and ref.hash:
        errors.append(
            _issue(
                "manifest_unresolved",
                "input_manifest_ref",
                "Input Manifest is required to resolve Brief sources",
            )
        )

    if not _ID_RE.fullmatch(brief.objective.id) or not brief.objective.id.startswith(
        "GOAL-"
    ):
        errors.append(
            _issue(
                "invalid_id",
                "objective.id",
                "objective must have a GOAL-NNN application ID",
            )
        )

    records = _all_records(brief)
    ids_seen: dict[str, str] = {}
    for collection_name, record in records:
        path = f"{collection_name}.{record.id or '<missing>'}"
        if not _ID_RE.fullmatch(record.id) or not record.id.startswith(
            getattr(record, "prefix", "")
        ):
            errors.append(
                _issue(
                    "invalid_id",
                    f"{path}.id",
                    "record ID has an invalid category prefix or ordinal",
                )
            )
        if record.id in ids_seen:
            errors.append(
                _issue(
                    "duplicate_id",
                    f"{path}.id",
                    f"record ID duplicates {ids_seen[record.id]}",
                )
            )
        else:
            ids_seen[record.id] = path
        source_refs = _record_source_refs(record)
        # Acceptance criteria inherit provenance through their requirement or
        # objective trace; Phase 28D does not give them a separate source_refs
        # field. Every other content record must cite manifest sources.
        if not source_refs and not isinstance(record, AcceptanceCriterion):
            errors.append(
                _issue(
                    "missing_source_refs",
                    f"{path}.source_refs",
                    "record must cite at least one source",
                )
            )
        for field in fields(record):
            value = getattr(record, field.name)
            if isinstance(value, str):
                _validate_string_safety(value, f"{path}.{field.name}", errors)
                limit = (
                    MAX_OBJECTIVE_CHARACTERS
                    if collection_name == "objective"
                    else MAX_STATEMENT_CHARACTERS
                )
                if field.name in {
                    "rationale",
                    "mitigation",
                    "trigger",
                    "verification_method",
                    "impact_if_false",
                }:
                    limit = MAX_DETAIL_CHARACTERS
                if len(value) > limit:
                    errors.append(
                        _issue(
                            "size_limit",
                            f"{path}.{field.name}",
                            f"field exceeds {limit} characters",
                        )
                    )

    source_refs_by_id = {source.source_id: source for source in brief.source_references}
    if not (1 <= len(brief.source_references) <= 250):
        errors.append(
            _issue(
                "count_limit",
                "source_references",
                "source_references must contain 1-250 entries",
            )
        )
    if len(source_refs_by_id) != len(brief.source_references):
        errors.append(
            _issue(
                "duplicate_source_reference",
                "source_references",
                "source references must be unique",
            )
        )
    manifest_sources = {}
    if input_manifest is not None:
        manifest_sources = {
            source.source_id: source for source in input_manifest.sources
        }
    for index, source in enumerate(brief.source_references):
        path = f"source_references[{index}]"
        if (
            not source.source_id
            or not source.source_type
            or not _HASH_RE.fullmatch(source.content_hash)
        ):
            errors.append(
                _issue(
                    "invalid_source_reference",
                    path,
                    "source reference identity, type, and hash are required",
                )
            )
        manifest_source = manifest_sources.get(source.source_id)
        if input_manifest is not None:
            if manifest_source is None:
                errors.append(
                    _issue(
                        "unresolved_source_reference",
                        path,
                        "source reference does not resolve to the bound manifest",
                    )
                )
            else:
                if (
                    source.source_type != manifest_source.source_type
                    or source.content_hash != manifest_source.content_hash
                ):
                    errors.append(
                        _issue(
                            "source_reference_hash_mismatch",
                            path,
                            "source reference metadata differs from the manifest",
                        )
                    )
    used_sources = {
        source_ref
        for _, record in records
        for source_ref in _record_source_refs(record)
    }
    for source_ref in sorted(used_sources - set(source_refs_by_id)):
        errors.append(
            _issue(
                "unresolved_source_reference",
                "records",
                f"record source reference is not declared: {source_ref}",
            )
        )
    for source_ref in sorted(set(source_refs_by_id) - used_sources):
        warnings.append(
            _issue(
                "orphan_source_reference",
                "source_references",
                f"source reference is not used: {source_ref}",
            )
        )

    limits = {
        "background": (0, 20),
        "scope": (1, 50),
        "requirements": (1, 100),
        "constraints": (0, 100),
        "acceptance_criteria": (1, 100),
        "architecture_context": (0, 40),
        "interface_contracts": (0, 40),
        "implementation_strategy": (1, 30),
        "validation_strategy": (1, 30),
        "assumptions": (0, 40),
        "risks": (0, 40),
        "unresolved_questions": (0, 30),
        "operator_decisions": (0, 40),
    }
    for collection_name, (lower, upper) in limits.items():
        count = len(getattr(brief, collection_name))
        if not lower <= count <= upper:
            errors.append(
                _issue(
                    "count_limit",
                    collection_name,
                    f"{collection_name} must contain {lower}-{upper} entries",
                )
            )
    if not any(item.classification == "in_scope" for item in brief.scope):
        errors.append(
            _issue(
                "scope_missing_in_scope",
                "scope",
                "scope must contain at least one in_scope item",
            )
        )

    scope_by_statement: dict[str, tuple[str, str]] = {}
    for item in brief.scope:
        key = _normalized_scope_statement(item.statement)
        prior = scope_by_statement.get(key)
        if prior is not None:
            prior_id, prior_classification = prior
            if prior_classification != item.classification:
                errors.append(
                    _issue(
                        "scope_precedence_conflict",
                        f"scope.{item.id}",
                        f"exact scope overlap conflicts with {prior_id}; precedence cannot silently resolve it",
                    )
                )
            else:
                errors.append(
                    _issue(
                        "duplicate_scope",
                        f"scope.{item.id}",
                        f"scope duplicates {prior_id}",
                    )
                )
        else:
            scope_by_statement[key] = (item.id, item.classification)

    requirements = {item.id: item for item in brief.requirements}
    constraints = {item.id: item for item in brief.constraints}
    criteria = {item.id: item for item in brief.acceptance_criteria}
    assumptions = {item.id: item for item in brief.assumptions}
    for item in brief.requirements:
        if (
            item.type not in REQUIREMENT_TYPES
            or item.priority not in REQUIREMENT_PRIORITIES
        ):
            errors.append(
                _issue(
                    "invalid_requirement_enum",
                    f"requirements.{item.id}",
                    "requirement type or priority is invalid",
                )
            )
        if (
            item.type == "non_functional"
            and item.quality_attribute not in QUALITY_ATTRIBUTES
        ):
            errors.append(
                _issue(
                    "missing_quality_attribute",
                    f"requirements.{item.id}",
                    "non-functional requirements require a valid quality_attribute",
                )
            )
        if item.type == "functional" and item.quality_attribute is not None:
            warnings.append(
                _issue(
                    "unexpected_quality_attribute",
                    f"requirements.{item.id}",
                    "functional requirement has a quality_attribute",
                )
            )
    for item in brief.background:
        if item.status not in FACT_STATUSES:
            errors.append(
                _issue(
                    "invalid_fact_status",
                    f"background.{item.id}.status",
                    "background facts must be verified",
                )
            )
    for item in brief.scope:
        if item.classification not in SCOPE_CLASSIFICATIONS:
            errors.append(
                _issue(
                    "invalid_scope_classification",
                    f"scope.{item.id}.classification",
                    "scope classification is invalid",
                )
            )
    for item in brief.constraints:
        if (
            item.type not in CONSTRAINT_TYPES
            or item.severity not in SEVERITIES
            or item.enforcement not in ENFORCEMENTS
        ):
            errors.append(
                _issue(
                    "invalid_constraint_enum",
                    f"constraints.{item.id}",
                    "constraint type, severity, or enforcement is invalid",
                )
            )
        if item.enforcement == "model_review" and item.severity == "must":
            errors.append(
                _issue(
                    "must_constraint_not_deterministic",
                    f"constraints.{item.id}",
                    "must constraints cannot rely only on model review",
                )
            )
        for target in item.applies_to_refs:
            if target not in ids_seen:
                errors.append(
                    _issue(
                        "unresolved_reference",
                        f"constraints.{item.id}.applies_to_refs",
                        f"unknown record reference: {target}",
                    )
                )
    required_criteria: set[str] = set()
    for item in brief.acceptance_criteria:
        if item.criticality not in CRITICALITIES:
            errors.append(
                _issue(
                    "invalid_criterion_enum",
                    f"acceptance_criteria.{item.id}.criticality",
                    "criterion criticality is invalid",
                )
            )
        if not item.verification_method.strip():
            errors.append(
                _issue(
                    "missing_verification_method",
                    f"acceptance_criteria.{item.id}",
                    "criterion verification_method is required",
                )
            )
        if not item.source_requirement_ids:
            errors.append(
                _issue(
                    "missing_criterion_traceability",
                    f"acceptance_criteria.{item.id}",
                    "criterion must trace to a requirement or objective",
                )
            )
        for reference in item.source_requirement_ids:
            if reference != brief.objective.id and reference not in requirements:
                errors.append(
                    _issue(
                        "unresolved_reference",
                        f"acceptance_criteria.{item.id}.source_requirement_ids",
                        f"unknown requirement or objective: {reference}",
                    )
                )
        if item.criticality == "required":
            required_criteria.add(item.id)
    covered_criteria = {
        reference
        for item in brief.validation_strategy
        for reference in item.acceptance_criterion_ids
    }
    missing_criteria = sorted(required_criteria - covered_criteria)
    if missing_criteria:
        errors.append(
            _issue(
                "acceptance_coverage_gap",
                "validation_strategy",
                f"required criteria lack explicit validation coverage: {', '.join(missing_criteria)}",
            )
        )
    for item in brief.implementation_strategy:
        for reference in (*item.requirement_ids, *item.constraint_ids):
            if reference not in ids_seen:
                errors.append(
                    _issue(
                        "unresolved_reference",
                        f"implementation_strategy.{item.id}",
                        f"unknown strategy reference: {reference}",
                    )
                )
    for item in brief.validation_strategy:
        for reference in (*item.acceptance_criterion_ids, *item.requirement_ids):
            if reference not in ids_seen:
                errors.append(
                    _issue(
                        "unresolved_reference",
                        f"validation_strategy.{item.id}",
                        f"unknown validation reference: {reference}",
                    )
                )
    for item in brief.assumptions:
        if item.confidence is not None and item.confidence not in {
            "low",
            "medium",
            "high",
        }:
            errors.append(
                _issue(
                    "invalid_assumption_confidence",
                    f"assumptions.{item.id}.confidence",
                    "assumption confidence is invalid",
                )
            )
    for item in brief.risks:
        if item.likelihood not in LIKELIHOODS or item.impact not in IMPACTS:
            errors.append(
                _issue(
                    "invalid_risk_enum",
                    f"risks.{item.id}",
                    "risk likelihood or impact is invalid",
                )
            )
    for item in brief.unresolved_questions:
        if item.classification not in QUESTION_CLASSIFICATIONS:
            errors.append(
                _issue(
                    "invalid_question_classification",
                    f"unresolved_questions.{item.id}",
                    "question classification is invalid",
                )
            )
        if (
            not item.allowed_resolver_roles
            or not set(item.allowed_resolver_roles) <= RESOLVER_ROLES
        ):
            errors.append(
                _issue(
                    "invalid_resolver_roles",
                    f"unresolved_questions.{item.id}",
                    "question resolver roles are invalid",
                )
            )
        if (
            item.classification == "non_blocking"
            and item.temporary_assumption_id not in assumptions
        ):
            errors.append(
                _issue(
                    "question_assumption_required",
                    f"unresolved_questions.{item.id}",
                    "non-blocking questions require a linked assumption",
                )
            )
    for item in brief.operator_decisions:
        if not item.decision.strip():
            errors.append(
                _issue(
                    "missing_operator_decision",
                    f"operator_decisions.{item.id}",
                    "operator decision is required",
                )
            )
        if input_manifest is not None:
            for source_ref in item.source_refs:
                source = manifest_sources.get(source_ref)
                if source is not None and source.source_type not in {
                    "planning_request",
                    "clarification_message",
                }:
                    errors.append(
                        _issue(
                            "operator_source_required",
                            f"operator_decisions.{item.id}",
                            "operator decisions must cite operator message sources",
                        )
                    )
    for item in brief.interface_contracts:
        if (
            item.kind not in INTERFACE_KINDS
            or item.change_permission not in CHANGE_PERMISSIONS
        ):
            errors.append(
                _issue(
                    "invalid_interface_enum",
                    f"interface_contracts.{item.id}",
                    "interface kind or change permission is invalid",
                )
            )

    canonical_bytes = brief.canonical_bytes()
    if (
        len(canonical_bytes.decode("utf-8")) > MAX_BRIEF_CHARACTERS
        or len(canonical_bytes) > MAX_BRIEF_BYTES
    ):
        errors.append(
            _issue("brief_size_limit", "brief", "canonical Brief exceeds size limit")
        )
    schema_valid = not any(
        issue.code
        in {
            "unsupported_schema",
            "invalid_manifest_ref",
            "invalid_id",
            "duplicate_id",
            "missing_source_refs",
            "count_limit",
            "invalid_source_reference",
            "unknown",
            "control_character",
            "unsafe_markup",
            "size_limit",
            "brief_size_limit",
        }
        for issue in errors
    )
    semantically_valid = schema_valid and not errors
    blocking_questions = any(
        item.classification in {"blocking", "operator_decision_required"}
        for item in brief.unresolved_questions
    )
    operator_review_required = bool(
        brief.operator_decisions
        or brief.assumptions
        or any(item.severity == "should" for item in brief.constraints)
    )
    protocol_acceptable = semantically_valid and not blocking_questions
    return PlanningBriefAcceptance(
        schema_valid=schema_valid,
        semantically_valid=semantically_valid,
        protocol_acceptable=protocol_acceptable,
        errors=tuple(errors),
        warnings=tuple(warnings),
        operator_review_required=operator_review_required,
    )


def require_valid_planning_brief(
    brief: PlanningBrief, *, input_manifest: InputManifest | None = None
) -> PlanningBriefAcceptance:
    acceptance = validate_planning_brief(brief, input_manifest=input_manifest)
    if not acceptance.semantically_valid:
        detail = "; ".join(
            f"{issue.code}: {issue.path}" for issue in acceptance.errors[:8]
        )
        raise PlanningBriefValidationError(detail or "Planning Brief is invalid")
    return acceptance


@dataclass(frozen=True)
class RecordChange:
    collection: str
    record_id: str
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"collection": self.collection, "record_id": self.record_id}
        if self.before is not None:
            payload["before"] = _thaw(self.before)
        if self.after is not None:
            payload["after"] = _thaw(self.after)
        return payload


@dataclass(frozen=True)
class PlanningBriefStructuralDiff:
    added_records: tuple[RecordChange, ...] = ()
    removed_records: tuple[RecordChange, ...] = ()
    changed_records: tuple[RecordChange, ...] = ()
    reordered_presentation: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(
            self.added_records
            or self.removed_records
            or self.changed_records
            or self.reordered_presentation
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "added_records": [item.to_dict() for item in self.added_records],
            "removed_records": [item.to_dict() for item in self.removed_records],
            "changed_records": [item.to_dict() for item in self.changed_records],
            "reordered_presentation": list(self.reordered_presentation),
        }


def _record_maps(brief: PlanningBrief) -> dict[str, dict[str, Mapping[str, Any]]]:
    values: dict[str, dict[str, Mapping[str, Any]]] = {
        "objective": {brief.objective.id: brief.objective.to_dict()}
    }
    for collection_name, _record_type in _COLLECTIONS:
        values[collection_name] = {
            item.id: item.to_dict() for item in getattr(brief, collection_name)
        }
    return values


def structural_diff(
    before: PlanningBrief, after: PlanningBrief
) -> PlanningBriefStructuralDiff:
    """Compare canonical content, not Markdown rendering."""

    before_maps = _record_maps(before)
    after_maps = _record_maps(after)
    added: list[RecordChange] = []
    removed: list[RecordChange] = []
    changed: list[RecordChange] = []
    reordered: list[str] = []
    for collection_name, _record_type in (("objective", Goal), *_COLLECTIONS):
        old = before_maps[collection_name]
        new = after_maps[collection_name]
        for record_id in sorted(set(new) - set(old)):
            added.append(
                RecordChange(collection_name, record_id, after=_freeze(new[record_id]))
            )
        for record_id in sorted(set(old) - set(new)):
            removed.append(
                RecordChange(collection_name, record_id, before=_freeze(old[record_id]))
            )
        for record_id in sorted(set(old) & set(new)):
            if old[record_id] != new[record_id]:
                changed.append(
                    RecordChange(
                        collection_name,
                        record_id,
                        before=_freeze(old[record_id]),
                        after=_freeze(new[record_id]),
                    )
                )
        old_order = list(old)
        new_order = list(new)
        if set(old_order) == set(new_order) and old_order != new_order:
            reordered.append(collection_name)
    return PlanningBriefStructuralDiff(
        tuple(added), tuple(removed), tuple(changed), tuple(reordered)
    )


diff_planning_briefs = structural_diff


def _escape(value: Any) -> str:
    text = html.escape(str(value), quote=False)
    text = text.replace("\\", "\\\\")
    for marker in ("`", "*", "_", "[", "]", "#", "|", ">"):
        text = text.replace(marker, "\\" + marker)
    return text


def _render_record_list(
    title: str, records: Sequence[Record], render: Any
) -> list[str]:
    lines = [f"## {_escape(title)}"]
    if not records:
        lines.append("_None._")
        return lines
    for record in records:
        lines.append(render(record))
    return lines


def render_planning_brief(brief: PlanningBrief) -> str:
    """Render a deterministic compatibility Markdown projection."""

    lines = [
        "# Planning Brief",
        "",
        f"Schema version: `{_escape(brief.schema_version)}`",
        f"Input Manifest: `{_escape(brief.input_manifest_ref.id)}` ({_escape(brief.input_manifest_ref.hash)})",
        "",
        "## Objective",
        f"- **{_escape(brief.objective.id)}** — {_escape(brief.objective.statement)}",
        "",
    ]
    lines.extend(
        _render_record_list(
            "Background",
            brief.background,
            lambda item: f"- **{_escape(item.id)}** [{_escape(item.status)}] — {_escape(item.statement)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Scope",
            brief.scope,
            lambda item: f"- **{_escape(item.id)}** `{_escape(item.classification)}` — {_escape(item.statement)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Requirements",
            brief.requirements,
            lambda item: f"- **{_escape(item.id)}** `{_escape(item.priority)}` `{_escape(item.type)}` — {_escape(item.statement)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Constraints",
            brief.constraints,
            lambda item: f"- **{_escape(item.id)}** `{_escape(item.severity)}` — {_escape(item.statement)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Acceptance Criteria",
            brief.acceptance_criteria,
            lambda item: f"- **{_escape(item.id)}** `{_escape(item.criticality)}` — {_escape(item.statement)} (verify: {_escape(item.verification_method)})",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Architecture Context",
            brief.architecture_context,
            lambda item: f"- **{_escape(item.id)}** `{_escape(item.component)}` — {_escape(item.statement)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Interface Contracts",
            brief.interface_contracts,
            lambda item: f"- **{_escape(item.id)}** `{_escape(item.kind)}` `{_escape(item.change_permission)}` — {_escape(item.current_contract)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Implementation Strategy",
            brief.implementation_strategy,
            lambda item: f"- **{_escape(item.id)}** — {_escape(item.statement)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Validation Strategy",
            brief.validation_strategy,
            lambda item: f"- **{_escape(item.id)}** — {_escape(item.statement)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Assumptions",
            brief.assumptions,
            lambda item: f"- **{_escape(item.id)}** — {_escape(item.statement)} (impact if false: {_escape(item.impact_if_false)})",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Risks",
            brief.risks,
            lambda item: f"- **{_escape(item.id)}** `{_escape(item.likelihood)}/{_escape(item.impact)}` — {_escape(item.description)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Unresolved Questions",
            brief.unresolved_questions,
            lambda item: f"- **{_escape(item.id)}** `{_escape(item.classification)}` — {_escape(item.statement)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Operator Decisions",
            brief.operator_decisions,
            lambda item: f"- **{_escape(item.id)}** — {_escape(item.statement)} Decision: {_escape(item.decision)}",
        )
    )
    lines.append("")
    lines.extend(
        _render_record_list(
            "Source References",
            brief.source_references,
            lambda item: f"- **{_escape(item.source_id)}** `{_escape(item.source_type)}` `{_escape(item.content_hash)}`",
        )
    )
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class PlanningBriefCompatibilityProjection:
    source_brief_hash: str
    renderer_version: str
    requirements: str
    design: str
    implementation_plan: str
    planner_markdown: str = ""

    @property
    def projection_hashes(self) -> Mapping[str, str]:
        return MappingProxyType(
            {name: canonical_json_hash(value) for name, value in self.values().items()}
        )

    def values(self) -> dict[str, str]:
        return {
            "requirements": self.requirements,
            "design": self.design,
            "implementation_plan": self.implementation_plan,
            "planner_markdown": self.planner_markdown,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_brief_hash": self.source_brief_hash,
            "renderer_version": self.renderer_version,
            "projections": self.values(),
            "projection_hashes": dict(self.projection_hashes),
        }


def _render_projection(
    title: str, sections: Sequence[tuple[str, Sequence[Record]]]
) -> str:
    lines = [f"# {title}", ""]
    for section_title, records in sections:
        lines.append(f"## {_escape(section_title)}")
        if not records:
            lines.append("_None._")
        else:
            for item in records:
                text = getattr(item, "statement", getattr(item, "description", ""))
                lines.append(f"- **{_escape(item.id)}** — {_escape(text)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def project_compatibility(
    brief: PlanningBrief,
    *,
    task_plan: str | None = None,
    renderer_version: str = PLANNING_BRIEF_RENDERER_VERSION,
) -> PlanningBriefCompatibilityProjection:
    """Produce the legacy artifact values without making them authoritative."""

    requirements = _render_projection(
        "Requirements",
        (
            ("Objective", (brief.objective,)),
            ("Scope", brief.scope),
            ("Requirements", brief.requirements),
            ("Constraints", brief.constraints),
            ("Acceptance Criteria", brief.acceptance_criteria),
            ("Assumptions", brief.assumptions),
            ("Unresolved Questions", brief.unresolved_questions),
        ),
    )
    design = _render_projection(
        "Design",
        (
            ("Architecture Context", brief.architecture_context),
            ("Interface Contracts", brief.interface_contracts),
            ("Risks", brief.risks),
        ),
    )
    implementation = _render_projection(
        "Implementation Plan",
        (
            ("Implementation Strategy", brief.implementation_strategy),
            ("Validation Strategy", brief.validation_strategy),
        ),
    )
    return PlanningBriefCompatibilityProjection(
        source_brief_hash=brief.content_hash,
        renderer_version=renderer_version,
        requirements=requirements,
        design=design,
        implementation_plan=implementation,
        planner_markdown=task_plan or "",
    )


compatibility_projection = project_compatibility
render_markdown = render_planning_brief
validate_brief = validate_planning_brief
InputManifestRef = InputManifestReference
BriefAcceptance = PlanningBriefAcceptance
Background = BackgroundFact


__all__ = [
    "AcceptanceCriterion",
    "ArchitectureContext",
    "Assumption",
    "BackgroundFact",
    "Background",
    "BriefAcceptance",
    "CHANGE_PERMISSIONS",
    "Constraint",
    "Goal",
    "InputManifestReference",
    "InputManifestRef",
    "ImplementationStrategy",
    "InterfaceContract",
    "OperatorDecision",
    "PlanningBrief",
    "PlanningBriefAcceptance",
    "PlanningBriefCompatibilityProjection",
    "PlanningBriefError",
    "PlanningBriefRendererVersion",
    "PlanningBriefSchemaError",
    "PlanningBriefStructuralDiff",
    "PlanningBriefValidationError",
    "Requirement",
    "Risk",
    "ScopeItem",
    "SourceReference",
    "UnresolvedQuestion",
    "ValidationIssue",
    "ValidationStrategy",
    "assign_canonical_ids",
    "canonical_json_bytes",
    "canonical_json_hash",
    "compatibility_projection",
    "diff_planning_briefs",
    "project_compatibility",
    "render_markdown",
    "render_planning_brief",
    "require_valid_planning_brief",
    "structural_diff",
    "validate_brief",
    "validate_planning_brief",
    "PLANNING_BRIEF_RENDERER_VERSION",
    "PLANNING_BRIEF_SCHEMA_VERSION",
    "PLANNING_BRIEF_STAGE_NAME",
    "PLANNING_BRIEF_STAGE_VERSION",
    "PLANNING_BRIEF_VALIDATOR_VERSION",
]

# Backward-compatible spelling for callers that treat the renderer version as
# a value rather than a module constant.
PlanningBriefRendererVersion = str
