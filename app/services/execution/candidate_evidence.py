"""Phase 29C-7B read-only evidence and deterministic predicate primitives.

This module is intentionally narrower than validation-run orchestration.  It
can read one already-persisted runtime outcome, persist an immutable metadata
snapshot, and evaluate one versioned predicate against that snapshot.  It
never opens a path, calls a network client, starts a subprocess/provider, or
changes lifecycle authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Callable, Protocol
import unicodedata

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAttempt,
    ExecutionTaskAttemptOutcome,
    ExecutionTaskResolvedValidationEvidence,
    ExecutionTaskValidationPredicateResult as DBValidationPredicateResult,
    ExecutionTaskValidationSpecification,
)
from app.services.execution.execution_task_runtime_execution_service import (
    ExecutionTaskRuntimeExecutionService,
)
from app.services.execution.validation_contract import (
    ExecutionValidationContractError,
    ValidationContractService,
)
from app.services.planning.operator_review import canonical_json_hash
from app.services.planning.validation_contract import (
    PREDICATE_VERSIONS,
    StructuredValidationContract,
    ValidationContractError,
    ValidationEvidenceDescriptor,
    ValidationPredicate,
    VALIDATION_HASH_ALGORITHM,
    VALIDATION_RESOLVER_VERSION,
)


REFERENCE_GRAMMAR_VERSION = "candidate-evidence-reference/1"
RESOLVER_CONTRACT_VERSION = "candidate-evidence-resolver/1"
RESOLVER_ID = "sql-candidate-outcome"
PREDICATE_RESULT_SCHEMA_VERSION = "candidate-validation-predicate-result/1"
VALIDATOR_IMPLEMENTATION_VERSION = 1

MAX_REFERENCE_LENGTH = 255
MAX_OUTPUT_REFERENCE_LENGTH = 512
MAX_EVIDENCE_BYTES = 1_048_576
MAX_JSON_DEPTH = 8
MAX_JSON_ITEMS = 128
MAX_JSON_STRING_LENGTH = 4_096
MAX_METADATA_BYTES = 8_192
MAX_DIAGNOSTIC_BYTES = 2_048
MAX_DIAGNOSTIC_ITEMS = 32
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_COMMAND_ID_LENGTH = 128
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_RE = re.compile(r"^candidate-output://([1-9][0-9]{0,18})$")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]{0,31}):/{2}.*$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

EVIDENCE_RESOLUTION_STATUSES = frozenset(
    {
        "resolved",
        "missing",
        "hash_mismatch",
        "unsupported",
        "unavailable",
        "invalid_reference",
        "too_large",
        "invalid_content",
    }
)
PREDICATE_RESULT_STATUSES = frozenset(
    {
        "passed",
        "failed",
        "missing_evidence",
        "validator_error",
        "unsupported",
        "invalid_evidence",
    }
)


class CandidateEvidenceError(RuntimeError):
    """Bounded error at the resolver/validator primitive boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


CandidateEvidenceResolutionError = CandidateEvidenceError
ValidationPrimitiveError = CandidateEvidenceError


def _bounded_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise CandidateEvidenceError(
            "candidate_evidence_reference_invalid", f"{field} is invalid"
        )
    value = unicodedata.normalize("NFC", value)
    if not value or len(value) > limit or _CONTROL_RE.search(value):
        raise CandidateEvidenceError(
            "candidate_evidence_reference_invalid", f"{field} is invalid"
        )
    return value


def _optional_text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    value = _bounded_text(value, field, limit)
    return value or None


def _hash(value: Any, field: str) -> str:
    value = _bounded_text(value, field, 64).lower()
    if not _HASH_RE.fullmatch(value):
        raise CandidateEvidenceError(
            "candidate_evidence_reference_invalid", f"{field} is invalid"
        )
    return value


