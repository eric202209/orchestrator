#!/usr/bin/env python3
"""Read-only Phase 12I debug-repair budget attribution helper."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


TARGET_CASE = "debug_import_error_repair"
TARGET_REASON = "debug_repair_budget_exhausted"


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"Expected JSON object: {path}")
    return data


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _expand_report_paths(paths: list[Path]) -> list[Path]:
    expanded: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        data = _load_json(path)
        report_paths = data.get("run_report_paths")
        if isinstance(report_paths, list):
            for report_path in report_paths:
                child = Path(str(report_path))
                if child not in seen:
                    expanded.append(child)
                    seen.add(child)
            continue
        if path not in seen:
            expanded.append(path)
            seen.add(path)
    return expanded


def _case_id(report: dict[str, Any]) -> str | None:
    case = report.get("case")
    if isinstance(case, dict):
        value = case.get("case_id")
        return str(value) if value is not None else None
    value = report.get("case_id")
    return str(value) if value is not None else None


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("event_type") or "")


def _details(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details")
    return details if isinstance(details, dict) else {}


def _short(value: Any, limit: int = 220) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _shape(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, list):
        if not value:
            return "list[0]"
        first = value[0]
        if isinstance(first, dict):
            return f"list[{len(value)}] first_item=dict keys={','.join(sorted(first.keys()))}"
        return f"list[{len(value)}] first_item={type(first).__name__}"
    if isinstance(value, dict):
        return f"dict keys={','.join(sorted(value.keys()))}"
    return type(value).__name__


def _extract_failed_step_before_budget(events: list[dict[str, Any]], budget_index: int) -> dict[str, Any] | None:
    for event in reversed(events[:budget_index]):
        if _event_type(event) != "step_finished":
            continue
        details = _details(event)
        if details.get("status") == "failed":
            return details
    return None


def _extract_first_step_failure(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        if _event_type(event) != "step_finished":
            continue
        details = _details(event)
        if details.get("status") == "failed":
            return details
    return None


def _extract_retry_command(events: list[dict[str, Any]], budget_index: int) -> str:
    for event in reversed(events[:budget_index]):
        if _event_type(event) != "retry_entered":
            continue
        envelope = _details(event).get("failure_envelope")
        if not isinstance(envelope, dict):
            continue
        input_data = envelope.get("input")
        if not isinstance(input_data, dict):
            continue
        verification = input_data.get("verification")
        commands = input_data.get("commands")
        if verification:
            return str(verification)
        if isinstance(commands, list) and commands:
            return str(commands[0])
    return ""


def _extract_debug_output_shape(events: list[dict[str, Any]], budget_index: int) -> str:
    for event in reversed(events[:budget_index]):
        details = _details(event)
        for key in ("debug_repair_output", "repair_output", "parsed_repair", "debug_data"):
            if key in details:
                return _shape(details.get(key))
    for event in reversed(events[:budget_index]):
        if _event_type(event) == "phase_finished":
            details = _details(event)
            if details.get("phase") == "debugging" and details.get("fix_type"):
                return f"fix_type={details.get('fix_type')}"
    return "unknown"


def _patch_applied(events: list[dict[str, Any]], budget_index: int) -> str:
    if any(_event_type(event) == "repair_applied" for event in events[:budget_index]):
        return "yes"
    for event in events[:budget_index]:
        if _event_type(event) != "phase_finished":
            continue
        details = _details(event)
        if details.get("phase") == "debugging" and details.get("status") == "retrying_step":
            return "accepted_for_retry"
    return "no"


def _task_completed_present(events: list[dict[str, Any]]) -> bool:
    return any(_event_type(event) == "task_completed" for event in events)


def _budget_event_indexes(events: list[dict[str, Any]]) -> list[int]:
    indexes: list[int] = []
    for index, event in enumerate(events):
        details = _details(event)
        if TARGET_REASON in str(details.get("reason") or "") or TARGET_REASON in str(
            details.get("debug_repair_terminal_reason") or ""
        ):
            indexes.append(index)
    return indexes


def _conclusion(row: dict[str, Any]) -> str:
    if row["final_scorer_verifier_result"].startswith("passed=True") and not row[
        "task_completed_event_present"
    ]:
        if row["retry_verifier_result"].startswith("failed"):
            return "lifecycle_verifier_and_final_scorer_verifier_mismatch"
        return "verifier_passed_but_completion_event_missing"
    if row["patch_applied"] == "accepted_for_retry" and row["retry_verifier_result"].startswith("failed"):
        return "real_repair_budget_or_patch_quality_failure"
    if row["debug_repair_output_shape"] == "unknown":
        return "reporting_attribution_gap"
    return "ambiguous"


def _row_for_budget_event(report_path: Path, report: dict[str, Any], events: list[dict[str, Any]], budget_index: int) -> dict[str, Any]:
    budget_event = events[budget_index]
    budget_details = _details(budget_event)
    first_failure = _extract_first_step_failure(events) or {}
    retry_failure = _extract_failed_step_before_budget(events, budget_index) or {}
    verifier = report.get("verifier") if isinstance(report.get("verifier"), dict) else {}
    path_observability = (
        report.get("path_observability")
        if isinstance(report.get("path_observability"), dict)
        else {}
    )

    row = {
        "run_report_path": str(report_path),
        "event_journal_path": str(
            (report.get("input") or {}).get("event_journal_path") or ""
        ),
        "primary_failure_phase": path_observability.get("primary_failure_phase"),
        "cross_stage_convergence_class": path_observability.get(
            "cross_stage_convergence_class"
        ),
        "planning_root_cause": path_observability.get("planning_root_cause"),
        "step_verifier_command": _short(first_failure.get("error"), 500),
        "step_verifier_result": (
            f"failed step_index={first_failure.get('step_index')}"
            if first_failure
            else "unknown"
        ),
        "debug_repair_output_shape": _extract_debug_output_shape(events, budget_index),
        "patch_applied": _patch_applied(events, budget_index),
        "retry_command": _short(_extract_retry_command(events, budget_index), 500),
        "retry_verifier_result": (
            f"failed step_index={retry_failure.get('step_index')}"
            if retry_failure
            else "unknown"
        ),
        "retry_verifier_error": _short(retry_failure.get("error"), 500),
        "final_scorer_verifier_command": verifier.get("command"),
        "final_scorer_verifier_result": (
            f"passed={verifier.get('passed')} exit_code={verifier.get('exit_code')}"
        ),
        "task_completed_event_present": _task_completed_present(events),
        "terminal_reason": budget_details.get("debug_repair_terminal_reason")
        or budget_details.get("reason")
        or TARGET_REASON,
    }
    row["conclusion"] = _conclusion(row)
    return row


def analyze(paths: list[Path]) -> dict[str, Any]:
    report_paths = _expand_report_paths(paths)
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for report_path in report_paths:
        report = _load_json(report_path)
        if _case_id(report) != TARGET_CASE:
            skipped.append({"path": str(report_path), "reason": "non_target_case"})
            continue
        path_observability = report.get("path_observability")
        if not isinstance(path_observability, dict) or not path_observability.get(
            "debug_repair_reached"
        ):
            skipped.append({"path": str(report_path), "reason": "debug_repair_not_reached"})
            continue
        event_journal_path = Path(
            str((report.get("input") or {}).get("event_journal_path") or "")
        )
        if not event_journal_path.is_file():
            skipped.append({"path": str(report_path), "reason": "event_journal_missing"})
            continue
        events = _load_jsonl(event_journal_path)
        budget_indexes = _budget_event_indexes(events)
        if not budget_indexes:
            skipped.append({"path": str(report_path), "reason": "budget_reason_missing"})
            continue
        for budget_index in budget_indexes:
            rows.append(_row_for_budget_event(report_path, report, events, budget_index))

    conclusions: dict[str, int] = {}
    for row in rows:
        conclusion = str(row["conclusion"])
        conclusions[conclusion] = conclusions.get(conclusion, 0) + 1
    return {
        "schema_version": 1,
        "phase": "12I",
        "mode": "read_only_attribution",
        "target": {
            "case_id": TARGET_CASE,
            "debug_repair_reached": True,
            "terminal_reason_contains": TARGET_REASON,
        },
        "row_count": len(rows),
        "conclusion_distribution": conclusions,
        "rows": rows,
        "skipped": skipped,
    }


def _markdown_table(payload: dict[str, Any]) -> str:
    headers = [
        "Run",
        "Step verifier result",
        "Debug repair output shape",
        "Patch applied?",
        "Retry verifier result",
        "Final scorer verifier result",
        "Task completed?",
        "Terminal reason",
        "Conclusion",
    ]
    lines = [
        "# Phase 12I Debug Repair Budget Attribution",
        "",
        f"Rows: {payload['row_count']}",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in payload["rows"]:
        values = [
            Path(row["run_report_path"]).name,
            row["step_verifier_result"],
            row["debug_repair_output_shape"],
            row["patch_applied"],
            row["retry_verifier_result"],
            row["final_scorer_verifier_result"],
            "yes" if row["task_completed_event_present"] else "no",
            str(row["terminal_reason"]),
            str(row["conclusion"]),
        ]
        escaped = [str(value).replace("|", "\\|") for value in values]
        lines.append("| " + " | ".join(escaped) + " |")
    lines.append("")
    lines.append("## Details")
    for row in payload["rows"]:
        lines.append("")
        lines.append(f"### {Path(row['run_report_path']).name}")
        lines.append("")
        lines.append(f"- Step verifier command/error: `{_short(row['step_verifier_command'], 900)}`")
        lines.append(f"- Retry command: `{_short(row['retry_command'], 900)}`")
        lines.append(f"- Retry verifier error: `{_short(row['retry_verifier_error'], 900)}`")
        lines.append(f"- Final scorer verifier command: `{row['final_scorer_verifier_command']}`")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Phase 12I attribution for debug repair budget exhaustion."
    )
    parser.add_argument("reports", nargs="+", type=Path, help="Run or aggregate report JSON paths.")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = analyze(args.reports)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(_markdown_table(payload), encoding="utf-8")
    if not args.json_output and not args.markdown_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
