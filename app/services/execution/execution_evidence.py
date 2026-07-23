"""Immutable, producer-agnostic execution artifact authority (Phase 29C-11).

A trusted producer (runtime completion adapter, command runner, test runner,
lint runner) ingests bytes it already has.  This module never opens a path,
calls a network client, starts a subprocess, or executes a command or test.
It writes one immutable metadata row per artifact and reuses the existing
Phase 29C-9 content-addressed blob store for the bytes themselves: this
module owns metadata, the blob store owns bytes.

Only four evidence kinds are supported in v1: ``candidate``, ``command``,
``test``, ``lint``.  Each kind is bound to exactly one producer identity.
Unknown kinds and unknown producers fail closed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import unicodedata
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionEvidence,
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAttempt,
    ExecutionTaskCandidateContent,
)
from app.services.execution.candidate_content import (
    CandidateContentError,
    CandidateContentStore,
    CONTENT_STORE_BACKEND_ID,
    CONTENT_STORE_BACKEND_VERSION,
    LocalContentAddressedStore,
    MAX_CANDIDATE_CONTENT_BYTES,
    MEDIA_TYPES,
    _chunks as _content_chunks,
    _hash_bytes,
)
from app.services.planning.operator_review import canonical_json_hash


EXECUTION_EVIDENCE_SCHEMA_VERSION = "execution-evidence/1"
EXECUTION_EVIDENCE_REFERENCE_GRAMMAR_VERSION = "execution-evidence-reference/1"
MAX_EVIDENCE_BYTES = MAX_CANDIDATE_CONTENT_BYTES
MAX_IDEMPOTENCY_KEY_LENGTH = 128
DEFAULT_EVIDENCE_MEDIA_TYPE = "application/octet-stream"

# Reserved for future versions: benchmark, coverage, profiling, security
# scan, static analysis, and custom plugin kinds are not accepted here.
SUPPORTED_EVIDENCE_KINDS = frozenset({"candidate", "command", "test", "lint"})

# Each kind is bound to exactly one producer identity in v1.
EVIDENCE_KIND_PRODUCERS: dict[str, str] = {
    "candidate": "runtime",
    "command": "command-runner",
    "test": "test-runner",
    "lint": "lint-runner",
}
SUPPORTED_EVIDENCE_PRODUCERS = frozenset(EVIDENCE_KIND_PRODUCERS.values())

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REFERENCE_RE = re.compile(r"^execution-evidence://([1-9][0-9]{0,18})$")
_HASH_REFERENCE_RE = re.compile(r"^execution-evidence-sha256://([0-9a-f]{64})$")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]{0,31}):/{2}.*$")


class ExecutionEvidenceError(RuntimeError):
    """Bounded execution-evidence authority failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _bounded_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ExecutionEvidenceError(
            "execution_evidence_metadata_invalid", f"{field} is invalid"
        )
    result = unicodedata.normalize("NFC", value)
    if not result or len(result) > limit or _CONTROL_RE.search(result):
        raise ExecutionEvidenceError(
            "execution_evidence_metadata_invalid", f"{field} is invalid"
        )
    return result


def _optional_hash(value: Any, field: str) -> str | None:
    if value is None:
        return None
    result = str(value).strip().lower()
    if not _HASH_RE.fullmatch(result):
        raise ExecutionEvidenceError(
            "execution_evidence_hash_invalid", f"{field} is invalid"
        )
    return result