def _optional_hash(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _hash(value, field)


def _bounded_json(value: Any, *, field: str, depth: int = 0) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise CandidateEvidenceError(
            "candidate_evidence_too_large", f"{field} is too deeply nested"
        )
    if isinstance(value, Mapping):
        if len(value) > MAX_JSON_ITEMS:
            raise CandidateEvidenceError(
                "candidate_evidence_too_large", f"{field} has too many items"
            )
        result: dict[str, Any] = {}
        for key, item in value.items():
            key = _bounded_text(key, f"{field} key", MAX_JSON_STRING_LENGTH)
            result[key] = _bounded_json(item, field=field, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_JSON_ITEMS:
            raise CandidateEvidenceError(
                "candidate_evidence_too_large", f"{field} has too many items"
            )
        return [_bounded_json(item, field=field, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _bounded_text(value, field, MAX_JSON_STRING_LENGTH)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return value
    raise CandidateEvidenceError(
        "candidate_evidence_invalid_content", f"{field} is not bounded JSON"
    )


def _bounded_json_bytes(
    value: Any, *, field: str, limit: int
) -> dict[str, Any] | list[Any] | None:
    normalized = _bounded_json(value, field=field)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > limit:
        raise CandidateEvidenceError(
            "candidate_evidence_too_large", f"{field} is too large"
        )
    return normalized


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class EvidenceReference:
    grammar_version: str
    scheme: str
    identifier: int
    normalized: str


def parse_evidence_reference(value: str) -> EvidenceReference:
    """Parse the sole certified resolver reference grammar.

    ``candidate-output://N`` identifies an immutable runtime outcome row.  It
    is not a filesystem/object-store URL and is never dereferenced as one.
    """

    raw = _bounded_text(value, "reference", MAX_REFERENCE_LENGTH).strip()
    if raw != value:
        raw = unicodedata.normalize("NFC", raw)
    if "@" in raw or "?" in raw or "#" in raw or "\\" in raw:
        raise CandidateEvidenceError(
            "candidate_evidence_reference_invalid", "reference syntax is invalid"
        )
    match = _REFERENCE_RE.fullmatch(raw.lower())
    if match is not None:
        identifier = int(match.group(1))
        return EvidenceReference(
            grammar_version=REFERENCE_GRAMMAR_VERSION,
            scheme="candidate-output",
            identifier=identifier,
            normalized=f"candidate-output://{identifier}",
        )
    if _SCHEME_RE.match(raw):
        raise CandidateEvidenceError(
            "candidate_evidence_scheme_unsupported", "reference scheme is unsupported"
        )
    raise CandidateEvidenceError(
        "candidate_evidence_reference_invalid",
        "reference is not a supported absolute reference",
    )


def normalize_evidence_reference(value: str) -> str:
    return parse_evidence_reference(value).normalized


@dataclass(frozen=True)
class ResolveCandidateEvidenceCommand:
    execution_plan_id: int
    execution_task_id: int
    execution_task_attempt_id: int
    candidate_outcome_id: int
    validation_specification_id: int
    validation_specification_hash: str
    evidence_key: str
    evidence_type: str
    evidence_source: str
    expected_reference: str
    expected_output_reference: str | None = None
    expected_content_hash: str | None = None
    expected_hash_algorithm: str | None = None
    resolver_version: str = VALIDATION_RESOLVER_VERSION
    environment_configuration_hash: str = ""
    resolution_idempotency_key: str = ""
    deterministic_resolution_command_id: str = ""
    creation_actor_type: str = "validation_service"
    creation_actor_id: str = "system"


@dataclass(frozen=True)
class ResolvedCandidateEvidence:
    id: int
    execution_plan_id: int
    execution_task_id: int
    execution_task_attempt_id: int
    candidate_outcome_id: int
    validation_specification_id: int
    validation_specification_hash: str
    evidence_key: str
    evidence_type: str
    source: str
    normalized_reference: str
    source_authority_id: str
    resolver_id: str
    resolver_version: str
    resolver_contract_version: str
    environment_configuration_hash: str
    expected_hash_algorithm: str | None
    expected_hash: str | None
    actual_hash: str | None
    media_type: str | None
    byte_size: int | None
    structured_metadata_summary: Mapping[str, Any]
    content_projection: Mapping[str, Any] | None
    resolution_status: str
    canonical_evidence_payload_hash: str
    resolved_at: datetime


@dataclass(frozen=True)
class ResolvedEvidenceResult:
    evidence: ResolvedCandidateEvidence
    replayed: bool = False


@dataclass(frozen=True)
class CandidateOutcomeAuthority:
    id: int
    execution_plan_id: int
    execution_task_id: int
    execution_task_attempt_id: int
    outcome_status: str
    output_reference: str | None
    output_hash: str | None
    completed_at: datetime


class ImmutableCandidateEvidenceSource(Protocol):
    def read(self, candidate_outcome_id: int) -> CandidateOutcomeAuthority | None:
        """Read one authority row without opening or copying external content."""


class CandidateEvidenceResolver(Protocol):
    def resolve(
        self, command: ResolveCandidateEvidenceCommand
    ) -> ResolvedEvidenceResult: ...


class SqlCandidateOutcomeSource:
    """Read-only adapter for the existing canonical runtime outcome table."""

    def __init__(self, db: Session):
        self.db = db

    def read(self, candidate_outcome_id: int) -> CandidateOutcomeAuthority | None:
        outcome = self.db.get(ExecutionTaskAttemptOutcome, int(candidate_outcome_id))
        if outcome is None:
            return None
        if outcome.completed_at is None:
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "candidate outcome timestamp is invalid",
            )
        return CandidateOutcomeAuthority(
            id=outcome.id,
            execution_plan_id=outcome.execution_plan_id,
            execution_task_id=outcome.execution_task_id,
            execution_task_attempt_id=outcome.execution_task_attempt_id,
            outcome_status=outcome.outcome_status,
            output_reference=_optional_text(
                outcome.output_reference,
                "candidate output reference",
                MAX_OUTPUT_REFERENCE_LENGTH,
            ),
            output_hash=_optional_hash(outcome.output_hash, "candidate output hash"),
            completed_at=outcome.completed_at,
        )


@dataclass(frozen=True)
class ValidatorExecutionContext:
    validation_specification_id: int
    validation_specification_hash: str
    validator_set_id: str
    validator_set_version: str
    environment_configuration_hash: str
    resolver_version: str

    @property
    def environment_identity(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "validator_set_id": self.validator_set_id,
                "validator_set_version": self.validator_set_version,
                "configuration_hash": self.environment_configuration_hash,
                "resolver_version": self.resolver_version,
            }
        )


@dataclass(frozen=True)
class CandidatePredicateResult:
    result_status: str
    passed: bool
    result_code: str
    diagnostics: Mapping[str, Any]
    expected_summary: Mapping[str, Any] | None = None
    actual_summary: Mapping[str, Any] | None = None


class DeterministicCandidateValidator(Protocol):
    validator_id: str
    validator_version: int

    def validate(
        self,
        predicate: ValidationPredicate,
        evidence: ResolvedCandidateEvidence,
        context: ValidatorExecutionContext,
    ) -> CandidatePredicateResult: ...


@dataclass(frozen=True)
class ValidatorRegistration:
    predicate_id: str
    predicate_version: int
    validator_id: str
    validator_version: int
    validator: DeterministicCandidateValidator


class ValidatorRegistryError(CandidateEvidenceError):
    pass


class DeterministicValidatorRegistry:
    """Bounded, deterministic predicate-to-implementation registry."""

    def __init__(
        self,
        *,
        validator_set_id: str = "deterministic_readonly",
        validator_set_version: str = "1",
        configuration_hash: str | None = None,
    ):
        self.validator_set_id = _bounded_text(validator_set_id, "validator set", 128)
        self.validator_set_version = _bounded_text(
            validator_set_version, "validator set version", 64
        )
        self.configuration_hash = (
            None
            if configuration_hash is None
            else _hash(configuration_hash, "configuration hash")
        )
        self._registrations: dict[tuple[str, int], ValidatorRegistration] = {}

    def register(
        self,
        *,
        predicate_id: str,
        predicate_version: int,
        validator_id: str,
        validator_version: int,
        validator: DeterministicCandidateValidator,
    ) -> None:
        if not isinstance(predicate_id, str) or not _IDENTIFIER_RE.fullmatch(
            predicate_id
        ):
            raise ValidatorRegistryError(
                "validation_validator_not_registered",
                "validator predicate id is invalid",
            )
        if (
            isinstance(predicate_version, bool)
            or not isinstance(predicate_version, int)
            or predicate_version <= 0
        ):
            raise ValidatorRegistryError(
                "validation_predicate_version_unsupported",
                "predicate version is invalid",
            )
        if predicate_id not in PREDICATE_VERSIONS:
            raise ValidatorRegistryError(
                "validation_predicate_unsupported", "predicate is unsupported"
            )
        if predicate_version not in PREDICATE_VERSIONS[predicate_id]:
            raise ValidatorRegistryError(
                "validation_predicate_version_unsupported",
                "predicate version is unsupported",
            )
        if not isinstance(validator_id, str) or not _IDENTIFIER_RE.fullmatch(
            validator_id
        ):
            raise ValidatorRegistryError(
                "validation_validator_not_registered", "validator id is invalid"
            )
        if (
            isinstance(validator_version, bool)
            or not isinstance(validator_version, int)
            or validator_version <= 0
        ):
            raise ValidatorRegistryError(
                "validation_validator_not_registered", "validator version is invalid"
            )
        if not callable(getattr(validator, "validate", None)):
            raise ValidatorRegistryError(
                "validation_validator_not_registered",
                "validator implementation is invalid",
            )
        if (
            getattr(validator, "validator_id", None) != validator_id
            or getattr(validator, "validator_version", None) != validator_version
        ):
            raise ValidatorRegistryError(
                "validation_validator_not_registered",
                "validator implementation is unversioned or mismatched",
            )
        key = (predicate_id, predicate_version)
        if key in self._registrations:
            raise ValidatorRegistryError(
                "validation_validator_not_registered", "validator is already registered"
            )
        self._registrations[key] = ValidatorRegistration(
            predicate_id=predicate_id,
            predicate_version=predicate_version,
            validator_id=validator_id,
            validator_version=validator_version,
            validator=validator,
        )

    def registration(
        self, predicate_id: str, predicate_version: int
    ) -> ValidatorRegistration | None:
        return self._registrations.get((predicate_id, predicate_version))

    def resolve(
        self,
        predicate: ValidationPredicate,
        context: ValidatorExecutionContext,
        *,
        expected_validator_id: str | None = None,
        expected_validator_version: int | None = None,
    ) -> ValidatorRegistration:
        if (
            context.validator_set_id != self.validator_set_id
            or context.validator_set_version != self.validator_set_version
        ):
            raise ValidatorRegistryError(
                "validation_environment_mismatch",
                "validator set identity does not match environment",
            )
        if (
            self.configuration_hash is not None
            and context.environment_configuration_hash != self.configuration_hash
        ):
            raise ValidatorRegistryError(
                "validation_environment_mismatch",
                "validator configuration does not match environment",
            )
        if predicate.predicate_id not in PREDICATE_VERSIONS:
            raise ValidatorRegistryError(
                "validation_predicate_unsupported", "predicate is unsupported"
            )
        if (
            predicate.predicate_version
            not in PREDICATE_VERSIONS[predicate.predicate_id]
        ):
            raise ValidatorRegistryError(
                "validation_predicate_version_unsupported",
                "predicate version is unsupported",
            )
        registration = self.registration(
            predicate.predicate_id, predicate.predicate_version
        )
        if registration is None:
            raise ValidatorRegistryError(
                "validation_validator_not_registered", "validator is not registered"
            )
        if (
            expected_validator_id is not None
            and expected_validator_id != registration.validator_id
        ):
            raise ValidatorRegistryError(
                "validation_validator_version_mismatch",
                "validator identity does not match registry",
            )
        if (
            expected_validator_version is not None
            and expected_validator_version != registration.validator_version
        ):
            raise ValidatorRegistryError(
                "validation_validator_version_mismatch",
                "validator version does not match registry",
            )
        return registration


def _predicate_result(
    status: str,
    code: str,
    *,
    diagnostics: Mapping[str, Any] | None = None,
    expected: Mapping[str, Any] | None = None,
    actual: Mapping[str, Any] | None = None,
) -> CandidatePredicateResult:
    if status not in PREDICATE_RESULT_STATUSES:
        raise CandidateEvidenceError(
            "validation_validator_error", "validator returned an invalid status"
        )
    return CandidatePredicateResult(
        result_status=status,
        passed=status == "passed",
        result_code=_bounded_text(code, "result code", 64),
        diagnostics=MappingProxyType(
            _bounded_json_bytes(
                diagnostics or {}, field="diagnostics", limit=MAX_DIAGNOSTIC_BYTES
            )
            or {}
        ),
        expected_summary=(
            MappingProxyType(
                _bounded_json_bytes(
                    expected, field="expected summary", limit=MAX_METADATA_BYTES
                )
                or {}
            )
            if expected is not None
            else None
        ),
        actual_summary=(
            MappingProxyType(
                _bounded_json_bytes(
                    actual, field="actual summary", limit=MAX_METADATA_BYTES
                )
                or {}
            )
            if actual is not None
            else None
        ),
    )


def _evidence_gate(
    evidence: ResolvedCandidateEvidence,
) -> CandidatePredicateResult | None:
    if evidence.resolution_status == "resolved":
        return None
    if evidence.resolution_status == "missing":
        return _predicate_result("missing_evidence", "candidate_evidence_missing")
    if evidence.resolution_status == "unsupported":
        return _predicate_result("unsupported", "candidate_evidence_unsupported")
    return _predicate_result("invalid_evidence", "candidate_evidence_invalid")


class _RequiredOutputExistsValidator:
    validator_id = "required_output_exists"
    validator_version = VALIDATOR_IMPLEMENTATION_VERSION

    def validate(self, predicate, evidence, context):
        gate = _evidence_gate(evidence)
        if gate:
            return gate
        return _predicate_result(
            (
                "passed"
                if evidence.source_authority_id and evidence.normalized_reference
                else "failed"
            ),
            (
                "required_output_present"
                if evidence.source_authority_id and evidence.normalized_reference
                else "required_output_missing"
            ),
        )


class _OutputReferenceExistsValidator:
    validator_id = "output_reference_exists"
    validator_version = VALIDATOR_IMPLEMENTATION_VERSION

    def validate(self, predicate, evidence, context):
        gate = _evidence_gate(evidence)
        if gate:
            return gate
        passed = bool(evidence.normalized_reference and evidence.source_authority_id)
        return _predicate_result(
            "passed" if passed else "failed",
            "output_reference_present" if passed else "output_reference_missing",
            actual=(
                {"normalized_reference": evidence.normalized_reference}
                if passed
                else {}
            ),
        )


class _OutputHashMatchesValidator:
    validator_id = "output_hash_matches"
    validator_version = VALIDATOR_IMPLEMENTATION_VERSION

    def validate(self, predicate, evidence, context):
        gate = _evidence_gate(evidence)
        if gate:
            return gate
        if not evidence.expected_hash or not evidence.actual_hash:
            return _predicate_result(
                "invalid_evidence", "candidate_evidence_hash_unavailable"
            )
        passed = evidence.expected_hash == evidence.actual_hash
        return _predicate_result(
            "passed" if passed else "failed",
            "output_hash_matches" if passed else "output_hash_mismatch",
            expected={
                "hash_algorithm": evidence.expected_hash_algorithm,
                "hash": evidence.expected_hash,
            },
            actual={
                "hash_algorithm": evidence.expected_hash_algorithm,
                "hash": evidence.actual_hash,
            },
        )


def _field_value(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


class _RequiredFieldsPresentValidator:
    validator_id = "required_fields_present"
    validator_version = VALIDATOR_IMPLEMENTATION_VERSION

    def validate(self, predicate, evidence, context):
        gate = _evidence_gate(evidence)
        if gate:
            return gate
        if not isinstance(evidence.content_projection, Mapping):
            return _predicate_result(
                "invalid_evidence", "candidate_evidence_structured_content_unavailable"
            )
        fields = predicate.parameters.get("fields")
        if not isinstance(fields, list):
            return _predicate_result("validator_error", "validator_parameters_invalid")
        missing = [
            field
            for field in fields
            if not _field_value(evidence.content_projection, field)[0]
        ]
        return _predicate_result(
            "failed" if missing else "passed",
            "required_fields_missing" if missing else "required_fields_present",
            expected={"fields": fields},
            actual={"missing_fields": missing},
        )


def build_default_validator_registry(
    *, configuration_hash: str | None = None
) -> DeterministicValidatorRegistry:
    registry = DeterministicValidatorRegistry(configuration_hash=configuration_hash)
    registry.register(
        predicate_id="required_output_exists",
        predicate_version=1,
        validator_id="required_output_exists",
        validator_version=1,
        validator=_RequiredOutputExistsValidator(),
    )
    registry.register(
        predicate_id="output_reference_exists",
        predicate_version=1,
        validator_id="output_reference_exists",
        validator_version=1,
        validator=_OutputReferenceExistsValidator(),
    )
    registry.register(
        predicate_id="output_hash_matches",
        predicate_version=1,
        validator_id="output_hash_matches",
        validator_version=1,
        validator=_OutputHashMatchesValidator(),
    )
    registry.register(
        predicate_id="required_fields_present",
        predicate_version=1,
        validator_id="required_fields_present",
        validator_version=1,
        validator=_RequiredFieldsPresentValidator(),
    )
    return registry


def _command_text(value: Any, field: str, limit: int) -> str:
    value = _bounded_text(value, field, limit).strip()
    if not value:
        raise CandidateEvidenceError(
            "candidate_evidence_reference_invalid", f"{field} is required"
        )
    return value


def _resolution_command_payload(
    command: ResolveCandidateEvidenceCommand,
) -> dict[str, Any]:
    expected_reference = normalize_evidence_reference(command.expected_reference)
    return {
        "schema_version": RESOLVER_CONTRACT_VERSION,
        "reference_grammar_version": REFERENCE_GRAMMAR_VERSION,
        "execution_plan_id": int(command.execution_plan_id),
        "execution_task_id": int(command.execution_task_id),
        "execution_task_attempt_id": int(command.execution_task_attempt_id),
        "candidate_outcome_id": int(command.candidate_outcome_id),
        "validation_specification_id": int(command.validation_specification_id),
        "validation_specification_hash": _hash(
            command.validation_specification_hash, "validation specification hash"
        ),
        "evidence_key": _command_text(command.evidence_key, "evidence key", 64),
        "evidence_type": _command_text(command.evidence_type, "evidence type", 64),
        "evidence_source": _command_text(
            command.evidence_source, "evidence source", 64
        ),
        "expected_reference": expected_reference,
        "expected_output_reference": _optional_text(
            command.expected_output_reference,
            "expected output reference",
            MAX_OUTPUT_REFERENCE_LENGTH,
        ),
        "expected_content_hash": _optional_hash(
            command.expected_content_hash, "expected content hash"
        ),
        "expected_hash_algorithm": _optional_text(
            command.expected_hash_algorithm, "expected hash algorithm", 16
        ),
        "resolver_version": _command_text(
            command.resolver_version, "resolver version", 64
        ),
        "environment_configuration_hash": _hash(
            command.environment_configuration_hash, "environment configuration hash"
        ),
        "resolution_idempotency_key": _command_text(
            command.resolution_idempotency_key,
            "resolution idempotency key",
            MAX_IDEMPOTENCY_KEY_LENGTH,
        ),
        "deterministic_resolution_command_id": _command_text(
            command.deterministic_resolution_command_id,
            "resolution command id",
            MAX_COMMAND_ID_LENGTH,
        ),
        "creation_actor_type": _command_text(
            command.creation_actor_type, "creation actor type", 64
        ),
        "creation_actor_id": _command_text(
            command.creation_actor_id, "creation actor id", 255
        ),
    }


def _snapshot_payload(row: ExecutionTaskResolvedValidationEvidence) -> dict[str, Any]:
    return {
        "schema_version": RESOLVER_CONTRACT_VERSION,
        "reference_grammar_version": REFERENCE_GRAMMAR_VERSION,
        "execution_plan_id": row.execution_plan_id,
        "execution_task_id": row.execution_task_id,
        "execution_task_attempt_id": row.execution_task_attempt_id,
        "candidate_outcome_id": row.candidate_outcome_id,
        "validation_specification_id": row.validation_specification_id,
        "validation_specification_hash": row.validation_specification_hash,
        "evidence_key": row.evidence_key,
        "evidence_type": row.evidence_type,
        "source": row.source,
        "normalized_reference": row.normalized_reference,
        "source_authority_id": row.source_authority_id,
        "resolver_id": row.resolver_id,
        "resolver_version": row.resolver_version,
        "resolver_contract_version": row.resolver_contract_version,
        "environment_configuration_hash": row.environment_configuration_hash,
        "expected_hash_algorithm": row.expected_hash_algorithm,
        "expected_hash": row.expected_hash,
        "actual_hash": row.actual_hash,
        "media_type": row.media_type,
        "byte_size": row.byte_size,
        "structured_metadata_summary": row.structured_metadata_summary,
        "content_addressed_reference": row.content_addressed_reference,
        "content_projection": row.content_projection,
        "expected_output_reference": row.expected_output_reference,
        "resolution_status": row.resolution_status,
        "task_state_at_resolution": row.task_state_at_resolution,
        "task_state_version_at_resolution": row.task_state_version_at_resolution,
    }


def _snapshot_dto(
    row: ExecutionTaskResolvedValidationEvidence,
) -> ResolvedCandidateEvidence:
    return ResolvedCandidateEvidence(
        id=row.id,
        execution_plan_id=row.execution_plan_id,
        execution_task_id=row.execution_task_id,
        execution_task_attempt_id=row.execution_task_attempt_id,
        candidate_outcome_id=row.candidate_outcome_id,
        validation_specification_id=row.validation_specification_id,
        validation_specification_hash=row.validation_specification_hash,
        evidence_key=row.evidence_key,
        evidence_type=row.evidence_type,
        source=row.source,
        normalized_reference=row.normalized_reference,
        source_authority_id=row.source_authority_id,
        resolver_id=row.resolver_id,
        resolver_version=row.resolver_version,
        resolver_contract_version=row.resolver_contract_version,
        environment_configuration_hash=row.environment_configuration_hash,
        expected_hash_algorithm=row.expected_hash_algorithm,
        expected_hash=row.expected_hash,
        actual_hash=row.actual_hash,
        media_type=row.media_type,
        byte_size=row.byte_size,
        structured_metadata_summary=_freeze(row.structured_metadata_summary or {}),
        content_projection=(
            _freeze(row.content_projection)
            if isinstance(row.content_projection, Mapping)
            else None
        ),
        resolution_status=row.resolution_status,
        canonical_evidence_payload_hash=row.canonical_evidence_payload_hash,
        resolved_at=_utc(row.resolved_at),
    )


class CandidateEvidenceResolverService:
    """Persist one immutable, idempotent snapshot from an outcome authority row."""

    def __init__(
        self,
        db: Session,
        *,
        source: ImmutableCandidateEvidenceSource | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.db = db
        self.source = source or SqlCandidateOutcomeSource(db)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def resolve(
        self, command: ResolveCandidateEvidenceCommand
    ) -> ResolvedEvidenceResult:
        payload = _resolution_command_payload(command)
        command_hash = canonical_json_hash(payload)
        existing = (
            self.db.query(ExecutionTaskResolvedValidationEvidence)
            .filter(
                ExecutionTaskResolvedValidationEvidence.resolution_idempotency_key
                == payload["resolution_idempotency_key"]
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_resolution_command_hash != command_hash:
                raise CandidateEvidenceError(
                    "candidate_evidence_idempotency_conflict",
                    "resolution idempotency key is bound to another command",
                )
            return ResolvedEvidenceResult(_snapshot_dto(existing), replayed=True)

        duplicate = (
            self.db.query(ExecutionTaskResolvedValidationEvidence)
            .filter(
                ExecutionTaskResolvedValidationEvidence.candidate_outcome_id
                == command.candidate_outcome_id,
                ExecutionTaskResolvedValidationEvidence.validation_specification_id
                == command.validation_specification_id,
                ExecutionTaskResolvedValidationEvidence.evidence_key
                == payload["evidence_key"],
            )
            .one_or_none()
        )
        if duplicate is not None:
            raise CandidateEvidenceError(
                "candidate_evidence_resolution_conflict",
                "candidate evidence already has a canonical snapshot",
            )

        plan, task, specification, descriptor, environment, outcome, attempt = (
            self._authorize(command, payload)
        )
        source_outcome = self.source.read(outcome.id)
        if source_outcome is None:
            raise CandidateEvidenceError(
                "candidate_evidence_not_found", "candidate outcome was not found"
            )
        if source_outcome.id != outcome.id:
            raise CandidateEvidenceError(
                "candidate_evidence_source_not_immutable", "source identity is invalid"
            )

        status, status_code = self._resolution_status(
            command, descriptor, source_outcome, payload["expected_reference"]
        )
        now = self._now()
        metadata = (
            _bounded_json_bytes(
                {
                    "outcome_status": source_outcome.outcome_status,
                    "source_authority_id": f"execution-task-attempt-outcome:{source_outcome.id}",
                    "has_output_reference": bool(source_outcome.output_reference),
                    "has_output_hash": bool(source_outcome.output_hash),
                },
                field="structured metadata summary",
                limit=MAX_METADATA_BYTES,
            )
            or {}
        )
        evidence_payload = {
            "schema_version": RESOLVER_CONTRACT_VERSION,
            "reference_grammar_version": REFERENCE_GRAMMAR_VERSION,
            "execution_plan_id": plan.id,
            "execution_task_id": task.id,
            "execution_task_attempt_id": attempt.id,
            "candidate_outcome_id": outcome.id,
            "validation_specification_id": specification.id,
            "validation_specification_hash": specification.canonical_specification_hash,
            "evidence_key": descriptor.evidence_key,
            "evidence_type": descriptor.evidence_type,
            "source": descriptor.source,
            "normalized_reference": payload["expected_reference"],
            "source_authority_id": f"execution-task-attempt-outcome:{outcome.id}",
            "resolver_id": RESOLVER_ID,
            "resolver_version": command.resolver_version,
            "resolver_contract_version": RESOLVER_CONTRACT_VERSION,
            "environment_configuration_hash": environment.configuration_hash,
            "expected_hash_algorithm": descriptor.expected_hash_algorithm,
            "expected_hash": command.expected_content_hash,
            "actual_hash": source_outcome.output_hash,
            "media_type": None,
            "byte_size": None,
            "structured_metadata_summary": metadata,
            "content_addressed_reference": None,
            "content_projection": None,
            "expected_output_reference": command.expected_output_reference,
            "resolution_status": status,
            "task_state_at_resolution": task.status,
            "task_state_version_at_resolution": task.state_version,
        }
        evidence_hash = canonical_json_hash(evidence_payload)
        row = ExecutionTaskResolvedValidationEvidence(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            execution_task_attempt_id=attempt.id,
            candidate_outcome_id=outcome.id,
            validation_specification_id=specification.id,
            validation_specification_hash=specification.canonical_specification_hash,
            evidence_key=descriptor.evidence_key,
            evidence_type=descriptor.evidence_type,
            source=descriptor.source,
            normalized_reference=payload["expected_reference"],
            source_authority_id=f"execution-task-attempt-outcome:{outcome.id}",
            resolver_id=RESOLVER_ID,
            resolver_version=command.resolver_version,
            resolver_contract_version=RESOLVER_CONTRACT_VERSION,
            environment_configuration_hash=environment.configuration_hash,
            expected_hash_algorithm=descriptor.expected_hash_algorithm,
            expected_hash=command.expected_content_hash,
            actual_hash=source_outcome.output_hash,
            media_type=None,
            byte_size=None,
            structured_metadata_summary=metadata,
            content_addressed_reference=None,
            content_projection=None,
            expected_output_reference=command.expected_output_reference,
            resolution_status=status,
            resolution_idempotency_key=payload["resolution_idempotency_key"],
            deterministic_resolution_command_id=payload[
                "deterministic_resolution_command_id"
            ],
            canonical_resolution_command_payload=payload,
            canonical_resolution_command_hash=command_hash,
            canonical_evidence_payload=evidence_payload,
            canonical_evidence_payload_hash=evidence_hash,
            task_state_at_resolution=task.status,
            task_state_version_at_resolution=task.state_version,
            resolved_at=now,
            creation_actor_type=payload["creation_actor_type"],
            creation_actor_id=payload["creation_actor_id"],
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(ExecutionTaskResolvedValidationEvidence)
                .filter(
                    ExecutionTaskResolvedValidationEvidence.resolution_idempotency_key
                    == payload["resolution_idempotency_key"]
                )
                .one_or_none()
            )
            if (
                replay is not None
                and replay.canonical_resolution_command_hash == command_hash
            ):
                return ResolvedEvidenceResult(_snapshot_dto(replay), replayed=True)
            raise CandidateEvidenceError(
                "candidate_evidence_resolution_conflict",
                "resolution command conflicts with canonical evidence",
            ) from exc
        return ResolvedEvidenceResult(_snapshot_dto(row))

    def _authorize(self, command, payload):
        plan = self.db.get(ExecutionPlan, int(command.execution_plan_id))
        task = self.db.get(ExecutionTask, int(command.execution_task_id))
        specification = self.db.get(
            ExecutionTaskValidationSpecification,
            int(command.validation_specification_id),
        )
        outcome = self.db.get(
            ExecutionTaskAttemptOutcome, int(command.candidate_outcome_id)
        )
        attempt = self.db.get(
            ExecutionTaskAttempt, int(command.execution_task_attempt_id)
        )
        if any(item is None for item in (plan, task, specification, outcome, attempt)):
            raise CandidateEvidenceError(
                "candidate_evidence_not_found",
                "candidate evidence authority is incomplete",
            )
        if plan.status != "active" or plan.superseded_by_execution_plan_id is not None:
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure", "execution plan is not active"
            )
        if (
            task.execution_plan_id != plan.id
            or specification.execution_task_id != task.id
        ):
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "task and specification identity do not match",
            )
        if task.validation_contract_status != "structured_executable":
            raise CandidateEvidenceError(
                "candidate_evidence_validation_contract_unavailable",
                "task has no executable validation contract",
            )
        if (
            task.validation_contract_id != specification.id
            or specification.execution_plan_id != plan.id
        ):
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "validation specification identity does not match",
            )
        if (
            specification.canonical_specification_hash
            != payload["validation_specification_hash"]
        ):
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "validation specification hash does not match",
            )
        if task.status != "awaiting_validation":
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "task is not awaiting validation",
            )
        contract_integrity = ValidationContractService(
            self.db
        ).verify_validation_contract_integrity(specification.id)
        if not contract_integrity.verified:
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "validation specification integrity failed",
            )
        if (
            outcome.execution_plan_id != plan.id
            or outcome.execution_task_id != task.id
            or outcome.execution_task_attempt_id != attempt.id
        ):
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "candidate outcome identity does not match",
            )
        if outcome.outcome_status != "candidate_completed":
            raise CandidateEvidenceError(
                "candidate_evidence_reference_missing",
                "candidate outcome is not candidate_completed",
            )
        if attempt.attempt_status != "candidate_completed":
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "runtime attempt status does not match outcome",
            )
        runtime_integrity = ExecutionTaskRuntimeExecutionService(
            self.db
        ).verify_attempt_outcome_integrity(outcome.id)
        if not runtime_integrity.verified:
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "runtime evidence integrity failed",
            )
        structured = specification.canonical_payload.get("structured_contract")
        try:
            contract = StructuredValidationContract.from_mapping(structured)
        except (TypeError, ValidationContractError) as exc:
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "structured validation contract is invalid",
            ) from exc
        descriptor = next(
            (
                item
                for item in contract.evidence_descriptors
                if item.evidence_key == payload["evidence_key"]
            ),
            None,
        )
        if descriptor is None:
            raise CandidateEvidenceError(
                "candidate_evidence_reference_missing",
                "evidence descriptor was not found",
            )
        if (
            descriptor.evidence_type != payload["evidence_type"]
            or descriptor.source != payload["evidence_source"]
        ):
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "evidence descriptor does not match command",
            )
        if payload["expected_hash_algorithm"] != descriptor.expected_hash_algorithm:
            raise CandidateEvidenceError(
                "candidate_evidence_integrity_failure",
                "expected hash algorithm does not match descriptor",
            )
        if (
            descriptor.resolver_version != payload["resolver_version"]
            or contract.environment.resolver_version != payload["resolver_version"]
        ):
            raise CandidateEvidenceError(
                "validation_environment_mismatch",
                "resolver version does not match frozen environment",
            )
        if (
            contract.environment.configuration_hash
            != payload["environment_configuration_hash"]
        ):
            raise CandidateEvidenceError(
                "validation_environment_mismatch",
                "resolver environment does not match frozen environment",
            )
        reference = parse_evidence_reference(payload["expected_reference"])
        if reference.identifier != outcome.id:
            raise CandidateEvidenceError(
                "candidate_evidence_reference_invalid",
                "reference does not identify candidate outcome",
            )
        return (
            plan,
            task,
            specification,
            descriptor,
            contract.environment,
            outcome,
            attempt,
        )

    @staticmethod
    def _resolution_status(command, descriptor, outcome, normalized_reference):
        if (
            descriptor.source != "candidate_outcome"
            or descriptor.evidence_type
            not in {
                "candidate_output_reference",
                "candidate_output_hash",
            }
        ):
            return "unsupported", "candidate_evidence_source_unsupported"
        if descriptor.expected_media_type is not None:
            return "unsupported", "candidate_evidence_media_type_unavailable"
        if descriptor.expected_hash_algorithm and command.expected_content_hash is None:
            return "missing", "candidate_evidence_reference_missing"
        if not outcome.output_reference:
            return "missing", "candidate_evidence_reference_missing"
        if (
            command.expected_output_reference is not None
            and command.expected_output_reference != outcome.output_reference
        ):
            return "invalid_reference", "candidate_evidence_reference_invalid"
        if descriptor.evidence_type == "candidate_output_hash":
            if (
                descriptor.expected_hash_algorithm != VALIDATION_HASH_ALGORITHM
                or command.expected_content_hash is None
            ):
                return "unsupported", "candidate_evidence_hash_contract_unsupported"
            if outcome.output_hash is None:
                return "missing", "candidate_evidence_reference_missing"
        if command.expected_content_hash is not None:
            if outcome.output_hash is None:
                return "missing", "candidate_evidence_reference_missing"
            if command.expected_content_hash != outcome.output_hash:
                return "hash_mismatch", "candidate_evidence_hash_mismatch"
        return "resolved", "candidate_evidence_resolved"


