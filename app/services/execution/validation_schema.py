"""Immutable, bounded JSON Schema authority for Phase 29C-10.

Only the database row is authoritative in this phase.  The service never
loads a schema from a caller-supplied path, URL, provider, or executable
format.  Schemas are canonicalized before identity is assigned and are
validated with the pinned ``jsonschema`` Draft 2020-12 implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import re
import unicodedata
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ExecutionTaskValidationSpecification, ExecutionValidationSchema


VALIDATION_SCHEMA_SCHEMA_VERSION = "execution-validation-schema/1"
VALIDATION_SCHEMA_CANONICALIZATION_VERSION = "execution-validation-schema-canonical/1"
VALIDATION_SCHEMA_TYPE = "json_schema"
SUPPORTED_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_STORAGE_BACKEND_ID = "database-json"
SCHEMA_STORAGE_BACKEND_VERSION = "1"
VALIDATOR_IMPLEMENTATION_ID = "jsonschema-draft202012"
VALIDATOR_IMPLEMENTATION_VERSION = importlib.metadata.version("jsonschema")

MAX_SCHEMA_ENCODED_BYTES = 65_536
MAX_SCHEMA_DEPTH = 8
MAX_SCHEMA_OBJECT_MEMBERS = 256
MAX_SCHEMA_ARRAY_LENGTH = 256
MAX_SCHEMA_STRING_LENGTH = 4_096
MAX_SCHEMA_REFERENCE_COUNT = 16
MAX_SCHEMA_REGEX_LENGTH = 256
MAX_SCHEMA_TOTAL_REGEX_LENGTH = 2_048
MAX_SCHEMA_KEYWORDS = 256
MAX_SCHEMA_LOGICAL_NAME_LENGTH = 128
MAX_SCHEMA_LOGICAL_VERSION_LENGTH = 64
MAX_SCHEMA_IDEMPOTENCY_KEY_LENGTH = 128

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCHEMA_REFERENCE_RE = re.compile(r"^validation-schema://(sha256:[0-9a-f]{64})$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# This is deliberately a subset of Draft 2020-12.  In particular, URI
# identity/dynamic-reference and format/content execution features are not
# accepted at this boundary.
SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema",
        "$ref",
        "$defs",
        "type",
        "enum",
        "const",
        "multipleOf",
        "maximum",
        "exclusiveMaximum",
        "minimum",
        "exclusiveMinimum",
        "maxLength",
        "minLength",
        "pattern",
        "maxItems",
        "minItems",
        "uniqueItems",
        "contains",
        "maxContains",
        "minContains",
        "maxProperties",
        "minProperties",
        "required",
        "dependentRequired",
        "properties",
        "patternProperties",
        "additionalProperties",
        "propertyNames",
        "unevaluatedProperties",
        "items",
        "prefixItems",
        "unevaluatedItems",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "dependentSchemas",
        "title",
        "description",
        "default",
        "deprecated",
        "readOnly",
        "writeOnly",
        "examples",
        "$comment",
    }
)
_SCHEMA_MAP_KEYWORDS = frozenset(
    {"properties", "patternProperties", "$defs", "dependentSchemas"}
)
_SCHEMA_VALUE_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SCHEMA_ARRAY_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_REGEX_KEYWORDS = frozenset({"pattern", "patternProperties"})
_ANNOTATION_KEYWORDS = frozenset(
    {"default", "description", "examples", "title", "$comment"}
)


class ValidationSchemaError(RuntimeError):
    """Bounded schema-authority error suitable for API/service mapping."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass
class _SchemaBounds:
    depth: int = 0
    object_members: int = 0
    array_length: int = 0
    max_string_length: int = 0
    references: int = 0
    regex_length: int = 0
    keyword_count: int = 0


@dataclass(frozen=True)
class ValidationSchemaInspection:
    schema_id: str
    schema_sha256: str
    schema_type: str
    dialect: str
    encoded_size: int
    depth: int
    object_members: int
    array_length: int
    max_string_length: int
    reference_count: int
    regex_length: int
    referenced_by_count: int
    integrity_verified: bool
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "schema_sha256": self.schema_sha256,
            "schema_type": self.schema_type,
            "dialect": self.dialect,
            "encoded_size": self.encoded_size,
            "depth": self.depth,
            "object_members": self.object_members,
            "array_length": self.array_length,
            "max_string_length": self.max_string_length,
            "reference_count": self.reference_count,
            "regex_length": self.regex_length,
            "referenced_by_count": self.referenced_by_count,
            "integrity_verified": self.integrity_verified,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class ValidationSchemaIntegrityResult:
    schema_id: str | None
    verified: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class CreateValidationSchemaCommand:
    schema: Mapping[str, Any]
    idempotency_key: str
    logical_name: str | None = None
    logical_version: str | None = None
    creation_actor_type: str = "operator"
    creation_actor_id: str = "system"


