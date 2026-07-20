"""Immutable Protocol v2 operator-review values and canonical hashing.

Review records are authority metadata only.  They bind an existing failed
checkpoint to an operator decision; they never contain or replace canonical
Brief or Structured Task Plan content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import unicodedata
import uuid
from typing import Any, Mapping, Sequence


REVIEW_SCHEMA_VERSION = "protocol-v2-review-event/1.0"
REVIEW_POLICY_VERSION = "protocol-v2-review-policy/1.0"
REVIEW_POLICY_DEFAULTS = {
    "approval_quorum": 1,
    "human_approval_required": True,
    "provider_self_approval": False,
    "approval_comment_required": True,
    "rejection_reason_required": True,
    "approval_expiry": "none",
    "newer_candidate_supersedes": True,
    "amendment_mode": "regenerate_only",
    "required_review_blocks_completion": True,
    "rejected_candidate_retention_days": 90,
    "supported_stages": ("planning_brief", "structured_task_plan"),
}

REVIEW_EVENT_TYPES = frozenset(
    {
        "review_opened",
        "acknowledge_only",
        "approve_unchanged",
        "reject",
        "request_regeneration",
        "request_amendment",
        "cancel_review",
    }
)
TERMINAL_REVIEW_EVENT_TYPES = frozenset(
    {
        "approve_unchanged",
        "reject",
        "request_regeneration",
        "request_amendment",
        "cancel_review",
    }
)
REVIEW_REASON_CODES = frozenset(
    {
        "acceptance_policy_operator_action",
        "acceptance_policy_review_required_task",
        "acceptance_policy_review_gate",
        "acceptance_policy_atomicity_review",
        "acceptance_policy_capacity_review",
        "explicit_operator_review",
    }
)
ELIGIBILITY_CLASSES = (
    "valid_review_required",
    "invalid",
    "stale",
    "already_accepted",
    "already_rejected",
    "superseded",
    "not_protocol_v2",
    "post_commit_unreviewable",
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNSAFE_MARKUP_RE = re.compile(r"(?:<[^>]{1,256}>|javascript\s*:|data\s*:)")
_CREDENTIAL_SHAPED_RE = re.compile(
    r"(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|bearer)"
    r"\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ReviewDomainError(ValueError):
    """A review value violates its immutable application contract."""


def _nfc(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value)).strip()


def _bounded_text(value: Any, field_name: str, limit: int, *, required: bool) -> str:
    text = _nfc(value)
    if required and not text:
        raise ReviewDomainError(f"{field_name} is required")
    if len(text) > limit:
        raise ReviewDomainError(f"{field_name} exceeds {limit} characters")
    if _CONTROL_RE.search(text):
        raise ReviewDomainError(f"{field_name} contains control characters")
    if _UNSAFE_MARKUP_RE.search(text):
        raise ReviewDomainError(f"{field_name} contains unsafe markup")
    if _CREDENTIAL_SHAPED_RE.search(text):
        raise ReviewDomainError(f"{field_name} contains credential-shaped content")
    return text


def _freeze(value: Any) -> Any:
    """Convert JSON-shaped values into immutable tuples recursively."""

    if isinstance(value, Mapping):
        return tuple(
            (str(key), _freeze(value[key]))
            for key in sorted(value, key=lambda item: str(item))
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ReviewDomainError("non-finite JSON values are forbidden")
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, tuple):
        if all(isinstance(item, tuple) and len(item) == 2 for item in value):
            return {str(key): _thaw(item) for key, item in value}
        return [_thaw(item) for item in value]
    return value


def _canonicalize(value: Any) -> Any:
    """Return a JSON-shaped NFC-normalized value with deterministic objects."""

    if isinstance(value, Mapping):
        return {
            unicodedata.normalize("NFC", str(key)): _canonicalize(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ReviewDomainError("non-finite JSON values are forbidden")
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON compactly, with sorted keys and NFC Unicode."""

    try:
        return json.dumps(
            _canonicalize(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewDomainError("review payload is not finite JSON") from exc


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _hash(value: Any, field_name: str) -> str:
    normalized = _nfc(value).lower()
    if not _HASH_RE.fullmatch(normalized):
        raise ReviewDomainError(f"{field_name} must be a lowercase SHA-256 hash")
    return normalized


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ReviewPredecessorBinding:
    checkpoint_id: int
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "checkpoint_id", int(self.checkpoint_id))
        object.__setattr__(
            self, "content_hash", _hash(self.content_hash, "content_hash")
        )

    def to_dict(self) -> dict[str, Any]:
        return {"checkpoint_id": self.checkpoint_id, "content_hash": self.content_hash}


@dataclass(frozen=True)
class ReviewCandidateBinding:
    planning_session_id: int
    project_id: int
    protocol_version: str
    session_generation_id: str
    stage_name: str
    stage_version: int
    stage_generation_id: str
    candidate_checkpoint_id: int
    candidate_checkpoint_version: int
    candidate_content_hash: str
    validation_hash: str
    validator_version: str
    input_manifest_id: str
    input_manifest_hash: str
    predecessors: tuple[ReviewPredecessorBinding, ...] = ()
    accepted_brief_checkpoint_id: int | None = None
    accepted_brief_hash: str | None = None
    stage_configuration_fingerprint: str = ""
    candidate_attempt_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("planning_session_id", "project_id", "candidate_checkpoint_id"):
            object.__setattr__(self, name, int(getattr(self, name)))
        for name in ("stage_version", "candidate_checkpoint_version"):
            value = int(getattr(self, name))
            if value < 1:
                raise ReviewDomainError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "protocol_version",
            _bounded_text(
                self.protocol_version, "protocol_version", 16, required=True
            ).lower(),
        )
        if self.protocol_version != "v2":
            raise ReviewDomainError("review candidates require Protocol v2")
        for name in (
            "session_generation_id",
            "stage_name",
            "stage_generation_id",
            "validator_version",
            "input_manifest_id",
        ):
            object.__setattr__(
                self, name, _bounded_text(getattr(self, name), name, 256, required=True)
            )
        for name in (
            "candidate_content_hash",
            "validation_hash",
            "input_manifest_hash",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), name))
        config = _bounded_text(
            self.stage_configuration_fingerprint,
            "stage_configuration_fingerprint",
            128,
            required=True,
        )
        object.__setattr__(
            self,
            "stage_configuration_fingerprint",
            _hash(config, "stage_configuration_fingerprint"),
        )
        if self.accepted_brief_checkpoint_id is not None:
            object.__setattr__(
                self,
                "accepted_brief_checkpoint_id",
                int(self.accepted_brief_checkpoint_id),
            )
        if self.accepted_brief_hash is not None:
            object.__setattr__(
                self,
                "accepted_brief_hash",
                _hash(self.accepted_brief_hash, "accepted_brief_hash"),
            )
        object.__setattr__(
            self,
            "candidate_attempt_id",
            _bounded_text(
                self.candidate_attempt_id, "candidate_attempt_id", 128, required=False
            )
            or None,
        )
        predecessors = tuple(
            (
                item
                if isinstance(item, ReviewPredecessorBinding)
                else ReviewPredecessorBinding(**item)
            )
            for item in self.predecessors
        )
        object.__setattr__(self, "predecessors", predecessors)

    @property
    def candidate_hash(self) -> str:
        return self.candidate_content_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "planning_session_id": self.planning_session_id,
            "project_id": self.project_id,
            "protocol_version": self.protocol_version,
            "session_generation_id": self.session_generation_id,
            "stage_name": self.stage_name,
            "stage_version": self.stage_version,
            "stage_generation_id": self.stage_generation_id,
            "candidate_checkpoint_id": self.candidate_checkpoint_id,
            "candidate_checkpoint_version": self.candidate_checkpoint_version,
            "candidate_content_hash": self.candidate_content_hash,
            "validation_hash": self.validation_hash,
            "validator_version": self.validator_version,
            "input_manifest_id": self.input_manifest_id,
            "input_manifest_hash": self.input_manifest_hash,
            "predecessors": [item.to_dict() for item in self.predecessors],
            "accepted_brief_checkpoint_id": self.accepted_brief_checkpoint_id,
            "accepted_brief_hash": self.accepted_brief_hash,
            "stage_configuration_fingerprint": self.stage_configuration_fingerprint,
            "candidate_attempt_id": self.candidate_attempt_id,
        }