def verify_resolved_validation_evidence_integrity(
    db: Session, evidence_id: int
) -> "ValidationPrimitiveIntegrityResult":
    return ValidationPrimitiveService(db).verify_resolved_validation_evidence_integrity(
        evidence_id
    )


@dataclass(frozen=True)
class ValidationPrimitiveIntegrityResult:
    execution_plan_id: int | None
    execution_task_id: int | None
    verified: bool
    issues: tuple[str, ...] = ()


def _issue(
    result: ValidationPrimitiveIntegrityResult, *issues: str
) -> ValidationPrimitiveIntegrityResult:
    return ValidationPrimitiveIntegrityResult(
        execution_plan_id=result.execution_plan_id,
        execution_task_id=result.execution_task_id,
        verified=False,
        issues=tuple(sorted(set(result.issues).union(issues))),
    )


class ValidationPrimitiveService:
    def __init__(
        self, db: Session, *, registry: DeterministicValidatorRegistry | None = None
    ):
        self.db = db
        self.registry = registry or build_default_validator_registry()

    def verify_resolved_validation_evidence_integrity(
        self, evidence_id: int
    ) -> ValidationPrimitiveIntegrityResult:
        row = self.db.get(ExecutionTaskResolvedValidationEvidence, int(evidence_id))
        if row is None:
            return ValidationPrimitiveIntegrityResult(
                None, None, False, ("resolved_evidence_missing",)
            )
        issues: list[str] = []
        plan = self.db.get(ExecutionPlan, row.execution_plan_id)
        task = self.db.get(ExecutionTask, row.execution_task_id)
        attempt = self.db.get(ExecutionTaskAttempt, row.execution_task_attempt_id)
        outcome = self.db.get(ExecutionTaskAttemptOutcome, row.candidate_outcome_id)
        specification = self.db.get(
            ExecutionTaskValidationSpecification, row.validation_specification_id
        )
        if any(item is None for item in (plan, task, attempt, outcome, specification)):
            issues.append("resolved_evidence_authority_missing")
        if task is not None and task.execution_plan_id != row.execution_plan_id:
            issues.append("resolved_evidence_task_identity_mismatch")
        if outcome is not None and (
            outcome.execution_plan_id != row.execution_plan_id
            or outcome.execution_task_id != row.execution_task_id
            or outcome.execution_task_attempt_id != row.execution_task_attempt_id
        ):
            issues.append("resolved_evidence_outcome_identity_mismatch")
        if specification is not None:
            if (
                specification.execution_plan_id != row.execution_plan_id
                or specification.execution_task_id != row.execution_task_id
            ):
                issues.append("resolved_evidence_specification_identity_mismatch")
            if (
                specification.canonical_specification_hash
                != row.validation_specification_hash
            ):
                issues.append("resolved_evidence_specification_hash_tampered")
            contract_integrity = ValidationContractService(
                self.db
            ).verify_validation_contract_integrity(specification.id)
            issues.extend(contract_integrity.issues)
            try:
                contract = StructuredValidationContract.from_mapping(
                    specification.canonical_payload["structured_contract"]
                )
                descriptor = next(
                    item
                    for item in contract.evidence_descriptors
                    if item.evidence_key == row.evidence_key
                )
                if (
                    descriptor.evidence_type != row.evidence_type
                    or descriptor.source != row.source
                    or descriptor.resolver_version != row.resolver_version
                    or descriptor.expected_hash_algorithm != row.expected_hash_algorithm
                    or contract.environment.configuration_hash
                    != row.environment_configuration_hash
                    or contract.environment.resolver_version != row.resolver_version
                ):
                    issues.append("resolved_evidence_descriptor_mismatch")
            except (KeyError, StopIteration, TypeError, ValidationContractError):
                issues.append("resolved_evidence_descriptor_missing")
        if row.source != "candidate_outcome":
            issues.append("resolved_evidence_source_unsupported")
        if (
            row.source_authority_id
            != f"execution-task-attempt-outcome:{row.candidate_outcome_id}"
        ):
            issues.append("resolved_evidence_source_identity_tampered")
        try:
            reference = parse_evidence_reference(row.normalized_reference)
            if reference.identifier != row.candidate_outcome_id:
                issues.append("resolved_evidence_reference_tampered")
        except CandidateEvidenceError:
            issues.append("resolved_evidence_reference_invalid")
        if row.expected_hash_algorithm not in {None, VALIDATION_HASH_ALGORITHM}:
            issues.append("resolved_evidence_expected_hash_tampered")
        for field, value, issue_name in (
            (
                "expected_hash",
                row.expected_hash,
                "resolved_evidence_expected_hash_tampered",
            ),
            ("actual_hash", row.actual_hash, "resolved_evidence_actual_hash_tampered"),
            (
                "validation_specification_hash",
                row.validation_specification_hash,
                "resolved_evidence_specification_hash_tampered",
            ),
            (
                "environment_configuration_hash",
                row.environment_configuration_hash,
                "resolved_evidence_environment_tampered",
            ),
        ):
            if value is not None and not _HASH_RE.fullmatch(str(value)):
                issues.append(issue_name)
        if row.resolution_status not in EVIDENCE_RESOLUTION_STATUSES:
            issues.append("resolved_evidence_status_invalid")
        if row.task_state_at_resolution != "awaiting_validation":
            issues.append("resolved_evidence_task_state_invalid")
        if row.task_state_version_at_resolution < 0:
            issues.append("resolved_evidence_state_version_invalid")
        if outcome is not None:
            runtime_integrity = ExecutionTaskRuntimeExecutionService(
                self.db
            ).verify_attempt_outcome_integrity(outcome.id)
            issues.extend(runtime_integrity.issues)
        try:
            expected_payload = _snapshot_payload(row)
            if row.canonical_evidence_payload != expected_payload:
                issues.append("resolved_evidence_payload_tampered")
            if (
                canonical_json_hash(row.canonical_evidence_payload)
                != row.canonical_evidence_payload_hash
            ):
                issues.append("resolved_evidence_payload_hash_mismatch")
            if not isinstance(row.canonical_resolution_command_payload, Mapping):
                issues.append("resolved_evidence_command_payload_malformed")
            elif (
                canonical_json_hash(row.canonical_resolution_command_payload)
                != row.canonical_resolution_command_hash
            ):
                issues.append("resolved_evidence_command_hash_mismatch")
        except (TypeError, ValueError):
            issues.append("resolved_evidence_payload_malformed")
        if outcome is not None and (_utc(row.resolved_at) or row.resolved_at) < (
            _utc(outcome.completed_at) or outcome.completed_at
        ):
            issues.append("resolved_evidence_timestamp_order_invalid")
        duplicates = (
            self.db.query(ExecutionTaskResolvedValidationEvidence)
            .filter(
                ExecutionTaskResolvedValidationEvidence.candidate_outcome_id
                == row.candidate_outcome_id,
                ExecutionTaskResolvedValidationEvidence.validation_specification_id
                == row.validation_specification_id,
                ExecutionTaskResolvedValidationEvidence.evidence_key
                == row.evidence_key,
            )
            .count()
        )
        if duplicates != 1:
            issues.append("duplicate_resolved_evidence")
        return ValidationPrimitiveIntegrityResult(
            row.execution_plan_id,
            row.execution_task_id,
            not issues,
            tuple(sorted(set(issues))),
        )

    def verify_validation_predicate_result_integrity(
        self, result_id: int
    ) -> ValidationPrimitiveIntegrityResult:
        row = self.db.get(DBValidationPredicateResult, int(result_id))
        if row is None:
            return ValidationPrimitiveIntegrityResult(
                None, None, False, ("validation_predicate_result_missing",)
            )
        issues: list[str] = []
        evidence = self.db.get(
            ExecutionTaskResolvedValidationEvidence, row.evidence_snapshot_id
        )
        specification = self.db.get(
            ExecutionTaskValidationSpecification, row.validation_specification_id
        )
        task = self.db.get(ExecutionTask, row.execution_task_id)
        if evidence is None or specification is None or task is None:
            issues.append("validation_predicate_result_authority_missing")
        if evidence is not None:
            if (
                evidence.candidate_outcome_id != row.candidate_outcome_id
                or evidence.evidence_key != row.evidence_key
            ):
                issues.append("validation_predicate_result_evidence_mismatch")
            if (
                evidence.execution_plan_id != row.execution_plan_id
                or evidence.execution_task_id != row.execution_task_id
                or evidence.execution_task_attempt_id != row.execution_task_attempt_id
            ):
                issues.append("validation_predicate_result_identity_mismatch")
            issues.extend(
                self.verify_resolved_validation_evidence_integrity(evidence.id).issues
            )
            if (_utc(row.started_at) or row.started_at) < (
                _utc(evidence.resolved_at) or evidence.resolved_at
            ) or (_utc(row.completed_at) or row.completed_at) < (
                _utc(evidence.resolved_at) or evidence.resolved_at
            ):
                issues.append("validation_predicate_result_timestamp_order_invalid")
            if (_utc(row.completed_at) or row.completed_at) < (
                _utc(row.started_at) or row.started_at
            ):
                issues.append("validation_predicate_result_timestamp_order_invalid")
        if specification is not None:
            if (
                specification.canonical_specification_hash
                != row.validation_specification_hash
            ):
                issues.append("validation_predicate_result_specification_hash_tampered")
            try:
                contract = StructuredValidationContract.from_mapping(
                    specification.canonical_payload["structured_contract"]
                )
                predicate = next(
                    (
                        item
                        for item in contract.predicates
                        if item.predicate_id == row.predicate_id
                        and item.predicate_version == row.predicate_version
                    ),
                    None,
                )
                if (
                    predicate is None
                    or predicate.order != row.predicate_order
                    or predicate.evidence_key != row.evidence_key
                ):
                    issues.append("validation_predicate_result_predicate_mismatch")
            except (KeyError, TypeError, ValidationContractError):
                issues.append("validation_predicate_result_specification_invalid")
        registration = self.registry.registration(
            row.predicate_id, row.predicate_version
        )
        if registration is None and row.result_status != "unsupported":
            issues.append("validation_predicate_result_validator_not_registered")
        elif registration is not None:
            if (
                registration.validator_id != row.validator_id
                or registration.validator_version != row.validator_version
            ):
                issues.append("validation_predicate_result_validator_version_mismatch")
        if specification is not None and isinstance(
            specification.environment_identity, Mapping
        ):
            environment = specification.environment_identity
            if (
                environment.get("configuration_hash")
                != row.environment_configuration_hash
            ):
                issues.append("validation_predicate_result_environment_mismatch")
            if (
                environment.get("validator_set_id") != row.validator_set_id
                or environment.get("validator_set_version") != row.validator_set_version
            ):
                issues.append("validation_predicate_result_environment_mismatch")
        if row.result_status not in PREDICATE_RESULT_STATUSES:
            issues.append("validation_predicate_result_status_invalid")
        if bool(row.passed) != (row.result_status == "passed"):
            issues.append("validation_predicate_result_passed_mismatch")
        try:
            diagnostics = _bounded_json_bytes(
                row.diagnostics, field="diagnostics", limit=MAX_DIAGNOSTIC_BYTES
            )
            if diagnostics != row.diagnostics:
                issues.append("validation_predicate_result_diagnostics_tampered")
            if row.canonical_result_payload != _result_payload(row):
                issues.append("validation_predicate_result_payload_tampered")
            if (
                canonical_json_hash(row.canonical_result_payload)
                != row.canonical_result_hash
            ):
                issues.append("validation_predicate_result_hash_mismatch")
            expected_command = {
                "schema_version": PREDICATE_RESULT_SCHEMA_VERSION,
                "execution_plan_id": row.execution_plan_id,
                "execution_task_id": row.execution_task_id,
                "execution_task_attempt_id": row.execution_task_attempt_id,
                "candidate_outcome_id": row.candidate_outcome_id,
                "validation_specification_id": row.validation_specification_id,
                "validation_specification_hash": row.validation_specification_hash,
                "predicate_id": row.predicate_id,
                "predicate_version": row.predicate_version,
                "predicate_order": row.predicate_order,
                "evidence_snapshot_id": row.evidence_snapshot_id,
                "evidence_key": row.evidence_key,
                "validator_id": row.validator_id,
                "validator_version": row.validator_version,
                "validator_set_id": row.validator_set_id,
                "validator_set_version": row.validator_set_version,
                "environment_configuration_hash": row.environment_configuration_hash,
                "validator_idempotency_key": row.validator_idempotency_key,
                "deterministic_validator_command_id": row.deterministic_validator_command_id,
                "creation_actor_type": row.creation_actor_type,
                "creation_actor_id": row.creation_actor_id,
            }
            if row.canonical_validator_command_payload != expected_command:
                issues.append("validation_predicate_result_command_payload_tampered")
            if (
                canonical_json_hash(row.canonical_validator_command_payload)
                != row.canonical_validator_command_hash
            ):
                issues.append("validation_predicate_result_command_hash_mismatch")
        except (CandidateEvidenceError, TypeError, ValueError):
            issues.append("validation_predicate_result_payload_malformed")
        duplicates = (
            self.db.query(DBValidationPredicateResult)
            .filter(
                DBValidationPredicateResult.candidate_outcome_id
                == row.candidate_outcome_id,
                DBValidationPredicateResult.validation_specification_id
                == row.validation_specification_id,
                DBValidationPredicateResult.predicate_id == row.predicate_id,
                DBValidationPredicateResult.predicate_version == row.predicate_version,
            )
            .count()
        )
        if duplicates != 1:
            issues.append("duplicate_validation_predicate_result")
        return ValidationPrimitiveIntegrityResult(
            row.execution_plan_id,
            row.execution_task_id,
            not issues,
            tuple(sorted(set(issues))),
        )

    def verify_execution_task_validation_primitives_integrity(
        self, execution_task_id: int
    ) -> ValidationPrimitiveIntegrityResult:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            return ValidationPrimitiveIntegrityResult(
                None,
                int(execution_task_id),
                False,
                ("validation_primitive_integrity_failure",),
            )
        issues: list[str] = []
        contract = (
            self.db.get(
                ExecutionTaskValidationSpecification, task.validation_contract_id
            )
            if task.validation_contract_id
            else None
        )
        if (
            contract is None
            or task.validation_contract_status != "structured_executable"
        ):
            return ValidationPrimitiveIntegrityResult(
                task.execution_plan_id,
                task.id,
                False,
                ("validation_contract_unavailable",),
            )
        structured = contract.canonical_payload.get("structured_contract")
        try:
            executable = StructuredValidationContract.from_mapping(structured)
        except (TypeError, ValidationContractError):
            return ValidationPrimitiveIntegrityResult(
                task.execution_plan_id,
                task.id,
                False,
                ("validation_primitive_integrity_failure",),
            )
        outcomes = (
            self.db.query(ExecutionTaskAttemptOutcome)
            .filter(
                ExecutionTaskAttemptOutcome.execution_task_id == task.id,
                ExecutionTaskAttemptOutcome.outcome_status == "candidate_completed",
            )
            .order_by(ExecutionTaskAttemptOutcome.id.asc())
            .all()
        )
        outcome = outcomes[-1] if outcomes else None
        snapshots = (
            self.db.query(ExecutionTaskResolvedValidationEvidence)
            .filter(
                ExecutionTaskResolvedValidationEvidence.execution_task_id == task.id
            )
            .all()
        )
        results = (
            self.db.query(DBValidationPredicateResult)
            .filter(DBValidationPredicateResult.execution_task_id == task.id)
            .all()
        )
        if outcome is None:
            issues.append("candidate_evidence_reference_missing")
        for snapshot in snapshots:
            issues.extend(
                self.verify_resolved_validation_evidence_integrity(snapshot.id).issues
            )
        for result in results:
            issues.extend(
                self.verify_validation_predicate_result_integrity(result.id).issues
            )
        for descriptor in executable.evidence_descriptors:
            matching = [
                item
                for item in snapshots
                if item.evidence_key == descriptor.evidence_key
                and (outcome is None or item.candidate_outcome_id == outcome.id)
            ]
            if descriptor.required and len(matching) == 0:
                issues.append(f"missing_required_evidence:{descriptor.evidence_key}")
            if (
                any(item.resolution_status == "unsupported" for item in matching)
                and descriptor.required
            ):
                issues.append(
                    f"unsupported_required_evidence:{descriptor.evidence_key}"
                )
        for predicate in executable.predicates:
            matching = [
                item
                for item in results
                if item.predicate_id == predicate.predicate_id
                and item.predicate_version == predicate.predicate_version
                and (outcome is None or item.candidate_outcome_id == outcome.id)
            ]
            if predicate.required and len(matching) == 0:
                issues.append(f"missing_required_predicate:{predicate.predicate_id}")
            if (
                any(item.result_status == "unsupported" for item in matching)
                and predicate.required
            ):
                issues.append(
                    f"unsupported_required_predicate:{predicate.predicate_id}"
                )
            if any(item.result_status == "validator_error" for item in matching):
                issues.append(f"validator_error:{predicate.predicate_id}")
        return ValidationPrimitiveIntegrityResult(
            task.execution_plan_id, task.id, not issues, tuple(sorted(set(issues)))
        )

    def verify_execution_plan_validation_primitives_integrity(
        self, execution_plan_id: int
    ) -> ValidationPrimitiveIntegrityResult:
        plan = self.db.get(ExecutionPlan, int(execution_plan_id))
        if plan is None:
            return ValidationPrimitiveIntegrityResult(
                int(execution_plan_id),
                None,
                False,
                ("validation_primitive_integrity_failure",),
            )
        issues: list[str] = []
        for task in (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == plan.id)
            .order_by(ExecutionTask.plan_task_id.asc())
            .all()
        ):
            issues.extend(
                self.verify_execution_task_validation_primitives_integrity(
                    task.id
                ).issues
            )
        return ValidationPrimitiveIntegrityResult(
            plan.id, None, not issues, tuple(sorted(set(issues)))
        )

    def inspect_execution_task_validation_primitives(
        self, execution_task_id: int
    ) -> "ValidationPrimitivesInspection":
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            raise CandidateEvidenceError(
                "validation_primitive_integrity_failure", "Execution Task was not found"
            )
        specification = (
            self.db.get(
                ExecutionTaskValidationSpecification, task.validation_contract_id
            )
            if task.validation_contract_id
            else None
        )
        if (
            specification is None
            or task.validation_contract_status != "structured_executable"
        ):
            return ValidationPrimitivesInspection(
                task.execution_plan_id,
                task.id,
                "evidence_resolution_not_started",
                (),
                (),
                "validation_primitives_incomplete",
            )
        structured = StructuredValidationContract.from_mapping(
            specification.canonical_payload["structured_contract"]
        )
        snapshots = (
            self.db.query(ExecutionTaskResolvedValidationEvidence)
            .filter_by(execution_task_id=task.id)
            .all()
        )
        results = (
            self.db.query(DBValidationPredicateResult)
            .filter_by(execution_task_id=task.id)
            .all()
        )
        evidence_projection = tuple(
            EvidenceInspectionProjection(
                item.evidence_key,
                item.resolution_status,
                item.resolution_status != "resolved",
            )
            for item in sorted(snapshots, key=lambda item: item.evidence_key)
        )
        predicate_projection = tuple(
            PredicateInspectionProjection(
                item.predicate_id,
                item.predicate_version,
                _predicate_projection_status(item.result_status),
                item.result_status != "passed",
            )
            for item in sorted(
                results, key=lambda item: (item.predicate_id, item.predicate_version)
            )
        )
        if any(item.resolution_status == "hash_mismatch" for item in snapshots):
            evidence_state = "evidence_hash_mismatch"
        elif any(item.resolution_status == "missing" for item in snapshots):
            evidence_state = "evidence_missing"
        elif any(item.resolution_status == "unsupported" for item in snapshots):
            evidence_state = "evidence_unsupported"
        elif snapshots and all(
            item.resolution_status == "resolved" for item in snapshots
        ):
            evidence_state = "evidence_resolved"
        else:
            evidence_state = "evidence_resolution_not_started"
        if not results:
            predicate_state = "predicate_not_evaluated"
        elif any(item.result_status == "validator_error" for item in results):
            predicate_state = "predicate_validator_error"
        elif any(item.result_status == "unsupported" for item in results):
            predicate_state = "predicate_unsupported"
        elif any(item.result_status == "failed" for item in results):
            predicate_state = "predicate_failed"
        elif all(item.result_status == "passed" for item in results):
            predicate_state = "predicate_passed"
        else:
            predicate_state = "predicate_not_evaluated"
        expected_evidence = {
            item.evidence_key
            for item in structured.evidence_descriptors
            if item.required
        }
        expected_predicates = {
            (item.predicate_id, item.predicate_version)
            for item in structured.predicates
            if item.required
        }
        complete = expected_evidence.issubset(
            {
                item.evidence_key
                for item in snapshots
                if item.resolution_status == "resolved"
            }
        ) and expected_predicates.issubset(
            {(item.predicate_id, item.predicate_version) for item in results}
        )
        return ValidationPrimitivesInspection(
            task.execution_plan_id,
            task.id,
            evidence_state,
            evidence_projection,
            predicate_projection,
            (
                "validation_primitives_complete"
                if complete
                else "validation_primitives_incomplete"
            ),
            predicate_state=predicate_state,
        )


