from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_burn_in_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "runtime_naming_burn_in_report.py"
    )
    spec = importlib.util.spec_from_file_location("runtime_naming_burn_in_report", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


burn_in = _load_burn_in_module()


def test_phase11q_burn_in_report_aliases_match():
    report = burn_in.build_burn_in_report()

    assert report["passed"] is True
    assert report["audit_failures"] == []
    assert report["audit_surface_count"] >= 17
    assert all(check["passed"] for check in report["alias_checks"])


def test_phase11q_burn_in_report_emits_old_and_architecture_names():
    report = burn_in.build_burn_in_report()
    path_observability = report["per_run_reports"][0]["path_observability"]
    aggregate = report["aggregate"]
    metadata = report["runtime_metadata_checks"]

    assert path_observability["phase7f_used"] is True
    assert path_observability["bounded_execution_debug_repair_used"] is True
    assert "phase7f_used_count" not in aggregate
    assert "bounded_execution_debug_repair_used_count" in aggregate
    assert metadata["phase7f_debug_prompt_mode"] == "phase7f_bounded_debug_repair"
    assert (
        metadata["bounded_execution_debug_prompt_mode"]
        == "bounded_execution_debug_repair"
    )
    assert metadata["diagnostic_label"] == "PHASE7F_DEBUG_REPAIR"
    assert metadata["diagnostic_label_architecture"] == "BOUNDED_EXECUTION_DEBUG_REPAIR"
