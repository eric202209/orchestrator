"""Persistence primitives for Planning Protocol v2.

This module is intentionally not called by the current synthesis or commit
flows.  Later protocol stages can use these append-only records without
changing the legacy PlanningArtifact contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    PlanningCheckpoint,
    PlanningCheckpointDependency,
    PlanningCommitManifest,
    PlanningCompletionManifest,
    PlanningProtocolInput,
    PlanningSession,
)
from app.services.planning.input_manifest import (
    InputManifest,
    InputManifestBuilder,
    InputManifestValidationError,
)

PROTOCOL_V1 = "v1"
PROTOCOL_V2 = "v2"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({PROTOCOL_V1, PROTOCOL_V2})
CHECKPOINT_STATUSES = frozenset({"accepted", "failed", "invalidated"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Only identity/configuration fields that are already non-secret in the
# runtime identity contract are accepted into the persisted snapshot.
_SAFE_MODEL_CONFIGURATION_KEYS = frozenset(
    {
        "model",
        "planner_model",
        "reasoning_profile",
        "configuration_fingerprint",
        "temperature",
        "top_p",
        "max_tokens",
        "seed",
        "response_format",
        "provider_options_hash",
    }
)


class ProtocolPersistenceError(ValueError):
    """The requested protocol record is invalid or conflicts with history."""


class ProtocolOwnershipError(RuntimeError):
    """A protocol write was attempted by a stale session owner."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ProtocolPersistenceError(
            "protocol payload is not JSON serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_protocol_version(value: str | None) -> str:
    version = str(value or "").strip().lower()
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ProtocolPersistenceError(f"unsupported protocol version: {version!r}")
    return version


