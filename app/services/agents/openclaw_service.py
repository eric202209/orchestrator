"""OpenClaw Session Service

Integration service for orchestrating AI development tasks via OpenClaw sessions.
Handles session lifecycle, tool execution tracking, and log streaming.
Implements multi-step orchestration workflow: PLANNING → EXECUTING → DEBUGGING → PLAN_REVISION → DONE

OPTIMIZATIONS:
- Reduced planning time through context caching and prompt optimization
- Reduced execution time by minimizing logging overhead
- Added streaming for better user experience
- Implemented request compression
- Enhanced error handling with intelligent recovery

"""

import json
import subprocess
import logging
import asyncio
import contextlib
import hashlib
import os
import shutil
import shlex
import time
import re
import tempfile
import uuid
from types import SimpleNamespace
from typing import Optional, Dict, Any, List, Callable
from datetime import UTC, datetime
from pathlib import Path
import httpx
from sqlalchemy.orm import Session
from app.models import Session as SessionModel, Task, LogEntry, Project
from app.config import settings
from app.services.agents.agent_backends import BackendDescriptor, get_backend_descriptor
from app.services.agents.interfaces import (
    AgentInterfaceDescriptor,
    AgentRuntimeError,
    ContextWindowPolicy,
    RuntimeBackendResult,
    RetryStrategy,
)
from app.services.agents.runtime_adapters.openclaw_adapter import (
    normalize_openclaw_execution_result,
)
from app.services.model_adaptation import resolve_adaptation_profile
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.agents.subprocess_lifecycle import (
    register_process_group,
    unregister_process_group,
    kill_process_group,
)
from app.services.orchestration.task_rules import (
    should_execute_in_canonical_project_root,
)
from app.services.orchestration.validation.git_containment_guard import (
    build_git_containment_env,
    cleanup_git_containment_shim,
)
from app.services.orchestration.execution.executor_workspace_binding import (
    ExecutorWorkspaceBinding,
    ExecutorWorkspaceBindingError,
    bind_openclaw_workspace,
)
from app.services.orchestration.validation.runtime_pollution_guard import (
    detect_runtime_pollution,
    existing_known_scaffold_entries,
    snapshot_top_level_entries,
)
from app.services.orchestration.validation.workspace_guard import (
    has_recent_file_activity,
)
from app.runtime_naming import (
    BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL,
    canonical_diagnostic_label,
    diagnostic_label_alias_details,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.observability import (
    build_text_trace_payload,
    start_langfuse_observation,
    update_langfuse_observation,
)
from app.services.permissions.approval import PermissionApprovalService
from app.services.orchestration.prompt_optimization import (
    optimize_prompt,
    perf_tracker,
)
from app.services.workspace.checkpoint_service import CheckpointService, CheckpointError
from app.services.tasks.tool_tracking import ToolTrackingService
from app.services.workspace.system_settings import (
    get_effective_agent_backend,
    get_effective_agent_model_family,
    get_effective_runtime_root,
)
from app.services.workspace.workspace_paths import (
    HYDRATION_EXCLUDED_NAMES,
    is_executor_runtime_scaffold,
)
from app.services.agents.openclaw_response import (
    channel_metadata as _openclaw_channel_metadata,
    looks_like_openclaw_diagnostic_payload as _looks_like_openclaw_diagnostic_payload,
    parse_openclaw_response as _parse_openclaw_cli_response,
    payload_contains_model_content as _payload_contains_model_content,
    recover_json_like_output_from_stderr as _recover_json_like_output_from_stderr,
    stream_diagnostics_summary as _stream_diagnostics_summary,
    summarize_cli_error as _summarize_cli_error,
    text_contains_model_content as _text_contains_model_content,
)

logger = logging.getLogger(__name__)

OPENCLAW_CLI_LOCK_RETRY_ATTEMPTS = int(
    os.environ.get("ORCHESTRATOR_OPENCLAW_CLI_LOCK_RETRY_ATTEMPTS", "4")
)
OPENCLAW_CLI_LOCK_RETRY_BASE_SECONDS = float(
    os.environ.get("ORCHESTRATOR_OPENCLAW_CLI_LOCK_RETRY_BASE_SECONDS", "0.75")
)
OPENCLAW_CLI_LOCK_MARKERS = (
    "session file locked",
    "sessions.json.lock",
)

# Phase 22C-0: process-lifetime cache for `openclaw --version` diagnostics
# (see OpenClawSessionService._resolve_openclaw_cli_version).
_OPENCLAW_VERSION_CACHE: Dict[tuple, Optional[str]] = {}

_NOISY_OPENCLAW_STDERR_PATTERNS = (
    re.compile(r"^[\[\]{}],?$"),
    re.compile(r'^"payloads":\s*\[$'),
    re.compile(r'^"text":\s*".*",?$'),
    re.compile(r'^"mediaUrl":\s*(null|".*"),?$'),
    re.compile(r'^"meta":\s*{$'),
    re.compile(r'^"durationMs":\s*\d+,?$'),
    re.compile(r'^"agentMeta":\s*{$'),
    re.compile(r'^"sessionId":\s*"[^"]+",?$'),
    re.compile(r'^"sessionKey":\s*"[^"]+",?$'),
    re.compile(r'^"provider":\s*"[^"]+",?$'),
    re.compile(r'^"model":\s*"[^"]+",?$'),
    re.compile(r'^"lastCallUsage":\s*{$'),
    re.compile(r'^"input":\s*\d+,?$'),
    re.compile(r'^"output":\s*\d+,?$'),
    re.compile(r'^"cacheRead":\s*\d+,?$'),
    re.compile(r'^"cacheWrite":\s*\d+,?$'),
    re.compile(r'^"listChars":\s*\d+,?$'),
    re.compile(r'^"tools":\s*{$'),
    re.compile(r'^"propertiesCount":\s*\d+,?$'),
    re.compile(r'^"schemaChars":\s*\d+,?$'),
    re.compile(r'^"summaryChars":\s*\d+,?$'),
    re.compile(r'^"promptChars":\s*\d+,?$'),
    re.compile(r'^"blockChars":\s*\d+,?$'),
    re.compile(r'^"rawChars":\s*\d+,?$'),
    re.compile(r'^"injectedChars":\s*\d+,?$'),
    re.compile(r'^"truncated":\s*(true|false),?$'),
    re.compile(r'^"missing":\s*(true|false),?$'),
    re.compile(r'^"replayInvalid":\s*(true|false),?$'),
    re.compile(r'^"livenessState":\s*"[^"]+",?$'),
    re.compile(r'^"stopReason":\s*"[^"]+",?$'),
    re.compile(r'^"path":\s*".*",?$'),
    re.compile(r'^"name":\s*"[^"]+",?$'),
    re.compile(r'^"entries":\s*\[$'),
    re.compile(r'^"skills":\s*{$'),
    re.compile(
        r"^(?:\x1b\[[0-9;]*m)*\[agents\](?:\x1b\[[0-9;]*m)*\s+"
        r"(?:\x1b\[[0-9;]*m)*synced openai-codex credentials from external cli"
        r"(?:\x1b\[[0-9;]*m)*$"
    ),
)


class OpenClawSessionError(AgentRuntimeError):
    """Custom exception for OpenClaw session errors"""

    runtime_diagnostics: Dict[str, Any] | None = None


class OpenClawNoOutputTimeoutError(OpenClawSessionError):
    """Raised when a one-shot OpenClaw prompt produces no output before its guard."""

    def __init__(self, message: str, diagnostics: Dict[str, Any]):
        super().__init__(message)
        self.runtime_diagnostics = diagnostics


class OpenClawAgentSelectionError(OpenClawSessionError):
    """Raised when no configured OpenClaw agent matches the resolved project
    workspace and Orchestrator refuses to fall back to OpenClaw's default
    agent/workspace (Phase 22C-0 fail-closed containment)."""


class OpenClawSessionService:
    """Service for managing OpenClaw session orchestration"""

    _stream_diagnostics_summary = staticmethod(_stream_diagnostics_summary)
    _looks_like_openclaw_diagnostic_payload = staticmethod(
        _looks_like_openclaw_diagnostic_payload
    )
    _payload_contains_model_content = staticmethod(_payload_contains_model_content)
    _text_contains_model_content = staticmethod(_text_contains_model_content)
    _channel_metadata = staticmethod(_openclaw_channel_metadata)
    _recover_json_like_output_from_stderr = staticmethod(
        _recover_json_like_output_from_stderr
    )
    _summarize_cli_error = staticmethod(_summarize_cli_error)

    def _parse_openclaw_response(self, result: Any) -> Dict[str, Any]:
        return _parse_openclaw_cli_response(result, self._log_entry)

    MAX_PROMPT_LENGTH = 50000  # Leave room for model overhead
    STREAM_READ_LIMIT = 262144  # Allow large JSON/log lines from newer OpenClaw builds

    @staticmethod
    def _is_openclaw_cli_lock_contention(stdout_text: str, stderr_text: str) -> bool:
        combined = f"{stdout_text}\n{stderr_text}".lower()
        return any(marker in combined for marker in OPENCLAW_CLI_LOCK_MARKERS)

    def __init__(
        self,
        db: Session,
        session_id: int,
        task_id: Optional[int] = None,
        use_demo_mode: Optional[bool] = None,
        task_execution_id: Optional[int] = None,
    ):
        self.db = db
        self.session_id = session_id
        self.task_id = task_id
        # Use config value if not explicitly provided
        self.use_demo_mode = (
            use_demo_mode if use_demo_mode is not None else settings.DEMO_MODE
        )
        self.session_model = (
            db.query(SessionModel).filter(SessionModel.id == session_id).first()
        )
        self.task_model = (
            db.query(Task).filter(Task.id == task_id).first() if task_id else None
        )
        self.task_execution_id = task_execution_id
        self.project_id: Optional[int] = None
        # Phase 23C: explicit single-source-of-truth override for the
        # execution cwd, set by dispatch when RUNTIME_WORKSPACE_ENABLED
        # redirects execution into a Task Execution Sandbox. When set,
        # _resolve_execution_cwd() returns it directly instead of
        # re-deriving the project/task workspace path independently.
        self.execution_cwd_override: Optional[str] = None
        # Phase 23D: set only by bind_runtime_workspace(), when a Runtime
        # Executor Context is sandboxed. Overrides where this instance
        # resolves openclaw.json from (an ephemeral, per-invocation copy),
        # never the operator's real ~/.openclaw/openclaw.json.
        self._openclaw_config_path_override: Optional[Path] = None
        self._workspace_binding: Optional[ExecutorWorkspaceBinding] = None
        self.backend_role: Optional[str] = None
        self._safety_prompt_injected = False
        self.openclaw_session_key: Optional[str] = None
        self._task_session_id: Optional[str] = None
        self._last_selected_openclaw_agent_id: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        self.backend_descriptor: BackendDescriptor = get_backend_descriptor(
            get_effective_agent_backend(settings.AGENT_BACKEND, db=db)
        )
        # Initialize checkpoint service
        from app.services.workspace.checkpoint_service import CheckpointService

        self.checkpoint_service = CheckpointService(db)

    def get_backend_metadata(self) -> Dict[str, Any]:
        """Return normalized backend metadata for logs, APIs, and orchestration."""

        model_family = self._model_family_for_role()
        adaptation_profile = resolve_adaptation_profile(
            backend=self.backend_descriptor.name,
            model_family=model_family,
        )
        return {
            "backend": self.backend_descriptor.name,
            "display_name": self.backend_descriptor.display_name,
            "implementation": self.backend_descriptor.implementation,
            "model_family": model_family,
            "adaptation_profile": adaptation_profile.name,
            "agent_interface": self.describe_interface().to_dict(),
            "capabilities": self.backend_descriptor.capabilities.to_dict(),
        }

    def _model_family_for_role(self) -> str:
        role_model = ""
        if self.backend_role == "planning":
            role_model = settings.PLANNER_MODEL
        elif self.backend_role == "debug_repair":
            role_model = settings.DEBUG_REPAIR_MODEL
        elif self.backend_role == "execution":
            role_model = settings.EXECUTION_MODEL or settings.OLLAMA_AGENT_MODEL
        return str(role_model or "").strip() or get_effective_agent_model_family(
            settings.AGENT_MODEL, db=self.db
        )

    def normalize_execution_result(
        self,
        result: Dict[str, Any],
        *,
        role: str = "execution",
        duration_seconds: float = 0.0,
    ) -> RuntimeBackendResult:
        """Normalize OpenClaw execution output into the shared backend contract."""

        return normalize_openclaw_execution_result(
            result,
            backend_id=self.backend_descriptor.name,
            role=role,
            duration_seconds=duration_seconds,
        )

    def describe_interface(self) -> AgentInterfaceDescriptor:
        model_family = self._model_family_for_role()
        profile = resolve_adaptation_profile(
            backend=self.backend_descriptor.name,
            model_family=model_family,
        )
        return AgentInterfaceDescriptor(
            backend=self.backend_descriptor.name,
            model_family=model_family,
            planning_prompt_template="assemble_planning_prompt",
            execution_prompt_template="assemble_execution_prompt",
            prompt_dialect=profile.prompt_dialect,
            tool_capability_map={
                "shell": True,
                "filesystem": True,
                "checkpoint_resume": bool(
                    self.backend_descriptor.capabilities.supports_checkpoint_resume
                ),
                "streaming": bool(
                    self.backend_descriptor.capabilities.supports_streaming
                ),
            },
            tool_shape=profile.tool_shape,
            preferred_retry_strategy=RetryStrategy(
                planning="minimal_prompt_then_repair",
                execution="compact_prompt_then_debug",
                completion="repair_step_then_revalidate",
            ),
            context_window_policy=ContextWindowPolicy(
                max_input_tokens=self.backend_descriptor.capabilities.max_context_tokens,
                overflow_strategy="retry_compact",
                compaction_strategy=profile.context_window_policy,
            ),
        )

    def build_cli_agent_command(
        self,
        prompt: str,
        *,
        source_brain: str = "local",
        timeout_seconds: int = 180,
        session_prefix: str = "planning",
    ) -> List[str]:
        """Build a one-shot CLI agent command for synchronous planning-style tasks."""

        cmd = self._resolve_openclaw_command()
        runtime_session_key = f"{session_prefix}-{int(time.time())}"
        if session_prefix.startswith("planning"):
            runtime_session_key += f"-{uuid.uuid4().hex[:12]}"
        full_cmd = self._build_openclaw_agent_command(
            cmd, cwd=self._resolve_execution_cwd()
        )
        if source_brain == "local":
            full_cmd.append("--local")
        full_cmd.extend(
            [
                "--session-id",
                runtime_session_key,
                "--message",
                prompt,
                "--json",
                "--timeout",
                str(timeout_seconds),
            ]
        )
        return full_cmd

    def _openclaw_config_path(self) -> Path:
        # Phase 23D: an active runtime-workspace binding takes priority --
        # it points at an ephemeral, per-invocation config copy, never at
        # the real ~/.openclaw/openclaw.json (see bind_runtime_workspace()).
        config_path_override = getattr(self, "_openclaw_config_path_override", None)
        if config_path_override:
            return config_path_override
        configured = os.environ.get("OPENCLAW_CONFIG_PATH", "").strip()
        if configured:
            return Path(configured).expanduser()
        state_dir = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
        if state_dir:
            return Path(state_dir).expanduser() / "openclaw.json"
        return Path.home() / ".openclaw" / "openclaw.json"

    @staticmethod
    def _paths_same(left: str, right: str) -> bool:
        try:
            return (
                Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
            )
        except Exception:
            return False

    def _find_openclaw_agent_for_workspace(self, cwd: Optional[str]) -> Optional[str]:
        if not cwd:
            return None
        config_path = self._openclaw_config_path()
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        agents = (config.get("agents") or {}).get("list") or []
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            agent_id = str(agent.get("id") or "").strip()
            workspace = str(agent.get("workspace") or "").strip()
            if agent_id and workspace and self._paths_same(workspace, cwd):
                return agent_id
        return None

    def _build_openclaw_agent_command(
        self, base_command: List[str], *, cwd: Optional[str]
    ) -> List[str]:
        """Select the OpenClaw agent whose configured workspace matches ``cwd``.

        Phase 22C-0: when ``cwd`` resolves to a real project directory but no
        configured OpenClaw agent's workspace matches it, this used to
        silently omit ``--agent`` and let OpenClaw fall back to its default
        agent/workspace -- the exact mechanism behind the Phase 22A wrong-
        workspace S1 (a task marked DONE while its artifact landed under
        OpenClaw's own default workspace instead of the project). Refuse to
        dispatch instead.
        """

        full_cmd = [*base_command, "agent"]
        agent_id = self._find_openclaw_agent_for_workspace(cwd)
        if agent_id:
            full_cmd.extend(["--agent", agent_id])
            self._last_selected_openclaw_agent_id = agent_id
            return full_cmd

        self._last_selected_openclaw_agent_id = None
        if cwd:
            error = (
                "No OpenClaw agent is configured with a workspace matching the "
                f"resolved project directory: {cwd}. Refusing to fall back to "
                "OpenClaw's default agent/workspace (Phase 22C-0 fail-closed "
                "containment). Register an OpenClaw agent whose `workspace` "
                "equals this path, or route this task to a different backend."
            )
            self._log_entry("ERROR", f"[OPENCLAW] {error}", commit=True)
            raise OpenClawAgentSelectionError(error)
        return full_cmd

    def bind_runtime_workspace(self, context: Optional[Any]) -> None:
        """Bind this session's OpenClaw execution to a Runtime Executor
        Context (Phase 23D, Goal 3).

        No-op when ``context`` is ``None`` or not sandboxed (Model A) --
        the existing static ``openclaw.json`` match against the Project
        Workspace already works unchanged. When sandboxed, resolves an
        ephemeral, per-invocation config copy whose template agent's
        ``workspace`` is rewritten to the Runtime Workspace, and points
        this instance's config resolution at it. Fails closed
        (``OpenClawAgentSelectionError``) if no template agent matches the
        Project Workspace -- never falls back to the Project Workspace or
        a default agent, and never invents a new agent identity.
        """
        if context is None or not getattr(context, "is_sandboxed", False):
            return
        real_config_path = self._openclaw_config_path()
        try:
            self._workspace_binding = bind_openclaw_workspace(
                context, real_config_path=real_config_path
            )
        except ExecutorWorkspaceBindingError as exc:
            raise OpenClawAgentSelectionError(str(exc)) from exc
        self._openclaw_config_path_override = self._workspace_binding.config_path

    def release_runtime_workspace_binding(self) -> None:
        """Discard the ephemeral config copy from ``bind_runtime_workspace``.

        Never raises -- safe to call unconditionally from a `finally` block,
        including when no binding was ever established.
        """
        if getattr(self, "_workspace_binding", None) is None:
            return
        self._workspace_binding.release()
        self._workspace_binding = None
        self._openclaw_config_path_override = None

    def _runtime_result_contract(self) -> Dict[str, Any]:
        project_workspace = None
        project_id = None
        if self.task_model and self.task_model.project_id:
            project_id = self.task_model.project_id
            project_model = (
                self.db.query(Project).filter(Project.id == project_id).first()
            )
            if project_model is not None:
                project_workspace = str(
                    resolve_project_workspace_path(
                        project_model.workspace_path, project_model.name
                    ).resolve()
                )
        runtime_workspace = self.execution_cwd_override or self._resolve_execution_cwd()
        return {
            "schema": "openclaw.runtime_result.v1",
            "executor": self.backend_descriptor.name,
            "project_id": project_id,
            "task_id": self.task_id,
            "task_execution_id": self.task_execution_id,
            "project_workspace": project_workspace,
            "runtime_workspace": runtime_workspace,
            "runtime_workspace_enabled": bool(self.execution_cwd_override),
            "openclaw_config_path": (
                str(self._openclaw_config_path_override)
                if self._openclaw_config_path_override
                else None
            ),
        }

    def _apply_workspace_binding_env(self, env: Dict[str, str]) -> Dict[str, str]:
        """Propagate an active runtime-workspace binding's config path to a
        subprocess's environment via ``OPENCLAW_CONFIG_PATH``.

        The parent process resolves the same ephemeral config via
        ``_openclaw_config_path()`` (for agent selection); the child
        `openclaw` CLI process must resolve the identical file so the agent
        id passed with ``--agent`` maps to the Runtime Workspace there too,
        not the real, persistent config.
        """
        config_path_override = getattr(self, "_openclaw_config_path_override", None)
        if config_path_override:
            env["OPENCLAW_CONFIG_PATH"] = str(config_path_override)
        return env

    @staticmethod
    def _extract_reported_workspace_dir(*texts: str) -> Optional[str]:
        combined = "\n".join(text for text in texts if text)
        if not combined:
            return None
        pattern = re.compile(r'"workspaceDir"\s*:\s*"([^"]+)"')
        match = pattern.search(combined)
        return match.group(1) if match else None

    @staticmethod
    def _workspace_dir_is_under_project_root(
        reported_workspace_dir: str, project_root: str
    ) -> bool:
        try:
            reported = Path(reported_workspace_dir).expanduser().resolve()
            root = Path(project_root).expanduser().resolve()
            reported.relative_to(root)
            return True
        except Exception:
            return False

    def _apply_reported_workspace_guard(
        self,
        result: Dict[str, Any],
        *,
        reported_workspace_dir: Optional[str],
        expected_project_root: Optional[str],
        execution_started_at_epoch: Optional[float] = None,
    ) -> Dict[str, Any]:
        if reported_workspace_dir:
            result["reported_workspace_dir"] = reported_workspace_dir
        if result.get("status") != "completed":
            return result

        if (
            reported_workspace_dir
            and expected_project_root
            and not self._workspace_dir_is_under_project_root(
                reported_workspace_dir, expected_project_root
            )
        ):
            error = (
                "OpenClaw reported workspaceDir outside the resolved project root: "
                f"{reported_workspace_dir} (expected under {expected_project_root})"
            )
            self._log_entry("ERROR", f"[OPENCLAW] {error}", commit=True)
            return {
                **result,
                "status": "failed",
                "error": error,
                "workspace_contract_failed": True,
                "expected_project_root": expected_project_root,
            }

        if not reported_workspace_dir and expected_project_root:
            # Phase 22C-0: a completed result with no workspaceDir evidence
            # must not be accepted on the absence of evidence alone (RCA F5:
            # "a completion whose sole workspace evidence is absent is
            # treated as compliant"). Fall back to positive evidence -- did
            # any file under the expected project root actually change
            # during this invocation -- before accepting.
            has_file_evidence = False
            if execution_started_at_epoch is not None:
                try:
                    has_file_evidence = has_recent_file_activity(
                        Path(expected_project_root), execution_started_at_epoch
                    )
                except Exception:
                    has_file_evidence = False
            result["workspace_evidence_source"] = (
                "file_activity_fallback" if has_file_evidence else "none"
            )
            if not has_file_evidence:
                error = (
                    "OpenClaw completed without reporting workspaceDir, and no "
                    "file activity was detected under the expected project root "
                    f"({expected_project_root}) during execution. Missing "
                    "workspace evidence is not treated as success (Phase 22C-0 "
                    "containment)."
                )
                self._log_entry("ERROR", f"[OPENCLAW] {error}", commit=True)
                return {
                    **result,
                    "status": "failed",
                    "error": error,
                    "workspace_contract_failed": True,
                    "workspace_evidence_missing": True,
                    "expected_project_root": expected_project_root,
                }

        return result

    def _record_runtime_pollution(
        self,
        result: Dict[str, Any],
        *,
        expected_project_root: Optional[str],
        pre_execution_top_level: set,
    ) -> None:
        """Attach runtime-pollution diagnostics to ``result`` (mutates in place).

        Phase 22C-0: detects unexpected top-level artifacts left by this run
        via a before/after diff (not solely the known-scaffold-name list --
        see `runtime_pollution_guard`), plus a separate check for known
        OpenClaw scaffold names already present on disk (persists across
        runs; a single-run diff cannot re-surface pollution an earlier run
        already wrote). This only detects and reports -- it never deletes or
        modifies anything in the project workspace.
        """

        if not expected_project_root:
            return
        try:
            root = Path(expected_project_root)
            post_top_level = snapshot_top_level_entries(root)
            pollution = detect_runtime_pollution(
                before=pre_execution_top_level, after=post_top_level
            )
            pollution["existing_known_scaffold_entries"] = (
                existing_known_scaffold_entries(root)
            )
        except Exception:
            return

        result["runtime_pollution"] = pollution
        if pollution.get("known_scaffold_matches"):
            self._log_entry(
                "WARN",
                "[OPENCLAW][RUNTIME_POLLUTION] New OpenClaw runtime scaffold "
                f"files appeared in the project root this run: "
                f"{pollution['known_scaffold_matches']}. Phase 22C-0 detects "
                "and reports this; it does not delete anything.",
                commit=True,
            )
        elif pollution.get("unclassified_new_entries"):
            self._log_entry(
                "INFO",
                "[OPENCLAW][RUNTIME_POLLUTION] New top-level entries appeared "
                f"in the project root this run: "
                f"{pollution['unclassified_new_entries']}. May be legitimate "
                "task output; recorded for investigation.",
                commit=True,
            )

    @staticmethod
    def _openclaw_invocation_metadata(
        *,
        full_cmd: List[str],
        prompt: str,
        timeout_seconds: int,
        cwd: Optional[str],
        invocation_kind: str,
        isolate_workspace_context: bool = False,
        no_output_timeout_seconds: Optional[int] = None,
        expected_project_root: Optional[str] = None,
        openclaw_version: Optional[str] = None,
        git_containment_active: bool = False,
    ) -> Dict[str, Any]:
        """Return comparable OpenClaw subprocess metadata without logging prompt text."""

        try:
            agent_index = full_cmd.index("agent")
        except ValueError:
            agent_index = 1 if len(full_cmd) > 1 else 0

        executable_parts = full_cmd[:agent_index] or full_cmd[:1]
        args = full_cmd[agent_index:]
        flags: Dict[str, Any] = {}
        redacted_args: List[str] = []
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--message":
                flags[token] = "<redacted>"
                redacted_args.extend([token, "<redacted>"])
                index += 2
                continue
            if token.startswith("--"):
                next_value = args[index + 1] if index + 1 < len(args) else None
                if next_value is not None and not str(next_value).startswith("--"):
                    flags[token] = next_value
                    redacted_args.extend([token, str(next_value)])
                    index += 2
                else:
                    flags[token] = True
                    redacted_args.append(token)
                    index += 1
                continue
            redacted_args.append(token)
            index += 1

        session_id = str(flags.get("--session-id") or "")
        session_id_shape = re.sub(r"\d", "0", session_id)
        session_id_prefix = (
            session_id.rsplit("-", 1)[0] if "-" in session_id else session_id
        )

        return {
            "invocation_kind": invocation_kind,
            "executable_path": executable_parts[0] if executable_parts else None,
            "executable_args": executable_parts[1:],
            "subcommand": "agent" if "agent" in args else (args[0] if args else None),
            "args_redacted": redacted_args,
            "has_local_flag": "--local" in flags,
            "has_json_flag": "--json" in flags,
            "timeout_arg": str(flags.get("--timeout") or timeout_seconds),
            "session_id_prefix": session_id_prefix,
            "session_id_shape": session_id_shape,
            "cwd": cwd,
            "isolate_workspace_context": isolate_workspace_context,
            "prompt_size": len(prompt or ""),
            "prompt_sha256_12": hashlib.sha256(
                (prompt or "").encode("utf-8")
            ).hexdigest()[:12],
            "no_output_timeout_seconds": no_output_timeout_seconds,
            # Phase 22C-0 diagnostics: make selected agent, resolved project
            # root, OpenClaw version, and git containment status directly
            # readable from run-start identity without reconstructing them
            # from args_redacted.
            "selected_agent": flags.get("--agent"),
            "expected_project_root": expected_project_root,
            "openclaw_version": openclaw_version,
            "git_containment_active": git_containment_active,
        }

    @staticmethod
    def _estimate_token_count(text: str) -> int:
        """Return a deterministic rough token estimate for diagnostics only."""

        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)

    @staticmethod
    def _is_bounded_debug_repair_diagnostic_label(
        diagnostic_label: Optional[str],
    ) -> bool:
        return (
            canonical_diagnostic_label(diagnostic_label)
            == BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL
        )

    @staticmethod
    def _diagnostic_label_architecture(
        diagnostic_label: Optional[str],
    ) -> Optional[str]:
        if OpenClawSessionService._is_bounded_debug_repair_diagnostic_label(
            diagnostic_label
        ):
            return BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL
        return None

    @staticmethod
    def _diagnostic_invocation_kind(diagnostic_label: Optional[str]) -> str:
        """Classify labeled OpenClaw calls without changing the CLI invocation."""

        if OpenClawSessionService._is_bounded_debug_repair_diagnostic_label(
            diagnostic_label
        ):
            return "debug_repair"
        return "planning"

    @staticmethod
    def _diagnostic_timeout_boundary(diagnostic_label: Optional[str]) -> str:
        """Name the owner of an OpenClaw wait timeout for runtime diagnostics."""

        if OpenClawSessionService._is_bounded_debug_repair_diagnostic_label(
            diagnostic_label
        ):
            return "debug_repair_wait_for"
        return "planning_wait_for"

    @staticmethod
    def _diagnostic_text_tail(text: str, max_chars: int = 500) -> str:
        """Return a bounded, redacted stream tail for timeout diagnostics."""

        if not text:
            return ""
        tail = text[-max_chars:]
        tail = re.sub(
            r"(?i)(api[_-]?key|access[_-]?token|secret|password|bearer)\s*[:=]\s*"
            r"['\"]?[^'\"\s,}]+",
            r"\1=<redacted>",
            tail,
        )
        tail = re.sub(
            r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+",
            "bearer <redacted>",
            tail,
        )
        return tail

    def _should_use_structured_debug_repair_direct_chat(
        self, diagnostic_label: Optional[str]
    ) -> bool:
        """Route bounded debug repair through direct chat when configured.

        The diagnostic label remains the historical Phase 7F marker for existing
        reports. New configuration and metadata use the architecture name.
        """

        if not self._is_bounded_debug_repair_diagnostic_label(diagnostic_label):
            return False
        if not settings.DEBUG_REPAIR_DIRECT_ENABLED:
            return False
        return self.backend_descriptor.name == "local_openclaw"

    @staticmethod
    def _debug_repair_direct_config() -> Dict[str, str]:
        """Resolve direct debug repair settings with planning-repair fallbacks."""

        return {
            "backend": (
                settings.DEBUG_REPAIR_BACKEND or settings.REPAIR_BACKEND or ""
            ).strip(),
            "base_url": (
                settings.DEBUG_REPAIR_BASE_URL
                or settings.PLANNING_REPAIR_BASE_URL
                or ""
            ).rstrip("/"),
            "model": (settings.DEBUG_REPAIR_MODEL or settings.PLANNING_REPAIR_MODEL),
            "api_key": settings.DEBUG_REPAIR_API_KEY
            or settings.PLANNING_REPAIR_API_KEY,
        }

    @staticmethod
    def _debug_repair_disable_thinking() -> bool:
        return bool(settings.DEBUG_REPAIR_DISABLE_THINKING)

    @staticmethod
    def _debug_repair_direct_payload(prompt: str, model: str) -> Dict[str, Any]:
        """Build the direct structured-repair payload."""

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 2048,
            "stream": False,
        }
        if OpenClawSessionService._debug_repair_disable_thinking():
            payload["think"] = False
            payload["enable_thinking"] = False
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    @staticmethod
    def _debug_repair_responses_payload(prompt: str, model: str) -> Dict[str, Any]:
        """Build an OpenAI Responses API payload for direct debug repair."""

        return {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
        }

    @staticmethod
    def _extract_responses_output_text(body: Any) -> str:
        """Extract text from an OpenAI Responses API body."""

        if not isinstance(body, dict):
            return ""
        output_text = body.get("output_text")
        if isinstance(output_text, str):
            return output_text

        parts: list[str] = []
        output = body.get("output")
        if not isinstance(output, list):
            return ""
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if not isinstance(content_item, dict):
                    continue
                text = content_item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _extract_chat_completion_content(body: Any) -> str:
        """Extract assistant content from an OpenAI-compatible chat response."""

        if not isinstance(body, dict):
            return ""
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        return ""

    async def _execute_structured_debug_repair_direct_call(
        self,
        prompt: str,
        *,
        timeout_seconds: int,
        diagnostic_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        config = self._debug_repair_direct_config()
        base_url = config["base_url"]
        model = config["model"]
        if not base_url or not model:
            raise OpenClawSessionError(
                "Debug repair direct call is enabled but base URL or model is not configured"
            )

        uses_responses_api = config["backend"] == "openai_responses_api"
        if uses_responses_api:
            payload = self._debug_repair_responses_payload(prompt, model)
            url = f"{base_url}/responses"
            timeout_boundary = "debug_repair_responses_api"
            backend_id = "debug_repair_openai_responses_api"
        else:
            payload = self._debug_repair_direct_payload(prompt, model)
            url = f"{base_url}/chat/completions"
            timeout_boundary = "debug_repair_direct_chat"
            backend_id = "debug_repair_direct_chat_completions"

        headers = {"Content-Type": "application/json"}
        api_key = config["api_key"].strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        started_at = time.monotonic()
        self._log_entry(
            "INFO",
            "[DEBUG_REPAIR_DIRECT] attempting direct structured repair "
            f"model={model} timeout={timeout_seconds}s prompt_chars={len(prompt or '')}",
            metadata=json.dumps(
                {
                    **diagnostic_label_alias_details(
                        BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL
                    ),
                    "architecture_lane": "debug_repair",
                    "invocation_kind": "debug_repair",
                    "timeout_boundary": timeout_boundary,
                    "prompt_chars": len(prompt or ""),
                    "prompt_sha256_12": hashlib.sha256(
                        (prompt or "").encode("utf-8")
                    ).hexdigest()[:12],
                    "backend": backend_id,
                    "responses_api": uses_responses_api,
                    "disable_thinking": self._debug_repair_disable_thinking(),
                    **(diagnostic_metadata or {}),
                }
            ),
        )

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            duration_seconds = time.monotonic() - started_at
            error = OpenClawSessionError(
                f"Debug repair direct call failed: {type(exc).__name__}: {str(exc)[:200]}"
            )
            error.runtime_diagnostics = {
                **diagnostic_label_alias_details(BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL),
                "architecture_lane": "debug_repair",
                "invocation_kind": "debug_repair",
                "timeout_boundary": timeout_boundary,
                "backend": backend_id,
                "responses_api": uses_responses_api,
                "direct_chat_completions": not uses_responses_api,
                "disable_thinking": self._debug_repair_disable_thinking(),
                "duration_seconds": round(duration_seconds, 3),
                "timed_out": isinstance(
                    exc, (httpx.TimeoutException, asyncio.TimeoutError)
                ),
            }
            raise error

        if uses_responses_api:
            output = self._extract_responses_output_text(body)
        else:
            output = self._extract_chat_completion_content(body)
        duration_seconds = time.monotonic() - started_at
        choices = body.get("choices") if isinstance(body, dict) else None
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        message = first_choice.get("message") if isinstance(first_choice, dict) else {}
        reasoning = message.get("reasoning") if isinstance(message, dict) else None
        finish_reason = (
            first_choice.get("finish_reason")
            if isinstance(first_choice, dict)
            else None
        )

        diagnostics = {
            **diagnostic_label_alias_details(BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL),
            "architecture_lane": "debug_repair",
            "invocation_kind": "debug_repair",
            "timeout_boundary": timeout_boundary,
            "backend": backend_id,
            "responses_api": uses_responses_api,
            "direct_chat_completions": not uses_responses_api,
            "disable_thinking": self._debug_repair_disable_thinking(),
            "duration_seconds": round(duration_seconds, 3),
            "timeout_seconds": timeout_seconds,
            "finish_reason": finish_reason,
            "content_chars": len(output or ""),
            "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
            "usage": body.get("usage") if isinstance(body, dict) else None,
        }
        self._log_entry(
            "INFO",
            "[DEBUG_REPAIR_DIRECT] completed direct structured repair "
            f"duration={duration_seconds:.1f}s output_chars={len(output or '')} "
            f"reasoning_chars={diagnostics['reasoning_chars']}",
            metadata=json.dumps(diagnostics),
        )

        if not output.strip():
            error = OpenClawSessionError(
                "Debug repair direct call returned empty content"
            )
            error.runtime_diagnostics = diagnostics
            raise error

        return {
            "status": "completed",
            "output": output,
            "logs": [],
            "backend": backend_id,
            "model_family": model,
            "diagnostics": diagnostics,
        }

    async def _run_cli_prompt_with_diagnostics(
        self,
        full_cmd: List[str],
        *,
        timeout_seconds: int,
        cwd: Optional[str],
        prompt: str = "",
        invocation_kind: str = "prompt",
        isolate_workspace_context: bool = False,
        no_output_timeout_seconds: Optional[int] = None,
    ) -> tuple[subprocess.CompletedProcess[str], Dict[str, Any]]:
        if cwd is None and (
            self.task_model is not None or self.session_model is not None
        ):
            raise OpenClawSessionError(
                "Refusing to run OpenClaw without a resolved project workspace cwd"
            )

        started_at = time.monotonic()
        first_output_at: Optional[float] = None
        last_output_at: Optional[float] = None
        previous_output_at: Optional[float] = None
        max_silent_gap: Optional[float] = None
        stdout_chunks: List[str] = []
        stderr_chunks: List[str] = []
        first_output_event = asyncio.Event()
        response_ready_event = asyncio.Event()
        response_boundary_reached = False

        expected_project_root = self._resolve_project_root_for_workspace_guard()
        subprocess_env, git_guard_shim_dir = build_git_containment_env()
        subprocess_env = self._apply_workspace_binding_env(subprocess_env)

        diagnostics: Dict[str, Any] = {
            "timeout_seconds": timeout_seconds,
            "timeout_with_cleanup_seconds": timeout_seconds + 30,
            "no_output_timeout_seconds": no_output_timeout_seconds,
            "no_output_timeout": False,
            "timed_out": False,
            "cancelled": False,
            "return_code": None,
            "stream_stalled": None,
            "truncated": False,
            "timeout_boundary": None,
            "response_boundary_reached": False,
            "response_cleanup_return_code": None,
            "invocation": self._openclaw_invocation_metadata(
                full_cmd=full_cmd,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                cwd=cwd,
                invocation_kind=invocation_kind,
                isolate_workspace_context=isolate_workspace_context,
                no_output_timeout_seconds=no_output_timeout_seconds,
                expected_project_root=expected_project_root,
                git_containment_active=git_guard_shim_dir is not None,
            ),
        }

        try:
            subprocess_start_started_at = time.monotonic()
            process = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.STREAM_READ_LIMIT,
                cwd=cwd,
                env=subprocess_env,
                start_new_session=True,
            )
        except BaseException:
            cleanup_git_containment_shim(git_guard_shim_dir)
            raise
        register_process_group(process.pid)
        subprocess_started_at = time.monotonic()
        diagnostics["process_pid"] = process.pid
        diagnostics["subprocess_start_seconds"] = round(
            subprocess_started_at - subprocess_start_started_at, 3
        )
        diagnostics["subprocess_started_after_seconds"] = round(
            subprocess_started_at - started_at, 3
        )

        async def collect_stream(stream, chunks: List[str], stream_name: str) -> None:
            nonlocal first_output_at, last_output_at, previous_output_at, max_silent_gap
            while True:
                line = await stream.readline()
                if not line:
                    break

                now = time.monotonic()
                if first_output_at is None:
                    first_output_at = now
                    first_output_event.set()
                if previous_output_at is not None:
                    gap = now - previous_output_at
                    max_silent_gap = (
                        gap if max_silent_gap is None else max(max_silent_gap, gap)
                    )
                previous_output_at = now
                last_output_at = now

                line_text = line.decode("utf-8", errors="replace").strip()
                chunks.append(line_text)

                # A one-shot CLI can leave a reader or descendant alive after
                # it has emitted the complete gateway response.  The parser
                # already treats structured model content as the valid final
                # response contract, so use that boundary to begin normal
                # process-group cleanup instead of waiting for EOF.
                stdout_text = "\n".join(filter(None, stdout_chunks)).strip()
                stderr_text = "\n".join(filter(None, stderr_chunks)).strip()
                response_received = self._text_contains_model_content(stdout_text)
                if not response_received:
                    response_received = bool(
                        self._recover_json_like_output_from_stderr(stderr_text)
                    )
                if response_received:
                    response_ready_event.set()

                if (
                    stream_name == "stderr"
                    and line_text
                    and self._should_emit_stderr_line(line_text)
                ):
                    self._log_entry("WARN", line_text, commit=True)

        stream_task = asyncio.ensure_future(
            asyncio.gather(
                collect_stream(process.stdout, stdout_chunks, "stdout"),
                collect_stream(process.stderr, stderr_chunks, "stderr"),
            )
        )
        try:
            if no_output_timeout_seconds:
                first_output_task = asyncio.create_task(first_output_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {first_output_task, stream_task},
                        timeout=no_output_timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if first_output_task not in done and stream_task not in done:
                        no_output_elapsed = time.monotonic() - started_at
                        diagnostics["no_output_timeout"] = True
                        diagnostics["timed_out"] = True
                        diagnostics["cancelled"] = True
                        diagnostics["timeout_boundary"] = "repair_no_output"
                        diagnostics["no_output_timeout_elapsed_seconds"] = round(
                            no_output_elapsed, 3
                        )
                        kill_process_group(process.pid)
                        await process.wait()
                        diagnostics["return_code"] = process.returncode
                        stream_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await stream_task
                        raise OpenClawNoOutputTimeoutError(
                            (
                                "OpenClaw prompt produced no output before "
                                f"{no_output_timeout_seconds}s"
                            ),
                            diagnostics,
                        )
                finally:
                    first_output_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await first_output_task
            response_ready_task = asyncio.create_task(response_ready_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {stream_task, response_ready_task},
                    timeout=timeout_seconds + 30,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise asyncio.TimeoutError
                response_boundary_reached = (
                    response_ready_task in done and not stream_task.done()
                )
                if response_boundary_reached and not stream_task.done():
                    diagnostics["response_boundary_reached"] = True
                    kill_process_group(process.pid)
                    await process.wait()
                    diagnostics["response_cleanup_return_code"] = process.returncode
                    await asyncio.wait_for(stream_task, timeout=5)
                else:
                    await stream_task
                    await asyncio.wait_for(process.wait(), timeout=timeout_seconds + 30)
                return_code = process.returncode
            finally:
                response_ready_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await response_ready_task
            diagnostics["return_code"] = return_code
            if response_boundary_reached:
                # Cleanup may necessarily signal the CLI after the successful
                # response.  Keep the established successful response
                # contract while retaining the actual signal result above.
                diagnostics["return_code"] = 0
        except asyncio.TimeoutError:
            diagnostics["timed_out"] = True
            diagnostics["timeout_boundary"] = (
                diagnostics.get("timeout_boundary") or "process_timeout"
            )
            kill_process_group(process.pid)
            await process.wait()
            diagnostics["return_code"] = process.returncode
            raise
        except asyncio.CancelledError:
            diagnostics["cancelled"] = True
            diagnostics["timeout_boundary"] = (
                diagnostics.get("timeout_boundary") or "caller_cancelled"
            )
            kill_process_group(process.pid)
            await process.wait()
            diagnostics["return_code"] = process.returncode
            raise
        finally:
            unregister_process_group(process.pid)
            stdout_text = "\n".join(filter(None, stdout_chunks)).strip()
            stderr_text = "\n".join(filter(None, stderr_chunks)).strip()
            duration_seconds = time.monotonic() - started_at
            channel_metadata = self._channel_metadata(stdout_text, stderr_text)
            diagnostics.update(
                {
                    "duration_seconds": round(duration_seconds, 3),
                    "first_output_after_seconds": (
                        None
                        if first_output_at is None
                        else round(first_output_at - started_at, 3)
                    ),
                    "last_output_after_seconds": (
                        None
                        if last_output_at is None
                        else round(last_output_at - started_at, 3)
                    ),
                    "max_silent_gap_seconds": (
                        None if max_silent_gap is None else round(max_silent_gap, 3)
                    ),
                    "stdout_chars": len(stdout_text),
                    "stderr_chars": len(stderr_text),
                    "stdout_lines": len([line for line in stdout_chunks if line]),
                    "stderr_lines": len([line for line in stderr_chunks if line]),
                    **channel_metadata,
                    "output_token_estimate": self._estimate_token_count(
                        f"{stdout_text}\n{stderr_text}".strip()
                    ),
                    "stream_stalled": bool(
                        first_output_at is not None
                        and last_output_at is not None
                        and (time.monotonic() - last_output_at) >= 10
                    ),
                    "truncated": "truncated" in f"{stdout_text}\n{stderr_text}".lower(),
                }
            )
            cleanup_git_containment_shim(git_guard_shim_dir)
            self._log_entry(
                "INFO",
                "[OPENCLAW][REPAIR_DIAGNOSTICS] "
                + self._stream_diagnostics_summary(diagnostics),
                metadata=json.dumps(diagnostics),
                commit=True,
            )

        stdout_text = "\n".join(filter(None, stdout_chunks)).strip()
        stderr_text = "\n".join(filter(None, stderr_chunks)).strip()
        return (
            subprocess.CompletedProcess(
                args=full_cmd,
                returncode=int(diagnostics.get("return_code") or 0),
                stdout=stdout_text,
                stderr=stderr_text,
            ),
            diagnostics,
        )

    def parse_cli_response(
        self, proc: subprocess.CompletedProcess[str]
    ) -> Dict[str, Any]:
        """Normalize subprocess output into the same payload shape as streaming execution."""

        return self._parse_openclaw_response(proc)

    def reports_context_overflow(self, result: Optional[Dict[str, Any]]) -> bool:
        """Detect overflow errors for the local OpenClaw CLI/runtime."""

        return self._is_context_overflow_result(result)

    def _resolve_openclaw_command(self) -> List[str]:
        """Resolve the OpenClaw CLI command with fallback locations."""
        configured_args = shlex.split((settings.OPENCLAW_CLI_ARGS or "").strip())
        configured_path = (settings.OPENCLAW_CLI_PATH or "").strip()
        candidates: List[str] = []

        if configured_path:
            candidates.append(configured_path)

        detected_path = shutil.which("openclaw")
        if detected_path:
            candidates.append(detected_path)

        candidates.extend(
            [
                "/usr/local/bin/openclaw",
                "/usr/bin/openclaw",
                str(Path.home() / ".local" / "bin" / "openclaw"),
                "/root/.local/bin/openclaw",
            ]
        )

        for candidate in dict.fromkeys(candidates):
            if (
                candidate
                and os.path.isfile(candidate)
                and os.access(candidate, os.X_OK)
            ):
                return [candidate, *configured_args]

        node_executable = shutil.which("node")
        node_entrypoints = [
            "/opt/openclaw/dist/index.js",
            "/root/.openclaw/app/dist/index.js",
        ]
        for entrypoint in node_entrypoints:
            if node_executable and os.path.isfile(entrypoint):
                return [node_executable, entrypoint, *configured_args]

        raise OpenClawSessionError(
            "OpenClaw CLI not found. Install `openclaw`, add it to PATH, set "
            "`OPENCLAW_CLI_PATH`, or configure `OPENCLAW_CLI_ARGS` for a Node entrypoint."
        )

    def _resolve_openclaw_cli_version(self) -> Optional[str]:
        """Best-effort `openclaw --version` for run-start identity diagnostics.

        Phase 22C-0 (RCA F5.3): the integration works by inference (config
        scraping, stdout scraping) with no versioned contract; recording the
        resolved CLI version at least makes format drift diagnosable after
        the fact instead of invisible. Cached per resolved command for the
        life of the process so this never adds per-task subprocess latency
        beyond the first call (Orchestrator priority: fewer, cheaper calls on
        the execution hot path).
        """

        try:
            base_command = self._resolve_openclaw_command()
        except Exception:
            return None
        cache_key = tuple(base_command)
        if cache_key in _OPENCLAW_VERSION_CACHE:
            return _OPENCLAW_VERSION_CACHE[cache_key]
        try:
            proc = subprocess.run(
                [*base_command, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = (proc.stdout or proc.stderr or "").strip()
            version = output.splitlines()[0][:200] if output else None
        except Exception:
            version = None
        _OPENCLAW_VERSION_CACHE[cache_key] = version
        return version

    def _resolve_execution_cwd(self) -> Optional[str]:
        """Resolve the best working directory for OpenClaw subprocess execution."""
        if self.execution_cwd_override:
            return self.execution_cwd_override
        try:
            project_model = None
            if self.project_id is not None:
                project_model = (
                    self.db.query(Project).filter(Project.id == self.project_id).first()
                )
            elif self.session_model and self.session_model.project_id:
                project_model = (
                    self.db.query(Project)
                    .filter(Project.id == self.session_model.project_id)
                    .first()
                )
            elif self.task_model and self.task_model.project_id:
                project_model = (
                    self.db.query(Project)
                    .filter(Project.id == self.task_model.project_id)
                    .first()
                )

            if not project_model:
                return None

            project_workspace = resolve_project_workspace_path(
                project_model.workspace_path, project_model.name
            )

            if self.task_model and should_execute_in_canonical_project_root(
                self.task_model,
                getattr(self.task_model, "execution_profile", None),
                getattr(self.task_model, "title", None),
                getattr(self.task_model, "description", None),
            ):
                return str(project_workspace.resolve())

            if self.task_model and self.task_model.task_subfolder:
                return str(
                    (project_workspace / self.task_model.task_subfolder).resolve()
                )

            return str(project_workspace.resolve())
        except Exception as exc:
            self._log_entry(
                "WARN",
                f"[OPENCLAW] Failed to resolve execution cwd, falling back to default: {exc}",
            )
            return None

    def _resolve_project_root_for_workspace_guard(self) -> Optional[str]:
        if self.execution_cwd_override:
            # Phase 23C: when execution is redirected into a Task Execution
            # Sandbox, the containment guard (Phase 22C-0) must validate
            # OpenClaw's reported workspaceDir against the Runtime Workspace
            # it was actually dispatched into, not the Project Workspace --
            # otherwise every runtime-workspace execution would trip the
            # guard as a false positive. The guard itself stays fully active.
            return self.execution_cwd_override
        try:
            project_model = None
            if self.project_id is not None:
                project_model = (
                    self.db.query(Project).filter(Project.id == self.project_id).first()
                )
            elif self.session_model and self.session_model.project_id:
                project_model = (
                    self.db.query(Project)
                    .filter(Project.id == self.session_model.project_id)
                    .first()
                )
            elif self.task_model and self.task_model.project_id:
                project_model = (
                    self.db.query(Project)
                    .filter(Project.id == self.task_model.project_id)
                    .first()
                )
            if not project_model:
                return None
            return str(
                resolve_project_workspace_path(
                    project_model.workspace_path, project_model.name
                ).resolve()
            )
        except Exception:
            return None

    def _append_runtime_event(
        self, event_type: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Best-effort typed runtime event append for local OpenClaw flows."""

        if not self.task_model:
            return

        project_dir = self._resolve_execution_cwd()
        if not project_dir:
            return
        if (
            self.task_model
            and not should_execute_in_canonical_project_root(
                self.task_model,
                getattr(self.task_model, "execution_profile", None),
                getattr(self.task_model, "title", None),
                getattr(self.task_model, "description", None),
            )
            and getattr(self.task_model, "task_subfolder", None)
        ):
            project_dir = str(
                (Path(project_dir) / str(self.task_model.task_subfolder)).resolve()
            )

        try:
            append_orchestration_event(
                project_dir=project_dir,
                session_id=self.session_id,
                task_id=self.task_model.id,
                event_type=event_type,
                details=details or {},
            )
        except Exception:
            pass

    @staticmethod
    def _should_emit_stderr_line(line_text: str) -> bool:
        """Hide raw OpenClaw JSON telemetry from live logs while keeping it in buffers."""

        trimmed = (line_text or "").strip()
        if not trimmed:
            return False

        if (
            '"propertiesCount"' in trimmed
            or '"schemaChars"' in trimmed
            or '"summaryChars"' in trimmed
            or '"promptChars"' in trimmed
            or '"blockChars"' in trimmed
            or '"rawChars"' in trimmed
            or '"injectedChars"' in trimmed
            or '"bootstrapTotalMaxChars"' in trimmed
            or '"bootstrapTruncation"' in trimmed
            or '"systemPromptReport"' in trimmed
            or '"injectedWorkspaceFiles"' in trimmed
            or '"payloads"' in trimmed
            or '"mediaUrl"' in trimmed
            or '"agentMeta"' in trimmed
            or '"durationMs"' in trimmed
            or '"lastCallUsage"' in trimmed
            or '"cacheRead"' in trimmed
            or '"cacheWrite"' in trimmed
            or '"listChars"' in trimmed
            or '"tools"' in trimmed
            or '"replayInvalid"' in trimmed
            or '"livenessState"' in trimmed
            or '"stopReason"' in trimmed
        ):
            return False

        return not any(
            pattern.match(trimmed) for pattern in _NOISY_OPENCLAW_STDERR_PATTERNS
        )

    async def create_session(
        self, task_description: str, context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Create a new OpenClaw session for task execution

        Args:
            task_description: Description of the task to execute
            context: Additional context (previous logs, project info, etc.)

        Returns:
            OpenClaw session key

        Raises:
            OpenClawSessionError: If session creation fails
        """
        try:
            # Log session creation (commit immediately so WS stream shows it)
            self._log_entry(
                "INFO",
                f"Creating OpenClaw session for task: {task_description[:100]}",
                commit=True,
            )

            # Use the main OpenClaw session that already exists
            self.openclaw_session_key = "agent:main:main"

            # Generate a stable session ID for this task run; all steps share it
            # so the AI agent retains context across the entire task lifecycle.
            task_id_str = str(self.task_id or self.session_id)
            self._task_session_id = (
                f"orchestrator-task-{task_id_str}-{int(time.time())}"
            )

            self._log_entry(
                "INFO",
                f"✅ OpenClaw session set to: {self.openclaw_session_key}",
                commit=True,
            )

            return self.openclaw_session_key

        except Exception as e:
            error_msg = f"Failed to create OpenClaw session: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)

    async def create_openclaw_session(
        self, task_description: str, context: Optional[Dict[str, Any]] = None
    ) -> str:
        """Backward-compatible alias for older callers."""

        return await self.create_session(task_description, context=context)

    async def execute_task(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        log_callback: Optional[Callable[..., None]] = None,
        *,
        reuse_task_session: bool = True,
        diagnostic_label: Optional[str] = None,
        diagnostic_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a task via OpenClaw session (legacy single-mode)

        OPTIMIZATIONS:
        - Optimize prompt to reduce planning time
        - Compress context to reduce token usage
        - Track performance metrics

        Args:
            prompt: The prompt/task to execute
            timeout_seconds: Maximum execution time
            log_callback: Optional callback for real-time log streaming

        Returns:
            Execution result with logs and status

        Raises:
            OpenClawSessionError: If execution fails
        """
        with start_langfuse_observation(
            name="openclaw-agent-request",
            as_type="generation",
            input=build_text_trace_payload(prompt),
            metadata={
                "backend": self.backend_descriptor.name,
                "session_id": self.session_id,
                "task_id": self.task_id,
                "reuse_task_session": reuse_task_session,
                "demo_mode": self.use_demo_mode,
            },
            model=self._model_family_for_role(),
        ) as observation:
            try:
                # OPTIMIZATION: Track start time
                perf_tracker.start("execute_task")
                start_time = time.time()

                # OPTIMIZATION: Optimize prompt to reduce planning time
                optimized_prompt = optimize_prompt(prompt, max_tokens=25000)

                # Check if we should use demo mode or real execution
                if self.use_demo_mode:
                    # DEMO MODE: Return mock logs (for UI testing)
                    result = await self._execute_demo_mode(optimized_prompt)
                    # Demo mode always completes successfully (by design)
                    result["status"] = "completed"
                elif self._should_use_structured_debug_repair_direct_chat(
                    diagnostic_label
                ):
                    result = await self._execute_structured_debug_repair_direct_call(
                        optimized_prompt,
                        timeout_seconds=timeout_seconds,
                        diagnostic_metadata={
                            **(diagnostic_metadata or {}),
                            "original_prompt_size": len(prompt or ""),
                            "optimized_prompt_size": len(optimized_prompt or ""),
                        },
                    )
                else:
                    # REAL MODE: Execute task via OpenClaw HTTP API
                    diagnostics_kwargs: Dict[str, Any] = {}
                    if diagnostic_label:
                        diagnostics_kwargs = {
                            "diagnostic_label": diagnostic_label,
                            "diagnostic_metadata": {
                                **(diagnostic_metadata or {}),
                                "original_prompt_size": len(prompt or ""),
                                "optimized_prompt_size": len(optimized_prompt or ""),
                            },
                        }
                    result = await self.execute_task_with_streaming(
                        optimized_prompt,
                        timeout_seconds,
                        log_callback,
                        reuse_task_session=reuse_task_session,
                        **diagnostics_kwargs,
                    )
                    if self._is_context_overflow_result(result):
                        retry_prompt = optimize_prompt(
                            prompt,
                            max_tokens=700,
                            hard_char_limit=1800,
                        )
                        if retry_prompt != optimized_prompt:
                            retry_diagnostics_kwargs: Dict[str, Any] = {}
                            if diagnostic_label:
                                retry_diagnostics_kwargs = {
                                    "diagnostic_label": diagnostic_label,
                                    "diagnostic_metadata": {
                                        **(diagnostic_metadata or {}),
                                        "original_prompt_size": len(prompt or ""),
                                        "optimized_prompt_size": len(
                                            retry_prompt or ""
                                        ),
                                        "context_overflow_retry": True,
                                    },
                                }
                            self._log_entry(
                                "WARN",
                                "[OPENCLAW] Context overflow detected; retrying once with a compact prompt",
                            )
                            result = await self.execute_task_with_streaming(
                                retry_prompt,
                                timeout_seconds,
                                log_callback,
                                reuse_task_session=reuse_task_session,
                                **retry_diagnostics_kwargs,
                            )

                # OPTIMIZATION: Log performance metrics
                duration = time.time() - start_time
                self._log_entry(
                    "INFO",
                    f"[PERFORMANCE] Task executed in {duration:.2f}s (optimized prompt)",
                )

                result_status = result.get("status")
                if result_status == "completed":
                    self._log_entry(
                        "INFO",
                        "[OPENCLAW] Request returned output; awaiting orchestration validation",
                    )
                elif result_status == "failed":
                    self._log_entry(
                        "ERROR",
                        f"[OPENCLAW] Request failed before orchestration validation: "
                        f"{result.get('error', 'Execution failed')}",
                    )
                else:
                    self._log_entry(
                        "WARNING",
                        f"[OPENCLAW] Request returned unexpected status before orchestration validation: "
                        f"{result_status or 'unknown'}",
                    )

                result.setdefault("backend", self.backend_descriptor.name)
                result.setdefault("model_family", self._model_family_for_role())
                result.setdefault(
                    "backend_capabilities",
                    self.backend_descriptor.capabilities.to_dict(),
                )
                update_langfuse_observation(
                    observation,
                    output=build_text_trace_payload(
                        result.get("output") or result.get("error") or result_status
                    ),
                    metadata={
                        "status": result_status,
                        "backend": self.backend_descriptor.name,
                    },
                    level="ERROR" if result_status == "failed" else None,
                    status_message=(
                        str(result.get("error") or "")[:500]
                        if result_status == "failed"
                        else None
                    ),
                )
                return result

            except Exception as e:
                error_msg = f"Task execution failed: {str(e)}"
                self._log_entry("ERROR", error_msg)
                update_langfuse_observation(
                    observation,
                    level="ERROR",
                    status_message=error_msg[:500],
                    output={"status": "failed", "reason": "exception"},
                )

                wrapped_error = OpenClawSessionError(error_msg)
                diagnostics = getattr(e, "runtime_diagnostics", None)
                if isinstance(diagnostics, dict):
                    wrapped_error.runtime_diagnostics = diagnostics
                raise wrapped_error

    @staticmethod
    def _is_context_overflow_result(result: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(result, dict):
            return False

        candidates = [
            str(result.get("error") or ""),
            str(result.get("output") or ""),
        ]
        lowered = "\n".join(candidates).lower()
        markers = (
            "context window exceeded",
            "context size has been exceeded",
            "context length exceeded",
            "prompt is too long for the model",
        )
        return any(marker in lowered for marker in markers)

    async def _check_and_request_permission(
        self,
        operation_type: str,
        target_path: Optional[str] = None,
        command: Optional[str] = None,
        description: Optional[str] = None,
    ) -> bool:
        """
        Check if permission is required and request if needed

        Args:
            operation_type: Type of operation
            target_path: Target path
            command: Shell command
            description: User-friendly description

        Returns:
            True if permission granted or not required
        """
        if not self.session_model or not self.session_model.project_id:
            return True  # No project context, skip permission check

        try:
            permission_service = PermissionApprovalService(self.db)

            # Check if permission is required
            if not permission_service.check_permission_required(
                operation_type, target_path
            ):
                self._log_entry(
                    "INFO", f"Permission not required for: {operation_type}"
                )
                return True

            # Check if already granted
            if permission_service.is_permission_granted(
                self.session_model.project_id,
                operation_type,
                target_path or "",
                self.session_model.id,
            ):
                self._log_entry(
                    "INFO", f"Permission already granted for: {operation_type}"
                )
                return True

            # Request permission
            self._log_entry(
                "WARN",
                f"Permission required for: {operation_type} on {target_path or command}",
            )

            permission = permission_service.create_permission_request(
                project_id=self.session_model.project_id,
                session_id=self.session_model.id,
                task_id=self.task_model.id if self.task_model else None,
                operation_type=operation_type,
                target_path=target_path,
                command=command,
                description=description,
                expires_in_minutes=30,
            )

            self._log_entry(
                "INFO",
                f"Permission request created: {permission.id}, waiting for approval",
            )
            self._append_runtime_event(
                EventType.WAITING_FOR_INPUT,
                {
                    "kind": "permission_request",
                    "permission_request_id": permission.id,
                    "operation_type": operation_type,
                    "target_path": target_path,
                    "command": command,
                    "description": description,
                },
            )

            # Return False to indicate permission is pending
            return False

        except (ValueError, TypeError, AttributeError) as e:
            self._log_entry(
                "ERROR",
                f"Permission check failed and operation was allowed: {str(e)}",
            )
            # Fail open - allow operation if permission system fails
            return True

    async def _execute_demo_mode(self, prompt: str) -> Dict[str, Any]:
        """
        Demo mode: Return mock logs for UI testing

        Args:
            prompt: Task prompt

        Returns:
            Mock execution result
        """
        # Get recent logs from database
        recent_logs = (
            self.db.query(LogEntry)
            .filter(LogEntry.session_id == self.session_id)
            .order_by(LogEntry.created_at.desc())
            .limit(10)
            .all()
        )

        self._log_entry("INFO", "Running in DEMO MODE - no actual task execution")

        return {
            "status": "completed",
            "mode": "demo",
            "output": f"Demo execution of: {prompt[:50]}...",
            "logs": [
                {
                    "level": log.level,
                    "message": log.message,
                    "timestamp": log.created_at.isoformat(),
                }
                for log in recent_logs
            ],
            "execution_time": 0.0,
            "note": "Demo mode - no actual task was executed. Enable real mode to execute via OpenClaw API.",
        }

    async def stream_logs(self, callback: callable) -> None:
        """
        Stream logs from OpenClaw session to callback

        Args:
            callback: Function to receive log entries (log_level, message, metadata)
        """
        try:
            self._log_entry("INFO", "Starting log streaming")

            # TODO: Implement actual log streaming
            # This would read from OpenClaw session logs and emit via callback

            while True:
                # Simulate log stream
                await callback(
                    "INFO", "Log stream active...", {"session_id": self.session_id}
                )
                # Break for now (would be infinite loop in production)
                break

        except Exception as e:
            self._log_entry("ERROR", f"Log streaming failed: {str(e)}")
            raise

    async def execute_task_with_streaming(
        self,
        prompt: str,
        timeout_seconds: int = 300,
        log_callback: Optional[Callable] = None,
        *,
        reuse_task_session: bool = True,
        diagnostic_label: Optional[str] = None,
        diagnostic_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute task via OpenClaw CLI with real-time log streaming (optimized)"""

        # Reuse the stable per-task session ID set in create_openclaw_session so
        # all steps in this task share one AI context window.
        task_id_str = str(self.task_id or self.session_id)
        if reuse_task_session:
            new_session_id = (
                self._task_session_id
                or f"orchestrator-task-{task_id_str}-{int(time.time())}"
            )
        else:
            new_session_id = (
                f"orchestrator-task-{task_id_str}-fresh-{int(time.time() * 1000)}"
            )

        git_guard_shim_dir = None
        try:
            openclaw_command = self._resolve_openclaw_command()
            execution_cwd = self._resolve_execution_cwd()
            full_cmd = self._build_openclaw_agent_command(
                openclaw_command, cwd=execution_cwd
            )

            # Phase 22C-0 containment setup: resolve the identity this run is
            # accountable to, snapshot the project root for pollution
            # detection, and install the git-mutation-blocking PATH shim.
            # All best-effort / non-fatal -- a containment-setup problem must
            # not itself block a task that would otherwise dispatch fine.
            expected_project_root = (
                self._resolve_project_root_for_workspace_guard() or execution_cwd
            )
            pre_execution_top_level: set = set()
            if expected_project_root:
                pre_execution_top_level = snapshot_top_level_entries(
                    Path(expected_project_root)
                )
            execution_started_at_epoch = time.time()
            subprocess_env, git_guard_shim_dir = build_git_containment_env()
            subprocess_env = self._apply_workspace_binding_env(subprocess_env)
            openclaw_version = self._resolve_openclaw_cli_version()

            full_cmd.extend(
                [
                    "--local",
                    "--session-id",
                    new_session_id,
                    "--message",
                    prompt,
                    "--json",
                    "--timeout",
                    str(timeout_seconds),
                ]
            )
            started_at = time.monotonic()
            first_output_at: Optional[float] = None
            last_output_at: Optional[float] = None
            previous_output_at: Optional[float] = None
            max_silent_gap: Optional[float] = None
            return_code: Optional[int] = None
            timed_out = False
            cancelled = False
            timeout_boundary: Optional[str] = None
            process: Optional[asyncio.subprocess.Process] = None
            process_pid: Optional[int] = None
            subprocess_start_seconds: Optional[float] = None
            subprocess_started_after_seconds: Optional[float] = None

            stdout_chunks: List[str] = []
            stderr_chunks: List[str] = []
            cli_lock_diagnostics: Dict[str, Any] = {}
            invocation_kind = self._diagnostic_invocation_kind(diagnostic_label)

            async def stream_output(
                stream,
                level: str,
                chunks: List[str],
                emit_live_logs: bool = True,
            ) -> None:
                nonlocal first_output_at, last_output_at
                nonlocal previous_output_at, max_silent_gap
                while True:
                    line = await stream.readline()
                    if not line:
                        break

                    now = time.monotonic()
                    if first_output_at is None:
                        first_output_at = now
                    if previous_output_at is not None:
                        gap = now - previous_output_at
                        max_silent_gap = (
                            gap if max_silent_gap is None else max(max_silent_gap, gap)
                        )
                    previous_output_at = now
                    last_output_at = now

                    line_text = line.decode("utf-8", errors="replace").strip()
                    chunks.append(line_text)

                    if line_text:
                        if emit_live_logs:
                            if level == "WARN" and not self._should_emit_stderr_line(
                                line_text
                            ):
                                continue
                            # Commit each streamed line so the session websocket,
                            # which polls the database, can surface it immediately.
                            self._log_entry(level, line_text, commit=True)

                            if log_callback:
                                await log_callback(level, line_text)

            try:
                max_lock_retry_attempts = max(1, OPENCLAW_CLI_LOCK_RETRY_ATTEMPTS)
                for cli_attempt in range(1, max_lock_retry_attempts + 1):
                    process = None
                    stdout_chunks.clear()
                    stderr_chunks.clear()
                    return_code = None
                    subprocess_start_started_at = time.monotonic()
                    process = await asyncio.create_subprocess_exec(
                        *full_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=self.STREAM_READ_LIMIT,
                        cwd=execution_cwd,
                        env=subprocess_env,
                        start_new_session=True,
                    )
                    register_process_group(process.pid)
                    subprocess_started_at = time.monotonic()
                    process_pid = process.pid
                    subprocess_start_seconds = (
                        subprocess_started_at - subprocess_start_started_at
                    )
                    subprocess_started_after_seconds = (
                        subprocess_started_at - started_at
                    )

                    await asyncio.wait_for(
                        asyncio.gather(
                            # OpenClaw emits its final machine-readable JSON on stdout.
                            # Buffer it for parsing, but don't flood Live Logs with raw JSON lines.
                            stream_output(
                                process.stdout,
                                "INFO",
                                stdout_chunks,
                                emit_live_logs=False,
                            ),
                            # Keep stderr visible because it contains actionable warnings/errors.
                            stream_output(
                                process.stderr,
                                "WARN",
                                stderr_chunks,
                                emit_live_logs=True,
                            ),
                        ),
                        timeout=timeout_seconds + 30,
                    )

                    return_code = await asyncio.wait_for(
                        process.wait(), timeout=timeout_seconds + 30
                    )
                    unregister_process_group(process.pid)
                    stdout_text = "\n".join(filter(None, stdout_chunks)).strip()
                    stderr_text = "\n".join(filter(None, stderr_chunks)).strip()
                    if (
                        return_code != 0
                        and cli_attempt < max_lock_retry_attempts
                        and self._is_openclaw_cli_lock_contention(
                            stdout_text, stderr_text
                        )
                    ):
                        retry_delay = min(
                            10.0, OPENCLAW_CLI_LOCK_RETRY_BASE_SECONDS * cli_attempt
                        )
                        cli_lock_diagnostics = {
                            "openclaw_cli_lock_retry_attempt": cli_attempt,
                            "openclaw_cli_lock_retry_attempts": cli_attempt,
                            "openclaw_cli_lock_retry_max_attempts": (
                                max_lock_retry_attempts
                            ),
                            "openclaw_cli_lock_retry_delay_seconds": round(
                                retry_delay, 3
                            ),
                        }
                        self._log_entry(
                            "WARN",
                            (
                                "[OPENCLAW] Local session file was locked; "
                                f"retrying CLI call in {retry_delay:.2f}s "
                                f"(attempt {cli_attempt + 1}/{max_lock_retry_attempts})"
                            ),
                            metadata=json.dumps(
                                {
                                    "event_type": "openclaw_cli_lock_retry",
                                    **cli_lock_diagnostics,
                                }
                            ),
                            commit=True,
                        )
                        await asyncio.sleep(retry_delay)
                        continue
                    if cli_attempt > 1:
                        cli_lock_diagnostics = {
                            **cli_lock_diagnostics,
                            "openclaw_cli_lock_retry_attempts": cli_attempt - 1,
                        }
                    break
            except asyncio.TimeoutError:
                timed_out = True
                timeout_boundary = self._diagnostic_timeout_boundary(diagnostic_label)
                try:
                    if process is not None:
                        kill_process_group(process.pid)
                        await process.wait()
                        return_code = process.returncode
                except Exception as exc:
                    logger.debug(
                        "[OPENCLAW] Failed to terminate timed out process cleanly: %s",
                        exc,
                    )
                stdout_text = "\n".join(filter(None, stdout_chunks)).strip()
                stderr_text = "\n".join(filter(None, stderr_chunks)).strip()
                timeout_error = OpenClawSessionError(
                    f"Task timed out after {timeout_seconds}s"
                )
                timeout_error.runtime_diagnostics = {
                    **(diagnostic_metadata or {}),
                    "diagnostic_label": diagnostic_label,
                    "diagnostic_label_architecture": (
                        self._diagnostic_label_architecture(diagnostic_label)
                    ),
                    "timeout_seconds": timeout_seconds,
                    "timeout_with_cleanup_seconds": timeout_seconds + 30,
                    "duration_seconds": round(time.monotonic() - started_at, 3),
                    "stdout_chars": len(stdout_text),
                    "stderr_chars": len(stderr_text),
                    "stdout_lines": len([line for line in stdout_chunks if line]),
                    "stderr_lines": len([line for line in stderr_chunks if line]),
                    "stdout_tail": self._diagnostic_text_tail(stdout_text),
                    "stderr_tail": self._diagnostic_text_tail(stderr_text),
                    **self._channel_metadata(stdout_text, stderr_text),
                    **cli_lock_diagnostics,
                    "timed_out": True,
                    "cancelled": False,
                    "return_code": return_code,
                    "process_pid": process_pid,
                    "subprocess_start_seconds": (
                        None
                        if subprocess_start_seconds is None
                        else round(subprocess_start_seconds, 3)
                    ),
                    "subprocess_started_after_seconds": (
                        None
                        if subprocess_started_after_seconds is None
                        else round(subprocess_started_after_seconds, 3)
                    ),
                    "timeout_boundary": timeout_boundary,
                }
                raise timeout_error
            except asyncio.CancelledError:
                cancelled = True
                timeout_boundary = "caller_cancelled"
                try:
                    if process is not None:
                        kill_process_group(process.pid)
                        await process.wait()
                        return_code = process.returncode
                except Exception as exc:
                    logger.debug(
                        "[OPENCLAW] Failed to terminate cancelled process cleanly: %s",
                        exc,
                    )
                raise
            finally:
                if diagnostic_label:
                    stdout_text = "\n".join(filter(None, stdout_chunks)).strip()
                    stderr_text = "\n".join(filter(None, stderr_chunks)).strip()
                    duration_seconds = time.monotonic() - started_at
                    truncated = "truncated" in (f"{stdout_text}\n{stderr_text}".lower())
                    channel_metadata = self._channel_metadata(stdout_text, stderr_text)
                    diagnostics: Dict[str, Any] = {
                        **(diagnostic_metadata or {}),
                        "diagnostic_label": diagnostic_label,
                        "diagnostic_label_architecture": (
                            self._diagnostic_label_architecture(diagnostic_label)
                        ),
                        "timeout_seconds": timeout_seconds,
                        "timeout_with_cleanup_seconds": timeout_seconds + 30,
                        "planning_prompt_size": len(prompt or ""),
                        "planning_duration": round(duration_seconds, 3),
                        "duration_seconds": round(duration_seconds, 3),
                        "first_output_after_seconds": (
                            None
                            if first_output_at is None
                            else round(first_output_at - started_at, 3)
                        ),
                        "last_output_after_seconds": (
                            None
                            if last_output_at is None
                            else round(last_output_at - started_at, 3)
                        ),
                        "max_silent_gap_seconds": (
                            None if max_silent_gap is None else round(max_silent_gap, 3)
                        ),
                        "stdout_chars": len(stdout_text),
                        "stderr_chars": len(stderr_text),
                        "stdout_lines": len([line for line in stdout_chunks if line]),
                        "stderr_lines": len([line for line in stderr_chunks if line]),
                        "stdout_tail": self._diagnostic_text_tail(stdout_text),
                        "stderr_tail": self._diagnostic_text_tail(stderr_text),
                        **channel_metadata,
                        **cli_lock_diagnostics,
                        "output_token_estimate": self._estimate_token_count(
                            f"{stdout_text}\n{stderr_text}".strip()
                        ),
                        "stream_stalled": bool(
                            first_output_at is not None
                            and last_output_at is not None
                            and (time.monotonic() - last_output_at) >= 10
                        ),
                        "truncated": truncated,
                        "truncated_output_detected": truncated,
                        "timed_out": timed_out,
                        "cancelled": cancelled,
                        "return_code": return_code,
                        "process_pid": process_pid,
                        "subprocess_start_seconds": (
                            None
                            if subprocess_start_seconds is None
                            else round(subprocess_start_seconds, 3)
                        ),
                        "subprocess_started_after_seconds": (
                            None
                            if subprocess_started_after_seconds is None
                            else round(subprocess_started_after_seconds, 3)
                        ),
                        "timeout_boundary": timeout_boundary,
                        "contract_violation_type": None,
                        "invocation": self._openclaw_invocation_metadata(
                            full_cmd=full_cmd,
                            prompt=prompt,
                            timeout_seconds=timeout_seconds,
                            cwd=execution_cwd,
                            invocation_kind=invocation_kind,
                            isolate_workspace_context=False,
                            no_output_timeout_seconds=None,
                            expected_project_root=expected_project_root,
                            openclaw_version=openclaw_version,
                            git_containment_active=git_guard_shim_dir is not None,
                        ),
                    }
                    self._log_entry(
                        "INFO",
                        f"[OPENCLAW][{diagnostic_label}_DIAGNOSTICS] "
                        + self._stream_diagnostics_summary(diagnostics),
                        metadata=json.dumps(diagnostics),
                        commit=True,
                    )
            stdout_text = "\n".join(filter(None, stdout_chunks)).strip()
            stderr_text = "\n".join(filter(None, stderr_chunks)).strip()

            self._log_entry(
                "INFO",
                f"[OPENCLAW] Return code: {return_code}, stdout_len: {len(stdout_text)}, stderr_len: {len(stderr_text)}",
                commit=True,
            )

            completed = subprocess.CompletedProcess(
                args=full_cmd,
                returncode=return_code,
                stdout=stdout_text,
                stderr=stderr_text,
            )
            result = self._parse_openclaw_response(completed)
            result["openclaw_version"] = openclaw_version
            result["selected_openclaw_agent"] = self._last_selected_openclaw_agent_id
            result["runtime_result"] = {
                **self._runtime_result_contract(),
                "openclaw_version": openclaw_version,
                "selected_openclaw_agent": self._last_selected_openclaw_agent_id,
            }
            self._record_runtime_pollution(
                result,
                expected_project_root=expected_project_root,
                pre_execution_top_level=pre_execution_top_level,
            )
            return self._apply_reported_workspace_guard(
                result,
                reported_workspace_dir=self._extract_reported_workspace_dir(
                    stdout_text, stderr_text
                ),
                expected_project_root=expected_project_root,
                execution_started_at_epoch=execution_started_at_epoch,
            )

        except asyncio.TimeoutError:
            try:
                if process is not None:
                    kill_process_group(process.pid)
                    await process.wait()
            except Exception as exc:
                logger.debug(
                    "[OPENCLAW] Failed to terminate timed out process cleanly: %s",
                    exc,
                )

            raise OpenClawSessionError(f"Task timed out after {timeout_seconds}s")

        except Exception as e:
            self._log_entry("ERROR", f"Real mode execution failed: {str(e)}")
            raise
        finally:
            cleanup_git_containment_shim(git_guard_shim_dir)

    async def track_tool_execution(
        self, tool_name: str, params: Dict[str, Any], result: Any, success: bool
    ) -> None:
        """
        Track tool execution for audit trail

        Args:
            tool_name: Name of the tool executed
            params: Tool parameters
            result: Tool execution result
            success: Whether execution was successful
        """
        try:
            tool_log = {
                "tool": tool_name,
                "params": params,
                "result": str(result)[:1000],  # Truncate long results
                "success": success,
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": self.session_id,
                "task_id": self.task_id,
            }

            level = "INFO" if success else "ERROR"
            message = (
                f"Tool '{tool_name}' executed {'successfully' if success else 'failed'}"
            )

            self._log_entry(level, message, metadata=json.dumps(tool_log))

        except Exception as e:
            logger.error(f"Failed to track tool execution: {str(e)}")

    async def get_session_context(self) -> Dict[str, Any]:
        """
        Get current session context for LLM prompts

        Returns:
            Dictionary with session state, recent logs, task info
        """
        # Get recent log entries
        recent_logs = (
            self.db.query(LogEntry)
            .filter(LogEntry.session_id == self.session_id)
            .order_by(LogEntry.created_at.desc())
            .limit(20)
            .all()
        )

        logs_data = [
            {
                "level": log.level,
                "message": log.message,
                "timestamp": log.created_at.isoformat(),
                "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
            }
            for log in recent_logs
        ]

        # Get task context if available
        task_context = None
        if self.task_model:
            task_context = {
                "id": self.task_model.id,
                "title": self.task_model.title,
                "status": self.task_model.status.value,
                "description": self.task_model.description,
                "steps": (
                    json.loads(self.task_model.steps) if self.task_model.steps else None
                ),
                "current_step": self.task_model.current_step,
            }

        return {
            "session_id": self.session_id,
            "session_name": (
                self.session_model.name
                if self.session_model
                else (
                    self.task_model.project.name
                    if self.task_model and self.task_model.project
                    else "Unknown"
                )
            ),
            "task": task_context,
            "recent_logs": logs_data,
            "openclaw_session_key": self.openclaw_session_key,
            "project_workspace_path": (
                # Phase 23D-1: when execution is redirected into a Task
                # Execution Sandbox, verify_workspace_contract's pre-execution
                # check compares this field against the sandboxed
                # expected_root; reuse the same Phase 23C override the
                # post-execution guard already honors so both checks agree
                # on which root is authoritative for this dispatch.
                self.execution_cwd_override
                or (
                    str(
                        resolve_project_workspace_path(
                            self.task_model.project.workspace_path,
                            self.task_model.project.name,
                        ).resolve()
                    )
                    if self.task_model
                    and self.task_model.project
                    and self.task_model.project.workspace_path
                    else None
                )
            ),
            "task_workspace_path": self._resolve_execution_cwd(),
            "execution_cwd": self._resolve_execution_cwd(),
        }

    async def invoke_prompt(
        self,
        prompt: str,
        *,
        timeout_seconds: int = 180,
        source_brain: str = "local",
        session_prefix: str = "planning",
        isolate_workspace_context: bool = False,
        no_output_timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Run a one-shot prompt using the CLI request path."""
        planning_temp_dir = None
        previous_cwd_override = self.execution_cwd_override
        try:
            if session_prefix == "planning" and self.project_id is not None:
                project = (
                    self.db.query(Project).filter(Project.id == self.project_id).first()
                )
                if project is None:
                    raise OpenClawSessionError(
                        f"Planning project {self.project_id} was not found"
                    )
                project_workspace = resolve_project_workspace_path(
                    project.workspace_path, project.name
                ).resolve()
                planning_root = (
                    get_effective_runtime_root(self.db)
                    / "planning"
                    / str(self.project_id)
                )
                planning_root.mkdir(parents=True, exist_ok=True)
                planning_temp_dir = tempfile.TemporaryDirectory(
                    prefix="invocation-", dir=planning_root
                )
                runtime_workspace = Path(planning_temp_dir.name).resolve()

                def _ignore_runtime_scaffold(
                    source_dir: str, names: List[str]
                ) -> set[str]:
                    ignored = {
                        name for name in names if name in HYDRATION_EXCLUDED_NAMES
                    }
                    agents_path = Path(source_dir) / "AGENTS.md"
                    if "AGENTS.md" in names and is_executor_runtime_scaffold(
                        agents_path
                    ):
                        ignored.add("AGENTS.md")
                    return ignored

                shutil.copytree(
                    project_workspace,
                    runtime_workspace,
                    ignore=_ignore_runtime_scaffold,
                    dirs_exist_ok=True,
                )
                context = SimpleNamespace(
                    executor="openclaw",
                    runtime_workspace=runtime_workspace,
                    project_workspace=project_workspace,
                    project_id=self.project_id,
                    task_execution_id=None,
                    is_sandboxed=True,
                )
                self.execution_cwd_override = str(runtime_workspace)
                self.bind_runtime_workspace(context)

            full_cmd = self.build_cli_agent_command(
                prompt,
                source_brain=source_brain,
                timeout_seconds=timeout_seconds,
                session_prefix=session_prefix,
            )
            isolated_temp_dir = None
            if isolate_workspace_context:
                isolated_temp_dir = tempfile.TemporaryDirectory(
                    prefix="openclaw-planning-repair-"
                )
                cwd = isolated_temp_dir.name
            else:
                cwd = self._resolve_execution_cwd()
            try:
                proc, diagnostics = await self._run_cli_prompt_with_diagnostics(
                    full_cmd,
                    timeout_seconds=timeout_seconds,
                    cwd=cwd,
                    prompt=prompt,
                    invocation_kind=session_prefix,
                    isolate_workspace_context=isolate_workspace_context,
                    no_output_timeout_seconds=no_output_timeout_seconds,
                )
            finally:
                if isolated_temp_dir is not None:
                    isolated_temp_dir.cleanup()
        except subprocess.TimeoutExpired as exc:
            raise OpenClawSessionError(
                f"Prompt invocation timed out after {timeout_seconds}s"
            ) from exc
        except asyncio.TimeoutError as exc:
            raise OpenClawSessionError(
                f"Prompt invocation timed out after {timeout_seconds}s"
            ) from exc
        except OpenClawNoOutputTimeoutError:
            raise
        except asyncio.CancelledError:
            raise
        finally:
            if planning_temp_dir is not None:
                self.release_runtime_workspace_binding()
                self.execution_cwd_override = previous_cwd_override
                planning_temp_dir.cleanup()
        result = self.parse_cli_response(proc)
        result["runtime_diagnostics"] = diagnostics
        return result

    def _log_entry(
        self,
        level: str,
        message: str,
        metadata: Optional[str] = None,
        commit: bool = False,
    ) -> LogEntry:
        """Create database log entry with instance tracking

        Args:
            level: Log level (INFO, WARN, ERROR, etc.)
            message: Log message
            metadata: Optional metadata
            commit: If True, commit immediately. If False, batch commit (default False)

        Returns:
            LogEntry object
        """
        # Get instance_id from session if available
        session_instance_id = None
        if self.session_model:
            session_instance_id = self.session_model.instance_id
        elif self.task_model and self.task_model.project:
            # Fallback: try to get project info from task
            session_instance_id = None  # Will be set by session later

        log_entry = LogEntry(
            session_id=self.session_id,
            session_instance_id=session_instance_id,
            task_id=self.task_id,
            task_execution_id=self.task_execution_id,
            level=level,
            message=message,
            log_metadata=metadata,
        )
        self.db.add(log_entry)
        # Only commit if explicitly requested (for performance)
        if commit:
            self.db.commit()
        return log_entry

    async def start_session(self, task_description: str) -> str:
        """
        Start a session for the given task description

        Args:
            task_description: Description of the task to execute

        Returns:
            OpenClaw session key
        """
        return await self.create_session(task_description)

    async def stop_session(self) -> None:
        """
        Stop the OpenClaw session gracefully

        Raises:
            OpenClawSessionError: If stop fails
        """
        try:
            self._log_entry("INFO", "Stopping OpenClaw session")

            # Cleanup session resources
            await self.cleanup()

            self._log_entry("INFO", "OpenClaw session stopped")

        except Exception as e:
            error_msg = f"Failed to stop session: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)

    async def pause_session(self) -> None:
        """
        Pause the OpenClaw session with full checkpoint save

        Saves current execution state including:
        - Session context (task description, project info)
        - Orchestration workflow state (PLANNING, EXECUTING, etc.)
        - Current step index and results from completed steps
        - Tool execution history

        Raises:
            OpenClawSessionError: If pause fails
        """
        try:
            self._log_entry("INFO", "Pausing OpenClaw session with checkpoint")

            # Create checkpoint service instance
            checkpoint_service = CheckpointService(self.db)

            # Get current session context
            context_data = await self.get_session_context()
            task_context = context_data.get("task") or {}
            if self.task_model:
                context_data["task_id"] = self.task_model.id
                context_data["task_description"] = (
                    self.task_model.description or self.task_model.title
                )
                context_data["task_subfolder"] = getattr(
                    self.task_model, "task_subfolder", None
                )
                if self.task_model.project:
                    context_data["project_name"] = self.task_model.project.name
                    if self.task_model.project.workspace_path:
                        from app.services.workspace.project_isolation_service import (
                            resolve_project_workspace_path,
                        )

                        workspace_path = str(
                            resolve_project_workspace_path(
                                self.task_model.project.workspace_path,
                                self.task_model.project.name,
                            )
                        )
                        context_data["workspace_path_override"] = workspace_path
                        if context_data.get(
                            "task_subfolder"
                        ) and not should_execute_in_canonical_project_root(
                            self.task_model,
                            getattr(self.task_model, "execution_profile", None),
                            getattr(self.task_model, "title", None),
                            getattr(self.task_model, "description", None),
                        ):
                            context_data["project_dir_override"] = str(
                                Path(workspace_path) / context_data["task_subfolder"]
                            )
                        else:
                            context_data["project_dir_override"] = workspace_path
            elif task_context:
                context_data["task_id"] = task_context.get("id")
                context_data["task_description"] = task_context.get(
                    "description"
                ) or task_context.get("title")

            # Save orchestration state if available
            orchestration_state = {}
            if hasattr(self, "_orchestration_state"):
                orchestration_state = self._orchestration_state

            # Get step results from execution history
            step_results = []
            try:
                tool_service = ToolTrackingService(self.db)
                executions = tool_service.get_execution_history(
                    session_id=self.session_id, limit=50
                )

                for exec_item in executions:
                    step_results.append(
                        {
                            "step_type": "tool_execution",
                            "tool_name": exec_item.tool_name,
                            "parameters": (
                                json.loads(exec_item.parameters)
                                if isinstance(exec_item.parameters, str)
                                else exec_item.parameters
                            ),
                            "result": exec_item.result,
                            "success": exec_item.success,
                            "executed_at": (
                                exec_item.executed_at.isoformat()
                                if exec_item.executed_at
                                else None
                            ),
                        }
                    )
            except Exception as exc:
                logger.debug("[OPENCLAW] Ignoring tool tracking restore error: %s", exc)

            # Find current step index (last executed step)
            current_step_index = len(step_results) - 1 if step_results else 0

            # Save checkpoint with detailed state
            checkpoint_name = f"paused_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"

            checkpoint_result = checkpoint_service.save_checkpoint(
                session_id=self.session_id,
                checkpoint_name=checkpoint_name,
                context_data=context_data,
                orchestration_state=orchestration_state,
                current_step_index=current_step_index,
                step_results=step_results,
            )

            # Log checkpoint details
            self._log_entry(
                "INFO",
                f"Checkpoint saved: {checkpoint_result['path']} (name: {checkpoint_name})",
            )

            # Terminate the OpenClaw process gracefully
            await self.cleanup()

            self._log_entry(
                "INFO", f"OpenClaw session paused - Checkpoint saved: {checkpoint_name}"
            )

        except Exception as e:
            error_msg = f"Failed to pause session: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)

    async def resume_session(self, checkpoint_name: Optional[str] = None) -> str:
        """
        Resume a paused session from a checkpoint

        Args:
            checkpoint_name: Specific checkpoint to restore (optional - uses latest if not specified)

        Returns:
            OpenClaw session key for resumed execution

        Raises:
            OpenClawSessionError: If resume fails
        """
        try:
            self._log_entry("INFO", "Resuming OpenClaw session from checkpoint")

            # Create checkpoint service instance
            checkpoint_service = CheckpointService(self.db)

            # Load checkpoint data
            checkpoint_data = checkpoint_service.load_checkpoint(
                session_id=self.session_id, checkpoint_name=checkpoint_name
            )

            # Restore context
            context_data = checkpoint_data.get("context", {})
            orchestration_state = checkpoint_data.get("orchestration_state", {})
            step_results = checkpoint_data.get("step_results", [])
            current_step_index = checkpoint_data.get("current_step_index", 0)

            # Reconstruct task description from context (if available)
            task_description = (
                context_data.get("task_description")
                or context_data.get("description")
                or "Resumed session"
            )

            # Create new OpenClaw session with restored context
            # Include information about resumed execution in the prompt
            resume_context = {
                **context_data,
                "resumed_from_checkpoint": True,
                "checkpoint_name": checkpoint_data.get("checkpoint_name"),
                "completed_steps_count": len(step_results),
                "last_step_index": current_step_index,
                "previous_orchestration_state": orchestration_state.get("status", ""),
                "previous_plan_summary": (
                    json.dumps(orchestration_state.get("plan", []))[:500]
                    if orchestration_state.get("plan")
                    else None
                ),
            }

            # Create OpenClaw session
            session_key = await self.create_session(
                task_description=task_description, context=resume_context
            )

            # Log successful resume with checkpoint info
            self._log_entry(
                "INFO",
                f"OpenClaw session resumed from checkpoint: {checkpoint_data.get('checkpoint_name')}",
            )
            self._log_entry(
                "INFO",
                f"Restored state - Completed steps: {len(step_results)}, Current step index: {current_step_index}, Previous status: {orchestration_state.get('status', 'unknown')}",
            )

            return session_key

        except CheckpointError as e:
            error_msg = f"No valid checkpoint found to resume from: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)
        except Exception as e:
            error_msg = f"Failed to resume session: {str(e)}"
            self._log_entry("ERROR", error_msg)
            raise OpenClawSessionError(error_msg)

    async def cleanup(self) -> None:
        """Clean up session resources"""
        try:
            self._log_entry("INFO", "Cleaning up OpenClaw session")

            # Terminate any running processes
            if self.process:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()

            self._log_entry("INFO", "Session cleanup complete")

        except Exception as e:
            logger.error(f"Cleanup failed: {str(e)}")
