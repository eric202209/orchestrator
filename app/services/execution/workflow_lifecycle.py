"""Workflow lifecycle binding over the R3A workflow budget (Phase 40E-4R3B-1).

This module owns no table and performs no provider I/O; every durable write
goes through ``workflow_budget``. While ``WORKFLOW_BUDGET_ENFORCEMENT_ENABLED``
is false (the default) every function returns without touching the database,
so legacy attempts stay unbound and ``LEGACY_UNENFORCED``.

Binding hooks run only after the caller has committed the attempt row: R3A
writes on a separate ``BEGIN IMMEDIATE`` connection that can neither see an
uncommitted row nor take the lock the caller holds. Hooks never raise; a
failed or ambiguous binding leaves the attempt unbound, and the Worker's
``require_dispatch_identity`` admission check is the single fail-closed point.
No caller here derives lineage from ``(session_id, task_id)``.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import socket
from typing import Any, Iterator
import uuid

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ExecutorLaunchIntent,
    OneShotAuthorizationClaim,
    OneShotExecutionAuthorization,
    PlanningSession,
    ProviderRequestReservation,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
    WorkflowBudget,
    WorkflowTaskExecutionBinding,
)
from app.services.execution.workflow_budget import (
    BindingKind,
    BudgetOutcome,
    EnforcementMode,
    InvalidBudgetPolicy,
    WorkflowBudgetPolicy,
    WorkflowBudgetSchemaUnavailable,
    WorkflowKind,
    bind_task_execution_attempt,
    close_workflow,
    create_planning_session_workflow,
    create_task_execution_workflow,
    open_session_workflow_uuids,
    resolve_planning_session_workflow,
    resolve_task_execution_binding,
    resolve_task_execution_workflow,
    trusted_utc_now,
    validate_budget_policy,
)

logger = logging.getLogger(__name__)

DISPATCH_PENDING_BINDING = "PENDING_BINDING"
DISPATCH_READY = "READY"
DISPATCH_CLAIMED = "CLAIMED"
DISPATCH_TERMINAL = "TERMINAL"
DISPATCH_REFUSED = "REFUSED"
_DISPATCH_KINDS = frozenset({"ROOT", "RETRY", "RECOVERY"})
_DISPATCH_LEASE_SECONDS = 120
_DISPATCH_RETRY_BASE_SECONDS = 300
_DISPATCH_RETRY_MAX_SECONDS = 3600
_DISPATCH_ENVELOPE_MAX_BYTES = 16384
_DISPATCH_SECRET_KEY_PARTS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)

WORKFLOW_IDENTITY_INVALID = "workflow_identity_invalid"


class WorkflowLifecycleError(RuntimeError):
    """Enforced workflow identity is missing, invalid, closed, or mismatched."""

    failure_category = WORKFLOW_IDENTITY_INVALID

    def __init__(self, reason: str):
        super().__init__(f"{WORKFLOW_IDENTITY_INVALID}: {reason}")
        self.reason = reason


RETAINED_EXECUTION_EVIDENCE_CONFLICT = "RETAINED_EXECUTION_EVIDENCE_CONFLICT"


class RetainedExecutionEvidenceConflict(RuntimeError):
    """A destructive operation targets rows referenced by immutable evidence."""


def retained_execution_evidence_exists(
    db: Session,
    *,
    project_ids=(),
    session_ids=(),
    task_ids=(),
    planning_session_ids=(),
) -> bool:
    """Check Phase 39/40 evidence references for the requested owned graph."""

    project_ids = set(project_ids)
    session_ids = set(session_ids)
    task_ids = set(task_ids)
    planning_session_ids = set(planning_session_ids)
    if project_ids:
        session_ids.update(
            row[0]
            for row in db.query(SessionModel.id)
            .filter(SessionModel.project_id.in_(project_ids))
            .all()
        )
        task_ids.update(
            row[0]
            for row in db.query(Task.id).filter(Task.project_id.in_(project_ids)).all()
        )
        planning_session_ids.update(
            row[0]
            for row in db.query(PlanningSession.id)
            .filter(PlanningSession.project_id.in_(project_ids))
            .all()
        )

    execution_scope = []
    if session_ids:
        execution_scope.append(TaskExecution.session_id.in_(session_ids))
    if task_ids:
        execution_scope.append(TaskExecution.task_id.in_(task_ids))
    execution_ids = set()
    if execution_scope:
        execution_ids.update(
            row[0] for row in db.query(TaskExecution.id).filter(or_(*execution_scope))
        )

    def exists(model, *conditions) -> bool:
        conditions = [condition for condition in conditions if condition is not None]
        return (
            bool(conditions)
            and db.query(model).filter(or_(*conditions)).first() is not None
        )

    def direct_project(model):
        return getattr(model, "project_id").in_(project_ids) if project_ids else None

    def direct_session(model):
        return getattr(model, "session_id").in_(session_ids) if session_ids else None

    def direct_task(model):
        return getattr(model, "task_id").in_(task_ids) if task_ids else None

    def direct_execution(model):
        if execution_ids and hasattr(model, "task_execution_id"):
            return getattr(model, "task_execution_id").in_(execution_ids)
        return None

    if exists(
        OneShotExecutionAuthorization,
        direct_project(OneShotExecutionAuthorization),
        direct_session(OneShotExecutionAuthorization),
        direct_task(OneShotExecutionAuthorization),
        direct_execution(OneShotExecutionAuthorization),
    ):
        return True
    if exists(
        ExecutorLaunchIntent,
        direct_project(ExecutorLaunchIntent),
        direct_session(ExecutorLaunchIntent),
        direct_task(ExecutorLaunchIntent),
        direct_execution(ExecutorLaunchIntent),
    ):
        return True
    if exists(OneShotAuthorizationClaim, direct_execution(OneShotAuthorizationClaim)):
        return True

    workflow_scope = []
    if project_ids:
        workflow_scope.append(WorkflowBudget.project_id.in_(project_ids))
    if session_ids:
        workflow_scope.append(WorkflowBudget.session_id.in_(session_ids))
    if task_ids:
        workflow_scope.append(WorkflowBudget.task_id.in_(task_ids))
    if planning_session_ids:
        workflow_scope.append(
            WorkflowBudget.planning_session_id.in_(planning_session_ids)
        )
    if execution_ids:
        workflow_scope.append(WorkflowBudget.root_task_execution_id.in_(execution_ids))
    workflow_ids = set()
    if workflow_scope:
        workflow_ids.update(
            row[0]
            for row in db.query(WorkflowBudget.workflow_uuid)
            .filter(or_(*workflow_scope))
            .all()
        )
    binding_scope = []
    if execution_ids:
        binding_scope.append(
            WorkflowTaskExecutionBinding.task_execution_id.in_(execution_ids)
        )
    if workflow_ids:
        binding_scope.append(
            WorkflowTaskExecutionBinding.workflow_uuid.in_(workflow_ids)
        )
    if exists(WorkflowBudget, *workflow_scope):
        return True
    if exists(WorkflowTaskExecutionBinding, *binding_scope):
        return True
    reservation_scope = []
    if execution_ids:
        reservation_scope.append(
            ProviderRequestReservation.task_execution_id.in_(execution_ids)
        )
    if workflow_ids:
        reservation_scope.append(
            ProviderRequestReservation.workflow_uuid.in_(workflow_ids)
        )
    return exists(ProviderRequestReservation, *reservation_scope)


def require_no_retained_execution_evidence(db: Session, **scope) -> None:
    if retained_execution_evidence_exists(db, **scope):
        raise RetainedExecutionEvidenceConflict(RETAINED_EXECUTION_EVIDENCE_CONFLICT)


def require_resume_provenance() -> None:
    """Fail closed where continuation lacks an exact durable parent attempt."""

    if enforcement_enabled():
        raise WorkflowLifecycleError("cross_generation_resume_unsupported")


def require_binding_outcome(
    outcome: BudgetOutcome | None,
    *,
    binding_kind: BindingKind | str,
) -> None:
    """Require the expected durable binding before an attempt can be published."""

    if not enforcement_enabled():
        return
    if outcome is BudgetOutcome.COMMIT_UNCERTAIN:
        # The caller may proceed to the durable publisher, which retries the
        # same T2 identity. Uncertainty is recoverable and must not terminalize
        # the TaskExecution or mint replacement provenance.
        return
    kind = getattr(binding_kind, "value", binding_kind)
    accepted = (
        {BudgetOutcome.CREATED, BudgetOutcome.ALREADY_EXISTS}
        if kind in {BindingKind.ROOT.value, "PLANNING_ROOT"}
        else {BudgetOutcome.BOUND, BudgetOutcome.ALREADY_BOUND}
    )
    if outcome not in accepted:
        value = outcome.value if isinstance(outcome, BudgetOutcome) else "UNAVAILABLE"
        raise WorkflowLifecycleError(f"workflow_binding_failed:{value}")


@dataclass(frozen=True)
class WorkflowRuntimeIdentity:
    """Canonical identity a runtime carries; built only from durable rows."""

    workflow_uuid: str
    workflow_kind: str
    session_id: int | None = None
    session_instance_id: str | None = None
    task_id: int | None = None
    task_execution_id: int | None = None
    planning_session_id: int | None = None
    planning_generation_id: str | None = None
    role: str | None = None


def enforcement_enabled() -> bool:
    return bool(getattr(settings, "WORKFLOW_BUDGET_ENFORCEMENT_ENABLED", False))


def require_managed_dispatch_contract(task_execution: Any) -> None:
    """Refuse legacy TaskExecution rows at enforced execution boundaries."""

    if not enforcement_enabled():
        return
    if getattr(task_execution, "id", None) is None:
        raise WorkflowLifecycleError("attempt_missing")
    if getattr(task_execution, "dispatch_state", None) is None:
        raise WorkflowLifecycleError("dispatch_contract_required")


def active_policy(kind: WorkflowKind) -> WorkflowBudgetPolicy | None:
    """The configured ENFORCED policy, ``None`` when disabled; invalid raises."""

    if not enforcement_enabled():
        return None
    policy = WorkflowBudgetPolicy(
        policy_id=str(settings.WORKFLOW_BUDGET_POLICY_ID or ""),
        policy_version=str(settings.WORKFLOW_BUDGET_POLICY_VERSION or ""),
        workflow_kind=kind,
        max_requests=settings.WORKFLOW_BUDGET_MAX_REQUESTS,
        max_duration_seconds=settings.WORKFLOW_BUDGET_MAX_DURATION_SECONDS,
        enforcement_mode=EnforcementMode.ENFORCED,
    )
    try:
        validate_budget_policy(policy)
    except InvalidBudgetPolicy as exc:
        raise WorkflowLifecycleError("policy_invalid") from exc
    return policy


def _log_outcome(action: str, outcome: BudgetOutcome, **identity: Any) -> None:
    accepted = {
        BudgetOutcome.CREATED,
        BudgetOutcome.ALREADY_EXISTS,
        BudgetOutcome.BOUND,
        BudgetOutcome.ALREADY_BOUND,
        BudgetOutcome.CLOSED,
        BudgetOutcome.ALREADY_CLOSED,
    }
    log = logger.info if outcome in accepted else logger.warning
    log("[WORKFLOW_BUDGET] %s outcome=%s %s", action, outcome.value, identity)


def bind_new_root(
    db: Session,
    task_execution_id: int | None,
    *,
    expected_session_instance_id: str | None = None,
) -> BudgetOutcome | None:
    """A committed attempt that starts a new logical run becomes a root.

    Refused while the Task still has an OPEN, unexpired workflow (a paused or
    resumed run): that attempt stays unbound and the Worker refuses it.
    """

    if not enforcement_enabled() or task_execution_id is None:
        return None
    try:
        policy = active_policy(WorkflowKind.TASK_EXECUTION)
        result = create_task_execution_workflow(
            db,
            policy,
            root_task_execution_id=task_execution_id,
            exclusive_open_task=True,
            expected_session_instance_id=expected_session_instance_id,
        )
    except Exception as exc:  # preserve an idempotently retryable ambiguous result
        logger.warning("[WORKFLOW_BUDGET] root binding failed: %s", exc)
        if isinstance(exc, WorkflowLifecycleError):
            return None
        return BudgetOutcome.COMMIT_UNCERTAIN
    _log_outcome("bind_root", result.outcome, task_execution_id=task_execution_id)
    return result.outcome


def bind_continuation(
    db: Session,
    task_execution_id: int | None,
    *,
    parent_task_execution_id: int | None,
    binding_kind: BindingKind,
    expected_session_instance_id: str | None = None,
) -> BudgetOutcome | None:
    """Inherit the parent attempt's workflow; no parent means no binding."""

    if not enforcement_enabled() or task_execution_id is None:
        return None
    if parent_task_execution_id is None:
        _log_outcome(
            "bind_continuation",
            BudgetOutcome.IDENTITY_CONFLICT,
            task_execution_id=task_execution_id,
            reason="parent_unknown",
        )
        return BudgetOutcome.IDENTITY_CONFLICT
    try:
        result = bind_task_execution_attempt(
            db,
            task_execution_id=task_execution_id,
            parent_task_execution_id=parent_task_execution_id,
            binding_kind=binding_kind,
            expected_session_instance_id=expected_session_instance_id,
        )
    except Exception as exc:
        logger.warning("[WORKFLOW_BUDGET] continuation binding failed: %s", exc)
        if isinstance(exc, WorkflowLifecycleError):
            return None
        return BudgetOutcome.COMMIT_UNCERTAIN
    _log_outcome(
        "bind_continuation",
        result.outcome,
        task_execution_id=task_execution_id,
        parent_task_execution_id=parent_task_execution_id,
    )
    return result.outcome


