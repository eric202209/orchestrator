"""Immutable, execution-independent validation contract vocabulary.

This module defines the authored contract that is allowed to cross the
Planning -> Execution boundary.  It deliberately contains no resolver,
validator, filesystem, subprocess, model, or lifecycle behavior.  The
execution side persists a canonical projection of these values and evaluates
the projection only in a later phase.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import unicodedata
from typing import Any


VALIDATION_CONTRACT_SCHEMA_VERSION = "execution-task-validation-contract/1.0"
VALIDATION_CANONICALIZATION_VERSION = "execution-task-validation-canonical/1"
VALIDATION_HASH_ALGORITHM = "sha256"
VALIDATION_ENVIRONMENT_SCHEMA_VERSION = "validation-environment/1"
VALIDATION_RESOLVER_VERSION = "candidate-evidence-resolver/1"

CONTRACT_STATUSES = frozenset({"structured_executable", "validation_not_required"})
RELEASE_CONTRACT_STATUSES = frozenset(
    {
        "structured_executable",
        "legacy_unstructured",
        "validation_not_required",
        "unsupported",
    }
)
REVIEW_REQUIREMENTS = frozenset(
    {"none", "operator_required", "policy_required", "unsupported"}
)
EVIDENCE_TYPES = frozenset(
    {
        "candidate_output_reference",
        "candidate_output_hash",
        "candidate_content",
        "immutable_artifact",
        "command_result",
        "test_result",
        "schema_document",
        "review_decision",
    }
)
EVIDENCE_SOURCES = frozenset(
    {
        "candidate_outcome",
        "candidate_content",
        "release_contract",
        "review_authority",
    }
)
HASH_ALGORITHMS = frozenset({"sha256"})
PASS_POLICIES = frozenset({"all_required", "all_predicates", "any_required_group"})
PREDICATE_VERSIONS = {
    "required_output_exists": frozenset({1}),
    "output_reference_exists": frozenset({1}),
    "output_hash_matches": frozenset({1}),
    "content_exists": frozenset({1}),
    "content_hash_matches": frozenset({1}),
    "content_size_within_limit": frozenset({1}),
    "media_type_matches": frozenset({1}),
    "artifact_exists": frozenset({1}),
    "artifact_hash_matches": frozenset({1}),
    "json_schema_matches": frozenset({1}),
    "required_fields_present": frozenset({1}),
    "command_exit_code_equals": frozenset({1}),
    "test_suite_result_passed": frozenset({1}),
    "all_of": frozenset({1}),
    "any_of": frozenset({1}),
}

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}(?:/[0-9]+)?$")
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$"
)
_FORBIDDEN_PARAMETER_KEYS = frozenset(
    {
        "code",
        "command",
        "expression",
        "filesystem_path",
        "llm",
        "model",
        "prompt",
        "python",
        "regex",
        "shell",
        "sql",
        "url",
    }
)


class ValidationContractError(ValueError):
    """Bounded contract error suitable for release-boundary mapping."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _text(value: Any, field_name: str, *, max_length: int = 255) -> str:
    if not isinstance(value, str):
        raise ValidationContractError(
            "validation_contract_parameters_invalid", f"{field_name} is invalid"
        )
    normalized = unicodedata.normalize("NFC", value)
    if not normalized.strip() or len(normalized) > max_length:
        raise ValidationContractError(
            "validation_contract_parameters_invalid", f"{field_name} is invalid"
        )
    if any(ord(char) < 32 and char not in "\t\n\r" for char in normalized):
        raise ValidationContractError(
            "validation_contract_parameters_invalid", f"{field_name} is invalid"
        )
    return normalized


def _identifier(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name, max_length=64)
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValidationContractError(
            "validation_contract_parameters_invalid", f"{field_name} is invalid"
        )
    return normalized