def normalize_evidence_media_type(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return DEFAULT_EVIDENCE_MEDIA_TYPE
    result = str(value).strip().lower()
    if result not in MEDIA_TYPES or _CONTROL_RE.search(result):
        raise ExecutionEvidenceError(
            "execution_evidence_media_type_invalid", "media type is not supported"
        )
    return result


@dataclass(frozen=True)
class ExecutionEvidenceReference:
    grammar_version: str
    scheme: str
    identifier: int | str
    normalized: str


def parse_execution_evidence_reference(value: str) -> ExecutionEvidenceReference:
    """Parse the sole certified execution-evidence reference grammar.

    ``execution-evidence://<id>`` identifies one immutable metadata row.
    ``execution-evidence-sha256://<hash>`` identifies content by hash and is
    only resolvable when it identifies exactly one row.  Neither form is a
    filesystem/object-store URL and neither is ever dereferenced as one.
    """

    raw = _bounded_text(value, "reference", 255).strip()
    if raw != value:
        raise ExecutionEvidenceError(
            "execution_evidence_reference_invalid", "reference whitespace is invalid"
        )
    raw = unicodedata.normalize("NFC", raw)
    if "@" in raw or "?" in raw or "#" in raw or "\\" in raw:
        raise ExecutionEvidenceError(
            "execution_evidence_reference_invalid", "reference syntax is invalid"
        )
    lowered = raw.lower()
    match = _REFERENCE_RE.fullmatch(lowered)
    if match is not None:
        identifier = int(match.group(1))
        return ExecutionEvidenceReference(
            grammar_version=EXECUTION_EVIDENCE_REFERENCE_GRAMMAR_VERSION,
            scheme="execution-evidence",
            identifier=identifier,
            normalized=f"execution-evidence://{identifier}",
        )
    match = _HASH_REFERENCE_RE.fullmatch(lowered)
    if match is not None:
        return ExecutionEvidenceReference(
            grammar_version=EXECUTION_EVIDENCE_REFERENCE_GRAMMAR_VERSION,
            scheme="execution-evidence-sha256",
            identifier=match.group(1),
            normalized=f"execution-evidence-sha256://{match.group(1)}",
        )
    if _SCHEME_RE.match(raw):
        raise ExecutionEvidenceError(
            "execution_evidence_scheme_unsupported", "reference scheme is unsupported"
        )
    raise ExecutionEvidenceError(
        "execution_evidence_reference_invalid",
        "reference is not a supported absolute reference",
    )


def normalize_execution_evidence_reference(value: str) -> str:
    return parse_execution_evidence_reference(value).normalized


def evidence_reference_for_id(evidence_id: int) -> str:
    identifier = int(evidence_id)
    if identifier <= 0:
        raise ExecutionEvidenceError(
            "execution_evidence_reference_invalid", "evidence id is invalid"
        )
    return f"execution-evidence://{identifier}"


@dataclass(frozen=True)
class IngestExecutionEvidenceCommand:
    execution_plan_id: int
    execution_task_id: int
    execution_task_attempt_id: int
    attempt_generation: int
    evidence_kind: str
    producer_id: str
    producer_version: str
    content: bytes | Iterable[bytes]
    declared_sha256: str | None = None
    media_type: str | None = None
    ingestion_idempotency_key: str = ""
    creation_actor_type: str = "runtime_adapter"
    creation_actor_id: str = "runtime"


@dataclass(frozen=True)
class ExecutionEvidenceIngestionResult:
    evidence: ExecutionEvidence
    replayed: bool = False


@dataclass(frozen=True)
class ExecutionEvidenceIntegrityResult:
    execution_plan_id: int | None
    execution_task_id: int | None
    verified: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionEvidenceResolution:
    evidence_id: int | None
    evidence_kind: str | None
    producer_id: str | None
    producer_version: str | None
    media_type: str | None
    byte_length: int | None
    content_sha256: str | None
    storage_backend_id: str | None
    storage_backend_version: str | None
    resolution_status: str
    verified: bool
    issues: tuple[str, ...] = ()


def _ingestion_payload(
    command: IngestExecutionEvidenceCommand,
    *,
    content_sha256: str,
    byte_length: int,
    media_type: str,
    declared_sha256: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": EXECUTION_EVIDENCE_SCHEMA_VERSION,
        "execution_plan_id": int(command.execution_plan_id),
        "execution_task_id": int(command.execution_task_id),
        "execution_task_attempt_id": int(command.execution_task_attempt_id),
        "attempt_generation": int(command.attempt_generation),
        "evidence_kind": command.evidence_kind,
        "producer_id": command.producer_id,
        "producer_version": command.producer_version,
        "content_sha256": content_sha256,
        "declared_sha256": declared_sha256,
        "byte_length": int(byte_length),
        "media_type": media_type,
        "ingestion_idempotency_key": command.ingestion_idempotency_key,
        "creation_actor_type": command.creation_actor_type,
        "creation_actor_id": command.creation_actor_id,
    }


def _metadata_payload(row: ExecutionEvidence) -> dict[str, Any]:
    payload = dict(row.canonical_metadata_payload or {})
    payload["execution_evidence_id"] = None
    return payload


class ExecutionEvidenceIngestionService:
    """Authorize and persist one immutable execution-evidence row."""

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
        self, command: IngestExecutionEvidenceCommand
    ) -> ExecutionEvidenceIngestionResult:
        evidence_kind = _bounded_text(command.evidence_kind, "evidence kind", 32)
        if evidence_kind not in SUPPORTED_EVIDENCE_KINDS:
            raise ExecutionEvidenceError(
                "execution_evidence_kind_unsupported", "evidence kind is unsupported"
            )
        producer_id = _bounded_text(command.producer_id, "producer id", 32)
        if producer_id not in SUPPORTED_EVIDENCE_PRODUCERS:
            raise ExecutionEvidenceError(
                "execution_evidence_producer_unsupported",
                "evidence producer is unsupported",
            )
        if EVIDENCE_KIND_PRODUCERS[evidence_kind] != producer_id:
            raise ExecutionEvidenceError(
                "execution_evidence_producer_kind_mismatch",
                "producer identity does not match evidence kind",
            )
        producer_version = _bounded_text(
            command.producer_version, "producer version", 64
        )
        idempotency_key = _bounded_text(
            command.ingestion_idempotency_key,
            "ingestion idempotency key",
            MAX_IDEMPOTENCY_KEY_LENGTH,
        )

        content = b"".join(_content_chunks(command.content))
        if len(content) > MAX_EVIDENCE_BYTES:
            raise ExecutionEvidenceError(
                "execution_evidence_too_large", "execution evidence exceeds limit"
            )
        content_sha256 = _hash_bytes(content)
        declared = _optional_hash(command.declared_sha256, "declared SHA-256")
        if declared is not None and declared != content_sha256:
            raise ExecutionEvidenceError(
                "execution_evidence_hash_mismatch",
                "declared SHA-256 does not match independently recomputed bytes",
            )
        media_type = normalize_evidence_media_type(command.media_type)

        payload = _ingestion_payload(
            command,
            content_sha256=content_sha256,
            byte_length=len(content),
            media_type=media_type,
            declared_sha256=declared,
        )
        payload["evidence_kind"] = evidence_kind
        payload["producer_id"] = producer_id
        payload["producer_version"] = producer_version
        payload["ingestion_idempotency_key"] = idempotency_key
        command_hash = canonical_json_hash(payload)

        existing = (
            self.db.query(ExecutionEvidence)
            .filter(ExecutionEvidence.ingestion_idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_ingestion_command_hash != command_hash:
                raise ExecutionEvidenceError(
                    "execution_evidence_idempotency_conflict",
                    "ingestion key is bound to different content or authority",
                )
            integrity = verify_execution_evidence_integrity(
                self.db, existing.id, store=self.store
            )
            if not integrity.verified:
                raise ExecutionEvidenceError(
                    "execution_evidence_integrity_failure",
                    "replayed evidence failed integrity verification",
                )
            return ExecutionEvidenceIngestionResult(existing, replayed=True)

        plan, task, attempt = self._authorize(command, evidence_kind=evidence_kind)

        stored = self.store.put(content)
        metadata_payload = {
            "schema_version": EXECUTION_EVIDENCE_SCHEMA_VERSION,
            "execution_evidence_id": None,
            "execution_plan_id": plan.id,
            "execution_task_id": task.id,
            "execution_task_attempt_id": attempt.id,
            "attempt_generation": attempt.attempt_generation,
            "evidence_kind": evidence_kind,
            "producer_id": producer_id,
            "producer_version": producer_version,
            "content_sha256": stored.content_sha256,
            "declared_sha256": declared,
            "byte_length": stored.byte_length,
            "media_type": media_type,
            "storage_backend_id": stored.backend_id,
            "storage_backend_version": stored.backend_version,
            "storage_key": stored.storage_key,
        }
        row = ExecutionEvidence(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            execution_task_attempt_id=attempt.id,
            attempt_generation=attempt.attempt_generation,
            evidence_kind=evidence_kind,
            producer_id=producer_id,
            producer_version=producer_version,
            content_sha256=stored.content_sha256,
            declared_sha256=declared,
            byte_length=stored.byte_length,
            media_type=media_type,
            storage_backend_id=stored.backend_id,
            storage_backend_version=stored.backend_version,
            storage_key=stored.storage_key,
            ingestion_idempotency_key=idempotency_key,
            canonical_ingestion_command_payload=payload,
            canonical_ingestion_command_hash=command_hash,
            canonical_metadata_payload=metadata_payload,
            canonical_metadata_hash=canonical_json_hash(metadata_payload),
            creation_actor_type=_bounded_text(
                command.creation_actor_type, "creation actor type", 64
            ),
            creation_actor_id=_bounded_text(
                command.creation_actor_id, "creation actor id", 255
            ),
            created_at=self._now(),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(ExecutionEvidence)
                .filter(ExecutionEvidence.ingestion_idempotency_key == idempotency_key)
                .one_or_none()
            )
            if (
                replay is not None
                and replay.canonical_ingestion_command_hash == command_hash
            ):
                return ExecutionEvidenceIngestionResult(replay, replayed=True)
            if stored.created:
                self._delete_if_unlinked(stored.storage_key)
            raise ExecutionEvidenceError(
                "execution_evidence_insert_conflict",
                "evidence metadata conflicts with canonical authority",
            ) from exc
        return ExecutionEvidenceIngestionResult(row)

    def _delete_if_unlinked(self, storage_key: str) -> None:
        linked = (
            self.db.query(ExecutionEvidence)
            .filter(ExecutionEvidence.storage_key == storage_key)
            .count()
        )
        linked += (
            self.db.query(ExecutionTaskCandidateContent)
            .filter(ExecutionTaskCandidateContent.storage_key == storage_key)
            .count()
        )
        if linked == 0:
            self.store.delete_if_unreferenced(storage_key)

    def _authorize(
        self, command: IngestExecutionEvidenceCommand, *, evidence_kind: str
    ) -> tuple[ExecutionPlan, ExecutionTask, ExecutionTaskAttempt]:
        plan = self.db.get(ExecutionPlan, int(command.execution_plan_id))
        task = self.db.get(ExecutionTask, int(command.execution_task_id))
        attempt = self.db.get(
            ExecutionTaskAttempt, int(command.execution_task_attempt_id)
        )
        if any(item is None for item in (plan, task, attempt)):
            raise ExecutionEvidenceError(
                "execution_evidence_authority_missing",
                "execution evidence authority is incomplete",
            )
        assert plan is not None and task is not None and attempt is not None
        if plan.status != "active" or plan.superseded_by_execution_plan_id is not None:
            raise ExecutionEvidenceError(
                "execution_evidence_authority_invalid", "execution plan is not active"
            )
        if (
            task.execution_plan_id != plan.id
            or attempt.execution_plan_id != plan.id
            or attempt.execution_task_id != task.id
        ):
            raise ExecutionEvidenceError(
                "execution_evidence_authority_invalid",
                "execution evidence is not bound to a consistent plan/task/attempt",
            )
        if attempt.attempt_generation != int(command.attempt_generation):
            raise ExecutionEvidenceError(
                "execution_evidence_authority_invalid",
                "attempt generation does not match the immutable attempt identity",
            )
        return plan, task, attempt


def verify_execution_evidence_integrity(
    db: Session,
    evidence_id: int,
    *,
    store: CandidateContentStore | None = None,
) -> ExecutionEvidenceIntegrityResult:
    row = db.get(ExecutionEvidence, int(evidence_id))
    if row is None:
        return ExecutionEvidenceIntegrityResult(
            None, None, False, ("execution_evidence_missing",)
        )
    issues: list[str] = []
    plan = db.get(ExecutionPlan, row.execution_plan_id)
    task = db.get(ExecutionTask, row.execution_task_id)
    attempt = db.get(ExecutionTaskAttempt, row.execution_task_attempt_id)
    if any(item is None for item in (plan, task, attempt)):
        issues.append("execution_evidence_authority_missing")
    if task is not None and task.execution_plan_id != row.execution_plan_id:
        issues.append("execution_evidence_task_plan_mismatch")
    if attempt is not None and (
        attempt.execution_plan_id != row.execution_plan_id
        or attempt.execution_task_id != row.execution_task_id
        or attempt.attempt_generation != row.attempt_generation
    ):
        issues.append("execution_evidence_attempt_linkage_mismatch")
    if row.evidence_kind not in SUPPORTED_EVIDENCE_KINDS:
        issues.append("execution_evidence_kind_invalid")
    elif EVIDENCE_KIND_PRODUCERS.get(row.evidence_kind) != row.producer_id:
        issues.append("execution_evidence_producer_invalid")
    if row.producer_id not in SUPPORTED_EVIDENCE_PRODUCERS:
        issues.append("execution_evidence_producer_invalid")
    if not _HASH_RE.fullmatch(str(row.content_sha256 or "")):
        issues.append("execution_evidence_hash_malformed")
    if row.declared_sha256 is not None and not _HASH_RE.fullmatch(row.declared_sha256):
        issues.append("execution_evidence_declared_hash_malformed")
    if row.media_type not in MEDIA_TYPES:
        issues.append("execution_evidence_media_type_invalid")
    if row.byte_length < 0 or row.byte_length > MAX_EVIDENCE_BYTES:
        issues.append("execution_evidence_size_invalid")
    if (
        row.storage_backend_id != CONTENT_STORE_BACKEND_ID
        or row.storage_backend_version != CONTENT_STORE_BACKEND_VERSION
    ):
        issues.append("execution_evidence_store_identity_invalid")
    try:
        expected_metadata = _metadata_payload(row)
        if row.canonical_metadata_payload != expected_metadata:
            issues.append("execution_evidence_metadata_tampered")
        if (
            canonical_json_hash(row.canonical_metadata_payload)
            != row.canonical_metadata_hash
        ):
            issues.append("execution_evidence_metadata_hash_mismatch")
        if (
            canonical_json_hash(row.canonical_ingestion_command_payload)
            != row.canonical_ingestion_command_hash
        ):
            issues.append("execution_evidence_ingestion_hash_mismatch")
        if (
            row.canonical_ingestion_command_payload.get("content_sha256")
            != row.content_sha256
        ):
            issues.append("execution_evidence_ingestion_payload_tampered")
    except (TypeError, ValueError, AttributeError):
        issues.append("execution_evidence_payload_malformed")
    reader = store or LocalContentAddressedStore()
    try:
        data = reader.read(row.storage_key)
        if len(data) != row.byte_length:
            issues.append("execution_evidence_size_mismatch")
        if _hash_bytes(data) != row.content_sha256:
            issues.append("execution_evidence_hash_mismatch")
    except CandidateContentError as exc:
        issues.append(exc.code)
    return ExecutionEvidenceIntegrityResult(
        row.execution_plan_id,
        row.execution_task_id,
        not issues,
        tuple(sorted(set(issues))),
    )


def resolve_execution_evidence_reference(
    db: Session,
    reference: str,
    *,
    store: CandidateContentStore | None = None,
) -> ExecutionEvidenceResolution:
    """Read-only resolution of an ``execution-evidence://`` reference.

    This is a validation-resolver primitive only: it reads one immutable
    authority row (or none) and reports its identity plus integrity.  It
    never mutates lifecycle state and is not wired into the C7B acceptance
    path in this phase.
    """

    parsed = parse_execution_evidence_reference(reference)
    if parsed.scheme == "execution-evidence":
        row = db.get(ExecutionEvidence, int(parsed.identifier))
    else:
        matches = (
            db.query(ExecutionEvidence)
            .filter(ExecutionEvidence.content_sha256 == str(parsed.identifier))
            .limit(2)
            .all()
        )
        if len(matches) > 1:
            return ExecutionEvidenceResolution(
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "ambiguous_reference",
                False,
                ("execution_evidence_reference_ambiguous",),
            )
        row = matches[0] if matches else None
    if row is None:
        return ExecutionEvidenceResolution(
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "missing",
            False,
            ("execution_evidence_missing",),
        )
    integrity = verify_execution_evidence_integrity(db, row.id, store=store)
    return ExecutionEvidenceResolution(
        evidence_id=row.id,
        evidence_kind=row.evidence_kind,
        producer_id=row.producer_id,
        producer_version=row.producer_version,
        media_type=row.media_type,
        byte_length=row.byte_length,
        content_sha256=row.content_sha256,
        storage_backend_id=row.storage_backend_id,
        storage_backend_version=row.storage_backend_version,
        resolution_status="resolved" if integrity.verified else "integrity_failure",
        verified=integrity.verified,
        issues=integrity.issues,
    )


def cleanup_unlinked_execution_evidence(
    db: Session, *, store: CandidateContentStore | None = None
) -> tuple[str, ...]:
    """Delete only hash objects with no committed linkage in either authority.

    The blob store is shared with Phase 29C-9 candidate content, so a key is
    only orphaned when neither table references it.
    """

    reader = store or LocalContentAddressedStore()
    linked = {row.storage_key for row in db.query(ExecutionEvidence.storage_key).all()}
    linked |= {
        row.storage_key
        for row in db.query(ExecutionTaskCandidateContent.storage_key).all()
    }
    removed: list[str] = []
    for key in reader.list_storage_keys():
        if key not in linked:
            reader.delete_if_unreferenced(key)
            removed.append(key)
    return tuple(sorted(removed))


__all__ = [
    "EVIDENCE_KIND_PRODUCERS",
    "EXECUTION_EVIDENCE_REFERENCE_GRAMMAR_VERSION",
    "EXECUTION_EVIDENCE_SCHEMA_VERSION",
    "MAX_EVIDENCE_BYTES",
    "SUPPORTED_EVIDENCE_KINDS",
    "SUPPORTED_EVIDENCE_PRODUCERS",
    "ExecutionEvidenceError",
    "ExecutionEvidenceIngestionResult",
    "ExecutionEvidenceIngestionService",
    "ExecutionEvidenceIntegrityResult",
    "ExecutionEvidenceReference",
    "ExecutionEvidenceResolution",
    "IngestExecutionEvidenceCommand",
    "cleanup_unlinked_execution_evidence",
    "evidence_reference_for_id",
    "normalize_evidence_media_type",
    "normalize_execution_evidence_reference",
    "parse_execution_evidence_reference",
    "resolve_execution_evidence_reference",
    "verify_execution_evidence_integrity",
]