@dataclass(frozen=True)
class ValidationSchemaCreationResult:
    schema: ExecutionValidationSchema
    replayed: bool = False


def _bounded_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValidationSchemaError(
            "validation_schema_metadata_invalid", f"{field} is invalid"
        )
    result = unicodedata.normalize("NFC", value)
    if not result or len(result) > limit or _CONTROL_RE.search(result):
        raise ValidationSchemaError(
            "validation_schema_metadata_invalid", f"{field} is invalid"
        )
    return result


def _optional_text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, limit)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValidationSchemaError(
            "validation_schema_payload_invalid", "schema is not canonical JSON"
        ) from exc


def _schema_hash(canonical_bytes: bytes) -> str:
    return f"sha256:{hashlib.sha256(canonical_bytes).hexdigest()}"


def _canonical_value(
    value: Any,
    *,
    depth: int,
    bounds: _SchemaBounds,
    schema_node: bool,
    schema_map: bool = False,
) -> Any:
    if depth > MAX_SCHEMA_DEPTH:
        raise ValidationSchemaError(
            "validation_schema_bound_exceeded", "schema nesting depth exceeds the bound"
        )
    bounds.depth = max(bounds.depth, depth)
    if isinstance(value, Mapping):
        bounds.object_members += len(value)
        if bounds.object_members > MAX_SCHEMA_OBJECT_MEMBERS:
            raise ValidationSchemaError(
                "validation_schema_bound_exceeded",
                "schema object members exceed the bound",
            )
        result: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            if not isinstance(raw_key, str):
                raise ValidationSchemaError(
                    "validation_schema_payload_invalid",
                    "schema object keys must be strings",
                )
            key = unicodedata.normalize("NFC", raw_key)
            if (
                not key
                or len(key) > MAX_SCHEMA_STRING_LENGTH
                or _CONTROL_RE.search(key)
            ):
                raise ValidationSchemaError(
                    "validation_schema_bound_exceeded",
                    "schema string length is invalid",
                )
            bounds.max_string_length = max(bounds.max_string_length, len(key))
            if key in result:
                raise ValidationSchemaError(
                    "validation_schema_payload_invalid",
                    "schema contains duplicate keys",
                )
            if schema_node and not schema_map and key not in SUPPORTED_SCHEMA_KEYWORDS:
                raise ValidationSchemaError(
                    "validation_schema_keyword_unsupported",
                    "schema keyword is unsupported",
                )
            if schema_node and key in _REGEX_KEYWORDS and key == "pattern":
                _record_regex(raw_item, bounds)
            if schema_node and key == "$ref":
                _record_reference(raw_item, bounds)
            child_schema = schema_map
            child_schema_map = False
            if schema_node and key in _SCHEMA_MAP_KEYWORDS:
                child_schema_map = True
            elif schema_node and key in _SCHEMA_VALUE_KEYWORDS:
                child_schema = True
            elif schema_node and key in _SCHEMA_ARRAY_KEYWORDS:
                child_schema = True
            result[key] = _canonical_value(
                raw_item,
                depth=depth + 1,
                bounds=bounds,
                schema_node=child_schema,
                schema_map=child_schema_map,
            )
        bounds.keyword_count += len(value) if schema_node else 0
        if bounds.keyword_count > MAX_SCHEMA_KEYWORDS:
            raise ValidationSchemaError(
                "validation_schema_bound_exceeded",
                "schema keyword count exceeds the bound",
            )
        return result
    if isinstance(value, (list, tuple)):
        bounds.array_length += len(value)
        if (
            len(value) > MAX_SCHEMA_ARRAY_LENGTH
            or bounds.array_length > MAX_SCHEMA_ARRAY_LENGTH * 4
        ):
            raise ValidationSchemaError(
                "validation_schema_bound_exceeded",
                "schema array length exceeds the bound",
            )
        return [
            _canonical_value(
                item,
                depth=depth + 1,
                bounds=bounds,
                schema_node=schema_node,
                schema_map=False,
            )
            for item in value
        ]
    if isinstance(value, str):
        result = unicodedata.normalize("NFC", value)
        if len(result) > MAX_SCHEMA_STRING_LENGTH or _CONTROL_RE.search(result):
            raise ValidationSchemaError(
                "validation_schema_bound_exceeded",
                "schema string length exceeds the bound",
            )
        bounds.max_string_length = max(bounds.max_string_length, len(result))
        return result
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValidationSchemaError(
        "validation_schema_payload_invalid", "schema contains a non-JSON value"
    )


