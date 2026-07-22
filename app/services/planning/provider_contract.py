"""Generated provider contracts for Protocol v2 planning candidates.

The provider contract is rendered from the same frozen dataclass record types
that the strict candidate parsers instantiate.  It is descriptive metadata for
the provider boundary; canonical IDs, hashes, topology, and lifecycle values
remain application-owned.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import MISSING, fields, is_dataclass
import json
import types
from typing import Any, Union, get_args, get_origin, get_type_hints

from app.services.planning.planning_brief import (
    AcceptanceCriterion,
    ArchitectureContext,
    Assumption,
    BackgroundFact,
    CHANGE_PERMISSIONS,
    Constraint,
    CONSTRAINT_TYPES,
    CRITICALITIES,
    ENFORCEMENTS,
    FACT_STATUSES,
    Goal,
    IMPACTS,
    ImplementationStrategy,
    InterfaceContract,
    INTERFACE_KINDS,
    LIKELIHOODS,
    OperatorDecision,
    QUALITY_ATTRIBUTES,
    QUESTION_CLASSIFICATIONS,
    Requirement,
    REQUIREMENT_PRIORITIES,
    REQUIREMENT_TYPES,
    RESOLVER_ROLES,
    Risk,
    SCOPE_CLASSIFICATIONS,
    SEVERITIES,
    ScopeItem,
    UnresolvedQuestion,
    ValidationStrategy,
)
from app.services.planning.structured_task_plan import (
    BLOCKING_STATES,
    CONFIDENCE_VALUES,
    DEPENDENCY_TYPES,
    EFFORT_UNITS,
    EXECUTION_OWNER_ROLES,
    GROUP_KINDS,
    GROUP_SKIP_POLICIES,
    ISOLATION_MODES,
    NETWORK_MODES,
    OMISSION_REASON_CODES,
    OMISSION_TARGET_KINDS,
    PARALLELISM_MODES,
    REVIEW_MODES,
    TASK_CATEGORIES,
    TASK_COMPLEXITIES,
    TASK_PRIORITIES,
    TRACEABILITY_ROLES,
    TRACEABILITY_TARGET_KINDS,
    WRITE_SCOPES,
    Dependency,
    EffortEstimate,
    ExecutionGroup,
    ExecutionProfile,
    IntentionalOmission,
    Task,
    Traceability,
    WorkItem,
)


PLANNING_BRIEF_CANDIDATE_RECORD_TYPES: Mapping[str, type[Any]] = {
    "background": BackgroundFact,
    "scope": ScopeItem,
    "requirements": Requirement,
    "constraints": Constraint,
    "acceptance_criteria": AcceptanceCriterion,
    "architecture_context": ArchitectureContext,
    "interface_contracts": InterfaceContract,
    "implementation_strategy": ImplementationStrategy,
    "validation_strategy": ValidationStrategy,
    "assumptions": Assumption,
    "risks": Risk,
    "unresolved_questions": UnresolvedQuestion,
    "operator_decisions": OperatorDecision,
}
PLANNING_BRIEF_CANDIDATE_FIELDS = (
    "objective",
    *PLANNING_BRIEF_CANDIDATE_RECORD_TYPES,
)

STRUCTURED_TASK_PLAN_CANDIDATE_RECORD_TYPES: Mapping[str, type[Any]] = {
    "Task": Task,
    "Dependency": Dependency,
    "ExecutionGroup": ExecutionGroup,
    "IntentionalOmission": IntentionalOmission,
    "WorkItem": WorkItem,
    "Traceability": Traceability,
    "EffortEstimate": EffortEstimate,
    "ExecutionProfile": ExecutionProfile,
}
STRUCTURED_TASK_PLAN_CANDIDATE_FIELDS = (
    "tasks",
    "dependencies",
    "execution_groups",
    "intentional_omissions",
)


_BRIEF_ENUMS: Mapping[tuple[str, str], Sequence[str]] = {
    ("BackgroundFact", "status"): FACT_STATUSES,
    ("ScopeItem", "classification"): SCOPE_CLASSIFICATIONS,
    ("Requirement", "type"): REQUIREMENT_TYPES,
    ("Requirement", "priority"): REQUIREMENT_PRIORITIES,
    ("Requirement", "quality_attribute"): (*QUALITY_ATTRIBUTES,),
    ("Constraint", "type"): CONSTRAINT_TYPES,
    ("Constraint", "severity"): SEVERITIES,
    ("Constraint", "enforcement"): ENFORCEMENTS,
    ("AcceptanceCriterion", "criticality"): CRITICALITIES,
    ("ArchitectureContext", "kind"): INTERFACE_KINDS,
    ("InterfaceContract", "kind"): INTERFACE_KINDS,
    ("InterfaceContract", "change_permission"): CHANGE_PERMISSIONS,
    ("Risk", "likelihood"): LIKELIHOODS,
    ("Risk", "impact"): IMPACTS,
    ("UnresolvedQuestion", "classification"): QUESTION_CLASSIFICATIONS,
    ("UnresolvedQuestion", "allowed_resolver_roles"): RESOLVER_ROLES,
}
_TASK_PLAN_ENUMS: Mapping[tuple[str, str], Sequence[str]] = {
    ("Task", "priority"): TASK_PRIORITIES,
    ("Task", "complexity"): TASK_COMPLEXITIES,
    ("Task", "category"): TASK_CATEGORIES,
    ("Task", "blocking_state"): BLOCKING_STATES,
    ("Dependency", "type"): DEPENDENCY_TYPES,
    ("ExecutionGroup", "kind"): GROUP_KINDS,
    ("ExecutionGroup", "skip_policy"): GROUP_SKIP_POLICIES,
    ("EffortEstimate", "unit"): EFFORT_UNITS,
    ("EffortEstimate", "confidence"): CONFIDENCE_VALUES,
    ("ExecutionProfile", "owner_role"): EXECUTION_OWNER_ROLES,
    ("ExecutionProfile", "isolation"): ISOLATION_MODES,
    ("ExecutionProfile", "write_scope"): (*WRITE_SCOPES,),
    ("ExecutionProfile", "network"): NETWORK_MODES,
    ("ExecutionProfile", "parallelism"): PARALLELISM_MODES,
    ("ExecutionProfile", "review"): REVIEW_MODES,
    ("Traceability", "target_kind"): TRACEABILITY_TARGET_KINDS,
    ("Traceability", "role"): TRACEABILITY_ROLES,
    ("IntentionalOmission", "target_kind"): OMISSION_TARGET_KINDS,
    ("IntentionalOmission", "reason_code"): OMISSION_REASON_CODES,
}

_MANIFEST_SOURCE_REFERENCE_SEMANTICS = (
    "every value must exactly equal a canonical source_id supplied in the "
    "bounded Input Manifest"
)
_SEMANTIC_RECORD_REFERENCE_SEMANTICS = (
    "semantic record reference using objective or collection[index]; "
    "the application assigns canonical IDs"
)
_REFERENCE_FIELD_SEMANTICS = {
    "source_refs": _MANIFEST_SOURCE_REFERENCE_SEMANTICS,
    "applies_to_refs": _SEMANTIC_RECORD_REFERENCE_SEMANTICS,
    "source_requirement_ids": _SEMANTIC_RECORD_REFERENCE_SEMANTICS,
    "requirement_ids": _SEMANTIC_RECORD_REFERENCE_SEMANTICS,
    "constraint_ids": _SEMANTIC_RECORD_REFERENCE_SEMANTICS,
    "acceptance_criterion_ids": _SEMANTIC_RECORD_REFERENCE_SEMANTICS,
    "temporary_assumption_id": _SEMANTIC_RECORD_REFERENCE_SEMANTICS,
}

BRIEF_SOURCE_REFERENCE_INSTRUCTIONS = (
    "Every source reference must exactly equal an identifier supplied by the "
    "application in the bounded Input Manifest. Do not create, abbreviate, "
    "rename, normalize, infer, or alias source identifiers. Do not use filenames, "
    "titles, ordinal labels, human descriptions, URLs, or array positions unless "
    "those exact values are the supplied canonical identifiers. When no supplied "
    "source supports a semantic statement, preserve uncertainty, mark the "
    "applicable field accordingly, or omit the unsupported assertion as allowed by "
    "the schema. Never invent a source reference to satisfy a required-looking "
    "structure."
)

BRIEF_SEMANTIC_AUTHORITY_INSTRUCTIONS = (
    "SEMANTIC AUTHORITY (STRICT):\n"
    "Requirements: use type=non_functional only when the bounded Input Manifest "
    "supports a non-functional statement. A non-functional requirement must set "
    "quality_attribute to one of the allowed values. Do not manufacture a quality "
    "attribute or synthetic quality requirement; if a quality choice is explicitly "
    "unresolved, preserve it as a sourced question or omit the unsupported claim.\n"
    "Operator decisions: emit an operator_decisions record only for an explicit "
    "operator instruction or decision. Every source_refs value on that record must "
    "resolve to a supplied source whose source_type is planning_request or "
    "clarification_message. Never infer operator authority from project rules, "
    "repository, runtime, or other source types.\n"
    "Questions and assumptions: do not silently resolve uncertainty present in the "
    "bounded input. A non_blocking question must reference an assumptions[index] "
    "record through temporary_assumption_id; blocking and operator_decision_required "
    "questions may leave that field null. Assumptions and questions must cite "
    "supplied sources and must not invent facts. If no unresolved ambiguity is "
    "present, omit the question and assumption records.\n"
    "Constraints: a must constraint cannot rely only on model_review. Use "
    "deterministic or test enforcement only when the supplied input defines a "
    "testable condition; use operator_review only when the supplied input explicitly "
    "requires operator judgment. Do not invent a numeric threshold or subjective "
    "acceptance condition. If the condition is unsupported or not testable, preserve "
    "uncertainty as a sourced question or omit the constraint."
)

_FIELD_CONDITIONAL_SEMANTICS = {
    ("Requirement", "quality_attribute"): (
        "required when type is non_functional; otherwise omit or set null; "
        "the value must be one of the listed quality attributes and must be "
        "supported by bounded input"
    ),
    ("Constraint", "enforcement"): (
        "when severity is must, model_review is not acceptable as the only "
        "enforcement; do not invent thresholds or acceptance conditions"
    ),
    ("UnresolvedQuestion", "temporary_assumption_id"): (
        "required for classification=non_blocking and must reference "
        "assumptions[index]; nullable for blocking or operator_decision_required "
        "questions"
    ),
}

_RECORD_AUTHORITY_SEMANTICS = {
    ("OperatorDecision", "source_authority"): (
        "source_refs must resolve only to supplied planning_request or "
        "clarification_message sources; emit only for explicit operator authority"
    )
}


def _nullable(annotation: Any) -> tuple[Any, bool]:
    args = get_args(annotation)
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType) and type(None) in args:
        non_null = tuple(item for item in args if item is not type(None))
        return (non_null[0] if len(non_null) == 1 else Any), True
    return annotation, False


def _json_type(annotation: Any) -> dict[str, Any]:
    annotation, nullable = _nullable(annotation)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if annotation is Any:
        result: dict[str, Any] = {"type": "any"}
    elif annotation is str:
        result = {"type": "string"}
    elif annotation is int:
        result = {"type": "integer"}
    elif annotation is float:
        result = {"type": "number"}
    elif annotation is bool:
        result = {"type": "boolean"}
    elif annotation is type(None):
        result = {"type": "null"}
    elif origin in (list, tuple, Sequence):
        item_type = args[0] if args else Any
        result = {"type": "array", "items": _json_type(item_type)}
    elif origin in (dict, Mapping):
        result = {
            "type": "object",
            "additional_properties": _json_type(args[1] if len(args) > 1 else Any),
        }
    elif isinstance(annotation, type) and is_dataclass(annotation):
        result = {"type": "object", "record_type": annotation.__name__}
    else:
        result = {"type": getattr(annotation, "__name__", str(annotation))}
    if nullable:
        result["nullable"] = True
    return result


def _record_contract(
    record_type: type[Any], enums: Mapping[tuple[str, str], Sequence[str]]
) -> dict[str, Any]:
    hints = get_type_hints(record_type)
    result_fields: dict[str, Any] = {}
    required: list[str] = []
    optional: list[str] = []
    for item in fields(record_type):
        if item.name == "id":
            continue
        descriptor = _json_type(hints.get(item.name, item.type))
        allowed = enums.get((record_type.__name__, item.name))
        if allowed:
            descriptor["allowed_values"] = sorted(str(value) for value in allowed)
        reference_semantics = _REFERENCE_FIELD_SEMANTICS.get(item.name)
        if reference_semantics is not None:
            descriptor["reference_semantics"] = reference_semantics
        conditional_semantics = _FIELD_CONDITIONAL_SEMANTICS.get(
            (record_type.__name__, item.name)
        )
        if conditional_semantics is not None:
            descriptor["conditional_semantics"] = conditional_semantics
        if descriptor.get("nullable"):
            optional.append(item.name)
        else:
            required.append(item.name)
        result_fields[item.name] = descriptor
    result = {
        "record_type": record_type.__name__,
        "required_fields": required,
        "optional_fields": optional,
        "fields": result_fields,
        "forbidden_fields": ["id"],
    }
    authority_semantics = _RECORD_AUTHORITY_SEMANTICS.get(
        (record_type.__name__, "source_authority")
    )
    if authority_semantics is not None:
        result["source_authority"] = authority_semantics
    return result


def _collection_contract(
    record_types: Mapping[str, type[Any]],
    enums: Mapping[tuple[str, str], Sequence[str]],
) -> dict[str, Any]:
    return {
        name: {
            "type": "array",
            "items": {"type": "object", "record_type": record_type.__name__},
            "record": _record_contract(record_type, enums),
        }
        for name, record_type in record_types.items()
    }


def build_planning_brief_schema_contract() -> dict[str, Any]:
    """Return the complete semantic-only Brief candidate contract."""

    objective = _record_contract(Goal, _BRIEF_ENUMS)
    return {
        "contract": "protocol-v2-planning-brief-candidate",
        "json_shape": "exactly_one_object",
        "top_level": {
            "required_fields": list(PLANNING_BRIEF_CANDIDATE_FIELDS),
            "optional_fields": [],
            "fields": {
                "objective": {
                    "type": "object",
                    "record": objective,
                },
                **_collection_contract(
                    PLANNING_BRIEF_CANDIDATE_RECORD_TYPES, _BRIEF_ENUMS
                ),
            },
        },
        "source_reference_requirements": {
            "source_refs": "array[string]; every value must be a source_id present in the supplied Input Manifest",
            "manifest_authority": BRIEF_SOURCE_REFERENCE_INSTRUCTIONS,
            "semantic_record_refs": "applies_to_refs, source_requirement_ids, requirement_ids, constraint_ids, acceptance_criterion_ids, and temporary_assumption_id use objective or collection[index] references only; they are not manifest source identifiers",
            "record_references": "Use objective or collection[index] only; the application resolves and assigns canonical IDs",
        },
        "semantic_authority": {
            "requirements": {
                "functional_vs_non_functional": "Use non_functional only for supported non-functional input; non_functional requires quality_attribute",
                "unsupported_quality": "Do not manufacture quality attributes; preserve explicitly unresolved quality choices as sourced questions or omit unsupported claims",
            },
            "operator_decisions": {
                "authority": "Only explicit operator instruction or decision may create an operator_decisions record",
                "source_types": ["planning_request", "clarification_message"],
            },
            "questions_and_assumptions": {
                "uncertainty": "Do not silently resolve bounded-input uncertainty",
                "non_blocking_link": "non_blocking questions require temporary_assumption_id referencing assumptions[index]",
                "blocking_link": "blocking and operator_decision_required questions may omit temporary_assumption_id",
            },
            "constraints": {
                "must": "must constraints cannot rely only on model_review",
                "evidence": "Do not invent thresholds or subjective acceptance conditions; preserve unsupported conditions as sourced uncertainty or omit",
            },
        },
        "application_owned_fields": [
            "all record id fields",
            "schema_version",
            "input_manifest_ref",
            "content_hash",
            "source_references",
            "checkpoint metadata",
            "lifecycle and review metadata",
        ],
        "forbidden_legacy_fields": [
            "title",
            "description",
            "objectives",
            "deliverables",
            "timeline",
            "brief_type",
            "source_refs at top level",
            "generic planning-document keys",
        ],
        "ordering": "record array order is non-authoritative; preserve source references and semantic relationships",
        "uncertainty": "preserve unresolved questions and assumptions; do not invent facts",
    }


def build_structured_task_plan_schema_contract() -> dict[str, Any]:
    """Return the complete semantic-only Structured Task Plan contract."""

    fields_contract = {
        name: {
            "type": "array",
            "items": {"type": "object", "record_type": record_type.__name__},
            "record": _record_contract(record_type, _TASK_PLAN_ENUMS),
        }
        for name, record_type in (
            ("tasks", Task),
            ("dependencies", Dependency),
            ("execution_groups", ExecutionGroup),
            ("intentional_omissions", IntentionalOmission),
        )
    }
    return {
        "contract": "protocol-v2-structured-task-plan-candidate",
        "json_shape": "exactly_one_object",
        "top_level": {
            "required_fields": list(STRUCTURED_TASK_PLAN_CANDIDATE_FIELDS),
            "optional_fields": [],
            "fields": fields_contract,
        },
        "additional_record_types": {
            record_type.__name__: _record_contract(record_type, _TASK_PLAN_ENUMS)
            for record_type in STRUCTURED_TASK_PLAN_CANDIDATE_RECORD_TYPES.values()
        },
        "reference_semantics": {
            "traceability": "target_id must be an ID from the accepted Brief; target_kind and role must use the listed enums",
            "dependency_task_refs": "prerequisite_task_id and dependent_task_id must use tasks[index] or #index, never TASK-NNN",
            "execution_group_task_refs": "task_ids must use tasks[index] or #index; the application resolves them",
            "omissions": "target_id must be an accepted Brief requirement or acceptance criterion and the reason must be explicit",
            "manifest_sources": "Structured Task Plan candidates have no source_refs field; traceability and omission targets use accepted Brief IDs, never manifest source identifiers",
        },
        "semantic_rules": {
            "coverage": {
                "required_target_kinds": [
                    "goal",
                    "requirement",
                    "acceptance_criterion",
                    "constraint",
                ],
                "accounting": (
                    "Account for every accepted Brief goal, requirement, acceptance "
                    "criterion, and constraint through one or more valid traceability "
                    "records or a permitted intentional omission. Silence is not "
                    "coverage; narrative mention and similar task-title words are "
                    "not coverage."
                ),
                "traceability_roles": {
                    "goal": ["implements", "enables", "verifies"],
                    "requirement": ["implements", "enables"],
                    "acceptance_criterion": ["implements", "verifies"],
                    "constraint": ["constrained_by"],
                },
                "omission_target_kinds": [
                    "requirement",
                    "acceptance_criterion",
                ],
                "omission_restriction": (
                    "An omission target must be an accepted Brief requirement or "
                    "acceptance criterion, use an allowed reason, and name an "
                    "accepted Brief scope or operator-decision ID. Required "
                    "requirements and acceptance criteria, goals, and constraints "
                    "cannot be omitted. Do not invent an omission target."
                ),
            },
            "execution_groups": {
                "optional": True,
                "membership": (
                    "Execution groups are optional; when present, each listed task "
                    "reference must resolve and a task cannot belong to multiple "
                    "groups."
                ),
                "sequential_member_rule": (
                    "A sequential group's member order is graph semantics. Adjacent "
                    "members require an ordering or hard_completion edge; do not "
                    "hide a prerequisite in narrative text."
                ),
                "parallel_member_rule": (
                    "Parallel members may not depend on one another and may not "
                    "conflict on a writable target unless the work is read-only."
                ),
                "cross_group_dependencies": (
                    "Cross-group dependency references use the same provider-facing "
                    "task indexes as all dependencies; group membership does not "
                    "make a dependency reference visible or valid by itself."
                ),
                "ordering": (
                    "Dependency edges are authoritative for graph ordering; candidate "
                    "emission order and group order are not substitutes for edges."
                ),
            },
        },
        "application_owned_fields": [
            "all Task, Dependency, and ExecutionGroup id fields",
            "schema_version",
            "brief_ref",
            "input_manifest_ref",
            "topology",
            "content_hash",
            "checkpoint, lifecycle, acceptance, and review metadata",
        ],
        "forbidden_fields": [
            "TASK-NNN",
            "DEP-NNN",
            "GROUP-NNN",
            "topology",
            "hashes",
            "legacy title/description/objectives/deliverables/timeline keys",
            "new Brief-intent records",
        ],
        "graph_constraints": {
            "dependencies": "represent every semantic prerequisite; no self-edge, duplicate edge, or cycle",
            "execution_groups": "represent sequential and parallel semantics; do not encode parallel conflicts as parallel",
            "coverage": "cover every required Brief goal, requirement, acceptance criterion, and must constraint, or emit an authorized omission",
            "ordering": "candidate emission order is non-authoritative; dependency and group references are semantic",
        },
    }


def render_schema_contract(contract: Mapping[str, Any]) -> str:
    """Render a deterministic, complete schema block for a provider prompt."""

    return json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2)


__all__ = [
    "BRIEF_SEMANTIC_AUTHORITY_INSTRUCTIONS",
    "BRIEF_SOURCE_REFERENCE_INSTRUCTIONS",
    "PLANNING_BRIEF_CANDIDATE_FIELDS",
    "PLANNING_BRIEF_CANDIDATE_RECORD_TYPES",
    "STRUCTURED_TASK_PLAN_CANDIDATE_FIELDS",
    "STRUCTURED_TASK_PLAN_CANDIDATE_RECORD_TYPES",
    "build_planning_brief_schema_contract",
    "build_structured_task_plan_schema_contract",
    "render_schema_contract",
]