@dataclass(frozen=True)
class ReviewValidationSnapshot:
    validator_version: str
    validation_hash: str
    schema_valid: bool
    semantically_valid: bool
    protocol_acceptable: bool
    review_reason_codes: tuple[str, ...] = ()
    errors: tuple[tuple[str, str, str, str], ...] = ()
    warnings: tuple[tuple[str, str, str, str], ...] = ()
    snapshot: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "validator_version",
            _bounded_text(
                self.validator_version, "validator_version", 128, required=True
            ),
        )
        object.__setattr__(
            self, "validation_hash", _hash(self.validation_hash, "validation_hash")
        )
        codes = tuple(
            sorted(
                {
                    _bounded_text(code, "review_reason_code", 128, required=True)
                    for code in self.review_reason_codes
                }
            )
        )
        object.__setattr__(self, "review_reason_codes", codes)
        object.__setattr__(
            self,
            "errors",
            tuple(tuple(_nfc(part) for part in item) for item in self.errors),
        )
        object.__setattr__(
            self,
            "warnings",
            tuple(tuple(_nfc(part) for part in item) for item in self.warnings),
        )
        object.__setattr__(
            self, "snapshot", _freeze(dict(self.snapshot)) if self.snapshot else ()
        )

    @classmethod
    def from_validation(
        cls,
        validation: Any,
        *,
        review_reason_codes: Sequence[str] = (),
        extra: Mapping[str, Any] | None = None,
    ) -> "ReviewValidationSnapshot":
        def issues(items: Sequence[Any]) -> tuple[tuple[str, str, str, str], ...]:
            return tuple(
                (
                    _nfc(getattr(item, "code", "")),
                    _nfc(getattr(item, "path", "")),
                    _nfc(getattr(item, "message", "")),
                    _nfc(getattr(item, "severity", "error")),
                )
                for item in items
            )

        snapshot = dict(validation.to_dict())
        if extra:
            snapshot.update(dict(extra))
        return cls(
            validator_version=str(getattr(validation, "validator_version", "")),
            validation_hash=str(getattr(validation, "validation_hash")),
            schema_valid=bool(getattr(validation, "schema_valid", False)),
            semantically_valid=bool(getattr(validation, "semantically_valid", False)),
            protocol_acceptable=bool(getattr(validation, "protocol_acceptable", False)),
            review_reason_codes=tuple(review_reason_codes),
            errors=issues(getattr(validation, "errors", ())),
            warnings=issues(getattr(validation, "warnings", ())),
            snapshot=tuple((str(key), value) for key, value in snapshot.items()),
        )

    def to_dict(self) -> dict[str, Any]:
        result = _thaw(self.snapshot) if self.snapshot else {}
        result.update(
            {
                "schema_valid": self.schema_valid,
                "semantically_valid": self.semantically_valid,
                "protocol_acceptable": self.protocol_acceptable,
                "validator_version": self.validator_version,
                "validation_hash": self.validation_hash,
                "review_reason_codes": list(self.review_reason_codes),
                "errors": [
                    dict(zip(("code", "path", "message", "severity"), item))
                    for item in self.errors
                ],
                "warnings": [
                    dict(zip(("code", "path", "message", "severity"), item))
                    for item in self.warnings
                ],
            }
        )
        return result


