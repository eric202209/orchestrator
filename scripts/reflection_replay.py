#!/usr/bin/env python3
"""Phase 17B-V: Offline reflection replay corpus tool.

Replays ReflectionRetryStrategy against a synthetic failure corpus and
reports latency, output length, quality classification, and audit completeness.

No runtime mutation. No database. Offline only.

Usage:
    PYTHONPATH=. python3 scripts/reflection_replay.py [--profile standard|medium|low_resource]
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Callable, Optional

sys.path.insert(0, ".")

from app.services.analytics.reflection_validation import (
    ReflectionQuality,
    ReflectionRecord,
    aggregate_reflections,
    audit_sequence_complete,
    classify_reflection_output,
)
from app.services.orchestration.recovery.failure_event import make_failure_event
from app.services.orchestration.recovery.strategies.reflection_retry import (
    ReflectionRetryStrategy,
)


# ── Synthetic failure corpus ───────────────────────────────────────────────────

_CORPUS = [
    {
        "failure_class": "unknown_failure",
        "source": "unknown",
        "error_message": "RuntimeError: unexpected None returned from step executor",
    },
    {
        "failure_class": "unknown_failure",
        "source": "execution",
        "error_message": "AttributeError: 'NoneType' object has no attribute 'result'",
    },
    {
        "failure_class": "debug_parse_error",
        "source": "execution",
        "error_message": "ParseError: failed to parse JSON response from model output",
    },
    {
        "failure_class": "debug_parse_error",
        "source": "execution",
        "error_message": "ValueError: parse stage received malformed token stream",
    },
    {
        "failure_class": "unknown_failure",
        "source": "unknown",
        "error_message": "KeyError: 'steps' missing from orchestration state",
    },
]


# ── Stub LLM callables ────────────────────────────────────────────────────────

def _stub_useful_llm(prompt: str) -> str:
    return (
        "Fix: Check that the step executor always returns a non-None result. "
        "Add a guard: `if result is None: raise ValueError('step executor returned None')`."
    )


def _stub_noise_llm(prompt: str) -> str:
    return "NO_RECOVERY_POSSIBLE"


def _stub_slow_llm(prompt: str) -> str:
    time.sleep(0.05)
    return "Update the configuration to handle the missing key gracefully."


_STUB_LLMS: list[tuple[str, Callable[[str], str]]] = [
    ("stub_useful", _stub_useful_llm),
    ("stub_noise", _stub_noise_llm),
    ("stub_slow", _stub_slow_llm),
]


# ── Replay runner ─────────────────────────────────────────────────────────────

def _simulate_audit_events(
    failure_class: str,
    machine_profile: str,
    result_outcome: str,
    result_success: bool,
) -> list[str]:
    """Simulate the audit event sequence that the registry would emit."""
    low_resource = machine_profile in ("low_resource", "compact_local")
    eligible = failure_class in ("unknown_failure", "debug_parse_error")

    events: list[str] = []

    if eligible and not low_resource:
        if result_outcome == "skipped":
            events.append("recovery_reflection_skipped")
        else:
            events.append("recovery_reflection_started")
            if result_success:
                events.append("recovery_reflection_completed")
            else:
                events.append("recovery_reflection_failed")

    events.append("recovery_decision_routed")
    return events


def run_replay(machine_profile: str = "standard") -> None:
    print(f"\n{'='*60}")
    print(f"Phase 17B-V Reflection Replay  |  profile={machine_profile}")
    print(f"{'='*60}\n")

    all_records: list[ReflectionRecord] = []
    audit_results: list[tuple[bool, list[str]]] = []

    for llm_name, llm_fn in _STUB_LLMS:
        print(f"--- LLM stub: {llm_name} ---")
        for item in _CORPUS:
            fe = make_failure_event(
                failure_class=item["failure_class"],
                source=item["source"],
                error_message=item["error_message"],
                session_id=1,
                task_id=1,
            )

            # Skip reflection on low_resource (mirrors registry guard)
            if machine_profile in ("low_resource", "compact_local"):
                llm_arg: Optional[Callable[[str], str]] = None
            else:
                llm_arg = llm_fn

            result = ReflectionRetryStrategy.execute(
                failure_event=fe,
                llm_callable=llm_arg,
            )

            quality = classify_reflection_output(
                result.llm_output,
                error_message=item["error_message"],
            )

            record = ReflectionRecord(
                failure_class=item["failure_class"],
                machine_profile=machine_profile,
                outcome=result.outcome,
                duration_ms=result.duration_ms,
                llm_output=result.llm_output,
                error=result.error,
                quality=quality,
            )
            all_records.append(record)

            audit_events = _simulate_audit_events(
                item["failure_class"],
                machine_profile,
                result.outcome,
                result.success,
            )
            ok, issues = audit_sequence_complete(
                audit_events,
                failure_class=item["failure_class"],
                machine_profile=machine_profile,
            )
            audit_results.append((ok, issues))

            output_chars = len(result.llm_output or "")
            status_icon = "OK" if ok else "FAIL"
            print(
                f"  [{status_icon}] {item['failure_class']:25s} "
                f"outcome={result.outcome:8s} "
                f"quality={quality.value:18s} "
                f"chars={output_chars:5d} "
                f"ms={result.duration_ms:4d}"
            )
            if not ok:
                for issue in issues:
                    print(f"       audit issue: {issue}")

        print()

    # Aggregate summary
    summary = aggregate_reflections(all_records)
    total_runs = len(all_records)
    audit_pass = sum(1 for ok, _ in audit_results if ok)

    print(f"\n{'='*60}")
    print("Aggregate Summary")
    print(f"{'='*60}")
    print(f"  Total runs:        {summary.total}")
    print(f"  Completed:         {summary.completed}")
    print(f"  Failed:            {summary.failed}")
    print(f"  Skipped:           {summary.skipped}")
    print(f"  Success rate:      {summary.success_rate:.1%}")
    print(f"  Avg latency (ms):  {summary.avg_latency_ms:.1f}")
    print(f"  Median latency:    {summary.median_latency_ms:.1f}")
    print(f"  Audit complete:    {audit_pass}/{total_runs}")
    print()
    print("  By machine profile:")
    for k, v in summary.by_machine_profile.items():
        print(f"    {k}: {v}")
    print("  By failure class:")
    for k, v in summary.by_failure_class.items():
        print(f"    {k}: {v}")
    print("  By quality:")
    for k, v in sorted(summary.by_quality.items()):
        print(f"    {k}: {v}")
    print()

    all_audit_ok = all(ok for ok, _ in audit_results)
    if all_audit_ok:
        print("AUDIT: 100% sequence completeness — PASS")
    else:
        fail_count = sum(1 for ok, _ in audit_results if not ok)
        print(f"AUDIT: {fail_count} sequence failures — REVIEW REQUIRED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 17B-V reflection replay")
    parser.add_argument(
        "--profile",
        choices=["standard", "medium", "low_resource"],
        default="standard",
        help="Machine profile to simulate (default: standard)",
    )
    args = parser.parse_args()
    run_replay(machine_profile=args.profile)