def _canonical_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise ValidationContractError(
            "validation_contract_parameters_invalid", "parameters are too deeply nested"
        )
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        if len(value) > 32:
            raise ValidationContractError(
                "validation_contract_parameters_invalid", "parameters are too large"
            )
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _text(key, "parameter key", max_length=64)
            if normalized_key.casefold() in _FORBIDDEN_PARAMETER_KEYS:
                raise ValidationContractError(
                    "validation_contract_parameters_invalid",
                    "unrestricted validator parameters are not permitted",
                )
            if normalized_key in result:
                raise ValidationContractError(
                    "validation_contract_parameters_invalid", "duplicate parameter key"
                )
            result[normalized_key] = _canonical_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 32:
            raise ValidationContractError(
                "validation_contract_parameters_invalid", "parameters are too large"
            )
        return [_canonical_value(item, depth=depth + 1) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValidationContractError(
        "validation_contract_parameters_invalid", "parameters must be JSON values"
    )


def canonical_validation_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _canonical_value(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValidationContractError(
            "validation_contract_parameters_invalid", "contract is not canonical JSON"
        ) from exc


def canonical_validation_hash(value: Any) -> str:
    return hashlib.sha256(canonical_validation_json_bytes(value)).hexdigest()


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationContractError(
            "validation_contract_parameters_invalid", f"{field_name} is invalid"
        )
    return value


def _validate_exact_keys(
    value: Mapping[str, Any], allowed: set[str], field_name: str
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValidationContractError(
            "validation_contract_parameters_invalid", f"{field_name} is invalid"
        )


def _validate_predicate_parameters(
    predicate_id: str, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    canonical = _canonical_value(parameters)
    if not isinstance(canonical, dict):
        raise ValidationContractError(
            "validation_contract_parameters_invalid", "predicate parameters are invalid"
        )
    if predicate_id in {
        "required_output_exists",
        "output_reference_exists",
        "output_hash_matches",
        "content_exists",
        "content_hash_matches",
        "content_size_within_limit",
        "media_type_matches",
        "artifact_exists",
        "artifact_hash_matches",
        "test_suite_result_passed",
    }:
        _validate_exact_keys(canonical, set(), "predicate parameters")
    elif predicate_id == "json_schema_matches":
        _validate_exact_keys(canonical, {"schema_evidence_key"}, "predicate parameters")
        schema_key = canonical.get("schema_evidence_key")
        if not isinstance(schema_key, str) or not _FIELD_RE.fullmatch(schema_key):
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "schema evidence key is invalid",
            )
    elif predicate_id == "required_fields_present":
        _validate_exact_keys(canonical, {"fields"}, "predicate parameters")
        fields_value = canonical.get("fields")
        if (
            not isinstance(fields_value, list)
            or not fields_value
            or len(fields_value) > 32
            or any(
                not isinstance(item, str) or not _FIELD_RE.fullmatch(item)
                for item in fields_value
            )
            or len(set(fields_value)) != len(fields_value)
        ):
            raise ValidationContractError(
                "validation_contract_parameters_invalid", "required fields are invalid"
            )
    elif predicate_id == "command_exit_code_equals":
        _validate_exact_keys(canonical, {"exit_code"}, "predicate parameters")
        exit_code = canonical.get("exit_code")
        if (
            isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or not (-255 <= exit_code <= 255)
        ):
            raise ValidationContractError(
                "validation_contract_parameters_invalid", "command exit code is invalid"
            )
    elif predicate_id in {"all_of", "any_of"}:
        _validate_exact_keys(canonical, {"predicate_ids"}, "predicate parameters")
        references = canonical.get("predicate_ids")
        if (
            not isinstance(references, list)
            or not references
            or len(references) > 16
            or any(
                not isinstance(item, str) or not _IDENTIFIER_RE.fullmatch(item)
                for item in references
            )
            or len(set(references)) != len(references)
        ):
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "composite predicate references are invalid",
            )
    else:
        raise ValidationContractError(
            "validation_predicate_unsupported", "predicate is not supported"
        )
    return canonical


@dataclass(frozen=True)
class ValidationPredicate:
    predicate_id: str
    predicate_version: int = 1
    evidence_key: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    required: bool = True
    order: int = 1

    def __post_init__(self) -> None:
        predicate_id = _identifier(self.predicate_id, "predicate_id")
        if predicate_id not in PREDICATE_VERSIONS:
            raise ValidationContractError(
                "validation_predicate_unsupported", "predicate is not supported"
            )
        if isinstance(self.predicate_version, bool) or not isinstance(
            self.predicate_version, int
        ):
            raise ValidationContractError(
                "validation_predicate_version_unsupported",
                "predicate version is not supported",
            )
        if self.predicate_version not in PREDICATE_VERSIONS[predicate_id]:
            raise ValidationContractError(
                "validation_predicate_version_unsupported",
                "predicate version is not supported",
            )
        if (
            isinstance(self.order, bool)
            or not isinstance(self.order, int)
            or not (1 <= self.order <= 1_000_000)
        ):
            raise ValidationContractError(
                "validation_contract_parameters_invalid", "predicate order is invalid"
            )
        evidence_key = self.evidence_key
        if predicate_id in {"all_of", "any_of"}:
            if evidence_key not in {"", None}:
                evidence_key = _identifier(evidence_key, "evidence_key")
        else:
            evidence_key = _identifier(evidence_key, "evidence_key")
        if not isinstance(self.required, bool):
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "predicate required flag is invalid",
            )
        object.__setattr__(self, "predicate_id", predicate_id)
        object.__setattr__(self, "evidence_key", evidence_key or "")
        object.__setattr__(
            self,
            "parameters",
            _validate_predicate_parameters(
                predicate_id, _mapping(self.parameters, "parameters")
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ValidationPredicate":
        value = _mapping(value, "predicate")
        required = {
            "predicate_id",
            "predicate_version",
            "evidence_key",
            "parameters",
            "required",
            "order",
        }
        if set(value) != required:
            raise ValidationContractError(
                "validation_contract_parameters_invalid", "predicate shape is invalid"
            )
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise ValidationContractError(
                "validation_contract_parameters_invalid", "predicate shape is invalid"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "predicate_id": self.predicate_id,
            "predicate_version": self.predicate_version,
            "evidence_key": self.evidence_key,
            "parameters": dict(self.parameters),
            "required": self.required,
            "order": self.order,
        }


@dataclass(frozen=True)
class ValidationEvidenceDescriptor:
    evidence_key: str
    evidence_type: str
    source: str = "candidate_outcome"
    required: bool = True
    expected_media_type: str | None = None
    expected_hash_algorithm: str | None = None
    resolver_version: str = VALIDATION_RESOLVER_VERSION

    def __post_init__(self) -> None:
        key = _identifier(self.evidence_key, "evidence_key")
        evidence_type = _text(self.evidence_type, "evidence_type", max_length=64)
        source = _text(self.source, "evidence source", max_length=64)
        if evidence_type not in EVIDENCE_TYPES:
            raise ValidationContractError(
                "validation_evidence_descriptor_invalid", "evidence type is invalid"
            )
        if source not in EVIDENCE_SOURCES:
            raise ValidationContractError(
                "validation_evidence_descriptor_invalid", "evidence source is invalid"
            )
        if not isinstance(self.required, bool):
            raise ValidationContractError(
                "validation_evidence_descriptor_invalid",
                "evidence required flag is invalid",
            )
        media_type = self.expected_media_type
        if media_type is not None:
            media_type = _text(media_type, "expected_media_type", max_length=128)
            if not _MEDIA_TYPE_RE.fullmatch(media_type):
                raise ValidationContractError(
                    "validation_evidence_descriptor_invalid", "media type is invalid"
                )
        hash_algorithm = self.expected_hash_algorithm
        if hash_algorithm is not None:
            hash_algorithm = _text(
                hash_algorithm, "expected_hash_algorithm", max_length=32
            )
            if hash_algorithm not in HASH_ALGORITHMS:
                raise ValidationContractError(
                    "validation_evidence_descriptor_invalid",
                    "hash algorithm is invalid",
                )
        resolver_version = _text(
            self.resolver_version, "resolver_version", max_length=64
        )
        if not _VERSION_RE.fullmatch(resolver_version):
            raise ValidationContractError(
                "validation_evidence_descriptor_invalid", "resolver version is invalid"
            )
        object.__setattr__(self, "evidence_key", key)
        object.__setattr__(self, "evidence_type", evidence_type)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "expected_media_type", media_type)
        object.__setattr__(self, "expected_hash_algorithm", hash_algorithm)
        object.__setattr__(self, "resolver_version", resolver_version)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ValidationEvidenceDescriptor":
        value = _mapping(value, "evidence descriptor")
        allowed = {
            "evidence_key",
            "evidence_type",
            "source",
            "required",
            "expected_media_type",
            "expected_hash_algorithm",
            "resolver_version",
        }
        _validate_exact_keys(value, allowed, "evidence descriptor")
        if not {"evidence_key", "evidence_type"}.issubset(value):
            raise ValidationContractError(
                "validation_evidence_descriptor_invalid",
                "evidence descriptor is invalid",
            )
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise ValidationContractError(
                "validation_evidence_descriptor_invalid",
                "evidence descriptor is invalid",
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_key": self.evidence_key,
            "evidence_type": self.evidence_type,
            "source": self.source,
            "required": self.required,
            "expected_media_type": self.expected_media_type,
            "expected_hash_algorithm": self.expected_hash_algorithm,
            "resolver_version": self.resolver_version,
        }


@dataclass(frozen=True)
class ValidationPassPolicy:
    policy_id: str
    policy_version: int = 1
    optional_predicate_behavior: str = "ignore"
    missing_evidence: str = "fail"
    validator_error: str = "fail"
    short_circuit: bool = False
    review_separate_requirement: bool = True

    def __post_init__(self) -> None:
        policy_id = _identifier(self.policy_id, "pass_policy.policy_id")
        if policy_id not in PASS_POLICIES:
            raise ValidationContractError(
                "validation_pass_policy_invalid", "pass policy is invalid"
            )
        if isinstance(self.policy_version, bool) or self.policy_version != 1:
            raise ValidationContractError(
                "validation_pass_policy_invalid", "pass policy version is invalid"
            )
        expected = {
            "all_required": ("ignore", False),
            "all_predicates": ("require", False),
            "any_required_group": ("ignore", True),
        }[policy_id]
        if (
            self.optional_predicate_behavior != expected[0]
            or self.short_circuit != expected[1]
        ):
            raise ValidationContractError(
                "validation_pass_policy_invalid", "pass policy semantics are invalid"
            )
        if self.missing_evidence != "fail" or self.validator_error != "fail":
            raise ValidationContractError(
                "validation_pass_policy_invalid",
                "missing evidence and validator errors must fail closed",
            )
        if self.review_separate_requirement is not True:
            raise ValidationContractError(
                "validation_pass_policy_invalid", "review must remain separate"
            )
        object.__setattr__(self, "policy_id", policy_id)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ValidationPassPolicy":
        value = _mapping(value, "pass_policy")
        required = {
            "policy_id",
            "policy_version",
            "optional_predicate_behavior",
            "missing_evidence",
            "validator_error",
            "short_circuit",
            "review_separate_requirement",
        }
        if set(value) != required:
            raise ValidationContractError(
                "validation_pass_policy_invalid", "pass policy is invalid"
            )
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise ValidationContractError(
                "validation_pass_policy_invalid", "pass policy is invalid"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "optional_predicate_behavior": self.optional_predicate_behavior,
            "missing_evidence": self.missing_evidence,
            "validator_error": self.validator_error,
            "short_circuit": self.short_circuit,
            "review_separate_requirement": self.review_separate_requirement,
        }


@dataclass(frozen=True)
class ValidationReviewRequirement:
    requirement: str = "none"
    requirement_version: int = 1

    def __post_init__(self) -> None:
        requirement = _text(self.requirement, "review_requirement", max_length=32)
        if requirement not in REVIEW_REQUIREMENTS or self.requirement_version != 1:
            raise ValidationContractError(
                "validation_review_requirement_invalid",
                "review requirement is invalid",
            )
        object.__setattr__(self, "requirement", requirement)

    @classmethod
    def from_value(cls, value: Any) -> "ValidationReviewRequirement":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(requirement=value)
        value = _mapping(value, "review_requirement")
        if set(value) != {"requirement", "requirement_version"}:
            raise ValidationContractError(
                "validation_review_requirement_invalid",
                "review requirement is invalid",
            )
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise ValidationContractError(
                "validation_review_requirement_invalid",
                "review requirement is invalid",
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "requirement_version": self.requirement_version,
        }


@dataclass(frozen=True)
class ValidationEnvironmentIdentity:
    schema_version: str = VALIDATION_ENVIRONMENT_SCHEMA_VERSION
    validator_set_id: str = "deterministic_readonly"
    validator_set_version: str = "1"
    configuration_hash: str = (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    resolver_version: str = VALIDATION_RESOLVER_VERSION
    toolchain_identity: str = "unspecified"
    timezone: str = "UTC"
    locale: str = "C"

    def __post_init__(self) -> None:
        values = {
            name: _text(getattr(self, name), name, max_length=128)
            for name in (
                "schema_version",
                "validator_set_id",
                "validator_set_version",
                "resolver_version",
                "toolchain_identity",
                "timezone",
                "locale",
            )
        }
        if values["schema_version"] != VALIDATION_ENVIRONMENT_SCHEMA_VERSION:
            raise ValidationContractError(
                "validation_environment_identity_invalid",
                "environment schema version is unsupported",
            )
        if not _HASH_RE.fullmatch(self.configuration_hash):
            raise ValidationContractError(
                "validation_environment_identity_invalid",
                "configuration hash is invalid",
            )
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ValidationEnvironmentIdentity":
        value = _mapping(value, "environment")
        allowed = {
            "schema_version",
            "validator_set_id",
            "validator_set_version",
            "configuration_hash",
            "resolver_version",
            "toolchain_identity",
            "timezone",
            "locale",
        }
        _validate_exact_keys(value, allowed, "environment")
        required = {
            "schema_version",
            "validator_set_id",
            "validator_set_version",
            "configuration_hash",
        }
        if not required.issubset(value):
            raise ValidationContractError(
                "validation_environment_identity_invalid",
                "environment identity is invalid",
            )
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise ValidationContractError(
                "validation_environment_identity_invalid",
                "environment identity is invalid",
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "validator_set_id": self.validator_set_id,
            "validator_set_version": self.validator_set_version,
            "configuration_hash": self.configuration_hash,
            "resolver_version": self.resolver_version,
            "toolchain_identity": self.toolchain_identity,
            "timezone": self.timezone,
            "locale": self.locale,
        }


@dataclass(frozen=True)
class StructuredValidationContract:
    """Planning-authored executable contract, without task identity."""

    schema_version: str = VALIDATION_CONTRACT_SCHEMA_VERSION
    status: str = "structured_executable"
    predicates: tuple[ValidationPredicate, ...] = ()
    evidence_descriptors: tuple[ValidationEvidenceDescriptor, ...] = ()
    pass_policy: ValidationPassPolicy | None = None
    review_requirement: ValidationReviewRequirement | str = "none"
    environment: ValidationEnvironmentIdentity = field(
        default_factory=ValidationEnvironmentIdentity
    )
    specification_source: str = "authored"
    no_validation_reason: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != VALIDATION_CONTRACT_SCHEMA_VERSION:
            raise ValidationContractError(
                "validation_contract_schema_unsupported",
                "validation contract schema version is unsupported",
            )
        if self.status not in CONTRACT_STATUSES:
            raise ValidationContractError(
                "validation_contract_schema_unsupported",
                "validation contract status is invalid",
            )
        predicates = tuple(
            (
                item
                if isinstance(item, ValidationPredicate)
                else ValidationPredicate.from_mapping(item)
            )
            for item in self.predicates
        )
        descriptors = tuple(
            (
                item
                if isinstance(item, ValidationEvidenceDescriptor)
                else ValidationEvidenceDescriptor.from_mapping(item)
            )
            for item in self.evidence_descriptors
        )
        if self.status == "structured_executable":
            if not predicates or not descriptors or self.pass_policy is None:
                raise ValidationContractError(
                    "validation_contract_parameters_invalid",
                    "structured validation requires predicates, evidence, and pass policy",
                )
        elif predicates or descriptors or self.pass_policy is not None:
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "validation_not_required cannot contain executable predicates",
            )
        review = ValidationReviewRequirement.from_value(self.review_requirement)
        environment = (
            self.environment
            if isinstance(self.environment, ValidationEnvironmentIdentity)
            else ValidationEnvironmentIdentity.from_mapping(self.environment)
        )
        source = _text(self.specification_source, "specification_source", max_length=64)
        if source not in {"authored", "operator_authored"}:
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "validation contract source is invalid",
            )
        if self.status == "validation_not_required":
            reason = _text(
                self.no_validation_reason or "",
                "no_validation_reason",
                max_length=512,
            )
        else:
            reason = None

        predicate_ids = [item.predicate_id for item in predicates]
        if len(set(predicate_ids)) != len(predicate_ids):
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "predicate identifiers must be unique",
            )
        orders = [item.order for item in predicates]
        if len(set(orders)) != len(orders):
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "predicate order must be unique",
            )
        descriptor_keys = [item.evidence_key for item in descriptors]
        if len(set(descriptor_keys)) != len(descriptor_keys):
            raise ValidationContractError(
                "validation_evidence_descriptor_invalid", "evidence keys must be unique"
            )
        descriptors_by_key = {item.evidence_key: item for item in descriptors}
        for predicate in predicates:
            if predicate.predicate_id not in {"all_of", "any_of"}:
                descriptor = descriptors_by_key.get(predicate.evidence_key)
                if descriptor is None:
                    raise ValidationContractError(
                        "validation_evidence_descriptor_invalid",
                        "predicate evidence dependency is not described",
                    )
                if (
                    predicate.predicate_id
                    in {
                        "required_output_exists",
                        "output_reference_exists",
                    }
                    and descriptor.evidence_type != "candidate_output_reference"
                ):
                    raise ValidationContractError(
                        "validation_evidence_descriptor_invalid",
                        "output-reference predicate has the wrong evidence type",
                    )
                if predicate.predicate_id == "output_hash_matches":
                    if descriptor.evidence_type != "candidate_output_hash":
                        raise ValidationContractError(
                            "validation_evidence_descriptor_invalid",
                            "output-hash predicate has the wrong evidence type",
                        )
                    if descriptor.expected_hash_algorithm != VALIDATION_HASH_ALGORITHM:
                        raise ValidationContractError(
                            "validation_evidence_descriptor_invalid",
                            "output-hash predicate requires sha256 evidence",
                        )
                if predicate.predicate_id in {
                    "content_exists",
                    "content_hash_matches",
                    "content_size_within_limit",
                    "media_type_matches",
                }:
                    if (
                        descriptor.evidence_type != "candidate_content"
                        or descriptor.source != "candidate_content"
                    ):
                        raise ValidationContractError(
                            "validation_evidence_descriptor_invalid",
                            "content predicate requires candidate-content evidence",
                        )
                if predicate.predicate_id == "content_hash_matches":
                    if descriptor.expected_hash_algorithm != VALIDATION_HASH_ALGORITHM:
                        raise ValidationContractError(
                            "validation_evidence_descriptor_invalid",
                            "content-hash predicate requires sha256 evidence",
                        )
                if predicate.predicate_id == "media_type_matches":
                    if descriptor.expected_media_type is None:
                        raise ValidationContractError(
                            "validation_evidence_descriptor_invalid",
                            "media-type predicate requires an expected media type",
                        )
                if predicate.predicate_id in {"artifact_hash_matches"}:
                    if descriptor.evidence_type != "immutable_artifact":
                        raise ValidationContractError(
                            "validation_evidence_descriptor_invalid",
                            "artifact-hash predicate has the wrong evidence type",
                        )
                    if descriptor.expected_hash_algorithm != VALIDATION_HASH_ALGORITHM:
                        raise ValidationContractError(
                            "validation_evidence_descriptor_invalid",
                            "artifact-hash predicate requires sha256 evidence",
                        )
            else:
                references = predicate.parameters["predicate_ids"]
                if any(reference not in predicate_ids for reference in references):
                    raise ValidationContractError(
                        "validation_contract_parameters_invalid",
                        "composite predicate references an unknown predicate",
                    )
        if self.status == "structured_executable" and self.pass_policy is not None:
            policy = (
                self.pass_policy
                if isinstance(self.pass_policy, ValidationPassPolicy)
                else ValidationPassPolicy.from_mapping(self.pass_policy)
            )
        else:
            policy = None
        object.__setattr__(
            self, "predicates", tuple(sorted(predicates, key=lambda item: item.order))
        )
        object.__setattr__(
            self,
            "evidence_descriptors",
            tuple(sorted(descriptors, key=lambda item: item.evidence_key)),
        )
        object.__setattr__(self, "review_requirement", review)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "pass_policy", policy)
        object.__setattr__(self, "specification_source", source)
        object.__setattr__(self, "no_validation_reason", reason)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StructuredValidationContract":
        value = _mapping(value, "validation_contract")
        allowed = {
            "schema_version",
            "status",
            "predicates",
            "evidence_descriptors",
            "pass_policy",
            "review_requirement",
            "environment",
            "specification_source",
            "no_validation_reason",
        }
        _validate_exact_keys(value, allowed, "validation_contract")
        required = {
            "schema_version",
            "status",
            "predicates",
            "evidence_descriptors",
            "review_requirement",
            "environment",
            "specification_source",
        }
        if not required.issubset(value):
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "validation contract shape is invalid",
            )
        predicates = value["predicates"]
        descriptors = value["evidence_descriptors"]
        if not isinstance(predicates, Sequence) or isinstance(predicates, (str, bytes)):
            raise ValidationContractError(
                "validation_contract_parameters_invalid", "predicates must be an array"
            )
        if not isinstance(descriptors, Sequence) or isinstance(
            descriptors, (str, bytes)
        ):
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "evidence descriptors must be an array",
            )
        try:
            return cls(
                schema_version=value["schema_version"],
                status=value["status"],
                predicates=tuple(
                    ValidationPredicate.from_mapping(item) for item in predicates
                ),
                evidence_descriptors=tuple(
                    ValidationEvidenceDescriptor.from_mapping(item)
                    for item in descriptors
                ),
                pass_policy=(
                    None
                    if value.get("pass_policy") is None
                    else ValidationPassPolicy.from_mapping(value["pass_policy"])
                ),
                review_requirement=ValidationReviewRequirement.from_value(
                    value["review_requirement"]
                ),
                environment=ValidationEnvironmentIdentity.from_mapping(
                    value["environment"]
                ),
                specification_source=value["specification_source"],
                no_validation_reason=value.get("no_validation_reason"),
            )
        except ValidationContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "validation contract shape is invalid",
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "predicates": [item.to_dict() for item in self.predicates],
            "evidence_descriptors": [
                item.to_dict() for item in self.evidence_descriptors
            ],
            "pass_policy": self.pass_policy.to_dict() if self.pass_policy else None,
            "review_requirement": self.review_requirement.to_dict(),
            "environment": self.environment.to_dict(),
            "specification_source": self.specification_source,
            "no_validation_reason": self.no_validation_reason,
        }


