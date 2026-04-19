"""Task reporting helpers for orchestration workers."""

from __future__ import annotations

import json
from typing import Any, Dict

from app.models import LogEntry, Task


def build_task_report_payload(db: Any, task_id: int) -> Dict[str, Any]:
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise ValueError(f"Task {task_id} not found")

    logs = (
        db.query(LogEntry)
        .filter(LogEntry.task_id == task_id)
        .order_by(LogEntry.created_at)
        .all()
    )

    return {
        "task_id": task.id,
        "title": task.title,
        "status": task.status.value,
        "created_at": task.created_at.isoformat(),
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "duration_seconds": (
            (task.completed_at - task.started_at).total_seconds()
            if task.started_at and task.completed_at
            else None
        ),
        "structured_state": {
            "task_id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "status": task.status.value,
            "plan_position": getattr(task, "plan_position", None),
            "execution_profile": getattr(task, "execution_profile", None),
            "workspace_status": getattr(task, "workspace_status", None),
            "task_subfolder": getattr(task, "task_subfolder", None),
            "created_at": task.created_at.isoformat(),
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": (
                task.completed_at.isoformat() if task.completed_at else None
            ),
            "error_message": task.error_message,
        },
        "logs": [
            {
                "level": log.level,
                "message": log.message,
                "timestamp": log.created_at.isoformat(),
            }
            for log in logs
        ],
    }


def render_task_report(
    report: Dict[str, Any], output_format: str = "json"
) -> Dict[str, Any]:
    if output_format == "markdown":
        report_text = f"# Task Report: {report['title']}\n\n"
        report_text += f"**Status:** {report['status']}\n\n"
        report_text += f"**Duration:** {report['duration_seconds']} seconds\n\n"
        structured_state = report.get("structured_state", {})
        if structured_state:
            report_text += "## Structured State\n\n"
            report_text += "```json\n"
            report_text += json.dumps(structured_state, indent=2)
            report_text += "\n```\n\n"
        report_text += "## Logs\n\n"
        for log in report["logs"]:
            report_text += f"- [{log['level']}] {log['message']}\n"

        return {"report": report_text, "format": "markdown"}

    return {"report": report, "format": output_format}
