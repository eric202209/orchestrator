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
import logging
from typing import Any, Iterator

from sqlalchemy.orm import Session

from app.config import settings
from app.models import PlanningSession
from app.services.execution.workflow_budget import (
    BindingKind,
    BudgetOutcome,
    EnforcementMode,
    InvalidBudgetPolicy,
    WorkflowBudgetPolicy,
    WorkflowKind,
    bind_task_execution_attempt,
    close_workflow,
    create_planning_session_workflow,
    create_task_execution_workflow,
    open_session_workflow_uuids,
    resolve_planning_session_workflow,
    resolve_task_execution_binding,
    resolve_task_execution_workflow,
    validate_budget_policy,
)

logger = logging.getLogger(__name__)

WORKFLOW_IDENTITY_INVALID = "workflow_identity_invalid"


class WorkflowLifecycleError(RuntimeError):
    """Enforced workflow identity is missing, invalid, closed, or mismatched."""

    failure_category = WORKFLOW_IDENTITY_INVALID

    def __init__(self, reason: str):
        super().__init__(f"{WORKFLOW_IDENTITY_INVALID}: {reason}")
        self.reason = reason


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


def bind_new_root(db: Session, task_execution_id: int | None) -> BudgetOutcome | None:
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
        )
    except Exception as exc:  # never break the legacy caller; Worker fails closed
        logger.warning("[WORKFLOW_BUDGET] root binding failed: %s", exc)
        return None
    _log_outcome("bind_root", result.outcome, task_execution_id=task_execution_id)
    return result.outcome


def bind_continuation(
    db: Session,
    task_execution_id: int | None,
    *,
    parent_task_execution_id: int | None,
    binding_kind: BindingKind,
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
        )
    except Exception as exc:
        logger.warning("[WORKFLOW_BUDGET] continuation binding failed: %s", exc)
        return None
    _log_outcome(
        "bind_continuation",
        result.outcome,
        task_execution_id=task_execution_id,
        parent_task_execution_id=parent_task_execution_id,
    )
    return result.outcome


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


def bind_planning_root(db: Session, planning_session_id: int | None) -> None:
    """A committed PlanningSession generation owns one PLANNING_SESSION workflow."""

    if not enforcement_enabled() or planning_session_id is None:
        return
    try:
        policy = active_policy(WorkflowKind.PLANNING_SESSION)
        result = create_planning_session_workflow(
            db, policy, planning_session_id=planning_session_id
        )
    except Exception as exc:
        logger.warning("[WORKFLOW_BUDGET] planning root failed: %s", exc)
        return
    _log_outcome(
        "bind_planning_root", result.outcome, planning_session_id=planning_session_id
    )


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
