"""Session websocket streaming helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from app.auth import verify_token
from app.database import get_db_session as create_db_session
from app.models import LogEntry, Session as SessionModel, User
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.log_stream_service import LogStreamService
from app.services.streaming_health import (
    record_stream_error,
    register_stream_connection,
    unregister_stream_connection,
)


logger = logging.getLogger(__name__)


def _poll_new_orchestration_events(
    workspace_path: str,
    session_id: int,
    task_event_cursors: Dict[int, int],
) -> Tuple[List[Dict[str, Any]], Dict[int, int]]:
    """Read new events from per-task JSONL journals since the last cursor position.

    ``task_event_cursors`` maps task_id → number of lines already sent.
    Returns (new_events_list, updated_cursors).
    """
    events_dir = Path(workspace_path) / ".openclaw" / "events"
    if not events_dir.exists():
        return [], task_event_cursors

    new_events: List[Dict[str, Any]] = []
    updated_cursors = dict(task_event_cursors)

    try:
        for log_path in sorted(events_dir.glob(f"session_{session_id}_task_*.jsonl")):
            try:
                task_id = int(log_path.stem.split("_task_")[-1])
            except (ValueError, IndexError):
                continue
            already_sent = updated_cursors.get(task_id, 0)
            try:
                with log_path.open("r", encoding="utf-8") as fh:
                    all_lines = fh.readlines()
            except OSError:
                continue
            for line in all_lines[already_sent:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    new_events.append(event)
                except json.JSONDecodeError:
                    pass
            updated_cursors[task_id] = len(all_lines)
    except Exception:
        pass

    return new_events, updated_cursors


def _prepare_initial_orchestration_events(
    workspace_path: Optional[str],
    session_id: int,
    *,
    replay_limit: int = 100,
) -> Tuple[List[Dict[str, Any]], Dict[int, int]]:
    """Replay a bounded event backlog to reconnecting websocket clients."""

    if not workspace_path:
        return [], {}

    events, cursors = _poll_new_orchestration_events(workspace_path, session_id, {})
    if replay_limit > 0 and len(events) > replay_limit:
        events = events[-replay_limit:]
    return events, cursors


active_websockets: dict = {}


def _remove_active_websocket(session_id: int, websocket: WebSocket) -> None:
    try:
        active_websockets[session_id] = [
            w
            for w in active_websockets.get(session_id, [])
            if w.get("websocket") != websocket
        ]
        if not active_websockets[session_id]:
            del active_websockets[session_id]
    except Exception:
        pass


def _get_websocket_token(websocket: WebSocket) -> Optional[str]:
    authorization = websocket.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()

    token = websocket.query_params.get("token")
    if token:
        return token.strip()

    return None


def _authenticate_websocket(websocket: WebSocket, db: Session) -> Optional[User]:
    token = _get_websocket_token(websocket)
    if not token:
        return None

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    payload = verify_token(token, credentials_exception)
    email = payload.get("sub")
    if not email:
        return None

    user = db.query(User).filter(User.email == email).first()
    if not user or not user.is_active:
        return None

    return user


def _prepare_initial_log_batch(
    recent_logs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Normalize initial log order and cursor for websocket replay safety."""

    ordered_logs = list(reversed(recent_logs))
    last_log_id = max((log.get("id", 0) for log in ordered_logs), default=0)
    return ordered_logs, last_log_id


