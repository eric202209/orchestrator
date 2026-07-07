"""Sessions API endpoints for orchestration runtimes."""

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
    WebSocket,
)
from fastapi.requests import Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from typing import List, Optional, Any, Dict, Union
from datetime import datetime, timezone
import json
import logging
import re
import uuid
from app.database import get_db

logger = logging.getLogger(__name__)
from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskStatus,
)
from app.schemas import (
    SessionCreate,
    SessionUpdate,
    SessionResponse,
    TaskExecuteRequest,
)
from app.schemas.pagination import paginate
from app.services import (
    PromptTemplates,
    add_operator_guidance as _add_operator_guidance,
    approve_intervention as _approve_intervention,
    create_intervention_request as _create_intervention_request,
    deny_intervention as _deny_intervention,
    enrich_failure_summary_with_llm as _enrich_failure_summary_with_llm,
    get_intervention_history as _get_intervention_history,
    get_pending_interventions as _get_pending_interventions,
    request_human_intervention_lifecycle as _request_human_intervention_lifecycle,
    submit_intervention_reply as _submit_intervention_reply,
    get_or_generate_failure_summary as _get_or_generate_failure_summary,
    store_operator_feedback as _store_operator_feedback,
    trigger_replan as _trigger_replan,
    start_agent_session_payload as _start_agent_session_payload,
    check_session_overwrites_payload as _check_session_overwrites_payload,
    cleanup_orphaned_checkpoints_payload as _cleanup_orphaned_checkpoints_payload,
    cleanup_session_checkpoints_payload as _cleanup_session_checkpoints_payload,
    create_session_backup_payload as _create_session_backup_payload,
    delete_session_checkpoint_payload as _delete_session_checkpoint_payload,
    ensure_unique_session_name as _ensure_unique_session_name,
    get_session_logs_payload as _get_session_logs_payload,
    get_session_execution_dag_payload as _get_session_execution_dag_payload,
    get_session_divergence_compare_payload as _get_session_divergence_compare_payload,
    get_session_dispatch_watchdog_payload as _get_session_dispatch_watchdog_payload,
    get_session_reconciliation_audit_payload as _get_session_reconciliation_audit_payload,
    get_session_focus_mode_payload as _get_session_focus_mode_payload,
    get_session_mobile_interruptions_payload as _get_session_mobile_interruptions_payload,
    get_session_timeline_payload as _get_session_timeline_payload,
    get_session_recovery_context_payload as _get_session_recovery_context_payload,
    get_session_digest_payload as _get_session_digest_payload,
    get_session_state_diff_payload as _get_session_state_diff_payload,
    get_session_trace_export_payload as _get_session_trace_export_payload,
    get_session_workspace_info_payload as _get_session_workspace_info_payload,
    get_session_statistics_payload as _get_session_statistics_payload,
    get_sorted_logs_payload as _get_sorted_logs_payload,
    get_tool_execution_history_payload as _get_tool_execution_history_payload,
    inspect_session_checkpoint_payload as _inspect_session_checkpoint_payload,
    list_session_checkpoints_payload as _list_session_checkpoints_payload,
    load_session_checkpoint_payload as _load_session_checkpoint_payload,
    maybe_queue_next_automatic_task as _maybe_queue_next_automatic_task,
    queue_task_for_session as _queue_task_for_session,
    retry_session_with_stronger_planning_lane as _retry_session_with_stronger_planning_lane,
    replay_session_checkpoint_payload as _replay_session_checkpoint_payload,
    replay_session_checkpoint_counterfactual_payload as _replay_session_checkpoint_counterfactual_payload,
    refresh_session_dispatch_watchdog_alert as _refresh_session_dispatch_watchdog_alert,
    save_session_checkpoint_payload as _save_session_checkpoint_payload,
    pause_session_lifecycle as _pause_session_lifecycle,
    resume_session_lifecycle as _resume_session_lifecycle,
    start_session_lifecycle as _start_session_lifecycle,
    stop_session_lifecycle as _stop_session_lifecycle,
    stream_session_logs as _stream_session_logs,
    stream_session_status as _stream_session_status,
    track_tool_execution_payload as _track_tool_execution_payload,
    execute_task_payload as _execute_task_payload,
)
from app.services.session.session_inspection_service import (
    derive_orchestration_state_block as _derive_orchestration_state_block,
)
from app.services.session.session_query_service import (
    apply_session_filters as _apply_session_filters,
    apply_session_ordering as _apply_session_ordering,
    build_replay_boundary as _build_replay_boundary,
    get_session_intervention_or_404 as _get_session_intervention_or_404,
    json_payload_size as _json_payload_size,
    latest_failure_diagnostics as _latest_failure_diagnostics,
    latest_session_task_link as _latest_session_task_link,
    read_task_events_from_candidates as _read_task_events_from_candidates,
    require_session_access as _require_session_access,
    resolve_replay_task_id as _resolve_replay_task_id,
    serialize_failure_summary as _serialize_failure_summary,
    serialize_intervention as _serialize_intervention,
    serialize_session_timestamp as _serialize_session_timestamp,
    truncate_replay_report as _truncate_replay_report,
    MAX_TOOL_TRACK_JSON_CHARS,
)
from app.services.project.name_formatter import humanize_display_name
from app.services.auth.rate_limit import enforce_api_rate_limit
from app.services.orchestration.reporting.decision_timeline import (
    DEFAULT_TIMELINE_LIMIT,
    get_session_decision_timeline_payload,
)
from app.services.orchestration.reporting.replay import reconstruct_execution_state
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.system_settings import model_lane_snapshot
from app.services.session.session_lifecycle_service import (
    reconcile_terminal_running_sessions,
)
from app.services.orchestration.state.session_state import (
    mark_session_deleted,
    mark_session_pending,
)


from app.services.orchestration import is_known_event_type
from app.dependencies import get_current_active_user, get_current_user
from app.services.auth.authorization import (
    get_project_for_user,
    project_access_filter,
)

router = APIRouter()

DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS = 1800