def _dispatch_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _contains_dispatch_secret(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _DISPATCH_SECRET_KEY_PARTS):
                return True
            if _contains_dispatch_secret(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_dispatch_secret(item) for item in value)
    return False


def configure_task_execution_dispatch(
    db: Session,
    *,
    task_execution: TaskExecution,
    session: SessionModel,
    task: Task,
    dispatch_kind: str,
    prompt: str,
    timeout_seconds: int,
    parent_task_execution_id: int | None = None,
    context: dict[str, Any] | None = None,
    planning_backend_override: str | None = None,
    planning_escalation_metadata: dict[str, Any] | None = None,
    continuation_task_id: int | None = None,
    continuation_kind: str | None = None,
    continuation_retry_count: int | None = None,
    resume_checkpoint_name: str | None = None,
) -> bool:
    """Attach a bounded replay contract before the caller's T1 commit.

    Prompt text is not copied. Only an exact reference to the persisted Task
    description/title is replayable; mutable Task changes are detected by the
    stored digest and permanently refused. Ad-hoc prompts and checkpoint
    context remain legacy-unmanaged while enforcement is disabled.
    """

    if (
        dispatch_kind not in _DISPATCH_KINDS
        or not isinstance(session.instance_id, str)
        or not session.instance_id
        or task_execution.session_id != session.id
        or task_execution.task_id != task.id
        or (dispatch_kind == "ROOT" and parent_task_execution_id is not None)
        or (dispatch_kind in {"RETRY", "RECOVERY"} and parent_task_execution_id is None)
        or resume_checkpoint_name is not None
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        return False
    source = None
    if prompt == (task.description or "").strip() and prompt:
        source = "description"
    elif prompt == (task.title or "").strip() and prompt:
        source = "title"
    if source is None:
        return False

    invocation = {
        "version": 1,
        "session_id": session.id,
        "task_id": task.id,
        "queued_event_id": str(uuid.uuid4()),
        "prompt_source": source,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "timeout_seconds": timeout_seconds,
        "context": context,
        "planning_backend_override": planning_backend_override,
        "planning_escalation_metadata": planning_escalation_metadata,
        "continuation": (
            {
                "task_id": continuation_task_id,
                "kind": continuation_kind,
                "retry_count": continuation_retry_count,
            }
            if continuation_kind is not None
            else None
        ),
    }
    try:
        encoded = _dispatch_json(invocation)
    except (TypeError, ValueError):
        return False
    if len(
        encoded.encode("utf-8")
    ) > _DISPATCH_ENVELOPE_MAX_BYTES or _contains_dispatch_secret(invocation):
        return False

    task_execution.dispatch_state = DISPATCH_PENDING_BINDING
    task_execution.dispatch_kind = dispatch_kind
    task_execution.dispatch_session_instance_id = session.instance_id
    task_execution.dispatch_parent_task_execution_id = parent_task_execution_id
    task_execution.dispatch_celery_task_id = str(uuid.uuid4())
    task_execution.dispatch_invocation = invocation
    task_execution.dispatch_invocation_sha256 = hashlib.sha256(
        encoded.encode("utf-8")
    ).hexdigest()
    task_execution.dispatch_lease_owner = None
    task_execution.dispatch_lease_fence = 0
    task_execution.dispatch_lease_expires_at = None
    task_execution.dispatch_publish_attempts = 0
    task_execution.dispatch_next_attempt_at = datetime.now(timezone.utc)
    task_execution.dispatch_last_error = None
    task_execution.dispatch_refusal_reason = None
    task_execution.dispatch_claimed_at = None
    db.flush()
    return True


def _dispatch_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _refuse_task_execution_dispatch(
    db: Session, task_execution_id: int, reason: str
) -> None:
    execution = (
        db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
    )
    if execution is None or execution.dispatch_state in {
        DISPATCH_CLAIMED,
        DISPATCH_TERMINAL,
    }:
        return
    execution.dispatch_state = DISPATCH_REFUSED
    execution.dispatch_refusal_reason = reason[:255]
    execution.dispatch_last_error = reason[:1000]
    execution.dispatch_lease_owner = None
    execution.dispatch_lease_expires_at = None
    db.commit()


def _defer_task_execution_dispatch(
    db: Session, task_execution_id: int, reason: str
) -> None:
    """Keep transient binding/DB failures recoverable with bounded backoff."""

    execution = (
        db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
    )
    if execution is None or execution.dispatch_state not in {
        DISPATCH_PENDING_BINDING,
        DISPATCH_READY,
    }:
        db.rollback()
        return
    execution.dispatch_last_error = reason[:1000]
    execution.dispatch_next_attempt_at = _dispatch_now() + timedelta(
        seconds=_DISPATCH_RETRY_BASE_SECONDS
    )
    db.commit()


def _dispatch_replay_arguments(
    db: Session, execution: TaskExecution
) -> tuple[dict[str, Any] | None, str | None]:
    invocation = execution.dispatch_invocation
    if not isinstance(invocation, dict):
        return None, "invocation_missing"
    try:
        canonical = _dispatch_json(invocation).encode("utf-8")
    except (TypeError, ValueError):
        return None, "invocation_invalid"
    if hashlib.sha256(canonical).hexdigest() != execution.dispatch_invocation_sha256:
        return None, "invocation_digest_mismatch"
    if (
        invocation.get("version") != 1
        or invocation.get("session_id") != execution.session_id
        or invocation.get("task_id") != execution.task_id
        or execution.dispatch_kind not in _DISPATCH_KINDS
        or not execution.dispatch_session_instance_id
        or not execution.dispatch_celery_task_id
    ):
        return None, "dispatch_identity_mismatch"
    session = (
        db.query(SessionModel).filter(SessionModel.id == execution.session_id).first()
    )
    task = db.query(Task).filter(Task.id == execution.task_id).first()
    if session is None or task is None or session.deleted_at is not None:
        return None, "session_or_task_missing"
    if session.instance_id != execution.dispatch_session_instance_id:
        return None, "session_generation_changed"
    if execution.status != TaskStatus.PENDING:
        return None, "attempt_not_pending"
    session_task_link = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == execution.session_id,
            SessionTask.task_id == execution.task_id,
        )
        .order_by(SessionTask.id.desc())
        .first()
    )
    if (
        task.status != TaskStatus.PENDING
        or session_task_link is None
        or session_task_link.status != TaskStatus.PENDING
    ):
        return None, "task_or_session_task_not_pending"
    if execution.dispatch_kind in {"RETRY", "RECOVERY"}:
        parent = (
            db.query(TaskExecution)
            .filter(TaskExecution.id == execution.dispatch_parent_task_execution_id)
            .first()
        )
        if (
            parent is None
            or parent.session_id != execution.session_id
            or parent.task_id != execution.task_id
            or parent.attempt_number >= execution.attempt_number
        ):
            return None, "explicit_parent_identity_invalid"
    exact_continuation = (
        session.continuation_task_execution_id == execution.id
        and session.continuation_task_id is not None
    )
    continuation = invocation.get("continuation")
    if execution.dispatch_kind == "RECOVERY" or exact_continuation:
        if session.status not in {"recovering", "retry_pending"}:
            return None, f"recovery_session_not_runnable:{session.status}"
        if not exact_continuation:
            return None, "continuation_identity_missing"
        if (
            session.continuation_task_id != execution.task_id
            or session.continuation_task_execution_id != execution.id
        ):
            return None, "continuation_marker_changed"
        current_continuation = {
            "task_id": session.continuation_task_id,
            "kind": session.continuation_kind,
            "retry_count": session.continuation_retry_count,
        }
        if continuation is not None and continuation != current_continuation:
            return None, "continuation_marker_changed"
        continuation = current_continuation
    elif session.status not in {"pending", "running"}:
        return None, f"session_not_runnable:{session.status}"
    prompt_source = invocation.get("prompt_source")
    prompt = (
        (task.description or "").strip()
        if prompt_source == "description"
        else (task.title or "").strip() if prompt_source == "title" else None
    )
    if prompt is None or hashlib.sha256(
        prompt.encode("utf-8")
    ).hexdigest() != invocation.get("prompt_sha256"):
        return None, "task_prompt_reference_changed"
    if _contains_dispatch_secret(invocation):
        return None, "invocation_contains_secret_field"

    args: dict[str, Any] = {
        "session_id": execution.session_id,
        "task_id": execution.task_id,
        "prompt": prompt,
        "timeout_seconds": invocation.get("timeout_seconds"),
        "expected_session_instance_id": execution.dispatch_session_instance_id,
        "task_execution_id": execution.id,
        "queued_event_id": invocation.get("queued_event_id"),
        "context": invocation.get("context"),
        "planning_backend_override": invocation.get("planning_backend_override"),
        "planning_escalation_metadata": invocation.get("planning_escalation_metadata"),
    }
    if continuation:
        args.update(
            {
                "continuation_task_id": continuation.get("task_id"),
                "continuation_kind": continuation.get("kind"),
                "continuation_retry_count": continuation.get("retry_count"),
            }
        )
    return args, None


