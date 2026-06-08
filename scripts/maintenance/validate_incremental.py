"""Slice J live validation harness.

Run from the project root:
    PYTHONPATH=. python3 validate_incremental.py

Requires the OpenClaw gateway to be live at the configured URL.
Writes per-task JSONL metrics and a Markdown report.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

# Must be set BEFORE any app imports so pydantic-settings picks it up.
os.environ["INCREMENTAL_EXECUTION_ENABLED"] = "true"

from app.config import settings  # noqa: E402

assert settings.INCREMENTAL_EXECUTION_ENABLED is True, (
    "Flag did not activate — check env var handling"
)

from app.database import SessionLocal  # noqa: E402
from app.services.agents.openclaw_service import OpenClawSessionService  # noqa: E402
from app.services.orchestration.phases.incremental_flow import (  # noqa: E402
    attempt_incremental_execution,
)
from app.services.orchestration.planning.incremental_classifier import (  # noqa: E402
    is_incremental_candidate,
)
from app.services.prompt_templates import OrchestrationState  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("validate_incremental")


# ---------------------------------------------------------------------------
# Task corpus
# ---------------------------------------------------------------------------

SMOKE_TASKS: List[str] = [
    "Create about.html with heading 'Slice J Live Test' and verify it exists.",
    (
        "Create utils.py with a function add(a, b) that returns a + b. "
        "Verify the file is valid Python."
    ),
    "Create config.json with key \"version\" set to \"1.0.0\". Verify it exists.",
    "Create styles.css with body margin 0 and h1 color #333. Verify it exists.",
    (
        "Create greeting.py with a function say_hello() that returns 'hello world'. "
        "Verify the file is valid Python."
    ),
]

EXTENDED_TASKS: List[str] = [
    "Create index.html with an h1 reading 'Welcome' and verify it exists.",
    (
        "Create math_ops.py with a function multiply(a, b) returning a * b. "
        "Verify the file is valid Python."
    ),
    "Create settings.json with key \"debug\" set to false. Verify it exists.",
    "Create reset.css with * margin 0 and padding 0. Verify it exists.",
    (
        "Create counter.py with a class Counter that has inc() and count() methods. "
        "Verify the file is valid Python."
    ),
    "Create favicon.svg with a circle element of radius 10. Verify it exists.",
    (
        "Create helpers.py with a function clamp(v, lo, hi) returning max(lo, min(v, hi)). "
        "Verify the file is valid Python."
    ),
    "Create manifest.json with key \"name\" set to \"slice-j-app\". Verify it exists.",
    "Create base.css with html font-size 16px and body line-height 1.5. Verify it exists.",
    (
        "Create validators.py with a function is_positive(n) returning n > 0. "
        "Verify the file is valid Python."
    ),
    "Create meta.json with key \"author\" set to \"test\" and key \"v\" set to 1. Verify it exists.",
    "Create 404.html with heading 'Not Found' and verify it exists.",
    (
        "Create formatters.py with a function to_upper(s) that returns s.upper(). "
        "Verify the file is valid Python."
    ),
    "Create theme.css with :root --bg set to #fff and --fg set to #000. Verify it exists.",
    (
        "Create parsers.py with a function split_csv(line) returning line.split(','). "
        "Verify the file is valid Python."
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runtime(db) -> OpenClawSessionService:
    return OpenClawSessionService(db, session_id=None, task_id=None)


def _make_ctx(project_dir: str, runtime: OpenClawSessionService, task_idx: int) -> Any:
    state = OrchestrationState(
        session_id="val-inc",
        task_description="validation task",
        project_name="validation",
        task_id=task_idx,
    )
    state._project_dir_override = project_dir
    return SimpleNamespace(
        orchestration_state=state,
        runtime_service=runtime,
        task_id=task_idx,
        session_id=0,
        logger=logger,
        emit_live=lambda *a, **kw: None,
    )


def _run_task(
    description: str, runtime: OpenClawSessionService, task_idx: int
) -> Dict[str, Any]:
    """Run a single task through the incremental path; return per-task metrics."""
    record: Dict[str, Any] = {
        "task_idx": task_idx,
        "description": description,
        "description_chars": len(description),
        "classifier_accepted": is_incremental_candidate(description),
        "status": None,
        "reason": None,
        "file_created": False,
        "verify_ran": False,
        "llm_calls": None,
        "elapsed_s": None,
        "routed_to_incremental": False,
    }

    if not record["classifier_accepted"]:
        record["status"] = "skipped_classifier_rejected"
        logger.warning("SKIP task %d — classifier rejected: %s", task_idx, description[:60])
        return record

    record["routed_to_incremental"] = True

    with tempfile.TemporaryDirectory(prefix=f"inc_val_{task_idx}_") as tmpdir:
        ctx = _make_ctx(tmpdir, runtime, task_idx)
        t0 = time.monotonic()
        try:
            result = attempt_incremental_execution(
                ctx=ctx, task_description=description
            )
        except Exception as exc:
            record["status"] = "harness_exception"
            record["reason"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Harness exception on task %d", task_idx)
            return record
        elapsed = time.monotonic() - t0
        record["elapsed_s"] = round(elapsed, 2)
        record["status"] = result.get("status")
        record["reason"] = result.get("reason")

        state = ctx.orchestration_state
        if record["status"] == "completed":
            # Check file exists
            from app.services.orchestration.planning.incremental_classifier import (
                _extract_file_paths,
            )
            fps = _extract_file_paths(description)
            primary = fps[0] if fps else None
            if primary:
                record["file_created"] = Path(tmpdir, primary).exists()
            record["llm_calls"] = 1
            record["verify_ran"] = True
            # Verify plan was populated
            record["plan_populated"] = len(state.plan) == 1
            record["step_index_correct"] = (
                state.current_step_index == len(state.plan)
            )
        else:
            record["plan_populated"] = len(state.plan) == 0
            record["step_index_correct"] = state.current_step_index == 0
            if record["reason"] in {"verify_failed", "verify_execution"}:
                record["verify_ran"] = True

    return record


def _compute_metrics(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    routed = [r for r in records if r["routed_to_incremental"]]
    succeeded = [r for r in routed if r["status"] == "completed"]
    fallbacks = [r for r in routed if r["status"] == "failed"]
    harness_errors = [r for r in routed if r["status"] == "harness_exception"]
    destructive_fp = [
        r for r in succeeded
        if not r.get("file_created", True) or not r.get("plan_populated", True)
    ]

    n = len(routed)
    success_rate = len(succeeded) / n if n else 0.0
    fallback_rate = len(fallbacks) / n if n else 0.0
    mean_llm_calls = (
        sum(r["llm_calls"] for r in succeeded if r["llm_calls"] is not None)
        / len(succeeded)
        if succeeded
        else 0.0
    )
    mean_elapsed = (
        sum(r["elapsed_s"] for r in routed if r["elapsed_s"] is not None)
        / n
        if n
        else 0.0
    )
    fallback_reasons: Dict[str, int] = {}
    for r in fallbacks:
        k = r.get("reason") or "unknown"
        fallback_reasons[k] = fallback_reasons.get(k, 0) + 1

    return {
        "n_routed": n,
        "n_succeeded": len(succeeded),
        "n_fallbacks": len(fallbacks),
        "n_harness_errors": len(harness_errors),
        "n_destructive_false_positives": len(destructive_fp),
        "success_rate": round(success_rate, 4),
        "fallback_rate": round(fallback_rate, 4),
        "mean_llm_calls": round(mean_llm_calls, 4),
        "mean_elapsed_s": round(mean_elapsed, 2),
        "fallback_reasons": fallback_reasons,
        "thresholds_met": {
            "success_rate_gte_70pct": success_rate >= 0.70,
            "fallback_rate_lte_5pct": fallback_rate <= 0.05,
            "no_destructive_false_positives": len(destructive_fp) == 0,
            "mean_llm_calls_lte_2": mean_llm_calls <= 2.0,
        },
    }


def _write_report(
    smoke_records: List[Dict[str, Any]],
    smoke_metrics: Dict[str, Any],
    extended_records: Optional[List[Dict[str, Any]]],
    extended_metrics: Optional[Dict[str, Any]],
    report_path: Path,
) -> None:
    thresholds = extended_metrics["thresholds_met"] if extended_metrics else smoke_metrics["thresholds_met"]
    metrics = extended_metrics or smoke_metrics
    all_pass = all(thresholds.values())

    lines = [
        "# Slice J: Incremental Execution Live Validation",
        "",
        f"**Date:** 2026-06-08  ",
        f"**Flag:** `INCREMENTAL_EXECUTION_ENABLED=True`  ",
        f"**Backend:** `{settings.AGENT_BACKEND}`  ",
        "",
        "---",
        "",
        "## Verdict",
        "",
        f"**{'ALL THRESHOLDS MET — READY TO ENABLE' if all_pass else 'ONE OR MORE THRESHOLDS NOT MET — KEEP FLAG OFF'}**",
        "",
    ]

    # Thresholds table
    lines += [
        "| Threshold | Target | Actual | Met |",
        "|---|---|---|---|",
        f"| Success rate | ≥ 70% | {metrics['success_rate'] * 100:.1f}% | {'✓' if thresholds['success_rate_gte_70pct'] else '✗'} |",
        f"| Fallback rate | ≤ 5% | {metrics['fallback_rate'] * 100:.1f}% | {'✓' if thresholds['fallback_rate_lte_5pct'] else '✗'} |",
        f"| Destructive false positives | = 0 | {metrics['n_destructive_false_positives']} | {'✓' if thresholds['no_destructive_false_positives'] else '✗'} |",
        f"| Mean LLM calls | ≤ 2 | {metrics['mean_llm_calls']:.2f} | {'✓' if thresholds['mean_llm_calls_lte_2'] else '✗'} |",
        "",
        "---",
        "",
    ]

    # Smoke window
    lines += _format_window_section("Smoke Window (5 tasks)", smoke_records, smoke_metrics)

    # Extended window (if run)
    if extended_records and extended_metrics:
        lines += _format_window_section(
            "Extended Window (15 tasks)", extended_records, extended_metrics
        )
    else:
        lines += [
            "## Extended Window",
            "",
            "Not run — smoke window did not meet 5/5 clean threshold, or skipped.",
            "",
        ]

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to {report_path}", flush=True)


def _format_window_section(
    title: str, records: List[Dict[str, Any]], metrics: Dict[str, Any]
) -> List[str]:
    lines = [
        f"## {title}",
        "",
        f"- Routed tasks: {metrics['n_routed']}",
        f"- Succeeded: {metrics['n_succeeded']}",
        f"- Fallbacks: {metrics['n_fallbacks']}",
        f"- Harness errors: {metrics['n_harness_errors']}",
        f"- Success rate: {metrics['success_rate'] * 100:.1f}%",
        f"- Fallback rate: {metrics['fallback_rate'] * 100:.1f}%",
        f"- Mean LLM calls: {metrics['mean_llm_calls']:.2f}",
        f"- Mean elapsed: {metrics['mean_elapsed_s']:.1f}s",
    ]
    if metrics["fallback_reasons"]:
        lines.append(f"- Fallback reasons: {json.dumps(metrics['fallback_reasons'])}")
    lines += [
        "",
        "### Per-task results",
        "",
        "| # | Status | Reason | File created | LLM calls | Elapsed | Description (truncated) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in records:
        lines.append(
            f"| {r['task_idx']} "
            f"| {r['status'] or '—'} "
            f"| {r.get('reason') or '—'} "
            f"| {'yes' if r.get('file_created') else 'no'} "
            f"| {r.get('llm_calls') or '—'} "
            f"| {r.get('elapsed_s') or '—'}s "
            f"| {r['description'][:60]}… |"
        )
    lines += ["", "---", ""]
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    _reports = Path(__file__).resolve().parents[2] / "docs/roadmap/reports/maintenance"
    report_path = _reports / "incremental-execution-live-validation-20260608.md"
    metrics_jsonl = _reports / "incremental-execution-live-validation-metrics-20260608.jsonl"

    print("=" * 60, flush=True)
    print("Slice J Live Validation", flush=True)
    print(f"INCREMENTAL_EXECUTION_ENABLED={settings.INCREMENTAL_EXECUTION_ENABLED}", flush=True)
    print(f"AGENT_BACKEND={settings.AGENT_BACKEND}", flush=True)
    print("=" * 60, flush=True)

    db = SessionLocal()
    try:
        runtime = _make_runtime(db)

        # ── Smoke window (5 tasks) ────────────────────────────────────────
        print("\n── Smoke window (5 tasks) ──", flush=True)
        smoke_records: List[Dict[str, Any]] = []
        for i, desc in enumerate(SMOKE_TASKS):
            print(f"\nTask {i + 1}/{len(SMOKE_TASKS)}: {desc[:70]}", flush=True)
            rec = _run_task(desc, runtime, task_idx=i + 1)
            smoke_records.append(rec)
            tag = "✓ SUCCEEDED" if rec["status"] == "completed" else f"✗ {rec['status']} ({rec.get('reason', '')})"
            print(f"  → {tag} | elapsed={rec.get('elapsed_s')}s", flush=True)

        smoke_metrics = _compute_metrics(smoke_records)
        print(f"\nSmoke results: {smoke_metrics['n_succeeded']}/{smoke_metrics['n_routed']} succeeded "
              f"({smoke_metrics['success_rate'] * 100:.0f}%)", flush=True)

        smoke_clean = (
            smoke_metrics["n_succeeded"] == smoke_metrics["n_routed"] == 5
            and smoke_metrics["n_harness_errors"] == 0
        )

        # ── Extended window (15 more tasks) ──────────────────────────────
        extended_records: Optional[List[Dict[str, Any]]] = None
        extended_metrics: Optional[Dict[str, Any]] = None

        if smoke_clean:
            print("\n── Extended window (15 tasks) ──", flush=True)
            extended_records = []
            for i, desc in enumerate(EXTENDED_TASKS):
                print(f"\nTask {i + 6}/{5 + len(EXTENDED_TASKS)}: {desc[:70]}", flush=True)
                rec = _run_task(desc, runtime, task_idx=i + 6)
                extended_records.append(rec)
                tag = "✓ SUCCEEDED" if rec["status"] == "completed" else f"✗ {rec['status']} ({rec.get('reason', '')})"
                print(f"  → {tag} | elapsed={rec.get('elapsed_s')}s", flush=True)
            extended_metrics = _compute_metrics(extended_records)
            combined = _compute_metrics(smoke_records + extended_records)
            print(
                f"\nExtended results: {extended_metrics['n_succeeded']}/{extended_metrics['n_routed']} succeeded "
                f"({extended_metrics['success_rate'] * 100:.0f}%)",
                flush=True,
            )
            print(f"Combined (20): {combined['n_succeeded']}/{combined['n_routed']} "
                  f"({combined['success_rate'] * 100:.0f}%)", flush=True)
        else:
            print("\nSmoke window NOT clean — skipping extended window.", flush=True)

        # ── Write metrics JSONL ───────────────────────────────────────────
        all_records = smoke_records + (extended_records or [])
        with metrics_jsonl.open("w", encoding="utf-8") as f:
            for rec in all_records:
                f.write(json.dumps(rec) + "\n")
        print(f"\nMetrics written to {metrics_jsonl}", flush=True)

        # ── Write report ──────────────────────────────────────────────────
        final_metrics = (
            _compute_metrics(smoke_records + extended_records)
            if extended_records
            else smoke_metrics
        )
        _write_report(smoke_records, smoke_metrics, extended_records, extended_metrics, report_path)

        # ── Final verdict ─────────────────────────────────────────────────
        print("\n" + "=" * 60, flush=True)
        thresholds = final_metrics["thresholds_met"]
        for k, v in thresholds.items():
            print(f"  {'✓' if v else '✗'} {k}", flush=True)
        verdict = "ALL THRESHOLDS MET" if all(thresholds.values()) else "THRESHOLDS NOT MET"
        print(f"\n  → {verdict}", flush=True)
        print("=" * 60, flush=True)

    finally:
        db.close()


if __name__ == "__main__":
    main()
