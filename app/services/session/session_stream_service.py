"""Session websocket streaming helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from app.auth import verify_token
from app.database import get_db_session as create_db_session
from app.models import LogEntry, Session as SessionModel, User
from app.services.log_stream_service import LogStreamService


logger = logging.getLogger(__name__)


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
        logger.error("WebSocket error for session %s: %s", session_id, str(exc))
    finally:
        heartbeat_task.cancel()
        _remove_active_websocket(session_id, websocket)


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
        logger.error("Status websocket error for session %s: %s", session_id, str(exc))
    finally:
        status_task.cancel()
        heartbeat_task.cancel()