def _record_reference(value: Any, bounds: _SchemaBounds) -> None:
    if not isinstance(value, str) or not value.startswith("#") or "://" in value:
        raise ValidationSchemaError(
            "validation_schema_external_reference",
            "only local JSON Pointer references are supported",
        )
    if value != "#" and not value.startswith("#/"):
        raise ValidationSchemaError(
            "validation_schema_reference_invalid",
            "schema reference must be a local JSON Pointer",
        )
    bounds.references += 1
    if bounds.references > MAX_SCHEMA_REFERENCE_COUNT:
        raise ValidationSchemaError(
            "validation_schema_bound_exceeded",
            "schema reference count exceeds the bound",
        )


def _record_regex(value: Any, bounds: _SchemaBounds) -> None:
    if not isinstance(value, str) or len(value) > MAX_SCHEMA_REGEX_LENGTH:
        raise ValidationSchemaError(
            "validation_schema_bound_exceeded", "schema regex length exceeds the bound"
        )
    try:
        re.compile(value)
    except re.error as exc:
        raise ValidationSchemaError(
            "validation_schema_regex_invalid", "schema regex is invalid"
        ) from exc
    bounds.regex_length += len(value)
    if bounds.regex_length > MAX_SCHEMA_TOTAL_REGEX_LENGTH:
        raise ValidationSchemaError(
            "validation_schema_bound_exceeded",
            "schema regex total length exceeds the bound",
        )