async def stream_session_logs(
    websocket: WebSocket, session_id: int, db: Session
) -> None:
    current_user = _authenticate_websocket(websocket, db)
    if not current_user:
        await websocket.close(code=1008, reason="Authentication required")
        return

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        logger.warning(
            "WebSocket connection rejected: session %s not found", session_id
        )
        await websocket.close(code=1008, reason="Session not found")
        return

    await websocket.accept()
    register_stream_connection("session_logs")
    logger.info(
        "WebSocket connected for session %s, instance: %s",
        session_id,
        session.instance_id,
    )

    if session_id not in active_websockets:
        active_websockets[session_id] = []
    active_websockets[session_id].append(
        {"websocket": websocket, "last_activity": datetime.utcnow()}
    )

    await websocket.send_json(
        {
            "type": "connected",
            "session_id": session_id,
            "session_instance_id": session.instance_id,
            "timestamp": datetime.utcnow().isoformat(),
            "heartbeat_interval": 30,
        }
    )

    # Resolve workspace path for orchestration event journal polling.
    _workspace_path: Optional[str] = None
    if session.project_id:
        from app.models import Project

        _project = db.query(Project).filter(Project.id == session.project_id).first()
        if _project and _project.workspace_path:
            _workspace_path = str(
                resolve_project_workspace_path(_project.workspace_path, _project.name)
            )

    log_service = LogStreamService(db)
    recent_logs = log_service.get_recent_logs(
        session_id, instance_id=session.instance_id, limit=20
    )
    logger.info(
        "Sending %s recent logs to WebSocket (filtered by instance: %s)",
        len(recent_logs),
        session.instance_id,
    )
    if not recent_logs and session.instance_id:
        logger.warning(
            "No logs found for session %s with instance_id %s",
            session_id,
            session.instance_id,
        )
        fallback_logs = log_service.get_recent_logs(
            session_id, instance_id=None, limit=20
        )
        logger.info(
            "Fallback: Found %s logs without instance filter", len(fallback_logs)
        )
        recent_logs = fallback_logs

    recent_logs, last_log_id = _prepare_initial_log_batch(recent_logs)
    for log in recent_logs:
        await websocket.send_json({"type": "log", **log})

    initial_orch_events, _task_event_cursors = _prepare_initial_orchestration_events(
        _workspace_path, session_id
    )
    for orch_event in initial_orch_events:
        await websocket.send_json({"type": "orchestration_event", **orch_event})

    logger.info("Sent %s initial logs, starting main loop...", len(recent_logs))

    _TERMINAL_STATUSES = frozenset({"stopped", "paused", "completed", "failed"})

    async def heartbeat_sender() -> None:
        try:
            while True:
                await asyncio.sleep(30)
                await websocket.send_text("ping")
                logger.debug("Sent heartbeat ping to session %s", session_id)
        except Exception as exc:
            logger.error(
                "Heartbeat sender error for session %s: %s", session_id, str(exc)
            )

    heartbeat_task = asyncio.create_task(heartbeat_sender())
    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                data = None

            poll_db = create_db_session()
            session_is_terminal = False
            terminal_status = None
            alert_level = None
            alert_message = None
            try:
                current_session = (
                    poll_db.query(SessionModel)
                    .filter(SessionModel.id == session_id)
                    .first()
                )
                if current_session:
                    query = poll_db.query(LogEntry).filter(
                        LogEntry.session_id == session_id,
                        LogEntry.id > last_log_id,
                    )
                    if current_session.instance_id:
                        query = query.filter(
                            LogEntry.session_instance_id == current_session.instance_id
                        )
                    else:
                        query = query.filter(LogEntry.session_instance_id.is_(None))

                    new_logs = (
                        query.order_by(LogEntry.created_at.asc()).limit(100).all()
                    )
                    for log in new_logs:
                        last_log_id = max(last_log_id, log.id)
                        await websocket.send_json(
                            {
                                "type": "log",
                                "id": log.id,
                                "session_id": log.session_id,
                                "task_id": log.task_id,
                                "message": log.message,
                                "level": log.level,
                                "timestamp": (
                                    log.created_at.isoformat()
                                    if log.created_at
                                    else None
                                ),
                                "metadata": (
                                    json.loads(log.log_metadata)
                                    if log.log_metadata
                                    else {}
                                ),
                                "session_instance_id": log.session_instance_id,
                            }
                        )

                    # Emit new orchestration events from the JSONL journal.
                    if _workspace_path:
                        new_orch_events, _task_event_cursors = (
                            _poll_new_orchestration_events(
                                _workspace_path, session_id, _task_event_cursors
                            )
                        )
                        for orch_event in new_orch_events:
                            await websocket.send_json(
                                {"type": "orchestration_event", **orch_event}
                            )

                    # Detect terminal state after draining all pending logs
                    if (
                        not current_session.is_active
                        and current_session.status in _TERMINAL_STATUSES
                    ):
                        session_is_terminal = True
                        terminal_status = current_session.status
                        alert_level = getattr(current_session, "last_alert_level", None)
                        alert_message = getattr(
                            current_session, "last_alert_message", None
                        )
            finally:
                poll_db.close()

            if session_is_terminal:
                await websocket.send_json(
                    {
                        "type": "session_ended",
                        "session_id": session_id,
                        "status": terminal_status,
                        "alert_level": alert_level,
                        "alert_message": alert_message,
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                )
                logger.info(
                    "Session %s reached terminal status %r; closing log stream",
                    session_id,
                    terminal_status,
                )
                break

            if data is None:
                continue

            for ws_info in active_websockets.get(session_id, []):
                if ws_info.get("websocket") == websocket:
                    ws_info["last_activity"] = datetime.utcnow()
                    break

            if data == "ping":
                await websocket.send_text("pong")
                logger.debug("Received ping from session %s, sent pong", session_id)
            elif data.lower() == "pong":
                logger.debug("Client pong received for session %s", session_id)
            else:
                logger.debug("Received message from WebSocket: %s...", data[:100])
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected gracefully for session %s", session_id)
    except Exception as exc:
        record_stream_error("session_logs", exc)
        logger.error("WebSocket error for session %s: %s", session_id, str(exc))
    finally:
        heartbeat_task.cancel()
        _remove_active_websocket(session_id, websocket)
        unregister_stream_connection("session_logs")


async def stream_session_status(
    websocket: WebSocket, session_id: int, db: Session
) -> None:
    current_user = _authenticate_websocket(websocket, db)
    if not current_user:
        await websocket.close(code=1008, reason="Authentication required")
        return

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        await websocket.close(code=1008, reason="Session not found")
        return

    await websocket.accept()
    register_stream_connection("session_status")
    await websocket.send_json(
        {
            "type": "connected",
            "session_id": session_id,
            "timestamp": datetime.utcnow().isoformat(),
            "heartbeat_interval": 30,
            "status_interval": 2,
        }
    )

    _TERMINAL_STATUSES_STATUS = frozenset({"stopped", "paused", "completed", "failed"})

    async def status_sender() -> None:
        last_snapshot: Optional[Dict[str, Any]] = None
        while True:
            await asyncio.sleep(2)
            poll_db = create_db_session()
            try:
                current = (
                    poll_db.query(SessionModel)
                    .filter(SessionModel.id == session_id)
                    .first()
                )
                if not current:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "session_id": session_id,
                            "message": "Session not found",
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    )
                    break

                snapshot = {
                    "id": current.id,
                    "status": current.status,
                    "is_active": current.is_active,
                    "started_at": (
                        current.started_at.isoformat() if current.started_at else None
                    ),
                    "stopped_at": (
                        current.stopped_at.isoformat() if current.stopped_at else None
                    ),
                    "paused_at": (
                        current.paused_at.isoformat() if current.paused_at else None
                    ),
                    "resumed_at": (
                        current.resumed_at.isoformat() if current.resumed_at else None
                    ),
                    "updated_at": (
                        current.updated_at.isoformat() if current.updated_at else None
                    ),
                    "alert_level": getattr(current, "last_alert_level", None),
                    "alert_message": getattr(current, "last_alert_message", None),
                }

                if snapshot != last_snapshot:
                    await websocket.send_json(
                        {
                            "type": "status_update",
                            "session_id": session_id,
                            "status": snapshot,
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    )
                    last_snapshot = snapshot

                # Signal terminal state so the frontend stops waiting
                if (
                    not current.is_active
                    and current.status in _TERMINAL_STATUSES_STATUS
                ):
                    await websocket.send_json(
                        {
                            "type": "session_terminal",
                            "session_id": session_id,
                            "status": current.status,
                            "alert_level": getattr(current, "last_alert_level", None),
                            "alert_message": getattr(
                                current, "last_alert_message", None
                            ),
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    )
                    logger.info(
                        "Session %s is terminal (%s); closing status stream",
                        session_id,
                        current.status,
                    )
                    break
            except Exception as exc:
                logger.error(
                    "Status sender error for session %s: %s", session_id, str(exc)
                )
                break
            finally:
                poll_db.close()

    async def heartbeat_sender() -> None:
        try:
            while True:
                await asyncio.sleep(30)
                await websocket.send_text("ping")
        except Exception as exc:
            logger.error(
                "Status heartbeat error for session %s: %s", session_id, str(exc)
            )

    status_task = asyncio.create_task(status_sender())
    heartbeat_task = asyncio.create_task(heartbeat_sender())

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                # Exit the receive loop once the status_sender has finished
                if status_task.done():
                    break
                continue

            if data == "ping":
                await websocket.send_text("pong")
            elif data.lower() == "pong":
                continue
            else:
                logger.debug(
                    "Status websocket received message for session %s: %s",
                    session_id,
                    data[:100],
                )
    except WebSocketDisconnect:
        logger.info("Status websocket disconnected for session %s", session_id)
    except Exception as exc:
        record_stream_error("session_status", exc)
        logger.error("Status websocket error for session %s: %s", session_id, str(exc))
    finally:
        status_task.cancel()
        heartbeat_task.cancel()
        unregister_stream_connection("session_status")
