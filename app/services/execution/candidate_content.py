"""Immutable byte authority for Phase 29C-9 candidate outcomes.

The command boundary accepts bytes only from the trusted runtime completion
adapter.  The content store has no API for caller-supplied paths, URLs,
subprocesses, or network access.  Database rows contain bounded metadata and a
projection; the raw candidate remains behind this versioned store interface.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAttempt,
    ExecutionTaskAttemptOutcome,
    ExecutionTaskCandidateContent,
)
from app.services.planning.operator_review import canonical_json_hash


CANDIDATE_CONTENT_SCHEMA_VERSION = "execution-task-candidate-content/1.0"
CONTENT_STORE_BACKEND_ID = "local-content-addressed"
CONTENT_STORE_BACKEND_VERSION = "1"
CONTENT_PROJECTION_VERSION = "bounded-json/1"
MAX_CANDIDATE_CONTENT_BYTES = 1_048_576
MAX_JSON_PROJECTION_BYTES = 65_536
MAX_JSON_PROJECTION_DEPTH = 8
MAX_JSON_PROJECTION_ITEMS = 128
MAX_JSON_PROJECTION_STRING_LENGTH = 4_096
MEDIA_TYPES = frozenset({"application/json", "text/plain", "application/octet-stream"})
DEFAULT_MEDIA_TYPE = "application/octet-stream"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^sha256/[0-9a-f]{2}/([0-9a-f]{64})$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class CandidateContentError(RuntimeError):
    """Bounded content authority failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class StoredCandidateContent:
    content_sha256: str
    byte_length: int
    storage_key: str
    backend_id: str = CONTENT_STORE_BACKEND_ID
    backend_version: str = CONTENT_STORE_BACKEND_VERSION
    created: bool = False


class CandidateContentStore(Protocol):
    backend_id: str
    backend_version: str

    def put(self, content: bytes | Iterable[bytes]) -> StoredCandidateContent:
        """Write once and return the content-addressed identity."""

    def read(self, storage_key: str) -> bytes:
        """Read through the store interface and enforce object integrity."""

    def delete_if_unreferenced(self, storage_key: str) -> None:
        """Best-effort cleanup for a failed metadata insert."""

    def list_storage_keys(self) -> Iterable[str]:
        """List only hash-derived objects owned by this backend."""


def _chunks(content: bytes | Iterable[bytes]) -> Iterable[bytes]:
    if isinstance(content, bytes):
        for offset in range(0, len(content), 64 * 1024):
            yield content[offset : offset + 64 * 1024]
        return
    if isinstance(content, (bytearray, memoryview)):
        raw = bytes(content)
        for offset in range(0, len(raw), 64 * 1024):
            yield raw[offset : offset + 64 * 1024]
        return
    if isinstance(content, (str, Mapping)):
        raise CandidateContentError(
            "candidate_content_bytes_invalid", "candidate content must be bytes"
        )
    try:
        iterator = iter(content)
    except TypeError as exc:
        raise CandidateContentError(
            "candidate_content_bytes_invalid", "candidate content must be bytes"
        ) from exc
    for chunk in iterator:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise CandidateContentError(
                "candidate_content_bytes_invalid", "content chunks must be bytes"
            )
        raw = bytes(chunk)
        if raw:
            yield raw


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_hash(value: Any, field: str) -> str:
    result = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(result):
        raise CandidateContentError(
            "candidate_content_hash_invalid", f"{field} is invalid"
        )
    return result