@dataclass(frozen=True)
class TaskValidationContractProjection:
    contract_status: str
    original_done_when: tuple[str, ...]
    structured_contract: StructuredValidationContract | None
    canonical_payload: dict[str, Any]
    canonical_hash: str


def build_task_validation_contract(task: Any) -> TaskValidationContractProjection:
    """Build a task-level contract without interpreting prose.

    A missing contract on any WorkItem makes the whole task legacy.  This is
    intentionally conservative: structured predicates are never inferred from
    a sibling WorkItem's ``done_when`` text.
    """

    work_items = tuple(getattr(task, "work_items", ()) or ())
    original_done_when = tuple(str(item.done_when) for item in work_items)
    authored = tuple(getattr(item, "validation_contract", None) for item in work_items)
    if not work_items or any(item is None for item in authored):
        status = "legacy_unstructured"
        structured = None
    else:
        contracts = tuple(
            (
                item
                if isinstance(item, StructuredValidationContract)
                else StructuredValidationContract.from_mapping(item)
            )
            for item in authored
        )
        statuses = {item.status for item in contracts}
        if statuses == {"validation_not_required"}:
            status = "validation_not_required"
            structured = contracts[0]
            if any(item.to_dict() != structured.to_dict() for item in contracts[1:]):
                raise ValidationContractError(
                    "validation_contract_parameters_invalid",
                    "all no-validation WorkItems must use the same contract",
                )
        elif statuses == {"structured_executable"}:
            status = "structured_executable"
            first = contracts[0]
            predicates: list[ValidationPredicate] = []
            descriptors: list[ValidationEvidenceDescriptor] = []
            for contract in contracts:
                if (
                    contract.pass_policy.to_dict() != first.pass_policy.to_dict()
                    or contract.review_requirement.to_dict()
                    != first.review_requirement.to_dict()
                    or contract.environment.to_dict() != first.environment.to_dict()
                    or contract.specification_source != first.specification_source
                ):
                    raise ValidationContractError(
                        "validation_contract_parameters_invalid",
                        "WorkItem contracts must share policy, review, and environment identity",
                    )
                predicates.extend(contract.predicates)
                descriptors.extend(contract.evidence_descriptors)
            structured = StructuredValidationContract(
                schema_version=first.schema_version,
                status=status,
                predicates=tuple(predicates),
                evidence_descriptors=tuple(descriptors),
                pass_policy=first.pass_policy,
                review_requirement=first.review_requirement,
                environment=first.environment,
                specification_source=first.specification_source,
            )
        else:
            raise ValidationContractError(
                "validation_contract_parameters_invalid",
                "WorkItems cannot mix executable and no-validation contracts",
            )

    payload: dict[str, Any] = {
        "canonicalization_version": VALIDATION_CANONICALIZATION_VERSION,
        "schema_version": VALIDATION_CONTRACT_SCHEMA_VERSION,
        "contract_status": status,
        "original_done_when": list(original_done_when),
        "structured_contract": structured.to_dict() if structured else None,
    }
    return TaskValidationContractProjection(
        contract_status=status,
        original_done_when=original_done_when,
        structured_contract=structured,
        canonical_payload=payload,
        canonical_hash=canonical_validation_hash(payload),
    )


