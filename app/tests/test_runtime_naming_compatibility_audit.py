from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_audit_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "runtime_naming_compatibility_audit.py"
    )
    spec = importlib.util.spec_from_file_location(
        "runtime_naming_compatibility_audit", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _load_audit_module()


def test_phase11p_runtime_naming_audit_covers_required_surfaces():
    old_names = {surface.old_name for surface in audit.AUDIT_SURFACES}

    assert {
        "phase7f_used",
        "phase7g_used",
        "phase7f_used_count",
        "phase7g_used_count",
        "phase7f_exercised_rate",
        "phase7g_exercised_rate",
        "phase7f_bounded_debug_repair",
        "phase7g_diff_repair",
        "phase7f_bounded_debug_timeout",
        "phase7f_rejection_reason",
        "phase7f_parsed_shape",
        "phase7f_raw_output_excerpt",
        "phase7f_debug_repair_output_invalid",
        "phase7f_ops_fix_stale_replace",
        "phase7f_ops_fix_correction",
        "PHASE7F_DEBUG_REPAIR",
        "PHASE7F_REPAIR_*",
    } <= old_names


def test_phase11p_runtime_naming_audit_tokens_are_present():
    assert audit.validate_surfaces() == []


def test_phase11p_runtime_naming_audit_recommends_no_removal_yet():
    assert all("Keep" in surface.recommended_action for surface in audit.AUDIT_SURFACES)


def test_phase11p_runtime_naming_audit_records_reader_policy():
    policies = {
        surface.old_name: surface.reader_policy for surface in audit.AUDIT_SURFACES
    }

    assert "prefer" in policies["phase7f_used"].lower()
    assert "prefer" in policies["phase7g_used"].lower()
    assert "debug_prompt_mode_architecture" in policies["phase7f_bounded_debug_repair"]
    assert "DEBUG_REPAIR_*" in policies["PHASE7F_REPAIR_*"]