def _predicate_projection_status(status: str) -> str:
    return {
        "passed": "predicate_passed",
        "failed": "predicate_failed",
        "validator_error": "predicate_validator_error",
        "unsupported": "predicate_unsupported",
    }.get(status, "predicate_not_evaluated")


@dataclass(frozen=True)
class EvidenceInspectionProjection:
    evidence_key: str
    status: str
    blocked: bool


@dataclass(frozen=True)
class PredicateInspectionProjection:
    predicate_id: str
    predicate_version: int
    status: str
    blocked: bool


@dataclass(frozen=True)
class ValidationPrimitivesInspection:
    execution_plan_id: int
    execution_task_id: int
    evidence_state: str
    evidence: tuple[EvidenceInspectionProjection, ...]
    predicates: tuple[PredicateInspectionProjection, ...]
    primitive_state: str
    predicate_state: str = "predicate_not_evaluated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_plan_id": self.execution_plan_id,
            "execution_task_id": self.execution_task_id,
            "evidence_state": self.evidence_state,
            "evidence": [item.__dict__ for item in self.evidence],
            "predicates": [item.__dict__ for item in self.predicates],
            "predicate_state": self.predicate_state,
            "primitive_state": self.primitive_state,
        }


@dataclass(frozen=True)
class EvaluateCandidatePredicateCommand:
    execution_plan_id: int
    execution_task_id: int
    execution_task_attempt_id: int
    candidate_outcome_id: int
    validation_specification_id: int
    validation_specification_hash: str
    predicate_id: str
    predicate_version: int
    predicate_order: int
    evidence_snapshot_id: int
    evidence_key: str
    validator_id: str
    validator_version: int
    validator_set_id: str
    validator_set_version: str
    environment_configuration_hash: str
    validator_idempotency_key: str
    deterministic_validator_command_id: str
    creation_actor_type: str = "validation_service"
    creation_actor_id: str = "system"


