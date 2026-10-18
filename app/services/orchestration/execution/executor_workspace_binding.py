"""Execution-time executor workspace binding layer (Phase 23D/ORS1).

Phase 23C confirmed a real architectural blocker: OpenClaw's fail-closed
agent-selection guard (Phase 22C-0) requires a configured agent whose
`workspace` field equals the execution cwd exactly, but a Task Execution
Sandbox path is unique per task execution -- no static `openclaw.json`
entry can ever match it, so every runtime-workspace dispatch raised
`OpenClawAgentSelectionError` by design.

This module closes that gap without rewriting the operator's persistent
`openclaw.json` and without creating or deleting any agent identity in it.
Normal Orchestrator dispatch reads the real config read-only, projects only
the selected local provider/model route into a private invocation config, and
points one synthetic agent at the current Runtime Workspace. The persistent
runner-template selector below remains available for explicit
historical/maintenance callers, but is not a normal lifecycle prerequisite.
The projection is consumed by exactly one dispatch (via
`OPENCLAW_CONFIG_PATH`, already an existing
`OpenClawSessionService._openclaw_config_path()` seam) and discarded on
release -- the same ephemeral-artifact-per-invocation pattern
`git_containment_guard.build_git_containment_env` already uses for the git
shim.

Deliberately independent of OpenClaw's own service module (imports only
`RuntimeExecutorContext`) so a future executor with different workspace
binding semantics can add its own `bind_<executor>_workspace` function here
without this module growing OpenClaw-specific control flow (Goal 5:
Runtime Workspace ownership belongs to Orchestrator; an executor is only a
consumer of a bound context).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import socket
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from app.config import settings
from app.services.orchestration.execution.runtime_context import (
    RuntimeExecutorContext,
)

logger = logging.getLogger(__name__)

# Phase 37 Pre-B-F2B: ephemeral binding artifact ownership contract.
# Every binding lives in one Orchestrator-owned root (never loose in /tmp),
# carries non-sensitive ownership metadata, and holds an exclusive flock on
# its lock file for its whole lifetime. The kernel drops that lock when the
# last holder (worker or inherited OpenClaw child) exits -- including on
# SIGKILL -- so the lock, not PID or age, is the liveness authority used by
# `binding_reconciliation`.
BINDING_ROOT_ENV = "ORCHESTRATOR_OPENCLAW_BINDING_ROOT"
BINDING_DIR_PREFIX = "orchestrator-openclaw-binding-"
BINDING_METADATA_NAME = ".orchestrator-binding.json"
BINDING_LOCK_NAME = ".orchestrator-binding.lock"
BINDING_METADATA_SCHEMA = "orchestrator.openclaw_binding.v1"
BINDING_ARTIFACT_TYPE = "openclaw_runtime_binding"

# D-37-13: the only route currently authorized for an ephemeral local binding.
# The provider name is OpenClaw's OpenAI-compatible adapter; it does not imply
# that the route is the external OpenAI service.  The selected provider's
# explicit baseUrl remains the routing authority.
SUPPORTED_LOCAL_MODEL_REF = "openai/qwen-local"

# These are the provider/model fields required by the pinned OpenClaw runtime
# for the selected OpenAI-compatible local route.  Unknown fields are rejected
# rather than copied, because an unknown field may be credential-bearing or
# may change provider selection.
_LOCAL_PROVIDER_KEYS = frozenset({"api", "apiKey", "auth", "baseUrl", "models"})
_LOCAL_MODEL_KEYS = frozenset(
    {
        "compat",
        "contextWindow",
        "cost",
        "id",
        "input",
        "maxTokens",
        "name",
        "reasoning",
        "api",
    }
)

# The OpenClaw child receives only process/runtime plumbing.  Provider
# credentials stay in the selected provider projection, not in inherited env.
_OPENCLAW_ENV_KEYS = frozenset(
    {
        "FORCE_COLOR",
        "LANG",
        "LOGNAME",
        "NO_COLOR",
        "OPENCLAW_NO_RESPAWN",
        "PATH",
        "SHELL",
        "TERM",
        "TZ",
        "USER",
    }
)


def restricted_openclaw_environment(
    base_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Return a non-secret environment allowlist for a managed OpenClaw child.

    Deliberately excluded are provider/API/OAuth, gateway, GitHub, Langfuse,
    application-secret, proxy and generic ``XDG_*`` variables.  Binding-local
    ``HOME``/``TMPDIR`` and the two OpenClaw binding paths are added by the
    binding itself after this function returns.
    """

    source = dict(base_env if base_env is not None else os.environ)
    return {
        key: value
        for key, value in source.items()
        if key in _OPENCLAW_ENV_KEYS or key.startswith("LC_")
    }


