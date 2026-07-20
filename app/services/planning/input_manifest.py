"""Canonical, immutable input provenance for Planning Protocol v2.

The manifest is deliberately a domain value rather than a SQLAlchemy model.
Persistence owns the immutable database envelope; this module owns the
canonical bytes, source ordering, redaction, identity, and validation rules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from types import MappingProxyType
from typing import Any
import unicodedata


INPUT_MANIFEST_SCHEMA_VERSION = "protocol-v2-input-manifest/1.0"
INPUT_MANIFEST_PROTOCOL_VERSION = "v2"
SUPPORTED_INPUT_MANIFEST_SCHEMA_VERSIONS = frozenset({INPUT_MANIFEST_SCHEMA_VERSION})
MANIFEST_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_ID_RE = re.compile(r"^manifest:[0-9a-f]{32,64}$")
SOURCE_ID_RE = re.compile(r"^source:[a-z0-9_.-]+:[0-9a-f]{32}$")
SOURCE_TYPE_ORDER = {
    "planning_request": 10,
    "clarification_message": 20,
    "project_metadata": 30,
    "project_rules": 40,
    "repository": 50,
    "engineering_context": 60,
    "structural_information": 70,
    "runtime_configuration": 80,
    "replanning_lineage": 90,
}
REQUIRED_SOURCE_TYPES = frozenset(
    {"planning_request", "project_metadata", "runtime_configuration"}
)
REDACTION_MARKER_VERSION = "typed-redaction/1"
_SECRET_KEY_MARKERS = (
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
)
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.I | re.S,
        ),
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    ),
    (
        "credential_shape",
        re.compile(
            r"\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|secret)\s*[:=]\s*['\"]?[^\s,'\"}]+",
            re.I,
        ),
    ),
    (
        "connection_string",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s'\"]+", re.I
        ),
    ),
    ("provider_key_shape", re.compile(r"\b(?:sk|ghp|xox[baprs])-[A-Za-z0-9_-]{12,}\b")),
)


class InputManifestError(ValueError):
    """Base class for malformed or unsafe input manifests."""


class InputManifestValidationError(InputManifestError):
    """A manifest cannot be accepted as canonical provenance."""


def _normalize(value: Any) -> Any:
    """Normalize JSON-compatible values recursively using Unicode NFC."""

    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {
            unicodedata.normalize("NFC", str(key)): _normalize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    raise InputManifestError(
        f"manifest value is not JSON-compatible: {type(value).__name__}"
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize normalized JSON with deterministic UTF-8 bytes."""

    try:
        normalized = _normalize(value)
        return json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise InputManifestError("manifest is not canonically serializable") from exc


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _secret_key(key: Any) -> bool:
    normalized = str(key).strip().casefold().replace("-", "_")
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _redact(value: Any, classes: set[str]) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = unicodedata.normalize("NFC", str(key))
            if _secret_key(normalized_key):
                classes.add("configured_secret")
                redacted[normalized_key] = {
                    "__redacted__": "configured_secret",
                    "marker_version": REDACTION_MARKER_VERSION,
                }
            else:
                redacted[normalized_key] = _redact(item, classes)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item, classes) for item in value]
    if isinstance(value, str):
        redacted = value
        for class_name, pattern in _SECRET_PATTERNS:
            if pattern.search(redacted):
                classes.add(class_name)
                redacted = pattern.sub(
                    f"<redacted:{class_name}:{REDACTION_MARKER_VERSION}>",
                    redacted,
                )
        return redacted
    return value


def redact_json(value: Any) -> tuple[Any, tuple[str, ...]]:
    """Return deterministic redacted content and sorted redaction classes."""

    classes: set[str] = set()
    redacted = _redact(value, classes)
    return _normalize(redacted), tuple(sorted(classes))


def _source_id(source_type: str, stable_key: str) -> str:
    return f"source:{source_type}:{hashlib.sha256(stable_key.encode('utf-8')).hexdigest()[:32]}"


def _source_content_hash(content: Any) -> str:
    return canonical_json_hash(content)


def _normalize_repository_identity(value: Any) -> str | None:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        return None
    normalized = re.sub(r"(?i)(://)[^/@]+@", r"\1", normalized)
    while normalized.endswith("/"):
        normalized = normalized[:-1]
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.lower()