@dataclass(frozen=True)
class PredicateResultEvaluation:
    result: DBValidationPredicateResult
    replayed: bool = False


def _validator_command_payload(
    command: EvaluateCandidatePredicateCommand,
) -> dict[str, Any]:
    return {
        "schema_version": PREDICATE_RESULT_SCHEMA_VERSION,
        "execution_plan_id": int(command.execution_plan_id),
        "execution_task_id": int(command.execution_task_id),
        "execution_task_attempt_id": int(command.execution_task_attempt_id),
        "candidate_outcome_id": int(command.candidate_outcome_id),
        "validation_specification_id": int(command.validation_specification_id),
        "validation_specification_hash": _hash(
            command.validation_specification_hash, "validation specification hash"
        ),
        "predicate_id": _command_text(command.predicate_id, "predicate id", 64),
        "predicate_version": int(command.predicate_version),
        "predicate_order": int(command.predicate_order),
        "evidence_snapshot_id": int(command.evidence_snapshot_id),
        "evidence_key": _command_text(command.evidence_key, "evidence key", 64),
        "validator_id": _command_text(command.validator_id, "validator id", 64),
        "validator_version": int(command.validator_version),
        "validator_set_id": _command_text(
            command.validator_set_id, "validator set id", 128
        ),
        "validator_set_version": _command_text(
            command.validator_set_version, "validator set version", 64
        ),
        "environment_configuration_hash": _hash(
            command.environment_configuration_hash, "environment configuration hash"
        ),
        "validator_idempotency_key": _command_text(
            command.validator_idempotency_key,
            "validator idempotency key",
            MAX_IDEMPOTENCY_KEY_LENGTH,
        ),
        "deterministic_validator_command_id": _command_text(
            command.deterministic_validator_command_id,
            "validator command id",
            MAX_COMMAND_ID_LENGTH,
        ),
        "creation_actor_type": _command_text(
            command.creation_actor_type, "creation actor type", 64
        ),
        "creation_actor_id": _command_text(
            command.creation_actor_id, "creation actor id", 255
        ),
    }