def _ensure_task_execution_dispatch_ready(
    db: Session, task_execution_id: int
) -> tuple[bool, str | None]:
    execution = (
        db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
    )
    if execution is None or execution.dispatch_state not in {
        DISPATCH_PENDING_BINDING,
        DISPATCH_READY,
    }:
        return False, "dispatch_not_pending"
    if execution.dispatch_state == DISPATCH_READY:
        _, reason = _dispatch_replay_arguments(db, execution)
        if reason:
            _refuse_task_execution_dispatch(db, execution.id, reason)
            return False, reason
        try:
            validate_existing_dispatch_binding(
                db,
                task_execution=execution,
                session_instance_id=execution.dispatch_session_instance_id,
            )
        except WorkflowLifecycleError as exc:
            _refuse_task_execution_dispatch(db, execution.id, exc.reason)
            return False, exc.reason
        return True, None

    _, replay_error = _dispatch_replay_arguments(db, execution)
    if replay_error:
        _refuse_task_execution_dispatch(db, execution.id, replay_error)
        return False, replay_error
    try:
        # Release the ORM read transaction before R3A takes its independent
        # BEGIN IMMEDIATE connection. The R3A service remains the sole owner
        # of that write transaction.
        db.rollback()
        if enforcement_enabled():
            if execution.dispatch_kind == "ROOT":
                outcome = bind_new_root(
                    db,
                    execution.id,
                    expected_session_instance_id=execution.dispatch_session_instance_id,
                )
                if outcome is BudgetOutcome.COMMIT_UNCERTAIN:
                    _defer_task_execution_dispatch(
                        db, execution.id, "workflow_binding_commit_uncertain"
                    )
                    return False, "workflow_binding_commit_uncertain"
                require_binding_outcome(outcome, binding_kind="ROOT")
            else:
                kind = (
                    BindingKind.RETRY
                    if execution.dispatch_kind == "RETRY"
                    else BindingKind.RECOVERY
                )
                outcome = bind_continuation(
                    db,
                    execution.id,
                    parent_task_execution_id=execution.dispatch_parent_task_execution_id,
                    binding_kind=kind,
                    expected_session_instance_id=execution.dispatch_session_instance_id,
                )
                if outcome is BudgetOutcome.COMMIT_UNCERTAIN:
                    _defer_task_execution_dispatch(
                        db, execution.id, "workflow_binding_commit_uncertain"
                    )
                    return False, "workflow_binding_commit_uncertain"
                require_binding_outcome(outcome, binding_kind=kind.value)
        from app.services.orchestration.lifecycle.transitions import (
            begin_atomic_claim_transaction,
        )

        begin_atomic_claim_transaction(db)
        db.expire_all()
        execution = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.id == task_execution_id,
                TaskExecution.status == TaskStatus.PENDING,
                TaskExecution.dispatch_state == DISPATCH_PENDING_BINDING,
            )
            .first()
        )
        if execution is None:
            return False, "attempt_changed_before_ready"
        _, replay_error = _dispatch_replay_arguments(db, execution)
        if replay_error:
            _refuse_task_execution_dispatch(db, execution.id, replay_error)
            return False, replay_error
        validate_existing_dispatch_binding(
            db,
            task_execution=execution,
            session_instance_id=execution.dispatch_session_instance_id,
        )
        execution.dispatch_state = DISPATCH_READY
        execution.dispatch_next_attempt_at = _dispatch_now()
        db.commit()
        return True, None
    except WorkflowLifecycleError as exc:
        db.rollback()
        _refuse_task_execution_dispatch(db, task_execution_id, exc.reason)
        return False, exc.reason
    except Exception as exc:
        db.rollback()
        # Preserve PENDING_BINDING on transient/schema errors. The next
        # maintenance pass retries the same TE and idempotent R3A operation.
        logger.warning(
            "[TASK_DISPATCH] binding/reconciliation failed task_execution_id=%s error=%s",
            task_execution_id,
            exc.__class__.__name__,
        )
        _defer_task_execution_dispatch(
            db,
            task_execution_id,
            f"binding_reconciliation_error:{exc.__class__.__name__}",
        )
        return False, "binding_reconciliation_error"


