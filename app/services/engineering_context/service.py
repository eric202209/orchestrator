"""Deterministic Engineering Context for one operator-approved subsystem.

This module deliberately keeps Registration, Generation, Verification, and
Selection together.  The bounded Phase 27G slice has one registration and a
file-backed immutable object store; Planning only calls ``select`` and never
calls a lifecycle-mutating method.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

logger = logging.getLogger(__name__)

DEFAULT_SUBSYSTEM_ID = "project-log-authorization"
DEFAULT_SUBSYSTEM_VERSION = 1
DEFAULT_REGISTRY_PATH = Path(__file__).with_name("registrations.json")
STORAGE_DIR_NAME = ".agent/engineering-context"
SCHEMA_VERSION = 1


class RegistrationError(ValueError):
    """The operator-owned registration file is structurally invalid."""


class EngineeringContextError(RuntimeError):
    """A deterministic lifecycle operation failed closed."""


class ObjectIdentityConflict(EngineeringContextError):
    """An immutable object path already contains a different object."""


@dataclass(frozen=True)
class SubsystemRegistration:
    repository_identity: str
    subsystem_id: str
    subsystem_version: int
    scope: tuple[str, ...]
    triggers: tuple[str, ...]
    status: str
    provenance: Mapping[str, Any]
    created_at: str
    retired_at: str | None = None

    @property
    def identity(self) -> str:
        return (
            f"{self.repository_identity}:{self.subsystem_id}:"
            f"{self.subsystem_version}"
        )

    @property
    def is_live(self) -> bool:
        return self.status == "live" and not self.retired_at

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SubsystemRegistration":
        required = {
            "repository_identity",
            "subsystem_id",
            "subsystem_version",
            "scope",
            "triggers",
            "status",
            "provenance",
            "created_at",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise RegistrationError(f"missing_required_field:{','.join(missing)}")

        repository_identity = _normalize_repository_identity(raw["repository_identity"])
        subsystem_id = str(raw["subsystem_id"]).strip()
        version = raw["subsystem_version"]
        if not repository_identity or not subsystem_id:
            raise RegistrationError("empty_registration_identity")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise RegistrationError("invalid_subsystem_version")

        scope = _validate_scope(raw["scope"])
        triggers = _validate_triggers(raw["triggers"])
        status = str(raw["status"]).strip().lower()
        if status not in {"live", "retired"}:
            raise RegistrationError("invalid_registration_status")
        provenance = raw["provenance"]
        if not isinstance(provenance, Mapping) or not provenance:
            raise RegistrationError("invalid_registration_provenance")
        created_at = str(raw["created_at"]).strip()
        if not created_at:
            raise RegistrationError("missing_creation_timestamp")
        retired_at = raw.get("retired_at")
        if retired_at is not None and not str(retired_at).strip():
            raise RegistrationError("invalid_retirement_timestamp")

        return cls(
            repository_identity=repository_identity,
            subsystem_id=subsystem_id,
            subsystem_version=version,
            scope=scope,
            triggers=triggers,
            status=status,
            provenance=dict(provenance),
            created_at=created_at,
            retired_at=str(retired_at) if retired_at is not None else None,
        )


@dataclass(frozen=True)
class EngineeringContextObject:
    object_id: str
    repository_identity: str
    subsystem_id: str
    subsystem_version: int
    commit_fingerprint: str
    commit_sha: str
    scope: tuple[str, ...]
    raw_source_content: Mapping[str, str]
    per_file_hash: Mapping[str, str]
    provenance: Mapping[str, Any]
    generated_at: str
    verification_status: str
    publication_state: str

    @property
    def total_source_bytes(self) -> int:
        return sum(
            len(base64.b64decode(value)) for value in self.raw_source_content.values()
        )

    @property
    def identity(self) -> str:
        return self.object_id

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EngineeringContextObject":
        required = {
            "schema_version",
            "object_id",
            "repository_identity",
            "subsystem_id",
            "subsystem_version",
            "commit_fingerprint",
            "commit_sha",
            "scope",
            "raw_source_content",
            "per_file_hash",
            "provenance",
            "generated_at",
            "verification_status",
            "publication_state",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise EngineeringContextError(
                f"malformed_object:missing_required_field:{','.join(missing)}"
            )
        if raw["schema_version"] != SCHEMA_VERSION:
            raise EngineeringContextError("malformed_object:schema_version")

        scope = _validate_scope(raw["scope"])
        raw_content = raw["raw_source_content"]
        hashes = raw["per_file_hash"]
        if not isinstance(raw_content, Mapping) or set(raw_content) != set(scope):
            raise EngineeringContextError("malformed_object:scope_content_mismatch")
        if not isinstance(hashes, Mapping) or set(hashes) != set(scope):
            raise EngineeringContextError("malformed_object:scope_hash_mismatch")
        decoded: dict[str, str] = {}
        for path in scope:
            value = raw_content[path]
            digest = str(hashes[path])
            if not isinstance(value, str) or not _is_sha256(digest):
                raise EngineeringContextError("malformed_object:content_or_hash")
            try:
                decoded[path] = base64.b64decode(value, validate=True).decode(
                    "utf-8", errors="surrogateescape"
                )
            except (ValueError, UnicodeError) as exc:
                raise EngineeringContextError(
                    "malformed_object:invalid_base64"
                ) from exc
            if (
                _sha256(decoded[path].encode("utf-8", errors="surrogateescape"))
                != digest
            ):
                raise EngineeringContextError("malformed_object:content_hash_mismatch")

        version = raw["subsystem_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise EngineeringContextError("malformed_object:subsystem_version")
        status = str(raw["verification_status"])
        publication = str(raw["publication_state"])
        if status not in {"unverified", "verified", "failed"}:
            raise EngineeringContextError("malformed_object:verification_status")
        if publication not in {
            "candidate",
            "verified_candidate",
            "published",
            "failed",
        }:
            raise EngineeringContextError("malformed_object:publication_state")
        if publication == "published" and status != "verified":
            raise EngineeringContextError("malformed_object:published_unverified")

        object_id = str(raw["object_id"]).strip()
        if not object_id:
            raise EngineeringContextError("malformed_object:object_id")
        provenance = raw["provenance"]
        if not isinstance(provenance, Mapping):
            raise EngineeringContextError("malformed_object:provenance")
        return cls(
            object_id=object_id,
            repository_identity=_normalize_repository_identity(
                raw["repository_identity"]
            ),
            subsystem_id=str(raw["subsystem_id"]),
            subsystem_version=version,
            commit_fingerprint=str(raw["commit_fingerprint"]),
            commit_sha=str(raw["commit_sha"]),
            scope=scope,
            raw_source_content=dict(raw_content),
            per_file_hash=dict(hashes),
            provenance=dict(provenance),
            generated_at=str(raw["generated_at"]),
            verification_status=status,
            publication_state=publication,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "object_id": self.object_id,
            "repository_identity": self.repository_identity,
            "subsystem_id": self.subsystem_id,
            "subsystem_version": self.subsystem_version,
            "commit_fingerprint": self.commit_fingerprint,
            "commit_sha": self.commit_sha,
            "scope": list(self.scope),
            "raw_source_content": dict(self.raw_source_content),
            "per_file_hash": dict(self.per_file_hash),
            "provenance": dict(self.provenance),
            "generated_at": self.generated_at,
            "verification_status": self.verification_status,
            "publication_state": self.publication_state,
        }


@dataclass(frozen=True)
class EngineeringContextSelection:
    context: EngineeringContextObject | None
    reason: str
    matched_trigger: str | None
    diagnostics: Mapping[str, Any]

    @property
    def supplied(self) -> bool:
        return self.context is not None


@dataclass(frozen=True)
class _RepositorySnapshot:
    repository_identity: str
    commit_sha: str
    scope: tuple[str, ...]
    raw_source_content: Mapping[str, str]
    per_file_hash: Mapping[str, str]
    commit_fingerprint: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _normalize_repository_identity(value: Any) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.endswith("/"):
        normalized = normalized[:-1]
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.lower()


def _validate_scope(raw_scope: Any) -> tuple[str, ...]:
    if not isinstance(raw_scope, (list, tuple)) or not raw_scope:
        raise RegistrationError("invalid_file_scope")
    normalized: list[str] = []
    for raw_path in raw_scope:
        value = str(raw_path).strip().replace("\\", "/")
        path = Path(value)
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or value.startswith("./")
        ):
            raise RegistrationError(f"invalid_file_scope:{value}")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise RegistrationError("duplicate_file_scope")
    return tuple(sorted(normalized))


def _validate_triggers(raw_triggers: Any) -> tuple[str, ...]:
    if not isinstance(raw_triggers, (list, tuple)) or not raw_triggers:
        raise RegistrationError("invalid_deterministic_triggers")
    values = tuple(str(value).strip() for value in raw_triggers)
    if any(not value for value in values):
        raise RegistrationError("empty_deterministic_trigger")
    if len(set(value.casefold() for value in values)) != len(values):
        raise RegistrationError("duplicate_deterministic_trigger")
    return values


def _run_git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def repository_identity(root: Path) -> str:
    root = Path(root).resolve()
    remote = ""
    try:
        remote = _run_git(root, "config", "--get", "remote.origin.url")
    except (OSError, subprocess.SubprocessError):
        remote = ""
    return _normalize_repository_identity(remote or f"path:{root}")


class _ContextFileStore:
    """Atomic immutable JSON files under the repository's ignored .agent tree."""

    def __init__(self, repository_root: Path):
        self.directory = Path(repository_root).resolve() / STORAGE_DIR_NAME
        self.lock_path = self.directory / ".lock"

    def _ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o777)
        if not self.lock_path.exists():
            self.lock_path.touch()
        self.lock_path.chmod(0o666)

    @contextmanager
    def locked(self) -> Iterator[None]:
        self._ensure_directory()
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise EngineeringContextError("malformed_object:not_an_object")
        return raw

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o777)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o666)
            os.replace(temporary, path)
            path.chmod(0o666)
        finally:
            temporary.unlink(missing_ok=True)

    def _candidate_path(self, object_id: str) -> Path:
        return self.directory / f"{object_id}.candidate.json"

    def _published_path(self, object_id: str) -> Path:
        return self.directory / f"{object_id}.published.json"

    def save_candidate(self, context: EngineeringContextObject) -> None:
        path = self._candidate_path(context.object_id)
        with self.locked():
            if path.exists():
                existing = EngineeringContextObject.from_dict(self._read(path))
                if _immutable_object_identity(existing) != _immutable_object_identity(
                    context
                ):
                    raise ObjectIdentityConflict(context.object_id)
                if existing.publication_state in {"candidate", "verified_candidate"}:
                    return
                # A failed or previously verified candidate may be retried for
                # the same immutable source identity. Published objects remain
                # separately immutable under their .published path.
                self._atomic_write(path, context.to_dict())
                return
            self._atomic_write(path, context.to_dict())

    def read_candidate(self, object_id: str) -> EngineeringContextObject:
        with self.locked():
            return EngineeringContextObject.from_dict(
                self._read(self._candidate_path(object_id))
            )

    def update_candidate(
        self, context: EngineeringContextObject, *, state: str, verification: str
    ) -> EngineeringContextObject:
        updated = copy.copy(context)
        updated = EngineeringContextObject(
            **{
                **updated.__dict__,
                "publication_state": state,
                "verification_status": verification,
            }
        )
        with self.locked():
            self._atomic_write(
                self._candidate_path(context.object_id), updated.to_dict()
            )
        return updated

    def publish(self, context: EngineeringContextObject) -> EngineeringContextObject:
        if context.publication_state != "verified_candidate":
            raise EngineeringContextError("publication_requires_verified_candidate")
        published = EngineeringContextObject(
            **{
                **context.__dict__,
                "publication_state": "published",
                "verification_status": "verified",
            }
        )
        path = self._published_path(context.object_id)
        with self.locked():
            if path.exists():
                existing = EngineeringContextObject.from_dict(self._read(path))
                if _immutable_object_identity(existing) != _immutable_object_identity(
                    published
                ):
                    raise ObjectIdentityConflict(context.object_id)
                return existing
            self._atomic_write(path, published.to_dict())
        return published

    def mark_candidate_failed(self, object_id: str, reason: str) -> None:
        try:
            with self.locked():
                path = self._candidate_path(object_id)
                if not path.exists():
                    return
                context = EngineeringContextObject.from_dict(self._read(path))
                failed = EngineeringContextObject(
                    **{
                        **context.__dict__,
                        "publication_state": "failed",
                        "verification_status": "failed",
                        "provenance": {
                            **dict(context.provenance),
                            "failure_category": reason,
                        },
                    }
                )
                self._atomic_write(path, failed.to_dict())
        except (OSError, ValueError, EngineeringContextError):
            logger.warning(
                "[ENGINEERING_CONTEXT] failed to mark candidate failed object_id=%s",
                object_id,
            )

    def list_published(self) -> list[EngineeringContextObject]:
        if not self.directory.exists():
            return []
        objects: list[EngineeringContextObject] = []
        for path in sorted(self.directory.glob("*.published.json")):
            objects.append(EngineeringContextObject.from_dict(self._read(path)))
        return objects


