#!/usr/bin/env python3
"""Summarize Phase 11V medium benchmark reliability from scorer reports."""

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
    REPO_ROOT / "docs/roadmap/reports/evals/phase11v-medium-baseline.json"
)
STABLE_DISTRIBUTION_THRESHOLD = 0.8


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Report root must be an object: {path}")
    return payload


def _bool_path(payload: dict[str, Any], *path: str) -> bool:
    value: Any = payload
    for part in path:
        if not isinstance(value, dict):
            return False
        value = value.get(part)
    return bool(value)


def _text_path(payload: dict[str, Any], *path: str) -> str:
    value: Any = payload
    for part in path:
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    return str(value or "").strip()


def _primary_failure_phase(report: dict[str, Any]) -> str:
    return _text_path(report, "path_observability", "primary_failure_phase") or (
        "clean_success" if _bool_path(report, "result", "clean_success") else "unknown"
    )


def _first_blocker(report: dict[str, Any]) -> str:
    blockers = report.get("result", {}).get("blockers")
    if isinstance(blockers, list) and blockers:
        return str(blockers[0] or "unknown").strip() or "unknown"
    return "none" if _bool_path(report, "result", "clean_success") else "unknown"


def _verifier_tail(report: dict[str, Any]) -> str:
    verifier = report.get("verifier")
    if not isinstance(verifier, dict):
        return ""
    return "\n".join(
        str(verifier.get(key) or "")
        for key in ("stdout_tail", "stderr_tail")
        if verifier.get(key)
    )


def _compact_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return token or "unknown"


def _verifier_signature(report: dict[str, Any]) -> str:
    tail = _verifier_tail(report)
    if not tail:
        return _first_blocker(report)
    lowered = tail.lower()
    if "invalid choice" in lowered and "argparse" in lowered:
        match = re.search(r"invalid choice: ['\"]?([^'\"\s)]+)", tail, re.I)
        command = _compact_token(match.group(1)) if match else "unknown_command"
        return f"argparse_invalid_choice_{command}"
    if "modulenotfounderror" in lowered or "no module named" in lowered:
        return "module_not_found"
    if "syntaxerror" in lowered:
        return "syntax_error"
    if "assertionerror" in lowered:
        return "assertion_error"
    if "compileall" in lowered:
        return "compileall_failed"
    return _compact_token(tail[:120])


def _planning_validation_signature(reasons: list[str]) -> str:
    text = "\n".join(reasons).lower()
    if "decorators whose root name is undefined" in text:
        return "undefined_decorator_root"
    if "tests with obvious undefined names" in text:
        return "undefined_test_names"
    if "does not materialize any source changes" in text:
        return "missing_source_materialization"
    if "missing verification commands" in text:
        return "missing_verification_commands"
    return _compact_token(reasons[-1] if reasons else "verifier_failed")


def _journal_validation_reasons(journal_path: Path | None) -> list[str]:
    if journal_path is None or not journal_path.is_file():
        return []
    latest_reasons: list[str] = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event_type") != "validation_result":
            continue
        details = event.get("details")
        if not isinstance(details, dict):
            continue
        reasons = details.get("reasons")
        if isinstance(reasons, list):
            latest_reasons = [str(reason) for reason in reasons if reason]
    return latest_reasons


def _failure_signature(
    report: dict[str, Any], validation_reasons: list[str] | None = None
) -> str:
    phase = _primary_failure_phase(report)
    if phase == "clean_success":
        return "clean_success"
    if phase == "planning_validation" and validation_reasons:
        return f"{phase}:{_planning_validation_signature(validation_reasons)}"
    if _bool_path(report, "result", "verifier_passed") and _first_blocker(report) != "none":
        return f"{phase}:{_first_blocker(report)}"
    if phase in {"execution", "debug_repair", "verifier"}:
        return f"{phase}:{_verifier_signature(report)}"
    return f"{phase}:{_first_blocker(report)}"


def _repair_attempt_summary(report: dict[str, Any]) -> dict[str, Any]:
    events = report.get("events") if isinstance(report.get("events"), dict) else {}
    repair_events = (
        events.get("repair_events") if isinstance(events.get("repair_events"), dict) else {}
    )
    path = (
        report.get("path_observability")
        if isinstance(report.get("path_observability"), dict)
        else {}
    )
    attempts = int(repair_events.get("debug_repair_attempted") or 0)
    rejected = int(repair_events.get("repair_rejected") or 0)
    return {
        "debug_repair_attempted": attempts,
        "repair_rejected": rejected,
        "bounded_execution_debug_repair_used": bool(
            path.get("bounded_execution_debug_repair_used") or path.get("phase7f_used")
        ),
        "diff_scoped_debug_repair_used": bool(
            path.get("diff_scoped_debug_repair_used") or path.get("phase7g_used")
        ),
        "budget_or_rejection_terminal": rejected > 0 and attempts > 0,
    }


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _journal_paths_by_report_path(runner_aggregate_path: Path | None) -> dict[str, Path]:
    if runner_aggregate_path is None:
        return {}
    aggregate = _load_json(runner_aggregate_path)
    run_report_paths = aggregate.get("run_report_paths")
    if not isinstance(run_report_paths, list):
        run_report_paths = [
            result.get("report")
            for result in aggregate.get("results", [])
            if isinstance(result, dict)
        ]
    score_readiness = aggregate.get("score_readiness_summary")
    journal_paths = []
    if isinstance(score_readiness, dict) and isinstance(
        score_readiness.get("journal_paths"), list
    ):
        journal_paths = score_readiness["journal_paths"]
    if not journal_paths:
        journal_paths = [
            result.get("score_readiness", {}).get("event_journal_path")
            for result in aggregate.get("results", [])
            if isinstance(result, dict)
        ]
    mapping: dict[str, Path] = {}
    for report_path, journal_path in zip(run_report_paths or [], journal_paths or []):
        if report_path and journal_path:
            mapping[str(Path(report_path).resolve())] = Path(journal_path)
    return mapping


