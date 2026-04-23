"""Backend registry for orchestration model/runtime integrations."""

from __future__ import annotations

import shlex
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config import settings


@dataclass(frozen=True)
class BackendCapabilities:
    """Declared backend capabilities used for routing and operator visibility."""

    supports_planning: bool
    supports_step_execution: bool
    supports_debug_repair: bool
    supports_streaming: bool
    supports_checkpoint_resume: bool
    supports_tool_execution: bool
    supports_json_mode: bool
    mcp_capable: bool = False
    max_context_tokens: Optional[int] = None
    reliability_tier: str = "standard"
    latency_tier: str = "standard"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackendConfigMetadata:
    """Static backend configuration metadata."""

    auth_mode: str
    transport_mode: str
    required_env_vars: List[str]
    supported_prompt_format: str
    streaming_mode: str
    adaptation_profiles: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackendHealth:
    """Runtime readiness state used by the operator UI and routing."""

    available: bool
    ready: bool
    status: str
    errors: List[str]
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackendDescriptor:
    """Backend metadata exposed to orchestration and operator surfaces."""

    name: str
    display_name: str
    implementation: str
    default_model_family: str
    implemented: bool
    capabilities: BackendCapabilities
    config: BackendConfigMetadata
    health: BackendHealth

    @property
    def available(self) -> bool:
        return self.health.available

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["available"] = self.available
        payload["capabilities"] = self.capabilities.to_dict()
        payload["config"] = self.config.to_dict()
        payload["health"] = self.health.to_dict()
        return payload


class UnsupportedAgentBackendError(ValueError):
    """Raised when the configured backend is unknown or not implemented."""


@dataclass(frozen=True)
class _BackendRegistration:
    descriptor: BackendDescriptor
    health_check: Callable[[BackendDescriptor], BackendHealth]