def _dispatch_retry_delay(attempt: int) -> timedelta:
    seconds = min(
        _DISPATCH_RETRY_BASE_SECONDS * (2 ** min(max(attempt - 1, 0), 4)),
        _DISPATCH_RETRY_MAX_SECONDS,
    )
    return timedelta(seconds=seconds)


def _publish_one_task_execution_dispatch(
    db: Session,
    task_execution_id: int,
    *,
    publisher: Any = None,
) -> dict[str, Any]:
    ready, reason = _ensure_task_execution_dispatch_ready(db, task_execution_id)
    if not ready:
        execution = (
            db.query(TaskExecution)
            .filter(TaskExecution.id == task_execution_id)
            .first()
        )
        state = (
            "refused"
            if execution is not None and execution.dispatch_state == DISPATCH_REFUSED
            else "not_ready"
        )
        db.rollback()
        return {
            "task_execution_id": task_execution_id,
            "state": state,
            "reason": reason,
        }

    if publisher is None:
        return {
            "task_execution_id": task_execution_id,
            "state": "publisher_unconfigured",
            "reason": "dispatch_publisher_not_supplied",
        }

    from app.services.orchestration.lifecycle.transitions import (
        begin_atomic_claim_transaction,
    )

    begin_atomic_claim_transaction(db)
    now = _dispatch_now()
    lease_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
    execution = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.id == task_execution_id,
            TaskExecution.status == TaskStatus.PENDING,
            TaskExecution.dispatch_state == DISPATCH_READY,
            or_(
                TaskExecution.dispatch_next_attempt_at.is_(None),
                TaskExecution.dispatch_next_attempt_at <= now,
            ),
            or_(
                TaskExecution.dispatch_lease_owner.is_(None),
                TaskExecution.dispatch_lease_expires_at <= now,
            ),
        )
        .first()
    )
    if execution is None:
        db.rollback()
        return {"task_execution_id": task_execution_id, "state": "not_due"}
    previous_fence = execution.dispatch_lease_fence or 0
    updated = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.id == task_execution_id,
            TaskExecution.status == TaskStatus.PENDING,
            TaskExecution.dispatch_state == DISPATCH_READY,
            TaskExecution.dispatch_lease_fence == previous_fence,
            or_(
                TaskExecution.dispatch_lease_owner.is_(None),
                TaskExecution.dispatch_lease_expires_at <= now,
            ),
        )
        .update(
            {
                TaskExecution.dispatch_lease_owner: lease_owner,
                TaskExecution.dispatch_lease_fence: previous_fence + 1,
                TaskExecution.dispatch_lease_expires_at: now
                + timedelta(seconds=_DISPATCH_LEASE_SECONDS),
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        return {"task_execution_id": task_execution_id, "state": "lease_contended"}
    fence = previous_fence + 1
    execution.dispatch_publish_attempts = (execution.dispatch_publish_attempts or 0) + 1
    attempt = execution.dispatch_publish_attempts
    db.commit()

    db.expire_all()
    execution = (
        db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
    )
    args, replay_error = _dispatch_replay_arguments(db, execution)
    if replay_error:
        _refuse_task_execution_dispatch(db, task_execution_id, replay_error)
        return {
            "task_execution_id": task_execution_id,
            "state": "refused",
            "reason": replay_error,
        }
    try:
        publisher(
            kwargs=args,
            task_id=execution.dispatch_celery_task_id,
        )
        outcome = "published_unclaimed"
        error = None
    except Exception as exc:
        outcome = "publish_error"
        error = f"publisher_error:{exc.__class__.__name__}"

    # The broker call is outside SQLite. Fencing prevents an expired publisher
    # from overwriting a newer lease or the Worker claim.
    db.expire_all()
    current = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.id == task_execution_id,
            TaskExecution.dispatch_state == DISPATCH_READY,
            TaskExecution.dispatch_lease_owner == lease_owner,
            TaskExecution.dispatch_lease_fence == fence,
        )
        .first()
    )
    if current is not None:
        current.dispatch_last_error = error
        current.dispatch_next_attempt_at = now + _dispatch_retry_delay(attempt)
        current.dispatch_lease_owner = None
        current.dispatch_lease_expires_at = None
        db.commit()
    else:
        db.rollback()
    return {"task_execution_id": task_execution_id, "state": outcome, "fence": fence}


