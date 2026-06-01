#!/usr/bin/env python3
"""Simulate Phase 12C deterministic eval gates from aggregate reports."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


DEFAULT_POLICY = Path(__file__).with_name("phase12c-gate-policy.json")


@dataclass(frozen=True)
class CheckError(Exception):
    message: str


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise CheckError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CheckError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CheckError(f"Expected JSON object in {path}")
    return payload


def _rate(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _count(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _compare(actual: float | None, *, op: str, expected: float) -> bool:
    if actual is None:
        return False
    if op == ">=":
        return actual >= expected
    if op == "==":
        return actual == expected
    raise CheckError(f"Unsupported threshold operator: {op}")


def _threshold_results(
    aggregate: dict[str, Any], thresholds: dict[str, Any]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for field, rule in thresholds.items():
        if not isinstance(rule, dict):
            raise CheckError(f"Invalid threshold for {field}")
        op = rule.get("op")
        expected = rule.get("value")
        if not isinstance(op, str) or not isinstance(expected, int | float):
            raise CheckError(f"Invalid threshold rule for {field}")
        actual = _rate(aggregate, field)
        passed = _compare(actual, op=op, expected=float(expected))
        results.append(
            {
                "field": field,
                "actual": actual,
                "op": op,
                "expected": float(expected),
                "passed": passed,
            }
        )
    return results


def _negative_evidence(
    aggregate: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    config = policy.get("negative_evidence")
    if not isinstance(config, dict):
        return {
            "stable_failure_reason": False,
            "verifier_backed_guard_evidence": False,
            "passed": True,
        }

    stable_failure_reason = bool(
        config.get("allow_stable_primary_failure_phase")
        and aggregate.get("stable_primary_failure_phase") is True
    )
    allowed_blockers = config.get("verifier_backed_blockers", [])
    if not isinstance(allowed_blockers, list):
        allowed_blockers = []
    most_common_blocker = aggregate.get("most_common_blocker")
    verifier_backed = isinstance(most_common_blocker, str) and most_common_blocker in {
        blocker for blocker in allowed_blockers if isinstance(blocker, str)
    }
    return {
        "stable_failure_reason": stable_failure_reason,
        "verifier_backed_guard_evidence": verifier_backed,
        "passed": stable_failure_reason or verifier_backed,
    }


def _evaluate_case(
    *, aggregate_path: Path, aggregate: dict[str, Any], case_policy: dict[str, Any]
) -> dict[str, Any]:
    case_id = aggregate.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise CheckError(f"Aggregate report missing case_id: {aggregate_path}")
    role = case_policy.get("role")
    if not isinstance(role, str) or not role:
        raise CheckError(f"Policy role missing for case {case_id}")

    thresholds = case_policy.get("thresholds", {})
    if thresholds is None:
        thresholds = {}
    if not isinstance(thresholds, dict):
        raise CheckError(f"Policy thresholds must be an object for case {case_id}")
    threshold_results = _threshold_results(aggregate, thresholds)
    failed_thresholds = [
        result for result in threshold_results if result["passed"] is False
    ]
    evidence = _negative_evidence(aggregate, case_policy)
    if role == "simulated_negative_gate":
        would_pass = not failed_thresholds and evidence["passed"] is True
    elif role == "diagnostic_only":
        would_pass = None
    else:
        would_pass = not failed_thresholds
    would_fail = False if would_pass is None else not would_pass

    return {
        "case_id": case_id,
        "role": role,
        "aggregate_report_path": str(aggregate_path),
        "repeat_count": _count(aggregate, "repeat_count"),
        "clean_success_count": _count(aggregate, "clean_success_count"),
        "clean_success_rate": _rate(aggregate, "clean_success_rate"),
        "intended_path_observed_count": _count(
            aggregate, "intended_path_observed_count"
        ),
        "intended_path_observed_rate": _rate(
            aggregate, "intended_path_observed_rate"
        ),
        "would_pass": would_pass,
        "would_fail": would_fail,
        "blocking": False,
        "failed_thresholds": failed_thresholds,
        "thresholds": threshold_results,
        "evidence": evidence,
        "diagnostic_only": role == "diagnostic_only",
    }


def build_summary(
    *, policy_path: Path, aggregate_paths: list[Path], min_evidence_sets: int = 3
) -> dict[str, Any]:
    policy = _load_json(policy_path)
    if policy.get("mode") != "simulation":
        raise CheckError("Phase 12C-A checker only supports simulation mode")
    cases_policy = policy.get("cases")
    if not isinstance(cases_policy, dict):
        raise CheckError("Policy missing cases object")

    cases: list[dict[str, Any]] = []
    for aggregate_path in aggregate_paths:
        aggregate = _load_json(aggregate_path)
        case_id = aggregate.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise CheckError(f"Aggregate report missing case_id: {aggregate_path}")
        case_policy = cases_policy.get(case_id)
        if not isinstance(case_policy, dict):
            raise CheckError(f"No Phase 12C policy configured for case {case_id}")
        cases.append(
            _evaluate_case(
                aggregate_path=aggregate_path,
                aggregate=aggregate,
                case_policy=case_policy,
            )
        )

    grouped: dict[str, list[dict[str, Any]]] = {
        "simulated_hard_gate": [],
        "simulated_negative_gate": [],
        "diagnostic_only": [],
    }
    for case in cases:
        grouped.setdefault(case["role"], []).append(case)

    simulated_failures = [
        case
        for case in cases
        if case["would_fail"] is True and case["role"] != "diagnostic_only"
    ]
    stability = _build_stability_summary(
        cases=cases,
        min_evidence_sets=min_evidence_sets,
    )
    return {
        "schema_version": 1,
        "mode": "simulation",
        "policy_path": str(policy_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "simulated_failure_count": len(simulated_failures),
        "blocking_failure_count": 0,
        "stability": stability,
        "groups": grouped,
        "cases": cases,
    }


def _build_stability_summary(
    *, cases: list[dict[str, Any]], min_evidence_sets: int
) -> dict[str, Any]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_case.setdefault(case["case_id"], []).append(case)

    stability_cases: list[dict[str, Any]] = []
    for case_id in sorted(by_case):
        entries = by_case[case_id]
        role = entries[0]["role"]
        evidence_set_count = len(entries)
        simulated_pass_count = sum(1 for entry in entries if entry["would_pass"] is True)
        simulated_fail_count = sum(1 for entry in entries if entry["would_fail"] is True)
        diagnostic_only = role == "diagnostic_only"
        flaky = (
            not diagnostic_only
            and simulated_pass_count > 0
            and simulated_fail_count > 0
        )
        optional_warning_candidate = (
            not diagnostic_only
            and evidence_set_count >= min_evidence_sets
            and simulated_pass_count == evidence_set_count
            and simulated_fail_count == 0
        )
        stability_cases.append(
            {
                "case_id": case_id,
                "role": role,
                "evidence_set_count": evidence_set_count,
                "simulated_pass_count": simulated_pass_count,
                "simulated_fail_count": simulated_fail_count,
                "insufficient_evidence": evidence_set_count < min_evidence_sets,
                "flaky": flaky,
                "optional_warning_candidate": optional_warning_candidate,
                "promotion_ready": False,
                "blocking": False,
            }
        )

    return {
        "mode": "warning_only",
        "min_evidence_sets": min_evidence_sets,
        "case_count": len(stability_cases),
        "optional_warning_candidate_count": sum(
            1 for case in stability_cases if case["optional_warning_candidate"] is True
        ),
        "flaky_case_count": sum(1 for case in stability_cases if case["flaky"] is True),
        "promotion_ready_count": 0,
        "blocking_failure_count": 0,
        "cases": stability_cases,
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print("Phase 12C gate simulation")
    print(f"cases={summary['case_count']}")
    print(f"simulated_failures={summary['simulated_failure_count']}")
    print(f"blocking_failures={summary['blocking_failure_count']}")
    stability = summary["stability"]
    print(f"stability_mode={stability['mode']}")
    print(f"min_evidence_sets={stability['min_evidence_sets']}")
    print(
        "optional_warning_candidates="
        f"{stability['optional_warning_candidate_count']}"
    )
    print(f"flaky_cases={stability['flaky_case_count']}")
    for role in ("simulated_hard_gate", "simulated_negative_gate", "diagnostic_only"):
        print(f"\n{role}:")
        cases = summary["groups"].get(role, [])
        if not cases:
            print("  none")
            continue
        for case in cases:
            would_pass = case["would_pass"]
            would_fail = case["would_fail"]
            print(
                "  "
                f"{case['case_id']}: "
                f"clean_success_rate={case['clean_success_rate']} "
                f"intended_path_observed_rate={case['intended_path_observed_rate']} "
                f"would_pass={would_pass} would_fail={would_fail} "
                "blocking=false"
            )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulate Phase 12C deterministic eval gates."
    )
    parser.add_argument(
        "aggregate_reports",
        nargs="+",
        type=Path,
        help="Aggregate report JSON files from run_orchestrator_eval_slice.py.",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY,
        help=f"Gate simulation policy JSON. Defaults to {DEFAULT_POLICY}.",
    )
    parser.add_argument(
        "--output",
        "--summary-output",
        dest="output",
        type=Path,
        help="Optional path for the summary JSON artifact.",
    )
    parser.add_argument(
        "--min-evidence-sets",
        type=int,
        default=3,
        help=(
            "Minimum aggregate reports per case before a repeated pass can be "
            "reported as an optional warning candidate. Defaults to 3."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.min_evidence_sets < 1:
            raise CheckError("--min-evidence-sets must be at least 1")
        summary = build_summary(
            policy_path=args.policy,
            aggregate_paths=args.aggregate_reports,
            min_evidence_sets=args.min_evidence_sets,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        _print_summary(summary)
        return 0
    except CheckError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
