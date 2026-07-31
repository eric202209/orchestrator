"""Phase 31K regression ownership for configuration governance."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import LEGACY_ENV_ALIASES, Settings


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CONFIGURATION_CONTRACT = REPO_ROOT / "docs/roadmap/configuration-contract.md"
FEATURE_FLAG_INVENTORY = REPO_ROOT / "docs/roadmap/feature-flag-inventory.md"
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"

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


def test_legacy_aliases_are_documented_with_canonical_replacements():
    contract = CONFIGURATION_CONTRACT.read_text(encoding="utf-8")
    for legacy_name, canonical_name in LEGACY_ENV_ALIASES.items():
        assert f"`{legacy_name}`" in contract
        assert f"`{canonical_name}`" in contract


def test_feature_flag_inventory_covers_settings_flags_and_direct_env_flag():
    inventory = FEATURE_FLAG_INVENTORY.read_text(encoding="utf-8")
    settings_flags = {
        name
        for name in Settings.model_fields
        if name.endswith("_ENABLED")
        or name
        in {
            "ALLOW_TEST_ENDPOINTS",
            "DEMO_MODE",
            "ENABLE_TEST_RUNTIME_BACKENDS",
            "INLINE_PLANNING",
        }
    }
    settings_flags.add("ALLOW_TEST_KEYPAIR_ENDPOINT")

    for name in settings_flags | {"SLOT_BASED_PLANNING_REPAIR_EXPERIMENT"}:
        assert f"`{name}`" in inventory


def test_ci_runs_actionlint_before_build():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "uses: rhysd/actionlint@v1" in workflow
    assert "- lint-workflow" in workflow
