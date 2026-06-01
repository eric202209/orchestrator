#!/usr/bin/env python3
"""Read-only Phase 12E attribution for debug-repair failures."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


TARGET_CASE_ID = "debug_import_error_repair"
TARGET_FAILURE_PHASE = "debug_repair"
TARGET_CONVERGENCE_CLASS = "cross_stage_contract_regression"


@dataclass(frozen=True)
class AnalysisError(Exception):
    message: str


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise AnalysisError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AnalysisError(f"Expected JSON object in {path}")
    return payload


def _load_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []

    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnalysisError(f"Invalid JSONL in {path}:{index}: {exc}") from exc
        if isinstance(event, dict):
            events.append(event)
    return events


def _counter_value(report: dict[str, Any], key: str) -> int:
    repair_events = report.get("events", {}).get("repair_events", {})
    if isinstance(repair_events, dict):
        value = repair_events.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    event_counts = report.get("events", {}).get("event_type_counts", {})
    if isinstance(event_counts, dict):
        value = event_counts.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _event_count(events: list[dict[str, Any]], event_type: str) -> int:
    return sum(1 for event in events if event.get("event_type") == event_type)


def _last_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


def _details(event: dict[str, Any] | None) -> dict[str, Any]:
    if not event:
        return {}
    details = event.get("details")
    return details if isinstance(details, dict) else {}


def _format_shape(shape: Any) -> str:
    if not isinstance(shape, dict):
        return "unknown"
    shape_type = shape.get("type")
    if shape_type == "list":
        length = shape.get("length", "?")
        first_item_type = shape.get("first_item_type", "?")
        keys = shape.get("first_item_keys")
        suffix = ""
        if isinstance(keys, list):
            suffix = " keys=" + ",".join(str(key) for key in keys)
        return f"list[{length}] first_item={first_item_type}{suffix}"
    if shape_type:
        return str(shape_type)
    return "unknown"


def _repair_output_shape(rejected_details: dict[str, Any]) -> str:
    for key in (
        "bounded_execution_debug_repair_parsed_shape",
        "debug_repair_parsed_shape",
        "phase7f_parsed_shape",
    ):
        shape = rejected_details.get(key)
        formatted = _format_shape(shape)
        if formatted != "unknown":
            return formatted
    return "unknown"


def _rejection_reason(rejected_details: dict[str, Any]) -> str | None:
    for key in (
        "bounded_execution_debug_repair_rejection_reason",
        "debug_repair_rejection_reason",
        "phase7f_repair_rejection_reason",
        "debug_repair_terminal_reason",
        "reason_architecture",
        "reason",
    ):
        value = rejected_details.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _verification_result(report: dict[str, Any]) -> str:
    verifier = report.get("verifier")
    if not isinstance(verifier, dict):
        return "unavailable"
    passed = verifier.get("passed")
    exit_code = verifier.get("exit_code")
    return f"passed={passed} exit_code={exit_code}"


def _terminal_event(report: dict[str, Any]) -> str:
    result = report.get("result")
    if not isinstance(result, dict):
        return "unknown"
    completed = result.get("task_completed_event_present")
    failed = result.get("task_failed_event_present")
    if completed:
        return "task_completed"
    if failed:
        return "task_failed"
    return "missing"


def _should_include(report: dict[str, Any]) -> bool:
    case = report.get("case")
    if isinstance(case, dict) and case.get("case_id") != TARGET_CASE_ID:
        return False
    result = report.get("result")
    if isinstance(result, dict) and result.get("clean_success") is True:
        return False
    path = report.get("path_observability")
    if not isinstance(path, dict):
        return False
    return (
        path.get("primary_failure_phase") == TARGET_FAILURE_PHASE
        and path.get("cross_stage_convergence_class") == TARGET_CONVERGENCE_CLASS
    )


def _classify_failure(
    *,
    feedback_present: bool,
    repair_attempted: bool,
    repair_rejected: bool,
    repair_applied: bool,
    rejection_reason: str | None,
    verifier_passed: bool | None,
    touched_files: list[str],
) -> str:
    if not feedback_present:
        return "missing_failure_feedback"
    if not repair_attempted:
        return "debug_repair_not_attempted"
    if rejection_reason == "debug_repair_budget_exhausted":
        if touched_files:
            return "debug_repair_budget_exhausted_after_patch_attempts"
        return "debug_repair_budget_exhausted_without_patch"
    if repair_rejected and touched_files:
        return "debug_repair_output_rejected_after_workspace_change"
    if repair_rejected and not repair_applied and not touched_files:
        return "debug_repair_output_rejected_no_patch_applied"
    if repair_applied and verifier_passed is False:
        return "repair_patch_applied_but_verifier_failed"
    if repair_attempted and verifier_passed is False and not touched_files:
        return "debug_repair_attempted_but_no_patch_verified"
    return "ambiguous_debug_repair_failure"


def _resolve_report_path(aggregate_path: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return aggregate_path.parent / path


def _run_label(report_path: Path, index: int) -> str:
    stem = report_path.stem
    for suffix in ("-r01", "-r02", "-r03", "-r04", "-r05"):
        if stem.endswith(suffix):
            return suffix.lstrip("-")
    return f"run-{index}"


def _row_from_report(
    *, aggregate_path: Path, aggregate: dict[str, Any], report_path: Path, index: int
) -> dict[str, Any] | None:
    report = _load_json(report_path)
    if not _should_include(report):
        return None

    event_journal_path = None
    input_payload = report.get("input")
    if isinstance(input_payload, dict):
        event_journal_value = input_payload.get("event_journal_path")
        if isinstance(event_journal_value, str) and event_journal_value:
            event_journal_path = Path(event_journal_value)
    events = _load_jsonl(event_journal_path)

    feedback_count = _event_count(events, "debug_feedback_captured") or _counter_value(
        report, "debug_feedback_captured"
    )
    attempted_count = _event_count(events, "debug_repair_attempted") or _counter_value(
        report, "debug_repair_attempted"
    )
    rejected_count = _event_count(events, "repair_rejected") or _counter_value(
        report, "repair_rejected"
    )
    applied_count = _event_count(events, "repair_applied") or _counter_value(
        report, "repair_applied"
    )

    rejected_details = _details(_last_event(events, "repair_rejected"))
    touch_scope = report.get("touch_scope")
    touched_files = []
    if isinstance(touch_scope, dict) and isinstance(touch_scope.get("touched_files"), list):
        touched_files = [str(path) for path in touch_scope["touched_files"]]
    verifier = report.get("verifier") if isinstance(report.get("verifier"), dict) else {}
    verifier_passed = verifier.get("passed") if isinstance(verifier, dict) else None
    if not isinstance(verifier_passed, bool):
        verifier_passed = None

    feedback_present = feedback_count > 0
    repair_attempted = attempted_count > 0
    repair_rejected = rejected_count > 0
    repair_applied = applied_count > 0
    rejection_reason = _rejection_reason(rejected_details)
    failure_class = _classify_failure(
        feedback_present=feedback_present,
        repair_attempted=repair_attempted,
        repair_rejected=repair_rejected,
        repair_applied=repair_applied,
        rejection_reason=rejection_reason,
        verifier_passed=verifier_passed,
        touched_files=touched_files,
    )
    path = report["path_observability"]
    return {
        "set": aggregate.get("repeat_seed") or aggregate_path.stem,
        "run": _run_label(report_path, index),
        "aggregate_report_path": str(aggregate_path),
        "run_report_path": str(report_path),
        "event_journal_path": str(event_journal_path) if event_journal_path else None,
        "failure_feedback_present": feedback_present,
        "repair_attempted": repair_attempted,
        "repair_output_shape": _repair_output_shape(rejected_details),
        "repair_rejected": repair_rejected,
        "repair_rejection_reason": rejection_reason,
        "verification_result": _verification_result(report),
        "terminal_event": _terminal_event(report),
        "failure_class": failure_class,
        "primary_failure_phase": path.get("primary_failure_phase"),
        "cross_stage_convergence_class": path.get("cross_stage_convergence_class"),
        "planning_root_cause": path.get("planning_root_cause"),
        "touched_files": touched_files,
    }


def build_summary(aggregate_paths: list[Path]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for aggregate_path in aggregate_paths:
        aggregate = _load_json(aggregate_path)
        if aggregate.get("case_id") != TARGET_CASE_ID:
            continue
        run_paths = aggregate.get("run_report_paths")
        if not isinstance(run_paths, list):
            raise AnalysisError(f"Aggregate missing run_report_paths: {aggregate_path}")
        for index, run_path_value in enumerate(run_paths, start=1):
            report_path = _resolve_report_path(aggregate_path, run_path_value)
            if report_path is None:
                continue
            row = _row_from_report(
                aggregate_path=aggregate_path,
                aggregate=aggregate,
                report_path=report_path,
                index=index,
            )
            if row is not None:
                rows.append(row)

    failure_classes = Counter(row["failure_class"] for row in rows)
    rejection_reasons = Counter(
        row["repair_rejection_reason"] or "none" for row in rows
    )
    return {
        "schema_version": 1,
        "phase": "12E",
        "mode": "read_only_attribution",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": {
            "case_id": TARGET_CASE_ID,
            "primary_failure_phase": TARGET_FAILURE_PHASE,
            "cross_stage_convergence_class": TARGET_CONVERGENCE_CLASS,
        },
        "aggregate_report_count": len(aggregate_paths),
        "row_count": len(rows),
        "failure_class_distribution": dict(failure_classes),
        "repair_rejection_reason_distribution": dict(rejection_reasons),
        "rows": rows,
    }


def _markdown_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(summary: dict[str, Any]) -> str:
    headers = [
        "Set",
        "Run",
        "Failure feedback present",
        "Repair attempted",
        "Repair output shape",
        "Repair rejected",
        "Verification result",
        "Terminal event",
        "Failure class",
    ]
    lines = [
        "# Phase 12E Debug Repair Failure Attribution",
        "",
        f"Rows: {summary['row_count']}",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in summary["rows"]:
        values = [
            row["set"],
            row["run"],
            row["failure_feedback_present"],
            row["repair_attempted"],
            row["repair_output_shape"],
            row["repair_rejected"],
            row["verification_result"],
            row["terminal_event"],
            row["failure_class"],
        ]
        lines.append("| " + " | ".join(_markdown_escape(value) for value in values) + " |")
    lines.append("")
    return "\n".join(lines)


def _print_summary(summary: dict[str, Any]) -> None:
    print("Phase 12E debug repair attribution")
    print(f"aggregate_reports={summary['aggregate_report_count']}")
    print(f"rows={summary['row_count']}")
    for failure_class, count in summary["failure_class_distribution"].items():
        print(f"failure_class[{failure_class}]={count}")
    for reason, count in summary["repair_rejection_reason_distribution"].items():
        print(f"repair_rejection_reason[{reason}]={count}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read existing Phase 12E aggregate/run reports and classify "
            "debug_import_error_repair debug-repair failures."
        )
    )
    parser.add_argument(
        "aggregate_reports",
        nargs="+",
        type=Path,
        help="Aggregate reports containing run_report_paths.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path.",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="Optional Markdown table output path.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        summary = build_summary(args.aggregate_reports)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if args.markdown_output:
            args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_output.write_text(render_markdown(summary), encoding="utf-8")
        _print_summary(summary)
        return 0
    except AnalysisError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
