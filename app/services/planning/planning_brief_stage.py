"""Protocol v2 Planning Brief generation stage.

This module is the provider boundary for the first Protocol v2 content stage.
Providers return semantic records only.  The manifest, IDs, references to
canonical records, hashes, validation, and checkpoint metadata remain owned by
the application.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
import hashlib
import json
import logging
import re
import time
from typing import Any

from app.services.orchestration.stage_engine import (
    StageAcceptance,
    StageContext,
    StageDefinition,
    StageExecutionPolicy,
    StageValidation,
)
from app.services.planning.input_manifest import InputManifest, ManifestSource
from app.services.planning.planning_brief import (
    AcceptanceCriterion,
    ArchitectureContext,
    Assumption,
    BackgroundFact,
    Constraint,
    Goal,
    ImplementationStrategy,
    InterfaceContract,
    OperatorDecision,
    PlanningBrief,
    PlanningBriefSchemaError,
    Requirement,
    Risk,
    ScopeItem,
    SourceReference,
    UnresolvedQuestion,
    ValidationStrategy,
    canonical_json_bytes,
    validate_planning_brief,
)
from app.services.planning.provider_contract import (
    BRIEF_SEMANTIC_AUTHORITY_INSTRUCTIONS,
    BRIEF_SOURCE_REFERENCE_INSTRUCTIONS,
    PLANNING_BRIEF_CANDIDATE_FIELDS,
    PLANNING_BRIEF_CANDIDATE_RECORD_TYPES,
    build_planning_brief_schema_contract,
    render_schema_contract,
)
from app.services.planning.providers import (
    PlanningArtifactKind,
    PlanningProvider,
    PlanningProviderExecutionError,
    PlanningRequest,
    PlanningResponse,
    PlanningRuntimeOptions,
    PROVIDER_RUNTIME_FAILURES,
    ProviderFailureOrigin,
    ReasoningControls,
    SamplingControls,
)


DEFAULT_SOURCE_CHAR_LIMIT = 20_000
DEFAULT_TOTAL_SOURCE_CHAR_LIMIT = 100_000
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 360
DEFAULT_PROVIDER_FIRST_OUTPUT_TIMEOUT_SECONDS = 320
_CANONICAL_RECORD_REF = re.compile(r"^[A-Z]+-[0-9]{3}$")
_PROVIDER_RUNTIME_FAILURES = PROVIDER_RUNTIME_FAILURES
logger = logging.getLogger(__name__)


class PlanningBriefStageError(RuntimeError):
    """A deterministic, persisted failure classification for Brief generation."""

    classification = "application_error"

    def __init__(self, message: str):
        self.detail = str(message or self.classification)[:500]
        super().__init__(f"{self.classification}: {self.detail}")


class PlanningBriefTransportError(PlanningBriefStageError):
    classification = "transport_failure"


class PlanningBriefProviderOutputError(PlanningBriefStageError):
    classification = "provider_output_failure"


class PlanningBriefValidationError(PlanningBriefStageError):
    classification = "validation_failure"


class PlanningBriefApplicationError(PlanningBriefStageError):
    classification = "application_error"


class PlanningBriefProviderRuntimeError(PlanningBriefStageError):
    """A provider-boundary failure with a stable non-semantic class."""

    def __init__(self, classification: str, message: str):
        self.classification = (
            classification
            if classification in _PROVIDER_RUNTIME_FAILURES
            else "transport_failure"
        )
        super().__init__(message)


@dataclass(frozen=True)
class PlanningBriefProviderInput:
    """Bounded provider input assembled solely from the persisted manifest."""

    manifest_id: str
    manifest_hash: str
    manifest_schema_version: str
    sources: tuple[Mapping[str, Any], ...]
    stage_configuration: Mapping[str, Any]
    schema_instructions: Mapping[str, Any] = field(
        default_factory=build_planning_brief_schema_contract
    )
    # Routing metadata is runtime-only and is deliberately not serialized.
    project_id: int | None = None

    def __post_init__(self) -> None:
        if not self.schema_instructions:
            object.__setattr__(
                self, "schema_instructions", build_planning_brief_schema_contract()
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_manifest": {
                "id": self.manifest_id,
                "hash": self.manifest_hash,
                "schema_version": self.manifest_schema_version,
            },
            "sources": [dict(source) for source in self.sources],
            "schema_instructions": dict(self.schema_instructions),
            "stage_configuration": dict(self.stage_configuration),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


@dataclass(frozen=True)
class PlanningBriefCandidate:
    """Provider-owned semantic records before IDs or manifest references."""

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


_RECORD_TYPES = PLANNING_BRIEF_CANDIDATE_RECORD_TYPES
_SEQUENCE_FIELDS = {
    "source_refs",
    "applies_to_refs",
    "source_requirement_ids",
    "requirement_ids",
    "constraint_ids",
    "acceptance_criterion_ids",
    "allowed_resolver_roles",
}


def _parse_json_candidate(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise PlanningBriefProviderOutputError(
                "candidate is not valid JSON"
            ) from exc
    else:
        parsed = raw
    if not isinstance(parsed, Mapping):
        raise PlanningBriefProviderOutputError("candidate must be a JSON object")
    return parsed


def _parse_candidate_record(raw: Any, record_type: type[Any], path: str) -> Any:
    if not isinstance(raw, Mapping):
        raise PlanningBriefProviderOutputError(f"{path} must be an object")
    if "id" in raw:
        raise PlanningBriefProviderOutputError(f"{path}.id is application-owned")
    allowed = {field.name for field in fields(record_type)} - {"id"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise PlanningBriefProviderOutputError(
            f"{path} contains unknown fields: {', '.join(unknown)}"
        )
    for field_name in _SEQUENCE_FIELDS.intersection(raw):
        if not isinstance(raw[field_name], list):
            raise PlanningBriefProviderOutputError(
                f"{path}.{field_name} must be an array"
            )
    try:
        return record_type(**dict(raw))
    except (PlanningBriefSchemaError, TypeError, ValueError) as exc:
        raise PlanningBriefProviderOutputError(f"malformed {path}") from exc


def parse_planning_brief_candidate(raw: Any) -> PlanningBriefCandidate:
    """Parse the strict semantic-only provider contract without persistence."""

    parsed = _parse_json_candidate(raw)
    expected = set(PLANNING_BRIEF_CANDIDATE_FIELDS)
    unknown = sorted(set(parsed) - expected)
    missing = sorted(expected - set(parsed))
    if unknown:
        raise PlanningBriefProviderOutputError(
            f"candidate contains unknown fields: {', '.join(unknown)}"
        )
    if missing:
        raise PlanningBriefProviderOutputError(
            f"candidate is missing fields: {', '.join(missing)}"
        )
    objective = _parse_candidate_record(parsed["objective"], Goal, "objective")
    values: dict[str, tuple[Any, ...]] = {}
    for collection_name, record_type in _RECORD_TYPES.items():
        raw_values = parsed[collection_name]
        if not isinstance(raw_values, list):
            raise PlanningBriefProviderOutputError(
                f"{collection_name} must be an array"
            )
        values[collection_name] = tuple(
            _parse_candidate_record(item, record_type, f"{collection_name}[{index}]")
            for index, item in enumerate(raw_values)
        )
    return PlanningBriefCandidate(objective=objective, **values)


def _ordered_records(
    records: Sequence[Any], manifest: InputManifest
) -> tuple[tuple[int, Any], ...]:
    ordinals = {source.source_id: source.ordinal for source in manifest.sources}

    def sort_key(item: tuple[int, Any]) -> tuple[int, int]:
        index, record = item
        refs = tuple(getattr(record, "source_refs", ()))
        return (min((ordinals.get(ref, 10**9) for ref in refs), default=10**9), index)

    return tuple(sorted(enumerate(records), key=sort_key))


def _record_id(prefix: str, ordinal: int) -> str:
    return f"{prefix}-{ordinal + 1:03d}"


def _resolve_record_ref(
    value: str,
    *,
    collection: str,
    original_ids: Mapping[tuple[str, int], str],
    final_ids: Mapping[tuple[str, int], str],
) -> str:
    if value == "objective":
        return "GOAL-001"
    if _CANONICAL_RECORD_REF.fullmatch(value):
        raise PlanningBriefProviderOutputError(
            f"{collection} reference {value!r} is an application-owned ID"
        )
    match = re.fullmatch(r"([a-z_]+)\[(\d+)\]", value)
    if match is None:
        raise PlanningBriefProviderOutputError(
            f"{collection} reference {value!r} is not a semantic record reference"
        )
    target_collection, raw_index = match.groups()
    index = int(raw_index)
    original_key = (target_collection, index)
    if original_key not in original_ids:
        raise PlanningBriefProviderOutputError(
            f"{collection} reference {value!r} does not resolve"
        )
    return final_ids[original_key]


def canonicalize_planning_brief_candidate(
    candidate: PlanningBriefCandidate, manifest: InputManifest
) -> PlanningBrief:
    """Assign IDs, resolve semantic record references, order, and bind sources."""

    manifest.validate()
    source_ids = {source.source_id for source in manifest.sources}
    for collection_name, records in (
        ("objective", (candidate.objective,)),
        *(
            (collection_name, getattr(candidate, collection_name))
            for collection_name in _RECORD_TYPES
        ),
    ):
        for record in records:
            for source_ref in getattr(record, "source_refs", ()):
                if source_ref not in source_ids:
                    raise PlanningBriefProviderOutputError(
                        f"{collection_name} references unknown manifest source {source_ref!r}"
                    )

    ordered: dict[str, tuple[tuple[int, Any], ...]] = {
        collection_name: _ordered_records(getattr(candidate, collection_name), manifest)
        for collection_name in _RECORD_TYPES
    }
    original_ids: dict[tuple[str, int], str] = {}
    final_ids: dict[tuple[str, int], str] = {}
    prefixes = {
        collection_name: getattr(record_type, "prefix")
        for collection_name, record_type in _RECORD_TYPES.items()
    }
    for collection_name, items in ordered.items():
        for final_ordinal, (original_index, _record) in enumerate(items):
            original_ids[(collection_name, original_index)] = _record_id(
                prefixes[collection_name], original_index
            )
            final_ids[(collection_name, original_index)] = _record_id(
                prefixes[collection_name], final_ordinal
            )

    def rewrite(record: Any, collection_name: str, original_index: int) -> Any:
        updates: dict[str, Any] = {}
        for field_name in (
            "applies_to_refs",
            "source_requirement_ids",
            "requirement_ids",
            "constraint_ids",
            "acceptance_criterion_ids",
        ):
            if hasattr(record, field_name):
                updates[field_name] = tuple(
                    _resolve_record_ref(
                        value,
                        collection=collection_name,
                        original_ids=original_ids,
                        final_ids=final_ids,
                    )
                    for value in getattr(record, field_name)
                )
        if hasattr(record, "temporary_assumption_id"):
            value = record.temporary_assumption_id
            updates["temporary_assumption_id"] = (
                None
                if value is None
                else _resolve_record_ref(
                    value,
                    collection=collection_name,
                    original_ids=original_ids,
                    final_ids=final_ids,
                )
            )
        from dataclasses import replace

        return replace(record, **updates) if updates else record

    rewritten: dict[str, tuple[Any, ...]] = {
        collection_name: tuple(
            rewrite(record, collection_name, original_index)
            for original_index, record in items
        )
        for collection_name, items in ordered.items()
    }
    referenced_sources = {
        source_ref
        for record in (candidate.objective,)
        for source_ref in getattr(record, "source_refs", ())
    }
    referenced_sources.update(
        source_ref
        for records in rewritten.values()
        for record in records
        for source_ref in getattr(record, "source_refs", ())
    )
    manifest_by_id = {source.source_id: source for source in manifest.sources}
    source_references = tuple(
        SourceReference(
            source_id=source_id,
            source_type=manifest_by_id[source_id].source_type,
            content_hash=manifest_by_id[source_id].content_hash,
            label=manifest_by_id[source_id].source_type,
        )
        for source_id in sorted(referenced_sources)
    )
    return PlanningBrief.create(
        input_manifest=manifest,
        objective=candidate.objective,
        **rewritten,
        source_references=source_references,
    )


def build_planning_brief_provider_input(
    context: StageContext,
) -> PlanningBriefProviderInput:
    """Build bounded input from the persisted manifest and stage config only."""

    configuration = dict(context.configuration)
    source_limit = int(configuration.get("max_source_chars", DEFAULT_SOURCE_CHAR_LIMIT))
    total_limit = int(
        configuration.get("max_total_source_chars", DEFAULT_TOTAL_SOURCE_CHAR_LIMIT)
    )
    if source_limit < 1 or total_limit < 1:
        raise PlanningBriefApplicationError("source bounds must be positive")
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
            raise PlanningBriefApplicationError(
                f"source {source.source_id} exceeds bounded provider input"
            )
        total_chars += len(material)
        if total_chars > total_limit:
            raise PlanningBriefApplicationError(
                "manifest source material exceeds bound"
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
    request = PlanningBriefProviderInput(
        manifest_id=context.input_manifest.manifest_id,
        manifest_hash=context.input_manifest.manifest_hash,
        manifest_schema_version=context.input_manifest.schema_version,
        sources=tuple(sources),
        stage_configuration=configuration,
        schema_instructions=build_planning_brief_schema_contract(),
        project_id=getattr(context.session, "project_id", None),
    )
    if len(request.canonical_bytes()) > total_limit + 16_384:
        raise PlanningBriefApplicationError(
            "provider input exceeds deterministic bound"
        )
    return request


def build_planning_brief_request(
    provider_input: PlanningBriefProviderInput,
) -> PlanningRequest:
    """Render the unchanged Brief prompt into the provider-neutral request."""

    candidate_fields = ", ".join(PLANNING_BRIEF_CANDIDATE_FIELDS)
    schema = render_schema_contract(
        provider_input.schema_instructions or build_planning_brief_schema_contract()
    )
    prompt = (
        "Generate one Protocol v2 Planning Brief semantic candidate from INPUT.\n"
        "Return exactly one JSON object. Return no Markdown fences, explanation, "
        "commentary, or reasoning.\n"
        f"The only allowed top-level fields are: {candidate_fields}.\n"
        "Use only the specified nested record fields. Do not invent aliases or "
        "generic planning-document keys. Do not emit title, description, objectives, "
        "deliverables, timeline, brief_type, top-level source_refs, or any other "
        "unsupported legacy fields. Do not emit canonical IDs: every id field, "
        "manifest/checkpoint/hash/schema/lifecycle field is application-owned and "
        "forbidden in provider output. Preserve semantic uncertainty rather than "
        "inventing facts; use unresolved questions and assumptions when appropriate.\n"
        "SOURCE-REFERENCE AUTHORITY (STRICT):\n"
        + BRIEF_SOURCE_REFERENCE_INSTRUCTIONS
        + "\nManifest source_refs and semantic record-reference fields are distinct: "
        "source_refs use only supplied canonical source_id values; applies_to_refs "
        "and record relationship fields use objective or collection[index] references.\n\n"
        + BRIEF_SEMANTIC_AUTHORITY_INSTRUCTIONS
        + "\n\n"
        "COMPLETE RECORD-LEVEL SCHEMA CONTRACT:\n"
        + schema
        + "\n\nINPUT:\n"
        + provider_input.canonical_bytes().decode("utf-8")
    )
    timeout_seconds = int(
        provider_input.stage_configuration.get(
            "provider_timeout_seconds", DEFAULT_PROVIDER_TIMEOUT_SECONDS
        )
    )
    return PlanningRequest(
        artifact_kind=PlanningArtifactKind.PLANNING_BRIEF,
        prompt=prompt,
        protocol_input=provider_input.to_dict(),
        runtime_options=PlanningRuntimeOptions(timeout_seconds=timeout_seconds),
        reasoning=ReasoningControls(enabled=False),
        sampling=SamplingControls(temperature=0),
        project_id=provider_input.project_id,
        metadata={
            "manifest_id": provider_input.manifest_id,
            "manifest_hash": provider_input.manifest_hash,
        },
    )


class PlanningBriefStage(StageDefinition):
    """Registered Protocol v2 stage from Input Manifest to accepted Brief."""

    def __init__(self, provider: PlanningProvider):
        self.provider = provider
        super().__init__(
            "planning_brief",
            version=1,
            prerequisites=(),
            execution_policy=StageExecutionPolicy(retryable=True, max_attempts=1),
        )

    def execute(self, context: StageContext) -> PlanningBrief:
        stage_started_at = time.monotonic()
        try:
            request_started_at = time.monotonic()
            provider_input = build_planning_brief_provider_input(context)
            request = build_planning_brief_request(provider_input)
            request_construction_seconds = round(
                time.monotonic() - request_started_at, 3
            )
        except PlanningBriefStageError:
            raise
        except Exception as exc:
            raise PlanningBriefApplicationError(
                "provider input construction failed"
            ) from exc
        try:
            response = self.provider.generate(request)
        except PlanningProviderExecutionError as exc:
            if exc.classification in _PROVIDER_RUNTIME_FAILURES:
                raise PlanningBriefProviderRuntimeError(
                    exc.classification, exc.detail
                ) from exc
            message = (
                "provider invocation failed"
                if exc.origin is ProviderFailureOrigin.INVOCATION
                else "provider returned a failed result"
            )
            raise PlanningBriefTransportError(message) from exc
        except PlanningBriefStageError:
            raise
        except Exception as exc:
            raise PlanningBriefTransportError("provider invocation failed") from exc
        if not isinstance(response, PlanningResponse):
            raise PlanningBriefTransportError("provider returned a failed result")
        raw = response.candidate_text
        if not isinstance(raw, (str, Mapping)):
            raise PlanningBriefProviderOutputError(
                "provider returned no candidate output"
            )
        parser_seconds = 0.0
        canonicalization_seconds = 0.0
        try:
            parser_started_at = time.monotonic()
            candidate = parse_planning_brief_candidate(raw)
            parser_seconds = round(time.monotonic() - parser_started_at, 3)
            canonicalization_started_at = time.monotonic()
            output = canonicalize_planning_brief_candidate(
                candidate, context.input_manifest
            )
            canonicalization_seconds = round(
                time.monotonic() - canonicalization_started_at, 3
            )
            _log_brief_timing(
                request=request,
                provider_input=provider_input,
                response=response,
                request_construction_seconds=request_construction_seconds,
                parser_seconds=parser_seconds,
                canonicalization_seconds=canonicalization_seconds,
                total_seconds=round(time.monotonic() - stage_started_at, 3),
                failure_classification=None,
            )
            return output
        except PlanningBriefStageError as exc:
            _log_brief_timing(
                request=request,
                provider_input=provider_input,
                response=response,
                request_construction_seconds=request_construction_seconds,
                parser_seconds=parser_seconds
                or round(time.monotonic() - parser_started_at, 3),
                canonicalization_seconds=canonicalization_seconds,
                total_seconds=round(time.monotonic() - stage_started_at, 3),
                failure_classification=exc.classification,
            )
            raise
        except Exception as exc:
            _log_brief_timing(
                request=request,
                provider_input=provider_input,
                response=response,
                request_construction_seconds=request_construction_seconds,
                parser_seconds=parser_seconds
                or round(time.monotonic() - parser_started_at, 3),
                canonicalization_seconds=canonicalization_seconds,
                total_seconds=round(time.monotonic() - stage_started_at, 3),
                failure_classification="provider_output_failure",
            )
            raise PlanningBriefProviderOutputError(
                "candidate canonicalization failed"
            ) from exc

    def validate(self, output: Any, context: StageContext) -> StageValidation:
        validation_started_at = time.monotonic()
        if not isinstance(output, PlanningBrief):
            result = StageValidation(
                False, "provider_output_failure: output is not a Brief"
            )
            logger.info(
                "[PHASE28RV_TIMING] stage=planning_brief validation_seconds=%.3f "
                "validation_result=invalid_output",
                time.monotonic() - validation_started_at,
            )
            return result
        acceptance = validate_planning_brief(
            output, input_manifest=context.input_manifest
        )
        if not acceptance.semantically_valid:
            result = StageValidation(False, _validation_reason(acceptance))
        else:
            result = StageValidation(True)
        logger.info(
            "[PHASE28RV_TIMING] stage=planning_brief validation_seconds=%.3f "
            "validation_result=%s",
            time.monotonic() - validation_started_at,
            "accepted" if result.valid else "rejected",
        )
        return result

    def accept(self, output: Any, context: StageContext) -> StageAcceptance:
        if not isinstance(output, PlanningBrief):
            return StageAcceptance(
                False, "provider_output_failure: output is not a Brief"
            )
        acceptance = validate_planning_brief(
            output, input_manifest=context.input_manifest
        )
        if not acceptance.protocol_acceptable:
            return StageAcceptance(False, _validation_reason(acceptance))
        return StageAcceptance(True)


def _validation_reason(acceptance: Any) -> str:
    issues = sorted(
        (f"{issue.code}:{issue.path}" for issue in acceptance.errors),
    )
    detail = ",".join(issues[:8]) or "protocol acceptance failed"
    return f"validation_failure: {detail}"


def _log_brief_timing(
    *,
    request: PlanningRequest,
    provider_input: PlanningBriefProviderInput,
    response: PlanningResponse,
    request_construction_seconds: float,
    parser_seconds: float,
    canonicalization_seconds: float,
    total_seconds: float,
    failure_classification: str | None,
) -> None:
    metadata = {
        "artifact_kind": request.artifact_kind.value,
        "manifest_id": request.metadata.get("manifest_id"),
        "manifest_hash": request.metadata.get("manifest_hash"),
        "project_id": request.project_id,
    }
    metadata_hash = hashlib.sha256(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    logger.info(
        "[PHASE28RV_TIMING] stage=planning_brief provider=%s model=%s "
        "prompt_length=%s manifest_source_count=%s metadata_hash=%s "
        "provider_latency_seconds=%s request_construction_seconds=%s "
        "strict_parser_seconds=%s canonicalization_seconds=%s "
        "total_stage_seconds=%s failure_classification=%s provider_timings=%s",
        response.provider_name,
        response.runtime_metadata.model,
        len(request.prompt),
        len(provider_input.sources),
        metadata_hash,
        response.latency_seconds,
        request_construction_seconds,
        parser_seconds,
        canonicalization_seconds,
        total_seconds,
        failure_classification or "none",
        response.diagnostics.details.get("timings_seconds", {}),
    )


def build_protocol_v2_stage_definitions(
    db: Any,
    *,
    planning_provider: PlanningProvider | None = None,
) -> tuple[StageDefinition, ...]:
    """Return the deterministic default v2 registry."""

    # Kept as a compatibility import path for Phase 28G callers.  The lazy
    # import avoids a module cycle because the Task Plan stage reuses this
    # module's Brief provider contract and stage implementation.
    from app.services.planning.structured_task_plan_stage import (
        build_protocol_v2_stage_definitions as build_v2_definitions,
    )

    return build_v2_definitions(
        db,
        planning_provider=planning_provider,
    )


__all__ = [
    "DEFAULT_SOURCE_CHAR_LIMIT",
    "DEFAULT_TOTAL_SOURCE_CHAR_LIMIT",
    "DEFAULT_PROVIDER_FIRST_OUTPUT_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER_TIMEOUT_SECONDS",
    "PLANNING_BRIEF_CANDIDATE_FIELDS",
    "PlanningBriefApplicationError",
    "PlanningBriefCandidate",
    "PlanningBriefProviderInput",
    "PlanningBriefProviderOutputError",
    "PlanningBriefProviderRuntimeError",
    "PlanningBriefStage",
    "PlanningBriefStageError",
    "PlanningBriefTransportError",
    "PlanningBriefValidationError",
    "build_planning_brief_request",
    "build_planning_brief_provider_input",
    "build_protocol_v2_stage_definitions",
    "canonicalize_planning_brief_candidate",
    "parse_planning_brief_candidate",
]
