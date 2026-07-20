"""Persistence and application services for Protocol v2 operator review.

The service owns exact candidate binding, deterministic eligibility, append-only
event writes, idempotency, and Option A accepted promotion.  Callers own the
surrounding SQLAlchemy commit, matching the existing planning persistence
services.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import uuid
from typing import Any, Mapping, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    PlanningCheckpoint,
    PlanningCheckpointDependency,
    PlanningReviewEvent,
    PlanningSession,
)
from app.services.planning.input_manifest import InputManifest
from app.services.planning.operator_review import (
    ELIGIBILITY_CLASSES,
    REVIEW_EVENT_TYPES,
    REVIEW_REASON_CODES,
    REVIEW_SCHEMA_VERSION,
    TERMINAL_REVIEW_EVENT_TYPES,
    PlanningReviewEvent as DomainReviewEvent,
    PromotionCheckpointResult,
    ReviewActor,
    ReviewAggregate,
    ReviewCandidateBinding,
    ReviewConflict,
    ReviewDecisionRequest,
    ReviewDecisionResult,
    ReviewEligibilityResult,
    ReviewIntegrityError,
    ReviewOperationError,
    ReviewPredecessorBinding,
    ReviewProjection,
    ReviewValidationSnapshot,
    canonical_json_hash,
    project_review,
    verify_event_hash,
)
from app.services.planning.planning_brief import (
    PLANNING_BRIEF_RENDERER_VERSION,
    PLANNING_BRIEF_SCHEMA_VERSION,
    PLANNING_BRIEF_STAGE_NAME,
    PLANNING_BRIEF_STAGE_VERSION,
    PLANNING_BRIEF_VALIDATOR_VERSION,
    PlanningBrief,
    PlanningBriefSchemaError,
    validate_planning_brief,
)
from app.services.planning.protocol_persistence import (
    PROTOCOL_V2,
    PlanningProtocolPersistenceService,
    ProtocolPersistenceError,
)
from app.services.planning.structured_task_plan import (
    DEFAULT_TASK_PLAN_POLICY,
    STRUCTURED_TASK_PLAN_RENDERER_VERSION,
    STRUCTURED_TASK_PLAN_SCHEMA_VERSION,
    STRUCTURED_TASK_PLAN_STAGE_NAME,
    STRUCTURED_TASK_PLAN_STAGE_VERSION,
    STRUCTURED_TASK_PLAN_VALIDATOR_VERSION,
    StructuredTaskPlan,
    StructuredTaskPlanSchemaError,
    validate_structured_task_plan,
)


TERMINAL_DECISION_CONFLICTS = {
    "approve_unchanged": "review_already_decided",
    "reject": "review_already_decided",
    "request_regeneration": "review_already_decided",
    "request_amendment": "review_already_decided",
    "cancel_review": "review_already_decided",
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _event_snapshot_from_model(row: PlanningReviewEvent) -> ReviewValidationSnapshot:
    raw = row.validation_json or {}
    errors = tuple(
        tuple(
            str(item.get(name, "")) for name in ("code", "path", "message", "severity")
        )
        for item in raw.get("errors", [])
        if isinstance(item, Mapping)
    )
    warnings = tuple(
        tuple(
            str(item.get(name, "")) for name in ("code", "path", "message", "severity")
        )
        for item in raw.get("warnings", [])
        if isinstance(item, Mapping)
    )
    return ReviewValidationSnapshot(
        validator_version=str(row.validator_version),
        validation_hash=str(row.validation_hash),
        schema_valid=bool(raw.get("schema_valid", False)),
        semantically_valid=bool(raw.get("semantically_valid", False)),
        protocol_acceptable=bool(raw.get("protocol_acceptable", False)),
        review_reason_codes=tuple(row.review_reason_codes or ()),
        errors=errors,
        warnings=warnings,
        snapshot=tuple((str(key), value) for key, value in raw.items()),
    )


def event_from_model(row: PlanningReviewEvent) -> DomainReviewEvent:
    """Rehydrate and hash-check one persisted event."""

    binding = ReviewCandidateBinding(**dict(row.candidate_binding_json or {}))
    actor = ReviewActor(
        subject=str(row.operator_subject),
        role=str(row.operator_role),
        authority_basis=str(row.authority_basis),
        actor_kind=str(row.actor_kind),
    )
    try:
        event = DomainReviewEvent(
            event_id=str(row.event_id),
            review_id=str(row.review_id),
            event_sequence=int(row.event_sequence),
            event_type=str(row.event_type),
            candidate_binding=binding,
            validation=_event_snapshot_from_model(row),
            actor=actor,
            idempotency_key=str(row.idempotency_key),
            canonical_request_hash=str(row.canonical_request_hash),
            prior_review_head_sequence=int(row.prior_review_head_sequence),
            resulting_sequence=int(row.resulting_sequence),
            review_concurrency_token=str(row.review_concurrency_token),
            decision_text=row.decision_text,
            command_identity=row.command_identity,
            amendment_id=row.amendment_id,
            amendment_hash=row.amendment_hash,
            previous_event_hash=row.previous_event_hash,
            created_at=_utc(row.created_at),
            schema_version=str(row.schema_version),
            event_hash=str(row.event_hash),
            promotion_checkpoint_id=row.promotion_checkpoint_id,
        )
    except Exception as exc:
        raise ReviewIntegrityError(f"review event {row.event_id} is malformed") from exc
    verify_event_hash(event)
    return event


class OperatorReviewPersistenceService:
    """Append-only Protocol v2 review persistence and decision boundaries."""

    def __init__(self, db: Session):
        self.db = db
        self.protocol = PlanningProtocolPersistenceService(db)

    def _session(self, session_id: int, *, lock: bool = False) -> PlanningSession:
        query = self.db.query(PlanningSession).filter(PlanningSession.id == session_id)
        if lock:
            query = query.with_for_update()
        session = query.populate_existing().one_or_none()
        if session is None:
            raise ReviewOperationError(
                ReviewConflict("candidate_not_approvable", "planning session not found")
            )
        return session

    def _candidate(self, session_id: int, checkpoint_id: int) -> PlanningCheckpoint:
        candidate = (
            self.db.query(PlanningCheckpoint)
            .filter(
                PlanningCheckpoint.id == int(checkpoint_id),
                PlanningCheckpoint.planning_session_id == int(session_id),
            )
            .populate_existing()
            .one_or_none()
        )
        if candidate is None:
            raise ReviewOperationError(
                ReviewConflict(
                    "candidate_not_approvable",
                    "exact candidate checkpoint was not found",
                    candidate_checkpoint_id=int(checkpoint_id),
                )
            )
        return candidate

    def _manifest(self, session_id: int) -> InputManifest:
        try:
            return self.protocol.load_input_manifest(session_id)
        except Exception as exc:
            raise ReviewOperationError(
                ReviewConflict(
                    "integrity_failure", "persisted Input Manifest cannot be verified"
                )
            ) from exc

    @staticmethod
    def _reason_codes(validation: Any, metadata: Mapping[str, Any]) -> tuple[str, ...]:
        explicit = metadata.get("review_reason_codes", ())
        if not isinstance(explicit, (list, tuple)):
            explicit = ()
        codes = {str(code) for code in explicit if str(code) in REVIEW_REASON_CODES}
        for issue in tuple(getattr(validation, "warnings", ())) + tuple(
            getattr(validation, "errors", ())
        ):
            code = str(getattr(issue, "code", ""))
            severity = str(getattr(issue, "severity", "error"))
            if severity == "review_required" and code in REVIEW_REASON_CODES:
                codes.add(code)
        return tuple(sorted(codes))

    @staticmethod
    def _safe_snapshot_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "schema_valid",
            "semantically_valid",
            "protocol_acceptable",
            "operator_review_required",
            "validator_version",
            "validation_hash",
            "errors",
            "warnings",
            "review_reason_codes",
            "input_manifest_id",
            "input_manifest_hash",
            "brief_checkpoint_id",
            "brief_hash",
            "task_plan_hash",
            "stage_configuration_fingerprint",
            "policy",
            "task_count",
            "group_count",
        }
        result = {str(key): metadata[key] for key in allowed if key in metadata}
        # Validation evidence is audit metadata, not a provider transcript.
        if len(str(result).encode("utf-8")) > 64 * 1024:
            raise ReviewOperationError(
                ReviewConflict(
                    "integrity_failure", "validation snapshot exceeds review bound"
                )
            )
        return result

    def _build_binding(
        self,
        session: PlanningSession,
        candidate: PlanningCheckpoint,
        manifest: InputManifest,
        metadata: Mapping[str, Any],
    ) -> ReviewCandidateBinding:
        predecessor_rows = sorted(
            (
                edge.parent_checkpoint_id,
                (
                    edge.parent_checkpoint.content_hash
                    if edge.parent_checkpoint is not None
                    else None
                ),
            )
            for edge in candidate.dependencies
        )
        if any(content_hash is None for _, content_hash in predecessor_rows):
            raise ReviewOperationError(
                ReviewConflict("integrity_failure", "candidate predecessor is missing")
            )
        predecessors = tuple(
            ReviewPredecessorBinding(
                checkpoint_id=checkpoint_id, content_hash=content_hash
            )
            for checkpoint_id, content_hash in predecessor_rows
        )
        config = metadata.get(
            "stage_configuration_fingerprint",
            manifest.configuration_identity.stage_configuration_fingerprint,
        )
        return ReviewCandidateBinding(
            planning_session_id=session.id,
            project_id=session.project_id,
            protocol_version=session.protocol_version,
            session_generation_id=candidate.session_generation_id,
            stage_name=candidate.stage_name,
            stage_version=candidate.checkpoint_version,
            stage_generation_id=candidate.stage_generation_id,
            candidate_checkpoint_id=candidate.id,
            candidate_checkpoint_version=candidate.checkpoint_version,
            candidate_content_hash=candidate.content_hash,
            validation_hash=str(metadata.get("validation_hash", "")),
            validator_version=str(
                candidate.validator_version or metadata.get("validator_version", "")
            ),
            input_manifest_id=str(
                metadata.get("input_manifest_id", manifest.manifest_id)
            ),
            input_manifest_hash=str(
                metadata.get("input_manifest_hash", manifest.manifest_hash)
            ),
            predecessors=predecessors,
            accepted_brief_checkpoint_id=(
                int(metadata["brief_checkpoint_id"])
                if metadata.get("brief_checkpoint_id") is not None
                else None
            ),
            accepted_brief_hash=(
                str(metadata["brief_hash"]) if metadata.get("brief_hash") else None
            ),
            stage_configuration_fingerprint=str(config),
            candidate_attempt_id=candidate.attempt_id,
        )

    def classify_candidate(
        self, session_id: int, candidate_checkpoint_id: int
    ) -> ReviewEligibilityResult:
        """Classify one exact persisted candidate without selecting by latest."""

        self.db.flush()
        session = self._session(session_id)
        candidate = self._candidate(session_id, candidate_checkpoint_id)
        if (
            session.protocol_version != PROTOCOL_V2
            or candidate.protocol_version != PROTOCOL_V2
        ):
            return ReviewEligibilityResult(
                "not_protocol_v2", False, diagnostics=("protocol_v2_required",)
            )
        if session.committed_at is not None or session.completion_manifest is not None:
            return ReviewEligibilityResult(
                "post_commit_unreviewable",
                False,
                diagnostics=("commit_boundary_crossed",),
            )
        if candidate.session_generation_id != session.generation_id:
            return ReviewEligibilityResult(
                "stale", False, diagnostics=("session_generation_mismatch",)
            )
        if candidate.status != "failed":
            if candidate.status == "accepted":
                return ReviewEligibilityResult(
                    "already_accepted", False, diagnostics=("candidate_is_accepted",)
                )
            return ReviewEligibilityResult(
                "stale", False, diagnostics=("candidate_is_not_failed",)
            )
        try:
            manifest = self._manifest(session.id)
            metadata = candidate.validation_json or {}
            if not isinstance(metadata, Mapping):
                raise ReviewOperationError(
                    ReviewConflict(
                        "integrity_failure", "validation evidence is not an object"
                    )
                )
            binding = self._build_binding(session, candidate, manifest, metadata)
            if (
                binding.input_manifest_id != manifest.manifest_id
                or binding.input_manifest_hash != manifest.manifest_hash
            ):
                return ReviewEligibilityResult(
                    "stale", False, binding=binding, diagnostics=("manifest_mismatch",)
                )
            if (
                binding.stage_configuration_fingerprint
                != manifest.configuration_identity.stage_configuration_fingerprint
            ):
                return ReviewEligibilityResult(
                    "stale",
                    False,
                    binding=binding,
                    diagnostics=("configuration_mismatch",),
                )
            actual_content_hash = hashlib.sha256(
                candidate.content.encode("utf-8")
            ).hexdigest()
            if (
                actual_content_hash != candidate.content_hash
                or actual_content_hash != binding.candidate_content_hash
            ):
                return ReviewEligibilityResult(
                    "invalid",
                    False,
                    binding=binding,
                    diagnostics=("content_hash_mismatch",),
                )

            review_object: Any
            if (
                candidate.stage_name == PLANNING_BRIEF_STAGE_NAME
                and candidate.checkpoint_version == PLANNING_BRIEF_STAGE_VERSION
            ):
                if (
                    candidate.schema_version != PLANNING_BRIEF_SCHEMA_VERSION
                    or candidate.renderer_version != PLANNING_BRIEF_RENDERER_VERSION
                    or candidate.validator_version != PLANNING_BRIEF_VALIDATOR_VERSION
                ):
                    return ReviewEligibilityResult(
                        "invalid",
                        False,
                        binding=binding,
                        diagnostics=("unsupported_brief_metadata",),
                    )
                review_object = PlanningBrief.from_json(candidate.content)
                if review_object.canonical_json() != candidate.content:
                    return ReviewEligibilityResult(
                        "invalid",
                        False,
                        binding=binding,
                        diagnostics=("canonical_bytes_mismatch",),
                    )
                validation = validate_planning_brief(
                    review_object, input_manifest=manifest
                )
            elif (
                candidate.stage_name == STRUCTURED_TASK_PLAN_STAGE_NAME
                and candidate.checkpoint_version == STRUCTURED_TASK_PLAN_STAGE_VERSION
            ):
                if (
                    candidate.schema_version != STRUCTURED_TASK_PLAN_SCHEMA_VERSION
                    or candidate.renderer_version
                    != STRUCTURED_TASK_PLAN_RENDERER_VERSION
                    or candidate.validator_version
                    != STRUCTURED_TASK_PLAN_VALIDATOR_VERSION
                ):
                    return ReviewEligibilityResult(
                        "invalid",
                        False,
                        binding=binding,
                        diagnostics=("unsupported_task_plan_metadata",),
                    )
                brief = self.protocol.load_accepted_planning_brief(session.id)
                if brief is None:
                    return ReviewEligibilityResult(
                        "stale",
                        False,
                        binding=binding,
                        diagnostics=("accepted_brief_missing",),
                    )
                current_brief_checkpoint = self.protocol.effective_checkpoints(
                    session.id,
                    stage_versions={
                        PLANNING_BRIEF_STAGE_NAME: PLANNING_BRIEF_STAGE_VERSION
                    },
                ).get((PLANNING_BRIEF_STAGE_NAME, PLANNING_BRIEF_STAGE_VERSION))
                if (
                    current_brief_checkpoint is None
                    or current_brief_checkpoint.status != "accepted"
                    or binding.accepted_brief_checkpoint_id
                    != current_brief_checkpoint.id
                    or binding.accepted_brief_hash
                    != current_brief_checkpoint.content_hash
                ):
                    return ReviewEligibilityResult(
                        "stale",
                        False,
                        binding=binding,
                        diagnostics=("accepted_brief_lineage_mismatch",),
                    )
                review_object = StructuredTaskPlan.from_json(candidate.content)
                if review_object.canonical_json() != candidate.content:
                    return ReviewEligibilityResult(
                        "invalid",
                        False,
                        binding=binding,
                        diagnostics=("canonical_bytes_mismatch",),
                    )
                policy = (
                    metadata.get("policy")
                    if isinstance(metadata.get("policy"), Mapping)
                    else DEFAULT_TASK_PLAN_POLICY
                )
                validation = validate_structured_task_plan(
                    review_object, brief=brief, input_manifest=manifest, policy=policy
                )
                if review_object.brief_ref.checkpoint_id != str(
                    binding.accepted_brief_checkpoint_id or ""
                ):
                    return ReviewEligibilityResult(
                        "stale",
                        False,
                        binding=binding,
                        diagnostics=("brief_lineage_mismatch",),
                    )
            else:
                return ReviewEligibilityResult(
                    "invalid",
                    False,
                    binding=binding,
                    diagnostics=("unsupported_review_stage",),
                )

            expected_validation_hash = str(metadata.get("validation_hash", ""))
            if (
                not expected_validation_hash
                or expected_validation_hash != validation.validation_hash
            ):
                return ReviewEligibilityResult(
                    "invalid",
                    False,
                    binding=binding,
                    diagnostics=("validation_hash_mismatch",),
                )
            if (
                str(metadata.get("validator_version", candidate.validator_version))
                != validation.validator_version
            ):
                return ReviewEligibilityResult(
                    "invalid",
                    False,
                    binding=binding,
                    diagnostics=("validator_version_mismatch",),
                )
            reasons = self._reason_codes(validation, metadata)
            explicit_reasons = tuple(
                str(code)
                for code in metadata.get("review_reason_codes", ())
                if str(code) in REVIEW_REASON_CODES
            )
            snapshot = ReviewValidationSnapshot.from_validation(
                validation,
                review_reason_codes=reasons,
                extra=self._safe_snapshot_metadata(metadata),
            )
            if candidate.stage_name == PLANNING_BRIEF_STAGE_NAME and any(
                item.classification in {"blocking", "operator_decision_required"}
                for item in getattr(review_object, "unresolved_questions", ())
            ):
                return ReviewEligibilityResult(
                    "invalid",
                    False,
                    binding=binding,
                    validation=snapshot,
                    diagnostics=("blocking_review_finding_is_not_approvable",),
                )
            binding = replace(
                binding,
                validation_hash=validation.validation_hash,
                validator_version=validation.validator_version,
            )
            if not validation.schema_valid or not validation.semantically_valid:
                return ReviewEligibilityResult(
                    "invalid",
                    False,
                    binding=binding,
                    validation=snapshot,
                    diagnostics=("canonical_validation_failed",),
                )
            if (validation.protocol_acceptable and not explicit_reasons) or not reasons:
                return ReviewEligibilityResult(
                    "invalid",
                    False,
                    binding=binding,
                    validation=snapshot,
                    diagnostics=("candidate_is_not_review_required",),
                )

            accepted = (
                self.db.query(PlanningCheckpoint)
                .filter(
                    PlanningCheckpoint.planning_session_id == session.id,
                    PlanningCheckpoint.stage_name == candidate.stage_name,
                    PlanningCheckpoint.checkpoint_version
                    == candidate.checkpoint_version,
                    PlanningCheckpoint.status == "accepted",
                    PlanningCheckpoint.content_hash == candidate.content_hash,
                )
                .first()
            )
            if accepted is not None:
                return ReviewEligibilityResult(
                    "already_accepted",
                    False,
                    binding=binding,
                    validation=snapshot,
                    diagnostics=("matching_accepted_checkpoint_exists",),
                )
            reviewed = (
                self.db.query(PlanningReviewEvent)
                .filter(PlanningReviewEvent.candidate_checkpoint_id == candidate.id)
                .order_by(PlanningReviewEvent.event_sequence.desc())
                .all()
            )
            if any(item.event_type == "approve_unchanged" for item in reviewed):
                return ReviewEligibilityResult(
                    "already_accepted", False, binding=binding, validation=snapshot
                )
            if any(item.event_type == "reject" for item in reviewed):
                return ReviewEligibilityResult(
                    "already_rejected", False, binding=binding, validation=snapshot
                )
            later = (
                self.db.query(PlanningCheckpoint)
                .filter(
                    PlanningCheckpoint.planning_session_id == session.id,
                    PlanningCheckpoint.stage_name == candidate.stage_name,
                    PlanningCheckpoint.checkpoint_version
                    == candidate.checkpoint_version,
                    PlanningCheckpoint.id > candidate.id,
                    PlanningCheckpoint.session_generation_id == session.generation_id,
                    PlanningCheckpoint.content != "",
                )
                .first()
            )
            if later is not None:
                return ReviewEligibilityResult(
                    "superseded",
                    False,
                    binding=binding,
                    validation=snapshot,
                    reason_codes=reasons,
                    diagnostics=("newer_candidate_exists",),
                )
            return ReviewEligibilityResult(
                "valid_review_required",
                True,
                binding=binding,
                validation=snapshot,
                reason_codes=reasons,
            )
        except ReviewOperationError:
            raise
        except (
            PlanningBriefSchemaError,
            StructuredTaskPlanSchemaError,
            TypeError,
            ValueError,
        ) as exc:
            return ReviewEligibilityResult(
                "invalid", False, diagnostics=(f"schema_or_parse_failure:{exc}",)
            )
        except ProtocolPersistenceError as exc:
            return ReviewEligibilityResult(
                "invalid", False, diagnostics=(f"integrity_failure:{exc}",)
            )

    def _events(
        self, review_id: str, *, lock: bool = False
    ) -> tuple[DomainReviewEvent, ...]:
        query = (
            self.db.query(PlanningReviewEvent)
            .filter(PlanningReviewEvent.review_id == review_id)
            .order_by(PlanningReviewEvent.event_sequence.asc())
        )
        if lock:
            query = query.with_for_update()
        rows = query.all()
        events: list[DomainReviewEvent] = []
        previous_hash: str | None = None
        for expected_sequence, row in enumerate(rows, start=1):
            event = event_from_model(row)
            if event.event_sequence != expected_sequence:
                raise ReviewIntegrityError("review event sequence has a gap")
            if event.prior_review_head_sequence != expected_sequence - 1:
                raise ReviewIntegrityError("review event prior sequence is invalid")
            if event.previous_event_hash != previous_hash:
                raise ReviewIntegrityError("review event hash chain is broken")
            if event.schema_version != REVIEW_SCHEMA_VERSION:
                raise ReviewIntegrityError("unsupported review event schema")
            if events and event.candidate_binding != events[0].candidate_binding:
                raise ReviewIntegrityError("review event candidate binding changed")
            events.append(event)
            previous_hash = event.event_hash
        if not events:
            raise ReviewIntegrityError("review aggregate has no events")
        if sum(event.event_type in TERMINAL_REVIEW_EVENT_TYPES for event in events) > 1:
            raise ReviewIntegrityError(
                "review aggregate has multiple terminal decisions"
            )
        return tuple(events)

    def _projection(self, review_id: str, *, lock: bool = False) -> ReviewProjection:
        events = self._events(review_id, lock=lock)
        projection = project_review(ReviewAggregate(review_id=review_id, events=events))
        if projection.state == "pending":
            eligibility = self.classify_candidate(
                projection.candidate_binding.planning_session_id,
                projection.candidate_binding.candidate_checkpoint_id,
            )
            if eligibility.classification in {"stale", "superseded"}:
                projection = replace(
                    projection,
                    state=(
                        "superseded"
                        if eligibility.classification == "superseded"
                        else "stale"
                    ),
                    allowed_decisions=(),
                    stale=eligibility.classification == "stale",
                    superseded=eligibility.classification == "superseded",
                )
        if projection.state == "approved":
            approval_id = projection.terminal_event_id
            promotion = (
                self.db.query(PlanningCheckpoint)
                .filter(PlanningCheckpoint.promotion_review_event_id == approval_id)
                .one_or_none()
            )
            if promotion is None or promotion.status != "accepted":
                raise ReviewIntegrityError(
                    "approved review is missing its promotion checkpoint"
                )
            projection = replace(
                projection,
                accepted_promotion_checkpoint_id=promotion.id,
                accepted_promotion_hash=promotion.content_hash,
                current_accepted_artifact_id=promotion.id,
                current_accepted_artifact_hash=promotion.content_hash,
            )
        return projection

    def recover_review(self, review_id: str) -> ReviewProjection:
        """Rebuild a review projection without taking an action."""

        try:
            return self._projection(review_id)
        except ReviewIntegrityError as exc:
            rows = (
                self.db.query(PlanningReviewEvent)
                .filter(PlanningReviewEvent.review_id == review_id)
                .order_by(PlanningReviewEvent.event_sequence.asc())
                .all()
            )
            if not rows:
                raise
            first = rows[0]
            binding = ReviewCandidateBinding(**dict(first.candidate_binding_json or {}))
            return ReviewProjection(
                review_id=review_id,
                candidate_binding=binding,
                state="integrity_failure",
                current_sequence=int(first.event_sequence),
                review_head_token=str(first.event_hash),
                validation_state="unknown",
                review_required_reasons=tuple(first.review_reason_codes or ()),
                allowed_decisions=(),
                actor_history=(),
                integrity_error=str(exc),
            )

    def _existing_idempotency(
        self, actor: ReviewActor, request: ReviewDecisionRequest
    ) -> DomainReviewEvent | None:
        row = (
            self.db.query(PlanningReviewEvent)
            .filter(
                PlanningReviewEvent.operator_subject == actor.subject,
                PlanningReviewEvent.idempotency_key == request.idempotency_key,
            )
            .one_or_none()
        )
        if row is None:
            return None
        event = event_from_model(row)
        if event.canonical_request_hash != request.canonical_hash(
            event.event_type, event.review_id, event.candidate_binding
        ):
            raise ReviewOperationError(
                ReviewConflict(
                    "idempotency_key_conflict",
                    "idempotency key was reused with different content",
                    event.review_id,
                    event.candidate_binding.candidate_checkpoint_id,
                )
            )
        return event

    def _append_event(
        self,
        review_id: str,
        binding: ReviewCandidateBinding,
        validation: ReviewValidationSnapshot,
        actor: ReviewActor,
        event_type: str,
        request: ReviewDecisionRequest,
        *,
        decision_text: str | None = None,
        command_identity: str | None = None,
        amendment_id: str | None = None,
        amendment_hash: str | None = None,
        lock: bool = True,
    ) -> DomainReviewEvent:
        if event_type not in REVIEW_EVENT_TYPES:
            raise ReviewOperationError(
                ReviewConflict(
                    "candidate_not_approvable",
                    "unsupported review action",
                    review_id,
                    binding.candidate_checkpoint_id,
                )
            )
        existing = self._existing_idempotency(actor, request)
        if existing is not None:
            return existing
        current = self._events(review_id, lock=lock)
        prior = current[-1]
        if (
            request.expected_head_sequence is not None
            and request.expected_head_sequence != prior.event_sequence
        ):
            raise ReviewOperationError(
                ReviewConflict(
                    "stale_review_head",
                    "review head sequence is stale",
                    review_id,
                    binding.candidate_checkpoint_id,
                )
            )
        if (
            request.expected_head_token is not None
            and request.expected_head_token != prior.event_hash
        ):
            raise ReviewOperationError(
                ReviewConflict(
                    "stale_review_head",
                    "review head token is stale",
                    review_id,
                    binding.candidate_checkpoint_id,
                )
            )
        if any(event.event_type in TERMINAL_REVIEW_EVENT_TYPES for event in current):
            raise ReviewOperationError(
                ReviewConflict(
                    "review_already_decided",
                    "review aggregate already has a terminal decision",
                    review_id,
                    binding.candidate_checkpoint_id,
                )
            )
        canonical_hash = request.canonical_hash(event_type, review_id, binding)
        created = datetime.now(timezone.utc)
        event = DomainReviewEvent(
            event_id=str(uuid.uuid4()),
            review_id=review_id,
            event_sequence=prior.event_sequence + 1,
            event_type=event_type,
            candidate_binding=binding,
            validation=validation,
            actor=actor,
            idempotency_key=request.idempotency_key,
            canonical_request_hash=canonical_hash,
            prior_review_head_sequence=prior.event_sequence,
            resulting_sequence=prior.event_sequence + 1,
            review_concurrency_token=prior.event_hash,
            decision_text=decision_text,
            command_identity=command_identity,
            amendment_id=amendment_id,
            amendment_hash=amendment_hash,
            previous_event_hash=prior.event_hash,
            created_at=created,
        )
        row = self._model_from_event(event)
        self.db.add(row)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise ReviewOperationError(
                ReviewConflict(
                    "review_already_decided",
                    "review event lost a concurrency race",
                    review_id,
                    binding.candidate_checkpoint_id,
                )
            ) from exc
        return event

    @staticmethod
    def _model_from_event(event: DomainReviewEvent) -> PlanningReviewEvent:
        binding = event.candidate_binding
        return PlanningReviewEvent(
            event_id=event.event_id,
            review_id=event.review_id,
            event_sequence=event.event_sequence,
            event_type=event.event_type,
            schema_version=event.schema_version,
            planning_session_id=binding.planning_session_id,
            project_id=binding.project_id,
            protocol_version=binding.protocol_version,
            stage_name=binding.stage_name,
            stage_version=binding.stage_version,
            stage_generation_id=binding.stage_generation_id,
            candidate_checkpoint_id=binding.candidate_checkpoint_id,
            candidate_checkpoint_version=binding.candidate_checkpoint_version,
            candidate_content_hash=binding.candidate_content_hash,
            session_generation_id=binding.session_generation_id,
            input_manifest_id=binding.input_manifest_id,
            input_manifest_hash=binding.input_manifest_hash,
            brief_checkpoint_id=binding.accepted_brief_checkpoint_id,
            brief_hash=binding.accepted_brief_hash,
            predecessor_json=[item.to_dict() for item in binding.predecessors],
            configuration_fingerprint=binding.stage_configuration_fingerprint,
            candidate_attempt_id=binding.candidate_attempt_id,
            validator_version=event.validation.validator_version,
            validation_hash=event.validation.validation_hash,
            validation_json=event.validation.to_dict(),
            review_reason_codes=list(event.validation.review_reason_codes),
            candidate_binding_json=binding.to_dict(),
            operator_subject=event.actor.subject,
            operator_role=event.actor.role,
            authority_basis=event.actor.authority_basis,
            actor_kind=event.actor.actor_kind,
            decision_type=event.event_type,
            decision_text=event.decision_text,
            command_identity=event.command_identity,
            amendment_id=event.amendment_id,
            amendment_hash=event.amendment_hash,
            prior_review_head_sequence=event.prior_review_head_sequence,
            resulting_sequence=event.resulting_sequence,
            review_concurrency_token=event.review_concurrency_token,
            owner_fence_fingerprint=None,
            idempotency_key=event.idempotency_key,
            canonical_request_hash=event.canonical_request_hash,
            previous_event_hash=event.previous_event_hash,
            event_hash=event.event_hash,
            promotion_checkpoint_id=event.promotion_checkpoint_id,
            created_at=event.created_at,
        )

    def open_review_for_candidate(
        self,
        session_id: int,
        candidate_checkpoint_id: int,
        *,
        idempotency_key: str | None = None,
    ) -> ReviewProjection:
        eligibility = self.classify_candidate(session_id, candidate_checkpoint_id)
        if (
            not eligibility.eligible
            or eligibility.binding is None
            or eligibility.validation is None
        ):
            raise ReviewOperationError(
                ReviewConflict(
                    (
                        eligibility.classification
                        if eligibility.classification in ELIGIBILITY_CLASSES
                        else "candidate_not_approvable"
                    ),
                    "; ".join(eligibility.diagnostics) or "candidate is not reviewable",
                    candidate_checkpoint_id=int(candidate_checkpoint_id),
                )
            )
        existing_row = (
            self.db.query(PlanningReviewEvent)
            .filter(
                PlanningReviewEvent.candidate_checkpoint_id
                == int(candidate_checkpoint_id)
            )
            .order_by(PlanningReviewEvent.event_sequence.asc())
            .first()
        )
        if existing_row is not None:
            projection = self._projection(existing_row.review_id)
            if projection.candidate_binding != eligibility.binding:
                raise ReviewOperationError(
                    ReviewConflict(
                        "promotion_conflict",
                        "candidate is bound to an unrelated review aggregate",
                        projection.review_id,
                        int(candidate_checkpoint_id),
                    )
                )
            return projection
        actor = ReviewActor(
            "system:review-open", "system", "candidate-eligibility", "system"
        )
        request = ReviewDecisionRequest(
            idempotency_key=idempotency_key or f"review-open:{candidate_checkpoint_id}"
        )
        review_id = str(uuid.uuid4())
        event = DomainReviewEvent(
            event_id=str(uuid.uuid4()),
            review_id=review_id,
            event_sequence=1,
            event_type="review_opened",
            candidate_binding=eligibility.binding,
            validation=eligibility.validation,
            actor=actor,
            idempotency_key=request.idempotency_key,
            canonical_request_hash=request.canonical_hash(
                "review_opened", "pending", eligibility.binding
            ),
            prior_review_head_sequence=0,
            resulting_sequence=1,
            review_concurrency_token="root",
        )
        self.db.add(self._model_from_event(event))
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            existing = (
                self.db.query(PlanningReviewEvent)
                .filter(
                    PlanningReviewEvent.candidate_checkpoint_id
                    == int(candidate_checkpoint_id)
                )
                .first()
            )
            if existing is not None:
                return self._projection(existing.review_id)
            raise ReviewOperationError(
                ReviewConflict(
                    "promotion_conflict",
                    "review aggregate creation raced",
                    candidate_checkpoint_id=int(candidate_checkpoint_id),
                )
            ) from exc
        return self._projection(event.review_id)

    def _decision_request(
        self,
        request: ReviewDecisionRequest | None,
        *,
        idempotency_key: str | None,
        comment: str | None,
        reason: str | None,
        expected_head_sequence: int | None,
        expected_head_token: str | None,
        guidance: str | None = None,
        amendment_id: str | None = None,
        amendment_hash: str | None = None,
        command_identity: str | None = None,
    ) -> ReviewDecisionRequest:
        if request is not None:
            if (
                idempotency_key is not None
                and idempotency_key != request.idempotency_key
            ):
                raise ReviewOperationError(
                    ReviewConflict(
                        "idempotency_key_conflict",
                        "request idempotency key differs from supplied key",
                    )
                )
            return request
        return ReviewDecisionRequest(
            idempotency_key=idempotency_key or "",
            comment=comment,
            reason=reason,
            expected_head_sequence=expected_head_sequence,
            expected_head_token=expected_head_token,
            guidance=guidance,
            amendment_id=amendment_id,
            amendment_hash=amendment_hash,
            command_identity=command_identity,
        )

    @staticmethod
    def _assert_human(actor: ReviewActor | None) -> ReviewActor:
        if actor is None or not actor.is_human or not actor.authorized:
            raise ReviewOperationError(
                ReviewConflict(
                    "candidate_not_approvable",
                    "an authorized human operator is required",
                )
            )
        return actor

    def _ensure_current(
        self, projection: ReviewProjection
    ) -> tuple[PlanningCheckpoint, ReviewEligibilityResult]:
        eligibility = self.classify_candidate(
            projection.candidate_binding.planning_session_id,
            projection.candidate_binding.candidate_checkpoint_id,
        )
        if eligibility.binding != projection.candidate_binding:
            raise ReviewOperationError(
                ReviewConflict(
                    "lineage_mismatch",
                    "candidate binding changed",
                    projection.review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        if not eligibility.eligible:
            diagnostics = set(eligibility.diagnostics)
            if (
                "validation_hash_mismatch" in diagnostics
                or "validator_version_mismatch" in diagnostics
            ):
                conflict_code = "validation_mismatch"
            elif (
                "content_hash_mismatch" in diagnostics
                or "canonical_bytes_mismatch" in diagnostics
            ):
                conflict_code = "integrity_failure"
            elif any(
                item in diagnostics
                for item in {
                    "manifest_mismatch",
                    "configuration_mismatch",
                    "brief_lineage_mismatch",
                    "accepted_brief_lineage_mismatch",
                }
            ):
                conflict_code = "lineage_mismatch"
            else:
                conflict_code = {
                    "stale": "candidate_stale",
                    "superseded": "newer_candidate_exists",
                    "invalid": "candidate_not_approvable",
                    "already_accepted": "promotion_conflict",
                    "already_rejected": "review_already_decided",
                }.get(eligibility.classification, "candidate_not_approvable")
            raise ReviewOperationError(
                ReviewConflict(
                    conflict_code,
                    "; ".join(eligibility.diagnostics)
                    or "candidate is no longer approvable",
                    projection.review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        return (
            self._candidate(
                projection.candidate_binding.planning_session_id,
                projection.candidate_binding.candidate_checkpoint_id,
            ),
            eligibility,
        )

    def approve_review_unchanged(
        self,
        review_id: str,
        actor: ReviewActor | None = None,
        request: ReviewDecisionRequest | None = None,
        *,
        idempotency_key: str | None = None,
        comment: str | None = None,
        expected_head_sequence: int | None = None,
        expected_head_token: str | None = None,
    ) -> ReviewDecisionResult:
        actor = self._assert_human(actor)
        projection = self._projection(review_id, lock=True)
        request = self._decision_request(
            request,
            idempotency_key=idempotency_key,
            comment=comment,
            reason=None,
            expected_head_sequence=expected_head_sequence,
            expected_head_token=expected_head_token,
        )
        if (
            request.candidate_binding is not None
            and request.candidate_binding != projection.candidate_binding
        ):
            raise ReviewOperationError(
                ReviewConflict(
                    "lineage_mismatch",
                    "decision request candidate binding does not match review",
                    review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        replay = self._existing_idempotency(actor, request)
        if replay is not None:
            if (
                replay.review_id != review_id
                or replay.event_type != "approve_unchanged"
            ):
                raise ReviewOperationError(
                    ReviewConflict(
                        "idempotency_key_conflict",
                        "idempotency key belongs to another review action",
                        review_id,
                        projection.candidate_binding.candidate_checkpoint_id,
                    )
                )
            promotion = (
                self.db.query(PlanningCheckpoint)
                .filter(PlanningCheckpoint.promotion_review_event_id == replay.event_id)
                .one_or_none()
            )
            if promotion is None:
                raise ReviewIntegrityError("approval replay is missing promotion")
            return ReviewDecisionResult(
                review_id,
                replay.event_id,
                replay.event_type,
                "approved",
                PromotionCheckpointResult(
                    promotion.id,
                    promotion.checkpoint_version,
                    promotion.content_hash,
                    replay.event_id,
                    promotion.stage_name,
                ),
                replayed=True,
                completion_reevaluation_requested=True,
            )
        if projection.terminal_decision is not None:
            raise ReviewOperationError(
                ReviewConflict(
                    "review_already_decided",
                    "review already has a terminal decision",
                    review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        if not request.comment:
            raise ReviewOperationError(
                ReviewConflict(
                    "candidate_not_approvable",
                    "approval comment is required",
                    review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        candidate, eligibility = self._ensure_current(projection)
        session = self._session(candidate.planning_session_id, lock=True)
        if (
            request.expected_head_sequence is not None
            and request.expected_head_sequence != projection.current_sequence
        ):
            raise ReviewOperationError(
                ReviewConflict(
                    "stale_review_head",
                    "review head sequence is stale",
                    review_id,
                    candidate.id,
                )
            )
        event = self._append_event(
            review_id,
            projection.candidate_binding,
            eligibility.validation,
            actor,
            "approve_unchanged",
            request,
            decision_text=request.comment,
        )
        parent_ids = [
            edge.parent_checkpoint_id
            for edge in sorted(
                candidate.dependencies, key=lambda item: item.parent_checkpoint_id
            )
        ]
        if candidate.id not in parent_ids:
            parent_ids.append(candidate.id)
        promotion = self.protocol.record_checkpoint(
            session.id,
            stage_name=candidate.stage_name,
            checkpoint_version=candidate.checkpoint_version,
            content=candidate.content,
            stage_generation_id=candidate.stage_generation_id,
            attempt_id=str(uuid.uuid4()),
            session_generation_id=session.generation_id,
            protocol_version=PROTOCOL_V2,
            status="accepted",
            parent_checkpoint_ids=tuple(parent_ids),
            schema_version=candidate.schema_version,
            brief_hash=candidate.brief_hash,
            renderer_version=candidate.renderer_version,
            validator_version=candidate.validator_version,
            validation_json=dict(candidate.validation_json or {}),
            promotion_review_event_id=event.event_id,
            promotion_reason_code="operator_approve_unchanged",
            review_promotion=True,
        )
        result = PromotionCheckpointResult(
            promotion.id,
            promotion.checkpoint_version,
            promotion.content_hash,
            event.event_id,
            promotion.stage_name,
        )
        return ReviewDecisionResult(
            review_id,
            event.event_id,
            event.event_type,
            "approved",
            result,
            completion_reevaluation_requested=True,
        )

    def _append_terminal_decision(
        self,
        review_id: str,
        actor: ReviewActor | None,
        event_type: str,
        request: ReviewDecisionRequest | None,
        *,
        idempotency_key: str | None,
        comment: str | None,
        reason: str | None,
        expected_head_sequence: int | None,
        expected_head_token: str | None,
        guidance: str | None = None,
        amendment_id: str | None = None,
        amendment_hash: str | None = None,
        command_identity: str | None = None,
    ) -> ReviewDecisionResult:
        actor = self._assert_human(actor)
        projection = self._projection(review_id, lock=True)
        request = self._decision_request(
            request,
            idempotency_key=idempotency_key,
            comment=comment,
            reason=reason,
            expected_head_sequence=expected_head_sequence,
            expected_head_token=expected_head_token,
            guidance=guidance,
            amendment_id=amendment_id,
            amendment_hash=amendment_hash,
            command_identity=command_identity,
        )
        if (
            request.candidate_binding is not None
            and request.candidate_binding != projection.candidate_binding
        ):
            raise ReviewOperationError(
                ReviewConflict(
                    "lineage_mismatch",
                    "decision request candidate binding does not match review",
                    review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        replay = self._existing_idempotency(actor, request)
        if replay is not None:
            if replay.review_id != review_id or replay.event_type != event_type:
                raise ReviewOperationError(
                    ReviewConflict(
                        "idempotency_key_conflict",
                        "idempotency key belongs to another review action",
                        review_id,
                        projection.candidate_binding.candidate_checkpoint_id,
                    )
                )
            return ReviewDecisionResult(
                review_id,
                replay.event_id,
                replay.event_type,
                project_review(
                    ReviewAggregate(review_id, self._events(review_id))
                ).state,
                replayed=True,
            )
        if projection.terminal_decision is not None:
            raise ReviewOperationError(
                ReviewConflict(
                    "review_already_decided",
                    "review already has a terminal decision",
                    review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        if event_type == "reject" and not request.reason:
            raise ReviewOperationError(
                ReviewConflict(
                    "candidate_not_approvable",
                    "rejection reason is required",
                    review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        if event_type == "cancel_review" and not request.reason:
            raise ReviewOperationError(
                ReviewConflict(
                    "candidate_not_approvable",
                    "cancellation reason is required",
                    review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        if event_type == "acknowledge_only" and not request.comment:
            raise ReviewOperationError(
                ReviewConflict(
                    "candidate_not_approvable",
                    "acknowledgment comment is required",
                    review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        if event_type == "request_amendment" and not request.guidance:
            raise ReviewOperationError(
                ReviewConflict(
                    "candidate_not_approvable",
                    "amendment guidance is required",
                    review_id,
                    projection.candidate_binding.candidate_checkpoint_id,
                )
            )
        if event_type != "acknowledge_only":
            self._ensure_current(projection)
        validation = _event_snapshot_from_model(
            self.db.query(PlanningReviewEvent)
            .filter(
                PlanningReviewEvent.review_id == review_id,
                PlanningReviewEvent.event_sequence == 1,
            )
            .one()
        )
        text = request.reason or request.comment or request.guidance
        command = request.command_identity
        amendment = request.amendment_id
        amendment_digest = request.amendment_hash
        if event_type == "request_regeneration" and not command:
            command = "regenerate-" + uuid.uuid4().hex
        if event_type == "request_amendment":
            amendment = amendment or "amendment-" + uuid.uuid4().hex
            amendment_digest = amendment_digest or canonical_json_hash(
                {"guidance": request.guidance}
            )
            command = command or "amend-" + uuid.uuid4().hex
        event = self._append_event(
            review_id,
            projection.candidate_binding,
            validation,
            actor,
            event_type,
            request,
            decision_text=text,
            command_identity=command,
            amendment_id=amendment,
            amendment_hash=amendment_digest,
        )
        state = {
            "reject": "rejected",
            "cancel_review": "cancelled",
            "request_regeneration": "regeneration_requested",
            "request_amendment": "amendment_requested",
            "acknowledge_only": "pending",
        }[event_type]
        return ReviewDecisionResult(review_id, event.event_id, event.event_type, state)

    def reject_review(
        self,
        review_id: str,
        actor: ReviewActor | None = None,
        request: ReviewDecisionRequest | None = None,
        *,
        idempotency_key: str | None = None,
        reason: str | None = None,
        expected_head_sequence: int | None = None,
        expected_head_token: str | None = None,
    ) -> ReviewDecisionResult:
        return self._append_terminal_decision(
            review_id,
            actor,
            "reject",
            request,
            idempotency_key=idempotency_key,
            comment=None,
            reason=reason,
            expected_head_sequence=expected_head_sequence,
            expected_head_token=expected_head_token,
        )

    def cancel_review(
        self,
        review_id: str,
        actor: ReviewActor | None = None,
        request: ReviewDecisionRequest | None = None,
        *,
        idempotency_key: str | None = None,
        reason: str | None = None,
        expected_head_sequence: int | None = None,
        expected_head_token: str | None = None,
    ) -> ReviewDecisionResult:
        return self._append_terminal_decision(
            review_id,
            actor,
            "cancel_review",
            request,
            idempotency_key=idempotency_key,
            comment=None,
            reason=reason,
            expected_head_sequence=expected_head_sequence,
            expected_head_token=expected_head_token,
        )

    def acknowledge_review(
        self,
        review_id: str,
        actor: ReviewActor | None = None,
        request: ReviewDecisionRequest | None = None,
        *,
        idempotency_key: str | None = None,
        comment: str | None = None,
        expected_head_sequence: int | None = None,
        expected_head_token: str | None = None,
    ) -> ReviewDecisionResult:
        return self._append_terminal_decision(
            review_id,
            actor,
            "acknowledge_only",
            request,
            idempotency_key=idempotency_key,
            comment=comment,
            reason=None,
            expected_head_sequence=expected_head_sequence,
            expected_head_token=expected_head_token,
        )

    def request_regeneration(
        self,
        review_id: str,
        actor: ReviewActor | None = None,
        request: ReviewDecisionRequest | None = None,
        *,
        idempotency_key: str | None = None,
        guidance: str | None = None,
        expected_head_sequence: int | None = None,
        expected_head_token: str | None = None,
    ) -> ReviewDecisionResult:
        return self._append_terminal_decision(
            review_id,
            actor,
            "request_regeneration",
            request,
            idempotency_key=idempotency_key,
            comment=None,
            reason=None,
            expected_head_sequence=expected_head_sequence,
            expected_head_token=expected_head_token,
            guidance=guidance,
        )

    def request_amendment(
        self,
        review_id: str,
        actor: ReviewActor | None = None,
        request: ReviewDecisionRequest | None = None,
        *,
        idempotency_key: str | None = None,
        guidance: str | None = None,
        amendment_id: str | None = None,
        amendment_hash: str | None = None,
        expected_head_sequence: int | None = None,
        expected_head_token: str | None = None,
    ) -> ReviewDecisionResult:
        return self._append_terminal_decision(
            review_id,
            actor,
            "request_amendment",
            request,
            idempotency_key=idempotency_key,
            comment=None,
            reason=None,
            expected_head_sequence=expected_head_sequence,
            expected_head_token=expected_head_token,
            guidance=guidance,
            amendment_id=amendment_id,
            amendment_hash=amendment_hash,
        )

    def get_review(self, review_id: str) -> ReviewProjection:
        return self._projection(review_id)


OperatorReviewService = OperatorReviewPersistenceService


__all__ = [
    "OperatorReviewPersistenceService",
    "OperatorReviewService",
    "event_from_model",
]
