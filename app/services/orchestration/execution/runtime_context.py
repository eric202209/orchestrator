"""Runtime Executor Context (Phase 23D).

Phase 23C introduced a Task Execution Sandbox but wired it into dispatch as
a bare `TaskSandbox` object plus two independently-set attributes
(`orchestration_state._project_dir_override` and
`OpenClawSessionService.execution_cwd_override`) that happen to agree by
construction, not by a shared source of truth. This module gives execution
one object -- `RuntimeExecutorContext` -- that carries every piece of
runtime identity a dispatch needs, so downstream consumers (the execution
loop, the OpenClaw execution cwd, the workspace contract, the executor
workspace binding layer, disposal) read one constructed value instead of
each independently re-deriving or being independently told the same thing.

Not a redesign of the Phase 23A/23B/23C model: `runtime_workspace` is
exactly what `_project_dir_override` already pointed at (the Task Execution
Sandbox when one is allocated, otherwise the Project Workspace itself);
`project_workspace` is exactly `canonical_baseline_dir`/the resolved
project root. This module only stops that pair of facts from being carried
as two loose local variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.services.workspace.task_sandbox_allocator import TaskSandbox


@dataclass(frozen=True)
class RuntimeExecutorContext:
    """The single execution authority for one task dispatch.

    ``runtime_workspace`` is always the directory execution actually runs
    in. ``project_workspace`` is always the user's real repository root,
    unaffected by which directory execution runs in. When ``sandbox`` is
    ``None``, ``runtime_workspace == project_workspace`` (Model A: no Task
    Execution Sandbox allocated for this dispatch).
    """

    executor: str
    runtime_workspace: Path
    project_workspace: Path
    project_id: Optional[int]
    task_execution_id: Optional[int]
    runtime_root: Optional[Path] = None
    base_commit: Optional[str] = None
    sandbox: Optional[TaskSandbox] = None

    @property
    def is_sandboxed(self) -> bool:
        return self.sandbox is not None

    @classmethod
    def for_sandbox(
        cls,
        sandbox: TaskSandbox,
        *,
        project_workspace: Path,
        runtime_root: Optional[Path] = None,
    ) -> "RuntimeExecutorContext":
        base_commit = sandbox.read_metadata().get("base_commit")
        return cls(
            executor=sandbox.executor,
            runtime_workspace=sandbox.path,
            project_workspace=project_workspace,
            project_id=sandbox.project_id,
            task_execution_id=sandbox.task_execution_id,
            runtime_root=runtime_root,
            base_commit=base_commit,
            sandbox=sandbox,
        )

    @classmethod
    def for_project_workspace(
        cls,
        *,
        project_workspace: Path,
        executor: str,
        project_id: Optional[int],
        task_execution_id: Optional[int],
    ) -> "RuntimeExecutorContext":
        return cls(
            executor=executor,
            runtime_workspace=project_workspace,
            project_workspace=project_workspace,
            project_id=project_id,
            task_execution_id=task_execution_id,
        )