@dataclass(frozen=True)
class ReviewActor:
    subject: str
    role: str
    authority_basis: str
    actor_kind: str = "human"
    authorized: bool = True

    def __post_init__(self) -> None:
        for name in ("subject", "role", "authority_basis", "actor_kind"):
            object.__setattr__(
                self, name, _bounded_text(getattr(self, name), name, 128, required=True)
            )
        object.__setattr__(self, "actor_kind", self.actor_kind.lower())

    @property
    def is_human(self) -> bool:
        return self.actor_kind == "human"

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "role": self.role,
            "authority_basis": self.authority_basis,
            "actor_kind": self.actor_kind,
            "authorized": self.authorized,
        }


@dataclass(frozen=True)
class ReviewDecisionRequest:
    idempotency_key: str
    candidate_binding: ReviewCandidateBinding | None = None
    comment: str | None = None
    reason: str | None = None
    expected_head_sequence: int | None = None
    expected_head_token: str | None = None
    guidance: str | None = None
    amendment_id: str | None = None
    amendment_hash: str | None = None
    command_identity: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "idempotency_key",
            _bounded_text(self.idempotency_key, "idempotency_key", 128, required=True),
        )
        if self.candidate_binding is not None and not isinstance(
            self.candidate_binding, ReviewCandidateBinding
        ):
            object.__setattr__(
                self,
                "candidate_binding",
                ReviewCandidateBinding(**dict(self.candidate_binding)),
            )
        for name, limit in (
            ("comment", 4096),
            ("reason", 4096),
            ("guidance", 2048),
            ("amendment_id", 128),
            ("command_identity", 128),
        ):
            value = getattr(self, name)
            object.__setattr__(
                self, name, _bounded_text(value, name, limit, required=False) or None
            )
        if self.amendment_hash is not None:
            object.__setattr__(
                self, "amendment_hash", _hash(self.amendment_hash, "amendment_hash")
            )
        if (
            self.expected_head_sequence is not None
            and int(self.expected_head_sequence) < 0
        ):
            raise ReviewDomainError("expected_head_sequence must not be negative")
        if self.expected_head_sequence is not None:
            object.__setattr__(
                self, "expected_head_sequence", int(self.expected_head_sequence)
            )
        if self.expected_head_token is not None:
            object.__setattr__(
                self,
                "expected_head_token",
                _bounded_text(
                    self.expected_head_token, "expected_head_token", 128, required=True
                ),
            )

    def canonical_payload(
        self, event_type: str, review_id: str, binding: ReviewCandidateBinding
    ) -> dict[str, Any]:
        return {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "event_type": event_type,
            "review_id": review_id,
            "candidate_binding": binding.to_dict(),
            "idempotency_key": self.idempotency_key,
            "comment": self.comment,
            "reason": self.reason,
            "expected_head_sequence": self.expected_head_sequence,
            "expected_head_token": self.expected_head_token,
            "guidance": self.guidance,
            "amendment_id": self.amendment_id,
            "amendment_hash": self.amendment_hash,
            "command_identity": self.command_identity,
        }

    def canonical_hash(
        self, event_type: str, review_id: str, binding: ReviewCandidateBinding
    ) -> str:
        return canonical_json_hash(
            self.canonical_payload(event_type, review_id, binding)
        )