def _result_payload(row: DBValidationPredicateResult) -> dict[str, Any]:
    return {
        "schema_version": PREDICATE_RESULT_SCHEMA_VERSION,
        "execution_plan_id": row.execution_plan_id,
        "execution_task_id": row.execution_task_id,
        "execution_task_attempt_id": row.execution_task_attempt_id,
        "candidate_outcome_id": row.candidate_outcome_id,
        "validation_specification_id": row.validation_specification_id,
        "validation_specification_hash": row.validation_specification_hash,
        "predicate_id": row.predicate_id,
        "predicate_version": row.predicate_version,
        "predicate_order": row.predicate_order,
        "evidence_snapshot_id": row.evidence_snapshot_id,
        "evidence_key": row.evidence_key,
        "validator_id": row.validator_id,
        "validator_version": row.validator_version,
        "validator_set_id": row.validator_set_id,
        "validator_set_version": row.validator_set_version,
        "environment_configuration_hash": row.environment_configuration_hash,
        "result_status": row.result_status,
        "passed": bool(row.passed),
        "result_code": row.result_code,
        "diagnostics": row.diagnostics,
        "expected_summary": row.expected_summary,
        "actual_summary": row.actual_summary,
    }


class DeterministicValidatorService:
    """Evaluate one predicate against one existing immutable evidence row."""

    def __init__(
        self,
        db: Session,
        *,
        registry: DeterministicValidatorRegistry | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.db = db
        self.registry = registry or build_default_validator_registry()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def validate(
        self, command: EvaluateCandidatePredicateCommand
    ) -> PredicateResultEvaluation:
        payload = _validator_command_payload(command)
        command_hash = canonical_json_hash(payload)
        existing = (
            self.db.query(DBValidationPredicateResult)
            .filter(
                DBValidationPredicateResult.validator_idempotency_key
                == payload["validator_idempotency_key"]
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_validator_command_hash != command_hash:
                raise CandidateEvidenceError(
                    "validation_predicate_result_conflict",
                    "validator idempotency key is bound to another command",
                )
            return PredicateResultEvaluation(existing, replayed=True)
        duplicate = (
            self.db.query(DBValidationPredicateResult)
            .filter(
                DBValidationPredicateResult.candidate_outcome_id
                == command.candidate_outcome_id,
                DBValidationPredicateResult.validation_specification_id
                == command.validation_specification_id,
                DBValidationPredicateResult.predicate_id == command.predicate_id,
                DBValidationPredicateResult.predicate_version
                == command.predicate_version,
            )
            .one_or_none()
        )
        if duplicate is not None:
            raise CandidateEvidenceError(
                "validation_predicate_result_conflict",
                "predicate already has a canonical result",
            )
        evidence = self.db.get(
            ExecutionTaskResolvedValidationEvidence, command.evidence_snapshot_id
        )
        specification = self.db.get(
            ExecutionTaskValidationSpecification, command.validation_specification_id
        )
        task = self.db.get(ExecutionTask, command.execution_task_id)
        outcome = self.db.get(ExecutionTaskAttemptOutcome, command.candidate_outcome_id)
        attempt = self.db.get(ExecutionTaskAttempt, command.execution_task_attempt_id)
        if any(
            item is None for item in (evidence, specification, task, outcome, attempt)
        ):
            raise CandidateEvidenceError(
                "validation_evidence_not_resolved", "validator authority is incomplete"
            )
        if task.status != "awaiting_validation":
            raise CandidateEvidenceError(
                "validation_primitive_integrity_failure",
                "task is not awaiting validation",
            )
        if (
            evidence.execution_task_id != task.id
            or evidence.validation_specification_id != specification.id
            or evidence.candidate_outcome_id != outcome.id
        ):
            raise CandidateEvidenceError(
                "validation_evidence_not_resolved",
                "evidence does not match validator command",
            )
        evidence_integrity = ValidationPrimitiveService(
            self.db, registry=self.registry
        ).verify_resolved_validation_evidence_integrity(evidence.id)
        if not evidence_integrity.verified:
            raise CandidateEvidenceError(
                "validation_evidence_invalid", "resolved evidence integrity failed"
            )
        if (
            specification.canonical_specification_hash
            != payload["validation_specification_hash"]
        ):
            raise CandidateEvidenceError(
                "validation_primitive_integrity_failure",
                "validation specification hash does not match",
            )
        try:
            contract = StructuredValidationContract.from_mapping(
                specification.canonical_payload["structured_contract"]
            )
            predicate = next(
                item
                for item in contract.predicates
                if item.predicate_id == command.predicate_id
                and item.predicate_version == command.predicate_version
            )
        except (KeyError, StopIteration, TypeError, ValidationContractError) as exc:
            raise CandidateEvidenceError(
                "validation_predicate_not_found",
                "predicate is not in frozen specification",
            ) from exc
        if (
            predicate.order != command.predicate_order
            or predicate.evidence_key != command.evidence_key
        ):
            raise CandidateEvidenceError(
                "validation_primitive_integrity_failure",
                "predicate identity does not match command",
            )
        context = ValidatorExecutionContext(
            validation_specification_id=specification.id,
            validation_specification_hash=specification.canonical_specification_hash,
            validator_set_id=contract.environment.validator_set_id,
            validator_set_version=contract.environment.validator_set_version,
            environment_configuration_hash=contract.environment.configuration_hash,
            resolver_version=contract.environment.resolver_version,
        )
        started = self._now()
        try:
            registration = self.registry.resolve(
                predicate,
                context,
                expected_validator_id=command.validator_id,
                expected_validator_version=command.validator_version,
            )
            gate = _evidence_gate(_snapshot_dto(evidence))
            evaluation = gate or registration.validator.validate(
                predicate, _snapshot_dto(evidence), context
            )
            if not isinstance(evaluation, CandidatePredicateResult):
                raise CandidateEvidenceError(
                    "validation_validator_error", "validator returned an invalid result"
                )
            evaluation = _predicate_result(
                evaluation.result_status,
                evaluation.result_code,
                diagnostics=evaluation.diagnostics,
                expected=evaluation.expected_summary,
                actual=evaluation.actual_summary,
            )
        except ValidatorRegistryError as exc:
            if exc.code in {
                "validation_validator_not_registered",
                "validation_predicate_unsupported",
                "validation_predicate_version_unsupported",
            }:
                evaluation = _predicate_result("unsupported", exc.code)
            else:
                raise
        except CandidateEvidenceError as exc:
            if exc.code == "validation_validator_error":
                evaluation = _predicate_result(
                    "validator_error", "validation_validator_error"
                )
            else:
                raise
        except Exception:
            evaluation = _predicate_result(
                "validator_error", "validation_validator_error"
            )
        completed = self._now()
        result_payload = {
            "schema_version": PREDICATE_RESULT_SCHEMA_VERSION,
            "execution_plan_id": task.execution_plan_id,
            "execution_task_id": task.id,
            "execution_task_attempt_id": attempt.id,
            "candidate_outcome_id": outcome.id,
            "validation_specification_id": specification.id,
            "validation_specification_hash": specification.canonical_specification_hash,
            "predicate_id": predicate.predicate_id,
            "predicate_version": predicate.predicate_version,
            "predicate_order": predicate.order,
            "evidence_snapshot_id": evidence.id,
            "evidence_key": predicate.evidence_key,
            "validator_id": command.validator_id,
            "validator_version": command.validator_version,
            "validator_set_id": context.validator_set_id,
            "validator_set_version": context.validator_set_version,
            "environment_configuration_hash": context.environment_configuration_hash,
            "result_status": evaluation.result_status,
            "passed": evaluation.passed,
            "result_code": evaluation.result_code,
            "diagnostics": _plain(evaluation.diagnostics),
            "expected_summary": _plain(evaluation.expected_summary),
            "actual_summary": _plain(evaluation.actual_summary),
        }
        result_hash = canonical_json_hash(result_payload)
        row = DBValidationPredicateResult(
            execution_plan_id=task.execution_plan_id,
            execution_task_id=task.id,
            execution_task_attempt_id=attempt.id,
            candidate_outcome_id=outcome.id,
            validation_specification_id=specification.id,
            validation_specification_hash=specification.canonical_specification_hash,
            predicate_id=predicate.predicate_id,
            predicate_version=predicate.predicate_version,
            predicate_order=predicate.order,
            evidence_snapshot_id=evidence.id,
            evidence_key=predicate.evidence_key,
            validator_id=command.validator_id,
            validator_version=command.validator_version,
            validator_set_id=context.validator_set_id,
            validator_set_version=context.validator_set_version,
            environment_configuration_hash=context.environment_configuration_hash,
            result_status=evaluation.result_status,
            passed=evaluation.passed,
            result_code=evaluation.result_code,
            diagnostics=_plain(evaluation.diagnostics),
            expected_summary=_plain(evaluation.expected_summary),
            actual_summary=_plain(evaluation.actual_summary),
            canonical_result_payload=result_payload,
            canonical_result_hash=result_hash,
            validator_idempotency_key=payload["validator_idempotency_key"],
            deterministic_validator_command_id=payload[
                "deterministic_validator_command_id"
            ],
            canonical_validator_command_payload=payload,
            canonical_validator_command_hash=command_hash,
            started_at=started,
            completed_at=completed,
            creation_actor_type=payload["creation_actor_type"],
            creation_actor_id=payload["creation_actor_id"],
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(DBValidationPredicateResult)
                .filter(
                    DBValidationPredicateResult.validator_idempotency_key
                    == payload["validator_idempotency_key"]
                )
                .one_or_none()
            )
            if (
                replay is not None
                and replay.canonical_validator_command_hash == command_hash
            ):
                return PredicateResultEvaluation(replay, replayed=True)
            raise CandidateEvidenceError(
                "validation_predicate_result_conflict",
                "validator result conflicts with canonical result",
            ) from exc
        return PredicateResultEvaluation(row)

    evaluate = validate


def verify_validation_predicate_result_integrity(
    db: Session,
    result_id: int,
    *,
    registry: DeterministicValidatorRegistry | None = None,
) -> ValidationPrimitiveIntegrityResult:
    return ValidationPrimitiveService(
        db, registry=registry
    ).verify_validation_predicate_result_integrity(result_id)


def verify_execution_task_validation_primitives_integrity(
    db: Session,
    execution_task_id: int,
    *,
    registry: DeterministicValidatorRegistry | None = None,
) -> ValidationPrimitiveIntegrityResult:
    return ValidationPrimitiveService(
        db, registry=registry
    ).verify_execution_task_validation_primitives_integrity(execution_task_id)


def verify_execution_plan_validation_primitives_integrity(
    db: Session,
    execution_plan_id: int,
    *,
    registry: DeterministicValidatorRegistry | None = None,
) -> ValidationPrimitiveIntegrityResult:
    return ValidationPrimitiveService(
        db, registry=registry
    ).verify_execution_plan_validation_primitives_integrity(execution_plan_id)


__all__ = [
    "CandidateEvidenceError",
    "CandidateEvidenceResolver",
    "CandidateEvidenceResolverService",
    "CandidateOutcomeAuthority",
    "CandidatePredicateResult",
    "DeterministicCandidateValidator",
    "DeterministicValidatorRegistry",
    "DeterministicValidatorService",
    "EvaluateCandidatePredicateCommand",
    "EvidenceInspectionProjection",
    "EvidenceReference",
    "ImmutableCandidateEvidenceSource",
    "PredicateInspectionProjection",
    "PredicateResultEvaluation",
    "ResolveCandidateEvidenceCommand",
    "ResolvedCandidateEvidence",
    "ResolvedEvidenceResult",
    "SqlCandidateOutcomeSource",
    "ValidationPrimitiveIntegrityResult",
    "ValidationPrimitiveService",
    "ValidationPrimitivesInspection",
    "ValidatorExecutionContext",
    "ValidatorRegistryError",
    "build_default_validator_registry",
    "normalize_evidence_reference",
    "parse_evidence_reference",
    "verify_execution_plan_validation_primitives_integrity",
    "verify_execution_task_validation_primitives_integrity",
    "verify_resolved_validation_evidence_integrity",
    "verify_validation_predicate_result_integrity",
]