@dataclass(frozen=True)
class ManifestSource:
    """One ordered, redacted source in the manifest inventory."""

    source_id: str
    source_type: str
    ordinal: int
    content_hash: str
    identity_metadata: Mapping[str, Any]
    included: bool
    omission_reason: str | None
    redaction_state: str
    redaction_classes: tuple[str, ...]
    content: Any

    @property
    def source_hash(self) -> str:
        return self.content_hash

    @property
    def stable_manifest_id(self) -> str:
        return self.source_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "ordinal": self.ordinal,
            "content_hash": self.content_hash,
            "identity_metadata": _thaw(self.identity_metadata),
            "included": self.included,
            "omission_reason": self.omission_reason,
            "redaction_state": self.redaction_state,
            "redaction_classes": list(self.redaction_classes),
            "content": _thaw(self.content),
        }

    @classmethod
    def create(
        cls,
        *,
        source_type: str,
        stable_key: str,
        ordinal: int,
        content: Any,
        identity_metadata: Mapping[str, Any] | None = None,
        included: bool = True,
        omission_reason: str | None = None,
    ) -> "ManifestSource":
        normalized_type = str(source_type or "").strip().lower()
        if normalized_type not in SOURCE_TYPE_ORDER:
            raise InputManifestError(f"unsupported source type: {source_type!r}")
        redacted_content, classes = redact_json(content)
        redacted_identity, identity_classes = redact_json(identity_metadata or {})
        all_classes = tuple(sorted(set(classes) | set(identity_classes)))
        return cls(
            source_id=_source_id(normalized_type, str(stable_key)),
            source_type=normalized_type,
            ordinal=int(ordinal),
            content_hash=_source_content_hash(redacted_content),
            identity_metadata=_freeze(redacted_identity),
            included=bool(included),
            omission_reason=(
                unicodedata.normalize("NFC", str(omission_reason))
                if omission_reason is not None
                else None
            ),
            redaction_state="redacted" if all_classes else "none",
            redaction_classes=all_classes,
            content=_freeze(redacted_content),
        )


@dataclass(frozen=True)
class FreshnessMetadata:
    repository_revision: str | None
    repository_dirty: bool | None
    engineering_context_freshness: str
    structural_information_freshness: str
    selection_timestamps: Mapping[str, str]
    manifest_built_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_revision": self.repository_revision,
            "repository_dirty": self.repository_dirty,
            "engineering_context_freshness": self.engineering_context_freshness,
            "structural_information_freshness": self.structural_information_freshness,
            "selection_timestamps": _thaw(self.selection_timestamps),
            "manifest_built_at": self.manifest_built_at,
        }


@dataclass(frozen=True)
class ConfigurationIdentity:
    provider: str
    backend: str
    model: str
    reasoning_profile: str
    protocol_version: str
    stage_configuration_fingerprint: str

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "backend": self.backend,
            "model": self.model,
            "reasoning_profile": self.reasoning_profile,
            "protocol_version": self.protocol_version,
            "stage_configuration_fingerprint": self.stage_configuration_fingerprint,
        }


@dataclass(frozen=True)
class RepositoryIdentity:
    identity: str | None
    workspace: str | None
    revision: str | None
    dirty: bool | None
    available: bool
    omission_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "workspace": self.workspace,
            "revision": self.revision,
            "dirty": self.dirty,
            "available": self.available,
            "omission_reason": self.omission_reason,
        }


@dataclass(frozen=True)
class EngineeringContextIdentity:
    object_id: str | None
    subsystem_version: int | None
    content_hash: str | None
    repository_revision: str | None
    freshness: str
    selection_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "subsystem_version": self.subsystem_version,
            "content_hash": self.content_hash,
            "repository_revision": self.repository_revision,
            "freshness": self.freshness,
            "selection_reason": self.selection_reason,
        }


@dataclass(frozen=True)
class StructuralInformationIdentity:
    object_id: str | None
    schema_version: int | None
    algorithm_version: int | None
    content_hash: str | None
    parent_context_id: str | None
    freshness: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "schema_version": self.schema_version,
            "algorithm_version": self.algorithm_version,
            "content_hash": self.content_hash,
            "parent_context_id": self.parent_context_id,
            "freshness": self.freshness,
        }


@dataclass(frozen=True)
class GenerationIdentity:
    session_id: int
    session_generation_id: str
    manifest_generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_generation_id": self.session_generation_id,
            "manifest_generation": self.manifest_generation,
        }


@dataclass(frozen=True)
class RedactionSummary:
    marker_version: str
    source_count: int
    redacted_source_count: int
    classes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker_version": self.marker_version,
            "source_count": self.source_count,
            "redacted_source_count": self.redacted_source_count,
            "classes": list(self.classes),
        }