def reconcile_task_execution_dispatches(
    db: Session,
    *,
    limit: int = 50,
    task_execution_id: int | None = None,
    publisher: Any = None,
) -> dict[str, Any]:
    """Recover a bounded batch of exact pending attempts using durable identity."""

    if limit <= 0:
        return {"inspected_count": 0, "results": []}
    now = _dispatch_now()
    query = db.query(TaskExecution.id).filter(
        TaskExecution.status == TaskStatus.PENDING,
        TaskExecution.dispatch_state.in_([DISPATCH_PENDING_BINDING, DISPATCH_READY]),
        or_(
            TaskExecution.dispatch_next_attempt_at.is_(None),
            TaskExecution.dispatch_next_attempt_at <= now,
        ),
        or_(
            TaskExecution.dispatch_lease_owner.is_(None),
            TaskExecution.dispatch_lease_expires_at <= now,
        ),
    )
    if task_execution_id is not None:
        query = query.filter(TaskExecution.id == task_execution_id)
    ids = [row[0] for row in query.order_by(TaskExecution.id).limit(limit).all()]
    db.rollback()
    results = [
        _publish_one_task_execution_dispatch(db, execution_id, publisher=publisher)
        for execution_id in ids
    ]
    return {
        "inspected_count": len(ids),
        "published_count": sum(
            item["state"] == "published_unclaimed" for item in results
        ),
        "refused_count": sum(item["state"] == "refused" for item in results),
        "error_count": sum(
            item["state"] == "publish_error"
            or (item["state"] == "not_ready" and item.get("reason") is not None)
            for item in results
        ),
        "results": results,
    }