def _normalize_required(value: Any, field_name: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ProtocolPersistenceError(f"{field_name} is required")
    if len(normalized) > max_length:
        raise ProtocolPersistenceError(f"{field_name} exceeds {max_length} characters")
    return normalized


def _safe_model_configuration(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ProtocolPersistenceError(
            "model_configuration must be a non-empty mapping"
        )
    unsafe_keys = {
        str(key)
        for key in value
        if any(
            marker in str(key).casefold()
            for marker in ("secret", "password", "token", "api_key", "private_key")
        )
    }
    if unsafe_keys:
        raise ProtocolPersistenceError("model_configuration contains secret material")
    normalized = {
        str(key): value[key]
        for key in value
        if str(key) in _SAFE_MODEL_CONFIGURATION_KEYS
    }
    if not normalized:
        raise ProtocolPersistenceError(
            "model_configuration has no persisted identity fields"
        )
    _canonical_json(normalized)
    return normalized


def _normalize_hash(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ProtocolPersistenceError(f"{field_name} must be a lowercase SHA-256 hash")
    return normalized


class PlanningProtocolPersistenceService:
    """Create and read append-only Protocol v2 persistence records.

    The service never commits or mutates an existing protocol record.  The
    caller owns the surrounding transaction, matching the existing service
    conventions and allowing a future stage to commit its state atomically.
    """

    def __init__(self, db: Session):
        self.db = db

    def _get_session(self, session_id: int) -> PlanningSession:
        session = (
            self.db.query(PlanningSession)
            .filter(PlanningSession.id == session_id)
            .populate_existing()
            .one_or_none()
        )
        if session is None:
            raise ProtocolPersistenceError(f"planning session {session_id} not found")
        return session

    def _assert_owner(
        self,
        session_id: int,
        *,
        protocol_version: str | None,
        session_generation_id: str | None,
        fencing_token: str | None,
    ) -> PlanningSession:
        session = self._get_session(session_id)
        expected_protocol = _normalize_protocol_version(
            protocol_version or session.protocol_version
        )
        if session.protocol_version != expected_protocol:
            raise ProtocolPersistenceError("protocol version does not match session")
        expected_generation = _normalize_required(
            session_generation_id or session.generation_id,
            "session_generation_id",
            128,
        )
        if session.generation_id != expected_generation:
            raise ProtocolOwnershipError(
                "session generation does not match current owner"
            )
        expected_fence = _normalize_required(
            fencing_token or session.processing_token,
            "fencing_token",
            128,
        )
        if session.processing_token != expected_fence:
            raise ProtocolOwnershipError("fencing token does not match current owner")
        return session

    def assert_owner(
        self,
        session_id: int,
        *,
        protocol_version: str | None = None,
        session_generation_id: str | None = None,
        fencing_token: str | None = None,
    ) -> PlanningSession:
        """Validate the current session fence without exposing database writes."""

        return self._assert_owner(
            session_id,
            protocol_version=protocol_version,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )

    def record_input_manifest(
        self,
        session_id: int,
        *,
        manifest: InputManifest,
        model_configuration: Mapping[str, Any] | None = None,
    ) -> PlanningProtocolInput:
        """Persist the complete immutable manifest in the 28B input envelope."""

        session = self._get_session(session_id)
        if session.protocol_version != PROTOCOL_V2:
            raise ProtocolPersistenceError(
                "input manifests participate only in Protocol v2"
            )
        try:
            manifest.validate()
        except InputManifestValidationError as exc:
            raise ProtocolPersistenceError(str(exc)) from exc
        if manifest.protocol_version != session.protocol_version:
            raise ProtocolPersistenceError(
                "manifest protocol version does not match session"
            )
        if manifest.generation_identity.session_id != session.id:
            raise ProtocolPersistenceError("manifest ownership does not match session")
        if manifest.generation_identity.session_generation_id != session.generation_id:
            raise ProtocolOwnershipError("manifest generation does not match session")
        configuration = dict(
            model_configuration
            or {
                "model": manifest.configuration_identity.model,
                "reasoning_profile": manifest.configuration_identity.reasoning_profile,
                "configuration_fingerprint": manifest.configuration_identity.stage_configuration_fingerprint,
            }
        )
        safe_configuration = _safe_model_configuration(configuration)
        context_identity = (
            manifest.engineering_context_identity.object_id
            or manifest.engineering_context_identity.selection_reason
        )
        repository_identity = (
            manifest.repository_identity.identity
            or manifest.repository_identity.omission_reason
            or "repository_unavailable"
        )
        existing = (
            self.db.query(PlanningProtocolInput)
            .filter(PlanningProtocolInput.planning_session_id == session.id)
            .one_or_none()
        )
        if existing is not None:
            if (
                existing.manifest_hash != manifest.manifest_hash
                or existing.manifest_json != manifest.to_dict()
            ):
                raise ProtocolPersistenceError("planning input manifest is immutable")
            return existing

        record = PlanningProtocolInput(
            planning_session_id=session.id,
            protocol_version=manifest.protocol_version,
            session_generation_id=session.generation_id,
            input_hash=manifest.manifest_hash,
            engineering_context_identity=context_identity[:512],
            provider_identity=manifest.configuration_identity.provider[:255],
            model_configuration=safe_configuration,
            repository_identity=repository_identity[:512],
            manifest_id=manifest.manifest_id,
            manifest_schema_version=manifest.schema_version,
            manifest_hash=manifest.manifest_hash,
            manifest_json=manifest.to_dict(),
        )
        self.db.add(record)
        self.db.flush()
        return record

    def record_input_identity(
        self,
        session_id: int,
        *,
        planning_input: str,
        engineering_context_identity: str,
        provider_identity: str,
        model_configuration: Mapping[str, Any],
        repository_identity: str,
        protocol_version: str | None = None,
        session_generation_id: str | None = None,
    ) -> PlanningProtocolInput:
        """Compatibility adapter that creates the canonical v2 manifest."""

        session = self._get_session(session_id)
        protocol = _normalize_protocol_version(
            protocol_version or session.protocol_version
        )
        if session.protocol_version != protocol:
            raise ProtocolPersistenceError("protocol version does not match session")
        generation = _normalize_required(
            session_generation_id or session.generation_id,
            "session_generation_id",
            128,
        )
        if session.generation_id != generation:
            raise ProtocolOwnershipError(
                "session generation does not match input identity"
            )
        planning_input = _normalize_required(
            planning_input, "planning_input", 1_000_000
        )
        context_identity = _normalize_required(
            engineering_context_identity, "engineering_context_identity", 512
        )
        provider = _normalize_required(provider_identity, "provider_identity", 255)
        repository = _normalize_required(
            repository_identity, "repository_identity", 512
        )
        model_config = _safe_model_configuration(model_configuration)
        if protocol != PROTOCOL_V2:
            # Preserve the Phase 28B compatibility surface for callers that
            # explicitly use the old helper outside Protocol v2 execution.
            identity_payload = {
                "planning_input_hash": hashlib.sha256(
                    planning_input.encode("utf-8", errors="surrogateescape")
                ).hexdigest(),
                "engineering_context_identity": context_identity,
                "provider_identity": provider,
                "model_configuration": model_config,
                "protocol_version": protocol,
                "repository_identity": repository,
            }
            input_hash = _sha256_json(identity_payload)
            existing = (
                self.db.query(PlanningProtocolInput)
                .filter(PlanningProtocolInput.planning_session_id == session.id)
                .one_or_none()
            )
            if existing is not None:
                if existing.input_hash != input_hash:
                    raise ProtocolPersistenceError(
                        "planning input identity is immutable"
                    )
                return existing
            record = PlanningProtocolInput(
                planning_session_id=session.id,
                protocol_version=protocol,
                session_generation_id=generation,
                input_hash=input_hash,
                engineering_context_identity=context_identity,
                provider_identity=provider,
                model_configuration=model_config,
                repository_identity=repository,
            )
            self.db.add(record)
            self.db.flush()
            return record
        manifest = InputManifestBuilder.from_compatibility_identity(
            session_id=session.id,
            session_generation_id=generation,
            planning_input_hash=hashlib.sha256(
                planning_input.encode("utf-8", errors="surrogateescape")
            ).hexdigest(),
            engineering_context_identity=context_identity,
            provider_identity=provider,
            model_configuration=model_config,
            repository_identity=repository,
        )
        return self.record_input_manifest(
            session.id,
            manifest=manifest,
            model_configuration=model_config,
        )

    def load_input_manifest(self, session_id: int) -> InputManifest | None:
        """Reload and verify the persisted manifest; never rebuild from live state."""

        session = self._get_session(session_id)
        if session.protocol_version != PROTOCOL_V2:
            return None
        record = (
            self.db.query(PlanningProtocolInput)
            .filter(PlanningProtocolInput.planning_session_id == session.id)
            .one_or_none()
        )
        if record is None or not record.manifest_json or not record.manifest_hash:
            raise ProtocolPersistenceError(
                "Protocol v2 session has no persisted input manifest"
            )
        try:
            manifest = InputManifest.from_dict(record.manifest_json)
        except InputManifestValidationError as exc:
            raise ProtocolPersistenceError(
                f"invalid persisted input manifest: {exc}"
            ) from exc
        if record.protocol_version != session.protocol_version:
            raise ProtocolPersistenceError("persisted input envelope protocol mismatch")
        if record.session_generation_id != session.generation_id:
            raise ProtocolOwnershipError("persisted input envelope owner mismatch")
        if manifest.manifest_hash != record.manifest_hash:
            raise ProtocolPersistenceError("persisted input manifest hash mismatch")
        if record.input_hash != manifest.manifest_hash:
            raise ProtocolPersistenceError(
                "compatibility input hash diverges from manifest"
            )
        if manifest.schema_version not in {
            "protocol-v2-input-manifest/1.0",
        }:
            raise ProtocolPersistenceError(
                "unsupported persisted input manifest schema"
            )
        if manifest.protocol_version != session.protocol_version:
            raise ProtocolPersistenceError("persisted input manifest protocol mismatch")
        if manifest.generation_identity.session_id != session.id:
            raise ProtocolOwnershipError("persisted input manifest owner mismatch")
        if manifest.generation_identity.session_generation_id != session.generation_id:
            raise ProtocolOwnershipError("persisted input manifest generation mismatch")
        if record.manifest_id != manifest.manifest_id:
            raise ProtocolPersistenceError("persisted input manifest identity mismatch")
        return manifest

    def record_checkpoint(
        self,
        session_id: int,
        *,
        stage_name: str,
        content: str,
        stage_generation_id: str | None = None,
        attempt_id: str | None = None,
        fencing_token: str | None = None,
        session_generation_id: str | None = None,
        protocol_version: str | None = None,
        checkpoint_version: int = 1,
        status: str = "accepted",
        parent_checkpoint_ids: Sequence[int] = (),
        failure_reason: str | None = None,
        accepted_at: datetime | None = None,
        invalidated_at: datetime | None = None,
    ) -> PlanningCheckpoint:
        """Append one checkpoint and its parent edges under the current fence."""

        session = self._assert_owner(
            session_id,
            protocol_version=protocol_version,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )
        stage = _normalize_required(stage_name, "stage_name", 100)
        if checkpoint_version < 1:
            raise ProtocolPersistenceError("checkpoint_version must be positive")
        checkpoint_status = str(status or "").strip().lower()
        if checkpoint_status not in CHECKPOINT_STATUSES:
            raise ProtocolPersistenceError("invalid checkpoint status")
        if checkpoint_status != "accepted" and accepted_at is not None:
            raise ProtocolPersistenceError(
                "only accepted checkpoints may have accepted_at"
            )
        if checkpoint_status != "invalidated" and invalidated_at is not None:
            raise ProtocolPersistenceError(
                "only invalidated checkpoints may have invalidated_at"
            )
        stage_generation = _normalize_required(
            stage_generation_id or str(uuid.uuid4()), "stage_generation_id", 128
        )
        attempt = _normalize_required(
            attempt_id or str(uuid.uuid4()), "attempt_id", 128
        )
        checkpoint_content = str(content or "")
        now = _now()
        accepted_timestamp = accepted_at or (
            now if checkpoint_status == "accepted" else None
        )
        invalidated_timestamp = invalidated_at or (
            now if checkpoint_status == "invalidated" else None
        )
        parent_ids = tuple(
            dict.fromkeys(int(parent_id) for parent_id in parent_checkpoint_ids)
        )

        parents = []
        if parent_ids:
            parents = (
                self.db.query(PlanningCheckpoint)
                .filter(PlanningCheckpoint.id.in_(parent_ids))
                .all()
            )
            if len(parents) != len(parent_ids):
                raise ProtocolPersistenceError("checkpoint dependency does not exist")
            if any(parent.planning_session_id != session.id for parent in parents):
                raise ProtocolPersistenceError("checkpoint dependency crosses sessions")
            if any(
                parent.protocol_version != session.protocol_version
                for parent in parents
            ):
                raise ProtocolPersistenceError(
                    "checkpoint dependency crosses protocols"
                )

        checkpoint = PlanningCheckpoint(
            planning_session_id=session.id,
            stage_name=stage,
            checkpoint_version=checkpoint_version,
            protocol_version=session.protocol_version,
            session_generation_id=session.generation_id,
            stage_generation_id=stage_generation,
            attempt_id=attempt,
            fencing_token=_normalize_required(
                fencing_token or session.processing_token, "fencing_token", 128
            ),
            status=checkpoint_status,
            content_hash=hashlib.sha256(
                checkpoint_content.encode("utf-8", errors="surrogateescape")
            ).hexdigest(),
            content=checkpoint_content,
            accepted_at=accepted_timestamp,
            failure_reason=failure_reason,
            invalidated_at=invalidated_timestamp,
        )
        self.db.add(checkpoint)
        self.db.flush()
        self.db.add_all(
            [
                PlanningCheckpointDependency(
                    checkpoint_id=checkpoint.id,
                    parent_checkpoint_id=parent.id,
                )
                for parent in parents
            ]
        )
        self.db.flush()
        return checkpoint

    def list_checkpoints(self, session_id: int) -> list[PlanningCheckpoint]:
        """Read checkpoints in append order for deterministic recovery."""

        session = self._get_session(session_id)
        return (
            self.db.query(PlanningCheckpoint)
            .filter(
                PlanningCheckpoint.planning_session_id == session.id,
                PlanningCheckpoint.protocol_version == session.protocol_version,
                PlanningCheckpoint.session_generation_id == session.generation_id,
            )
            .order_by(PlanningCheckpoint.id.asc())
            .all()
        )

    def effective_checkpoints(
        self,
        session_id: int,
        *,
        stage_versions: Mapping[str, int] | None = None,
    ) -> dict[tuple[str, int], PlanningCheckpoint]:
        """Return the latest append-only record for each stage/version pair."""

        effective: dict[tuple[str, int], PlanningCheckpoint] = {}
        for checkpoint in self.list_checkpoints(session_id):
            if stage_versions is not None:
                expected_version = stage_versions.get(checkpoint.stage_name)
                if expected_version is None or checkpoint.checkpoint_version != int(
                    expected_version
                ):
                    continue
            effective[(checkpoint.stage_name, checkpoint.checkpoint_version)] = (
                checkpoint
            )
        return effective

    def accepted_predecessors(
        self,
        session_id: int,
        *,
        stage_versions: Mapping[str, int],
    ) -> dict[str, PlanningCheckpoint]:
        """Load accepted predecessor checkpoints in stable stage-name order."""

        effective = self.effective_checkpoints(
            session_id, stage_versions=stage_versions
        )
        return {
            stage_name: effective[(stage_name, int(stage_versions[stage_name]))]
            for stage_name in sorted(stage_versions)
            if (stage_name, int(stage_versions[stage_name])) in effective
            and effective[(stage_name, int(stage_versions[stage_name]))].status
            == "accepted"
        }

    def invalidate_checkpoints(
        self,
        session_id: int,
        *,
        stage_names: Sequence[str],
        reason: str,
        fencing_token: str | None = None,
        session_generation_id: str | None = None,
        protocol_version: str | None = None,
    ) -> list[PlanningCheckpoint]:
        """Append invalidation attempts for the current downstream records.

        Existing checkpoints remain immutable.  The latest record for each
        affected stage/version becomes authoritative, so the invalidation is
        visible to recovery and completion evaluation without erasing audit
        history.
        """

        session = self._assert_owner(
            session_id,
            protocol_version=protocol_version,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )
        names = {str(name).strip() for name in stage_names if str(name).strip()}
        if not names:
            return []
        invalidated: list[PlanningCheckpoint] = []
        effective = self.effective_checkpoints(session.id)
        for key in sorted(effective):
            checkpoint = effective[key]
            if checkpoint.stage_name not in names or checkpoint.status == "invalidated":
                continue
            parent_ids = [
                edge.parent_checkpoint_id
                for edge in sorted(
                    checkpoint.dependencies, key=lambda item: item.parent_checkpoint_id
                )
            ]
            invalidated.append(
                self.record_checkpoint(
                    session.id,
                    stage_name=checkpoint.stage_name,
                    checkpoint_version=checkpoint.checkpoint_version,
                    content=checkpoint.content,
                    stage_generation_id=checkpoint.stage_generation_id,
                    fencing_token=fencing_token,
                    session_generation_id=session_generation_id,
                    protocol_version=protocol_version,
                    status="invalidated",
                    parent_checkpoint_ids=parent_ids,
                    failure_reason=str(reason or "dependency changed"),
                )
            )
        return invalidated

    def record_completion_manifest(
        self,
        session_id: int,
        *,
        accepted_checkpoint_versions: Sequence[Mapping[str, Any]],
        dependency_hashes: Sequence[str],
        fencing_token: str | None = None,
        session_generation_id: str | None = None,
        protocol_version: str | None = None,
    ) -> PlanningCompletionManifest:
        """Persist the one immutable completion attestation for a session."""

        session = self._assert_owner(
            session_id,
            protocol_version=protocol_version,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )
        normalized_versions: list[dict[str, Any]] = []
        seen_stage_versions: set[tuple[str, int]] = set()
        for raw_version in accepted_checkpoint_versions:
            if not isinstance(raw_version, Mapping):
                raise ProtocolPersistenceError(
                    "accepted checkpoint version must be a mapping"
                )
            checkpoint_id = int(raw_version.get("checkpoint_id", 0))
            checkpoint = self.db.get(PlanningCheckpoint, checkpoint_id)
            if checkpoint is None or checkpoint.planning_session_id != session.id:
                raise ProtocolPersistenceError(
                    "accepted checkpoint does not belong to session"
                )
            if checkpoint.status != "accepted":
                raise ProtocolPersistenceError(
                    "completion manifest requires accepted checkpoints"
                )
            stage_version = (checkpoint.stage_name, checkpoint.checkpoint_version)
            if stage_version in seen_stage_versions:
                raise ProtocolPersistenceError(
                    "completion manifest repeats a stage version"
                )
            seen_stage_versions.add(stage_version)
            normalized_versions.append(
                {
                    "checkpoint_id": checkpoint.id,
                    "stage_name": checkpoint.stage_name,
                    "checkpoint_version": checkpoint.checkpoint_version,
                    "content_hash": checkpoint.content_hash,
                }
            )
        normalized_hashes = sorted(
            {_normalize_hash(value, "dependency_hash") for value in dependency_hashes}
        )
        manifest_payload = {
            "accepted_checkpoint_versions": normalized_versions,
            "dependency_hashes": normalized_hashes,
            "protocol_version": session.protocol_version,
            "session_generation_id": session.generation_id,
        }
        manifest_hash = _sha256_json(manifest_payload)
        existing = (
            self.db.query(PlanningCompletionManifest)
            .filter(PlanningCompletionManifest.planning_session_id == session.id)
            .one_or_none()
        )
        if existing is not None:
            if existing.manifest_hash != manifest_hash:
                raise ProtocolPersistenceError("completion manifest is immutable")
            return existing

        manifest = PlanningCompletionManifest(
            planning_session_id=session.id,
            protocol_version=session.protocol_version,
            session_generation_id=session.generation_id,
            accepted_checkpoint_versions=normalized_versions,
            dependency_hashes=normalized_hashes,
            manifest_hash=manifest_hash,
        )
        self.db.add(manifest)
        self.db.flush()
        return manifest

    def record_commit_manifest(
        self,
        session_id: int,
        *,
        task_provenance: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        commit_identity: str | None = None,
        completion_manifest_id: int | None = None,
        plan_id: int | None = None,
        fencing_token: str | None = None,
        session_generation_id: str | None = None,
        protocol_version: str | None = None,
    ) -> PlanningCommitManifest:
        """Persist a future commit identity without changing current commit."""

        session = self._assert_owner(
            session_id,
            protocol_version=protocol_version,
            session_generation_id=session_generation_id,
            fencing_token=fencing_token,
        )
        if not isinstance(task_provenance, (Mapping, list, tuple)):
            raise ProtocolPersistenceError("task_provenance must be JSON-shaped")
        provenance = (
            list(task_provenance)
            if isinstance(task_provenance, tuple)
            else task_provenance
        )
        _canonical_json(provenance)
        completion_manifest = None
        if completion_manifest_id is not None:
            completion_manifest = self.db.get(
                PlanningCompletionManifest, completion_manifest_id
            )
            if (
                completion_manifest is None
                or completion_manifest.planning_session_id != session.id
            ):
                raise ProtocolPersistenceError(
                    "completion manifest does not belong to session"
                )
        identity_payload = {
            "completion_manifest_id": completion_manifest_id,
            "plan_id": plan_id,
            "protocol_version": session.protocol_version,
            "session_generation_id": session.generation_id,
            "task_provenance": provenance,
        }
        identity = _normalize_required(
            commit_identity or _sha256_json(identity_payload), "commit_identity", 128
        )
        existing = (
            self.db.query(PlanningCommitManifest)
            .filter(PlanningCommitManifest.commit_identity == identity)
            .one_or_none()
        )
        if existing is not None:
            if existing.planning_session_id != session.id:
                raise ProtocolPersistenceError(
                    "commit identity belongs to another session"
                )
            if (
                existing.completion_manifest_id != completion_manifest_id
                or existing.plan_id != plan_id
                or existing.protocol_version != session.protocol_version
                or existing.session_generation_id != session.generation_id
                or existing.task_provenance != provenance
            ):
                raise ProtocolPersistenceError("commit manifest is immutable")
            return existing

        manifest = PlanningCommitManifest(
            planning_session_id=session.id,
            completion_manifest_id=completion_manifest_id,
            plan_id=plan_id,
            protocol_version=session.protocol_version,
            session_generation_id=session.generation_id,
            commit_identity=identity,
            task_provenance=provenance,
        )
        self.db.add(manifest)
        self.db.flush()
        return manifest

    def recovery_state(self, session_id: int) -> dict[str, Any]:
        """Return durable protocol state for a future stage recovery worker."""

        session = self._get_session(session_id)
        input_manifest = self.load_input_manifest(session_id)
        checkpoints = (
            self.db.query(PlanningCheckpoint)
            .filter(PlanningCheckpoint.planning_session_id == session.id)
            .order_by(PlanningCheckpoint.id.asc())
            .all()
        )
        return {
            "session_id": session.id,
            "protocol_version": session.protocol_version,
            "session_generation_id": session.generation_id,
            "input": session.protocol_input,
            "input_manifest": input_manifest,
            "checkpoints": checkpoints,
            "effective_checkpoints": self.effective_checkpoints(session.id),
            "completion_manifest": session.completion_manifest,
            "commit_manifests": list(session.commit_manifests),
        }


# Short alias for callers that prefer the generic service name.
ProtocolPersistenceService = PlanningProtocolPersistenceService