@dataclass(frozen=True)
class PlanningReviewEvent:
    event_id: str
    review_id: str
    event_sequence: int
    event_type: str
    candidate_binding: ReviewCandidateBinding
    validation: ReviewValidationSnapshot
    actor: ReviewActor
    idempotency_key: str
    canonical_request_hash: str
    prior_review_head_sequence: int
    resulting_sequence: int
    review_concurrency_token: str
    decision_text: str | None = None
    command_identity: str | None = None
    amendment_id: str | None = None
    amendment_hash: str | None = None
    previous_event_hash: str | None = None
    created_at: datetime = field(default_factory=_now)
    schema_version: str = REVIEW_SCHEMA_VERSION
    event_hash: str = ""
    promotion_checkpoint_id: int | None = None

    def __post_init__(self) -> None:
        event_id = _bounded_text(
            self.event_id or str(uuid.uuid4()), "event_id", 128, required=True
        )
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(
            self,
            "review_id",
            _bounded_text(self.review_id, "review_id", 128, required=True),
        )
        if self.event_type not in REVIEW_EVENT_TYPES:
            raise ReviewDomainError(f"unsupported review event type: {self.event_type}")
        if int(self.event_sequence) < 1 or int(self.resulting_sequence) != int(
            self.event_sequence
        ):
            raise ReviewDomainError("review event sequence is invalid")
        object.__setattr__(self, "event_sequence", int(self.event_sequence))
        object.__setattr__(self, "resulting_sequence", int(self.resulting_sequence))
        object.__setattr__(
            self, "prior_review_head_sequence", int(self.prior_review_head_sequence)
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _bounded_text(self.idempotency_key, "idempotency_key", 128, required=True),
        )
        object.__setattr__(
            self,
            "review_concurrency_token",
            _bounded_text(
                self.review_concurrency_token,
                "review_concurrency_token",
                128,
                required=True,
            ),
        )
        if self.previous_event_hash is not None:
            object.__setattr__(
                self,
                "previous_event_hash",
                _hash(self.previous_event_hash, "previous_event_hash"),
            )
        for name in ("decision_text", "command_identity", "amendment_id"):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                _bounded_text(
                    value,
                    name,
                    4096 if name == "decision_text" else 128,
                    required=False,
                )
                or None,
            )
        if self.amendment_hash is not None:
            object.__setattr__(
                self, "amendment_hash", _hash(self.amendment_hash, "amendment_hash")
            )
        if self.promotion_checkpoint_id is not None:
            object.__setattr__(
                self, "promotion_checkpoint_id", int(self.promotion_checkpoint_id)
            )
        if not self.event_hash:
            object.__setattr__(self, "event_hash", event_hash(self))
        elif self.event_hash != event_hash(self, include_hash=False):
            raise ReviewDomainError("event_hash does not match canonical event")

    def canonical_payload(self, *, include_hash: bool = False) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "review_id": self.review_id,
            "event_sequence": self.event_sequence,
            "event_type": self.event_type,
            "candidate_binding": self.candidate_binding.to_dict(),
            "validation": self.validation.to_dict(),
            "actor": self.actor.to_dict(),
            "idempotency_key": self.idempotency_key,
            "canonical_request_hash": self.canonical_request_hash,
            "prior_review_head_sequence": self.prior_review_head_sequence,
            "resulting_sequence": self.resulting_sequence,
            "review_concurrency_token": self.review_concurrency_token,
            "decision_text": self.decision_text,
            "command_identity": self.command_identity,
            "amendment_id": self.amendment_id,
            "amendment_hash": self.amendment_hash,
            "previous_event_hash": self.previous_event_hash,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "promotion_checkpoint_id": self.promotion_checkpoint_id,
        }
        if include_hash:
            payload["event_hash"] = self.event_hash
        return payload

    def canonical_bytes(self, *, include_hash: bool = True) -> bytes:
        return canonical_json_bytes(self.canonical_payload(include_hash=include_hash))


