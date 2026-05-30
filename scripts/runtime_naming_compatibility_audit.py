#!/usr/bin/env python3
"""Audit Phase 11P runtime naming compatibility readiness."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AuditSurface:
    old_name: str
    architecture_alias: str
    emitted_by: tuple[str, ...]
    read_by: tuple[str, ...]
    historical_report_impact: str
    removal_risk: str
    recommended_action: str
    reader_policy: str
    write_status: str
    required_tokens: tuple[str, ...]


AUDIT_SURFACES: tuple[AuditSurface, ...] = (
    AuditSurface(
        old_name="phase7f_used",
        architecture_alias="bounded_execution_debug_repair_used",
        emitted_by=("scripts/score_orchestrator_eval_case.py",),
        read_by=("scripts/evals/run_orchestrator_eval_slice.py",),
        historical_report_impact="Historical per-run eval reports use the old key.",
        removal_risk="High until all active readers prefer the architecture key and historical reports stay readable.",
        recommended_action="Keep old writes; keep reader fallback permanently or migrate report schema explicitly.",
        reader_policy="Active aggregate readers prefer the architecture key and fall back to the old key.",
        write_status="compatibility_write_retained",
        required_tokens=("phase7f_used", "bounded_execution_debug_repair_used"),
    ),
    AuditSurface(
        old_name="phase7g_used",
        architecture_alias="diff_scoped_debug_repair_used",
        emitted_by=("scripts/score_orchestrator_eval_case.py",),
        read_by=("scripts/evals/run_orchestrator_eval_slice.py",),
        historical_report_impact="Historical per-run eval reports use the old key.",
        removal_risk="High until all active readers prefer the architecture key and historical reports stay readable.",
        recommended_action="Keep old writes; keep reader fallback permanently or migrate report schema explicitly.",
        reader_policy="Active aggregate readers prefer the architecture key and fall back to the old key.",
        write_status="compatibility_write_retained",
        required_tokens=("phase7g_used", "diff_scoped_debug_repair_used"),
    ),
    AuditSurface(
        old_name="phase7f_used_count",
        architecture_alias="bounded_execution_debug_repair_used_count",
        emitted_by=("scripts/evals/run_orchestrator_eval_slice.py",),
        read_by=("scripts/evals/run_orchestrator_eval_slice.py",),
        historical_report_impact="Historical aggregate eval reports use the old count key.",
        removal_risk="High for longitudinal comparisons and dashboards that ingest aggregate JSON.",
        recommended_action="Old aggregate write removed in Phase 11S; keep architecture output and historical per-run fallback.",
        reader_policy="New aggregate reports emit only the architecture key; old per-run input fallback remains tested.",
        write_status="compatibility_write_removed_phase11s",
        required_tokens=("bounded_execution_debug_repair_used_count",),
    ),
    AuditSurface(
        old_name="phase7g_used_count",
        architecture_alias="diff_scoped_debug_repair_used_count",
        emitted_by=("scripts/evals/run_orchestrator_eval_slice.py",),
        read_by=("scripts/evals/run_orchestrator_eval_slice.py",),
        historical_report_impact="Historical aggregate eval reports use the old count key.",
        removal_risk="High for longitudinal comparisons and dashboards that ingest aggregate JSON.",
        recommended_action="Old aggregate write removed in Phase 11S; keep architecture output and historical per-run fallback.",
        reader_policy="New aggregate reports emit only the architecture key; old per-run input fallback remains tested.",
        write_status="compatibility_write_removed_phase11s",
        required_tokens=("diff_scoped_debug_repair_used_count",),
    ),
    AuditSurface(
        old_name="phase7f_exercised_rate",
        architecture_alias="bounded_execution_debug_repair_exercised_rate",
        emitted_by=("scripts/evals/run_orchestrator_eval_slice.py",),
        read_by=("scripts/evals/run_orchestrator_eval_slice.py",),
        historical_report_impact="Historical aggregate eval reports use the old rate key.",
        removal_risk="High for trend reports and release-readiness comparisons.",
        recommended_action="Old aggregate write removed in Phase 11S; keep architecture output and historical per-run fallback.",
        reader_policy="New aggregate reports emit only the architecture key; old per-run input fallback remains tested.",
        write_status="compatibility_write_removed_phase11s",
        required_tokens=("bounded_execution_debug_repair_exercised_rate",),
    ),
    AuditSurface(
        old_name="phase7g_exercised_rate",
        architecture_alias="diff_scoped_debug_repair_exercised_rate",
        emitted_by=("scripts/evals/run_orchestrator_eval_slice.py",),
        read_by=("scripts/evals/run_orchestrator_eval_slice.py",),
        historical_report_impact="Historical aggregate eval reports use the old rate key.",
        removal_risk="High for trend reports and release-readiness comparisons.",
        recommended_action="Old aggregate write removed in Phase 11S; keep architecture output and historical per-run fallback.",
        reader_policy="New aggregate reports emit only the architecture key; old per-run input fallback remains tested.",
        write_status="compatibility_write_removed_phase11s",
        required_tokens=("diff_scoped_debug_repair_exercised_rate",),
    ),
    AuditSurface(
        old_name="phase7f_bounded_debug_repair",
        architecture_alias="bounded_execution_debug_repair",
        emitted_by=("app/services/orchestration/phases/execution_loop.py",),
        read_by=(
            "scripts/score_orchestrator_eval_case.py",
            "app/services/orchestration/phases/failure_flow.py",
            "scripts/workspace_evidence_report.py",
        ),
        historical_report_impact="Historical event journals use the old prompt mode.",
        removal_risk="High because failure classification and reports still accept old journals.",
        recommended_action="Keep old prompt mode; keep additive debug_prompt_mode_architecture metadata.",
        reader_policy="Scorer path observability treats debug_prompt_mode_architecture as authoritative when present.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "phase7f_bounded_debug_repair",
            "bounded_execution_debug_repair",
            "debug_prompt_mode_architecture",
        ),
    ),
    AuditSurface(
        old_name="phase7g_diff_repair",
        architecture_alias="diff_scoped_debug_repair",
        emitted_by=("app/services/orchestration/phases/execution_loop.py",),
        read_by=(
            "scripts/score_orchestrator_eval_case.py",
            "scripts/workspace_evidence_report.py",
        ),
        historical_report_impact="Historical event journals use the old prompt mode.",
        removal_risk="Medium-high for eval path observability and evidence reports.",
        recommended_action="Keep old prompt mode; keep additive debug_prompt_mode_architecture metadata.",
        reader_policy="Scorer path observability treats debug_prompt_mode_architecture as authoritative when present.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "phase7g_diff_repair",
            "diff_scoped_debug_repair",
            "debug_prompt_mode_architecture",
        ),
    ),
    AuditSurface(
        old_name="phase7f_bounded_debug_timeout",
        architecture_alias="bounded_execution_debug_repair_timeout",
        emitted_by=(
            "app/services/orchestration/phases/execution_loop.py",
            "app/services/orchestration/phases/failure_flow.py",
        ),
        read_by=("app/services/orchestration/phases/failure_flow.py",),
        historical_report_impact="Historical diagnostics and terminal reasons use the old timeout marker.",
        removal_risk="High because terminalization logic still recognizes old diagnostics.",
        recommended_action="Keep old timeout marker until parser and terminal reason migration is explicit.",
        reader_policy="Failure-flow timeout classification accepts architecture metadata and old diagnostics.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "phase7f_bounded_debug_timeout",
            "bounded_execution_debug_repair_timeout",
        ),
    ),
    AuditSurface(
        old_name="phase7f_rejection_reason",
        architecture_alias="bounded_execution_debug_repair_rejection_reason",
        emitted_by=("app/services/orchestration/phases/execution_loop.py",),
        read_by=("app/tests/test_phase7f_debug_feedback.py",),
        historical_report_impact="Historical repair_rejected events use the old detail field.",
        removal_risk="Medium-high for report readers and regression fixtures.",
        recommended_action="Keep old rejection detail fields; prefer architecture aliases in new readers.",
        reader_policy="No removal; current regression coverage asserts architecture aliases are emitted.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "phase7f_rejection_reason",
            "bounded_execution_debug_repair_rejection_reason",
        ),
    ),
    AuditSurface(
        old_name="phase7f_parsed_shape",
        architecture_alias="bounded_execution_debug_repair_parsed_shape",
        emitted_by=("app/services/orchestration/phases/execution_loop.py",),
        read_by=("app/tests/test_phase7f_debug_feedback.py",),
        historical_report_impact="Historical invalid-output diagnostics use the old parsed-shape field.",
        removal_risk="Medium for diagnostics and fixtures.",
        recommended_action="Keep old field; keep additive alias.",
        reader_policy="No removal; current regression coverage asserts architecture aliases are emitted.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "phase7f_parsed_shape",
            "bounded_execution_debug_repair_parsed_shape",
        ),
    ),
    AuditSurface(
        old_name="phase7f_raw_output_excerpt",
        architecture_alias="bounded_execution_debug_repair_raw_output_excerpt",
        emitted_by=("app/services/orchestration/phases/execution_loop.py",),
        read_by=("app/tests/test_phase7f_debug_feedback.py",),
        historical_report_impact="Historical invalid-output diagnostics use the old excerpt field.",
        removal_risk="Medium for diagnostics and fixtures.",
        recommended_action="Keep old field; keep additive alias.",
        reader_policy="No removal; current regression coverage asserts architecture aliases are emitted.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "phase7f_raw_output_excerpt",
            "bounded_execution_debug_repair_raw_output_excerpt",
        ),
    ),
    AuditSurface(
        old_name="phase7f_debug_repair_output_invalid",
        architecture_alias="bounded_execution_debug_repair_output_invalid",
        emitted_by=("app/services/orchestration/phases/execution_loop.py",),
        read_by=("app/tests/test_phase7f_debug_feedback.py",),
        historical_report_impact="Historical rejection reasons use the old reason.",
        removal_risk="High if historical reports or alerting parse rejection reasons.",
        recommended_action="Keep old reason; keep reason_architecture metadata.",
        reader_policy="No removal; current regression coverage asserts architecture reason metadata is emitted.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "phase7f_debug_repair_output_invalid",
            "bounded_execution_debug_repair_output_invalid",
        ),
    ),
    AuditSurface(
        old_name="phase7f_ops_fix_stale_replace",
        architecture_alias="bounded_execution_debug_repair_ops_fix_stale_replace",
        emitted_by=("app/services/orchestration/phases/execution_loop.py",),
        read_by=("app/tests/test_phase7f_debug_feedback.py",),
        historical_report_impact="Historical stale replace rejection reasons use the old reason.",
        removal_risk="High if operator reports group stale exact-patch failures by reason.",
        recommended_action="Keep old reason; keep architecture reason and terminal_reason aliases.",
        reader_policy="No removal; current regression coverage asserts architecture reason metadata is emitted.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "phase7f_ops_fix_stale_replace",
            "bounded_execution_debug_repair_ops_fix_stale_replace",
        ),
    ),
    AuditSurface(
        old_name="phase7f_ops_fix_correction",
        architecture_alias="bounded_execution_debug_repair_ops_fix_correction",
        emitted_by=("app/services/orchestration/phases/execution_loop.py",),
        read_by=("app/tests/test_phase7f_debug_feedback.py",),
        historical_report_impact="Historical correction events use the old marker.",
        removal_risk="Medium for repair diagnostics.",
        recommended_action="Keep old marker; keep additive alias.",
        reader_policy="No removal; current regression coverage asserts architecture aliases are emitted.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "phase7f_ops_fix_correction",
            "bounded_execution_debug_repair_ops_fix_correction",
        ),
    ),
    AuditSurface(
        old_name="PHASE7F_DEBUG_REPAIR",
        architecture_alias="BOUNDED_EXECUTION_DEBUG_REPAIR",
        emitted_by=("app/services/agents/openclaw_service.py",),
        read_by=("app/services/agents/openclaw_service.py",),
        historical_report_impact="Historical runtime diagnostics and logs use the old diagnostic label.",
        removal_risk="High because direct runtime diagnostics still preserve old labels for compatibility.",
        recommended_action="Keep old diagnostic label writes; keep architecture label as input alias and metadata.",
        reader_policy="OpenClaw diagnostics accept the architecture label while preserving old label metadata.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "PHASE7F_DEBUG_REPAIR",
            "BOUNDED_EXECUTION_DEBUG_REPAIR",
            "diagnostic_label_architecture",
        ),
    ),
    AuditSurface(
        old_name="PHASE7F_REPAIR_*",
        architecture_alias="DEBUG_REPAIR_*",
        emitted_by=("app/config.py",),
        read_by=("app/services/agents/openclaw_service.py",),
        historical_report_impact="Environment compatibility for existing deployments depends on the old settings.",
        removal_risk="High operational risk for configured repair lanes.",
        recommended_action="Keep config fallbacks until an explicit operator migration is documented and tested.",
        reader_policy="Runtime config prefers DEBUG_REPAIR_* values and falls back to PHASE7F_REPAIR_* values.",
        write_status="compatibility_write_retained",
        required_tokens=(
            "PHASE7F_REPAIR_DIRECT_ENABLED",
            "PHASE7F_REPAIR_BASE_URL",
            "PHASE7F_REPAIR_MODEL",
            "PHASE7F_REPAIR_API_KEY",
            "PHASE7F_REPAIR_DISABLE_THINKING",
            "DEBUG_REPAIR_DIRECT_ENABLED",
            "DEBUG_REPAIR_BASE_URL",
            "DEBUG_REPAIR_MODEL",
            "DEBUG_REPAIR_API_KEY",
            "DEBUG_REPAIR_DISABLE_THINKING",
        ),
    ),
)


def _read_files(paths: Iterable[str]) -> str:
    chunks: list[str] = []
    for relative_path in paths:
        path = REPO_ROOT / relative_path
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def validate_surfaces(surfaces: Iterable[AuditSurface] = AUDIT_SURFACES) -> list[str]:
    failures: list[str] = []
    for surface in surfaces:
        searchable_text = _read_files((*surface.emitted_by, *surface.read_by))
        for token in surface.required_tokens:
            if token not in searchable_text:
                failures.append(f"{surface.old_name}: missing token {token!r}")
    return failures


def surfaces_as_dicts() -> list[dict[str, object]]:
    return [asdict(surface) for surface in AUDIT_SURFACES]


def surfaces_as_markdown() -> str:
    header = (
        "| Old name | Architecture alias | Emitted by | Read by | "
        "Historical report impact | Removal risk | Reader policy | Write status | Recommended action |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    rows = []
    for surface in AUDIT_SURFACES:
        rows.append(
            "| {old} | {alias} | {emitted} | {read} | {impact} | {risk} | {policy} | {status} | {action} |".format(
                old=f"`{surface.old_name}`",
                alias=f"`{surface.architecture_alias}`",
                emitted="<br>".join(f"`{path}`" for path in surface.emitted_by),
                read="<br>".join(f"`{path}`" for path in surface.read_by),
                impact=surface.historical_report_impact,
                risk=surface.removal_risk,
                policy=surface.reader_policy,
                status=surface.write_status,
                action=surface.recommended_action,
            )
        )
    return "\n".join((header, *rows))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and print the Phase 11P runtime naming compatibility audit."
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format.",
    )
    args = parser.parse_args()

    failures = validate_surfaces()
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1

    if args.format == "json":
        print(json.dumps(surfaces_as_dicts(), indent=2, sort_keys=True))
    else:
        print(surfaces_as_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