def build_report(
    report_paths: list[Path],
    *,
    source: str,
    runner_aggregate_path: Path | None = None,
) -> dict[str, Any]:
    journal_paths_by_report = _journal_paths_by_report_path(runner_aggregate_path)
    rows: list[dict[str, Any]] = []
    for path in report_paths:
        report = _load_json(path)
        repair_summary = _repair_attempt_summary(report)
        validation_reasons = _journal_validation_reasons(
            journal_paths_by_report.get(str(path.resolve()))
        )
        rows.append(
            {
                "report_path": str(path),
                "generated_at": report.get("generated_at"),
                "clean_success": _bool_path(report, "result", "clean_success"),
                "verifier_passed": _bool_path(report, "result", "verifier_passed"),
                "execution_reached": _bool_path(
                    report, "path_observability", "execution_reached"
                ),
                "debug_repair_reached": _bool_path(
                    report, "path_observability", "debug_repair_reached"
                ),
                "primary_failure_phase": _primary_failure_phase(report),
                "failure_signature": _failure_signature(report, validation_reasons),
                "first_blocker": _first_blocker(report),
                "planning_validation_reasons": validation_reasons,
                "repair": repair_summary,
            }
        )

    total = len(rows)
    phase_distribution = Counter(row["primary_failure_phase"] for row in rows)
    signature_distribution = Counter(row["failure_signature"] for row in rows)
    repair_attempt_count = sum(
        int(row["repair"]["debug_repair_attempted"]) for row in rows
    )
    repair_rejection_count = sum(int(row["repair"]["repair_rejected"]) for row in rows)
    top_phase_count = phase_distribution.most_common(1)[0][1] if total else 0
    top_signature_count = signature_distribution.most_common(1)[0][1] if total else 0
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "tool": "scripts/phase11v_medium_reliability_report.py",
        "source": source,
        "runner_aggregate_path": (
            str(runner_aggregate_path) if runner_aggregate_path else None
        ),
        "case_id": "medium_cli_multi_file_feature",
        "report_count": total,
        "clean_success_count": sum(row["clean_success"] for row in rows),
        "clean_success_rate": _rate(sum(row["clean_success"] for row in rows), total),
        "verifier_passed_count": sum(row["verifier_passed"] for row in rows),
        "verifier_passed_rate": _rate(
            sum(row["verifier_passed"] for row in rows), total
        ),
        "execution_reached_count": sum(row["execution_reached"] for row in rows),
        "execution_reached_rate": _rate(
            sum(row["execution_reached"] for row in rows), total
        ),
        "debug_repair_reached_count": sum(row["debug_repair_reached"] for row in rows),
        "debug_repair_reached_rate": _rate(
            sum(row["debug_repair_reached"] for row in rows), total
        ),
        "primary_failure_phase_distribution": dict(sorted(phase_distribution.items())),
        "stable_primary_failure_phase": _rate(top_phase_count, total)
        >= STABLE_DISTRIBUTION_THRESHOLD,
        "failure_signature_distribution": dict(sorted(signature_distribution.items())),
        "stable_failure_signature": _rate(top_signature_count, total)
        >= STABLE_DISTRIBUTION_THRESHOLD,
        "repair_convergence_proxy": {
            "debug_repair_attempt_count": repair_attempt_count,
            "repair_rejection_count": repair_rejection_count,
            "runs_with_repair_rejection": sum(
                row["repair"]["budget_or_rejection_terminal"] for row in rows
            ),
            "note": (
                "Derived from scorer summary fields. Slice 2 should replace this "
                "with per-attempt event-journal convergence."
            ),
        },
        "runs": rows,
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
        description="Build Phase 11V medium benchmark reliability summary."
    )
    parser.add_argument("--reports-glob", default=DEFAULT_REPORT_GLOB)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--runner-aggregate",
        type=Path,
        help="Optional runner aggregate with per-run event journal paths.",
    )
    args = parser.parse_args()

    report_paths = _resolve_reports(args.reports_glob, args.limit)
    if not report_paths:
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