def binding_artifact_root() -> Path:
    """Return the approved root for ephemeral OpenClaw binding artifacts."""

    configured = os.environ.get(BINDING_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / f"orchestrator-openclaw-bindings-{os.getuid()}"


def validate_binding_artifact_root(root: Path) -> None:
    """Fail closed unless ``root`` is a private directory owned by this uid."""

    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise ExecutorWorkspaceBindingError(
            f"Binding artifact root is unavailable: {root}: {exc}"
        ) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ExecutorWorkspaceBindingError(
            f"Binding artifact root is not a real directory: {root}"
        )
    if root_stat.st_uid != os.getuid() or root_stat.st_mode & 0o077:
        raise ExecutorWorkspaceBindingError(
            f"Binding artifact root {root} must be owned by uid {os.getuid()} "
            "with no group/other access"
        )


def _ensure_binding_artifact_root() -> Path:
    root = binding_artifact_root()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ExecutorWorkspaceBindingError(
            f"Could not create binding artifact root {root}: {exc}"
        ) from exc
    validate_binding_artifact_root(root)
    return root


def read_boot_id() -> Optional[str]:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def read_pid_namespace() -> Optional[str]:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def read_process_start_ticks(pid: int) -> Optional[int]:
    """Return a PID's start time (clock ticks since boot) to detect PID reuse."""

    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        # Field 22; the comm field (2) may contain spaces, so split after ')'.
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _binding_metadata(
    tmp_dir: Path, binding_id: str, task_execution_id: Any
) -> Dict[str, Any]:
    pid = os.getpid()
    return {
        "schema": BINDING_METADATA_SCHEMA,
        "artifact_type": BINDING_ARTIFACT_TYPE,
        "binding_id": binding_id,
        "artifact_dir_name": tmp_dir.name,
        "created_at_epoch": time.time(),
        "owner_service": "orchestrator.executor_workspace_binding",
        "owner_hostname": socket.gethostname(),
        "owner_uid": os.getuid(),
        "owner_pid": pid,
        "owner_pid_start_ticks": read_process_start_ticks(pid),
        "owner_pid_namespace": read_pid_namespace(),
        "boot_id": read_boot_id(),
        "task_execution_id": task_execution_id,
        "liveness": "exclusive_flock:" + BINDING_LOCK_NAME,
        "cleanup_eligibility": (
            "only when the binding lock is free and metadata revalidates; "
            "operator --apply required"
        ),
    }


def _write_private_file(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


class ExecutorWorkspaceBindingError(Exception):
    """Raised when a Runtime Workspace cannot be bound to an executor.

    Callers must fail closed: never fall back to the Project Workspace,
    never fall back to an executor's default/static configuration, and
    never invent a new agent/executor identity to route around this.
    """


def _json_copy(value: Any, *, field_name: str) -> Any:
    """Copy a JSON value without retaining references to source config data."""

    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise ExecutorWorkspaceBindingError(
            f"Unsupported OpenClaw {field_name} schema; refusing broad config copy"
        ) from exc


def _configured_agent_list(config: Dict[str, Any]) -> list[Any]:
    """Return the configured agent list or fail closed on unknown shape."""

    agents = config.get("agents")
    if agents is None:
        return []
    if not isinstance(agents, dict):
        raise ExecutorWorkspaceBindingError(
            "OpenClaw agent configuration has an unsupported schema; refusing binding"
        )
    entries = agents.get("list")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ExecutorWorkspaceBindingError(
            "OpenClaw agent list has an unsupported schema; refusing binding"
        )
    return entries


def project_local_openclaw_config(
    config: Dict[str, Any],
    *,
    model_ref: str,
    agent_id: str,
    runtime_workspace: Path,
    agent_dir: Path,
    state_dir: Path,
) -> Dict[str, Any]:
    """Build the minimum config for the pinned local OpenClaw route.

    This function is intentionally strict.  It retains the selected provider's
    explicit gateway URL, adapter, and authentication material, plus only the
    selected model's routing metadata.  It never copies persistent agent
    directories, auth profiles, defaults, tools, plugins, channels, or other
    providers.
    """

    if model_ref != SUPPORTED_LOCAL_MODEL_REF:
        raise ExecutorWorkspaceBindingError(
            f"Unsupported OpenClaw runtime route {model_ref!r}; refusing binding"
        )
    if not isinstance(config, dict):
        raise ExecutorWorkspaceBindingError(
            "OpenClaw configuration must be an object; refusing binding"
        )

    models = config.get("models")
    providers = models.get("providers") if isinstance(models, dict) else None
    provider = providers.get("openai") if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        raise ExecutorWorkspaceBindingError(
            "Selected local OpenClaw provider catalog is unavailable; refusing binding"
        )
    unknown_provider_keys = set(provider) - _LOCAL_PROVIDER_KEYS
    if unknown_provider_keys:
        raise ExecutorWorkspaceBindingError(
            "Selected local OpenClaw provider has unsupported schema fields; "
            "refusing binding"
        )

    base_url = provider.get("baseUrl")
    api = provider.get("api")
    api_key = provider.get("apiKey")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ExecutorWorkspaceBindingError(
            "Selected local OpenClaw provider has no explicit gateway URL"
        )
    if not isinstance(api, str) or not api.strip():
        raise ExecutorWorkspaceBindingError(
            "Selected local OpenClaw provider has no explicit API adapter"
        )
    if not isinstance(api_key, str) or not api_key.strip():
        raise ExecutorWorkspaceBindingError(
            "Selected local OpenClaw provider has no explicit gateway authentication"
        )
    # Environment/secret-reference markers cannot work with the restricted
    # child environment and must not be guessed or resolved from broad ambient
    # credentials.  A later task may add an explicitly authorized resolver.
    if api_key.startswith("$") or api_key.startswith("{"):
        raise ExecutorWorkspaceBindingError(
            "Selected local OpenClaw provider authentication uses an unsupported "
            "environment or secret reference"
        )
    auth = provider.get("auth")
    if auth is not None and (not isinstance(auth, str) or auth != "api-key"):
        raise ExecutorWorkspaceBindingError(
            "Selected local OpenClaw provider uses an unsupported auth mode"
        )

    provider_models = provider.get("models")
    if not isinstance(provider_models, list):
        raise ExecutorWorkspaceBindingError(
            "Selected local OpenClaw provider has no model catalog"
        )
    selected_models = [
        item
        for item in provider_models
        if isinstance(item, dict) and str(item.get("id") or "").strip() == "qwen-local"
    ]
    if len(selected_models) != 1:
        raise ExecutorWorkspaceBindingError(
            "Selected local OpenClaw model catalog entry is missing or ambiguous"
        )
    selected_model = selected_models[0]
    unknown_model_keys = set(selected_model) - _LOCAL_MODEL_KEYS
    if unknown_model_keys:
        raise ExecutorWorkspaceBindingError(
            "Selected local OpenClaw model has unsupported schema fields; refusing binding"
        )

    selected_provider = {
        "baseUrl": base_url,
        "api": api,
        "apiKey": api_key,
        "models": [_json_copy(selected_model, field_name="model")],
    }
    if auth is not None:
        selected_provider["auth"] = auth

    return {
        "models": {"providers": {"openai": selected_provider}},
        "agents": {
            "defaults": {
                "workspace": str(runtime_workspace),
                "skipBootstrap": True,
            },
            "list": [
                {
                    "id": agent_id,
                    "workspace": str(runtime_workspace),
                    "agentDir": str(agent_dir),
                    "model": {
                        "primary": SUPPORTED_LOCAL_MODEL_REF,
                        "fallbacks": [],
                    },
                }
            ],
        },
        "session": {"store": str(state_dir / "sessions.json")},
    }


def write_projected_local_auth_profile(
    agent_dir: Path, projected_config: Dict[str, Any]
) -> Path:
    """Write only the supported local provider credential into the binding.

    OpenClaw resolves non-secret local markers through its agent-local auth
    profile store.  The current projection supports only ``openai/qwen-local``;
    keep this helper equally narrow and source its value from that projection.
    """

    models = projected_config.get("models")
    providers = models.get("providers") if isinstance(models, dict) else None
    if not isinstance(providers, dict) or set(providers) != {"openai"}:
        raise ExecutorWorkspaceBindingError(
            "Projected OpenClaw auth profile requires the supported openai provider"
        )
    provider = providers["openai"]
    api_key = provider.get("apiKey") if isinstance(provider, dict) else None
    if not isinstance(api_key, str) or not api_key.strip():
        raise ExecutorWorkspaceBindingError(
            "Projected OpenClaw auth profile has no provider credential"
        )

    profile_path = Path(agent_dir) / "auth-profiles.json"
    _write_private_file(
        profile_path,
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "openai:default": {
                        "type": "api_key",
                        "provider": "openai",
                        "key": api_key,
                    }
                },
            },
            indent=2,
        ),
    )
    return profile_path


