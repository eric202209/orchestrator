"""Read-only mirror of the normal Orchestrator Planning generation call.

The production Planning path for this deployment is `direct_ollama`
(`AGENT_SECONDARY_BACKEND`), served by `OllamaRuntime._chat` against
`{OLLAMA_BASE_URL}/v1/chat/completions`. This module reproduces that request
shape exactly -- same endpoint, same model, same temperature, same
thinking-disabled flags, same `num_ctx` -- while reading `settings` only.

It creates no database session, writes no setting, and touches no persistent
OpenClaw or global configuration.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings
from app.services.agents.providers.ollama_adapter import (
    _extract_ollama_chat_content,
    _no_think_suffix,
    _strip_thinking,
)


@dataclass(frozen=True)
class ProviderIdentity:
    provider: str
    model: str
    endpoint: str
    num_ctx: int
    temperature: float
    thinking_disabled: bool
    timeout_seconds: int


def planning_provider_identity() -> ProviderIdentity:
    base_url = (settings.OLLAMA_BASE_URL or "http://localhost:11434").rstrip("/")
    endpoint = (
        f"{base_url}/chat/completions"
        if base_url.endswith("/v1")
        else f"{base_url}/v1/chat/completions"
    )
    return ProviderIdentity(
        provider="direct_ollama (AGENT_SECONDARY_BACKEND, planning-only routing)",
        model=(settings.OLLAMA_AGENT_MODEL or "").strip(),
        endpoint=endpoint,
        num_ctx=int(getattr(settings, "OLLAMA_NUM_CTX", 4096)),
        temperature=0.1,
        thinking_disabled=True,
        timeout_seconds=int(
            getattr(settings, "OLLAMA_PLANNING_TIMEOUT_SECONDS", 0) or 180
        ),
    )


class ProviderUnavailableError(RuntimeError):
    """The normal Planning provider could not be exercised."""


def planning_chat(system: str, user: str) -> str:
    """One Planning-shaped completion. Same payload the production adapter sends."""

    identity = planning_provider_identity()
    if not identity.model:
        raise ProviderUnavailableError("no Planning model is configured")
    payload = {
        "model": identity.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user + _no_think_suffix()},
        ],
        "stream": False,
        "temperature": identity.temperature,
        "think": False,
        "options": {"num_ctx": identity.num_ctx},
    }
    try:
        with httpx.Client(timeout=float(identity.timeout_seconds)) as client:
            response = client.post(
                identity.endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return _strip_thinking(_extract_ollama_chat_content(response.json()))
    except httpx.HTTPError as exc:
        raise ProviderUnavailableError(str(exc)) from exc


def planning_provider_reachable() -> bool:
    identity = planning_provider_identity()
    tags = identity.endpoint.replace("/v1/chat/completions", "/api/tags")
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(tags)
            response.raise_for_status()
            names = {entry.get("name") for entry in response.json().get("models", [])}
    except (httpx.HTTPError, ValueError, KeyError):
        return False
    return identity.model in names