def publish_task_execution_dispatch(
    db: Session,
    task_execution_id: int,
    *,
    publisher: Any = None,
) -> dict[str, Any]:
    """Run the normal T2–T5 path for an already committed TaskExecution."""
    if task_execution_id is None:
        return {
            "task_execution_id": None,
            "state": "not_managed",
        }
    result = reconcile_task_execution_dispatches(
        db,
        limit=1,
        task_execution_id=task_execution_id,
        publisher=publisher,
    )
    if not result["results"]:
        return {"task_execution_id": task_execution_id, "state": "not_due"}
    return result["results"][0]


def publisher_from_delay(delay_function: Any) -> Any:
    """Adapt an injected Celery ``.delay`` hook for deterministic callers/tests."""

    task = getattr(delay_function, "__self__", None)
    apply_async = getattr(task, "apply_async", None)
    if callable(apply_async):
        return apply_async

    def _injected_publisher(*, kwargs: dict[str, Any], task_id: str):
        return delay_function(**kwargs)

    return _injected_publisher


def publisher_from_task(task: Any) -> Any:
    """Adapt a Celery task object or deterministic delay-only test double."""

    apply_async = getattr(task, "apply_async", None)
    if callable(apply_async):
        return apply_async
    delay = getattr(task, "delay", None)
    if callable(delay):
        return publisher_from_delay(delay)
    return None


