"""In-memory task store for the medium CLI fixture."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    title: str
    completed: bool = False


class TaskStore:
    def __init__(self) -> None:
        self._tasks: list[Task] = []

    def add(self, title: str, *, completed: bool = False) -> Task:
        task = Task(title=title, completed=completed)
        self._tasks.append(task)
        return task

    def all(self) -> list[Task]:
        return list(self._tasks)

    def completed(self) -> list[Task]:
        return [task for task in self._tasks if task.completed]

    def summary(self) -> tuple[int, int]:
        raise NotImplementedError("summary counts are not implemented yet")