@router.post(
    "/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED
)
def create_session(
    session: SessionCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Create a new orchestration session."""
    # Verify project exists and is accessible to this user.
    get_project_for_user(db, session.project_id, current_user)

    session_data = session.model_dump()
    session_data["name"] = _ensure_unique_session_name(
        db,
        session.project_id,
        humanize_display_name(session_data.get("name") or "session"),
    )
    db_session = SessionModel(**session_data)
    mark_session_pending(db_session)
    db_session.instance_id = str(
        uuid.uuid4()
    )  # Generate unique instance ID immediately
    lane = model_lane_snapshot(db)
    db_session.model_lane_label = lane.get("label")
    db_session.model_lane_metadata = lane
    db.add(db_session)
    db.flush()

    # Log session creation (single commit with session)
    db.add(
        LogEntry(
            session_id=db_session.id,
            level="INFO",
            message=f"Session created: {db_session.name}",
            log_metadata=json.dumps({"project_id": session.project_id}),
        )
    )
    db.commit()
    db.refresh(db_session)

    return db_session


@router.get("/sessions")
def list_sessions(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    # Legacy skip/limit — preserved until Phase 15E-4
    # TODO(Phase15E-4): remove legacy skip/limit mode
    skip: int = 0,
    limit: int = 100,
    # Paginated mode — activated when page is provided
    page: Optional[int] = None,
    per_page: int = 25,
    # Filters
    status: Optional[str] = None,
    needs_attention: Optional[bool] = None,
    is_active: Optional[bool] = None,
    project_id: Optional[int] = None,
    search: Optional[str] = None,
    created_before: Optional[str] = None,
    created_after: Optional[str] = None,
    # Ordering
    order_by: str = "created_at",
    order_dir: str = "desc",
):
    """List sessions across projects.

    Legacy mode (no page param): returns List[SessionResponse] with skip/limit.
    Paginated mode (page param present): returns Page[SessionResponse].
    """
    if page is not None and page < 1:
        raise HTTPException(status_code=422, detail="page must be >= 1")
    if per_page < 1 or per_page > 200:
        raise HTTPException(
            status_code=422, detail="per_page must be between 1 and 200"
        )

    query = (
        db.query(SessionModel)
        .join(Project, Project.id == SessionModel.project_id)
        .filter(
            SessionModel.deleted_at.is_(None),
            Project.deleted_at.is_(None),
            project_access_filter(db, current_user),
        )
    )
    query = _apply_session_filters(
        query,
        status=status,
        needs_attention=needs_attention,
        project_id=project_id,
        search=search,
        created_before=created_before,
        created_after=created_after,
        is_active=is_active,
    )

    if page is None:
        # Legacy mode — preserve original skip/limit behaviour
        sessions = (
            query.order_by(SessionModel.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        reconcile_terminal_running_sessions(db, sessions)
        return [SessionResponse.model_validate(s) for s in sessions]

    # Paginated mode
    query = _apply_session_ordering(query, order_by=order_by, order_dir=order_dir)
    page_data = paginate(query, page, per_page)
    reconcile_terminal_running_sessions(db, page_data["items"])
    page_data["items"] = [SessionResponse.model_validate(s) for s in page_data["items"]]
    return page_data


class StuckSessionInfo(BaseModel):
    session_id: int
    session_name: Optional[str] = None
    project_id: Optional[int] = None
    stalled_minutes: float
    last_task_id: Optional[int] = None


@router.get("/sessions/stuck", response_model=List[StuckSessionInfo])
def list_stuck_sessions(
    stalled_minutes: int = Query(default=10, ge=1, le=1440),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return active running sessions with no task execution progress in the last N minutes."""
    from sqlalchemy import not_, exists as sa_exists
    from datetime import timedelta

    threshold = datetime.now(timezone.utc) - timedelta(minutes=stalled_minutes)
    threshold_naive = threshold.replace(tzinfo=None)

    candidates = (
        db.query(SessionModel)
        .join(Project, Project.id == SessionModel.project_id)
        .filter(
            SessionModel.is_active.is_(True),
            SessionModel.status == "running",
            SessionModel.deleted_at.is_(None),
            Project.deleted_at.is_(None),
            project_access_filter(db, current_user),
            not_(
                sa_exists().where(
                    TaskExecution.session_id == SessionModel.id,
                    TaskExecution.status == TaskStatus.RUNNING,
                )
            ),
        )
        .all()
    )

    result = []
    for s in candidates:
        last_progress = s.updated_at or s.started_at or s.created_at
        if last_progress is None:
            continue
        lp_naive = (
            last_progress.replace(tzinfo=None)
            if last_progress.tzinfo is not None
            else last_progress
        )
        if lp_naive > threshold_naive:
            continue
        delta_minutes = (
            datetime.now(timezone.utc).replace(tzinfo=None) - lp_naive
        ).total_seconds() / 60

        last_task_id: Optional[int] = None
        latest_link = (
            db.query(SessionTask)
            .filter(SessionTask.session_id == s.id)
            .order_by(SessionTask.id.desc())
            .first()
        )
        if latest_link:
            last_task_id = latest_link.task_id

        result.append(
            StuckSessionInfo(
                session_id=s.id,
                session_name=getattr(s, "name", None),
                project_id=s.project_id,
                stalled_minutes=round(delta_minutes, 1),
                last_task_id=last_task_id,
            )
        )
    return result


@router.get("/projects/{project_id}/sessions")
def get_project_sessions(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    # Legacy — TODO(Phase15E-4): remove legacy skip/limit mode
    skip: int = 0,
    limit: int = 100,
    # Paginated mode
    page: Optional[int] = None,
    per_page: int = 25,
    # Filters
    status: Optional[str] = None,
    needs_attention: Optional[bool] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
    created_before: Optional[str] = None,
    created_after: Optional[str] = None,
    # Ordering
    order_by: str = "created_at",
    order_dir: str = "desc",
):
    """Get sessions for a project.

    Legacy mode (no page): returns List[SessionResponse].
    Paginated mode (page param): returns Page[SessionResponse].
    """
    if page is not None and page < 1:
        raise HTTPException(status_code=422, detail="page must be >= 1")
    if per_page < 1 or per_page > 200:
        raise HTTPException(
            status_code=422, detail="per_page must be between 1 and 200"
        )

    get_project_for_user(db, project_id, current_user)

    query = db.query(SessionModel).filter(
        SessionModel.project_id == project_id,
        SessionModel.deleted_at.is_(None),
    )
    query = _apply_session_filters(
        query,
        status=status,
        needs_attention=needs_attention,
        project_id=None,  # already scoped via URL param
        search=search,
        created_before=created_before,
        created_after=created_after,
        is_active=is_active,
    )

    if page is None:
        # Legacy mode
        sessions = (
            query.order_by(SessionModel.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        reconcile_terminal_running_sessions(db, sessions)
        return [SessionResponse.model_validate(s) for s in sessions]

    # Paginated mode
    query = _apply_session_ordering(query, order_by=order_by, order_dir=order_dir)
    page_data = paginate(query, page, per_page)
    reconcile_terminal_running_sessions(db, page_data["items"])
    page_data["items"] = [SessionResponse.model_validate(s) for s in page_data["items"]]
    return page_data


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get a specific session with detailed information"""
    session = _require_session_access(db, session_id, current_user)
    reconcile_terminal_running_sessions(db, [session])
    db.refresh(session)

    # Get associated logs
    log_count = db.query(LogEntry).filter(LogEntry.session_id == session_id).count()

    # Get associated tasks
    from app.models import SessionTask as SessionTaskModel

    session_tasks = (
        db.query(SessionTaskModel)
        .filter(SessionTaskModel.session_id == session_id)
        .all()
    )

    # Derive failure_category from the latest task execution for this session.
    latest_execution = (
        db.query(TaskExecution)
        .filter(TaskExecution.session_id == session_id)
        .order_by(TaskExecution.id.desc())
        .first()
    )
    failure_category = (
        latest_execution.failure_category if latest_execution is not None else None
    )

    # Add metadata
    response = session
    response.log_count = log_count
    response.task_count = len(session_tasks)
    response.failure_category = failure_category
    response.orchestration_state = _derive_orchestration_state_block(
        db, session, latest_task_execution=latest_execution
    )

    return response


@router.patch("/sessions/{session_id}", response_model=SessionResponse)
def update_session(
    session_id: int,
    session_update: SessionUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update a session"""
    db_session = _require_session_access(db, session_id, current_user)

    update_data = session_update.model_dump(exclude_unset=True)
    if "execution_mode" in update_data and update_data["execution_mode"] not in {
        "automatic",
        "manual",
    }:
        raise HTTPException(
            status_code=400, detail="execution_mode must be 'automatic' or 'manual'"
        )
    for field, value in update_data.items():
        setattr(db_session, field, value)

    db.commit()
    db.refresh(db_session)

    # Log update
    db.add(
        LogEntry(
            session_id=session_id,
            level="INFO",
            message=f"Session updated: {session_id}",
            log_metadata=json.dumps({"updates": update_data}),
        )
    )
    db.commit()

    return db_session


@router.post("/sessions/{session_id}/refresh-tasks")
def refresh_session_tasks(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Refresh a session against the latest project task list and queue the next task if applicable."""
    from app.services.tasks.service import TaskService

    session = _require_session_access(db, session_id, current_user)

    if not session.project_id:
        raise HTTPException(
            status_code=400, detail="Session is not linked to a project"
        )

    task_service = TaskService(db)
    ordered_tasks = task_service.get_project_tasks(session.project_id)
    counts = {
        "total": len(ordered_tasks),
        "pending": len(
            [task for task in ordered_tasks if task.status == TaskStatus.PENDING]
        ),
        "running": len(
            [task for task in ordered_tasks if task.status == TaskStatus.RUNNING]
        ),
        "done": len([task for task in ordered_tasks if task.status == TaskStatus.DONE]),
        "failed": len(
            [task for task in ordered_tasks if task.status == TaskStatus.FAILED]
        ),
    }

    queued_task = None
    if session.status == "running" and session.execution_mode == "automatic":
        queued_task = _maybe_queue_next_automatic_task(db, session)

    db.add(
        LogEntry(
            session_id=session.id,
            session_instance_id=session.instance_id,
            level="INFO",
            message="Session tasks refreshed from project state",
            log_metadata=json.dumps(
                {
                    "execution_mode": session.execution_mode,
                    "counts": counts,
                    "queued_task": queued_task,
                }
            ),
        )
    )
    db.commit()

    return {
        "session_id": session.id,
        "execution_mode": session.execution_mode,
        "counts": counts,
        "queued_task": queued_task,
    }


@router.post("/sessions/{session_id}/tasks/{task_id}/run")
def run_session_task(
    session_id: int,
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Queue a specific task for manual execution inside a session."""
    enforce_api_rate_limit(request, "task_run", current_user=current_user)
    session = _require_session_access(db, session_id, current_user)

    if not session.instance_id:
        session.instance_id = str(uuid.uuid4())
        db.commit()
        db.refresh(session)

    queued = _queue_task_for_session(db=db, session=session, task_id=task_id)
    return {
        "status": "queued",
        "session_id": session.id,
        "execution_mode": session.execution_mode,
        "queued_task": queued,
    }


@router.delete(
    "/sessions/{session_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Delete a session"""
    from app.services.workspace.checkpoint_service import CheckpointService

    logger.info("DELETE /sessions/%s - Starting deletion", session_id)

    db_session = _require_session_access(db, session_id, current_user)

    mark_session_deleted(db_session, deleted_at=datetime.now(timezone.utc))
    if "__deleted__" not in db_session.name:
        db_session.name = f"{db_session.name}__deleted__{db_session.id}"

    checkpoint_service = CheckpointService(db)
    deleted_checkpoints = checkpoint_service.delete_all_checkpoints(session_id)
    orphan_cleanup = checkpoint_service.cleanup_orphaned_checkpoints()

    deleted_session_tasks = (
        db.query(SessionTask).filter(SessionTask.session_id == session_id).delete()
    )
    deleted_task_checkpoints = (
        db.query(TaskCheckpoint)
        .filter(TaskCheckpoint.session_id == session_id)
        .delete(synchronize_session=False)
    )

    # Delete all logs for this session to prevent ID reuse issues
    deleted_logs = db.query(LogEntry).filter(LogEntry.session_id == session_id).delete()

    db.commit()
    logger.info("Deleted %s logs for session %s", deleted_logs, session_id)
    logger.info(
        "Deleted session %s artifacts: checkpoints=%s session_tasks=%s task_checkpoints=%s orphan_cleanup=%s",
        session_id,
        deleted_checkpoints,
        deleted_session_tasks,
        deleted_task_checkpoints,
        orphan_cleanup,
    )

    # Optional: Actually delete the session row if you want hard delete behavior
    # db.delete(db_session)
    # db.commit()

    logger.info("DELETE /sessions/%s - Session deleted successfully", session_id)
    return None


class StartSessionRequest(BaseModel):
    task_description: str


@router.post("/sessions/{session_id}/start-openclaw")
async def start_openclaw_session_compat(
    session_id: int,
    request: StartSessionRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Backward-compatible alias for older OpenClaw-specific clients."""
    _require_session_access(db, session_id, current_user)

    return await _start_agent_session_payload(
        db, session_id, task_description=request.task_description
    )


@router.post("/sessions/{session_id}/execute")
async def execute_task(
    session_id: int,
    task_request: TaskExecuteRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Execute a task via the active orchestration runtime."""
    _require_session_access(db, session_id, current_user)
    return await _execute_task_payload(db, session_id, task_request)


@router.websocket("/sessions/{session_id}/logs/stream")
async def websocket_log_stream(
    websocket: WebSocket, session_id: int, db: Session = Depends(get_db)
):
    """WebSocket endpoint for real-time session log streaming."""
    await _stream_session_logs(websocket, session_id, db)


@router.websocket("/sessions/{session_id}/status")
async def websocket_session_status(
    websocket: WebSocket, session_id: int, db: Session = Depends(get_db)
):
    await _stream_session_status(websocket, session_id, db)


@router.get("/sessions/{session_id}/tools")
def get_tool_execution_history(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    task_id: Optional[int] = None,
    limit: int = 50,
    tool_name: Optional[str] = None,
):
    """Get tool execution history for a session"""
    _require_session_access(db, session_id, current_user)
    return _get_tool_execution_history_payload(
        db, session_id, task_id=task_id, limit=limit, tool_name=tool_name
    )


@router.get("/sessions/{session_id}/statistics")
def get_session_statistics(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    days: int = 7,
):
    """Get statistics for a session"""
    _require_session_access(db, session_id, current_user)
    return _get_session_statistics_payload(db, session_id, days=days)


class ToolTrackRequest(BaseModel):
    execution_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    params: Dict[str, Any] = Field(default_factory=dict)
    result: Any
    success: bool
    task_id: Optional[int] = None
    session_instance_id: Optional[str] = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def enforce_payload_caps(self):
        if _json_payload_size(self.params) > MAX_TOOL_TRACK_JSON_CHARS:
            raise ValueError("params payload is too large")
        if _json_payload_size(self.result) > MAX_TOOL_TRACK_JSON_CHARS:
            raise ValueError("result payload is too large")
        return self


@router.post("/sessions/{session_id}/tools/track")
def track_tool_execution(
    session_id: int,
    body: ToolTrackRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Manually track a tool execution with instance tracking

    Args:
        session_id: Session ID
        execution_id: Unique execution identifier
        tool_name: Name of the tool
        params: Tool parameters
        result: Tool execution result
        success: Whether execution was successful
        db: Database session
        task_id: Optional task ID
        session_instance_id: Instance UUID for log isolation (NEW)

    Returns:
        Tracked execution result
    """
    _require_session_access(db, session_id, current_user)
    return _track_tool_execution_payload(
        db,
        session_id=session_id,
        execution_id=body.execution_id,
        tool_name=body.tool_name,
        params=body.params,
        result=body.result,
        success=body.success,
        task_id=body.task_id,
        session_instance_id=body.session_instance_id,
    )


@router.post("/sessions/{session_id}/start")
async def start_session_lifecycle_endpoint(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Start a session lifecycle and queue work when applicable."""
    enforce_api_rate_limit(request, "session_start", current_user=current_user)
    _require_session_access(db, session_id, current_user)
    return await _start_session_lifecycle(db, session_id)


@router.post("/sessions/{session_id}/stop")
async def stop_session(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    force: bool = False,
):
    """Stop a running session gracefully."""
    _require_session_access(db, session_id, current_user)
    initiated_by = (
        getattr(current_user, "email", None)
        or str(getattr(current_user, "id", None) or "")
        or "authenticated_user"
    )
    return await _stop_session_lifecycle(
        db,
        session_id,
        force=force,
        initiated_by=initiated_by,
        source=f"api:{request.method} {request.url.path}",
    )


@router.post("/sessions/{session_id}/pause")
async def pause_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Pause a running session

    Saves current state and pauses execution
    """
    _require_session_access(db, session_id, current_user)
    return await _pause_session_lifecycle(db, session_id)


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Resume a paused session

    Restores saved state and continues execution
    """
    _require_session_access(db, session_id, current_user)
    return await _resume_session_lifecycle(db, session_id)


class RequestInterventionBody(BaseModel):
    intervention_type: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=4000)
    task_id: Optional[int] = None
    context_snapshot: Optional[Dict[str, Any]] = None
    expires_in_minutes: int = Field(default=120, ge=1, le=10080)
    initiated_by: str = Field(default="human", min_length=1, max_length=64)


class InterventionReplyBody(BaseModel):
    reply: str


class OperatorGuidanceBody(BaseModel):
    guidance: str
    task_id: Optional[int] = None


class DenyInterventionBody(BaseModel):
    reason: Optional[str] = None


@router.post("/sessions/{session_id}/request-intervention")
async def request_human_intervention(
    session_id: int,
    body: RequestInterventionBody,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Pause execution and register a human-in-the-loop intervention request.

    Sets session.status = 'awaiting_input'. The session can be resumed
    once the operator submits a reply, approval, or denial.
    """
    _require_session_access(db, session_id, current_user)
    result = await _request_human_intervention_lifecycle(
        db,
        session_id,
        intervention_type=body.intervention_type,
        prompt=body.prompt,
        task_id=body.task_id,
        context_snapshot=body.context_snapshot,
        expires_in_minutes=body.expires_in_minutes,
        initiated_by=body.initiated_by,
    )
    req = _get_session_intervention_or_404(db, session_id, result["intervention_id"])
    payload = _serialize_intervention(req)
    payload["message"] = result["message"]
    return payload


@router.post("/sessions/{session_id}/operator-guidance")
def add_session_operator_guidance(
    session_id: int,
    body: OperatorGuidanceBody,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Add non-blocking operator guidance to the next agent prompt boundary.

    Unlike /request-intervention, this endpoint does not pause the session,
    revoke Celery tasks, or create a pending intervention request.
    """
    _require_session_access(db, session_id, current_user)
    return _add_operator_guidance(
        db,
        session_id=session_id,
        task_id=body.task_id,
        guidance=body.guidance,
        operator_id=getattr(current_user, "email", None)
        or getattr(current_user, "id", None),
    )


@router.get("/sessions/{session_id}/interventions")
def list_session_interventions(
    session_id: int,
    pending_only: bool = False,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List intervention requests for a session (all or pending-only)."""
    _require_session_access(db, session_id, current_user)

    if pending_only:
        items = _get_pending_interventions(db, session_id=session_id, limit=limit)
    else:
        items = _get_intervention_history(db, session_id=session_id, limit=limit)
    return {
        "session_id": session_id,
        "interventions": [_serialize_intervention(r) for r in items],
        "total": len(items),
    }


@router.post("/sessions/{session_id}/interventions/{intervention_id}/reply")
def reply_to_intervention(
    session_id: int,
    intervention_id: int,
    body: InterventionReplyBody,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Submit operator guidance/reply for a pending intervention.

    Records the reply, injects it into the latest checkpoint context, and
    transitions the session from 'awaiting_input' → 'paused' so it can be
    resumed via the normal resume endpoint.
    """
    _require_session_access(db, session_id, current_user)
    _get_session_intervention_or_404(db, session_id, intervention_id)
    req = _submit_intervention_reply(
        db,
        intervention_id=intervention_id,
        operator_reply=body.reply,
        operator_id=getattr(current_user, "email", None)
        or getattr(current_user, "id", None),
    )
    payload = _serialize_intervention(req)
    payload["message"] = "Reply recorded. Session is now paused and ready to resume."
    return payload


@router.post("/sessions/{session_id}/interventions/{intervention_id}/approve")
def approve_session_intervention(
    session_id: int,
    intervention_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Approve an approval-type intervention request.

    Injects approval context into the checkpoint and transitions session to
    'paused' so it can continue after the operator-proposed action.
    """
    _require_session_access(db, session_id, current_user)
    _get_session_intervention_or_404(db, session_id, intervention_id)
    req = _approve_intervention(
        db,
        intervention_id=intervention_id,
        operator_id=getattr(current_user, "email", None)
        or getattr(current_user, "id", None),
    )
    payload = _serialize_intervention(req)
    payload["message"] = (
        "Intervention approved. Session is now paused and ready to resume."
    )
    return payload


@router.post("/sessions/{session_id}/interventions/{intervention_id}/deny")
def deny_session_intervention(
    session_id: int,
    intervention_id: int,
    body: DenyInterventionBody,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Deny an approval-type intervention request.

    Injects denial context into the checkpoint. The operator can steer the
    session toward an alternative approach via the resume endpoint.
    """
    _require_session_access(db, session_id, current_user)
    _get_session_intervention_or_404(db, session_id, intervention_id)
    req = _deny_intervention(
        db,
        intervention_id=intervention_id,
        reason=body.reason,
        operator_id=getattr(current_user, "email", None)
        or getattr(current_user, "id", None),
    )
    payload = _serialize_intervention(req)
    payload["message"] = (
        "Intervention denied. Session is paused; use resume to continue with updated context."
    )
    return payload


# ── Replan flow endpoints ─────────────────────────────────────────────────────


@router.get("/sessions/{session_id}/failure-summary")
def get_failure_summary(
    session_id: int,
    background_tasks: BackgroundTasks,
    enrich: bool = Query(False),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return the execution failure summary for a stopped/failed session.

    Generates a deterministic fallback summary on first call and optionally
    enriches it via the model in the background. Idempotent — subsequent calls
    return the cached record.
    """
    _require_session_access(db, session_id, current_user)
    record = _get_or_generate_failure_summary(db, session_id)
    if enrich:
        background_tasks.add_task(_enrich_failure_summary_with_llm, session_id)
    payload = _serialize_failure_summary(db, record)
    payload["diagnostics"] = _latest_failure_diagnostics(db, session_id)
    return payload


class OperatorFeedbackBody(BaseModel):
    feedback: str


@router.post("/sessions/{session_id}/operator-feedback")
def submit_operator_feedback(
    session_id: int,
    body: OperatorFeedbackBody,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Store operator free-text feedback on the failure summary.

    Feedback is combined with the agent-generated summary when the operator
    triggers the replan endpoint.
    """
    if not body.feedback.strip():
        raise HTTPException(status_code=400, detail="feedback must not be empty")
    _require_session_access(db, session_id, current_user)
    record = _store_operator_feedback(db, session_id, body.feedback)
    payload = _serialize_failure_summary(db, record)
    payload["message"] = "Operator feedback saved."
    return payload


@router.post("/sessions/{session_id}/replan")
def replan_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Seed a new PlanningSession from the failure summary + operator feedback.

    Operator-triggered only. The agent summarizes; the human decides whether to
    replan or retry. Returns the new planning_session_id so the frontend can
    navigate to Project Architect.
    """
    _require_session_access(db, session_id, current_user)
    return _trigger_replan(db, session_id)


@router.post("/sessions/{session_id}/compact-context")
async def compact_session_context(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Manually trigger deterministic context compaction for a session."""
    from app.config import settings
    from app.services.session.context_compaction import (
        compact_checkpoint_payload,
        estimate_tokens,
    )
    from app.services.workspace.checkpoint_service import CheckpointService

    _require_session_access(db, session_id, current_user)
    session_task = _latest_session_task_link(db, session_id)
    if not session_task:
        raise HTTPException(status_code=404, detail="No task found for this session")

    checkpoint_service = CheckpointService(db)
    latest_checkpoint = checkpoint_service.get_latest_checkpoint(
        session_id, session_task.task_id
    )
    if not latest_checkpoint:
        raise HTTPException(status_code=404, detail="No checkpoint found to compact")

    tokens_before = estimate_tokens(latest_checkpoint)
    compacted = compact_checkpoint_payload(
        latest_checkpoint,
        max_plan_steps=settings.MAX_PLAN_STEPS,
    )
    checkpoint_service.save_compact_checkpoint(
        session_id, session_task.task_id, compacted
    )
    tokens_after = estimate_tokens(compacted)

    return {
        "session_id": session_id,
        "task_id": session_task.task_id,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "reduction_pct": round((1 - tokens_after / max(tokens_before, 1)) * 100),
    }


@router.get("/sessions/{session_id}/events")
def get_session_events(
    session_id: int,
    task_id: Optional[int] = None,
    limit: int = Query(50, ge=1),
    event_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return the existing orchestration event journal for a session."""
    from app.services.orchestration.state.persistence import read_orchestration_events
    from app.services.session.session_runtime_service import (
        resolve_event_log_project_dir,
    )

    if event_type and not is_known_event_type(event_type):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown event_type '{event_type}'",
        )

    session = _require_session_access(db, session_id, current_user)
    if task_id is None:
        session_task = _latest_session_task_link(db, session_id)
        if not session_task:
            return {"session_id": session_id, "task_id": None, "events": [], "count": 0}
        task_id = session_task.task_id

    project_dir = resolve_event_log_project_dir(db, session, task_id)
    if not project_dir:
        raise HTTPException(status_code=404, detail="Could not resolve workspace path")

    events = read_orchestration_events(
        project_dir,
        session_id,
        task_id,
        event_type_filter=event_type,
    )
    return {
        "session_id": session_id,
        "task_id": task_id,
        "events": events[-limit:],
        "count": len(events),
    }


@router.get("/sessions/{session_id}/tasks/{task_id}/events")
def get_session_task_events(
    session_id: int,
    task_id: int,
    event_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return the append-only orchestration event journal for a session/task pair.

    Useful for replaying what happened during a failed or completed run without
    having to parse raw log text.  Pass ``event_type`` to filter to a single
    event type (e.g. ``validation_result``, ``step_finished``).
    """
    if event_type and not is_known_event_type(event_type):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown event_type '{event_type}'",
        )

    session = _require_session_access(db, session_id, current_user)

    project = db.query(Project).filter(Project.id == session.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    task = db.query(Task).filter(Task.id == task_id).first()
    events = _read_task_events_from_candidates(
        db=db,
        project=project,
        task=task,
        session_id=session_id,
        task_id=task_id,
        event_type_filter=event_type,
    )
    return {"session_id": session_id, "task_id": task_id, "events": events}


@router.get("/sessions/{session_id}/decision-timeline")
def get_session_decision_timeline(
    session_id: int,
    phase: Optional[str] = None,
    limit: int = Query(DEFAULT_TIMELINE_LIMIT, ge=1),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return a read-only normalized decision timeline for a session."""
    _require_session_access(db, session_id, current_user)

    return get_session_decision_timeline_payload(
        db,
        session_id,
        phase=phase,
        limit=limit,
    )


@router.get("/sessions/{session_id}/replay")
def get_session_replay_reconstruction(
    session_id: int,
    task_id: Optional[int] = None,
    boundary_mode: Optional[str] = None,
    event_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    snapshot_index: Optional[int] = None,
    checkpoint_name: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return a bounded read-only replay reconstruction report.

    This endpoint does not execute replay, load checkpoint payloads, resume
    sessions, emit events, write database rows, or influence runtime control.
    """

    session = _require_session_access(db, session_id, current_user)

    project = db.query(Project).filter(Project.id == session.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        resolved_task_id = _resolve_replay_task_id(db, session_id, task_id)
    except HTTPException as exc:
        if exc.status_code == 404 and exc.detail == "No task found for replay":
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        raise
    task = db.query(Task).filter(Task.id == resolved_task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    boundary = _build_replay_boundary(
        boundary_mode=boundary_mode,
        event_id=event_id,
        timestamp=timestamp,
        snapshot_index=snapshot_index,
        checkpoint_name=checkpoint_name,
    )
    project_dir = str(
        resolve_project_workspace_path(project.workspace_path, project.name, db=db)
    )
    report = reconstruct_execution_state(
        project_dir=project_dir,
        session_id=session_id,
        task_id=resolved_task_id,
        boundary=boundary,
        checkpoint_dir=None,
        compare_workspace=False,
    )
    return _truncate_replay_report(report)


@router.get("/sessions/{session_id}/diff")
def get_session_state_diff(
    session_id: int,
    from_checkpoint: Optional[int] = None,
    to_checkpoint: Optional[int] = None,
    task_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return a structured diff between two orchestration state snapshots."""
    _require_session_access(db, session_id, current_user)

    return _get_session_state_diff_payload(
        db,
        session_id,
        from_checkpoint=from_checkpoint,
        to_checkpoint=to_checkpoint,
        task_id=task_id,
    )


@router.get("/sessions/{session_id}/compare-divergence")
def get_session_divergence_compare(
    session_id: int,
    limit: int = 5,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Compare current session anomaly fingerprint with sibling sessions."""
    _require_session_access(db, session_id, current_user)

    return _get_session_divergence_compare_payload(
        db,
        session_id,
        limit=limit,
    )


@router.get("/sessions/{session_id}/trace-export")
def get_session_trace_export(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return derived trace spans for the latest session task."""
    _require_session_access(db, session_id, current_user)

    return _get_session_trace_export_payload(db, session_id)


@router.get("/sessions/{session_id}/execution-dag")
def get_session_execution_dag(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return a DAG view derived from events and checkpoint lineage."""
    _require_session_access(db, session_id, current_user)

    return _get_session_execution_dag_payload(db, session_id)


@router.get("/sessions/{session_id}/focus")
def get_session_focus_mode(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return operator focus-mode payload with low-signal noise suppressed."""
    _require_session_access(db, session_id, current_user)

    return _get_session_focus_mode_payload(db, session_id)


@router.get("/sessions/{session_id}/mobile-interruptions")
def get_session_mobile_interruptions(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return interruption-first mobile cards for approvals, retry, and stop."""
    _require_session_access(db, session_id, current_user)

    return _get_session_mobile_interruptions_payload(db, session_id)


@router.get("/sessions/{session_id}/timeline")
def get_session_timeline(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return a pre-aggregated operator narrative timeline for a session."""
    _require_session_access(db, session_id, current_user)
    return _get_session_timeline_payload(db, session_id)


@router.get("/sessions/{session_id}/recovery-context")
def get_session_recovery_context(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return structured recovery context for paused, stopped, or failed sessions.

    Assembles stop reason, task progress, preserved state, and recommended
    actions from existing data — no raw log parsing required by the operator.
    """
    _require_session_access(db, session_id, current_user)
    return _get_session_recovery_context_payload(db, session_id)


@router.post("/sessions/{session_id}/retry-planning-lane")
def retry_session_planning_lane(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Operator-controlled retry through the configured stronger planning lane."""
    enforce_api_rate_limit(request, "retry_planning_lane", current_user=current_user)
    session = _require_session_access(db, session_id, current_user)
    result = _retry_session_with_stronger_planning_lane(db, session)
    return {
        "status": "queued",
        "session_id": session.id,
        **result,
    }


@router.get("/sessions/{session_id}/digest")
def get_session_digest(
    session_id: int,
    enrich: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return a compact human-readable digest of session execution.

    Answers: what happened, why it stopped, what was preserved, what changed,
    and what to do next. Suitable for returning operators who weren't watching live.
    """
    _require_session_access(db, session_id, current_user)
    return _get_session_digest_payload(db, session_id, enrich=enrich)


@router.get("/sessions/{session_id}/dispatch-watchdog")
def get_session_dispatch_watchdog(
    session_id: int,
    sync_alert: bool = True,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return queue/claim watchdog state for session tasks."""
    _require_session_access(db, session_id, current_user)

    if sync_alert:
        return _refresh_session_dispatch_watchdog_alert(db, session_id)
    return _get_session_dispatch_watchdog_payload(db, session_id)


@router.get("/sessions/{session_id}/reconciliation-audit")
def get_session_reconciliation_audit(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Explain why a session is paused, waiting, or stalled."""
    _require_session_access(db, session_id, current_user)
    return _get_session_reconciliation_audit_payload(db, session_id)


@router.get("/sessions/{session_id}/prompts/{template_name}")
def get_prompt_template(
    session_id: int,
    template_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get a prompt template for the session"""
    _require_session_access(db, session_id, current_user)
    template = PromptTemplates.get_template(template_name)
    if not template:
        raise HTTPException(
            status_code=404, detail=f"Template '{template_name}' not found"
        )

    return {
        "template_name": template_name,
        "template": template,
        "session_id": session_id,
    }


@router.get("/sessions/{session_id}/logs")
def get_session_logs(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    limit: Optional[int] = 100,
    offset: int = 0,
):
    """
    Get logs for a session (simple endpoint for stopped sessions)

    This endpoint fetches all logs for a session, optionally filtered by instance_id.
    It's designed to work for stopped sessions where WebSocket streaming isn't available.

    Args:
        session_id: Session ID
        limit: Maximum number of logs to return (default: 100)
        offset: Offset for pagination (default: 0)

    Returns:
        List of log entries
    """
    _require_session_access(db, session_id, current_user)
    return _get_session_logs_payload(db, session_id, limit=limit, offset=offset)


@router.get("/sessions/{session_id}/logs/sorted")
def get_sorted_logs(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    order: str = "asc",  # "asc" for oldest first, "desc" for newest first
    deduplicate: bool = True,  # Remove duplicate entries
    level: Optional[str] = None,  # Optional filter by log level
    limit: Optional[int] = None,  # Optional limit on number of logs
    offset: int = 0,  # NEW: For pagination
):
    """
    Get sorted and optionally deduplicated logs for a session

    OPTIMIZED: Uses database-level sorting and pagination to avoid timeout issues

    Args:
        session_id: Session ID
        order: Sort order - "asc" (oldest first) or "desc" (newest first)
        deduplicate: Remove duplicate log entries (note: expensive for large datasets)
        level: Optional log level filter (INFO, WARNING, ERROR)
        limit: Optional limit on number of logs to return (default: 100)
        offset: Offset for pagination (default: 0)

    Returns:
        Sorted list of log entries
    """
    _require_session_access(db, session_id, current_user)
    return _get_sorted_logs_payload(
        db,
        session_id,
        order=order,
        deduplicate=deduplicate,
        level=level,
        limit=limit,
        offset=offset,
    )


@router.post("/generate-steps")
async def generate_steps_from_description(
    request: Request,
    body: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Generate task steps using OpenClaw AI"""
    task_name = body.get("task_name", "Task")
    description = body.get("description", "")

    # Create prompt for step generation
    prompt_template = PromptTemplates.get_template("generate_steps")
    if not prompt_template:
        prompt_template = """You are a task planning assistant. Given a task name and description, break it down into clear, actionable steps. Return the steps as a JSON array.

Task Name: {task_name}
Description: {description}

Return ONLY a JSON array of step objects with 'title' and 'description' fields. Example:
[
  {{ "title": "Step 1", "description": "First thing to do" }},
  {{ "title": "Step 2", "description": "Second thing to do" }}
]"""

    formatted_prompt = prompt_template.format(
        task_name=task_name, description=description
    )

    # Use a simple heuristic-based step generator as fallback.
    # Common patterns for different task types
    patterns = {
        "authentication|login|register": [
            {
                "title": "Create Authentication Routes",
                "description": "Set up /login and /register endpoints",
            },
            {
                "title": "Implement Password Hashing",
                "description": "Add bcrypt or similar for secure password storage",
            },
            {
                "title": "Create JWT Token Generation",
                "description": "Generate and validate JWT tokens for authentication",
            },
            {
                "title": "Build Login/Register Forms",
                "description": "Create frontend forms with validation",
            },
            {
                "title": "Add Protected Routes",
                "description": "Implement route guards for authenticated users",
            },
        ],
        "database|sql|model": [
            {
                "title": "Design Database Schema",
                "description": "Create tables and relationships",
            },
            {
                "title": "Implement ORM Models",
                "description": "Define SQLAlchemy models",
            },
            {
                "title": "Create Migrations",
                "description": "Set up Alembic for database migrations",
            },
            {
                "title": "Add Database Connection",
                "description": "Configure database connection pooling",
            },
        ],
        "frontend|ui|react": [
            {
                "title": "Setup Component Structure",
                "description": "Organize React components folder",
            },
            {
                "title": "Create UI Components",
                "description": "Build reusable UI components",
            },
            {"title": "Add Styling", "description": "Implement CSS/Tailwind styling"},
            {
                "title": "Add State Management",
                "description": "Setup React Context or Redux",
            },
        ],
    }

    desc_lower = description.lower()
    detected_pattern = None
    for pattern, steps in patterns.items():
        if re.search(pattern, desc_lower):
            detected_pattern = pattern
            break

    if detected_pattern:
        return {"steps": patterns[detected_pattern], "task_name": task_name}

    # Default generic steps
    default_steps = [
        {
            "title": "Analyze Requirements",
            "description": "Understand the task scope and requirements",
        },
        {
            "title": "Plan Implementation",
            "description": "Create a step-by-step implementation plan",
        },
        {
            "title": "Write Code",
            "description": "Implement the feature following best practices",
        },
        {
            "title": "Test Implementation",
            "description": "Write and run tests to verify functionality",
        },
        {"title": "Document Changes", "description": "Update documentation and README"},
    ]

    return {"steps": default_steps, "task_name": task_name}


class OverwriteCheckRequest(BaseModel):
    """Request model for overwrite check"""

    project_id: int
    task_subfolder: str
    planned_files: Optional[List[str]] = []


@router.post("/sessions/{session_id}/check-overwrites")
async def check_session_overwrites(
    session_id: int,
    request: OverwriteCheckRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Check for potential overwrites before executing a task in this session

    Args:
        session_id: Session ID to check
        request: Overwrite check request with project info and planned files
        db: Database session

    Returns:
        Overwrite protection result with safety status and warnings
    """
    _require_session_access(db, session_id, current_user)
    get_project_for_user(db, request.project_id, current_user)
    return _check_session_overwrites_payload(
        db,
        session_id,
        project_id=request.project_id,
        task_subfolder=request.task_subfolder,
        planned_files=request.planned_files,
    )


@router.post("/sessions/{session_id}/create-backup")
async def create_session_backup(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Create a backup of existing workspace before proceeding

    Args:
        session_id: Session ID to backup
        db: Database session

    Returns:
        Backup result with path and file count
    """
    try:
        _require_session_access(db, session_id, current_user)
        return _create_session_backup_payload(db, session_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")


@router.get("/sessions/{session_id}/workspace-info")
async def get_session_workspace_info(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Get workspace information for a session

    Args:
        session_id: Session ID to check
        db: Database session

    Returns:
        Workspace details including file count and last modified date
    """
    try:
        _require_session_access(db, session_id, current_user)
        return _get_session_workspace_info_payload(db, session_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Workspace info failed: {str(e)}")


@router.post("/sessions/{session_id}/checkpoint/save")
async def save_session_checkpoint(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Save a manual checkpoint for a paused session

    Args:
        session_id: Session ID to checkpoint
        db: Database session

    Returns:
        Checkpoint metadata including path and timestamp
    """
    _require_session_access(db, session_id, current_user)
    return await _save_session_checkpoint_payload(db, session_id)


@router.get("/sessions/{session_id}/checkpoints")
async def list_session_checkpoints(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    List all checkpoints for a session

    Args:
        session_id: Session ID to check
        db: Database session

    Returns:
        List of checkpoint metadata (oldest first)
    """
    try:
        _require_session_access(db, session_id, current_user)
        return _list_session_checkpoints_payload(db, session_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to list checkpoints: {str(e)}"
        )


@router.get("/sessions/{session_id}/checkpoints/{checkpoint_name}")
async def inspect_session_checkpoint(
    session_id: int,
    checkpoint_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return operator-friendly metadata for one checkpoint."""
    try:
        _require_session_access(db, session_id, current_user)
        return _inspect_session_checkpoint_payload(db, session_id, checkpoint_name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to inspect checkpoint: {str(e)}"
        )


@router.post("/sessions/{session_id}/checkpoint/load")
async def load_session_checkpoint(
    session_id: int,
    checkpoint_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Load a specific checkpoint for resuming execution

    Args:
        session_id: Session ID to resume
        checkpoint_name: Name of the checkpoint to load
        db: Database session

    Returns:
        Resume result with new session key
    """
    _require_session_access(db, session_id, current_user)
    return await _load_session_checkpoint_payload(db, session_id, checkpoint_name)


@router.post("/sessions/{session_id}/checkpoints/{checkpoint_name}/replay")
async def replay_session_checkpoint(
    session_id: int,
    checkpoint_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Resume execution from a selected checkpoint for operator replay."""
    _require_session_access(db, session_id, current_user)
    return await _replay_session_checkpoint_payload(db, session_id, checkpoint_name)


class CounterfactualReplayRequest(BaseModel):
    step_from_index: Optional[int] = None
    policy_profile: Optional[str] = None
    model_family: Optional[str] = None
    adaptation_profile: Optional[str] = None


@router.post(
    "/sessions/{session_id}/checkpoints/{checkpoint_name}/counterfactual-replay"
)
async def counterfactual_replay_session_checkpoint(
    session_id: int,
    checkpoint_name: str,
    request: CounterfactualReplayRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Replay a checkpoint with variable overrides for counterfactual root-cause analysis.

    Accepts optional overrides:
    - step_from_index: rewind execution to this step (0-based)
    - policy_profile: swap orchestration policy for this replay
    - model_family: swap model for this replay
    - adaptation_profile: swap prompt adaptation profile for this replay

    Applied overrides (step_from_index) are woven into the checkpoint state.
    Deferred overrides (policy/model) are stored in context.replay_overrides and
    require executor support to take full effect.
    """
    overrides = {
        k: v
        for k, v in {
            "step_from_index": request.step_from_index,
            "policy_profile": request.policy_profile,
            "model_family": request.model_family,
            "adaptation_profile": request.adaptation_profile,
        }.items()
        if v is not None
    }
    _require_session_access(db, session_id, current_user)
    return await _replay_session_checkpoint_counterfactual_payload(
        db, session_id, checkpoint_name, overrides=overrides
    )


@router.delete("/sessions/{session_id}/checkpoints/{checkpoint_name}")
async def delete_session_checkpoint(
    session_id: int,
    checkpoint_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Delete a specific checkpoint

    Args:
        session_id: Session ID
        checkpoint_name: Name of the checkpoint to delete
        db: Database session

    Returns:
        Deletion result
    """
    try:
        _require_session_access(db, session_id, current_user)
        return _delete_session_checkpoint_payload(db, session_id, checkpoint_name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete checkpoint: {str(e)}"
        )


@router.post("/sessions/{session_id}/checkpoint/cleanup")
async def cleanup_session_checkpoints(
    session_id: int,
    keep_latest: int = 3,
    max_age_hours: int = 24,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Clean up old checkpoints, keeping only the latest N

    Args:
        session_id: Session ID to cleanup
        keep_latest: Number of most recent checkpoints to keep
        max_age_hours: Delete checkpoints older than this (hours)
        db: Database session

    Returns:
        Cleanup statistics
    """
    try:
        _require_session_access(db, session_id, current_user)
        return _cleanup_session_checkpoints_payload(
            db, session_id, keep_latest=keep_latest, max_age_hours=max_age_hours
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to cleanup checkpoints: {str(e)}"
        )


@router.get("/sessions/{session_id}/knowledge-usage")
def get_session_knowledge_usage(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return knowledge references used in a session, grouped by trigger_phase."""
    _require_session_access(db, session_id, current_user)
    return get_session_knowledge_usage_payload(db, session_id)


def get_session_knowledge_usage_payload(db: Session, session_id: int) -> Dict[str, Any]:
    from app.models import KnowledgeItem, KnowledgeUsageLog as KUL

    session = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id, SessionModel.deleted_at.is_(None))
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    rows = (
        db.query(KUL, KnowledgeItem)
        .join(KnowledgeItem, KUL.knowledge_item_id == KnowledgeItem.id)
        .filter(KUL.session_id == session_id)
        .order_by(KUL.created_at)
        .all()
    )

    phases: Dict[str, list] = {}
    grouped: Dict[tuple[str, str, str, bool], dict] = {}
    for log, item in rows:
        key = (
            str(log.trigger_phase or ""),
            str(log.knowledge_item_id),
            str(log.retrieval_reason or ""),
            bool(log.used_in_prompt),
        )
        created_at_iso = _serialize_session_timestamp(log.created_at)
        entry = grouped.get(key)
        if entry is None:
            entry = {
                "knowledge_item_id": log.knowledge_item_id,
                "title": item.title,
                "knowledge_type": item.knowledge_type,
                "confidence_avg": float(log.confidence or 0.0),
                "confidence_max": float(log.confidence or 0.0),
                "retrieval_reason": log.retrieval_reason,
                "used_in_prompt": log.used_in_prompt,
                "usage_count": 1,
                "first_used_at": created_at_iso,
                "last_used_at": created_at_iso,
            }
            grouped[key] = entry
            continue

        entry["usage_count"] += 1
        confidence = float(log.confidence or 0.0)
        previous_count = entry["usage_count"] - 1
        entry["confidence_avg"] = (
            (float(entry["confidence_avg"]) * previous_count) + confidence
        ) / entry["usage_count"]
        entry["confidence_max"] = max(float(entry["confidence_max"]), confidence)
        if created_at_iso:
            if not entry["first_used_at"] or created_at_iso < entry["first_used_at"]:
                entry["first_used_at"] = created_at_iso
            if not entry["last_used_at"] or created_at_iso > entry["last_used_at"]:
                entry["last_used_at"] = created_at_iso

    for (trigger_phase, _, _, _), entry in grouped.items():
        phases.setdefault(trigger_phase, []).append(entry)

    for phase_entries in phases.values():
        phase_entries.sort(
            key=lambda entry: (
                entry["first_used_at"] or "",
                entry["title"],
                entry["retrieval_reason"] or "",
            )
        )

    flat_entries = [entry for entries in phases.values() for entry in entries]
    flat_entries_with_phase = [
        (phase, entry) for phase, entries in phases.items() for entry in entries
    ]
    top_items = sorted(
        flat_entries_with_phase,
        key=lambda item: (
            -float(item[1].get("confidence_max") or 0.0),
            str(item[1].get("title") or ""),
        ),
    )[:5]
    phase_summaries: Dict[str, Dict[str, Any]] = {}
    for phase in ("planning", "validation", "failure"):
        entries = phases.get(phase, [])
        phase_summaries[phase] = {
            "knowledge_used": any(bool(entry["used_in_prompt"]) for entry in entries),
            "item_count": len(entries),
            "retrieval_reasons": sorted(
                {
                    str(entry["retrieval_reason"])
                    for entry in entries
                    if entry.get("retrieval_reason")
                }
            ),
            "top_item_titles": [entry["title"] for entry in entries[:3]],
            "omission_reason": (
                None if entries else "no_retrieval_attempts_or_no_matching_items_logged"
            ),
        }

    return {
        "session_id": session_id,
        "knowledge_used": any(bool(entry["used_in_prompt"]) for entry in flat_entries),
        "omission_reason": (
            None
            if flat_entries
            else "no_retrieval_attempts_or_no_matching_items_logged"
        ),
        "top_items": [
            {
                "title": entry["title"],
                "knowledge_type": entry["knowledge_type"],
                "confidence": entry["confidence_max"],
                "phase": phase,
                "retrieval_reason": entry["retrieval_reason"],
            }
            for phase, entry in top_items
        ],
        "phase_summaries": phase_summaries,
        "phases": phases,
    }


@router.post("/checkpoints/orphaned/cleanup")
async def cleanup_orphaned_checkpoints(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """
    Clean checkpoint artifacts for sessions that are missing or soft-deleted.
    """
    try:
        return _cleanup_orphaned_checkpoints_payload(db)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cleanup orphaned checkpoints: {str(e)}",
        )
