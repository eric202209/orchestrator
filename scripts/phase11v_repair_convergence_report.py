#!/usr/bin/env python3
"""Summarize Phase 11V repair convergence from existing eval artifacts."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import glob
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_GLOB = (
    "docs/roadmap/reports/evals/"
    "orchestrator-eval-v1-medium-cli-multi-file-feature-queue-*.json"
)
DEFAULT_OUTPUT = (
    REPO_ROOT / "docs/roadmap/reports/evals/phase11v-repair-convergence.json"
)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _load_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _compact_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "unknown"


def _validation_signature(reasons: list[str]) -> str:
    text = "\n".join(reasons).lower()
    if not text:
        return "none"
    if "does not materialize any source changes" in text:
        return "missing_source_materialization"
    if "missing source materialization" in text:
        return "missing_source_materialization"
    if "python_source_syntax_invalid" in text or "invalid syntax" in text:
        return "python_source_syntax_invalid"
    if "decorators whose root name is undefined" in text:
        return "undefined_decorator_root"
    if "framework_mismatch" in text or "framework mismatch" in text:
        return "framework_mismatch"
    if "missing verification commands" in text:
        return "missing_verification_commands"
    if "steps without runnable commands" in text:
        return "missing_runnable_commands"
    if "invalid json" in text or "json" in text and "parse" in text:
        return "invalid_json"
    if "brittle heredoc" in text or "malformed commands" in text:
        return "brittle_commands"
    return _compact_token(reasons[0])[:120]


def _issue_flags(reasons: list[str]) -> dict[str, bool]:
    text = "\n".join(reasons).lower()
    return {
        "missing_source_materialization": (
            "does not materialize any source changes" in text
            or "missing source materialization" in text
        ),
        "command_contract": (
            "steps without runnable commands" in text
            or "missing verification commands" in text
            or "brittle heredoc" in text
            or "malformed commands" in text
        ),
        "python_syntax": (
            "python_source_syntax_invalid" in text or "invalid syntax" in text
        ),
        "framework_mismatch": (
            "decorators whose root name is undefined" in text
            or "framework_mismatch" in text
            or "framework mismatch" in text
        ),
        "invalid_output": "invalid json" in text
        or ("json" in text and "parse" in text)
        or "planning_json_error" in text,
    }


def _trend(before: bool, after: bool) -> str:
    if before and not after:
        return "improved"
    if not before and after:
        return "regressed"
    return "preserved" if before else "unchanged"


def _source_materialization_state(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return "unknown"
    for step in snapshot.get("plan_steps") or []:
        if not isinstance(step, dict):
            continue
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            path = str(operation.get("path") or "").lstrip("./")
            if (
                str(operation.get("op") or "")
                in {"write_file", "replace_in_file", "append_file"}
                and path.startswith("src/")
            ):
                return "present"
    return "absent"


def _source_materialization_delta(
    before_snapshot: dict[str, Any] | None,
    after_snapshot: dict[str, Any] | None,
) -> str:
    before = _source_materialization_state(before_snapshot)
    after = _source_materialization_state(after_snapshot)
    if before == "absent" and after == "present":
        return "added"
    if before == "present" and after == "absent":
        return "removed"
    if before == "present" and after == "present":
        return "preserved"
    if before == "absent" and after == "absent":
        return "unchanged_absent"
    return "unknown"


def _validation_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "validation_result":
            continue
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        reasons = details.get("reasons")
        if not isinstance(reasons, list):
            reasons = []
        rows.append(
            {
                "event_id": event.get("event_id"),
                "timestamp": event.get("timestamp"),
                "status": details.get("status"),
                "stage": details.get("stage"),
                "reasons": [str(reason) for reason in reasons if reason],
                "signature": _validation_signature(
                    [str(reason) for reason in reasons if reason]
                ),
            }
        )
    return rows


def _snapshots_by_event_id(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        event_id = str(snapshot.get("related_event_id") or "")
        if event_id:
            mapping[event_id] = snapshot
    return mapping


def _classify_lane(trigger_reason: str, before_signature: str) -> str:
    text = f"{trigger_reason}\n{before_signature}".lower()
    if "post_repair_framework" in text or "framework" in text:
        return "framework_retry"
    if "post_repair_python_source_syntax" in text or "python_source_syntax" in text:
        return "syntax_retry"
    if "diff_scoped" in text:
        return "diff_scoped_debug_repair"
    if "debug_repair" in text:
        return "debug_repair"
    return "planning_repair"


def _classification(
    *,
    after_event: dict[str, Any] | None,
    source_delta: str,
    command_trend: str,
    syntax_trend: str,
    framework_trend: str,
) -> str:
    if after_event is None:
        return "exhausted"
    after_flags = _issue_flags(after_event.get("reasons") or [])
    if after_flags["invalid_output"]:
        return "invalid_output"
    if source_delta == "removed":
        return "regressed"
    if any(trend == "improved" for trend in (command_trend, syntax_trend, framework_trend)):
        return "improved"
    if source_delta == "added":
        return "improved"
    if any(trend == "regressed" for trend in (command_trend, syntax_trend, framework_trend)):
        return "regressed"
    return "equivalent"


def _attempts_for_run(
    *,
    report_path: Path,
    report: dict[str, Any],
    journal_path: Path | None,
    snapshot_path: Path | None,
) -> list[dict[str, Any]]:
    validations = _validation_events(_load_jsonl(journal_path))
    snapshots = _snapshots_by_event_id(_load_jsonl(snapshot_path))
    attempts: list[dict[str, Any]] = []
    for index, before in enumerate(validations):
        after = validations[index + 1] if index + 1 < len(validations) else None
        before_snapshot = snapshots.get(str(before.get("event_id") or ""))
        after_snapshot = (
            snapshots.get(str(after.get("event_id") or "")) if after else None
        )
        before_flags = _issue_flags(before.get("reasons") or [])
        after_flags = _issue_flags(after.get("reasons") or []) if after else {}
        source_delta = _source_materialization_delta(before_snapshot, after_snapshot)
        command_trend = _trend(
            before_flags["command_contract"], bool(after_flags.get("command_contract"))
        )
        syntax_trend = _trend(
            before_flags["python_syntax"], bool(after_flags.get("python_syntax"))
        )
        framework_trend = _trend(
            before_flags["framework_mismatch"],
            bool(after_flags.get("framework_mismatch")),
        )
        trigger_reason = "; ".join(before.get("reasons") or [])
        after_signature = str(after.get("signature") if after else "terminal")
        repair_lane = _classify_lane(trigger_reason, str(before.get("signature") or ""))
        if index > 0 and str(before.get("signature") or "") in {
            "undefined_decorator_root",
            "framework_mismatch",
        }:
            repair_lane = "framework_retry"
        attempts.append(
            {
                "repair_attempt_index": index + 1,
                "repair_trigger_reason": trigger_reason,
                "repair_lane": repair_lane,
                "before_signature": before.get("signature"),
                "after_signature": after_signature,
                "source_materialization": source_delta,
                "command_verification_contract": command_trend,
                "python_syntax_validity": syntax_trend,
                "framework_mismatch": framework_trend,
                "final_classification": _classification(
                    after_event=after,
                    source_delta=source_delta,
                    command_trend=command_trend,
                    syntax_trend=syntax_trend,
                    framework_trend=framework_trend,
                ),
            }
        )
    if not attempts:
        return []
    attempts[-1]["run_terminal_failure_phase"] = (
        report.get("path_observability", {}).get("primary_failure_phase")
        if isinstance(report.get("path_observability"), dict)
        else None
    )
    attempts[-1]["run_report_path"] = str(report_path)
    return attempts


def _aggregate_metadata(runner_aggregate_path: Path | None) -> dict[str, Any]:
    if runner_aggregate_path is None or not runner_aggregate_path.is_file():
        return {}
    return _load_json(runner_aggregate_path)


def _paths_from_aggregate(
    runner_aggregate_path: Path | None,
) -> tuple[list[Path], dict[str, Path], dict[str, Path]]:
    aggregate = _aggregate_metadata(runner_aggregate_path)
    report_paths = [
        Path(path)
        for path in aggregate.get("run_report_paths", [])
        if isinstance(path, str)
    ]
    if not report_paths:
        report_paths = [
            Path(result.get("report"))
            for result in aggregate.get("results", [])
            if isinstance(result, dict) and result.get("report")
        ]
    journal_paths = [
        Path(path)
        for path in (
            aggregate.get("score_readiness_summary", {}).get("journal_paths", [])
            if isinstance(aggregate.get("score_readiness_summary"), dict)
            else []
        )
        if isinstance(path, str)
    ]
    if not journal_paths:
        journal_paths = [
            Path(result.get("score_readiness", {}).get("event_journal_path"))
            for result in aggregate.get("results", [])
            if isinstance(result, dict)
            and isinstance(result.get("score_readiness"), dict)
            and result.get("score_readiness", {}).get("event_journal_path")
        ]
    journal_by_report = {
        str(report.resolve()): journal
        for report, journal in zip(report_paths, journal_paths)
    }
    snapshot_by_report: dict[str, Path] = {}
    for report in report_paths:
        if report.is_file():
            payload = _load_json(report)
            snapshot = payload.get("input", {}).get("state_snapshot_path")
            if snapshot:
                snapshot_by_report[str(report.resolve())] = Path(snapshot)
    return report_paths, journal_by_report, snapshot_by_report


def build_report(
    report_paths: list[Path],
    *,
    source: str,
    runner_aggregate_path: Path | None = None,
) -> dict[str, Any]:
    aggregate_reports, journal_by_report, snapshot_by_report = _paths_from_aggregate(
        runner_aggregate_path
    )
    if aggregate_reports:
        report_paths = aggregate_reports

    runs: list[dict[str, Any]] = []
    all_attempts: list[dict[str, Any]] = []
    for report_path in report_paths:
        report = _load_json(report_path)
        report_key = str(report_path.resolve())
        snapshot_path = snapshot_by_report.get(report_key)
        if snapshot_path is None:
            snapshot_value = report.get("input", {}).get("state_snapshot_path")
            snapshot_path = Path(snapshot_value) if snapshot_value else None
        attempts = _attempts_for_run(
            report_path=report_path,
            report=report,
            journal_path=journal_by_report.get(report_key),
            snapshot_path=snapshot_path,
        )
        all_attempts.extend(attempts)
        runs.append(
            {
                "report_path": str(report_path),
                "journal_path": str(journal_by_report.get(report_key) or ""),
                "state_snapshot_path": str(snapshot_path or ""),
                "repair_attempt_count": len(attempts),
                "attempts": attempts,
            }
        )

    classification_counts = Counter(
        attempt["final_classification"] for attempt in all_attempts
    )
    repeated_failures = Counter(
        f"{attempt['repair_lane']}:{attempt['after_signature']}:{attempt['final_classification']}"
        for attempt in all_attempts
        if attempt["final_classification"] != "improved"
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "tool": "scripts/phase11v_repair_convergence_report.py",
        "source": source,
        "runner_aggregate_path": (
            str(runner_aggregate_path) if runner_aggregate_path else None
        ),
        "report_count": len(runs),
        "repair_attempt_count": len(all_attempts),
        "classification_distribution": dict(sorted(classification_counts.items())),
        "top_repeated_convergence_failures": [
            {"signature": signature, "count": count}
            for signature, count in repeated_failures.most_common(3)
        ],
        "runs": runs,
    }


def _resolve_reports(pattern: str, limit: int | None) -> list[Path]:
    search_pattern = str(Path(pattern) if Path(pattern).is_absolute() else REPO_ROOT / pattern)
    paths = sorted(Path(path) for path in glob.glob(search_pattern))
    paths = [
        path
        for path in paths
        if path.is_file() and path.stat().st_size > 0 and "aggregate" not in path.name
    ]
    if limit is not None:
        paths = paths[-limit:]
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build Phase 11V repair convergence summary."
    )
    parser.add_argument("--reports-glob", default=DEFAULT_REPORT_GLOB)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--runner-aggregate", type=Path)
    args = parser.parse_args()

    report_paths = _resolve_reports(args.reports_glob, args.limit)
    if args.runner_aggregate:
        report_paths = []
    if not report_paths and not args.runner_aggregate:
        raise SystemExit(f"No reports matched {args.reports_glob!r}")

    payload = build_report(
        report_paths,
        source=f"reports_glob={args.reports_glob}; limit={args.limit}",
        runner_aggregate_path=args.runner_aggregate,
    )
    output = args.output if args.output.is_absolute() else REPO_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
