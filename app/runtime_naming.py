"""Canonical runtime names and compatibility aliases.

Keep roadmap-era names at runtime boundaries only. Internal orchestration code
should use architecture names from this module.
"""

from __future__ import annotations

from typing import Any, Optional

BOUNDED_DEBUG_REPAIR_PROMPT_MODE = "bounded_execution_debug_repair"
DIFF_SCOPED_DEBUG_REPAIR_PROMPT_MODE = "diff_scoped_debug_repair"
COMPLETION_REPAIR_CAPSULE_PROMPT_MODE = "completion_repair_capsule"

LEGACY_BOUNDED_DEBUG_REPAIR_PROMPT_MODE = "phase7f_bounded_debug_repair"
LEGACY_DIFF_SCOPED_DEBUG_REPAIR_PROMPT_MODE = "phase7g_diff_repair"
LEGACY_COMPLETION_REPAIR_CAPSULE_PROMPT_MODE = "phase7h_capsule"

BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON = "bounded_execution_debug_repair_timeout"
BOUNDED_DEBUG_REPAIR_OUTPUT_INVALID_REASON = (
    "bounded_execution_debug_repair_output_invalid"
)
BOUNDED_DEBUG_REPAIR_OPS_FIX_STALE_REPLACE_REASON = (
    "bounded_execution_debug_repair_ops_fix_stale_replace"
)

LEGACY_BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON = "phase7f_bounded_debug_timeout"
BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL = "BOUNDED_EXECUTION_DEBUG_REPAIR"
LEGACY_BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL = "PHASE7F_DEBUG_REPAIR"

BOUNDED_DEBUG_REPAIR_CONTEXT = "bounded_execution_debug_repair"
BOUNDED_DEBUG_REPAIR_COMPLIANCE_RETRY_CONTEXT = (
    "bounded_execution_debug_repair_compliance_retry"
)
BOUNDED_DEBUG_REPAIR_STALE_REPLACE_CORRECTION_CONTEXT = (
    "bounded_execution_debug_repair_ops_fix_stale_replace_correction"
)

DEBUG_REPAIR_LEGACY_ENV_ALIASES = {
    "PHASE7F_REPAIR_DIRECT_ENABLED": "DEBUG_REPAIR_DIRECT_ENABLED",
    "PHASE7F_REPAIR_BASE_URL": "DEBUG_REPAIR_BASE_URL",
    "PHASE7F_REPAIR_MODEL": "DEBUG_REPAIR_MODEL",
    "PHASE7F_REPAIR_API_KEY": "DEBUG_REPAIR_API_KEY",
    "PHASE7F_REPAIR_DISABLE_THINKING": "DEBUG_REPAIR_DISABLE_THINKING",
}


def canonical_debug_prompt_mode(mode: Optional[str]) -> Optional[str]:
    if mode == LEGACY_BOUNDED_DEBUG_REPAIR_PROMPT_MODE:
        return BOUNDED_DEBUG_REPAIR_PROMPT_MODE
    if mode == LEGACY_DIFF_SCOPED_DEBUG_REPAIR_PROMPT_MODE:
        return DIFF_SCOPED_DEBUG_REPAIR_PROMPT_MODE
    if mode == LEGACY_COMPLETION_REPAIR_CAPSULE_PROMPT_MODE:
        return COMPLETION_REPAIR_CAPSULE_PROMPT_MODE
    return mode


def legacy_debug_prompt_mode(mode: Optional[str]) -> Optional[str]:
    canonical = canonical_debug_prompt_mode(mode)
    if canonical == BOUNDED_DEBUG_REPAIR_PROMPT_MODE:
        return LEGACY_BOUNDED_DEBUG_REPAIR_PROMPT_MODE
    if canonical == DIFF_SCOPED_DEBUG_REPAIR_PROMPT_MODE:
        return LEGACY_DIFF_SCOPED_DEBUG_REPAIR_PROMPT_MODE
    if canonical == COMPLETION_REPAIR_CAPSULE_PROMPT_MODE:
        return LEGACY_COMPLETION_REPAIR_CAPSULE_PROMPT_MODE
    return mode


def is_bounded_debug_repair_mode(mode: Optional[str]) -> bool:
    return canonical_debug_prompt_mode(mode) == BOUNDED_DEBUG_REPAIR_PROMPT_MODE


def is_diff_scoped_debug_repair_mode(mode: Optional[str]) -> bool:
    return canonical_debug_prompt_mode(mode) == DIFF_SCOPED_DEBUG_REPAIR_PROMPT_MODE


def debug_prompt_mode_alias_details(mode: Optional[str]) -> dict[str, Optional[str]]:
    canonical = canonical_debug_prompt_mode(mode)
    return {
        "debug_prompt_mode": legacy_debug_prompt_mode(canonical),
        "debug_prompt_mode_architecture": canonical,
    }


def completion_repair_prompt_mode_alias_details() -> dict[str, str]:
    return {
        "completion_repair_prompt_mode": LEGACY_COMPLETION_REPAIR_CAPSULE_PROMPT_MODE,
        "completion_repair_prompt_mode_architecture": (
            COMPLETION_REPAIR_CAPSULE_PROMPT_MODE
        ),
    }


def bounded_debug_repair_timeout_alias_details(value: Any = True) -> dict[str, Any]:
    return {
        LEGACY_BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON: value,
        BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON: value,
    }


def canonical_diagnostic_label(label: Optional[str]) -> Optional[str]:
    if label == LEGACY_BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL:
        return BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL
    return label


def diagnostic_label_alias_details(label: Optional[str]) -> dict[str, Optional[str]]:
    canonical = canonical_diagnostic_label(label)
    if canonical == BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL:
        return {
            "diagnostic_label": LEGACY_BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL,
            "diagnostic_label_architecture": BOUNDED_DEBUG_REPAIR_DIAGNOSTIC_LABEL,
        }
    return {
        "diagnostic_label": label,
        "diagnostic_label_architecture": canonical,
    }
