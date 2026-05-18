"""Configured model adaptation profiles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from .renderers import (
    render_claude_strict_tools_prompt,
    render_openai_responses_prompt,
    render_openclaw_prompt,
    render_qwen_compact_json_prompt,
)
from .schemas import PromptEnvelope


@dataclass(frozen=True)
class AdaptationProfile:
    """Operator-selectable backend/model adaptation profile."""

    name: str
    display_name: str
    backend: str
    model_family: str
    prompt_format: str
    renderer: str
    prompt_dialect: str
    tool_shape: str
    context_window_policy: str
    preferred_retry_strategy: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.pop("renderer", None)
        return payload


_ADAPTATION_PROFILES = {
    "openclaw_default": AdaptationProfile(
        name="openclaw_default",
        display_name="OpenClaw Default",
        backend="local_openclaw",
        model_family="local",
        prompt_format="rendered_text_sections",
        renderer="render_openclaw_prompt",
        prompt_dialect="openclaw_text_sections",
        tool_shape="native_cli_tools",
        context_window_policy="compress_then_retry",
        preferred_retry_strategy="compact_then_repair",
        description="Current text-section prompt rendering for the local OpenClaw CLI runtime.",
    ),
    "qwen_compact_json": AdaptationProfile(
        name="qwen_compact_json",
        display_name="Qwen Compact JSON",
        backend="local_openclaw",
        model_family="qwen",
        prompt_format="compact_json_envelope",
        renderer="render_qwen_compact_json_prompt",
        prompt_dialect="compact_json",
        tool_shape="native_cli_tools",
        context_window_policy="compress_then_retry",
        preferred_retry_strategy="compact_then_repair",
        description="Compact JSON-biased rendering for local Qwen planning and repair turns.",
    ),
    "ollama_default": AdaptationProfile(
        name="ollama_default",
        display_name="Ollama Default",
        backend="direct_ollama",
        model_family="ollama",
        prompt_format="rendered_text_sections",
        renderer="render_openclaw_prompt",
        prompt_dialect="ollama_chat",
        tool_shape="none",
        context_window_policy="truncate_context",
        preferred_retry_strategy="schema_first",
        description="Model-agnostic prompt rendering for direct local Ollama chat models.",
    ),
    "claude_strict_tools": AdaptationProfile(
        name="claude_strict_tools",
        display_name="Claude Strict Tools",
        backend="remote_openclaw_gateway",
        model_family="claude",
        prompt_format="structured_prompt_envelope",
        renderer="render_claude_strict_tools_prompt",
        prompt_dialect="strict_tool_json",
        tool_shape="gateway_tool_schema",
        context_window_policy="truncate_context",
        preferred_retry_strategy="schema_first",
        description="Strict tool-schema rendering for Claude-family gateway adapters.",
    ),
    "openai_responses_default": AdaptationProfile(
        name="openai_responses_default",
        display_name="OpenAI Responses Default",
        backend="openai_responses_api",
        model_family="gpt-5",
        prompt_format="structured_prompt_envelope",
        renderer="render_openai_responses_prompt",
        prompt_dialect="responses_json",
        tool_shape="responses_tools",
        context_window_policy="summarize_context",
        preferred_retry_strategy="structured_retry",
        description="Planned profile for mapping neutral orchestration prompts into Responses-style inputs.",
    ),
    "openai_responses_structured": AdaptationProfile(
        name="openai_responses_structured",
        display_name="OpenAI Responses Structured",
        backend="openai_responses_api",
        model_family="gpt",
        prompt_format="structured_prompt_envelope",
        renderer="render_openai_responses_prompt",
        prompt_dialect="responses_json",
        tool_shape="responses_tools",
        context_window_policy="summarize_context",
        preferred_retry_strategy="structured_retry",
        description="Model-agnostic structured Responses profile for OpenAI-family orchestration prompts.",
    ),
}

_RENDERERS = {
    "render_openclaw_prompt": render_openclaw_prompt,
    "render_qwen_compact_json_prompt": render_qwen_compact_json_prompt,
    "render_claude_strict_tools_prompt": render_claude_strict_tools_prompt,
    "render_openai_responses_prompt": render_openai_responses_prompt,
}


def list_adaptation_profiles() -> List[AdaptationProfile]:
    return list(_ADAPTATION_PROFILES.values())


def get_adaptation_profile(name: Optional[str]) -> AdaptationProfile:
    normalized = (name or "openclaw_default").strip().lower()
    return _ADAPTATION_PROFILES.get(
        normalized, _ADAPTATION_PROFILES["openclaw_default"]
    )


def resolve_adaptation_profile(
    *,
    backend: Optional[str],
    model_family: Optional[str],
    preferred_name: Optional[str] = None,
) -> AdaptationProfile:
    if preferred_name:
        profile = get_adaptation_profile(preferred_name)
        if profile.backend == (backend or profile.backend) and (
            not model_family or profile.model_family in (model_family, "gpt")
        ):
            return profile

    normalized_backend = (backend or "local_openclaw").strip().lower()
    normalized_family = (model_family or "").strip().lower()
    for profile in _ADAPTATION_PROFILES.values():
        if profile.backend != normalized_backend:
            continue
        if not normalized_family:
            return profile
        if profile.model_family == normalized_family:
            return profile
        if normalized_family.startswith(profile.model_family):
            return profile
    return get_adaptation_profile(preferred_name or "openclaw_default")


def render_prompt_for_profile(name: Optional[str], envelope: PromptEnvelope) -> str:
    profile = get_adaptation_profile(name)
    renderer = _RENDERERS.get(profile.renderer, render_openclaw_prompt)
    return renderer(envelope)