def close_task_workflow(
    db: Session, task_execution_id: int | None, *, reason: str
) -> BudgetOutcome | None:
    """Close the attempt's workflow at an authoritative terminal boundary.

    Refused (``ATTEMPT_ACTIVE``) while any bound attempt is still pending or
    running. Closing never refunds reservations or removes evidence.
    """

    if not enforcement_enabled() or task_execution_id is None:
        return None
    try:
        workflow = resolve_task_execution_workflow(db, task_execution_id)
        if workflow is None:
            return None
        result = close_workflow(db, workflow.workflow_uuid, require_quiescent=True)
    except Exception as exc:
        logger.warning("[WORKFLOW_BUDGET] closure failed: %s", exc)
        return None
    _log_outcome(
        "close", result.outcome, task_execution_id=task_execution_id, reason=reason
    )
    return result.outcome


def close_session_workflows(
    db: Session, session_id: int | None, *, reason: str
) -> dict[str, BudgetOutcome]:
    """Close every OPEN Task workflow of a stopped/cancelled Session."""

    if not enforcement_enabled() or session_id is None:
        return {}
    outcomes: dict[str, BudgetOutcome] = {}
    try:
        for workflow_uuid in open_session_workflow_uuids(db, session_id):
            result = close_workflow(db, workflow_uuid, require_quiescent=True)
            outcomes[workflow_uuid] = result.outcome
            _log_outcome(
                "close_session",
                result.outcome,
                session_id=session_id,
                workflow_uuid=workflow_uuid,
                reason=reason,
            )
    except Exception as exc:
        logger.warning("[WORKFLOW_BUDGET] session closure failed: %s", exc)
    return outcomes


def require_dispatch_identity(
    db: Session,
    *,
    task_execution: Any,
    session_instance_id: str | None,
) -> WorkflowRuntimeIdentity | None:
    """Worker admission: the attempt must own an OPEN workflow of this generation.

    Disabled returns ``None`` with no database access. Enabled raises
    ``WorkflowLifecycleError`` instead of falling back to legacy mode.
    """

    if not enforcement_enabled():
        return None
    active_policy(WorkflowKind.TASK_EXECUTION)
    task_execution_id = getattr(task_execution, "id", None)
    if task_execution_id is None:
        raise WorkflowLifecycleError("attempt_missing")
    try:
        binding = resolve_task_execution_binding(db, task_execution_id)
        workflow = resolve_task_execution_workflow(db, task_execution_id)
    except Exception as exc:
        raise WorkflowLifecycleError("schema_unavailable") from exc
    if binding is None or workflow is None:
        raise WorkflowLifecycleError("attempt_unbound")
    if workflow.status != "OPEN":
        raise WorkflowLifecycleError("workflow_closed")
    if workflow.deadline_at <= trusted_utc_now():
        raise WorkflowLifecycleError("workflow_deadline_expired")
    if (
        binding.session_id != getattr(task_execution, "session_id", None)
        or binding.task_id != getattr(task_execution, "task_id", None)
        or binding.attempt_number != getattr(task_execution, "attempt_number", None)
    ):
        raise WorkflowLifecycleError("attempt_identity_mismatch")
    if not session_instance_id or binding.session_instance_id != session_instance_id:
        raise WorkflowLifecycleError("session_generation_mismatch")
    return WorkflowRuntimeIdentity(
        workflow_uuid=workflow.workflow_uuid,
        workflow_kind=workflow.workflow_kind,
        session_id=binding.session_id,
        session_instance_id=binding.session_instance_id,
        task_id=binding.task_id,
        task_execution_id=binding.task_execution_id,
    )