def event_hash(event: PlanningReviewEvent, *, include_hash: bool = False) -> str:
    del include_hash  # kept for a clear call site when verifying persisted rows
    return canonical_json_hash(event.canonical_payload(include_hash=False))


@dataclass(frozen=True)
class ReviewConflict:
    code: str
    message: str
    review_id: str | None = None
    candidate_checkpoint_id: int | None = None


class ReviewOperationError(ReviewDomainError):
    def __init__(self, conflict: ReviewConflict):
        super().__init__(f"{conflict.code}: {conflict.message}")
        self.conflict = conflict


class ReviewIntegrityError(ReviewDomainError):
    pass


@dataclass(frozen=True)
class PromotionCheckpointResult:
    checkpoint_id: int
    checkpoint_version: int
    content_hash: str
    approval_event_id: str
    stage_name: str


@dataclass(frozen=True)
class ReviewDecisionResult:
    review_id: str
    event_id: str
    event_type: str
    state: str
    promotion: PromotionCheckpointResult | None = None
    replayed: bool = False
    completion_reevaluation_requested: bool = False


@dataclass(frozen=True)
class ReviewEligibilityResult:
    classification: str
    eligible: bool
    binding: ReviewCandidateBinding | None = None
    validation: ReviewValidationSnapshot | None = None
    reason_codes: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.classification not in ELIGIBILITY_CLASSES:
            raise ReviewDomainError(
                f"unknown eligibility classification: {self.classification}"
            )
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


@dataclass(frozen=True)
class ReviewAggregate:
    review_id: str
    events: tuple[PlanningReviewEvent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))


@dataclass(frozen=True)
class ReviewProjection:
    review_id: str
    candidate_binding: ReviewCandidateBinding
    state: str
    current_sequence: int
    review_head_token: str
    validation_state: str
    review_required_reasons: tuple[str, ...]
    allowed_decisions: tuple[str, ...]
    actor_history: tuple[ReviewActor, ...]
    decision_history: tuple[tuple[str, str, str | None], ...] = ()
    terminal_decision: str | None = None
    terminal_event_id: str | None = None
    accepted_promotion_checkpoint_id: int | None = None
    accepted_promotion_hash: str | None = None
    rejection_reason: str | None = None
    cancellation_reason: str | None = None
    command_identity: str | None = None
    amendment_id: str | None = None
    amendment_hash: str | None = None
    stale: bool = False
    superseded: bool = False
    current_accepted_artifact_id: int | None = None
    current_accepted_artifact_hash: str | None = None
    integrity_error: str | None = None