RUNNER_AGENT_ID_ENV = "OPENCLAW_RUNNER_AGENT_ID"
EPHEMERAL_AGENT_ID = "orchestrator-runtime"


@dataclass(frozen=True)
class OpenClawTemplateSelection:
    """The explicit template identity and its persistent workspace."""

    agent_id: str
    persistent_workspace: Path


def resolve_openclaw_runner_agent_id(
    configured_agent_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve the explicit runner identity without heuristic fallbacks."""

    explicit = str(configured_agent_id or "").strip()
    if explicit:
        return explicit
    environment_value = os.environ.get(RUNNER_AGENT_ID_ENV, "").strip()
    if environment_value:
        return environment_value
    configured = str(getattr(settings, "OPENCLAW_RUNNER_AGENT_ID", "") or "").strip()
    return configured or None


def _path_is_within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def validate_runtime_workspace_context(context: RuntimeExecutorContext) -> None:
    """Validate the runtime/project relationship before creating a binding.

    This is the safety portion of the old runner admission contract. It does
    not inspect OpenClaw identities, so workspace containment remains enforced
    even when the persistent operator config contains only ``main``.
    """

    runtime_root_value = getattr(context, "runtime_root", None)
    if not runtime_root_value:
        raise ExecutorWorkspaceBindingError(
            "Runtime Workspace binding has no approved Orchestrator runtime root"
        )
    runtime_root = Path(runtime_root_value).expanduser().resolve(strict=False)
    project_workspace = (
        Path(context.project_workspace).expanduser().resolve(strict=False)
    )
    runtime_workspace = (
        Path(context.runtime_workspace).expanduser().resolve(strict=False)
    )
    if not runtime_root.exists() or not runtime_root.is_dir():
        raise ExecutorWorkspaceBindingError(
            f"Approved Orchestrator runtime root does not exist: {runtime_root}"
        )
    if _path_is_within(project_workspace, runtime_root) or _path_is_within(
        runtime_root, project_workspace
    ):
        raise ExecutorWorkspaceBindingError(
            "Project Workspace and approved Orchestrator runtime root overlap; "
            "refusing ambiguous workspace ownership"
        )
    if _path_is_within(runtime_workspace, project_workspace):
        raise ExecutorWorkspaceBindingError(
            f"Runtime Workspace {runtime_workspace} is inside Project Workspace "
            f"{project_workspace}"
        )
    if not _path_is_within(runtime_workspace, runtime_root):
        raise ExecutorWorkspaceBindingError(
            f"Runtime Workspace {runtime_workspace} is outside approved runtime "
            f"root {runtime_root}"
        )
    if not runtime_workspace.exists() or not runtime_workspace.is_dir():
        raise ExecutorWorkspaceBindingError(
            "Runtime Workspace does not exist as a directory: " f"{runtime_workspace}"
        )


def _find_template_agent_id(config: Dict[str, Any], workspace: Path) -> Optional[str]:
    """Preserve the legacy F12 audit matcher; never use it for dispatch.

    Phase 31's read-only launch-precondition report still needs to identify
    historical ProjectRoot registrations. Normal execution must use
    ``select_runtime_owned_openclaw_template`` and never call this helper.
    """

    project_root = Path(workspace).expanduser().resolve(strict=False)
    matches = [
        str(agent.get("id") or "").strip()
        for agent in _configured_agent_list(config)
        if isinstance(agent, dict)
        and str(agent.get("id") or "").strip()
        and str(agent.get("workspace") or "").strip()
        and Path(str(agent.get("workspace") or "")).expanduser().resolve(strict=False)
        == project_root
    ]
    if len(matches) > 1:
        raise ExecutorWorkspaceBindingError(
            "Multiple OpenClaw agents are configured with a workspace matching "
            f"Project Workspace {project_root}: {matches}; refusing heuristic "
            "selection."
        )
    return matches[0] if matches else None


def validate_runtime_owned_openclaw_agent(
    config: Dict[str, Any],
    *,
    agent_id: Optional[str],
    project_workspace: Path,
    runtime_root: Path,
) -> OpenClawTemplateSelection:
    """Validate and return one explicit runner's persistent workspace."""

    resolved_id = str(agent_id or "").strip()
    if not resolved_id:
        raise ExecutorWorkspaceBindingError(
            "OpenClaw runner agent ID is not configured; refusing implicit "
            "main, ProjectRoot, nearest-workspace, or generic-workspace selection."
        )
    approved_root = Path(runtime_root).expanduser().resolve(strict=False)
    project_root = Path(project_workspace).expanduser().resolve(strict=False)
    if not approved_root.exists() or not approved_root.is_dir():
        raise ExecutorWorkspaceBindingError(
            f"Approved Orchestrator runtime root does not exist: {approved_root}"
        )
    if _path_is_within(project_root, approved_root) or _path_is_within(
        approved_root, project_root
    ):
        raise ExecutorWorkspaceBindingError(
            "Project Workspace and approved Orchestrator runtime root overlap; "
            "refusing ambiguous workspace ownership"
        )

    agents = _configured_agent_list(config)
    matches = [
        agent
        for agent in agents
        if isinstance(agent, dict) and str(agent.get("id") or "").strip() == resolved_id
    ]
    if len(matches) != 1:
        if not matches:
            raise ExecutorWorkspaceBindingError(
                f"Configured OpenClaw runner agent {resolved_id!r} was not found; "
                "refusing fallback selection"
            )
        raise ExecutorWorkspaceBindingError(
            f"Multiple OpenClaw runner entries use ID {resolved_id!r}; refusing "
            "conflicting identity selection"
        )

    workspace_value = str(matches[0].get("workspace") or "").strip()
    if not workspace_value:
        raise ExecutorWorkspaceBindingError(
            f"OpenClaw runner agent {resolved_id!r} has no persistent workspace"
        )
    persistent_workspace = Path(workspace_value).expanduser().resolve(strict=False)
    if not persistent_workspace.exists() or not persistent_workspace.is_dir():
        raise ExecutorWorkspaceBindingError(
            "OpenClaw runner workspace does not exist as a directory: "
            f"{persistent_workspace}"
        )
    if not _path_is_within(persistent_workspace, approved_root):
        raise ExecutorWorkspaceBindingError(
            f"OpenClaw runner workspace {persistent_workspace} is outside approved "
            f"runtime root {approved_root}"
        )
    if _path_is_within(persistent_workspace, project_root) or _path_is_within(
        project_root, persistent_workspace
    ):
        raise ExecutorWorkspaceBindingError(
            f"OpenClaw runner workspace {persistent_workspace} overlaps Project "
            f"Workspace {project_root}"
        )
    return OpenClawTemplateSelection(
        agent_id=resolved_id,
        persistent_workspace=persistent_workspace,
    )


def select_runtime_owned_openclaw_template(
    config: Dict[str, Any],
    context: RuntimeExecutorContext,
    *,
    configured_agent_id: Optional[str] = None,
) -> OpenClawTemplateSelection:
    """Select one explicit runtime-owned OpenClaw template."""

    agent_id = resolve_openclaw_runner_agent_id(configured_agent_id)
    if not agent_id:
        raise ExecutorWorkspaceBindingError(
            "OpenClaw runner agent ID is not configured; refusing implicit "
            "main, ProjectRoot, nearest-workspace, or generic-workspace selection."
        )

    runtime_root_value = getattr(context, "runtime_root", None)
    if not runtime_root_value:
        raise ExecutorWorkspaceBindingError(
            "Runtime Workspace binding has no approved Orchestrator runtime root"
        )
    runtime_root = Path(runtime_root_value).expanduser().resolve(strict=False)
    project_workspace = (
        Path(context.project_workspace).expanduser().resolve(strict=False)
    )
    runtime_workspace = (
        Path(context.runtime_workspace).expanduser().resolve(strict=False)
    )
    if not runtime_root.exists() or not runtime_root.is_dir():
        raise ExecutorWorkspaceBindingError(
            f"Approved Orchestrator runtime root does not exist: {runtime_root}"
        )
    if _path_is_within(project_workspace, runtime_root) or _path_is_within(
        runtime_root, project_workspace
    ):
        raise ExecutorWorkspaceBindingError(
            "Project Workspace and approved Orchestrator runtime root overlap; "
            "refusing ambiguous workspace ownership"
        )
    if _path_is_within(runtime_workspace, project_workspace):
        raise ExecutorWorkspaceBindingError(
            f"Runtime Workspace {runtime_workspace} is inside Project Workspace "
            f"{project_workspace}"
        )
    if not _path_is_within(runtime_workspace, runtime_root):
        raise ExecutorWorkspaceBindingError(
            f"Runtime Workspace {runtime_workspace} is outside approved runtime "
            f"root {runtime_root}"
        )
    if not runtime_workspace.exists() or not runtime_workspace.is_dir():
        raise ExecutorWorkspaceBindingError(
            "Runtime Workspace does not exist as a directory: " f"{runtime_workspace}"
        )

    return validate_runtime_owned_openclaw_agent(
        config,
        agent_id=agent_id,
        project_workspace=project_workspace,
        runtime_root=runtime_root,
    )


@dataclass
class ExecutorWorkspaceBinding:
    """An active, per-invocation binding. Must be released via `release()`."""

    agent_id: str
    persistent_workspace: Optional[Path]
    config_path: Path
    _tmp_dir: Path
    environment: Dict[str, str]
    binding_id: Optional[str] = None
    _lock_fd: Optional[int] = None
    _unregister_forced_cleanup: Optional[Callable[[], None]] = field(
        default=None, repr=False
    )

    def subprocess_pass_fds(self) -> Tuple[int, ...]:
        """FDs an OpenClaw child must inherit so it keeps the binding live."""

        return (self._lock_fd,) if self._lock_fd is not None else ()

    def release(self) -> None:
        """Remove the ephemeral config copy, then drop the lock. Never raises.

        Idempotent. A failed removal is logged (path and binding id only) and
        the lock is still dropped, so the residue becomes discoverable by
        `binding_reconciliation` instead of being silently forgotten.
        """
        unregister = self._unregister_forced_cleanup
        self._unregister_forced_cleanup = None
        if unregister is not None:
            unregister()
        failures = []
        try:
            if os.path.lexists(self._tmp_dir):
                shutil.rmtree(
                    self._tmp_dir,
                    onerror=lambda _fn, path, exc_info: failures.append(
                        (path, exc_info[0].__name__)
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - cleanup must never raise
            failures.append((str(self._tmp_dir), type(exc).__name__))
        finally:
            lock_fd, self._lock_fd = self._lock_fd, None
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
        if failures:
            logger.warning(
                "[EXECUTOR_WORKSPACE_BINDING] binding_release_failed %s",
                json.dumps(
                    {
                        "binding_id": self.binding_id,
                        "artifact_dir": str(self._tmp_dir),
                        "failures": failures[:10],
                        "residue_discoverable_by": "binding_reconciliation",
                    },
                    sort_keys=True,
                ),
            )


def bind_openclaw_workspace(
    context: RuntimeExecutorContext,
    *,
    real_config_path: Path,
    runner_agent_id: Optional[str] = None,
    model_ref: Optional[str] = None,
) -> ExecutorWorkspaceBinding:
    """Bind an OpenClaw agent's workspace to `context.runtime_workspace`.

    Reads `real_config_path` (the real, persistent `openclaw.json`) once,
    read-only. Normal dispatch adds a synthetic invocation-only agent to a
    private temp copy and binds it to `context.runtime_workspace`.

    ``runner_agent_id`` is retained only for explicit historical callers that
    still need the old persistent-template adapter. Normal Orchestrator code
    deliberately leaves it unset and never consults environment/default
    runner identity.
    """

    try:
        real_config = json.loads(Path(real_config_path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ExecutorWorkspaceBindingError(
            f"Could not read OpenClaw config at {real_config_path}: {exc}"
        ) from exc

    validate_runtime_workspace_context(context)
    legacy_selection = None
    if runner_agent_id is not None:
        legacy_selection = select_runtime_owned_openclaw_template(
            real_config,
            context,
            configured_agent_id=runner_agent_id,
        )
        agent_id = legacy_selection.agent_id
    else:
        agent_id = EPHEMERAL_AGENT_ID
    resolved_model_ref = str(model_ref or "").strip() or None
    if resolved_model_ref is None:
        raise ExecutorWorkspaceBindingError(
            "Explicit OpenClaw runtime model is required; refusing "
            "persistent/default model authority"
        )

    if legacy_selection is not None:
        selected_agent = next(
            (
                agent
                for agent in _configured_agent_list(real_config)
                if isinstance(agent, dict)
                and str(agent.get("id") or "").strip() == agent_id
            ),
            None,
        )
        if selected_agent is None:
            raise ExecutorWorkspaceBindingError(
                f"Configured OpenClaw runner agent {agent_id!r} was not found"
            )

    # Validate the selected route before creating a managed directory.  This
    # makes unsupported providers and unknown auth/schema requirements fail
    # closed without even a partial binding artifact.
    project_local_openclaw_config(
        real_config,
        model_ref=resolved_model_ref,
        agent_id=agent_id,
        runtime_workspace=Path(context.runtime_workspace),
        agent_dir=Path("/binding/agent"),
        state_dir=Path("/binding/state"),
    )

    configured_agents = _configured_agent_list(real_config)
    if legacy_selection is None and any(
        isinstance(agent, dict) and str(agent.get("id") or "").strip() == agent_id
        for agent in configured_agents
    ):
        raise ExecutorWorkspaceBindingError(
            f"Ephemeral OpenClaw agent ID {agent_id!r} collides with the "
            "operator config; refusing ambiguous identity selection"
        )

    def build_config(tmp_dir: Path) -> Dict[str, Any]:
        agent_dir = tmp_dir / "agent"
        agent_dir.mkdir(mode=0o700)
        projected = project_local_openclaw_config(
            real_config,
            model_ref=resolved_model_ref,
            agent_id=agent_id,
            runtime_workspace=Path(context.runtime_workspace),
            agent_dir=agent_dir,
            state_dir=tmp_dir / "state",
        )
        write_projected_local_auth_profile(agent_dir, projected)
        return projected

    binding = create_managed_binding(
        agent_id=agent_id,
        task_execution_id=getattr(context, "task_execution_id", None),
        build_config=build_config,
        persistent_workspace=(
            legacy_selection.persistent_workspace if legacy_selection else None
        ),
    )

    logger.info(
        "[EXECUTOR_WORKSPACE_BINDING] Bound OpenClaw agent %s workspace "
        "%s -> %s for task_execution_id=%s (template workspace %s; ephemeral config at %s; "
        "persistent %s untouched; binding_id=%s)",
        agent_id,
        legacy_selection.persistent_workspace if legacy_selection else None,
        context.runtime_workspace,
        context.task_execution_id,
        legacy_selection.persistent_workspace if legacy_selection else None,
        binding.config_path,
        real_config_path,
        binding.binding_id,
    )
    return binding


def create_managed_binding(
    *,
    agent_id: str,
    task_execution_id: Any,
    build_config: Callable[[Path], Dict[str, Any]],
    persistent_workspace: Optional[Path] = None,
) -> ExecutorWorkspaceBinding:
    """Create one managed, locked, owner-only invocation config directory.

    Lifecycle only (Phase 37 Pre-B-F2C): every per-invocation OpenClaw config
    copy shares this ownership contract -- managed root, lock-first flock,
    ownership metadata, a ``state/`` dir, a 0600 ``openclaw.json``, forced-
    termination release and reconciler visibility. ``build_config`` receives
    the directory once the lock and metadata exist, may add entries to it,
    and returns the config to write. Any failure removes the directory and
    drops the lock before re-raising, so nothing is left registered.
    """

    tmp_dir = Path(
        tempfile.mkdtemp(prefix=BINDING_DIR_PREFIX, dir=_ensure_binding_artifact_root())
    )
    binding_id = uuid.uuid4().hex
    lock_fd: Optional[int] = None
    try:
        # Take the liveness lock before anything else exists in the directory
        # so a reconciler can never observe valid metadata with a free lock
        # while this process is alive.
        lock_fd = os.open(
            tmp_dir / BINDING_LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_private_file(
            tmp_dir / BINDING_METADATA_NAME,
            json.dumps(
                _binding_metadata(tmp_dir, binding_id, task_execution_id), indent=2
            ),
        )
        state_dir = tmp_dir / "state"
        state_dir.mkdir(mode=0o700)
        (tmp_dir / "home").mkdir(mode=0o700)
        (tmp_dir / "tmp").mkdir(mode=0o700)
        bound_config = build_config(tmp_dir)
        config_path = tmp_dir / "openclaw.json"
        # The copy may carry provider settings from the operator config:
        # create it owner-only before writing any content.
        _write_private_file(config_path, "")
        config_path.write_text(json.dumps(bound_config, indent=2), encoding="utf-8")
    except BaseException:
        # Partial initialization: never leave a half-built (possibly
        # credential-bearing) directory behind for this process.
        ExecutorWorkspaceBinding(
            agent_id=agent_id,
            persistent_workspace=None,
            config_path=tmp_dir / "openclaw.json",
            _tmp_dir=tmp_dir,
            environment={},
            binding_id=binding_id,
            _lock_fd=lock_fd,
        ).release()
        raise
    binding = ExecutorWorkspaceBinding(
        agent_id=agent_id,
        persistent_workspace=persistent_workspace,
        config_path=config_path,
        _tmp_dir=tmp_dir,
        environment={
            "OPENCLAW_CONFIG_PATH": str(config_path),
            "OPENCLAW_STATE_DIR": str(state_dir),
            "HOME": str(tmp_dir / "home"),
            "TMPDIR": str(tmp_dir / "tmp"),
        },
        binding_id=binding_id,
        _lock_fd=lock_fd,
    )
    # A forced SIGTERM releases every binding this process still owns, not
    # only the worker-level ones (nested repair/fallback runtimes included).
    from app.services.agents.subprocess_lifecycle import (
        register_forced_termination_cleanup,
    )

    binding._unregister_forced_cleanup = register_forced_termination_cleanup(
        binding.release
    )
    return binding