def _immutable_object_identity(context: EngineeringContextObject) -> tuple[Any, ...]:
    return (
        context.object_id,
        context.repository_identity,
        context.subsystem_id,
        context.subsystem_version,
        context.commit_fingerprint,
        context.commit_sha,
        context.scope,
        tuple(sorted(context.per_file_hash.items())),
        tuple(sorted(context.raw_source_content.items())),
    )


class EngineeringContextService:
    """Operator lifecycle and read-only Planning selection for Phase 27G."""

    def __init__(self, *, registry_path: Path | None = None):
        self.registry_path = Path(registry_path or DEFAULT_REGISTRY_PATH)

    def load_registrations(self) -> tuple[SubsystemRegistration, ...]:
        try:
            with self.registry_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistrationError(
                f"registry_unavailable:{exc.__class__.__name__}"
            ) from exc
        if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION:
            raise RegistrationError("invalid_registry_schema")
        entries = raw.get("registrations")
        if not isinstance(entries, list):
            raise RegistrationError("invalid_registry_entries")
        registrations = tuple(
            SubsystemRegistration.from_dict(entry) for entry in entries
        )
        live_ids: set[str] = set()
        for registration in registrations:
            if registration.is_live:
                if registration.identity in live_ids:
                    raise RegistrationError(
                        f"duplicate_live_registration:{registration.identity}"
                    )
                live_ids.add(registration.identity)
        return registrations

    def _registration_for_generation(
        self,
        root: Path,
        subsystem_id: str,
        subsystem_version: int,
    ) -> SubsystemRegistration | None:
        identity = repository_identity(root)
        matches = [
            registration
            for registration in self.load_registrations()
            if registration.is_live
            and registration.repository_identity == identity
            and registration.subsystem_id == subsystem_id
            and registration.subsystem_version == subsystem_version
        ]
        if len(matches) > 1:
            raise RegistrationError("ambiguous_generation_registration")
        return matches[0] if matches else None

    def _snapshot(
        self, root: Path, registration: SubsystemRegistration
    ) -> _RepositorySnapshot:
        root = Path(root).resolve()
        identity = repository_identity(root)
        if identity != registration.repository_identity:
            raise EngineeringContextError("repository_identity_mismatch")
        try:
            commit_sha = _run_git(root, "rev-parse", "HEAD")
        except (OSError, subprocess.SubprocessError):
            commit_sha = "NO_COMMIT"
        raw_content: dict[str, str] = {}
        hashes: dict[str, str] = {}
        for relative_path in registration.scope:
            source_path = (root / relative_path).resolve()
            try:
                source_path.relative_to(root)
            except ValueError as exc:
                raise EngineeringContextError("invalid_registered_path") from exc
            try:
                source_bytes = source_path.read_bytes()
            except FileNotFoundError as exc:
                raise EngineeringContextError(
                    f"missing_scoped_file:{relative_path}"
                ) from exc
            except OSError as exc:
                raise EngineeringContextError(
                    f"scoped_file_unreadable:{relative_path}"
                ) from exc
            raw_content[relative_path] = base64.b64encode(source_bytes).decode("ascii")
            hashes[relative_path] = _sha256(source_bytes)
        fingerprint_input = {
            "repository_identity": identity,
            "commit_sha": commit_sha,
            "scope": list(registration.scope),
            "per_file_hash": hashes,
        }
        commit_fingerprint = "sha256:" + _sha256(
            json.dumps(fingerprint_input, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        return _RepositorySnapshot(
            repository_identity=identity,
            commit_sha=commit_sha,
            scope=registration.scope,
            raw_source_content=raw_content,
            per_file_hash=hashes,
            commit_fingerprint=commit_fingerprint,
        )

    def generate_and_verify(
        self,
        repository_root: Path,
        *,
        subsystem_id: str = DEFAULT_SUBSYSTEM_ID,
        subsystem_version: int = DEFAULT_SUBSYSTEM_VERSION,
        origin: str = "operator_bootstrap",
    ) -> dict[str, Any]:
        """Generate, structurally verify, and atomically publish one object.

        This is the explicit operator bootstrap callable and the callable used
        by the real Promotion hook.  Planning never calls it.
        """

        root = Path(repository_root).resolve()
        self._emit(
            "generation_started",
            repository_root=str(root),
            subsystem_id=subsystem_id,
            subsystem_version=subsystem_version,
            lifecycle_mutation_origin=origin,
        )
        try:
            registration = self._registration_for_generation(
                root, subsystem_id, subsystem_version
            )
            if registration is None:
                result = {"status": "skipped", "reason": "no_live_registration"}
                self._emit("generation_completed", **result)
                return result
            snapshot = self._snapshot(root, registration)
            object_id = _sha256(
                json.dumps(
                    {
                        "repository_identity": snapshot.repository_identity,
                        "subsystem_id": registration.subsystem_id,
                        "subsystem_version": registration.subsystem_version,
                        "commit_fingerprint": snapshot.commit_fingerprint,
                        "scope": list(snapshot.scope),
                        "per_file_hash": snapshot.per_file_hash,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            candidate = EngineeringContextObject(
                object_id=object_id,
                repository_identity=snapshot.repository_identity,
                subsystem_id=registration.subsystem_id,
                subsystem_version=registration.subsystem_version,
                commit_fingerprint=snapshot.commit_fingerprint,
                commit_sha=snapshot.commit_sha,
                scope=snapshot.scope,
                raw_source_content=snapshot.raw_source_content,
                per_file_hash=snapshot.per_file_hash,
                provenance={
                    **dict(registration.provenance),
                    "registration_identity": registration.identity,
                    "generated_by": "deterministic_repository_reader",
                    "generation_origin": origin,
                },
                generated_at=_now(),
                verification_status="unverified",
                publication_state="candidate",
            )
            store = _ContextFileStore(root)
            store.save_candidate(candidate)
            self._emit(
                "generation_candidate_created",
                object_id=object_id,
                files_supplied=len(candidate.scope),
                source_bytes=candidate.total_source_bytes,
            )
            verification = self.verify_candidate(root, object_id)
            if verification["status"] != "verified":
                result = {
                    "status": "failed",
                    "reason": verification["reason"],
                    "object_id": object_id,
                }
                self._emit("generation_failed", **result)
                return result
            published = store.publish(store.read_candidate(object_id))
            result = {
                "status": "published",
                "reason": "verified_and_published",
                "object_id": published.object_id,
                "commit_fingerprint": published.commit_fingerprint,
                "files_supplied": len(published.scope),
                "source_bytes": published.total_source_bytes,
            }
            self._emit("publication_result", **result)
            self._emit("generation_completed", **result)
            return result
        except RegistrationError as exc:
            result = {"status": "failed", "reason": str(exc)}
            self._emit("generation_failed", **result)
            return result
        except (OSError, EngineeringContextError, ValueError) as exc:
            result = {"status": "failed", "reason": str(exc)[:240]}
            self._emit("generation_failed", **result)
            return result

    def verify_candidate(self, repository_root: Path, object_id: str) -> dict[str, Any]:
        """Re-read all files and gate publication on exact identity/hash equality."""

        root = Path(repository_root).resolve()
        self._emit(
            "verification_started",
            object_id=object_id,
            lifecycle_mutation_origin="operator_or_promotion",
        )
        store = _ContextFileStore(root)
        try:
            candidate = store.read_candidate(object_id)
            if candidate.publication_state not in {"candidate", "verified_candidate"}:
                raise EngineeringContextError("candidate_not_unverified")
            registrations = self.load_registrations()
            registration = next(
                (
                    item
                    for item in registrations
                    if item.is_live
                    and item.repository_identity == candidate.repository_identity
                    and item.subsystem_id == candidate.subsystem_id
                    and item.subsystem_version == candidate.subsystem_version
                ),
                None,
            )
            if registration is None:
                raise EngineeringContextError("registration_missing_or_retired")
            if tuple(candidate.scope) != registration.scope:
                raise EngineeringContextError("scope_mismatch")
            current = self._snapshot(root, registration)
            if current.repository_identity != candidate.repository_identity:
                raise EngineeringContextError("repository_identity_mismatch")
            if current.commit_fingerprint != candidate.commit_fingerprint:
                raise EngineeringContextError("source_fingerprint_mismatch")
            if dict(current.per_file_hash) != dict(candidate.per_file_hash):
                raise EngineeringContextError("per_file_hash_mismatch")
            if dict(current.raw_source_content) != dict(candidate.raw_source_content):
                raise EngineeringContextError("raw_source_mismatch")
            verified = store.update_candidate(
                candidate, state="verified_candidate", verification="verified"
            )
            result = {
                "status": "verified",
                "reason": "exact_rehash_match",
                "object_id": verified.object_id,
                "files_supplied": len(verified.scope),
                "source_bytes": verified.total_source_bytes,
            }
            self._emit("verification_completed", **result)
            return result
        except (OSError, EngineeringContextError, RegistrationError, ValueError) as exc:
            reason = str(exc)[:240]
            store.mark_candidate_failed(object_id, reason)
            result = {"status": "failed", "reason": reason, "object_id": object_id}
            self._emit("verification_failed", **result)
            return result

    def select(
        self,
        repository_root: Path,
        *,
        task_title: str = "",
        task_text: str = "",
        subsystem_version: int | None = None,
    ) -> EngineeringContextSelection:
        """Select only an already-published, exact-fresh object."""

        root = Path(repository_root).resolve()
        query = f"{task_title}\n{task_text}"
        self._emit(
            "selection_attempted",
            repository_root=str(root),
            query_chars=len(query),
            lifecycle_mutation_origin="planning",
            lifecycle_mutation=False,
        )
        try:
            registrations = self.load_registrations()
        except RegistrationError as exc:
            return self._fallback("malformed_registration", None, str(exc))

        identity = repository_identity(root)
        matches: list[tuple[SubsystemRegistration, str]] = []
        retired_match = False
        identity_mismatch = False
        version_mismatch = False
        for registration in registrations:
            matched_trigger = _matched_trigger(registration.triggers, query)
            if matched_trigger is None:
                continue
            if not registration.is_live:
                retired_match = True
                continue
            if registration.repository_identity != identity:
                identity_mismatch = True
                continue
            if (
                subsystem_version is not None
                and registration.subsystem_version != subsystem_version
            ):
                version_mismatch = True
                continue
            matches.append((registration, matched_trigger))

        if len(matches) > 1:
            return self._fallback(
                "ambiguous_registration_match",
                None,
                "multiple live registrations matched deterministic triggers",
            )
        if not matches:
            if version_mismatch:
                return self._fallback("subsystem_version_mismatch", None, None)
            if retired_match:
                return self._fallback("retired_registration", None, None)
            if identity_mismatch:
                return self._fallback("repository_identity_mismatch", None, None)
            return self._fallback("no_subsystem_match", None, None)

        registration, matched_trigger = matches[0]
        try:
            current = self._snapshot(root, registration)
            objects = _ContextFileStore(root).list_published()
        except FileNotFoundError as exc:
            return self._fallback("missing_scoped_file", matched_trigger, str(exc))
        except OSError as exc:
            return self._fallback("storage_unavailable", matched_trigger, str(exc))
        except (EngineeringContextError, RegistrationError, ValueError) as exc:
            detail = str(exc)
            if detail.startswith("missing_scoped_file:"):
                return self._fallback("missing_scoped_file", matched_trigger, detail)
            if detail.startswith("scoped_file_unreadable:"):
                return self._fallback("scoped_file_unreadable", matched_trigger, detail)
            return self._fallback("malformed_object", matched_trigger, str(exc))

        matching_objects = [
            obj
            for obj in objects
            if obj.repository_identity == identity
            and obj.subsystem_id == registration.subsystem_id
            and obj.subsystem_version == registration.subsystem_version
        ]
        if not matching_objects:
            reason = "no_published_object"
            return self._fallback(reason, matched_trigger, None)
        fresh = [
            obj
            for obj in matching_objects
            if obj.publication_state == "published"
            and obj.verification_status == "verified"
            and obj.scope == registration.scope
            and obj.commit_fingerprint == current.commit_fingerprint
            and dict(obj.per_file_hash) == dict(current.per_file_hash)
        ]
        if not fresh:
            return self._fallback("stale_object", matched_trigger, None)

        selected = sorted(fresh, key=lambda obj: obj.object_id)[0]
        diagnostics = {
            "reason": "fresh_published_object",
            "matched_trigger": matched_trigger,
            "object_id": selected.object_id,
            "repository_identity": selected.repository_identity,
            "subsystem_id": selected.subsystem_id,
            "subsystem_version": selected.subsystem_version,
            "commit_fingerprint": selected.commit_fingerprint,
            "verification_status": selected.verification_status,
            "fresh": True,
            "files_supplied": len(selected.scope),
            "source_bytes": selected.total_source_bytes,
            "fallback_reason": None,
            "lifecycle_mutation_origin": "planning",
            "lifecycle_mutation": False,
        }
        self._emit("selection_result", context_supplied=True, **diagnostics)
        return EngineeringContextSelection(
            context=selected,
            reason="fresh_published_object",
            matched_trigger=matched_trigger,
            diagnostics=diagnostics,
        )

    def render_prompt_block(self, selection: EngineeringContextSelection) -> str | None:
        """Render immutable raw context for additive Planning input."""

        context = selection.context
        if context is None:
            return None
        lines = [
            "ENGINEERING CONTEXT (additive, bounded, raw repository source)",
            f"Subsystem: {context.subsystem_id} v{context.subsystem_version}",
            f"Repository identity: {context.repository_identity}",
            f"Commit/source fingerprint: {context.commit_fingerprint}",
            "Verification status: verified; freshness status: fresh",
            "Provenance: "
            + json.dumps(
                dict(context.provenance), sort_keys=True, separators=(",", ":")
            ),
            "This scope is not the entire repository. Existing repository tools remain available for files outside this supplied scope.",
        ]
        for relative_path in context.scope:
            raw_bytes = base64.b64decode(context.raw_source_content[relative_path])
            content = raw_bytes.decode("utf-8", errors="surrogateescape")
            lines.extend(
                [
                    f"FILE: {relative_path}",
                    "RAW SOURCE CONTENT BEGIN",
                    content,
                    "RAW SOURCE CONTENT END",
                ]
            )
        return "\n".join(lines)

    def generate_for_promotion(self, repository_root: Path) -> dict[str, Any]:
        """Run post-Promotion generation; failures are reported independently."""

        return self.generate_and_verify(repository_root, origin="promotion")

    def _fallback(
        self, reason: str, matched_trigger: str | None, detail: str | None
    ) -> EngineeringContextSelection:
        diagnostics = {
            "reason": reason,
            "matched_trigger": matched_trigger,
            "fallback_reason": reason,
            "context_supplied": False,
            "fresh": False,
            "verification_status": "unavailable",
            "files_supplied": 0,
            "source_bytes": 0,
            "detail": detail,
            "lifecycle_mutation_origin": "planning",
            "lifecycle_mutation": False,
        }
        self._emit("selection_result", **diagnostics)
        self._emit("fallback", **diagnostics)
        return EngineeringContextSelection(
            context=None,
            reason=reason,
            matched_trigger=matched_trigger,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _emit(event: str, **fields: Any) -> None:
        payload = {"event": event, **fields}
        logger.info("[ENGINEERING_CONTEXT] %s", json.dumps(payload, sort_keys=True))


def _matched_trigger(triggers: Sequence[str], query: str) -> str | None:
    folded = query.casefold()
    matches = [trigger for trigger in triggers if trigger.casefold() in folded]
    if not matches:
        return None
    return sorted(matches, key=lambda value: (-len(value), value.casefold()))[0]
