"""Phase 29B-1 Execution Authority Persistence Foundation.

``ExecutionPlanCommitService`` materializes one accepted Protocol v2
Structured Task Plan into a durable, immutable Execution Plan graph bound
to an exact ``PlanningCommitManifest`` authority.

This module is deliberately narrow: it never mutates Planning checkpoints,
review events, completion manifests, or commit manifests, and it never
creates runtime attempts, Celery jobs, legacy ``Task``/``Session`` rows,
workspaces, or ChangeSets.  See
``docs/roadmap/done/phase29/phase29b1-execution-authority-persistence-foundation.md``
for the full design record and deviations from the Phase 29A architecture
doc.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy.orm import Session

from app.models import (
    ExecutionDependencyEdge,
    ExecutionGroup,
    ExecutionGroupMember,
    ExecutionPlan,
    ExecutionTask,
    PlanningCommitManifest,
    PlanningCompletionManifest,
    PlanningSession,
)
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
)
from app.services.planning.structured_task_plan import (
    STRUCTURED_TASK_PLAN_STAGE_NAME,
    STRUCTURED_TASK_PLAN_STAGE_VERSION,
)

# Phase 29A's conservative dependency-type mapping (data only -- no
# scheduler eligibility, no review-gate resolution).  ``review_gate`` is
# kept as its own distinct runtime class per the Phase 29B-1 task brief,
# which is authoritative for this implementation slice; this is a
# deliberate, documented deviation from the Phase 29A doc's collapse of
# ``review_gate`` into plain ``blocking`` plus a side marker.
DEPENDENCY_RUNTIME_CLASS_MAP: Mapping[str, str] = {
    "hard_completion": "blocking",
    "ordering": "blocking",
    "review_gate": "blocking_review_gate",
    "artifact_ready": "blocking",
    "resource_serialization": "blocking",
}

PROTOCOL_V2 = "v2"


class ExecutionPlanCommitError(Exception):
    """Raised when an Execution Plan cannot be committed or verified."""


class ExecutionPlanCommitService:
    """Consumes an existing Planning commit authority in its own
    transaction.  Never invoked from Planning acceptance/promotion code."""

    def __init__(self, db: Session):
        self.db = db
        self._protocol = PlanningProtocolPersistenceService(db)

    # -- authority resolution -------------------------------------------------

    def _resolve_authority(self, planning_commit_manifest_id: int):
        manifest = self.db.get(PlanningCommitManifest, planning_commit_manifest_id)
        if manifest is None:
            raise ExecutionPlanCommitError("planning commit manifest not found")
        if manifest.protocol_version != PROTOCOL_V2:
            raise ExecutionPlanCommitError(
                "Execution Plan commit requires a Protocol v2 commit manifest"
            )

        session = self.db.get(PlanningSession, manifest.planning_session_id)
        if session is None:
            raise ExecutionPlanCommitError("planning session not found")
        if session.protocol_version != PROTOCOL_V2:
            raise ExecutionPlanCommitError(
                "Execution Plan commit requires a Protocol v2 Planning session"
            )
        if session.generation_id != manifest.session_generation_id:
            raise ExecutionPlanCommitError(
                "commit manifest session generation does not match the "
                "current Planning session generation"
            )

        if manifest.completion_manifest_id is None:
            raise ExecutionPlanCommitError(
                "commit manifest has no completion-manifest binding"
            )
        completion_manifest = self.db.get(
            PlanningCompletionManifest, manifest.completion_manifest_id
        )
        if (
            completion_manifest is None
            or completion_manifest.planning_session_id != session.id
        ):
            raise ExecutionPlanCommitError(
                "completion manifest does not belong to this Planning session"
            )
        if completion_manifest.session_generation_id != manifest.session_generation_id:
            raise ExecutionPlanCommitError(
                "completion manifest generation does not match the commit "
                "manifest generation"
            )

        task_plan = self._protocol.load_accepted_structured_task_plan(session.id)
        if task_plan is None:
            raise ExecutionPlanCommitError(
                "no accepted Structured Task Plan can be resolved for this "
                "Planning session"
            )

        effective = self._protocol.effective_checkpoints(
            session.id,
            stage_versions={
                STRUCTURED_TASK_PLAN_STAGE_NAME: STRUCTURED_TASK_PLAN_STAGE_VERSION
            },
        )
        checkpoint = effective.get(
            (STRUCTURED_TASK_PLAN_STAGE_NAME, STRUCTURED_TASK_PLAN_STAGE_VERSION)
        )
        if checkpoint is None or checkpoint.status != "accepted":
            raise ExecutionPlanCommitError(
                "accepted Structured Task Plan checkpoint could not be resolved"
            )
        if checkpoint.content_hash != task_plan.content_hash:
            raise ExecutionPlanCommitError(
                "Structured Task Plan checkpoint content hash mismatch"
            )

        bound_entry = None
        for entry in completion_manifest.accepted_checkpoint_versions or []:
            if (
                entry.get("stage_name") == STRUCTURED_TASK_PLAN_STAGE_NAME
                and entry.get("checkpoint_version")
                == STRUCTURED_TASK_PLAN_STAGE_VERSION
            ):
                bound_entry = entry
                break
        if bound_entry is None:
            raise ExecutionPlanCommitError(
                "completion manifest does not bind a Structured Task Plan " "checkpoint"
            )
        if (
            bound_entry.get("checkpoint_id") != checkpoint.id
            or bound_entry.get("content_hash") != task_plan.content_hash
        ):
            raise ExecutionPlanCommitError(
                "completion manifest / accepted checkpoint binding mismatch"
            )

        provenance = manifest.task_provenance
        # Phase 29B-2 canonical schema (``planning_execution_commit.v1``) names
        # the plan-hash field ``structured_task_plan_hash``.  A bare
        # ``task_plan_hash`` fallback is accepted only for the pre-existing
        # test-only provenance shape documented in the Phase 29B-1 report;
        # no production caller ever wrote that shape.
        provenance_hash = (
            provenance.get(
                "structured_task_plan_hash", provenance.get("task_plan_hash")
            )
            if isinstance(provenance, Mapping)
            else None
        )
        provenance_task_ids = (
            provenance.get("task_ids") if isinstance(provenance, Mapping) else None
        )
        if provenance_hash != task_plan.content_hash:
            raise ExecutionPlanCommitError(
                "commit manifest task provenance hash does not match the "
                "accepted Structured Task Plan"
            )
        expected_task_ids = sorted(task.id for task in task_plan.tasks)
        if (
            provenance_task_ids is None
            or sorted(str(value) for value in provenance_task_ids) != expected_task_ids
        ):
            raise ExecutionPlanCommitError(
                "commit manifest task provenance does not enumerate the "
                "accepted Structured Task Plan's task IDs"
            )

        return session, manifest, task_plan, checkpoint

    # -- commit ----------------------------------------------------------------

    def commit(self, planning_commit_manifest_id: int) -> ExecutionPlan:
        """Idempotently materialize the Execution Plan graph.

        Replaying with the same commit manifest returns the existing,
        identical Execution Plan.  A mismatched pre-existing graph fails
        closed without mutating anything.
        """

        session, manifest, task_plan, checkpoint = self._resolve_authority(
            planning_commit_manifest_id
        )

        existing = (
            self.db.query(ExecutionPlan)
            .filter(ExecutionPlan.planning_commit_manifest_id == manifest.id)
            .one_or_none()
        )
        if existing is not None:
            if (
                existing.planning_session_id != session.id
                or existing.protocol_version != manifest.protocol_version
                or existing.source_commit_identity != manifest.commit_identity
                or existing.source_plan_hash != task_plan.content_hash
                or existing.source_plan_checkpoint_id != checkpoint.id
            ):
                raise ExecutionPlanCommitError(
                    "an existing Execution Plan for this commit manifest does "
                    "not match the current Planning authority"
                )
            return existing

        execution_plan = ExecutionPlan(
            project_id=session.project_id,
            planning_session_id=session.id,
            planning_commit_manifest_id=manifest.id,
            generation=1,
            protocol_version=manifest.protocol_version,
            source_commit_identity=manifest.commit_identity,
            source_plan_checkpoint_id=checkpoint.id,
            source_plan_hash=task_plan.content_hash,
            status="active",
        )
        self.db.add(execution_plan)
        self.db.flush()

        task_rows: dict[str, ExecutionTask] = {}
        for task in task_plan.tasks:
            row = ExecutionTask(
                execution_plan_id=execution_plan.id,
                plan_task_id=task.id,
                title=task.title,
                blocking_state=task.blocking_state,
                task_spec=task.to_dict(),
                done_when=[item.done_when for item in task.work_items],
                status="pending",
            )
            self.db.add(row)
            task_rows[task.id] = row
        self.db.flush()

        seen_edges: set[tuple[int, int]] = set()
        for dependency in task_plan.dependencies:
            runtime_class = DEPENDENCY_RUNTIME_CLASS_MAP.get(dependency.type)
            if runtime_class is None:
                raise ExecutionPlanCommitError(
                    f"unknown Structured Task Plan dependency type: "
                    f"{dependency.type!r}"
                )
            prerequisite = task_rows.get(dependency.prerequisite_task_id)
            dependent = task_rows.get(dependency.dependent_task_id)
            if prerequisite is None or dependent is None:
                raise ExecutionPlanCommitError(
                    "dependency endpoint does not resolve within this " "Execution Plan"
                )
            if prerequisite.id == dependent.id:
                raise ExecutionPlanCommitError("dependency self-edge is not permitted")
            edge_key = (prerequisite.id, dependent.id)
            if edge_key in seen_edges:
                raise ExecutionPlanCommitError(
                    "duplicate dependency edge between the same two tasks"
                )
            seen_edges.add(edge_key)
            self.db.add(
                ExecutionDependencyEdge(
                    execution_plan_id=execution_plan.id,
                    plan_dependency_id=dependency.id,
                    prerequisite_execution_task_id=prerequisite.id,
                    dependent_execution_task_id=dependent.id,
                    source_dependency_type=dependency.type,
                    runtime_class=runtime_class,
                    rationale=dependency.reason or None,
                )
            )

        for group in task_plan.execution_groups:
            group_row = ExecutionGroup(
                execution_plan_id=execution_plan.id,
                plan_group_id=group.id,
                kind=group.kind,
                order_index=group.order,
                parallel_limit=group.parallel_limit,
                skip_policy=group.skip_policy,
            )
            self.db.add(group_row)
            self.db.flush()
            for index, task_ref in enumerate(group.task_ids):
                member_task = task_rows.get(task_ref)
                if member_task is None:
                    raise ExecutionPlanCommitError(
                        "execution group member does not resolve within this "
                        "Execution Plan"
                    )
                self.db.add(
                    ExecutionGroupMember(
                        execution_group_id=group_row.id,
                        execution_task_id=member_task.id,
                        member_order=index,
                    )
                )

        self.db.flush()
        return execution_plan

    # -- integrity ---------------------------------------------------------

    def verify_integrity(self, execution_plan_id: int) -> None:
        """Reload the persisted graph and verify it matches the accepted
        Structured Task Plan authority exactly.  Raises
        ``ExecutionPlanCommitError`` on any mismatch."""

        execution_plan = self.db.get(ExecutionPlan, execution_plan_id)
        if execution_plan is None:
            raise ExecutionPlanCommitError("Execution Plan not found")

        session, manifest, task_plan, checkpoint = self._resolve_authority(
            execution_plan.planning_commit_manifest_id
        )
        if execution_plan.planning_session_id != session.id:
            raise ExecutionPlanCommitError("Execution Plan session mismatch")
        if execution_plan.source_commit_identity != manifest.commit_identity:
            raise ExecutionPlanCommitError(
                "Execution Plan source commit identity mismatch"
            )
        if execution_plan.source_plan_hash != task_plan.content_hash:
            raise ExecutionPlanCommitError("Execution Plan source plan hash mismatch")
        if execution_plan.source_plan_checkpoint_id != checkpoint.id:
            raise ExecutionPlanCommitError(
                "Execution Plan source checkpoint identity mismatch"
            )

        stored_tasks: Sequence[ExecutionTask] = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == execution_plan.id)
            .all()
        )
        stored_by_plan_id = {row.plan_task_id: row for row in stored_tasks}
        expected_task_ids = {task.id for task in task_plan.tasks}
        if set(stored_by_plan_id) != expected_task_ids:
            raise ExecutionPlanCommitError(
                "Execution Plan tasks do not match the accepted Structured "
                "Task Plan's task set"
            )
        for task in task_plan.tasks:
            row = stored_by_plan_id[task.id]
            expected_done_when = [item.done_when for item in task.work_items]
            if (
                row.title != task.title
                or row.blocking_state != task.blocking_state
                or row.task_spec != task.to_dict()
                or row.done_when != expected_done_when
            ):
                raise ExecutionPlanCommitError(
                    f"Execution Task {task.id!r} does not match its Structured "
                    "Task Plan specification"
                )

        stored_edges = (
            self.db.query(ExecutionDependencyEdge)
            .filter(ExecutionDependencyEdge.execution_plan_id == execution_plan.id)
            .all()
        )
        row_by_task_id = {row.id: row.plan_task_id for row in stored_tasks}
        stored_edges_by_plan_id = {
            edge.plan_dependency_id: edge for edge in stored_edges
        }
        expected_dependency_ids = {
            dependency.id for dependency in task_plan.dependencies
        }
        if set(stored_edges_by_plan_id) != expected_dependency_ids:
            raise ExecutionPlanCommitError(
                "Execution Plan dependency edges do not match the accepted "
                "Structured Task Plan's dependency set"
            )
        for dependency in task_plan.dependencies:
            edge = stored_edges_by_plan_id[dependency.id]
            expected_runtime_class = DEPENDENCY_RUNTIME_CLASS_MAP[dependency.type]
            if (
                row_by_task_id.get(edge.prerequisite_execution_task_id)
                != dependency.prerequisite_task_id
                or row_by_task_id.get(edge.dependent_execution_task_id)
                != dependency.dependent_task_id
                or edge.source_dependency_type != dependency.type
                or edge.runtime_class != expected_runtime_class
            ):
                raise ExecutionPlanCommitError(
                    f"Execution dependency edge {dependency.id!r} does not "
                    "match its Structured Task Plan specification"
                )

        stored_groups = (
            self.db.query(ExecutionGroup)
            .filter(ExecutionGroup.execution_plan_id == execution_plan.id)
            .all()
        )
        stored_groups_by_plan_id = {
            group.plan_group_id: group for group in stored_groups
        }
        expected_group_ids = {group.id for group in task_plan.execution_groups}
        if set(stored_groups_by_plan_id) != expected_group_ids:
            raise ExecutionPlanCommitError(
                "Execution Plan groups do not match the accepted Structured "
                "Task Plan's group set"
            )
        for group in task_plan.execution_groups:
            group_row = stored_groups_by_plan_id[group.id]
            if (
                group_row.kind != group.kind
                or group_row.order_index != group.order
                or group_row.parallel_limit != group.parallel_limit
                or group_row.skip_policy != group.skip_policy
            ):
                raise ExecutionPlanCommitError(
                    f"Execution Group {group.id!r} metadata does not match its "
                    "Structured Task Plan specification"
                )
            members = (
                self.db.query(ExecutionGroupMember)
                .filter(ExecutionGroupMember.execution_group_id == group_row.id)
                .order_by(ExecutionGroupMember.member_order.asc())
                .all()
            )
            member_plan_task_ids = [
                row_by_task_id.get(member.execution_task_id) for member in members
            ]
            if tuple(member_plan_task_ids) != group.task_ids:
                raise ExecutionPlanCommitError(
                    f"Execution Group {group.id!r} membership does not match "
                    "its Structured Task Plan specification"
                )
