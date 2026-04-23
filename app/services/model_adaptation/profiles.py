"""Configured model adaptation profiles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from .renderers import render_openai_responses_prompt, render_openclaw_prompt
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
        description="Current text-section prompt rendering for the local OpenClaw CLI runtime.",
    ),
    "openai_responses_default": AdaptationProfile(
        name="openai_responses_default",
        display_name="OpenAI Responses Default",
        backend="openai_responses_api",
        model_family="gpt-5",
        prompt_format="structured_prompt_envelope",
        renderer="render_openai_responses_prompt",
        description="Planned profile for mapping neutral orchestration prompts into Responses-style inputs.",
    ),
}

_RENDERERS = {
    "render_openclaw_prompt": render_openclaw_prompt,
    "render_openai_responses_prompt": render_openai_responses_prompt,
}


def list_adaptation_profiles() -> List[AdaptationProfile]:
    return list(_ADAPTATION_PROFILES.values())


def get_adaptation_profile(name: Optional[str]) -> AdaptationProfile:
    normalized = (name or "openclaw_default").strip().lower()
    return _ADAPTATION_PROFILES.get(
        normalized, _ADAPTATION_PROFILES["openclaw_default"]
    )


def render_prompt_for_profile(name: Optional[str], envelope: PromptEnvelope) -> str:
    profile = get_adaptation_profile(name)
    renderer = _RENDERERS.get(profile.renderer, render_openclaw_prompt)
    return renderer(envelope)