def _validate_regex_keywords(value: Any, bounds: _SchemaBounds) -> None:
    """Visit patternProperties keys after generic canonicalization."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "patternProperties" and isinstance(item, Mapping):
                for pattern in item:
                    _record_regex(pattern, bounds)
            _validate_regex_keywords(item, bounds)
    elif isinstance(value, list):
        for item in value:
            _validate_regex_keywords(item, bounds)


def canonicalize_validation_schema(
    schema: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes, str, dict[str, int]]:
    if not isinstance(schema, Mapping):
        raise ValidationSchemaError(
            "validation_schema_payload_invalid", "schema must be an object"
        )
    bounds = _SchemaBounds()
    canonical = _canonical_value(schema, depth=0, bounds=bounds, schema_node=True)
    if canonical.get("$schema") != SUPPORTED_SCHEMA_DIALECT:
        raise ValidationSchemaError(
            "validation_schema_dialect_unsupported", "only Draft 2020-12 is supported"
        )
    _validate_regex_keywords(canonical, bounds)
    encoded = _canonical_json_bytes(canonical)
    if len(encoded) > MAX_SCHEMA_ENCODED_BYTES:
        raise ValidationSchemaError(
            "validation_schema_bound_exceeded",
            "canonical schema size exceeds the bound",
        )
    try:
        Draft202012Validator.check_schema(canonical)
    except SchemaError as exc:
        raise ValidationSchemaError(
            "validation_schema_payload_invalid",
            "schema is invalid for the supported dialect",
        ) from exc
    return (
        canonical,
        encoded,
        _schema_hash(encoded),
        {
            "encoded_size": len(encoded),
            "depth": bounds.depth,
            "object_members": bounds.object_members,
            "array_length": bounds.array_length,
            "max_string_length": bounds.max_string_length,
            "reference_count": bounds.references,
            "regex_length": bounds.regex_length,
        },
    )


def schema_reference_for_id(schema_id: str) -> str:
    if not _SCHEMA_ID_RE.fullmatch(schema_id):
        raise ValidationSchemaError(
            "validation_schema_reference_invalid", "schema id is invalid"
        )
    return f"validation-schema://{schema_id}"


def parse_schema_reference(reference: str) -> str:
    if not isinstance(reference, str):
        raise ValidationSchemaError(
            "validation_schema_reference_invalid", "schema reference is invalid"
        )
    match = _SCHEMA_REFERENCE_RE.fullmatch(unicodedata.normalize("NFC", reference))
    if match is None:
        raise ValidationSchemaError(
            "validation_schema_reference_invalid", "schema reference is invalid"
        )
    return match.group(1)


def _metadata_payload(
    *,
    schema_id: str,
    schema_size: int,
    bounds: Mapping[str, int],
    dialect: str,
    logical_name: str | None,
    logical_version: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": VALIDATION_SCHEMA_SCHEMA_VERSION,
        "canonicalization_version": VALIDATION_SCHEMA_CANONICALIZATION_VERSION,
        "schema_id": schema_id,
        "schema_sha256": schema_id,
        "schema_type": VALIDATION_SCHEMA_TYPE,
        "dialect": dialect,
        "storage_backend_id": SCHEMA_STORAGE_BACKEND_ID,
        "storage_backend_version": SCHEMA_STORAGE_BACKEND_VERSION,
        "logical_name": logical_name,
        "logical_version": logical_version,
        **dict(bounds),
        "schema_size_bytes": schema_size,
    }


class ExecutionValidationSchemaService:
    """Create, resolve, inspect, verify, and explicitly retain schema rows."""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self, command: CreateValidationSchemaCommand
    ) -> ValidationSchemaCreationResult:
        idempotency_key = _bounded_text(
            command.idempotency_key,
            "idempotency_key",
            MAX_SCHEMA_IDEMPOTENCY_KEY_LENGTH,
        )
        logical_name = _optional_text(
            command.logical_name, "logical_name", MAX_SCHEMA_LOGICAL_NAME_LENGTH
        )
        logical_version = _optional_text(
            command.logical_version,
            "logical_version",
            MAX_SCHEMA_LOGICAL_VERSION_LENGTH,
        )
        canonical, encoded, schema_id, bounds = canonicalize_validation_schema(
            command.schema
        )
        metadata = _metadata_payload(
            schema_id=schema_id,
            schema_size=len(encoded),
            bounds={
                key: value for key, value in bounds.items() if key != "encoded_size"
            },
            dialect=SUPPORTED_SCHEMA_DIALECT,
            logical_name=logical_name,
            logical_version=logical_version,
        )
        command_payload = {
            "schema_version": VALIDATION_SCHEMA_SCHEMA_VERSION,
            "schema_id": schema_id,
            "schema_sha256": schema_id,
            "schema_type": VALIDATION_SCHEMA_TYPE,
            "dialect": SUPPORTED_SCHEMA_DIALECT,
            "logical_name": logical_name,
            "logical_version": logical_version,
            "idempotency_key": idempotency_key,
            "creation_actor_type": _bounded_text(
                command.creation_actor_type, "creation_actor_type", 64
            ),
            "creation_actor_id": _bounded_text(
                command.creation_actor_id, "creation_actor_id", 255
            ),
        }
        command_hash = hashlib.sha256(
            _canonical_json_bytes(command_payload)
        ).hexdigest()
        existing = (
            self.db.query(ExecutionValidationSchema)
            .filter(ExecutionValidationSchema.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_command_hash != command_hash:
                raise ValidationSchemaError(
                    "validation_schema_idempotency_conflict",
                    "idempotency key is bound to different schema content",
                )
            integrity = self.verify_integrity(existing.id)
            if not integrity.verified:
                raise ValidationSchemaError(
                    "validation_schema_integrity_failure",
                    "replayed schema failed integrity verification",
                )
            return ValidationSchemaCreationResult(existing, replayed=True)

        existing = (
            self.db.query(ExecutionValidationSchema)
            .filter(ExecutionValidationSchema.schema_id == schema_id)
            .one_or_none()
        )
        if existing is not None:
            if (
                existing.canonical_schema_payload != canonical
                or existing.canonical_metadata_hash
                != hashlib.sha256(_canonical_json_bytes(metadata)).hexdigest()
            ):
                raise ValidationSchemaError(
                    "validation_schema_identity_conflict",
                    "schema identity is already bound to different authority metadata",
                )
            return ValidationSchemaCreationResult(existing, replayed=True)

        row = ExecutionValidationSchema(
            schema_id=schema_id,
            schema_type=VALIDATION_SCHEMA_TYPE,
            schema_version=VALIDATION_SCHEMA_SCHEMA_VERSION,
            dialect=SUPPORTED_SCHEMA_DIALECT,
            canonical_schema_payload=canonical,
            schema_sha256=schema_id,
            schema_size_bytes=len(encoded),
            schema_depth=bounds["depth"],
            schema_object_members=bounds["object_members"],
            schema_array_length=bounds["array_length"],
            schema_max_string_length=bounds["max_string_length"],
            schema_reference_count=bounds["reference_count"],
            schema_regex_length=bounds["regex_length"],
            storage_backend_id=SCHEMA_STORAGE_BACKEND_ID,
            storage_backend_version=SCHEMA_STORAGE_BACKEND_VERSION,
            logical_name=logical_name,
            logical_version=logical_version,
            idempotency_key=idempotency_key,
            canonical_command_payload=command_payload,
            canonical_command_hash=command_hash,
            canonical_metadata_payload=metadata,
            canonical_metadata_hash=hashlib.sha256(
                _canonical_json_bytes(metadata)
            ).hexdigest(),
            creation_actor_type=command_payload["creation_actor_type"],
            creation_actor_id=command_payload["creation_actor_id"],
            created_at=datetime.now(timezone.utc),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(ExecutionValidationSchema)
                .filter(ExecutionValidationSchema.idempotency_key == idempotency_key)
                .one_or_none()
            )
            if replay is not None and replay.canonical_command_hash == command_hash:
                return ValidationSchemaCreationResult(replay, replayed=True)
            raise ValidationSchemaError(
                "validation_schema_creation_conflict",
                "schema authority conflicts with a concurrent creation",
            ) from exc
        return ValidationSchemaCreationResult(row)

    def resolve_reference(
        self,
        reference: str,
        *,
        expected_hash: str | None = None,
        expected_dialect: str | None = None,
    ) -> ExecutionValidationSchema:
        schema_id = parse_schema_reference(reference)
        row = (
            self.db.query(ExecutionValidationSchema)
            .filter(ExecutionValidationSchema.schema_id == schema_id)
            .one_or_none()
        )
        if row is None:
            raise ValidationSchemaError(
                "validation_schema_missing", "schema authority is missing"
            )
        if expected_hash != row.schema_sha256:
            raise ValidationSchemaError(
                "validation_schema_hash_mismatch",
                "schema hash does not match authority",
            )
        if expected_dialect != row.dialect:
            raise ValidationSchemaError(
                "validation_schema_dialect_mismatch",
                "schema dialect does not match authority",
            )
        integrity = self.verify_integrity(row.id)
        if not integrity.verified:
            raise ValidationSchemaError(
                "validation_schema_integrity_failure",
                "schema authority integrity failed",
            )
        return row

    def verify_integrity(self, schema_id: int | str) -> ValidationSchemaIntegrityResult:
        row = (
            self.db.get(ExecutionValidationSchema, int(schema_id))
            if isinstance(schema_id, int)
            else self.db.query(ExecutionValidationSchema)
            .filter(ExecutionValidationSchema.schema_id == schema_id)
            .one_or_none()
        )
        if row is None:
            return ValidationSchemaIntegrityResult(
                None, False, ("validation_schema_missing",)
            )
        issues: list[str] = []
        try:
            canonical, encoded, derived_id, bounds = canonicalize_validation_schema(
                row.canonical_schema_payload
            )
        except ValidationSchemaError as exc:
            canonical = None
            encoded = b""
            derived_id = ""
            bounds = {}
            issues.append(exc.code)
        valid_schema_id = isinstance(row.schema_id, str) and _SCHEMA_ID_RE.fullmatch(
            row.schema_id
        )
        if (
            not valid_schema_id
            or row.schema_id != row.schema_sha256
            or not isinstance(row.schema_sha256, str)
        ):
            issues.append("validation_schema_identity_invalid")
        if derived_id != row.schema_id:
            issues.append("validation_schema_hash_mismatch")
        if canonical is not None:
            if row.canonical_schema_payload != canonical:
                issues.append("validation_schema_canonicalization_mismatch")
            if row.schema_size_bytes != len(encoded):
                issues.append("validation_schema_size_mismatch")
            for field, expected in (
                ("schema_depth", bounds["depth"]),
                ("schema_object_members", bounds["object_members"]),
                ("schema_array_length", bounds["array_length"]),
                ("schema_max_string_length", bounds["max_string_length"]),
                ("schema_reference_count", bounds["reference_count"]),
                ("schema_regex_length", bounds["regex_length"]),
            ):
                if getattr(row, field) != expected:
                    issues.append("validation_schema_bounds_mismatch")
        metadata = _metadata_payload(
            schema_id=row.schema_id,
            schema_size=row.schema_size_bytes,
            bounds={
                "depth": row.schema_depth,
                "object_members": row.schema_object_members,
                "array_length": row.schema_array_length,
                "max_string_length": row.schema_max_string_length,
                "reference_count": row.schema_reference_count,
                "regex_length": row.schema_regex_length,
            },
            dialect=row.dialect,
            logical_name=row.logical_name,
            logical_version=row.logical_version,
        )
        if (
            row.schema_type != VALIDATION_SCHEMA_TYPE
            or row.schema_version != VALIDATION_SCHEMA_SCHEMA_VERSION
        ):
            issues.append("validation_schema_type_unsupported")
        if row.dialect != SUPPORTED_SCHEMA_DIALECT:
            issues.append("validation_schema_dialect_unsupported")
        if row.canonical_metadata_payload != metadata:
            issues.append("validation_schema_metadata_tampered")
        if (
            row.canonical_metadata_hash
            != hashlib.sha256(_canonical_json_bytes(metadata)).hexdigest()
        ):
            issues.append("validation_schema_metadata_hash_mismatch")
        return ValidationSchemaIntegrityResult(
            row.schema_id, not issues, tuple(sorted(set(issues)))
        )

    def inspect(self, schema_id: int | str) -> ValidationSchemaInspection:
        row = (
            self.db.get(ExecutionValidationSchema, int(schema_id))
            if isinstance(schema_id, int)
            else self.db.query(ExecutionValidationSchema)
            .filter(ExecutionValidationSchema.schema_id == schema_id)
            .one_or_none()
        )
        if row is None:
            raise ValidationSchemaError(
                "validation_schema_missing", "schema authority is missing"
            )
        integrity = self.verify_integrity(row.id)
        referenced = (
            self.db.query(ExecutionTaskValidationSpecification)
            .filter(ExecutionTaskValidationSpecification.validation_schema_id == row.id)
            .count()
        )
        return ValidationSchemaInspection(
            schema_id=row.schema_id,
            schema_sha256=row.schema_sha256,
            schema_type=row.schema_type,
            dialect=row.dialect,
            encoded_size=row.schema_size_bytes,
            depth=row.schema_depth,
            object_members=row.schema_object_members,
            array_length=row.schema_array_length,
            max_string_length=row.schema_max_string_length,
            reference_count=row.schema_reference_count,
            regex_length=row.schema_regex_length,
            referenced_by_count=referenced,
            integrity_verified=integrity.verified,
            issues=integrity.issues,
        )

    def delete_if_unreferenced(self, schema_id: int | str) -> None:
        row = (
            self.db.get(ExecutionValidationSchema, int(schema_id))
            if isinstance(schema_id, int)
            else self.db.query(ExecutionValidationSchema)
            .filter(ExecutionValidationSchema.schema_id == schema_id)
            .one_or_none()
        )
        if row is None:
            raise ValidationSchemaError(
                "validation_schema_missing", "schema authority is missing"
            )
        references = (
            self.db.query(ExecutionTaskValidationSpecification)
            .filter(ExecutionTaskValidationSpecification.validation_schema_id == row.id)
            .count()
        )
        if references:
            raise ValidationSchemaError(
                "validation_schema_referenced",
                "schema authority is referenced by a released contract",
            )
        self.db.delete(row)
        self.db.flush()


def verify_validation_schema_integrity(
    db: Session, schema_id: int | str
) -> ValidationSchemaIntegrityResult:
    return ExecutionValidationSchemaService(db).verify_integrity(schema_id)


__all__ = [
    "CreateValidationSchemaCommand",
    "ExecutionValidationSchemaService",
    "MAX_SCHEMA_ARRAY_LENGTH",
    "MAX_SCHEMA_DEPTH",
    "MAX_SCHEMA_ENCODED_BYTES",
    "MAX_SCHEMA_OBJECT_MEMBERS",
    "MAX_SCHEMA_REFERENCE_COUNT",
    "MAX_SCHEMA_REGEX_LENGTH",
    "MAX_SCHEMA_STRING_LENGTH",
    "SUPPORTED_SCHEMA_DIALECT",
    "VALIDATOR_IMPLEMENTATION_ID",
    "VALIDATOR_IMPLEMENTATION_VERSION",
    "VALIDATION_SCHEMA_SCHEMA_VERSION",
    "ValidationSchemaError",
    "ValidationSchemaInspection",
    "ValidationSchemaIntegrityResult",
    "ValidationSchemaCreationResult",
    "canonicalize_validation_schema",
    "parse_schema_reference",
    "schema_reference_for_id",
    "verify_validation_schema_integrity",
]