def normalize_media_type(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return DEFAULT_MEDIA_TYPE
    result = str(value).strip().lower()
    if result not in MEDIA_TYPES or _CONTROL_RE.search(result):
        raise CandidateContentError(
            "candidate_content_media_type_invalid", "media type is not supported"
        )
    return result


def _store_key(content_sha256: str) -> str:
    return f"sha256/{content_sha256[:2]}/{content_sha256}"


def _projection_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_JSON_PROJECTION_DEPTH:
        raise CandidateContentError(
            "candidate_content_projection_too_large", "JSON projection is too deep"
        )
    if isinstance(value, dict):
        if len(value) > MAX_JSON_PROJECTION_ITEMS:
            raise CandidateContentError(
                "candidate_content_projection_too_large", "JSON object is too large"
            )
        return {
            str(key): _projection_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        if len(value) > MAX_JSON_PROJECTION_ITEMS:
            raise CandidateContentError(
                "candidate_content_projection_too_large", "JSON array is too large"
            )
        return [_projection_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        if len(value) > MAX_JSON_PROJECTION_STRING_LENGTH or _CONTROL_RE.search(value):
            raise CandidateContentError(
                "candidate_content_projection_too_large", "JSON string is too large"
            )
        return value
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return value
    raise CandidateContentError(
        "candidate_content_projection_invalid", "JSON contains an unsupported value"
    )


def _json_projection(content: bytes, media_type: str) -> tuple[Any | None, str | None]:
    if media_type != "application/json":
        return None, None
    try:
        text = content.decode("utf-8", errors="strict")

        def duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise CandidateContentError(
                        "candidate_content_projection_invalid",
                        "duplicate JSON object keys are not allowed",
                    )
                result[key] = value
            return result

        parsed = json.loads(
            text,
            object_pairs_hook=duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                CandidateContentError(
                    "candidate_content_projection_invalid",
                    f"JSON constant {value} is not allowed",
                )
            ),
        )
        projection = _projection_value(parsed)
        encoded = json.dumps(
            projection,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateContentError(
            "candidate_content_projection_invalid", "JSON is not strict UTF-8"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CandidateContentError(
            "candidate_content_projection_invalid", "JSON is malformed"
        ) from exc
    if len(encoded) > MAX_JSON_PROJECTION_BYTES:
        raise CandidateContentError(
            "candidate_content_projection_too_large", "JSON projection is too large"
        )
    return projection, _hash_bytes(encoded)


class LocalContentAddressedStore:
    """Bounded local store with hash-derived keys and atomic no-overwrite finalization."""

    backend_id = CONTENT_STORE_BACKEND_ID
    backend_version = CONTENT_STORE_BACKEND_VERSION

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        max_bytes: int = MAX_CANDIDATE_CONTENT_BYTES,
    ):
        if int(max_bytes) <= 0 or int(max_bytes) > MAX_CANDIDATE_CONTENT_BYTES:
            raise CandidateContentError(
                "candidate_content_limit_invalid", "content limit is outside the bound"
            )
        self.max_bytes = int(max_bytes)
        configured_root = root if root is not None else settings.CANDIDATE_CONTENT_DIR
        self.root = Path(configured_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o777)
        self._tmp_root = self.root / ".tmp"
        self._tmp_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self._tmp_root, 0o777)

    def _path_for_key(self, storage_key: str) -> Path:
        match = _KEY_RE.fullmatch(str(storage_key))
        if match is None:
            raise CandidateContentError(
                "candidate_content_storage_key_invalid", "storage key is invalid"
            )
        digest = match.group(1)
        path = self.root / "sha256" / digest[:2] / digest
        if path.resolve(strict=False).parent.parent != (self.root / "sha256").resolve(
            strict=False
        ):
            raise CandidateContentError(
                "candidate_content_storage_key_invalid", "storage key escapes store"
            )
        return path

    def put(self, content: bytes | Iterable[bytes]) -> StoredCandidateContent:
        digest = hashlib.sha256()
        total = 0
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self._tmp_root, prefix="candidate-", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                for chunk in _chunks(content):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise CandidateContentError(
                            "candidate_content_too_large",
                            "candidate content exceeds limit",
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            content_sha256 = digest.hexdigest()
            key = _store_key(content_sha256)
            final_path = self._path_for_key(key)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(final_path.parent, 0o777)
            if final_path.exists() or final_path.is_symlink():
                if final_path.is_symlink() or not final_path.is_file():
                    raise CandidateContentError(
                        "candidate_content_storage_mutable",
                        "final storage entry is not a file",
                    )
                if final_path.stat().st_mode & (
                    stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
                ):
                    raise CandidateContentError(
                        "candidate_content_storage_mutable",
                        "final storage entry is writable",
                    )
                existing = final_path.read_bytes()
                candidate = temp_path.read_bytes()
                if existing != candidate:
                    raise CandidateContentError(
                        "candidate_content_hash_collision",
                        "existing hash entry contains different bytes",
                    )
                return StoredCandidateContent(content_sha256, total, key, created=False)
            try:
                os.link(temp_path, final_path)
            except FileExistsError:
                if final_path.is_symlink() or not final_path.is_file():
                    raise CandidateContentError(
                        "candidate_content_storage_mutable",
                        "final storage entry is invalid",
                    )
                if final_path.stat().st_mode & (
                    stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
                ):
                    raise CandidateContentError(
                        "candidate_content_storage_mutable",
                        "concurrent final storage entry is writable",
                    )
                if final_path.read_bytes() != temp_path.read_bytes():
                    raise CandidateContentError(
                        "candidate_content_hash_collision",
                        "concurrent hash entry contains different bytes",
                    )
                return StoredCandidateContent(content_sha256, total, key, created=False)
            os.chmod(final_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            return StoredCandidateContent(content_sha256, total, key, created=True)
        except OSError as exc:
            raise CandidateContentError(
                "candidate_content_storage_failure", "content store write failed"
            ) from exc
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def read(self, storage_key: str) -> bytes:
        path = self._path_for_key(storage_key)
        if path.is_symlink() or not path.is_file():
            raise CandidateContentError(
                "candidate_content_storage_missing", "content object is missing"
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CandidateContentError(
                "candidate_content_storage_failure", "content object could not be read"
            ) from exc
        if len(data) > self.max_bytes:
            raise CandidateContentError(
                "candidate_content_too_large", "stored content exceeds limit"
            )
        digest = _KEY_RE.fullmatch(storage_key).group(1)  # type: ignore[union-attr]
        if path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise CandidateContentError(
                "candidate_content_storage_mutable", "stored content is writable"
            )
        if _hash_bytes(data) != digest:
            raise CandidateContentError(
                "candidate_content_storage_tampered", "stored content hash is invalid"
            )
        return data

    def delete_if_unreferenced(self, storage_key: str) -> None:
        path = self._path_for_key(storage_key)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise CandidateContentError(
                "candidate_content_storage_cleanup_failure", "content cleanup failed"
            ) from exc

    def list_storage_keys(self) -> Iterable[str]:
        root = self.root / "sha256"
        if not root.is_dir():
            return ()
        result: list[str] = []
        for prefix in root.iterdir():
            if not prefix.is_dir() or len(prefix.name) != 2:
                continue
            for entry in prefix.iterdir():
                if entry.is_file() and not entry.is_symlink():
                    key = f"sha256/{prefix.name}/{entry.name}"
                    if _KEY_RE.fullmatch(key):
                        result.append(key)
        return tuple(sorted(result))


@dataclass(frozen=True)
class IngestCandidateContentCommand:
    execution_plan_id: int
    execution_task_id: int
    execution_task_attempt_id: int
    attempt_generation: int
    candidate_outcome_id: int
    content: bytes | Iterable[bytes]
    declared_sha256: str | None = None
    media_type: str | None = None
    ingestion_idempotency_key: str = ""
    creation_actor_type: str = "runtime_adapter"
    creation_actor_id: str = "runtime"


@dataclass(frozen=True)
class CandidateContentIngestionResult:
    content: ExecutionTaskCandidateContent
    replayed: bool = False


@dataclass(frozen=True)
class CandidateContentIntegrityResult:
    execution_plan_id: int | None
    execution_task_id: int | None
    verified: bool
    issues: tuple[str, ...] = ()


def _ingestion_payload(
    command: IngestCandidateContentCommand,
    *,
    content_sha256: str,
    byte_length: int,
    media_type: str,
    declared_sha256: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": CANDIDATE_CONTENT_SCHEMA_VERSION,
        "execution_plan_id": int(command.execution_plan_id),
        "execution_task_id": int(command.execution_task_id),
        "execution_task_attempt_id": int(command.execution_task_attempt_id),
        "attempt_generation": int(command.attempt_generation),
        "candidate_outcome_id": int(command.candidate_outcome_id),
        "content_sha256": content_sha256,
        "declared_sha256": declared_sha256,
        "byte_length": int(byte_length),
        "media_type": media_type,
        "ingestion_idempotency_key": command.ingestion_idempotency_key,
        "creation_actor_type": command.creation_actor_type,
        "creation_actor_id": command.creation_actor_id,
    }


class CandidateContentIngestionService:
    """Authorize and persist one immutable content link for one outcome."""

    def __init__(
        self,
        db: Session,
        *,
        store: CandidateContentStore | None = None,
        now: callable | None = None,
    ):
        self.db = db
        self.store = store or LocalContentAddressedStore()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def ingest(
        self, command: IngestCandidateContentCommand
    ) -> CandidateContentIngestionResult:
        content = b"".join(_chunks(command.content))
        if len(content) > MAX_CANDIDATE_CONTENT_BYTES:
            raise CandidateContentError(
                "candidate_content_too_large", "candidate content exceeds limit"
            )
        content_sha256 = _hash_bytes(content)
        declared = (
            _validate_hash(command.declared_sha256, "declared SHA-256")
            if command.declared_sha256
            else None
        )
        outcome_claim = self.db.get(
            ExecutionTaskAttemptOutcome, int(command.candidate_outcome_id)
        )
        if outcome_claim is not None and outcome_claim.output_hash:
            runtime_declared = _validate_hash(
                outcome_claim.output_hash, "runtime declared SHA-256"
            )
            if declared is not None and declared != runtime_declared:
                raise CandidateContentError(
                    "candidate_content_hash_mismatch",
                    "ingestion claim differs from the C6B declared hash",
                )
            declared = runtime_declared
        media_type = normalize_media_type(command.media_type)
        if declared is not None and declared != content_sha256:
            raise CandidateContentError(
                "candidate_content_hash_mismatch",
                "declared SHA-256 does not match independently recomputed bytes",
            )
        payload = _ingestion_payload(
            command,
            content_sha256=content_sha256,
            byte_length=len(content),
            media_type=media_type,
            declared_sha256=declared,
        )
        command_hash = canonical_json_hash(payload)
        existing = (
            self.db.query(ExecutionTaskCandidateContent)
            .filter(
                ExecutionTaskCandidateContent.ingestion_idempotency_key
                == command.ingestion_idempotency_key
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_ingestion_command_hash != command_hash:
                raise CandidateContentError(
                    "candidate_content_idempotency_conflict",
                    "ingestion key is bound to different content or authority",
                )
            integrity = verify_candidate_content_integrity(
                self.db, existing.id, store=self.store
            )
            if not integrity.verified:
                raise CandidateContentError(
                    "candidate_content_integrity_failure",
                    "replayed content failed integrity verification",
                )
            return CandidateContentIngestionResult(existing, replayed=True)

        plan, task, attempt, outcome = self._authorize(command)
        duplicate = (
            self.db.query(ExecutionTaskCandidateContent)
            .filter(ExecutionTaskCandidateContent.candidate_outcome_id == outcome.id)
            .one_or_none()
        )
        if duplicate is not None:
            raise CandidateContentError(
                "candidate_content_outcome_conflict",
                "candidate outcome already has canonical content",
            )
        projection, projection_hash = _json_projection(content, media_type)
        stored = self.store.put(content)
        metadata_payload = {
            "schema_version": CANDIDATE_CONTENT_SCHEMA_VERSION,
            "candidate_content_id": None,
            "execution_plan_id": plan.id,
            "execution_task_id": task.id,
            "execution_task_attempt_id": attempt.id,
            "attempt_generation": attempt.attempt_generation,
            "candidate_outcome_id": outcome.id,
            "content_sha256": stored.content_sha256,
            "declared_sha256": declared,
            "byte_length": stored.byte_length,
            "media_type": media_type,
            "storage_backend_id": stored.backend_id,
            "storage_backend_version": stored.backend_version,
            "storage_key": stored.storage_key,
            "content_projection_hash": projection_hash,
            "content_projection_version": (
                CONTENT_PROJECTION_VERSION if projection is not None else None
            ),
        }
        row = ExecutionTaskCandidateContent(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            execution_task_attempt_id=attempt.id,
            attempt_generation=attempt.attempt_generation,
            candidate_outcome_id=outcome.id,
            content_sha256=stored.content_sha256,
            declared_sha256=declared,
            byte_length=stored.byte_length,
            media_type=media_type,
            storage_backend_id=stored.backend_id,
            storage_backend_version=stored.backend_version,
            storage_key=stored.storage_key,
            ingestion_idempotency_key=command.ingestion_idempotency_key,
            canonical_ingestion_command_payload=payload,
            canonical_ingestion_command_hash=command_hash,
            canonical_metadata_payload=metadata_payload,
            canonical_metadata_hash=canonical_json_hash(metadata_payload),
            content_projection=projection,
            content_projection_hash=projection_hash,
            content_projection_version=(
                CONTENT_PROJECTION_VERSION if projection is not None else None
            ),
            creation_actor_type=command.creation_actor_type,
            creation_actor_id=command.creation_actor_id,
            created_at=self._now(),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(ExecutionTaskCandidateContent)
                .filter(
                    ExecutionTaskCandidateContent.ingestion_idempotency_key
                    == command.ingestion_idempotency_key
                )
                .one_or_none()
            )
            if (
                replay is not None
                and replay.canonical_ingestion_command_hash == command_hash
            ):
                return CandidateContentIngestionResult(replay, replayed=True)
            if stored.created:
                self._delete_if_unlinked(stored.storage_key)
            raise CandidateContentError(
                "candidate_content_insert_conflict",
                "content metadata conflicts with canonical authority",
            ) from exc
        return CandidateContentIngestionResult(row)

    def _delete_if_unlinked(self, storage_key: str) -> None:
        linked = (
            self.db.query(ExecutionTaskCandidateContent)
            .filter(ExecutionTaskCandidateContent.storage_key == storage_key)
            .count()
        )
        if linked == 0:
            self.store.delete_if_unreferenced(storage_key)

    def _authorize(
        self, command: IngestCandidateContentCommand
    ) -> tuple[
        ExecutionPlan, ExecutionTask, ExecutionTaskAttempt, ExecutionTaskAttemptOutcome
    ]:
        plan = self.db.get(ExecutionPlan, int(command.execution_plan_id))
        task = self.db.get(ExecutionTask, int(command.execution_task_id))
        attempt = self.db.get(
            ExecutionTaskAttempt, int(command.execution_task_attempt_id)
        )
        outcome = self.db.get(
            ExecutionTaskAttemptOutcome, int(command.candidate_outcome_id)
        )
        if any(item is None for item in (plan, task, attempt, outcome)):
            raise CandidateContentError(
                "candidate_content_authority_missing",
                "candidate authority is incomplete",
            )
        assert (
            plan is not None
            and task is not None
            and attempt is not None
            and outcome is not None
        )
        if plan.status != "active" or plan.superseded_by_execution_plan_id is not None:
            raise CandidateContentError(
                "candidate_content_authority_invalid", "execution plan is not active"
            )
        if task.status != "awaiting_validation":
            raise CandidateContentError(
                "candidate_content_authority_invalid", "task is not awaiting validation"
            )
        if (
            task.execution_plan_id != plan.id
            or attempt.execution_plan_id != plan.id
            or attempt.execution_task_id != task.id
            or attempt.attempt_generation != int(command.attempt_generation)
            or outcome.execution_plan_id != plan.id
            or outcome.execution_task_id != task.id
            or outcome.execution_task_attempt_id != attempt.id
            or outcome.outcome_status != "candidate_completed"
            or attempt.attempt_status != "candidate_completed"
        ):
            raise CandidateContentError(
                "candidate_content_authority_invalid",
                "candidate content is not bound to the exact completed outcome",
            )
        from app.services.execution.execution_task_runtime_execution_service import (
            ExecutionTaskRuntimeExecutionService,
        )

        runtime_integrity = ExecutionTaskRuntimeExecutionService(
            self.db
        ).verify_attempt_outcome_integrity(outcome.id)
        if not runtime_integrity.verified:
            raise CandidateContentError(
                "candidate_content_integrity_failure",
                "runtime outcome integrity failed",
            )
        return plan, task, attempt, outcome


def _metadata_payload(row: ExecutionTaskCandidateContent) -> dict[str, Any]:
    payload = dict(row.canonical_metadata_payload or {})
    payload["candidate_content_id"] = None
    return payload


def verify_candidate_content_integrity(
    db: Session,
    content_id: int,
    *,
    store: CandidateContentStore | None = None,
) -> CandidateContentIntegrityResult:
    row = db.get(ExecutionTaskCandidateContent, int(content_id))
    if row is None:
        return CandidateContentIntegrityResult(
            None, None, False, ("candidate_content_missing",)
        )
    issues: list[str] = []
    plan = db.get(ExecutionPlan, row.execution_plan_id)
    task = db.get(ExecutionTask, row.execution_task_id)
    attempt = db.get(ExecutionTaskAttempt, row.execution_task_attempt_id)
    outcome = db.get(ExecutionTaskAttemptOutcome, row.candidate_outcome_id)
    if any(item is None for item in (plan, task, attempt, outcome)):
        issues.append("candidate_content_authority_missing")
    if task is not None and task.execution_plan_id != row.execution_plan_id:
        issues.append("candidate_content_task_plan_mismatch")
    if attempt is not None and (
        attempt.execution_plan_id != row.execution_plan_id
        or attempt.execution_task_id != row.execution_task_id
        or attempt.attempt_generation != row.attempt_generation
    ):
        issues.append("candidate_content_attempt_linkage_mismatch")
    if outcome is not None and (
        outcome.execution_plan_id != row.execution_plan_id
        or outcome.execution_task_id != row.execution_task_id
        or outcome.execution_task_attempt_id != row.execution_task_attempt_id
        or outcome.outcome_status != "candidate_completed"
    ):
        issues.append("candidate_content_outcome_linkage_mismatch")
    if not _HASH_RE.fullmatch(str(row.content_sha256 or "")):
        issues.append("candidate_content_hash_malformed")
    if row.declared_sha256 is not None and not _HASH_RE.fullmatch(row.declared_sha256):
        issues.append("candidate_content_declared_hash_malformed")
    if row.media_type not in MEDIA_TYPES:
        issues.append("candidate_content_media_type_invalid")
    if row.byte_length < 0 or row.byte_length > MAX_CANDIDATE_CONTENT_BYTES:
        issues.append("candidate_content_size_invalid")
    if (
        row.storage_backend_id != CONTENT_STORE_BACKEND_ID
        or row.storage_backend_version != CONTENT_STORE_BACKEND_VERSION
    ):
        issues.append("candidate_content_store_identity_invalid")
    try:
        expected_metadata = _metadata_payload(row)
        if row.canonical_metadata_payload != expected_metadata:
            issues.append("candidate_content_metadata_tampered")
        if (
            canonical_json_hash(row.canonical_metadata_payload)
            != row.canonical_metadata_hash
        ):
            issues.append("candidate_content_metadata_hash_mismatch")
        if (
            canonical_json_hash(row.canonical_ingestion_command_payload)
            != row.canonical_ingestion_command_hash
        ):
            issues.append("candidate_content_ingestion_hash_mismatch")
        if (
            row.canonical_ingestion_command_payload.get("content_sha256")
            != row.content_sha256
        ):
            issues.append("candidate_content_ingestion_payload_tampered")
    except (TypeError, ValueError, AttributeError):
        issues.append("candidate_content_payload_malformed")
    reader = store or LocalContentAddressedStore()
    try:
        data = reader.read(row.storage_key)
        if len(data) != row.byte_length:
            issues.append("candidate_content_size_mismatch")
        if _hash_bytes(data) != row.content_sha256:
            issues.append("candidate_content_hash_mismatch")
        projection, projection_hash = _json_projection(data, row.media_type)
        if (
            projection != row.content_projection
            or projection_hash != row.content_projection_hash
        ):
            issues.append("candidate_content_projection_mismatch")
    except CandidateContentError as exc:
        issues.append(exc.code)
    if outcome is not None:
        from app.services.execution.execution_task_runtime_execution_service import (
            ExecutionTaskRuntimeExecutionService,
        )

        issues.extend(
            ExecutionTaskRuntimeExecutionService(db)
            .verify_attempt_outcome_integrity(outcome.id)
            .issues
        )
    duplicate_count = (
        db.query(ExecutionTaskCandidateContent)
        .filter(
            ExecutionTaskCandidateContent.candidate_outcome_id
            == row.candidate_outcome_id
        )
        .count()
    )
    if duplicate_count != 1:
        issues.append("duplicate_candidate_content_linkage")
    return CandidateContentIntegrityResult(
        row.execution_plan_id,
        row.execution_task_id,
        not issues,
        tuple(sorted(set(issues))),
    )


def verify_execution_task_candidate_content_integrity(
    db: Session,
    execution_task_id: int,
    *,
    store: CandidateContentStore | None = None,
) -> CandidateContentIntegrityResult:
    task = db.get(ExecutionTask, int(execution_task_id))
    if task is None:
        return CandidateContentIntegrityResult(
            None, int(execution_task_id), False, ("candidate_content_task_missing",)
        )
    rows = (
        db.query(ExecutionTaskCandidateContent)
        .filter(ExecutionTaskCandidateContent.execution_task_id == task.id)
        .all()
    )
    issues: list[str] = []
    for row in rows:
        issues.extend(
            verify_candidate_content_integrity(db, row.id, store=store).issues
        )
    return CandidateContentIntegrityResult(
        task.execution_plan_id, task.id, not issues, tuple(sorted(set(issues)))
    )


def verify_execution_plan_candidate_content_integrity(
    db: Session,
    execution_plan_id: int,
    *,
    store: CandidateContentStore | None = None,
) -> CandidateContentIntegrityResult:
    plan = db.get(ExecutionPlan, int(execution_plan_id))
    if plan is None:
        return CandidateContentIntegrityResult(
            int(execution_plan_id), None, False, ("candidate_content_plan_missing",)
        )
    issues: list[str] = []
    rows = (
        db.query(ExecutionTaskCandidateContent)
        .filter(ExecutionTaskCandidateContent.execution_plan_id == plan.id)
        .all()
    )
    for row in rows:
        issues.extend(
            verify_candidate_content_integrity(db, row.id, store=store).issues
        )
    return CandidateContentIntegrityResult(
        plan.id, None, not issues, tuple(sorted(set(issues)))
    )


def verify_candidate_content_store_integrity(
    db: Session, *, store: CandidateContentStore | None = None
) -> CandidateContentIntegrityResult:
    """Detect missing metadata objects, orphan objects, and mutable state."""

    reader = store or LocalContentAddressedStore()
    issues: list[str] = []
    rows = db.query(ExecutionTaskCandidateContent).all()
    known = {row.storage_key for row in rows}
    for row in rows:
        issues.extend(
            verify_candidate_content_integrity(db, row.id, store=reader).issues
        )
    for key in reader.list_storage_keys():
        if key not in known:
            issues.append(f"orphan_candidate_content_storage:{key}")
    return CandidateContentIntegrityResult(
        None, None, not issues, tuple(sorted(set(issues)))
    )


def cleanup_unlinked_candidate_content(
    db: Session, *, store: CandidateContentStore | None = None
) -> tuple[str, ...]:
    """Delete only hash objects with no committed authoritative linkage row.

    The blob store is shared with the Phase 29C-11 execution evidence
    authority, so a key already referenced by that authority is never
    treated as orphaned here.
    """

    reader = store or LocalContentAddressedStore()
    linked = {
        row.storage_key
        for row in db.query(ExecutionTaskCandidateContent.storage_key).all()
    }
    from app.models import ExecutionEvidence

    linked |= {row.storage_key for row in db.query(ExecutionEvidence.storage_key).all()}
    removed: list[str] = []
    for key in reader.list_storage_keys():
        if key not in linked:
            reader.delete_if_unreferenced(key)
            removed.append(key)
    return tuple(sorted(removed))


__all__ = [
    "CANDIDATE_CONTENT_SCHEMA_VERSION",
    "CONTENT_PROJECTION_VERSION",
    "CONTENT_STORE_BACKEND_ID",
    "CONTENT_STORE_BACKEND_VERSION",
    "DEFAULT_MEDIA_TYPE",
    "MAX_CANDIDATE_CONTENT_BYTES",
    "CandidateContentError",
    "CandidateContentIngestionResult",
    "CandidateContentIngestionService",
    "CandidateContentIntegrityResult",
    "CandidateContentStore",
    "IngestCandidateContentCommand",
    "LocalContentAddressedStore",
    "MEDIA_TYPES",
    "StoredCandidateContent",
    "normalize_media_type",
    "verify_candidate_content_integrity",
    "verify_candidate_content_store_integrity",
    "cleanup_unlinked_candidate_content",
    "verify_execution_plan_candidate_content_integrity",
    "verify_execution_task_candidate_content_integrity",
]