def _resolve_openclaw_command_candidates() -> List[Path]:
    configured_path = (settings.OPENCLAW_CLI_PATH or "").strip()
    candidates: List[Path] = []
    if configured_path:
        candidates.append(Path(configured_path).expanduser())

    detected_path = shutil.which("openclaw")
    if detected_path:
        candidates.append(Path(detected_path))

    for known in (
        "/usr/local/bin/openclaw",
        "/usr/bin/openclaw",
        str(Path.home() / ".local" / "bin" / "openclaw"),
        "/root/.local/bin/openclaw",
        "/opt/openclaw/dist/index.js",
        "/root/.openclaw/app/dist/index.js",
    ):
        candidates.append(Path(known).expanduser())

    unique: List[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _check_local_openclaw_health(descriptor: BackendDescriptor) -> BackendHealth:
    warnings: List[str] = []
    errors: List[str] = []
    cli_args = (settings.OPENCLAW_CLI_ARGS or "").strip()
    if cli_args:
        try:
            shlex.split(cli_args)
        except ValueError as exc:
            warnings.append(f"OPENCLAW_CLI_ARGS could not be parsed cleanly: {exc}")

    command_found = False
    for candidate in _resolve_openclaw_command_candidates():
        try:
            if candidate.exists():
                command_found = True
                break
        except OSError:
            continue
    if not command_found:
        errors.append(
            "OpenClaw CLI was not found in PATH, OPENCLAW_CLI_PATH, or known install locations."
        )

    return BackendHealth(
        available=command_found,
        ready=not errors,
        status="ready" if not errors else "degraded",
        errors=errors,
        warnings=warnings,
    )


def _check_planned_backend_health(descriptor: BackendDescriptor) -> BackendHealth:
    return BackendHealth(
        available=False,
        ready=False,
        status="not_implemented",
        errors=[
            (
                f"{descriptor.display_name} is registered for future expansion, "
                "but no runtime adapter is implemented yet."
            )
        ],
        warnings=[],
    )


def _check_openai_backend_health(descriptor: BackendDescriptor) -> BackendHealth:
    errors: List[str] = []
    warnings: List[str] = []

    if not (settings.OPENAI_API_KEY or "").strip():
        errors.append("OPENAI_API_KEY is not configured.")

    return BackendHealth(
        available=not errors,
        ready=not errors,
        status="ready" if not errors else "degraded",
        errors=errors,
        warnings=warnings,
    )


def _base_descriptor(
    *,
    name: str,
    display_name: str,
    implementation: str,
    default_model_family: str,
    implemented: bool,
    capabilities: BackendCapabilities,
    config: BackendConfigMetadata,
) -> BackendDescriptor:
    return BackendDescriptor(
        name=name,
        display_name=display_name,
        implementation=implementation,
        default_model_family=default_model_family,
        implemented=implemented,
        capabilities=capabilities,
        config=config,
        health=BackendHealth(
            available=False,
            ready=False,
            status="unknown",
            errors=[],
            warnings=[],
        ),
    )


_BACKEND_REGISTRY: Dict[str, _BackendRegistration] = {
    "local_openclaw": _BackendRegistration(
        descriptor=_base_descriptor(
            name="local_openclaw",
            display_name="Local OpenClaw",
            implementation="app.services.agents.providers.openclaw_adapter.create_runtime",
            default_model_family="local",
            implemented=True,
            capabilities=BackendCapabilities(
                supports_planning=True,
                supports_step_execution=True,
                supports_debug_repair=True,
                supports_streaming=True,
                supports_checkpoint_resume=True,
                supports_tool_execution=True,
                supports_json_mode=False,
                mcp_capable=False,
                max_context_tokens=128000,
                reliability_tier="standard",
                latency_tier="local",
            ),
            config=BackendConfigMetadata(
                auth_mode="local_cli",
                transport_mode="cli",
                required_env_vars=[],
                supported_prompt_format="rendered_text_sections",
                streaming_mode="subprocess_jsonl",
                adaptation_profiles=["openclaw_default"],
            ),
        ),
        health_check=_check_local_openclaw_health,
    ),
    "remote_openclaw_gateway": _BackendRegistration(
        descriptor=_base_descriptor(
            name="remote_openclaw_gateway",
            display_name="Remote OpenClaw Gateway",
            implementation="app.services.agents.providers.remote_openclaw_adapter.create_runtime",
            default_model_family="gateway_default",
            implemented=False,
            capabilities=BackendCapabilities(
                supports_planning=True,
                supports_step_execution=True,
                supports_debug_repair=True,
                supports_streaming=True,
                supports_checkpoint_resume=False,
                supports_tool_execution=True,
                supports_json_mode=True,
                mcp_capable=False,
                max_context_tokens=None,
                reliability_tier="standard",
                latency_tier="network",
            ),
            config=BackendConfigMetadata(
                auth_mode="api_key",
                transport_mode="api",
                required_env_vars=["OPENCLAW_GATEWAY_URL", "OPENCLAW_API_KEY"],
                supported_prompt_format="rendered_text_sections",
                streaming_mode="http_stream",
                adaptation_profiles=["openclaw_default"],
            ),
        ),
        health_check=_check_planned_backend_health,
    ),
    "openai_responses_api": _BackendRegistration(
        descriptor=_base_descriptor(
            name="openai_responses_api",
            display_name="OpenAI Responses API",
            implementation="app.services.agents.providers.openai_adapter.create_runtime",
            default_model_family="gpt-5",
            implemented=True,
            capabilities=BackendCapabilities(
                supports_planning=True,
                supports_step_execution=False,
                supports_debug_repair=False,
                supports_streaming=True,
                supports_checkpoint_resume=False,
                supports_tool_execution=False,
                supports_json_mode=True,
                mcp_capable=True,
                max_context_tokens=None,
                reliability_tier="standard",
                latency_tier="network",
            ),
            config=BackendConfigMetadata(
                auth_mode="api_key",
                transport_mode="api",
                required_env_vars=["OPENAI_API_KEY"],
                supported_prompt_format="structured_prompt_envelope",
                streaming_mode="responses_stream",
                adaptation_profiles=["openai_responses_default"],
            ),
        ),
        health_check=_check_openai_backend_health,
    ),
}


def _resolve_registration(name: Optional[str]) -> Optional[_BackendRegistration]:
    normalized = (name or "").strip().lower() or "local_openclaw"
    return _BACKEND_REGISTRY.get(normalized)


def list_supported_backends() -> List[BackendDescriptor]:
    """Return the currently registered orchestration backends."""

    descriptors: List[BackendDescriptor] = []
    for registration in _BACKEND_REGISTRY.values():
        health = registration.health_check(registration.descriptor)
        descriptors.append(replace(registration.descriptor, health=health))
    return descriptors


def get_backend_descriptor(name: Optional[str]) -> BackendDescriptor:
    """Resolve a configured backend name to a concrete descriptor."""

    registration = _resolve_registration(name)
    if registration is None:
        raise UnsupportedAgentBackendError(
            f"Unsupported orchestration backend: {(name or '').strip() or '<empty>'}"
        )
    health = registration.health_check(registration.descriptor)
    return replace(registration.descriptor, health=health)


def require_backend_descriptor(name: Optional[str]) -> BackendDescriptor:
    """Resolve a backend and reject known-but-unimplemented providers explicitly."""

    descriptor = get_backend_descriptor(name)
    if not descriptor.implemented:
        raise UnsupportedAgentBackendError(
            f"Backend '{descriptor.name}' is registered but not implemented yet."
        )
    return descriptor
