"""Execution-time executor workspace binding layer (Phase 23D).

Phase 23C confirmed a real architectural blocker: OpenClaw's fail-closed
agent-selection guard (Phase 22C-0) requires a configured agent whose
`workspace` field equals the execution cwd exactly, but a Task Execution
Sandbox path is unique per task execution -- no static `openclaw.json`
entry can ever match it, so every runtime-workspace dispatch raised
`OpenClawAgentSelectionError` by design.

This module closes that gap without rewriting the operator's persistent
`openclaw.json` and without creating or deleting any agent identity in it.
It reads the real config read-only, finds the agent already registered for
the Project Workspace (the same static match OpenClaw agent selection uses
today), and writes a private, ephemeral copy of that config with only that
one agent's `workspace` field rewritten to the current Runtime Workspace.
The copy is consumed by exactly one dispatch (via `OPENCLAW_CONFIG_PATH`,
already an existing `OpenClawSessionService._openclaw_config_path()` seam)
and discarded on release -- the same ephemeral-artifact-per-invocation
pattern `git_containment_guard.build_git_containment_env` already uses for
the git shim.

Deliberately independent of OpenClaw's own service module (imports only
`RuntimeExecutorContext`) so a future executor with different workspace
binding semantics can add its own `bind_<executor>_workspace` function here
without this module growing OpenClaw-specific control flow (Goal 5:
Runtime Workspace ownership belongs to Orchestrator; an executor is only a
consumer of a bound context).
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from app.services.orchestration.execution.runtime_context import (
    RuntimeExecutorContext,
)

logger = logging.getLogger(__name__)


class ExecutorWorkspaceBindingError(Exception):
    """Raised when a Runtime Workspace cannot be bound to an executor.

    Callers must fail closed: never fall back to the Project Workspace,
    never fall back to an executor's default/static configuration, and
    never invent a new agent/executor identity to route around this.
    """


@dataclass
class ExecutorWorkspaceBinding:
    """An active, per-invocation binding. Must be released via `release()`."""

    agent_id: str
    config_path: Path
    _tmp_dir: Path

    def release(self) -> None:
        """Best-effort cleanup of the ephemeral config copy. Never raises."""
        try:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001 - cleanup must never raise
            logger.warning(
                "[EXECUTOR_WORKSPACE_BINDING] Failed to remove temp config " "dir %s",
                self._tmp_dir,
                exc_info=True,
            )


def _paths_match(left: str, right: str) -> bool:
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except Exception:
        return False


def _find_template_agent_id(config: Dict[str, Any], workspace: Path) -> Optional[str]:
    agents = (config.get("agents") or {}).get("list") or []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        agent_id = str(agent.get("id") or "").strip()
        agent_workspace = str(agent.get("workspace") or "").strip()
        if (
            agent_id
            and agent_workspace
            and _paths_match(agent_workspace, str(workspace))
        ):
            return agent_id
    return None


def bind_openclaw_workspace(
    context: RuntimeExecutorContext, *, real_config_path: Path
) -> ExecutorWorkspaceBinding:
    """Bind an OpenClaw agent's workspace to `context.runtime_workspace`.

    Reads `real_config_path` (the real, persistent `openclaw.json`) once,
    read-only. Finds the agent already configured with a `workspace` equal
    to `context.project_workspace` -- the same agent that would be selected
    for a Model A (non-sandboxed) dispatch against this project today.
    Writes a private temp copy of the whole config with only that agent's
    `workspace` field rewritten to `context.runtime_workspace`.

    Raises `ExecutorWorkspaceBindingError` if no such template agent
    exists -- this never registers a new agent to route around a missing
    match, matching the Phase 22C-0 fail-closed posture this layer must
    preserve, not weaken.
    """

    try:
        real_config = json.loads(Path(real_config_path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ExecutorWorkspaceBindingError(
            f"Could not read OpenClaw config at {real_config_path}: {exc}"
        ) from exc

    agent_id = _find_template_agent_id(real_config, context.project_workspace)
    if not agent_id:
        raise ExecutorWorkspaceBindingError(
            "No OpenClaw agent is configured with a workspace matching the "
            f"Project Workspace {context.project_workspace}; refusing to "
            "bind a Runtime Workspace (fail-closed -- no agent identity is "
            "invented by this layer)."
        )

    bound_config = json.loads(json.dumps(real_config))  # cheap deep copy
    for agent in (bound_config.get("agents") or {}).get("list") or []:
        if isinstance(agent, dict) and str(agent.get("id") or "").strip() == agent_id:
            agent["workspace"] = str(context.runtime_workspace)

    tmp_dir = Path(tempfile.mkdtemp(prefix="orchestrator-openclaw-binding-"))
    config_path = tmp_dir / "openclaw.json"
    config_path.write_text(json.dumps(bound_config, indent=2), encoding="utf-8")

    logger.info(
        "[EXECUTOR_WORKSPACE_BINDING] Bound OpenClaw agent %s workspace "
        "%s -> %s for task_execution_id=%s (ephemeral config at %s; "
        "persistent %s untouched)",
        agent_id,
        context.project_workspace,
        context.runtime_workspace,
        context.task_execution_id,
        config_path,
        real_config_path,
    )
    return ExecutorWorkspaceBinding(
        agent_id=agent_id, config_path=config_path, _tmp_dir=tmp_dir
    )