def legacy_validation_contract_payload(done_when: Any) -> tuple[dict[str, Any], str]:
    """Create only a compatibility hash for a pre-contract released task."""

    payload = {
        "canonicalization_version": VALIDATION_CANONICALIZATION_VERSION,
        "schema_version": VALIDATION_CONTRACT_SCHEMA_VERSION,
        "contract_status": "legacy_unstructured",
        "original_done_when": done_when,
        "structured_contract": None,
    }
    return payload, canonical_validation_hash(payload)


__all__ = [
    "CONTRACT_STATUSES",
    "EVIDENCE_TYPES",
    "PASS_POLICIES",
    "PREDICATE_VERSIONS",
    "RELEASE_CONTRACT_STATUSES",
    "StructuredValidationContract",
    "TaskValidationContractProjection",
    "ValidationContractError",
    "ValidationEnvironmentIdentity",
    "ValidationEvidenceDescriptor",
    "ValidationPassPolicy",
    "ValidationPredicate",
    "ValidationReviewRequirement",
    "VALIDATION_CANONICALIZATION_VERSION",
    "VALIDATION_CONTRACT_SCHEMA_VERSION",
    "VALIDATION_ENVIRONMENT_SCHEMA_VERSION",
    "VALIDATION_HASH_ALGORITHM",
    "VALIDATION_RESOLVER_VERSION",
    "build_task_validation_contract",
    "canonical_validation_hash",
    "canonical_validation_json_bytes",
    "legacy_validation_contract_payload",
]