@dataclass(frozen=True)
class InputManifest:
    """The sole Protocol v2 provenance authority for one session generation."""

    manifest_id: str
    schema_version: str
    protocol_version: str
    sources: tuple[ManifestSource, ...]
    freshness: FreshnessMetadata
    redaction: RedactionSummary
    configuration_identity: ConfigurationIdentity
    repository_identity: RepositoryIdentity
    engineering_context_identity: EngineeringContextIdentity
    structural_information_identity: StructuralInformationIdentity
    generation_identity: GenerationIdentity
    manifest_hash: str

    @property
    def source_inventory(self) -> tuple[ManifestSource, ...]:
        return self.sources

    @property
    def ordered_sources(self) -> tuple[ManifestSource, ...]:
        return self.sources

    @property
    def source_hashes(self) -> Mapping[str, str]:
        return MappingProxyType(
            {source.source_id: source.content_hash for source in self.sources}
        )

    @property
    def manifest_identity(self) -> str:
        return self.manifest_id

    @property
    def canonical_hash(self) -> str:
        return self.manifest_hash

    def _canonical_payload(self, *, include_manifest_id: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "sources": [source.to_dict() for source in self.sources],
            "freshness": self.freshness.to_dict(),
            "redaction": self.redaction.to_dict(),
            "configuration_identity": self.configuration_identity.to_dict(),
            "repository_identity": self.repository_identity.to_dict(),
            "engineering_context_identity": self.engineering_context_identity.to_dict(),
            "structural_information_identity": self.structural_information_identity.to_dict(),
            "generation_identity": self.generation_identity.to_dict(),
        }
        if include_manifest_id:
            payload["manifest_id"] = self.manifest_id
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self._canonical_payload())

    def canonical_json(self) -> str:
        return self.canonical_bytes().decode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        payload = self._canonical_payload()
        payload["manifest_hash"] = self.manifest_hash
        return payload

    def validate(self) -> "InputManifest":
        validate_input_manifest(self)
        return self

    @classmethod
    def create(
        cls,
        *,
        schema_version: str,
        protocol_version: str,
        sources: Sequence[ManifestSource],
        freshness: FreshnessMetadata,
        redaction: RedactionSummary,
        configuration_identity: ConfigurationIdentity,
        repository_identity: RepositoryIdentity,
        engineering_context_identity: EngineeringContextIdentity,
        structural_information_identity: StructuralInformationIdentity,
        generation_identity: GenerationIdentity,
    ) -> "InputManifest":
        ordered_sources = tuple(sorted(sources, key=lambda source: source.ordinal))
        provisional = cls(
            manifest_id="manifest:" + "0" * 32,
            schema_version=schema_version,
            protocol_version=protocol_version,
            sources=ordered_sources,
            freshness=freshness,
            redaction=redaction,
            configuration_identity=configuration_identity,
            repository_identity=repository_identity,
            engineering_context_identity=engineering_context_identity,
            structural_information_identity=structural_information_identity,
            generation_identity=generation_identity,
            manifest_hash="0" * 64,
        )
        identity_hash = canonical_json_hash(
            provisional._canonical_payload(include_manifest_id=False)
        )
        manifest_id = f"manifest:{identity_hash[:32]}"
        with_id = replace(provisional, manifest_id=manifest_id)
        manifest_hash = canonical_json_hash(with_id._canonical_payload())
        manifest = replace(with_id, manifest_hash=manifest_hash)
        manifest.validate()
        return manifest

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InputManifest":
        if not isinstance(raw, Mapping):
            raise InputManifestValidationError("manifest must be a mapping")
        required = {
            "manifest_id",
            "schema_version",
            "protocol_version",
            "sources",
            "freshness",
            "redaction",
            "configuration_identity",
            "repository_identity",
            "engineering_context_identity",
            "structural_information_identity",
            "generation_identity",
            "manifest_hash",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise InputManifestValidationError(
                f"manifest missing required fields: {','.join(missing)}"
            )
        try:
            source_values = tuple(
                ManifestSource(
                    source_id=str(item["source_id"]),
                    source_type=str(item["source_type"]),
                    ordinal=int(item["ordinal"]),
                    content_hash=str(item["content_hash"]),
                    identity_metadata=_freeze(_normalize(item["identity_metadata"])),
                    included=bool(item["included"]),
                    omission_reason=item.get("omission_reason"),
                    redaction_state=str(item["redaction_state"]),
                    redaction_classes=tuple(str(x) for x in item["redaction_classes"]),
                    content=_freeze(_normalize(item["content"])),
                )
                for item in raw["sources"]
            )
            freshness_raw = raw["freshness"]
            redaction_raw = raw["redaction"]
            configuration_raw = raw["configuration_identity"]
            repository_raw = raw["repository_identity"]
            context_raw = raw["engineering_context_identity"]
            structural_raw = raw["structural_information_identity"]
            generation_raw = raw["generation_identity"]
            manifest = cls(
                manifest_id=str(raw["manifest_id"]),
                schema_version=str(raw["schema_version"]),
                protocol_version=str(raw["protocol_version"]),
                sources=source_values,
                freshness=FreshnessMetadata(
                    repository_revision=freshness_raw.get("repository_revision"),
                    repository_dirty=freshness_raw.get("repository_dirty"),
                    engineering_context_freshness=str(
                        freshness_raw["engineering_context_freshness"]
                    ),
                    structural_information_freshness=str(
                        freshness_raw["structural_information_freshness"]
                    ),
                    selection_timestamps=_freeze(
                        _normalize(freshness_raw["selection_timestamps"])
                    ),
                    manifest_built_at=str(freshness_raw["manifest_built_at"]),
                ),
                redaction=RedactionSummary(
                    marker_version=str(redaction_raw["marker_version"]),
                    source_count=int(redaction_raw["source_count"]),
                    redacted_source_count=int(redaction_raw["redacted_source_count"]),
                    classes=tuple(str(x) for x in redaction_raw["classes"]),
                ),
                configuration_identity=ConfigurationIdentity(
                    provider=str(configuration_raw["provider"]),
                    backend=str(configuration_raw["backend"]),
                    model=str(configuration_raw["model"]),
                    reasoning_profile=str(configuration_raw["reasoning_profile"]),
                    protocol_version=str(configuration_raw["protocol_version"]),
                    stage_configuration_fingerprint=str(
                        configuration_raw["stage_configuration_fingerprint"]
                    ),
                ),
                repository_identity=RepositoryIdentity(
                    identity=repository_raw.get("identity"),
                    workspace=repository_raw.get("workspace"),
                    revision=repository_raw.get("revision"),
                    dirty=repository_raw.get("dirty"),
                    available=bool(repository_raw["available"]),
                    omission_reason=repository_raw.get("omission_reason"),
                ),
                engineering_context_identity=EngineeringContextIdentity(
                    object_id=context_raw.get("object_id"),
                    subsystem_version=context_raw.get("subsystem_version"),
                    content_hash=context_raw.get("content_hash"),
                    repository_revision=context_raw.get("repository_revision"),
                    freshness=str(context_raw["freshness"]),
                    selection_reason=str(context_raw["selection_reason"]),
                ),
                structural_information_identity=StructuralInformationIdentity(
                    object_id=structural_raw.get("object_id"),
                    schema_version=structural_raw.get("schema_version"),
                    algorithm_version=structural_raw.get("algorithm_version"),
                    content_hash=structural_raw.get("content_hash"),
                    parent_context_id=structural_raw.get("parent_context_id"),
                    freshness=str(structural_raw["freshness"]),
                ),
                generation_identity=GenerationIdentity(
                    session_id=int(generation_raw["session_id"]),
                    session_generation_id=str(generation_raw["session_generation_id"]),
                    manifest_generation=int(generation_raw["manifest_generation"]),
                ),
                manifest_hash=str(raw["manifest_hash"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InputManifestValidationError("malformed manifest identity") from exc
        manifest.validate()
        return manifest


def _source_from_parts(
    *,
    source_type: str,
    stable_key: str,
    ordinal: int,
    content: Any,
    identity_metadata: Mapping[str, Any] | None = None,
    included: bool = True,
    omission_reason: str | None = None,
) -> ManifestSource:
    return ManifestSource.create(
        source_type=source_type,
        stable_key=stable_key,
        ordinal=ordinal,
        content=content,
        identity_metadata=identity_metadata,
        included=included,
        omission_reason=omission_reason,
    )


def _coerce_context_identity(
    value: Mapping[str, Any] | None
) -> EngineeringContextIdentity:
    raw = dict(value or {})
    supplied = bool(raw.get("object_id") or raw.get("identity"))
    return EngineeringContextIdentity(
        object_id=(
            str(raw.get("object_id") or raw.get("identity")) if supplied else None
        ),
        subsystem_version=(
            int(raw["subsystem_version"])
            if raw.get("subsystem_version") is not None
            else None
        ),
        content_hash=(str(raw["content_hash"]) if raw.get("content_hash") else None),
        repository_revision=(
            str(raw.get("repository_revision") or raw.get("commit_fingerprint"))
            if raw.get("repository_revision") or raw.get("commit_fingerprint")
            else None
        ),
        freshness=str(
            raw.get("freshness") or ("fresh" if supplied else "not_selected")
        ),
        selection_reason=str(
            raw.get("selection_reason")
            or raw.get("reason")
            or ("selected" if supplied else "not_selected")
        ),
    )


def _coerce_structural_identity(
    value: Mapping[str, Any] | None,
    *,
    parent_context_id: str | None,
) -> StructuralInformationIdentity:
    raw = dict(value or {})
    supplied = bool(raw.get("object_id"))
    return StructuralInformationIdentity(
        object_id=str(raw["object_id"]) if supplied else None,
        schema_version=(
            int(raw["schema_version"])
            if raw.get("schema_version") is not None
            else None
        ),
        algorithm_version=(
            int(raw["algorithm_version"])
            if raw.get("algorithm_version") is not None
            else None
        ),
        content_hash=(str(raw["content_hash"]) if raw.get("content_hash") else None),
        parent_context_id=parent_context_id,
        freshness=str(
            raw.get("freshness") or ("fresh" if supplied else "not_selected")
        ),
    )


def _stage_fingerprint(stage_configuration: Mapping[str, Any] | None) -> str:
    return canonical_json_hash(stage_configuration or {"stages": []})


def build_input_manifest(
    *,
    session_id: int,
    session_generation_id: str,
    planning_request: Mapping[str, Any] | str,
    project_metadata: Mapping[str, Any],
    runtime_configuration: Mapping[str, Any],
    protocol_version: str = INPUT_MANIFEST_PROTOCOL_VERSION,
    clarification_messages: Sequence[Mapping[str, Any]] = (),
    project_rules: str | None = None,
    repository: Mapping[str, Any] | None = None,
    engineering_context: Mapping[str, Any] | None = None,
    structural_information: Mapping[str, Any] | None = None,
    replanning_lineage: Mapping[str, Any] | None = None,
    stage_configuration: Mapping[str, Any] | None = None,
    selection_timestamps: Mapping[str, str] | None = None,
    manifest_built_at: str | None = None,
    manifest_generation: int = 1,
) -> InputManifest:
    """Build a complete manifest from already selected, bounded evidence."""

    if str(protocol_version).strip().lower() != INPUT_MANIFEST_PROTOCOL_VERSION:
        raise InputManifestError("input manifests are supported only for Protocol v2")
    generation = GenerationIdentity(
        session_id=int(session_id),
        session_generation_id=str(session_generation_id).strip(),
        manifest_generation=int(manifest_generation),
    )
    planning_content = (
        {"content": planning_request}
        if isinstance(planning_request, str)
        else dict(planning_request)
    )
    message_id = str(planning_content.get("message_id") or "session-request")
    sources: list[ManifestSource] = [
        _source_from_parts(
            source_type="planning_request",
            stable_key=f"{session_generation_id}:{message_id}",
            ordinal=1,
            content=planning_content,
            identity_metadata={"message_id": message_id},
        )
    ]
    next_ordinal = 2
    for message in sorted(
        (dict(item) for item in clarification_messages),
        key=lambda item: (int(item.get("id") or 0), str(item.get("created_at") or "")),
    ):
        message_id = str(message.get("id") or f"clarification-{next_ordinal}")
        sources.append(
            _source_from_parts(
                source_type="clarification_message",
                stable_key=f"{session_generation_id}:{message_id}",
                ordinal=next_ordinal,
                content=message,
                identity_metadata={
                    "message_id": message_id,
                    "role": message.get("role"),
                },
            )
        )
        next_ordinal += 1
    if not clarification_messages:
        sources.append(
            _source_from_parts(
                source_type="clarification_message",
                stable_key=f"{session_generation_id}:clarification-history",
                ordinal=next_ordinal,
                content=[],
                identity_metadata={"collection": "clarification_history"},
                included=False,
                omission_reason="no_clarifications",
            )
        )
        next_ordinal += 1
    sources.append(
        _source_from_parts(
            source_type="project_metadata",
            stable_key=f"project:{project_metadata.get('project_id', session_id)}:metadata",
            ordinal=next_ordinal,
            content=dict(project_metadata),
            identity_metadata={"project_id": project_metadata.get("project_id")},
        )
    )
    next_ordinal += 1
    rules_included = bool(project_rules and str(project_rules).strip())
    sources.append(
        _source_from_parts(
            source_type="project_rules",
            stable_key=f"project:{project_metadata.get('project_id', session_id)}:rules",
            ordinal=next_ordinal,
            content=project_rules if rules_included else None,
            identity_metadata={"project_id": project_metadata.get("project_id")},
            included=rules_included,
            omission_reason=None if rules_included else "not_configured",
        )
    )
    next_ordinal += 1

    repository_data = dict(repository or {})
    repository_available = bool(
        repository_data.get("available") and repository_data.get("identity")
    )
    repository_identity = RepositoryIdentity(
        identity=_normalize_repository_identity(repository_data.get("identity")),
        workspace=(
            str(repository_data["workspace"]).replace("\\", "/")
            if repository_data.get("workspace")
            else None
        ),
        revision=(
            str(repository_data["revision"])
            if repository_data.get("revision")
            else None
        ),
        dirty=(
            repository_data.get("dirty")
            if repository_data.get("dirty") is not None
            else None
        ),
        available=repository_available,
        omission_reason=(
            None
            if repository_available
            else str(repository_data.get("omission_reason") or "repository_unavailable")
        ),
    )
    sources.append(
        _source_from_parts(
            source_type="repository",
            stable_key=f"repository:{repository_identity.identity or repository_identity.workspace or session_generation_id}",
            ordinal=next_ordinal,
            content=repository_identity.to_dict(),
            identity_metadata={"repository_identity": repository_identity.identity},
            included=repository_available,
            omission_reason=repository_identity.omission_reason,
        )
    )
    next_ordinal += 1

    context_identity = _coerce_context_identity(engineering_context)
    context_included = bool(context_identity.object_id)
    sources.append(
        _source_from_parts(
            source_type="engineering_context",
            stable_key=f"engineering-context:{context_identity.object_id or session_generation_id}",
            ordinal=next_ordinal,
            content=context_identity.to_dict(),
            identity_metadata={"object_id": context_identity.object_id},
            included=context_included,
            omission_reason=(
                None if context_included else context_identity.selection_reason
            ),
        )
    )
    next_ordinal += 1

    structural_identity = _coerce_structural_identity(
        structural_information,
        parent_context_id=context_identity.object_id,
    )
    structural_included = bool(structural_identity.object_id)
    sources.append(
        _source_from_parts(
            source_type="structural_information",
            stable_key=f"structural-information:{structural_identity.object_id or session_generation_id}",
            ordinal=next_ordinal,
            content=structural_identity.to_dict(),
            identity_metadata={"object_id": structural_identity.object_id},
            included=structural_included,
            omission_reason=None if structural_included else "not_selected",
        )
    )
    next_ordinal += 1

    config = {
        "provider": runtime_configuration.get("provider")
        or runtime_configuration.get("source_brain")
        or "unknown",
        "backend": runtime_configuration.get("backend")
        or runtime_configuration.get("planning_backend")
        or "unknown",
        "model": runtime_configuration.get("model")
        or runtime_configuration.get("planner_model")
        or "unknown",
        "reasoning_profile": runtime_configuration.get("reasoning_profile")
        or "default",
        "protocol_version": INPUT_MANIFEST_PROTOCOL_VERSION,
        "stage_configuration_fingerprint": runtime_configuration.get(
            "stage_configuration_fingerprint"
        )
        or _stage_fingerprint(stage_configuration),
    }
    configuration_identity = ConfigurationIdentity(
        **{key: str(value) for key, value in config.items()}
    )
    sources.append(
        _source_from_parts(
            source_type="runtime_configuration",
            stable_key=f"runtime:{session_generation_id}",
            ordinal=next_ordinal,
            content=configuration_identity.to_dict(),
            identity_metadata={
                "configuration_fingerprint": configuration_identity.stage_configuration_fingerprint
            },
        )
    )
    next_ordinal += 1

    if replanning_lineage is not None:
        sources.append(
            _source_from_parts(
                source_type="replanning_lineage",
                stable_key=f"replan:{session_generation_id}",
                ordinal=next_ordinal,
                content=dict(replanning_lineage),
                identity_metadata={"lineage": True},
            )
        )
    else:
        sources.append(
            _source_from_parts(
                source_type="replanning_lineage",
                stable_key=f"replan:{session_generation_id}",
                ordinal=next_ordinal,
                content=None,
                identity_metadata={"lineage": False},
                included=False,
                omission_reason="not_replanning",
            )
        )

    source_classes = sorted(
        {class_name for source in sources for class_name in source.redaction_classes}
    )
    redaction = RedactionSummary(
        marker_version=REDACTION_MARKER_VERSION,
        source_count=len(sources),
        redacted_source_count=sum(bool(source.redaction_classes) for source in sources),
        classes=tuple(source_classes),
    )
    repository_revision = repository_identity.revision
    freshness = FreshnessMetadata(
        repository_revision=repository_revision,
        repository_dirty=repository_identity.dirty,
        engineering_context_freshness=context_identity.freshness,
        structural_information_freshness=structural_identity.freshness,
        selection_timestamps=_freeze(_normalize(dict(selection_timestamps or {}))),
        manifest_built_at=str(
            manifest_built_at or datetime.now(timezone.utc).isoformat()
        ),
    )
    return InputManifest.create(
        schema_version=INPUT_MANIFEST_SCHEMA_VERSION,
        protocol_version=INPUT_MANIFEST_PROTOCOL_VERSION,
        sources=tuple(sources),
        freshness=freshness,
        redaction=redaction,
        configuration_identity=configuration_identity,
        repository_identity=repository_identity,
        engineering_context_identity=context_identity,
        structural_information_identity=structural_identity,
        generation_identity=generation,
    )


def build_compatibility_manifest(
    *,
    session_id: int,
    session_generation_id: str,
    planning_input_hash: str,
    engineering_context_identity: str,
    provider_identity: str,
    model_configuration: Mapping[str, Any],
    repository_identity: str,
) -> InputManifest:
    """Adapt the Phase 28B coarse identity without re-reading live state."""

    configuration = dict(model_configuration)
    return build_input_manifest(
        session_id=session_id,
        session_generation_id=session_generation_id,
        planning_request={
            "content_hash": planning_input_hash,
            "legacy_identity": True,
        },
        project_metadata={"project_id": session_id, "legacy_identity": True},
        runtime_configuration={
            "provider": provider_identity,
            "backend": configuration.get("backend") or provider_identity,
            "model": configuration.get("model")
            or configuration.get("planner_model")
            or "unknown",
            "reasoning_profile": configuration.get("reasoning_profile") or "default",
            "stage_configuration_fingerprint": configuration.get(
                "configuration_fingerprint"
            )
            or _stage_fingerprint(None),
        },
        repository={
            "identity": _normalize_repository_identity(repository_identity),
            "workspace": repository_identity,
            "available": bool(repository_identity),
            "omission_reason": (
                None if repository_identity else "repository_unavailable"
            ),
        },
        manifest_built_at="1970-01-01T00:00:00+00:00",
    )


def collect_repository_snapshot(workspace: str | Path | None) -> dict[str, Any]:
    """Collect only non-secret repository identity/freshness evidence."""

    raw = str(workspace or "").strip()
    if not raw:
        return {"available": False, "omission_reason": "repository_unavailable"}
    root = Path(raw).expanduser().resolve()
    identity = f"path:{root}"
    try:
        remote = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if remote:
            identity = remote.replace("\\", "/").rstrip("/")
            if identity.endswith(".git"):
                identity = identity[:-4]
            identity = identity.lower()
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        return {
            "available": True,
            "identity": identity,
            "workspace": str(root),
            "revision": revision or None,
            "dirty": bool(status.strip()),
        }
    except (OSError, subprocess.SubprocessError):
        return {
            "available": False,
            "identity": identity,
            "workspace": str(root),
            "omission_reason": "repository_revision_unavailable",
        }


def _validate_reference_values(value: Any, source_ids: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).endswith("_refs") or str(key) == "source_refs":
                if not isinstance(item, (list, tuple)):
                    raise InputManifestValidationError(
                        "source references must be arrays"
                    )
                for reference in item:
                    if str(reference) not in source_ids:
                        raise InputManifestValidationError(
                            f"missing source reference: {reference}"
                        )
            _validate_reference_values(item, source_ids)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_reference_values(item, source_ids)


def validate_input_manifest(manifest: InputManifest) -> None:
    """Validate schema, canonical hash, ordering, references, and redaction."""

    if manifest.schema_version not in SUPPORTED_INPUT_MANIFEST_SCHEMA_VERSIONS:
        raise InputManifestValidationError(
            f"unsupported input manifest schema version: {manifest.schema_version}"
        )
    if manifest.protocol_version != INPUT_MANIFEST_PROTOCOL_VERSION:
        raise InputManifestValidationError("manifest protocol version is not v2")
    if not MANIFEST_ID_RE.fullmatch(manifest.manifest_id):
        raise InputManifestValidationError("malformed manifest identity")
    if not MANIFEST_HASH_RE.fullmatch(manifest.manifest_hash):
        raise InputManifestValidationError("manifest hash is invalid")
    expected_hash = canonical_json_hash(manifest._canonical_payload())
    if expected_hash != manifest.manifest_hash:
        raise InputManifestValidationError(
            "manifest hash does not match canonical bytes"
        )
    if not manifest.sources:
        raise InputManifestValidationError("manifest source inventory is empty")
    source_ids: set[str] = set()
    source_types: set[str] = set()
    expected_source_order = tuple(
        sorted(
            manifest.sources,
            key=lambda source: (
                SOURCE_TYPE_ORDER.get(source.source_type, 999),
                source.ordinal,
            ),
        )
    )
    if tuple(manifest.sources) != expected_source_order:
        raise InputManifestValidationError("source inventory ordering is not canonical")
    all_source_ids = {source.source_id for source in manifest.sources}
    for expected_ordinal, source in enumerate(manifest.sources, start=1):
        if source.ordinal != expected_ordinal:
            raise InputManifestValidationError("source ordinals are not contiguous")
        if source.source_id in source_ids:
            raise InputManifestValidationError("duplicate source ID")
        source_ids.add(source.source_id)
        if not SOURCE_ID_RE.fullmatch(source.source_id):
            raise InputManifestValidationError("malformed source identity")
        if source.source_type not in SOURCE_TYPE_ORDER:
            raise InputManifestValidationError("unsupported source type")
        source_types.add(source.source_type)
        if not source.identity_metadata:
            raise InputManifestValidationError("source identity metadata is empty")
        if not MANIFEST_HASH_RE.fullmatch(source.content_hash):
            raise InputManifestValidationError("source hash is invalid")
        if source.content_hash != _source_content_hash(source.content):
            raise InputManifestValidationError(
                f"source hash does not match content: {source.source_id}"
            )
        if source.included and source.omission_reason is not None:
            raise InputManifestValidationError("included source has omission reason")
        if not source.included and not source.omission_reason:
            raise InputManifestValidationError("omitted source lacks omission reason")
        if source.redaction_state not in {"none", "redacted"}:
            raise InputManifestValidationError("invalid source redaction state")
        if source.redaction_state == "none" and source.redaction_classes:
            raise InputManifestValidationError(
                "unredacted source has redaction classes"
            )
        _validate_reference_values(source.identity_metadata, all_source_ids)
        _validate_reference_values(source.content, all_source_ids)
    if not REQUIRED_SOURCE_TYPES <= source_types:
        missing = sorted(REQUIRED_SOURCE_TYPES - source_types)
        raise InputManifestValidationError(
            f"required source types missing: {','.join(missing)}"
        )
    if manifest.redaction.source_count != len(manifest.sources):
        raise InputManifestValidationError("redaction source count is incorrect")
    if manifest.redaction.redacted_source_count != sum(
        bool(source.redaction_classes) for source in manifest.sources
    ):
        raise InputManifestValidationError("redaction outcome count is incorrect")
    classes = sorted(
        {
            class_name
            for source in manifest.sources
            for class_name in source.redaction_classes
        }
    )
    if list(manifest.redaction.classes) != classes:
        raise InputManifestValidationError("redaction classes are not canonical")
    config = manifest.configuration_identity
    for field_name in (
        "provider",
        "backend",
        "model",
        "reasoning_profile",
        "protocol_version",
    ):
        if not str(getattr(config, field_name)).strip():
            raise InputManifestValidationError(
                f"configuration identity lacks {field_name}"
            )
    if config.protocol_version != manifest.protocol_version:
        raise InputManifestValidationError("configuration protocol identity mismatch")
    if not MANIFEST_HASH_RE.fullmatch(config.stage_configuration_fingerprint):
        raise InputManifestValidationError("stage configuration fingerprint is invalid")
    generation = manifest.generation_identity
    if generation.session_id < 1 or not generation.session_generation_id.strip():
        raise InputManifestValidationError("generation identity is malformed")
    if generation.manifest_generation < 1:
        raise InputManifestValidationError("manifest generation is invalid")
    for value in (
        manifest.freshness.manifest_built_at,
        *manifest.freshness.selection_timestamps.values(),
    ):
        if not str(value).strip():
            raise InputManifestValidationError("freshness timestamp is empty")
    serialized = manifest.canonical_json()
    for class_name, pattern in _SECRET_PATTERNS:
        if pattern.search(serialized):
            raise InputManifestValidationError(
                f"secret leakage detected after redaction: {class_name}"
            )


class InputManifestBuilder:
    """Named builder facade used by session and compatibility integrations."""

    build = staticmethod(build_input_manifest)
    from_compatibility_identity = staticmethod(build_compatibility_manifest)


__all__ = [
    "ConfigurationIdentity",
    "EngineeringContextIdentity",
    "FreshnessMetadata",
    "GenerationIdentity",
    "InputManifest",
    "InputManifestBuilder",
    "InputManifestError",
    "InputManifestValidationError",
    "ManifestSource",
    "RedactionSummary",
    "RepositoryIdentity",
    "StructuralInformationIdentity",
    "build_compatibility_manifest",
    "build_input_manifest",
    "canonical_json_bytes",
    "canonical_json_hash",
    "collect_repository_snapshot",
    "redact_json",
    "validate_input_manifest",
]
