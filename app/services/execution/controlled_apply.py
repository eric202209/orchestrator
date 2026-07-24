"""Phase 29D-2 policy-v2, approval, apply-attempt, and re-verification.

The service stops at a read-only precondition boundary.  It never writes a
workspace, runs a candidate command, invokes Git mutation, dispatches a
worker, changes task lifecycle, releases dependencies, or creates a result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAcceptanceDecision,
    ExecutionTaskApplyApproval,
    ExecutionTaskApplyAttempt,
    ExecutionTaskApplyAuthorization,
    ExecutionTaskApplyPreconditionVerification,
    ExecutionTaskAttempt,
    ExecutionTaskChangeSet,
    ExecutionTaskChangeSetOperation,
    ExecutionWorkspaceBaseState,
    ExecutionWorkspacePathObservation,
    ExecutionWorkspaceTarget,
)
from app.services.execution.candidate_content import (
    CandidateContentStore,
    LocalContentAddressedStore,
)
from app.services.execution.changeset import (
    ChangeSetError,
    validate_changeset_path,
    verify_change_set_integrity,
)
from app.services.execution.workspace_authority import (
    WorkspaceAuthorityError,
    WorkspaceBaseStateService,
    WorkspaceObservation,
    verify_workspace_base_state_integrity,
    verify_workspace_target_integrity,
)
from app.services.planning.operator_review import canonical_json_hash


APPLY_POLICY_V2_SCHEMA_VERSION = "execution-task-apply-authorization/2.0"
APPLY_POLICY_VERSION_V2 = 2
APPLY_POLICY_V2_ID = "controlled_apply_policy"
APPROVAL_SCHEMA_VERSION = "execution-task-apply-approval/1.0"
APPLY_ATTEMPT_SCHEMA_VERSION = "execution-task-apply-attempt/1.0"
PRECONDITION_VERIFICATION_SCHEMA_VERSION = (
    "execution-task-apply-precondition-verification/1.0"
)
APPROVAL_REQUIRED = True
APPLY_ATTEMPT_STATUSES = frozenset(
    {"created", "precondition_verified", "blocked", "cancelled"}
)
VERIFICATION_OUTCOMES = frozenset(
    {
        "precondition_verified",
        "blocked_workspace_changed",
        "blocked_target_identity_changed",
        "blocked_repository_head_changed",
        "blocked_path_state_changed",
        "blocked_dirty_state",
        "blocked_approval_missing",
        "blocked_integrity_failure",
    }
)


class ControlledApplyError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ApplyPolicyV2Decision:
    status: str
    reason: str
    detail: str | None = None


def _approval_scope(
    approval: ExecutionTaskApplyApproval | None,
) -> tuple[int, str] | None:
    if approval is None:
        return None
    return int(approval.id), approval.canonical_approval_hash


def _base_path_map(
    db: Session, base_state: ExecutionWorkspaceBaseState
) -> dict[str, ExecutionWorkspacePathObservation]:
    return {
        row.path: row
        for row in db.query(ExecutionWorkspacePathObservation)
        .filter(ExecutionWorkspacePathObservation.base_state_id == base_state.id)
        .order_by(ExecutionWorkspacePathObservation.observation_index)
        .all()
    }


def _descriptor_matches(
    change_set: ExecutionTaskChangeSet,
    target: ExecutionWorkspaceTarget,
    base_state: ExecutionWorkspaceBaseState,
) -> str | None:
    descriptor = change_set.base_state_payload or {}
    if descriptor.get("project_id") not in (None, target.project_id):
        return "changeset_base_state_project_mismatch"
    if descriptor.get("workspace_identity") not in (None, target.target_identity):
        return "changeset_base_state_workspace_mismatch"
    if descriptor.get("repository_head") not in (None, base_state.repository_head):
        return "changeset_base_state_head_mismatch"
    declared_clean = descriptor.get("clean")
    if declared_clean is not None and bool(declared_clean) != bool(
        base_state.workspace_clean
    ):
        return "changeset_base_state_clean_mismatch"
    return None


def _approval_integrity(approval: ExecutionTaskApplyApproval) -> bool:
    return (
        canonical_json_hash(approval.reviewed_summary_payload)
        == approval.reviewed_summary_hash
        and canonical_json_hash(approval.canonical_approval_payload)
        == approval.canonical_approval_hash
    )


def evaluate_apply_policy_v2(
    db: Session,
    change_set: ExecutionTaskChangeSet,
    *,
    workspace_target: ExecutionWorkspaceTarget | None = None,
    base_state: ExecutionWorkspaceBaseState | None = None,
    approval: ExecutionTaskApplyApproval | None = None,
    store: CandidateContentStore | None = None,
) -> ApplyPolicyV2Decision:
    """Evaluate ``controlled_apply_policy/2`` without mutating any authority."""

    plan = db.get(ExecutionPlan, change_set.execution_plan_id)
    if plan is None:
        return ApplyPolicyV2Decision("blocked", "missing_plan_authority")
    if plan.status != "active" or plan.superseded_by_execution_plan_id is not None:
        return ApplyPolicyV2Decision("denied", "superseded_plan")
    task = db.get(ExecutionTask, change_set.execution_task_id)
    attempt = db.get(ExecutionTaskAttempt, change_set.execution_task_attempt_id)
    if task is None or attempt is None:
        return ApplyPolicyV2Decision("blocked", "missing_task_authority")
    if (
        task.execution_plan_id != plan.id
        or attempt.execution_plan_id != plan.id
        or attempt.execution_task_id != task.id
        or attempt.attempt_generation != change_set.attempt_generation
    ):
        return ApplyPolicyV2Decision("denied", "authority_linkage_mismatch")
    acceptance = db.get(
        ExecutionTaskAcceptanceDecision, change_set.acceptance_decision_id
    )
    if acceptance is None:
        return ApplyPolicyV2Decision("blocked", "missing_acceptance_authority")
    if (
        acceptance.decision_status != "accepted"
        or acceptance.canonical_decision_hash != change_set.acceptance_decision_hash
    ):
        return ApplyPolicyV2Decision("denied", "candidate_not_accepted")
    if (
        canonical_json_hash(acceptance.canonical_decision_payload)
        != acceptance.canonical_decision_hash
    ):
        return ApplyPolicyV2Decision("denied", "acceptance_integrity_failure")
    reader = store or LocalContentAddressedStore()
    changeset_integrity = verify_change_set_integrity(db, change_set.id, store=reader)
    if not changeset_integrity.verified:
        return ApplyPolicyV2Decision(
            "denied",
            "changeset_integrity_failure",
            ",".join(changeset_integrity.issues),
        )
    if workspace_target is None:
        return ApplyPolicyV2Decision("blocked", "blocked_missing_workspace_target")
    if base_state is None:
        return ApplyPolicyV2Decision("blocked", "blocked_missing_base_state")
    target_integrity = verify_workspace_target_integrity(db, workspace_target.id)
    if not target_integrity.verified:
        return ApplyPolicyV2Decision(
            "blocked", "blocked_integrity_failure", ",".join(target_integrity.issues)
        )
    base_integrity = verify_workspace_base_state_integrity(db, base_state.id)
    if not base_integrity.verified:
        return ApplyPolicyV2Decision(
            "blocked", "blocked_integrity_failure", ",".join(base_integrity.issues)
        )
    if (
        workspace_target.project_id != change_set.target_project_id
        or base_state.workspace_target_id != workspace_target.id
        or base_state.project_id != workspace_target.project_id
        or base_state.change_set_id != change_set.id
        or change_set.target_workspace_identity
        not in (None, workspace_target.target_identity)
        or base_state.target_identity != workspace_target.target_identity
    ):
        return ApplyPolicyV2Decision("denied", "workspace_target_linkage_mismatch")
    descriptor_reason = _descriptor_matches(change_set, workspace_target, base_state)
    if descriptor_reason:
        return ApplyPolicyV2Decision("blocked", descriptor_reason)
    if base_state.repository_kind != "git_worktree" or not base_state.repository_head:
        return ApplyPolicyV2Decision("blocked", "unsupported_workspace_type")
    if any(
        bool(value) for value in (base_state.repository_operation_state or {}).values()
    ):
        return ApplyPolicyV2Decision("blocked", "repository_operation_in_progress")
    if base_state.dirty_state == "conflicting_dirty":
        return ApplyPolicyV2Decision("blocked", "blocked_dirty_state")
    if base_state.dirty_state not in {"clean", "unrelated_dirty"}:
        return ApplyPolicyV2Decision("blocked", "blocked_dirty_state")
    path_map = _base_path_map(db, base_state)
    operations = change_set.canonical_changeset_payload.get("operations", [])
    for operation in operations:
        try:
            path = validate_changeset_path(operation.get("path"))
        except ChangeSetError as exc:
            return ApplyPolicyV2Decision("denied", "unsafe_operation_path", exc.code)
        observed = path_map.get(path)
        if observed is None:
            return ApplyPolicyV2Decision("blocked", "blocked_path_state_changed", path)
        if observed.symlink_status != "not_symlink" or observed.entry_type in {
            "directory",
            "special",
        }:
            return ApplyPolicyV2Decision(
                "blocked", "blocked_path_precondition_failed", path
            )
        kind = str(operation.get("operation"))
        if kind == "create_file" and observed.exists:
            return ApplyPolicyV2Decision(
                "blocked", "blocked_path_precondition_failed", path
            )
        if kind in {"replace_file", "delete_file"}:
            expected = operation.get("expected_previous_sha256")
            if (
                not observed.exists
                or observed.entry_type != "regular_file"
                or observed.content_sha256 != expected
            ):
                return ApplyPolicyV2Decision(
                    "blocked", "blocked_path_precondition_failed", path
                )
    if approval is None:
        return ApplyPolicyV2Decision("blocked", "blocked_approval_missing")
    if (
        approval.change_set_id != change_set.id
        or approval.change_set_hash != change_set.changeset_sha256
        or approval.workspace_target_id != workspace_target.id
        or approval.workspace_target_hash != workspace_target.canonical_target_hash
        or approval.base_state_id != base_state.id
        or approval.base_state_hash != base_state.canonical_observation_hash
        or approval.apply_policy_id != APPLY_POLICY_V2_ID
        or approval.apply_policy_version != APPLY_POLICY_VERSION_V2
    ):
        return ApplyPolicyV2Decision("denied", "approval_scope_mismatch")
    if not _approval_integrity(approval):
        return ApplyPolicyV2Decision(
            "blocked", "blocked_integrity_failure", "approval_integrity_failure"
        )
    if approval.decision != "approved":
        return ApplyPolicyV2Decision("denied", "approval_rejected")
    existing_scope = (
        db.query(ExecutionTaskApplyAuthorization)
        .filter(
            ExecutionTaskApplyAuthorization.change_set_id == change_set.id,
            ExecutionTaskApplyAuthorization.apply_policy_id == APPLY_POLICY_V2_ID,
            ExecutionTaskApplyAuthorization.apply_policy_version
            == APPLY_POLICY_VERSION_V2,
            ExecutionTaskApplyAuthorization.base_state_id == base_state.id,
            ExecutionTaskApplyAuthorization.authorization_status == "authorized",
        )
        .count()
    )
    if existing_scope:
        return ApplyPolicyV2Decision("denied", "conflicting_authorization_exists")
    return ApplyPolicyV2Decision("authorized", "authorized")


def controlled_apply_policy_v2(*args: Any, **kwargs: Any) -> ApplyPolicyV2Decision:
    """Stable named entry point for the version-2 policy."""

    return evaluate_apply_policy_v2(*args, **kwargs)


def controlled_apply_policy(*args: Any, **kwargs: Any) -> ApplyPolicyV2Decision:
    """Python entry point corresponding to ``controlled_apply_policy/2``."""

    return evaluate_apply_policy_v2(*args, **kwargs)


@dataclass(frozen=True)
class CreateApplyApprovalCommand:
    change_set_id: int
    workspace_target_id: int
    base_state_id: int
    decision: str
    reviewed_summary_payload: dict[str, Any]
    approval_idempotency_key: str
    approver_actor_type: str = "operator"
    approver_actor_id: str = "system"


@dataclass(frozen=True)
class ApplyApprovalResult:
    approval: ExecutionTaskApplyApproval
    replayed: bool = False


class ApplyApprovalService:
    def __init__(self, db: Session, *, now: Any = None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    def decide(self, command: CreateApplyApprovalCommand) -> ApplyApprovalResult:
        change_set = self.db.get(ExecutionTaskChangeSet, int(command.change_set_id))
        target = self.db.get(ExecutionWorkspaceTarget, int(command.workspace_target_id))
        base_state = self.db.get(
            ExecutionWorkspaceBaseState, int(command.base_state_id)
        )
        if change_set is None or target is None or base_state is None:
            raise ControlledApplyError(
                "apply_approval_authority_missing",
                "ChangeSet, target, or base state is missing",
            )
        if command.decision not in {"approved", "rejected"}:
            raise ControlledApplyError(
                "apply_approval_decision_invalid", "approval decision is invalid"
            )
        if (
            base_state.workspace_target_id != target.id
            or base_state.change_set_id != change_set.id
        ):
            raise ControlledApplyError(
                "apply_approval_scope_invalid",
                "approval scope is not one exact target/base/ChangeSet",
            )
        if change_set.target_project_id != target.project_id:
            raise ControlledApplyError(
                "apply_approval_scope_invalid", "approval project linkage is invalid"
            )
        summary_hash = canonical_json_hash(command.reviewed_summary_payload)
        payload = {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "change_set_id": change_set.id,
            "change_set_hash": change_set.changeset_sha256,
            "workspace_target_id": target.id,
            "workspace_target_hash": target.canonical_target_hash,
            "base_state_id": base_state.id,
            "base_state_hash": base_state.canonical_observation_hash,
            "apply_policy_id": APPLY_POLICY_V2_ID,
            "apply_policy_version": APPLY_POLICY_VERSION_V2,
            "decision": command.decision,
            "approver_actor_type": command.approver_actor_type,
            "approver_actor_id": command.approver_actor_id,
            "reviewed_summary_hash": summary_hash,
            "approval_idempotency_key": command.approval_idempotency_key,
        }
        approval_hash = canonical_json_hash(payload)
        existing = (
            self.db.query(ExecutionTaskApplyApproval)
            .filter(
                ExecutionTaskApplyApproval.approval_idempotency_key
                == command.approval_idempotency_key
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_approval_hash != approval_hash:
                raise ControlledApplyError(
                    "apply_approval_idempotency_conflict",
                    "approval key is bound to a different decision",
                )
            return ApplyApprovalResult(existing, replayed=True)
        conflicting = (
            self.db.query(ExecutionTaskApplyApproval)
            .filter(
                ExecutionTaskApplyApproval.change_set_id == change_set.id,
                ExecutionTaskApplyApproval.base_state_id == base_state.id,
                ExecutionTaskApplyApproval.apply_policy_id == APPLY_POLICY_V2_ID,
                ExecutionTaskApplyApproval.apply_policy_version
                == APPLY_POLICY_VERSION_V2,
            )
            .one_or_none()
        )
        if conflicting is not None:
            raise ControlledApplyError(
                "apply_approval_conflict",
                "an immutable approval already exists for this exact scope",
            )
        now = self._now()
        row = ExecutionTaskApplyApproval(
            execution_plan_id=change_set.execution_plan_id,
            execution_task_id=change_set.execution_task_id,
            execution_task_attempt_id=change_set.execution_task_attempt_id,
            attempt_generation=change_set.attempt_generation,
            change_set_id=change_set.id,
            change_set_hash=change_set.changeset_sha256,
            workspace_target_id=target.id,
            workspace_target_hash=target.canonical_target_hash,
            base_state_id=base_state.id,
            base_state_hash=base_state.canonical_observation_hash,
            apply_policy_id=APPLY_POLICY_V2_ID,
            apply_policy_version=APPLY_POLICY_VERSION_V2,
            decision=command.decision,
            approver_actor_type=command.approver_actor_type,
            approver_actor_id=command.approver_actor_id,
            reviewed_summary_payload=command.reviewed_summary_payload,
            reviewed_summary_hash=summary_hash,
            canonical_approval_payload=payload,
            canonical_approval_hash=approval_hash,
            approval_idempotency_key=command.approval_idempotency_key,
            decided_at=now,
            created_at=now,
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(ExecutionTaskApplyApproval)
                .filter(
                    ExecutionTaskApplyApproval.approval_idempotency_key
                    == command.approval_idempotency_key
                )
                .one_or_none()
            )
            if replay is not None and replay.canonical_approval_hash == approval_hash:
                return ApplyApprovalResult(replay, replayed=True)
            raise ControlledApplyError(
                "apply_approval_insert_conflict",
                "approval conflicts with canonical authority",
            ) from exc
        return ApplyApprovalResult(row)


@dataclass(frozen=True)
class AuthorizeApplyV2Command:
    change_set_id: int
    workspace_target_id: int
    base_state_id: int
    approval_id: int | None
    authorization_idempotency_key: str
    decision_actor_type: str = "system"
    decision_actor_id: str = "controlled-apply-v2"


@dataclass(frozen=True)
class ApplyAuthorizationV2Result:
    authorization: ExecutionTaskApplyAuthorization
    replayed: bool = False


class ApplyAuthorizationV2Service:
    def __init__(
        self,
        db: Session,
        *,
        store: CandidateContentStore | None = None,
        now: Any = None,
    ):
        self.db = db
        self.store = store or LocalContentAddressedStore()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def authorize(self, command: AuthorizeApplyV2Command) -> ApplyAuthorizationV2Result:
        change_set = self.db.get(ExecutionTaskChangeSet, int(command.change_set_id))
        target = self.db.get(ExecutionWorkspaceTarget, int(command.workspace_target_id))
        base_state = self.db.get(
            ExecutionWorkspaceBaseState, int(command.base_state_id)
        )
        approval = (
            self.db.get(ExecutionTaskApplyApproval, int(command.approval_id))
            if command.approval_id
            else None
        )
        if change_set is None:
            raise ControlledApplyError(
                "apply_authorization_changeset_missing", "ChangeSet does not exist"
            )
        input_payload = {
            "schema_version": APPLY_POLICY_V2_SCHEMA_VERSION,
            "change_set_id": change_set.id,
            "change_set_hash": change_set.changeset_sha256,
            "workspace_target_id": target.id if target else None,
            "workspace_target_hash": target.canonical_target_hash if target else None,
            "base_state_id": base_state.id if base_state else None,
            "base_state_hash": (
                base_state.canonical_observation_hash if base_state else None
            ),
            "approval_id": approval.id if approval else None,
            "approval_hash": approval.canonical_approval_hash if approval else None,
            "apply_policy_id": APPLY_POLICY_V2_ID,
            "apply_policy_version": APPLY_POLICY_VERSION_V2,
            "authorization_idempotency_key": command.authorization_idempotency_key,
            "decision_actor_type": command.decision_actor_type,
            "decision_actor_id": command.decision_actor_id,
        }
        input_hash = canonical_json_hash(input_payload)
        existing = (
            self.db.query(ExecutionTaskApplyAuthorization)
            .filter(
                ExecutionTaskApplyAuthorization.authorization_idempotency_key
                == command.authorization_idempotency_key
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_input_hash != input_hash:
                raise ControlledApplyError(
                    "apply_authorization_idempotency_conflict",
                    "authorization key is bound to a different request",
                )
            return ApplyAuthorizationV2Result(existing, replayed=True)
        if target is None or base_state is None:
            decision = ApplyPolicyV2Decision("blocked", "blocked_missing_base_state")
        else:
            decision = evaluate_apply_policy_v2(
                self.db,
                change_set,
                workspace_target=target,
                base_state=base_state,
                approval=approval,
                store=self.store,
            )
        if target is not None and base_state is not None:
            conflict = (
                self.db.query(ExecutionTaskApplyAuthorization)
                .filter(
                    ExecutionTaskApplyAuthorization.change_set_id == change_set.id,
                    ExecutionTaskApplyAuthorization.apply_policy_id
                    == APPLY_POLICY_V2_ID,
                    ExecutionTaskApplyAuthorization.apply_policy_version
                    == APPLY_POLICY_VERSION_V2,
                    ExecutionTaskApplyAuthorization.base_state_id == base_state.id,
                    ExecutionTaskApplyAuthorization.authorization_status
                    == "authorized",
                )
                .first()
            )
            if conflict is not None:
                raise ControlledApplyError(
                    "apply_authorization_conflict",
                    "this ChangeSet/base state already has a v2 authorization",
                )
        decision_payload = {
            "schema_version": APPLY_POLICY_V2_SCHEMA_VERSION,
            "execution_plan_id": change_set.execution_plan_id,
            "execution_task_id": change_set.execution_task_id,
            "execution_task_attempt_id": change_set.execution_task_attempt_id,
            "attempt_generation": change_set.attempt_generation,
            "change_set_id": change_set.id,
            "change_set_hash": change_set.changeset_sha256,
            "target_project_id": change_set.target_project_id,
            "workspace_target_id": target.id if target else None,
            "target_workspace_identity": target.target_identity if target else None,
            "base_state_id": base_state.id if base_state else None,
            "base_state_hash": (
                base_state.canonical_observation_hash if base_state else None
            ),
            "approval_id": approval.id if approval else None,
            "approval_hash": approval.canonical_approval_hash if approval else None,
            "apply_policy_id": APPLY_POLICY_V2_ID,
            "apply_policy_version": APPLY_POLICY_VERSION_V2,
            "authorization_status": decision.status,
            "decision_reason": decision.reason,
            "bounded_detail": decision.detail,
        }
        command_hash = canonical_json_hash(input_payload)
        row = ExecutionTaskApplyAuthorization(
            execution_plan_id=change_set.execution_plan_id,
            execution_task_id=change_set.execution_task_id,
            execution_task_attempt_id=change_set.execution_task_attempt_id,
            attempt_generation=change_set.attempt_generation,
            change_set_id=change_set.id,
            change_set_hash=change_set.changeset_sha256,
            acceptance_decision_id=change_set.acceptance_decision_id,
            acceptance_decision_hash=change_set.acceptance_decision_hash,
            target_project_id=change_set.target_project_id,
            workspace_target_id=target.id if target else None,
            base_state_id=base_state.id if base_state else None,
            target_workspace_identity=target.target_identity if target else None,
            base_state_hash=(
                base_state.canonical_observation_hash
                if base_state
                else change_set.base_state_hash
            ),
            apply_policy_id=APPLY_POLICY_V2_ID,
            apply_policy_version=APPLY_POLICY_VERSION_V2,
            authorization_status=decision.status,
            decision_reason=decision.reason,
            bounded_detail=decision.detail,
            canonical_input_payload=input_payload,
            canonical_input_hash=input_hash,
            canonical_decision_payload=decision_payload,
            canonical_decision_hash=canonical_json_hash(decision_payload),
            authorization_idempotency_key=command.authorization_idempotency_key,
            deterministic_authorization_command_id=f"apply-authorization-command:{command_hash}",
            decision_actor_type=command.decision_actor_type,
            decision_actor_id=command.decision_actor_id,
            decided_at=self._now(),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(ExecutionTaskApplyAuthorization)
                .filter(
                    ExecutionTaskApplyAuthorization.authorization_idempotency_key
                    == command.authorization_idempotency_key
                )
                .one_or_none()
            )
            if replay is not None and replay.canonical_input_hash == input_hash:
                return ApplyAuthorizationV2Result(replay, replayed=True)
            raise ControlledApplyError(
                "apply_authorization_insert_conflict",
                "authorization conflicts with canonical authority",
            ) from exc
        return ApplyAuthorizationV2Result(row)


@dataclass(frozen=True)
class CreateApplyAttemptCommand:
    authorization_id: int
    approval_id: int | None
    apply_attempt_idempotency_key: str
    creation_actor_type: str = "operator"
    creation_actor_id: str = "system"


@dataclass(frozen=True)
class ApplyAttemptResult:
    apply_attempt: ExecutionTaskApplyAttempt
    replayed: bool = False


def _classify_observation_drift(
    base: ExecutionWorkspaceBaseState, current: WorkspaceObservation
) -> tuple[str, str]:
    if current.target.target_identity != base.target_identity:
        return "blocked_target_identity_changed", "target_identity_changed"
    if current.repository_head != base.repository_head:
        return "blocked_repository_head_changed", "repository_head_changed"
    if (
        current.dirty_state != base.dirty_state
        or current.dirty_path_summary_hash != base.dirty_path_summary_hash
    ):
        return "blocked_dirty_state", "dirty_state_changed"
    old_paths = base.canonical_observation_payload.get("path_observations", [])
    new_paths = [item.payload() for item in current.path_observations]
    if old_paths != new_paths:
        return "blocked_path_state_changed", "path_state_changed"
    if current.canonical_hash != base.canonical_observation_hash:
        return "blocked_workspace_changed", "workspace_observation_changed"
    return "precondition_verified", "precondition_matches_authorized_base_state"


class ApplyAttemptService:
    def __init__(self, db: Session, *, now: Any = None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._base_states = WorkspaceBaseStateService(db, now=self._now)

    def create(self, command: CreateApplyAttemptCommand) -> ApplyAttemptResult:
        authorization = self.db.get(
            ExecutionTaskApplyAuthorization, int(command.authorization_id)
        )
        approval = (
            self.db.get(ExecutionTaskApplyApproval, int(command.approval_id))
            if command.approval_id
            else None
        )
        if authorization is None:
            raise ControlledApplyError(
                "apply_attempt_authorization_missing", "authorization does not exist"
            )
        change_set = self.db.get(ExecutionTaskChangeSet, authorization.change_set_id)
        target = (
            self.db.get(ExecutionWorkspaceTarget, authorization.workspace_target_id)
            if authorization.workspace_target_id
            else None
        )
        base_state = (
            self.db.get(ExecutionWorkspaceBaseState, authorization.base_state_id)
            if authorization.base_state_id
            else None
        )
        input_payload = {
            "schema_version": APPLY_ATTEMPT_SCHEMA_VERSION,
            "authorization_id": authorization.id,
            "authorization_hash": authorization.canonical_decision_hash,
            "approval_id": approval.id if approval else None,
            "approval_hash": approval.canonical_approval_hash if approval else None,
            "apply_attempt_idempotency_key": command.apply_attempt_idempotency_key,
            "creation_actor_type": command.creation_actor_type,
            "creation_actor_id": command.creation_actor_id,
        }
        input_hash = canonical_json_hash(input_payload)
        existing = (
            self.db.query(ExecutionTaskApplyAttempt)
            .filter(
                ExecutionTaskApplyAttempt.apply_attempt_idempotency_key
                == command.apply_attempt_idempotency_key
            )
            .one_or_none()
        )
        if existing is not None:
            if any(
                existing.canonical_command_payload.get(key) != value
                for key, value in input_payload.items()
            ):
                raise ControlledApplyError(
                    "apply_attempt_idempotency_conflict",
                    "attempt key is bound to a different intent",
                )
            return ApplyAttemptResult(existing, replayed=True)
        if (
            authorization.authorization_status != "authorized"
            or authorization.apply_policy_version != 2
        ):
            raise ControlledApplyError(
                "apply_attempt_not_authorized",
                "only an authorized policy-v2 decision can create an apply attempt",
            )
        if (
            change_set is None
            or target is None
            or base_state is None
            or approval is None
        ):
            raise ControlledApplyError(
                "apply_attempt_scope_missing",
                "attempt requires ChangeSet, target, base state, and approval",
            )
        if (
            approval.decision != "approved"
            or approval.id != command.approval_id
            or approval.change_set_id != authorization.change_set_id
            or approval.base_state_id != authorization.base_state_id
            or approval.base_state_hash != authorization.base_state_hash
        ):
            raise ControlledApplyError(
                "apply_attempt_approval_invalid",
                "approval is missing or does not bind the exact authorization",
            )
        if not _approval_integrity(approval):
            raise ControlledApplyError(
                "apply_attempt_approval_integrity_failure", "approval integrity failed"
            )
        if (
            not verify_workspace_target_integrity(self.db, target.id).verified
            or not verify_workspace_base_state_integrity(
                self.db, base_state.id
            ).verified
        ):
            raise ControlledApplyError(
                "apply_attempt_integrity_failure",
                "target or base-state integrity failed",
            )
        if (
            self.db.query(ExecutionTaskApplyAttempt)
            .filter(ExecutionTaskApplyAttempt.authorization_id == authorization.id)
            .count()
        ):
            raise ControlledApplyError(
                "apply_attempt_authorization_conflict",
                "authorization already has an apply attempt",
            )
        maximum = (
            self.db.query(func.max(ExecutionTaskApplyAttempt.attempt_number))
            .filter(
                ExecutionTaskApplyAttempt.execution_task_id
                == authorization.execution_task_id
            )
            .scalar()
        )
        attempt_number = int(maximum or 0) + 1
        command_payload = {
            **input_payload,
            "change_set_id": change_set.id,
            "change_set_hash": change_set.changeset_sha256,
            "workspace_target_id": target.id,
            "workspace_target_hash": target.canonical_target_hash,
            "base_state_id": base_state.id,
            "base_state_hash": base_state.canonical_observation_hash,
            "apply_policy_id": authorization.apply_policy_id,
            "apply_policy_version": authorization.apply_policy_version,
            "attempt_number": attempt_number,
        }
        command_hash = canonical_json_hash(command_payload)
        now = self._now()
        row = ExecutionTaskApplyAttempt(
            execution_plan_id=authorization.execution_plan_id,
            execution_task_id=authorization.execution_task_id,
            execution_task_attempt_id=authorization.execution_task_attempt_id,
            attempt_generation=authorization.attempt_generation,
            change_set_id=change_set.id,
            change_set_hash=change_set.changeset_sha256,
            authorization_id=authorization.id,
            authorization_hash=authorization.canonical_decision_hash,
            approval_id=approval.id,
            approval_hash=approval.canonical_approval_hash,
            workspace_target_id=target.id,
            workspace_target_hash=target.canonical_target_hash,
            base_state_id=base_state.id,
            base_state_hash=base_state.canonical_observation_hash,
            apply_policy_id=authorization.apply_policy_id,
            apply_policy_version=authorization.apply_policy_version,
            attempt_number=attempt_number,
            status="created",
            status_reason=None,
            canonical_command_payload=command_payload,
            canonical_command_hash=command_hash,
            precondition_verification_hash=None,
            apply_attempt_idempotency_key=command.apply_attempt_idempotency_key,
            creation_actor_type=command.creation_actor_type,
            creation_actor_id=command.creation_actor_id,
            created_at=now,
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(ExecutionTaskApplyAttempt)
                .filter(
                    ExecutionTaskApplyAttempt.apply_attempt_idempotency_key
                    == command.apply_attempt_idempotency_key
                )
                .one_or_none()
            )
            if replay is not None and all(
                replay.canonical_command_payload.get(key) == value
                for key, value in input_payload.items()
            ):
                return ApplyAttemptResult(replay, replayed=True)
            raise ControlledApplyError(
                "apply_attempt_insert_conflict",
                "attempt conflicts with canonical authority",
            ) from exc
        return ApplyAttemptResult(row)

    def verify_preconditions(
        self, apply_attempt_id: int
    ) -> ExecutionTaskApplyPreconditionVerification:
        attempt = self.db.get(ExecutionTaskApplyAttempt, int(apply_attempt_id))
        if attempt is None:
            raise ControlledApplyError(
                "apply_attempt_missing", "apply attempt does not exist"
            )
        base_state = self.db.get(ExecutionWorkspaceBaseState, attempt.base_state_id)
        target = self.db.get(ExecutionWorkspaceTarget, attempt.workspace_target_id)
        change_set = self.db.get(ExecutionTaskChangeSet, attempt.change_set_id)
        authorization = self.db.get(
            ExecutionTaskApplyAuthorization, attempt.authorization_id
        )
        approval = self.db.get(ExecutionTaskApplyApproval, attempt.approval_id)
        outcome = "precondition_verified"
        reason = "precondition_matches_authorized_base_state"
        observed_hash: str | None = None
        observed_target_identity: str | None = None
        observation_payload: dict[str, Any] = {}
        if attempt.status in {"blocked", "cancelled"}:
            outcome, reason = (
                "blocked_integrity_failure",
                "attempt_is_not_reverifiable",
            )
        elif any(
            item is None
            for item in (base_state, target, change_set, authorization, approval)
        ):
            outcome, reason = (
                "blocked_integrity_failure",
                "apply_attempt_authority_missing",
            )
        elif (
            authorization.authorization_status != "authorized"
            or approval.decision != "approved"
        ):
            outcome, reason = (
                "blocked_approval_missing",
                "approval_or_authorization_not_approved",
            )
        elif not _approval_integrity(approval):
            outcome, reason = "blocked_integrity_failure", "approval_integrity_failure"
        elif (
            not verify_workspace_target_integrity(self.db, target.id).verified
            or not verify_workspace_base_state_integrity(
                self.db, base_state.id
            ).verified
        ):
            outcome, reason = (
                "blocked_integrity_failure",
                "workspace_authority_integrity_failure",
            )
        else:
            try:
                current = self._base_states.observe_current(target.id, change_set.id)
                observed_hash = current.canonical_hash
                observed_target_identity = current.target.target_identity
                observation_payload = current.canonical_payload
                outcome, reason = _classify_observation_drift(base_state, current)
            except WorkspaceAuthorityError as exc:
                observed_target_identity = target.target_identity
                reason = exc.code
                if (
                    exc.code.startswith("workspace_target")
                    or exc.code == "repository_root_mismatch"
                ):
                    outcome = "blocked_target_identity_changed"
                elif "head" in exc.code:
                    outcome = "blocked_repository_head_changed"
                elif "dirty" in exc.code or "operation" in exc.code:
                    outcome = "blocked_dirty_state"
                elif "path" in exc.code or "file" in exc.code:
                    outcome = "blocked_path_state_changed"
                else:
                    outcome = "blocked_workspace_changed"
        sequence = (
            int(
                self.db.query(
                    func.max(ExecutionTaskApplyPreconditionVerification.sequence)
                )
                .filter(
                    ExecutionTaskApplyPreconditionVerification.apply_attempt_id
                    == attempt.id
                )
                .scalar()
                or 0
            )
            + 1
        )
        payload = {
            "schema_version": PRECONDITION_VERIFICATION_SCHEMA_VERSION,
            "apply_attempt_id": attempt.id,
            "sequence": sequence,
            "outcome": outcome,
            "reason": reason,
            "authorized_base_state_id": attempt.base_state_id,
            "authorized_base_state_hash": attempt.base_state_hash,
            "observed_target_identity": observed_target_identity,
            "observed_state_hash": observed_hash,
            "observation": observation_payload,
        }
        verification_hash = canonical_json_hash(payload)
        verification = ExecutionTaskApplyPreconditionVerification(
            apply_attempt_id=attempt.id,
            sequence=sequence,
            outcome=outcome,
            reason=reason,
            authorized_base_state_id=attempt.base_state_id,
            authorized_base_state_hash=attempt.base_state_hash,
            observed_target_identity=observed_target_identity,
            observed_state_hash=observed_hash,
            canonical_verification_payload=payload,
            canonical_verification_hash=verification_hash,
            created_at=self._now(),
        )
        self.db.add(verification)
        if outcome == "precondition_verified":
            if attempt.status == "created":
                setattr(attempt, "status", "precondition_verified")
                attempt.status_reason = reason
                attempt.precondition_verification_hash = verification_hash
        else:
            if attempt.status in {"created", "precondition_verified"}:
                setattr(attempt, "status", "blocked")
                attempt.status_reason = outcome
                if attempt.precondition_verification_hash is None:
                    attempt.precondition_verification_hash = verification_hash
        self.db.flush()
        return verification


@dataclass(frozen=True)
class ApplyIntegrityResult:
    authority_id: int | None
    verified: bool
    issues: tuple[str, ...] = ()


def verify_apply_approval_integrity(
    db: Session, approval_id: int
) -> ApplyIntegrityResult:
    row = db.get(ExecutionTaskApplyApproval, int(approval_id))
    if row is None:
        return ApplyIntegrityResult(None, False, ("apply_approval_missing",))
    issues: list[str] = []
    if canonical_json_hash(row.reviewed_summary_payload) != row.reviewed_summary_hash:
        issues.append("apply_approval_summary_hash_mismatch")
    if (
        canonical_json_hash(row.canonical_approval_payload)
        != row.canonical_approval_hash
    ):
        issues.append("apply_approval_canonical_hash_mismatch")
    change_set = db.get(ExecutionTaskChangeSet, row.change_set_id)
    if change_set is None or change_set.changeset_sha256 != row.change_set_hash:
        issues.append("apply_approval_changeset_mismatch")
    target = db.get(ExecutionWorkspaceTarget, row.workspace_target_id)
    if target is None or target.canonical_target_hash != row.workspace_target_hash:
        issues.append("apply_approval_target_mismatch")
    base = db.get(ExecutionWorkspaceBaseState, row.base_state_id)
    if base is None or base.canonical_observation_hash != row.base_state_hash:
        issues.append("apply_approval_base_state_mismatch")
    return ApplyIntegrityResult(row.id, not issues, tuple(sorted(set(issues))))


def verify_apply_attempt_integrity(
    db: Session, attempt_id: int
) -> ApplyIntegrityResult:
    row = db.get(ExecutionTaskApplyAttempt, int(attempt_id))
    if row is None:
        return ApplyIntegrityResult(None, False, ("apply_attempt_missing",))
    issues: list[str] = []
    if canonical_json_hash(row.canonical_command_payload) != row.canonical_command_hash:
        issues.append("apply_attempt_command_hash_mismatch")
    authorization = db.get(ExecutionTaskApplyAuthorization, row.authorization_id)
    approval = db.get(ExecutionTaskApplyApproval, row.approval_id)
    change_set = db.get(ExecutionTaskChangeSet, row.change_set_id)
    if (
        authorization is None
        or authorization.canonical_decision_hash != row.authorization_hash
    ):
        issues.append("apply_attempt_authorization_mismatch")
    if approval is None or approval.canonical_approval_hash != row.approval_hash:
        issues.append("apply_attempt_approval_mismatch")
    if change_set is None or change_set.changeset_sha256 != row.change_set_hash:
        issues.append("apply_attempt_changeset_mismatch")
    verification_rows = (
        db.query(ExecutionTaskApplyPreconditionVerification)
        .filter(ExecutionTaskApplyPreconditionVerification.apply_attempt_id == row.id)
        .order_by(ExecutionTaskApplyPreconditionVerification.sequence)
        .all()
    )
    for item in verification_rows:
        if (
            canonical_json_hash(item.canonical_verification_payload)
            != item.canonical_verification_hash
        ):
            issues.append(f"apply_attempt_verification_hash_mismatch:{item.id}")
    if (
        row.precondition_verification_hash
        and row.precondition_verification_hash
        not in {item.canonical_verification_hash for item in verification_rows}
    ):
        issues.append("apply_attempt_verification_pointer_mismatch")
    if row.status == "precondition_verified" and not any(
        item.outcome == "precondition_verified" for item in verification_rows
    ):
        issues.append("apply_attempt_status_without_verified_precondition")
    return ApplyIntegrityResult(row.id, not issues, tuple(sorted(set(issues))))


def verify_precondition_verification_integrity(
    db: Session, verification_id: int
) -> ApplyIntegrityResult:
    row = db.get(ExecutionTaskApplyPreconditionVerification, int(verification_id))
    if row is None:
        return ApplyIntegrityResult(
            None, False, ("apply_precondition_verification_missing",)
        )
    issues: list[str] = []
    if row.outcome not in VERIFICATION_OUTCOMES:
        issues.append("apply_precondition_verification_outcome_invalid")
    if (
        canonical_json_hash(row.canonical_verification_payload)
        != row.canonical_verification_hash
    ):
        issues.append("apply_precondition_verification_hash_mismatch")
    return ApplyIntegrityResult(row.id, not issues, tuple(sorted(set(issues))))


__all__ = [
    "APPLY_ATTEMPT_STATUSES",
    "APPLY_POLICY_VERSION_V2",
    "APPROVAL_REQUIRED",
    "ApplyApprovalResult",
    "ApplyApprovalService",
    "ApplyAttemptResult",
    "ApplyAttemptService",
    "ApplyAuthorizationV2Result",
    "ApplyAuthorizationV2Service",
    "ApplyIntegrityResult",
    "ApplyPolicyV2Decision",
    "AuthorizeApplyV2Command",
    "ControlledApplyError",
    "CreateApplyApprovalCommand",
    "CreateApplyAttemptCommand",
    "controlled_apply_policy",
    "controlled_apply_policy_v2",
    "evaluate_apply_policy_v2",
    "verify_apply_approval_integrity",
    "verify_apply_attempt_integrity",
    "verify_precondition_verification_integrity",
]
