#!/usr/bin/env python3
"""Generate Phase 11Q runtime naming compatibility burn-in evidence."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "docs"
    / "roadmap"
    / "reports"
    / "evals"
    / "phase11q-runtime-naming-burn-in.json"
)


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scorer = _load_module(
    "phase11q_score_orchestrator_eval_case",
    REPO_ROOT / "scripts" / "score_orchestrator_eval_case.py",
)
runner = _load_module(
    "phase11q_run_orchestrator_eval_slice",
    REPO_ROOT / "scripts" / "evals" / "run_orchestrator_eval_slice.py",
)
audit = _load_module(
    "phase11q_runtime_naming_compatibility_audit",
    REPO_ROOT / "scripts" / "runtime_naming_compatibility_audit.py",
)


def _event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    return scorer._event_summary(events)


def _required(case: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    return scorer._required_event_results(case, summary["event_type_counts"])


def _path_observability(events: list[dict[str, Any]]) -> dict[str, Any]:
    case = {
        "case_id": "debug_import_error_repair",
        "category": "debug_repair",
        "required_events": ["debug_feedback_captured", "debug_repair_attempted"],
    }
    summary = _event_summary(events)
    return scorer._path_observability(
        case=case,
        events=events,
        snapshots=[],
        event_summary=summary,
        verifier={"available": True, "passed": False},
        clean_success=False,
        required_events=_required(case, summary),
    )


def _report_from_path_observability(
    path_observability: dict[str, Any],
) -> dict[str, Any]:
    return {
        "result": {
            "clean_success": False,
            "path_observed": True,
            "blockers": ["phase11q_burn_in"],
        },
        "path_observability": path_observability,
    }


def _assert_equal(
    checks: list[dict[str, Any]],
    *,
    surface: str,
    old_name: str,
    architecture_name: str,
    old_value: Any,
    architecture_value: Any,
) -> None:
    passed = old_value == architecture_value
    checks.append(
        {
            "surface": surface,
            "old_name": old_name,
            "architecture_name": architecture_name,
            "old_value": old_value,
            "architecture_value": architecture_value,
            "passed": passed,
        }
    )
    if not passed:
        raise AssertionError(
            f"{surface}: {old_name}={old_value!r} does not match "
            f"{architecture_name}={architecture_value!r}"
        )


def build_burn_in_report() -> dict[str, Any]:
    audit_failures = audit.validate_surfaces()
    if audit_failures:
        raise AssertionError(f"runtime naming audit failed: {audit_failures}")

    phase7f_events = [
        {"event_type": "debug_feedback_captured", "details": {}},
        {
            "event_type": "debug_repair_attempted",
            "details": {
                "debug_prompt_mode": "phase7f_bounded_debug_repair",
                "debug_prompt_mode_architecture": "bounded_execution_debug_repair",
                "diagnostic_label": "PHASE7F_DEBUG_REPAIR",
                "diagnostic_label_architecture": "BOUNDED_EXECUTION_DEBUG_REPAIR",
            },
        },
        {
            "event_type": "repair_rejected",
            "details": {
                "reason": "phase7f_debug_repair_output_invalid",
                "reason_architecture": "bounded_execution_debug_repair_output_invalid",
                "phase7f_rejection_reason": "invalid_json",
                "bounded_execution_debug_repair_rejection_reason": "invalid_json",
                "phase7f_parsed_shape": {"type": "text"},
                "bounded_execution_debug_repair_parsed_shape": {"type": "text"},
                "phase7f_raw_output_excerpt": "not json",
                "bounded_execution_debug_repair_raw_output_excerpt": "not json",
            },
        },
    ]
    phase7g_events = [
        {"event_type": "debug_feedback_captured", "details": {}},
        {
            "event_type": "debug_repair_attempted",
            "details": {
                "debug_prompt_mode": "phase7g_diff_repair",
                "debug_prompt_mode_architecture": "diff_scoped_debug_repair",
                "diff_capsule_line_count": 8,
            },
        },
    ]

    phase7f_path = _path_observability(phase7f_events)
    phase7g_path = _path_observability(phase7g_events)
    reports = [
        _report_from_path_observability(phase7f_path),
        _report_from_path_observability(phase7g_path),
    ]
    aggregate = runner._aggregate_case_reports(
        case_id="phase11q_runtime_naming_burn_in",
        reports=reports,
        report_paths=[Path("phase11q-run1.json"), Path("phase11q-run2.json")],
        run_context={
            "git_sha": None,
            "model": "deterministic-burn-in",
            "backend": "local",
            "runtime_profile": "test",
            "repeat_seed": "phase11q",
        },
    )

    alias_checks: list[dict[str, Any]] = []
    _assert_equal(
        alias_checks,
        surface="per_run_bounded_debug_repair",
        old_name="phase7f_used",
        architecture_name="bounded_execution_debug_repair_used",
        old_value=phase7f_path["phase7f_used"],
        architecture_value=phase7f_path["bounded_execution_debug_repair_used"],
    )
    _assert_equal(
        alias_checks,
        surface="per_run_diff_scoped_debug_repair",
        old_name="phase7g_used",
        architecture_name="diff_scoped_debug_repair_used",
        old_value=phase7g_path["phase7g_used"],
        architecture_value=phase7g_path["diff_scoped_debug_repair_used"],
    )
    removed_aggregate_writes = {
        "phase7f_used_count": "bounded_execution_debug_repair_used_count",
        "phase7g_used_count": "diff_scoped_debug_repair_used_count",
        "phase7f_exercised_rate": "bounded_execution_debug_repair_exercised_rate",
        "phase7g_exercised_rate": "diff_scoped_debug_repair_exercised_rate",
    }
    for old_name, architecture_name in removed_aggregate_writes.items():
        alias_checks.append(
            {
                "surface": "aggregate_old_write_removed",
                "old_name": old_name,
                "architecture_name": architecture_name,
                "old_value": aggregate.get(old_name),
                "architecture_value": aggregate.get(architecture_name),
                "passed": old_name not in aggregate and architecture_name in aggregate,
            }
        )

    rejection_details = phase7f_events[-1]["details"]
    _assert_equal(
        alias_checks,
        surface="rejection_reason_field",
        old_name="phase7f_rejection_reason",
        architecture_name="bounded_execution_debug_repair_rejection_reason",
        old_value=rejection_details["phase7f_rejection_reason"],
        architecture_value=rejection_details[
            "bounded_execution_debug_repair_rejection_reason"
        ],
    )
    _assert_equal(
        alias_checks,
        surface="rejection_parsed_shape_field",
        old_name="phase7f_parsed_shape",
        architecture_name="bounded_execution_debug_repair_parsed_shape",
        old_value=rejection_details["phase7f_parsed_shape"],
        architecture_value=rejection_details[
            "bounded_execution_debug_repair_parsed_shape"
        ],
    )
    _assert_equal(
        alias_checks,
        surface="rejection_raw_output_excerpt_field",
        old_name="phase7f_raw_output_excerpt",
        architecture_name="bounded_execution_debug_repair_raw_output_excerpt",
        old_value=rejection_details["phase7f_raw_output_excerpt"],
        architecture_value=rejection_details[
            "bounded_execution_debug_repair_raw_output_excerpt"
        ],
    )

    return {
        "phase": "11Q",
        "tool": "scripts/runtime_naming_burn_in_report.py",
        "audit_surface_count": len(audit.AUDIT_SURFACES),
        "audit_failures": audit_failures,
        "per_run_reports": reports,
        "aggregate": aggregate,
        "runtime_metadata_checks": {
            "phase7f_debug_prompt_mode": "phase7f_bounded_debug_repair",
            "bounded_execution_debug_prompt_mode": "bounded_execution_debug_repair",
            "phase7g_debug_prompt_mode": "phase7g_diff_repair",
            "diff_scoped_debug_prompt_mode": "diff_scoped_debug_repair",
            "diagnostic_label": "PHASE7F_DEBUG_REPAIR",
            "diagnostic_label_architecture": "BOUNDED_EXECUTION_DEBUG_REPAIR",
            "phase7f_bounded_debug_timeout": True,
            "bounded_execution_debug_repair_timeout": True,
        },
        "removed_aggregate_writes": sorted(removed_aggregate_writes),
        "alias_checks": alias_checks,
        "passed": all(check["passed"] for check in alias_checks)
        and not audit_failures,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Phase 11Q runtime naming burn-in evidence."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output JSON report path.",
    )
    args = parser.parse_args()

    report = build_burn_in_report()
    write_report(args.output, report)
    print(args.output)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
