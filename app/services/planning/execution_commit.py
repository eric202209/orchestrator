"""Phase 29B-2 Planning-to-Execution Commit Boundary.

``PlanningExecutionCommitService`` is the sole production caller of
``PlanningProtocolPersistenceService.record_commit_manifest`` for Protocol v2.
It releases one exact operator-approved Structured Task Plan from Planning
authority into Execution authority:

    operator-approved Structured Task Plan
        -> PlanningCommitManifest (Transaction A, its own commit)
        -> ExecutionPlanCommitService (Transaction B, its own commit)

This is an authority handoff, not execution dispatch.  It never mutates
Planning checkpoint content, review history, or completion manifests, and it
never executes tasks, enqueues jobs, or creates legacy ``Task``/``Session``
rows.  See
``docs/roadmap/done/phase29/phase29b2-planning-to-execution-commit-boundary.md``
for the full design record.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy.orm import Session

from app.models import (
    ExecutionCommitCommand,
    ExecutionDependencyEdge,
    ExecutionGroup,
    ExecutionGroupMember,
    ExecutionPlan,
    ExecutionTask,
    PlanningCommitManifest,
    PlanningCompletionManifest,
    PlanningReviewEvent,
    PlanningSession,
    Project,
)
from app.services.execution.execution_plan_commit_service import (
    ExecutionPlanCommitError,
    ExecutionPlanCommitService,
)
from app.services.orchestration.stage_engine import StageDefinition, StageExecutor
from app.services.planning.operator_review import ReviewDomainError, canonical_json_hash
from app.services.planning.operator_review_persistence import (
    OperatorReviewPersistenceService,
)
from app.services.planning.protocol_persistence import (
    PROTOCOL_V2,
    PlanningProtocolPersistenceService,
    ProtocolPersistenceError,
)
from app.services.planning.structured_task_plan import (
    STRUCTURED_TASK_PLAN_STAGE_NAME,
    STRUCTURED_TASK_PLAN_STAGE_VERSION,
)
from app.services.planning.validation_contract import ValidationContractError
from app.services.execution.validation_contract import ValidationContractService

PROVENANCE_SCHEMA = "planning_execution_commit.v1"
COMMAND_SCHEMA = "planning_execution_commit_command.v1"

# Bounded public code for a failed Transaction B (Execution materialization).
# Never the raw ``ExecutionPlanCommitError`` text.
EXECUTION_MATERIALIZATION_FAILED = "execution_materialization_failed"

logger = logging.getLogger(__name__)

# Public bounded error codes.  The API layer maps these to HTTP statuses.
ERROR_CODES = frozenset(
    {
        "session_not_found",
        "forbidden",
        "protocol_v2_required",
        "authority_stale",
        "task_plan_not_approved",
        "approval_integrity_failure",
        "completion_manifest_pending",
        "completion_manifest_missing",
        "completion_manifest_inconsistent",
        "commit_manifest_conflict",
        "idempotency_key_conflict",
        "integrity_failure",
        "validation_contract_missing",
        "validation_contract_schema_unsupported",
        "validation_predicate_unsupported",
        "validation_predicate_version_unsupported",
        "validation_contract_parameters_invalid",
        "validation_evidence_descriptor_invalid",
        "validation_pass_policy_invalid",
        "validation_review_requirement_invalid",
        "validation_environment_identity_invalid",
        "validation_contract_hash_mismatch",
        "validation_contract_release_conflict",
        "validation_contract_idempotency_conflict",
        "validation_contract_integrity_failure",
        "validation_schema_reference_missing",
        "validation_schema_missing",
        "validation_schema_hash_mismatch",
        "validation_schema_dialect_mismatch",
        "validation_schema_dialect_unsupported",
        "validation_schema_integrity_failure",
    }
)


class ExecutionCommitError(Exception):
    """A bounded, publicly-mappable execution-commit failure."""

    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown execution commit error code: {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExecutionCommitRequest:
    """Exact operator-approved authority the caller expects to release.

    The server resolves current state independently, but always compares it
    against this exact expectation and fails closed on any mismatch -- a
    request that only says "commit the latest plan" is never accepted.
    """

    idempotency_key: str
    operator_subject: str
    structured_task_plan_checkpoint_id: int
    structured_task_plan_hash: str
    expected_session_generation_id: str
    expected_review_id: str | None = None
    expected_approval_event_id: str | None = None


@dataclass(frozen=True)
class ExecutionCommitResult:
    planning_session_id: int
    session_generation_id: str
    structured_task_plan_checkpoint_id: int
    structured_task_plan_hash: str
    review_id: str
    approval_event_id: str
    completion_manifest_id: int
    completion_manifest_hash: str
    planning_commit_manifest_id: int
    commit_identity: str
    boundary_state: str
    idempotent_replay: bool
    integrity_status: str
    execution_plan_id: int | None = None
    execution_plan_generation: int | None = None
    execution_plan_status: str | None = None
    task_count: int = 0
    dependency_edge_count: int = 0
    group_count: int = 0
    group_membership_count: int = 0
    retryable: bool = False
    execution_error_code: str | None = None
    # Internal-only diagnostic detail.  Never serialized in the public API
    # response -- log it server-side instead.
    execution_failure_reason: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PlanningExecutionCommitService:
    """Narrow orchestration service for the Planning->Execution authority
    handoff.  Never modifies Planning checkpoint content or review history,
    never generates Planning content, and never dispatches execution."""

    def __init__(self, db: Session):
        self.db = db
        self.protocol = PlanningProtocolPersistenceService(db)

    # -- resolution ----------------------------------------------------

    def _resolve_session_and_project(
        self, session_id: int
    ) -> tuple[PlanningSession, Project]:
        session = self.db.get(PlanningSession, session_id)
        if session is None:
            raise ExecutionCommitError(
                "session_not_found", "planning session not found"
            )
        project = self.db.get(Project, session.project_id)
        if project is None or project.deleted_at is not None:
            raise ExecutionCommitError("session_not_found", "project is not accessible")
        if session.protocol_version != PROTOCOL_V2:
            raise ExecutionCommitError(
                "protocol_v2_required",
                "execution commit requires a Protocol v2 planning session",
            )
        return session, project

    def _resolve_approved_task_plan(
        self, session: PlanningSession, request: ExecutionCommitRequest
    ):
        effective = self.protocol.effective_checkpoints(
            session.id,
            stage_versions={
                STRUCTURED_TASK_PLAN_STAGE_NAME: STRUCTURED_TASK_PLAN_STAGE_VERSION
            },
        )
        checkpoint = effective.get(
            (STRUCTURED_TASK_PLAN_STAGE_NAME, STRUCTURED_TASK_PLAN_STAGE_VERSION)
        )
        if checkpoint is None or checkpoint.status != "accepted":
            raise ExecutionCommitError(
                "task_plan_not_approved",
                "no accepted Structured Task Plan checkpoint exists for this "
                "planning session",
            )
        if checkpoint.promotion_review_event_id is None:
            raise ExecutionCommitError(
                "task_plan_not_approved",
                "the accepted Structured Task Plan was not released through "
                "an operator review promotion",
            )
        if (
            checkpoint.id != request.structured_task_plan_checkpoint_id
            or checkpoint.content_hash != request.structured_task_plan_hash
        ):
            raise ExecutionCommitError(
                "authority_stale",
                "expected Structured Task Plan checkpoint/hash does not "
                "match the current accepted authority",
            )
        if session.generation_id != request.expected_session_generation_id:
            raise ExecutionCommitError(
                "authority_stale",
                "expected planning session generation does not match the "
                "current session",
            )
        return checkpoint

    def _resolve_approval_event(
        self,
        session: PlanningSession,
        checkpoint,
        request: ExecutionCommitRequest,
    ) -> tuple[str, str, str]:
        event_id = str(checkpoint.promotion_review_event_id)
        row = (
            self.db.query(PlanningReviewEvent)
            .filter(PlanningReviewEvent.event_id == event_id)
            .one_or_none()
        )
        if row is None or row.event_type != "approve_unchanged":
            raise ExecutionCommitError(
                "approval_integrity_failure",
                "promotion review event could not be resolved",
            )
        review_id = str(row.review_id)
        reviews = OperatorReviewPersistenceService(self.db)
        try:
            projection = reviews.get_review(review_id)
        except ReviewDomainError as exc:
            raise ExecutionCommitError(
                "approval_integrity_failure", "review integrity could not be verified"
            ) from exc
        binding = projection.candidate_binding
        if (
            binding.planning_session_id != session.id
            or binding.stage_name != STRUCTURED_TASK_PLAN_STAGE_NAME
            or binding.candidate_checkpoint_version
            != STRUCTURED_TASK_PLAN_STAGE_VERSION
        ):
            raise ExecutionCommitError(
                "approval_integrity_failure",
                "approval event is not bound to this Structured Task Plan " "candidate",
            )
        if (
            projection.state != "approved"
            or projection.terminal_event_id != event_id
            or projection.accepted_promotion_checkpoint_id != checkpoint.id
            or projection.accepted_promotion_hash != checkpoint.content_hash
        ):
            raise ExecutionCommitError(
                "task_plan_not_approved",
                "review aggregate does not show a terminal approval bound "
                "to this checkpoint",
            )
        if (
            request.expected_review_id is not None
            and request.expected_review_id != review_id
        ):
            raise ExecutionCommitError(
                "authority_stale",
                "expected review id does not match the resolved approval",
            )
        if (
            request.expected_approval_event_id is not None
            and request.expected_approval_event_id != event_id
        ):
            raise ExecutionCommitError(
                "authority_stale",
                "expected approval event id does not match the resolved " "approval",
            )
        return review_id, event_id, str(row.operator_subject)

    def _acquire_lease(self, session: PlanningSession) -> PlanningSession:
        """Acquire a short-lived processing lease so ``_assert_owner``-gated
        persistence calls (completion reevaluation, commit-manifest record)
        have a valid fencing token, mirroring
        ``PlanningSessionService._prepare_direct_owner``.  Never runs a
        provider and never advances any stage -- required stages are
        already accepted before this is called."""

        locked = (
            self.db.query(PlanningSession)
            .filter(PlanningSession.id == session.id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        if locked.processing_token is not None:
            raise ExecutionCommitError(
                "completion_manifest_pending",
                "planning session is currently processing; retry after it " "completes",
            )
        locked.processing_token = uuid.uuid4().hex
        locked.processing_started_at = _now()
        self.db.flush()
        return locked

    @staticmethod
    def _release_lease(locked: PlanningSession) -> None:
        locked.processing_token = None
        locked.processing_started_at = None

    def _evaluate_completion(self, locked: PlanningSession):
        # Deterministic reevaluation through the existing stage/completion
        # machinery only -- never a hand-assembled manifest.  A minimal
        # provider-free stage graph is used because completion for an
        # already-accepted checkpoint set never calls execute/validate/accept.
        executor = StageExecutor(
            self.db,
            stage_definitions=(
                StageDefinition("planning_brief", version=1),
                StageDefinition(
                    "structured_task_plan",
                    version=1,
                    prerequisites=("planning_brief",),
                ),
            ),
            configuration={},
        )
        return executor.evaluate_completion(
            locked.id,
            session_generation_id=locked.generation_id,
            fencing_token=locked.processing_token,
        )

    @staticmethod
    def _verify_completion_manifest(session, checkpoint, manifest) -> None:
        if (
            manifest.planning_session_id != session.id
            or manifest.session_generation_id != session.generation_id
        ):
            raise ExecutionCommitError(
                "completion_manifest_inconsistent",
                "completion manifest does not match this planning session "
                "generation",
            )
        bound = None
        for entry in manifest.accepted_checkpoint_versions or ():
            if entry.get("stage_name") == STRUCTURED_TASK_PLAN_STAGE_NAME:
                bound = entry
                break
        if (
            bound is None
            or bound.get("checkpoint_id") != checkpoint.id
            or bound.get("content_hash") != checkpoint.content_hash
        ):
            raise ExecutionCommitError(
                "completion_manifest_inconsistent",
                "completion manifest does not bind the accepted Structured "
                "Task Plan checkpoint",
            )

    @staticmethod
    def _commit_identity(
        session, checkpoint, completion_manifest, review_id, event_id
    ) -> str:
        # Deliberately excludes ``operator_subject`` (and any other audit-only
        # field) so replaying the exact same authority is recognized as the
        # same commit regardless of which authorized operator issues the
        # replay -- only the authority payload determines commit identity.
        return canonical_json_hash(
            {
                "schema": PROVENANCE_SCHEMA,
                "planning_session_id": session.id,
                "session_generation_id": session.generation_id,
                "completion_manifest_id": completion_manifest.id,
                "structured_task_plan_checkpoint_id": checkpoint.id,
                "structured_task_plan_hash": checkpoint.content_hash,
                "review_id": review_id,
                "approval_event_id": event_id,
            }
        )

    @staticmethod
    def _build_provenance(
        session,
        checkpoint,
        task_plan,
        completion_manifest,
        review_id,
        event_id,
        operator_subject,
    ) -> dict:
        return {
            "schema": PROVENANCE_SCHEMA,
            "planning_session_id": session.id,
            "session_generation_id": session.generation_id,
            "completion_manifest_id": completion_manifest.id,
            "completion_manifest_hash": completion_manifest.manifest_hash,
            "structured_task_plan_checkpoint_id": checkpoint.id,
            "structured_task_plan_hash": checkpoint.content_hash,
            "task_ids": [task.id for task in task_plan.tasks],
            "review_id": review_id,
            "approval_event_id": event_id,
            "promotion_checkpoint_id": checkpoint.id,
            "operator_subject": operator_subject,
        }

    # -- idempotency command binding -------------------------------------

    @staticmethod
    def _canonical_request_hash(
        session_id: int, request: ExecutionCommitRequest
    ) -> str:
        return canonical_json_hash(
            {
                "schema": COMMAND_SCHEMA,
                "planning_session_id": session_id,
                "structured_task_plan_checkpoint_id": (
                    request.structured_task_plan_checkpoint_id
                ),
                "structured_task_plan_hash": request.structured_task_plan_hash,
                "expected_session_generation_id": (
                    request.expected_session_generation_id
                ),
                "expected_review_id": request.expected_review_id,
                "expected_approval_event_id": request.expected_approval_event_id,
            }
        )

    def _find_command(
        self, request: ExecutionCommitRequest
    ) -> ExecutionCommitCommand | None:
        return (
            self.db.query(ExecutionCommitCommand)
            .filter(
                ExecutionCommitCommand.operator_subject == request.operator_subject,
                ExecutionCommitCommand.idempotency_key == request.idempotency_key,
            )
            .one_or_none()
        )

    def _replay_from_command(
        self, command: ExecutionCommitCommand
    ) -> ExecutionCommitResult:
        manifest = self.db.get(
            PlanningCommitManifest, command.planning_commit_manifest_id
        )
        if manifest is None:
            raise ExecutionCommitError(
                "integrity_failure",
                "the commit manifest bound to this idempotency key could "
                "not be resolved",
            )
        execution_plan = None
        if command.execution_plan_id is not None:
            execution_plan = self.db.get(ExecutionPlan, command.execution_plan_id)
        if execution_plan is None:
            raise ExecutionCommitError(
                "integrity_failure",
                "the Execution Plan bound to this idempotency key could "
                "not be resolved",
            )
        provenance = manifest.task_provenance
        task_count = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == execution_plan.id)
            .count()
        )
        edge_count = (
            self.db.query(ExecutionDependencyEdge)
            .filter(ExecutionDependencyEdge.execution_plan_id == execution_plan.id)
            .count()
        )
        group_count = (
            self.db.query(ExecutionGroup)
            .filter(ExecutionGroup.execution_plan_id == execution_plan.id)
            .count()
        )
        membership_count = (
            self.db.query(ExecutionGroupMember)
            .join(ExecutionGroup)
            .filter(ExecutionGroup.execution_plan_id == execution_plan.id)
            .count()
        )
        return ExecutionCommitResult(
            planning_session_id=manifest.planning_session_id,
            session_generation_id=manifest.session_generation_id,
            structured_task_plan_checkpoint_id=provenance.get(
                "structured_task_plan_checkpoint_id"
            ),
            structured_task_plan_hash=provenance.get("structured_task_plan_hash"),
            review_id=provenance.get("review_id"),
            approval_event_id=provenance.get("approval_event_id"),
            completion_manifest_id=manifest.completion_manifest_id,
            completion_manifest_hash=provenance.get("completion_manifest_hash"),
            planning_commit_manifest_id=manifest.id,
            commit_identity=manifest.commit_identity,
            boundary_state="released",
            idempotent_replay=True,
            integrity_status="valid",
            execution_plan_id=execution_plan.id,
            execution_plan_generation=execution_plan.generation,
            execution_plan_status=execution_plan.status,
            task_count=task_count,
            dependency_edge_count=edge_count,
            group_count=group_count,
            group_membership_count=membership_count,
        )

    def _upsert_command(
        self,
        existing_command: ExecutionCommitCommand | None,
        *,
        session_id: int,
        request: ExecutionCommitRequest,
        canonical_request_hash: str,
        planning_commit_manifest_id: int,
    ) -> None:
        if existing_command is None:
            self.db.add(
                ExecutionCommitCommand(
                    planning_session_id=session_id,
                    operator_subject=request.operator_subject,
                    idempotency_key=request.idempotency_key,
                    canonical_request_hash=canonical_request_hash,
                    planning_commit_manifest_id=planning_commit_manifest_id,
                    boundary_state="released_execution_pending",
                )
            )
        else:
            existing_command.planning_commit_manifest_id = planning_commit_manifest_id
            existing_command.boundary_state = "released_execution_pending"

    # -- commit ----------------------------------------------------------

    def commit(
        self,
        session_id: int,
        request: ExecutionCommitRequest,
    ) -> ExecutionCommitResult:
        canonical_request_hash = self._canonical_request_hash(session_id, request)
        existing_command = self._find_command(request)
        if existing_command is not None:
            if (
                existing_command.canonical_request_hash != canonical_request_hash
                or existing_command.planning_session_id != session_id
            ):
                raise ExecutionCommitError(
                    "idempotency_key_conflict",
                    "idempotency key is already bound to a different "
                    "execution commit request",
                )
            if existing_command.boundary_state == "released":
                return self._replay_from_command(existing_command)
            # boundary_state == "released_execution_pending": the bound
            # Planning authority was already released in Transaction A but
            # Execution materialization (Transaction B) previously failed.
            # Fall through and retry against the same authority.

        session, _project = self._resolve_session_and_project(session_id)
        checkpoint = self._resolve_approved_task_plan(session, request)
        review_id, event_id, approval_operator_subject = self._resolve_approval_event(
            session, checkpoint, request
        )
        task_plan = self.protocol.load_accepted_structured_task_plan(session.id)
        if task_plan is None or task_plan.content_hash != checkpoint.content_hash:
            raise ExecutionCommitError(
                "integrity_failure",
                "accepted Structured Task Plan could not be re-derived",
            )
        try:
            ValidationContractService.preflight_task_plan(task_plan)
        except ValidationContractError as exc:
            raise ExecutionCommitError(exc.code, exc.message) from exc

        # Only manifests that claim to release *this exact* accepted
        # checkpoint are compared for conflict.  A manifest bound to a
        # different accepted promotion checkpoint (e.g. a future Planning
        # amendment/regeneration cycle) is a separate historical release,
        # not a competing claim over the same release identity, and must
        # not be rejected here.
        prior_manifests = (
            self.db.query(PlanningCommitManifest)
            .filter(PlanningCommitManifest.planning_session_id == session.id)
            .all()
        )
        for prior in prior_manifests:
            provenance = prior.task_provenance
            if not isinstance(provenance, Mapping):
                continue
            prior_checkpoint_id = provenance.get("structured_task_plan_checkpoint_id")
            if prior_checkpoint_id != checkpoint.id:
                continue
            prior_hash = provenance.get("structured_task_plan_hash")
            if prior_hash != checkpoint.content_hash:
                raise ExecutionCommitError(
                    "commit_manifest_conflict",
                    "a different Planning commit manifest already releases "
                    "a competing authority for this Structured Task Plan "
                    "checkpoint",
                )

        # A pure identity replay never needs a processing lease: nothing new
        # is written to Planning state when the exact release identity
        # (session, checkpoint, completion manifest, review, approval) has
        # already produced a commit manifest.  This is checked with a plain
        # read before touching ``processing_token`` so a concurrent owner of
        # an unrelated Planning operation never blocks an already-released
        # authority from being replayed.
        existing_completion = (
            self.db.query(PlanningCompletionManifest)
            .filter(PlanningCompletionManifest.planning_session_id == session.id)
            .one_or_none()
        )
        planning_commit_manifest = None
        completion_manifest = existing_completion
        if existing_completion is not None:
            self._verify_completion_manifest(session, checkpoint, existing_completion)
            prospective_identity = self._commit_identity(
                session, checkpoint, existing_completion, review_id, event_id
            )
            planning_commit_manifest = (
                self.db.query(PlanningCommitManifest)
                .filter(PlanningCommitManifest.commit_identity == prospective_identity)
                .one_or_none()
            )

        if planning_commit_manifest is not None:
            replayed_a = True
            self._upsert_command(
                existing_command,
                session_id=session.id,
                request=request,
                canonical_request_hash=canonical_request_hash,
                planning_commit_manifest_id=planning_commit_manifest.id,
            )
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        else:
            # All ``_assert_owner``-gated persistence calls below (completion
            # reevaluation, commit-manifest record) share one short-lived
            # lease.  On any failure this whole attempt is rolled back
            # uncommitted, so the lease never needs to be explicitly
            # released on the error path.
            locked = self._acquire_lease(session)
            try:
                if completion_manifest is None:
                    completion = self._evaluate_completion(locked)
                    if not completion.complete or completion.manifest is None:
                        raise ExecutionCommitError(
                            "completion_manifest_missing", completion.reason
                        )
                    completion_manifest = completion.manifest
                    self._verify_completion_manifest(
                        session, checkpoint, completion_manifest
                    )

                provenance = self._build_provenance(
                    session,
                    checkpoint,
                    task_plan,
                    completion_manifest,
                    review_id,
                    event_id,
                    approval_operator_subject,
                )
                commit_identity = self._commit_identity(
                    session, checkpoint, completion_manifest, review_id, event_id
                )
                try:
                    planning_commit_manifest = self.protocol.record_commit_manifest(
                        session.id,
                        task_provenance=provenance,
                        commit_identity=commit_identity,
                        completion_manifest_id=completion_manifest.id,
                        fencing_token=locked.processing_token,
                        session_generation_id=session.generation_id,
                        protocol_version=PROTOCOL_V2,
                    )
                except ProtocolPersistenceError as exc:
                    raise ExecutionCommitError(
                        "commit_manifest_conflict", str(exc)
                    ) from exc
            except Exception:
                self.db.rollback()
                raise
            self._release_lease(locked)

            self._upsert_command(
                existing_command,
                session_id=session.id,
                request=request,
                canonical_request_hash=canonical_request_hash,
                planning_commit_manifest_id=planning_commit_manifest.id,
            )
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

            replayed_a = any(
                item.id == planning_commit_manifest.id for item in prior_manifests
            )

        self.db.refresh(planning_commit_manifest)
        command = self._find_command(request)

        # -- Transaction B: Execution materialization --------------------
        existing_execution_plan_id = None
        pre_existing = (
            self.db.query(ExecutionPlan)
            .filter(
                ExecutionPlan.planning_commit_manifest_id == planning_commit_manifest.id
            )
            .one_or_none()
        )
        if pre_existing is not None:
            existing_execution_plan_id = pre_existing.id

        try:
            execution_service = ExecutionPlanCommitService(self.db)
            execution_plan = execution_service.commit(planning_commit_manifest.id)
            execution_service.verify_integrity(execution_plan.id)
            command.execution_plan_id = execution_plan.id
            command.boundary_state = "released"
            self.db.commit()
        except ExecutionPlanCommitError as exc:
            self.db.rollback()
            logger.error(
                "execution_commit_materialization_failed",
                extra={
                    "planning_session_id": session.id,
                    "planning_commit_manifest_id": planning_commit_manifest.id,
                    "operator_subject": request.operator_subject,
                    "idempotency_key": request.idempotency_key,
                    "detail": str(exc),
                },
            )
            return ExecutionCommitResult(
                planning_session_id=session.id,
                session_generation_id=session.generation_id,
                structured_task_plan_checkpoint_id=checkpoint.id,
                structured_task_plan_hash=checkpoint.content_hash,
                review_id=review_id,
                approval_event_id=event_id,
                completion_manifest_id=completion_manifest.id,
                completion_manifest_hash=completion_manifest.manifest_hash,
                planning_commit_manifest_id=planning_commit_manifest.id,
                commit_identity=planning_commit_manifest.commit_identity,
                boundary_state="released_execution_pending",
                idempotent_replay=replayed_a,
                integrity_status="execution_materialization_pending",
                retryable=True,
                execution_error_code=EXECUTION_MATERIALIZATION_FAILED,
                execution_failure_reason=str(exc),
            )

        replayed_b = existing_execution_plan_id == execution_plan.id
        task_count = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == execution_plan.id)
            .count()
        )
        edge_count = (
            self.db.query(ExecutionDependencyEdge)
            .filter(ExecutionDependencyEdge.execution_plan_id == execution_plan.id)
            .count()
        )
        group_count = (
            self.db.query(ExecutionGroup)
            .filter(ExecutionGroup.execution_plan_id == execution_plan.id)
            .count()
        )
        membership_count = (
            self.db.query(ExecutionGroupMember)
            .join(ExecutionGroup)
            .filter(ExecutionGroup.execution_plan_id == execution_plan.id)
            .count()
        )
        return ExecutionCommitResult(
            planning_session_id=session.id,
            session_generation_id=session.generation_id,
            structured_task_plan_checkpoint_id=checkpoint.id,
            structured_task_plan_hash=checkpoint.content_hash,
            review_id=review_id,
            approval_event_id=event_id,
            completion_manifest_id=completion_manifest.id,
            completion_manifest_hash=completion_manifest.manifest_hash,
            planning_commit_manifest_id=planning_commit_manifest.id,
            commit_identity=planning_commit_manifest.commit_identity,
            boundary_state="released",
            idempotent_replay=replayed_a or replayed_b,
            integrity_status="valid",
            execution_plan_id=execution_plan.id,
            execution_plan_generation=execution_plan.generation,
            execution_plan_status=execution_plan.status,
            task_count=task_count,
            dependency_edge_count=edge_count,
            group_count=group_count,
            group_membership_count=membership_count,
        )


__all__ = [
    "ERROR_CODES",
    "PROVENANCE_SCHEMA",
    "ExecutionCommitError",
    "ExecutionCommitRequest",
    "ExecutionCommitResult",
    "PlanningExecutionCommitService",
]
