"""Phase 29B-2 authenticated Planning-to-Execution commit-boundary API.

This is an operator command that releases one exact operator-approved
Protocol v2 Structured Task Plan from Planning authority into Execution
authority.  It does not dispatch work; see
``app.services.planning.execution_commit`` for the full contract.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import PlanningSession, Project, User
from app.schemas.planning_execution_commit import (
    ExecutionCommitRequestPayload,
    ExecutionCommitResponse,
)
from app.services.auth.authorization import is_admin_user
from app.services.auth.rate_limit import enforce_api_rate_limit
from app.services.planning.execution_commit import (
    ExecutionCommitError,
    ExecutionCommitRequest,
    ExecutionCommitResult,
    PlanningExecutionCommitService,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/planning")

_HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

_ERROR_STATUS = {
    "session_not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "protocol_v2_required": _HTTP_422,
    "authority_stale": status.HTTP_412_PRECONDITION_FAILED,
    "task_plan_not_approved": _HTTP_422,
    "approval_integrity_failure": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "completion_manifest_pending": status.HTTP_409_CONFLICT,
    "completion_manifest_missing": _HTTP_422,
    "completion_manifest_inconsistent": _HTTP_422,
    "commit_manifest_conflict": status.HTTP_409_CONFLICT,
    "idempotency_key_conflict": status.HTTP_409_CONFLICT,
    "integrity_failure": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "validation_contract_missing": _HTTP_422,
    "validation_contract_schema_unsupported": _HTTP_422,
    "validation_predicate_unsupported": _HTTP_422,
    "validation_predicate_version_unsupported": _HTTP_422,
    "validation_contract_parameters_invalid": _HTTP_422,
    "validation_evidence_descriptor_invalid": _HTTP_422,
    "validation_pass_policy_invalid": _HTTP_422,
    "validation_review_requirement_invalid": _HTTP_422,
    "validation_environment_identity_invalid": _HTTP_422,
    "validation_contract_hash_mismatch": status.HTTP_409_CONFLICT,
    "validation_contract_release_conflict": status.HTTP_409_CONFLICT,
    "validation_contract_idempotency_conflict": status.HTTP_409_CONFLICT,
    "validation_contract_integrity_failure": status.HTTP_500_INTERNAL_SERVER_ERROR,
}

_SAFE_MESSAGE = {
    "approval_integrity_failure": "Review integrity could not be verified",
    "integrity_failure": "Execution commit integrity could not be verified",
    "session_not_found": "Planning session not found",
    "forbidden": "Execution commit is not authorized",
}


def _problem(code: str, message: str | None = None) -> HTTPException:
    safe_message = _SAFE_MESSAGE.get(
        code, message or "Execution commit could not be completed"
    )
    return HTTPException(
        status_code=_ERROR_STATUS.get(code, _HTTP_422),
        detail={"code": code, "message": safe_message},
    )


def _audit(
    action: str,
    *,
    session_id: int,
    project_id: int,
    current_user: User,
    result: str,
    error_code: str | None = None,
    execution_plan_id: int | None = None,
) -> None:
    logger.info(
        "execution_commit_audit",
        extra={
            "audit_event": "execution_commit_" + action,
            "session_id": session_id,
            "project_id": project_id,
            "operator_subject": current_user.email,
            "result": result,
            "error_code": error_code,
            "execution_plan_id": execution_plan_id,
        },
    )


def _context(
    request: Request, session_id: int, db: Session, current_user: User
) -> tuple[PlanningSession, Project]:
    session = (
        db.query(PlanningSession)
        .join(Project, Project.id == PlanningSession.project_id)
        .filter(
            PlanningSession.id == int(session_id),
            Project.deleted_at.is_(None),
        )
        .one_or_none()
    )
    if session is None or session.protocol_version != "v2":
        raise _problem("session_not_found")
    project = db.get(Project, session.project_id)
    if project is None or project.deleted_at is not None:
        raise _problem("session_not_found")
    if not is_admin_user(current_user) and project.user_id != current_user.id:
        _audit(
            "authorization_denial",
            session_id=session.id,
            project_id=project.id,
            current_user=current_user,
            result="denied",
            error_code="forbidden",
        )
        raise _problem("forbidden")
    enforce_api_rate_limit(
        request,
        "execution_commit",
        current_user=current_user,
        scope_id=str(project.id),
    )
    return session, project


def _response(result: ExecutionCommitResult) -> ExecutionCommitResponse:
    return ExecutionCommitResponse(
        planning_session_id=result.planning_session_id,
        session_generation_id=result.session_generation_id,
        structured_task_plan_checkpoint_id=result.structured_task_plan_checkpoint_id,
        structured_task_plan_hash=result.structured_task_plan_hash,
        review_id=result.review_id,
        approval_event_id=result.approval_event_id,
        completion_manifest_id=result.completion_manifest_id,
        completion_manifest_hash=result.completion_manifest_hash,
        planning_commit_manifest_id=result.planning_commit_manifest_id,
        commit_identity=result.commit_identity,
        boundary_state=result.boundary_state,
        idempotent_replay=result.idempotent_replay,
        integrity_status=result.integrity_status,
        execution_plan_id=result.execution_plan_id,
        execution_plan_generation=result.execution_plan_generation,
        execution_plan_status=result.execution_plan_status,
        task_count=result.task_count,
        dependency_edge_count=result.dependency_edge_count,
        group_count=result.group_count,
        group_membership_count=result.group_membership_count,
        retryable=result.retryable,
        execution_error_code=result.execution_error_code,
    )


@router.post(
    "/sessions/{session_id}/execution-commit",
    response_model=ExecutionCommitResponse,
    summary="Release one operator-approved Structured Task Plan into Execution authority",
)
def commit_execution(
    session_id: int,
    payload: ExecutionCommitRequestPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    session, project = _context(request, session_id, db, current_user)
    service = PlanningExecutionCommitService(db)
    try:
        result = service.commit(
            session.id,
            ExecutionCommitRequest(
                idempotency_key=payload.idempotency_key,
                operator_subject=current_user.email,
                structured_task_plan_checkpoint_id=payload.structured_task_plan_checkpoint_id,
                structured_task_plan_hash=payload.structured_task_plan_hash,
                expected_session_generation_id=payload.expected_session_generation_id,
                expected_review_id=payload.expected_review_id,
                expected_approval_event_id=payload.expected_approval_event_id,
            ),
        )
    except ExecutionCommitError as exc:
        _audit(
            "commit",
            session_id=session.id,
            project_id=project.id,
            current_user=current_user,
            result="failed",
            error_code=exc.code,
        )
        raise _problem(exc.code, exc.message) from exc
    _audit(
        "commit",
        session_id=session.id,
        project_id=project.id,
        current_user=current_user,
        result="replayed" if result.idempotent_replay else "success",
        execution_plan_id=result.execution_plan_id,
    )
    if result.boundary_state == "released_execution_pending":
        response.status_code = status.HTTP_202_ACCEPTED
    return _response(result)


__all__ = ["router"]