def validate_existing_dispatch_binding(
    db: Session,
    *,
    task_execution: Any,
    session_instance_id: str | None,
) -> WorkflowRuntimeIdentity | None:
    """Validate any existing R3A binding even when global enforcement is off.

    Disabled mode keeps unbound legacy execution compatible. A managed attempt
    that already owns a workflow may never use disabled mode to bypass a
    closed, expired, or generation-mismatched workflow.
    """

    if enforcement_enabled():
        return require_dispatch_identity(
            db,
            task_execution=task_execution,
            session_instance_id=session_instance_id,
        )
    task_execution_id = getattr(task_execution, "id", None)
    if task_execution_id is None:
        return None
    try:
        binding = resolve_task_execution_binding(db, task_execution_id)
        if binding is None:
            return None
        workflow = resolve_task_execution_workflow(db, task_execution_id)
    except WorkflowBudgetSchemaUnavailable as exc:
        # Enforcement-disabled operation retains the pre-R3A legacy path. The
        # R3A schema may be absent in offline/legacy databases, so absence of
        # that optional policy subsystem cannot block dispatch. When the
        # schema is available, any existing binding is still validated below.
        logger.debug(
            "[WORKFLOW_BUDGET] disabled binding lookup unavailable: %s",
            exc.__class__.__name__,
        )
        return None
    except Exception as exc:
        raise WorkflowLifecycleError("workflow_binding_unavailable") from exc
    if workflow is None or workflow.status != "OPEN":
        raise WorkflowLifecycleError("workflow_closed")
    if workflow.deadline_at <= trusted_utc_now():
        raise WorkflowLifecycleError("workflow_deadline_expired")
    if (
        binding.session_id != getattr(task_execution, "session_id", None)
        or binding.task_id != getattr(task_execution, "task_id", None)
        or binding.attempt_number != getattr(task_execution, "attempt_number", None)
        or not session_instance_id
        or binding.session_instance_id != session_instance_id
    ):
        raise WorkflowLifecycleError("session_generation_mismatch")
    return WorkflowRuntimeIdentity(
        workflow_uuid=workflow.workflow_uuid,
        workflow_kind=workflow.workflow_kind,
        session_id=binding.session_id,
        session_instance_id=binding.session_instance_id,
        task_id=binding.task_id,
        task_execution_id=binding.task_execution_id,
    )


def attach_workflow_identity(
    runtime: Any, identity: WorkflowRuntimeIdentity | None, role: Any
) -> None:
    """Give a runtime its role-scoped identity; legacy runtimes are untouched."""

    # Only a durable-row identity is attached; mocks or foreign objects are not.
    if not isinstance(identity, WorkflowRuntimeIdentity) or runtime is None:
        return
    role_name = getattr(role, "value", role)
    runtime.workflow_identity = replace(
        identity, role=str(role_name) if role_name else None
    )


def propagate_workflow_identity(parent: Any, child: Any, role: Any) -> None:
    """A derived runtime (repair, fallback) keeps its parent attempt's identity."""

    if child is None or child is parent:
        return
    attach_workflow_identity(child, getattr(parent, "workflow_identity", None), role)


# Interactive Planning runs synchronously under one claimed PlanningSession
# generation; the scope carries that identity into ``invoke_runtime_prompt``
# for both Protocol v1 synthesis and Protocol v2 stage providers.
_PLANNING_IDENTITY: ContextVar[WorkflowRuntimeIdentity | None] = ContextVar(
    "planning_workflow_identity", default=None
)


def current_planning_identity() -> WorkflowRuntimeIdentity | None:
    return _PLANNING_IDENTITY.get()


@contextmanager
def planning_identity_scope(
    identity: WorkflowRuntimeIdentity | None,
) -> Iterator[None]:
    token = _PLANNING_IDENTITY.set(identity)
    try:
        yield
    finally:
        _PLANNING_IDENTITY.reset(token)


def bind_planning_root(
    db: Session, planning_session_id: int | None
) -> BudgetOutcome | None:
    """A committed PlanningSession generation owns one PLANNING_SESSION workflow."""

    if not enforcement_enabled() or planning_session_id is None:
        return None
    try:
        policy = active_policy(WorkflowKind.PLANNING_SESSION)
        result = create_planning_session_workflow(
            db, policy, planning_session_id=planning_session_id
        )
    except Exception as exc:
        logger.warning("[WORKFLOW_BUDGET] planning root failed: %s", exc)
        return None
    _log_outcome(
        "bind_planning_root", result.outcome, planning_session_id=planning_session_id
    )
    return result.outcome


def require_planning_identity(
    db: Session, planning_session_id: int, generation_id: str | None
) -> WorkflowRuntimeIdentity | None:
    """Processing admission for one claimed PlanningSession generation."""

    if not enforcement_enabled():
        return None
    active_policy(WorkflowKind.PLANNING_SESSION)
    try:
        workflow = resolve_planning_session_workflow(db, planning_session_id)
    except Exception as exc:
        raise WorkflowLifecycleError("schema_unavailable") from exc
    if workflow is None:
        raise WorkflowLifecycleError("planning_unbound")
    if workflow.status != "OPEN":
        raise WorkflowLifecycleError("workflow_closed")
    if workflow.deadline_at <= trusted_utc_now():
        raise WorkflowLifecycleError("workflow_deadline_expired")
    current = (
        db.query(PlanningSession.generation_id)
        .filter(PlanningSession.id == planning_session_id)
        .scalar()
    )
    if not generation_id or current != generation_id:
        raise WorkflowLifecycleError("planning_generation_mismatch")
    return WorkflowRuntimeIdentity(
        workflow_uuid=workflow.workflow_uuid,
        workflow_kind=workflow.workflow_kind,
        planning_session_id=planning_session_id,
        planning_generation_id=generation_id,
    )


def close_planning_workflow(
    db: Session, planning_session_id: int | None, *, reason: str
) -> None:
    """Close the current generation's workflow at a terminal Planning boundary."""

    if not enforcement_enabled() or planning_session_id is None:
        return
    try:
        workflow = resolve_planning_session_workflow(db, planning_session_id)
        if workflow is None:
            return
        result = close_workflow(db, workflow.workflow_uuid)
    except Exception as exc:
        logger.warning("[WORKFLOW_BUDGET] planning closure failed: %s", exc)
        return
    _log_outcome(
        "close_planning",
        result.outcome,
        planning_session_id=planning_session_id,
        reason=reason,
    )
