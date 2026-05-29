"""Output formatting helpers for the medium CLI fixture."""

from __future__ import annotations

from medium_cli.store import Task


def format_task_line(task: Task, *, include_status: bool = False) -> str:
    if not include_status:
        return task.title
    status = "complete" if task.completed else "open"
    return f"{task.title} [{status}]"


def format_summary(total: int, completed: int) -> str:
    raise NotImplementedError("summary formatting is not implemented yet")
