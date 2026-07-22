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

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy.orm import Session

from app.models import (
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

PROVENANCE_SCHEMA = "planning_execution_commit.v1"

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
        "integrity_failure",
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

    # -- commit ----------------------------------------------------------

    def commit(
        self,
        session_id: int,
        request: ExecutionCommitRequest,
    ) -> ExecutionCommitResult:
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

        prior_manifests = (
            self.db.query(PlanningCommitManifest)
            .filter(PlanningCommitManifest.planning_session_id == session.id)
            .all()
        )
        for prior in prior_manifests:
            provenance = prior.task_provenance
            prior_hash = (
                provenance.get("structured_task_plan_hash")
                if isinstance(provenance, Mapping)
                else None
            )
            if prior_hash != checkpoint.content_hash:
                raise ExecutionCommitError(
                    "commit_manifest_conflict",
                    "a different Planning commit manifest already releases "
                    "a competing task-plan authority for this session",
                )

        # All ``_assert_owner``-gated persistence calls below (completion
        # reevaluation, commit-manifest record) share one short-lived lease.
        # On any failure this whole attempt is rolled back uncommitted, so
        # the lease never needs to be explicitly released on the error path.
        locked = self._acquire_lease(session)
        try:
            existing_completion = (
                self.db.query(PlanningCompletionManifest)
                .filter(PlanningCompletionManifest.planning_session_id == session.id)
                .one_or_none()
            )
            if existing_completion is not None:
                completion_manifest = existing_completion
            else:
                completion = self._evaluate_completion(locked)
                if not completion.complete or completion.manifest is None:
                    raise ExecutionCommitError(
                        "completion_manifest_missing", completion.reason
                    )
                completion_manifest = completion.manifest
            self._verify_completion_manifest(session, checkpoint, completion_manifest)

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
        self.db.commit()
        self.db.refresh(planning_commit_manifest)

        replayed_a = any(
            item.id == planning_commit_manifest.id for item in prior_manifests
        )

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
            self.db.commit()
        except ExecutionPlanCommitError as exc:
            self.db.rollback()
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
                integrity_status="execution_materialization_failed",
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