def project_review(aggregate: ReviewAggregate) -> ReviewProjection:
    """Derive review state from a verified append-only event tuple."""

    if not aggregate.events:
        raise ReviewIntegrityError("review has no events")
    events = tuple(sorted(aggregate.events, key=lambda item: item.event_sequence))
    first = events[0]
    if first.event_type != "review_opened" or first.event_sequence != 1:
        raise ReviewIntegrityError("review stream does not begin with review_opened")
    binding = first.candidate_binding
    terminal = next(
        (item for item in events if item.event_type in TERMINAL_REVIEW_EVENT_TYPES),
        None,
    )
    if terminal is not None:
        state = {
            "approve_unchanged": "approved",
            "reject": "rejected",
            "request_regeneration": "regeneration_requested",
            "request_amendment": "amendment_requested",
            "cancel_review": "cancelled",
        }[terminal.event_type]
        allowed: tuple[str, ...] = ()
    else:
        state = "pending"
        allowed = (
            "approve_unchanged",
            "reject",
            "request_regeneration",
            "request_amendment",
            "cancel_review",
            "acknowledge_only",
        )
    promotion = next(
        (item for item in events if item.event_type == "approve_unchanged"), None
    )
    rejection = next((item for item in events if item.event_type == "reject"), None)
    cancellation = next(
        (item for item in events if item.event_type == "cancel_review"), None
    )
    command = next((item for item in reversed(events) if item.command_identity), None)
    return ReviewProjection(
        review_id=aggregate.review_id,
        candidate_binding=binding,
        state=state,
        current_sequence=events[-1].event_sequence,
        review_head_token=events[-1].event_hash,
        validation_state=(
            "review_required" if first.validation.review_reason_codes else "valid"
        ),
        review_required_reasons=first.validation.review_reason_codes,
        allowed_decisions=allowed,
        actor_history=tuple(item.actor for item in events),
        decision_history=tuple(
            (item.event_id, item.event_type, item.decision_text) for item in events
        ),
        terminal_decision=terminal.event_type if terminal else None,
        terminal_event_id=terminal.event_id if terminal else None,
        accepted_promotion_checkpoint_id=(
            promotion.promotion_checkpoint_id if promotion else None
        ),
        accepted_promotion_hash=(binding.candidate_content_hash if promotion else None),
        rejection_reason=rejection.decision_text if rejection else None,
        cancellation_reason=cancellation.decision_text if cancellation else None,
        command_identity=command.command_identity if command else None,
        amendment_id=command.amendment_id if command else None,
        amendment_hash=command.amendment_hash if command else None,
    )


def verify_event_hash(event: PlanningReviewEvent) -> None:
    expected = event_hash(event)
    if event.event_hash != expected:
        raise ReviewIntegrityError(f"review event {event.event_id} hash mismatch")


__all__ = [
    "ELIGIBILITY_CLASSES",
    "REVIEW_EVENT_TYPES",
    "REVIEW_POLICY_DEFAULTS",
    "REVIEW_POLICY_VERSION",
    "REVIEW_REASON_CODES",
    "REVIEW_SCHEMA_VERSION",
    "ReviewActor",
    "ReviewAggregate",
    "ReviewCandidateBinding",
    "ReviewConflict",
    "ReviewDecisionRequest",
    "ReviewDecisionResult",
    "ReviewDomainError",
    "ReviewEligibilityResult",
    "ReviewIntegrityError",
    "ReviewOperationError",
    "ReviewPredecessorBinding",
    "ReviewProjection",
    "ReviewValidationSnapshot",
    "PlanningReviewEvent",
    "PromotionCheckpointResult",
    "canonical_json_bytes",
    "canonical_json_hash",
    "event_hash",
    "project_review",
    "verify_event_hash",
]
