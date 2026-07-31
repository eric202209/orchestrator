"""Phase 31K regression ownership for configuration governance."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# These declarations are consumed by Docker, build identity, frontend Vite, or
# operator scripts rather than by the Pydantic Settings model.
NON_SETTINGS_DECLARATIONS = {
    "CDP_BIND_HOST",
    "HOST_WORKSPACE_ROOT",
    "LLAMA_CTX",
    "NOVNC_BIND_HOST",
    "OPENCLAW_WORKSPACE",
    "ORCHESTRATOR_BUILD_TIME",
    "ORCHESTRATOR_CONFIG_SOURCE",
    "ORCHESTRATOR_GIT_SHA",
    "ORCHESTRATOR_IMAGE_ID",
    "ORCHESTRATOR_IMAGE_TAG",
    "ORCHESTRATOR_REPO_GIT_SHA",
    "RELAY_EXPECTED_CONVERSATION_URL",
    "SLOT_BASED_PLANNING_REPAIR_EXPERIMENT",
    "VITE_API_URL",
    "VITE_API_WS_HOST",
    "VRAM_LIMIT_MB",
    "WORKSPACE_ROOT",
}


def _declared_environment_names(text: str) -> set[str]:
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))


def test_env_example_declares_every_settings_field_and_only_reviewed_extras():
    declared = _declared_environment_names(ENV_EXAMPLE.read_text(encoding="utf-8"))
    settings_names = set(Settings.model_fields)

    assert settings_names <= declared
    assert declared - settings_names == NON_SETTINGS_DECLARATIONS
