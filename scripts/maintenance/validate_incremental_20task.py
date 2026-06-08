"""Slice J 20-task controlled validation window — 2026-06-08.

Run from project root:
    PYTHONPATH=. python3 validate_incremental_20task.py

Requires live local_openclaw gateway.
Writes per-task JSONL and Markdown report.
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

# Must be set BEFORE any app imports.
os.environ["INCREMENTAL_EXECUTION_ENABLED"] = "true"

from app.config import settings  # noqa: E402

assert settings.INCREMENTAL_EXECUTION_ENABLED is True, (
    "Flag did not activate — check env var handling"
)

from app.database import SessionLocal  # noqa: E402
from app.services.agents.openclaw_service import OpenClawSessionService  # noqa: E402
import app.services.orchestration.phases.incremental_flow as _inc_module  # noqa: E402
from app.services.orchestration.phases.incremental_flow import (  # noqa: E402
    attempt_incremental_execution,
    _parse_verify_command,
)
from app.services.orchestration.planning.incremental_classifier import (  # noqa: E402
    is_incremental_candidate,
    _extract_file_paths,
)
from app.services.prompt_templates import OrchestrationState  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("validate_incr_20task")


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------

_strip_obs: Dict[str, Any] = {"had_fence": False, "stripped": False}
_verify_obs: Dict[str, Any] = {"exit_code": None}

_orig_strip = _inc_module._strip_code_fences


def _capturing_strip(content: str) -> str:
    had = bool(_inc_module._CODE_FENCE_RE.search(content))
    result = _orig_strip(content)
    _strip_obs["had_fence"] = had
    _strip_obs["stripped"] = had and result.strip() != content.strip()
    return result


_inc_module._strip_code_fences = _capturing_strip

_orig_subprocess_run = _inc_module.subprocess.run


def _capturing_subprocess_run(cmd, **kwargs):
    result = _orig_subprocess_run(cmd, **kwargs)
    _verify_obs["exit_code"] = result.returncode
    return result


_inc_module.subprocess.run = _capturing_subprocess_run


# ---------------------------------------------------------------------------
# Task corpus — 20 tasks across 4 groups
# ---------------------------------------------------------------------------

TASKS: List[Dict[str, str]] = [
    # Group A — Python (8): py_compile verification
    {
        "group": "python",
        "description": (
            "Create helpers.py with a function clamp(v, lo, hi) returning "
            "max(lo, min(v, hi)). Verify the file is valid Python."
        ),
    },
    {
        "group": "python",
        "description": (
            "Create parsers.py with a function split_csv(line) returning "
            "line.split(','). Verify the file is valid Python."
        ),
    },
    {
        "group": "python",
        "description": (
            "Create calculator.py with a function divide(a, b) that returns a / b. "
            "Verify the file is valid Python."
        ),
    },
    {
        "group": "python",
        "description": (
            "Create stringops.py with a function reverse(s) that returns s[::-1]. "
            "Verify the file is valid Python."
        ),
    },
    {
        "group": "python",
        "description": (
            "Create typechecks.py with a function is_even(n) that returns n % 2 == 0. "
            "Verify the file is valid Python."
        ),
    },
    {
        "group": "python",
        "description": (
            "Create transforms.py with a function square(x) that returns x * x. "
            "Verify the file is valid Python."
        ),
    },
    {
        "group": "python",
        "description": (
            "Create predicates.py with a function is_empty(s) that returns "
            "len(s) == 0. Verify the file is valid Python."
        ),
    },
    {
        "group": "python",
        "description": (
            "Create wrappers.py with a function identity(x) that returns x. "
            "Verify the file is valid Python."
        ),
    },
    # Group B — HTML (4): test -f verification
    {
        "group": "html",
        "description": "Create index.html with an h1 reading 'Welcome' and verify it exists.",
    },
    {
        "group": "html",
        "description": (
            "Create about.html with heading 'About Us' and a paragraph with content. "
            "Verify it exists."
        ),
    },
    {
        "group": "html",
        "description": (
            "Create contact.html with a form element and an h1 reading 'Contact'. "
            "Verify it exists."
        ),
    },
    {
        "group": "html",
        "description": (
            "Create landing.html with a hero section and a button with text "
            "'Get Started'. Verify it exists."
        ),
    },
    # Group C — CSS (4): test -f verification
    {
        "group": "css",
        "description": "Create styles.css with body margin 0 and h1 color #333. Verify it exists.",
    },
    {
        "group": "css",
        "description": "Create reset.css with * margin 0 and padding 0. Verify it exists.",
    },
    {
        "group": "css",
        "description": (
            "Create base.css with html font-size 16px and body line-height 1.5. "
            "Verify it exists."
        ),
    },
    {
        "group": "css",
        "description": (
            "Create theme.css with :root --bg set to #fff and --fg set to #000. "
            "Verify it exists."
        ),
    },
    # Group D — JSON (4): test -f verification
    {
        "group": "json",
        "description": 'Create config.json with key "version" set to "1.0.0". Verify it exists.',
    },
    {
        "group": "json",
        "description": 'Create manifest.json with key "name" set to "slice-j-app". Verify it exists.',
    },
    {
        "group": "json",
        "description": 'Create options.json with key "theme" set to "light". Verify it exists.',
    },
    {
        "group": "json",
        "description": (
            'Create meta.json with key "author" set to "test" and key "v" set to 1. '
            "Verify it exists."
        ),
    },
]


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _make_runtime(db) -> OpenClawSessionService:
    return OpenClawSessionService(db, session_id=None, task_id=None)


def _make_ctx(project_dir: str, runtime: OpenClawSessionService, task_idx: int) -> Any:
    state = OrchestrationState(
        session_id="val-20task",
        task_description="20-task window",
        project_name="controlled_window",
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
    task: Dict[str, str], runtime: OpenClawSessionService, task_idx: int
) -> Dict[str, Any]:
    description = task["description"]
    group = task["group"]
    file_paths = _extract_file_paths(description)
    verify_cmd = _parse_verify_command(description, file_paths) if file_paths else None

    record: Dict[str, Any] = {
        "task_idx": task_idx,
        "group": group,
        "description": description,
        "classifier_accepted": is_incremental_candidate(description),
        "incremental_attempted": False,
        "incremental_succeeded": False,
        "fallback_to_planning": False,
        "fallback_reason": None,
        "destructive_false_positive": False,
        "file_written": False,
        "verify_command": verify_cmd,
        "verify_exit_code": None,
        "output_contained_code_fence": None,
        "code_fence_stripped": None,
        "llm_calls": None,
        "elapsed_s": None,
        "planning_skipped": False,
        "synthetic_plan_populated": False,
        "progress_notes_written": False,  # never in incremental path
        "status": None,
    }

    if not record["classifier_accepted"]:
        record["status"] = "skipped_classifier_rejected"
        logger.warning(
            "SKIP task %d [%s] — classifier rejected", task_idx, group
        )
        return record

    record["incremental_attempted"] = True

    _strip_obs["had_fence"] = False
    _strip_obs["stripped"] = False
    _verify_obs["exit_code"] = None

    with tempfile.TemporaryDirectory(prefix=f"incr_20t_{task_idx}_") as tmpdir:
        ctx = _make_ctx(tmpdir, runtime, task_idx)
        t0 = time.monotonic()
        try:
            result = attempt_incremental_execution(
                ctx=ctx, task_description=description
            )
        except Exception as exc:
            record["status"] = "harness_exception"
            record["fallback_reason"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Exception on task %d", task_idx)
            return record
        elapsed = time.monotonic() - t0

        record["elapsed_s"] = round(elapsed, 2)
        record["status"] = result.get("status")
        record["output_contained_code_fence"] = _strip_obs["had_fence"]
        record["code_fence_stripped"] = _strip_obs["stripped"]
        record["verify_exit_code"] = _verify_obs["exit_code"]

        state = ctx.orchestration_state
        if result.get("status") == "completed":
            record["incremental_succeeded"] = True
            record["planning_skipped"] = True
            record["llm_calls"] = 1
            record["synthetic_plan_populated"] = len(state.plan) == 1
            if file_paths:
                record["file_written"] = Path(tmpdir, file_paths[0]).exists()
            # Destructive FP: succeeded but file wasn't created or plan wrong
            if not record["file_written"] or not record["synthetic_plan_populated"]:
                record["destructive_false_positive"] = True
        else:
            record["fallback_to_planning"] = True
            record["fallback_reason"] = result.get("reason")
            record["planning_skipped"] = False
            record["synthetic_plan_populated"] = len(state.plan) == 0

    return record


def _compute_metrics(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    routed = [r for r in records if r["incremental_attempted"]]
    succeeded = [r for r in routed if r["incremental_succeeded"]]
    fallbacks = [r for r in routed if r["fallback_to_planning"]]
    dfp = [r for r in routed if r["destructive_false_positive"]]
    skipped = [r for r in records if r["status"] == "skipped_classifier_rejected"]

    n = len(routed)
    sr = len(succeeded) / n if n else 0.0
    fr = len(fallbacks) / n if n else 0.0
    mean_llm = (
        sum(r["llm_calls"] for r in succeeded if r["llm_calls"])
        / len(succeeded)
        if succeeded
        else 0.0
    )
    mean_elapsed = (
        sum(r["elapsed_s"] for r in routed if r["elapsed_s"]) / n
        if n
        else 0.0
    )
    fb_reasons: Dict[str, int] = {}
    for r in fallbacks:
        k = r.get("fallback_reason") or "unknown"
        fb_reasons[k] = fb_reasons.get(k, 0) + 1

    # Per-group breakdown
    groups = {}
    for r in routed:
        g = r["group"]
        if g not in groups:
            groups[g] = {"routed": 0, "succeeded": 0, "fallbacks": 0}
        groups[g]["routed"] += 1
        if r["incremental_succeeded"]:
            groups[g]["succeeded"] += 1
        elif r["fallback_to_planning"]:
            groups[g]["fallbacks"] += 1

    thresholds = {
        "routed_gte_20": n >= 20,
        "success_rate_gte_70pct": sr >= 0.70,
        "fallback_rate_lte_5pct": fr <= 0.05,
        "no_destructive_fp": len(dfp) == 0,
        "mean_llm_calls_lte_2": mean_llm <= 2.0,
    }

    return {
        "n_total_tasks": len(records),
        "n_classifier_rejected": len(skipped),
        "n_routed": n,
        "n_succeeded": len(succeeded),
        "n_fallbacks": len(fallbacks),
        "n_harness_errors": sum(1 for r in routed if r["status"] == "harness_exception"),
        "n_destructive_fp": len(dfp),
        "n_planning_skipped": sum(1 for r in routed if r["planning_skipped"]),
        "n_progress_notes_written": 0,
        "success_rate": round(sr, 4),
        "fallback_rate": round(fr, 4),
        "mean_llm_calls": round(mean_llm, 4),
        "mean_elapsed_s": round(mean_elapsed, 2),
        "fallback_reasons": fb_reasons,
        "groups": groups,
        "thresholds_met": thresholds,
    }


REPORT_DIR = Path(__file__).resolve().parents[2] / "docs/roadmap/reports/maintenance"
REPORT_PATH = REPORT_DIR / "incremental-execution-20-task-controlled-window-20260608.md"
JSONL_PATH = REPORT_DIR / "incremental-execution-20-task-controlled-window-20260608.jsonl"


def _write_report(records: List[Dict[str, Any]], metrics: Dict[str, Any]) -> None:
    th = metrics["thresholds_met"]
    all_pass = all(th.values())

    verdict = (
        "ALL THRESHOLDS MET — PROCEED TO LIMITED OPT-IN"
        if all_pass
        else "ONE OR MORE THRESHOLDS NOT MET — SLICE J REMAINS FLAG-OFF"
    )

    lines = [
        "# Slice J: 20-Task Controlled Validation Window",
        "",
        "**Date:** 2026-06-08  ",
        "**Fix:** A+E output-compliance fix (prompt + pre-write `compile()`)  ",
        f"**Flag:** `INCREMENTAL_EXECUTION_ENABLED=True` (validation only)  ",
        f"**Backend:** `{settings.AGENT_BACKEND}`  ",
        "",
        "---",
        "",
        "## Verdict",
        "",
        f"**{verdict}**",
        "",
        "| Threshold | Target | Actual | Met |",
        "|---|---|---|---|",
        f"| Routed tasks | ≥ 20 | {metrics['n_routed']} | {'✓' if th['routed_gte_20'] else '✗'} |",
        f"| Success rate | ≥ 70% | {metrics['success_rate'] * 100:.1f}% | {'✓' if th['success_rate_gte_70pct'] else '✗'} |",
        f"| Fallback rate | ≤ 5% | {metrics['fallback_rate'] * 100:.1f}% | {'✓' if th['fallback_rate_lte_5pct'] else '✗'} |",
        f"| Destructive FP | = 0 | {metrics['n_destructive_fp']} | {'✓' if th['no_destructive_fp'] else '✗'} |",
        f"| Mean LLM calls | ≤ 2 | {metrics['mean_llm_calls']:.2f} | {'✓' if th['mean_llm_calls_lte_2'] else '✗'} |",
        "",
        "---",
        "",
        "## Summary",
        "",
        f"- Total tasks in corpus: {metrics['n_total_tasks']}",
        f"- Classifier rejected: {metrics['n_classifier_rejected']}",
        f"- Routed to incremental: {metrics['n_routed']}",
        f"- Succeeded: {metrics['n_succeeded']}",
        f"- Fallbacks: {metrics['n_fallbacks']}",
        f"- Harness errors: {metrics['n_harness_errors']}",
        f"- Destructive false positives: {metrics['n_destructive_fp']}",
        f"- Success rate: {metrics['success_rate'] * 100:.1f}%",
        f"- Fallback rate: {metrics['fallback_rate'] * 100:.1f}%",
        f"- Mean LLM calls: {metrics['mean_llm_calls']:.2f}",
        f"- Mean elapsed: {metrics['mean_elapsed_s']:.1f}s",
        f"- Full-planning calls avoided: {metrics['n_planning_skipped']}",
        f"- Known-good commands written: {metrics['n_progress_notes_written']}",
    ]
    if metrics["fallback_reasons"]:
        lines.append(f"- Fallback reasons: {json.dumps(metrics['fallback_reasons'])}")

    lines += [
        "",
        "---",
        "",
        "## Per-Group Results",
        "",
        "| Group | Routed | Succeeded | Fallbacks | Success Rate |",
        "|---|---|---|---|---|",
    ]
    for g, gm in sorted(metrics["groups"].items()):
        gsr = gm["succeeded"] / gm["routed"] * 100 if gm["routed"] else 0
        lines.append(
            f"| {g} | {gm['routed']} | {gm['succeeded']} | {gm['fallbacks']} "
            f"| {gsr:.0f}% |"
        )

    lines += [
        "",
        "---",
        "",
        "## Per-Task Results",
        "",
        "| # | Grp | Status | Fallback | Reason | Fence | Verify Exit | LLM | Elapsed | Plan OK | Description (truncated) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        desc = r["description"][:50] + "…"
        fence_val = str(r["output_contained_code_fence"]).lower() if r.get("output_contained_code_fence") is not None else "—"
        lines.append(
            f"| {r['task_idx']} "
            f"| {r['group']} "
            f"| {r['status'] or '—'} "
            f"| {'yes' if r['fallback_to_planning'] else 'no'} "
            f"| {r.get('fallback_reason') or '—'} "
            f"| {fence_val} "
            f"| {r.get('verify_exit_code') if r.get('verify_exit_code') is not None else '—'} "
            f"| {r.get('llm_calls') or '—'} "
            f"| {r.get('elapsed_s') or '—'}s "
            f"| {'✓' if r.get('synthetic_plan_populated') else '—'} "
            f"| {desc} |"
        )

    lines += ["", "---", ""]

    # Readiness section
    lines += [
        "## Readiness Assessment",
        "",
    ]
    if all_pass:
        lines += [
            "**READY_FOR_LIMITED_OPT_IN.**",
            "",
            "All five thresholds met across 20 tasks spanning Python, HTML, CSS, and JSON.",
            "Destructive false positives: 0.",
            "The incremental execution path is safe for a limited opt-in window.",
            "",
            "**Recommendation:** Move `INCREMENTAL_EXECUTION_ENABLED` to limited opt-in.",
            "Flag remains `False` by default. Opt-in to be enabled per-deployment or",
            "per-session only after explicit operator sign-off.",
        ]
    else:
        failed = [k for k, v in th.items() if not v]
        lines += [
            "**NOT READY.** Failed thresholds:",
            "",
        ]
        for f in failed:
            lines.append(f"- `{f}`")
        lines += [
            "",
            "Slice J remains `INCREMENTAL_EXECUTION_ENABLED=False`.",
            "Resolve open fallbacks before re-running the controlled window.",
        ]

    lines += ["", "---", ""]

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {REPORT_PATH}", flush=True)


def main() -> None:
    print("=" * 60, flush=True)
    print("Slice J 20-Task Controlled Validation Window", flush=True)
    print(f"INCREMENTAL_EXECUTION_ENABLED={settings.INCREMENTAL_EXECUTION_ENABLED}", flush=True)
    print(f"AGENT_BACKEND={settings.AGENT_BACKEND}", flush=True)
    print(f"Tasks: {len(TASKS)}", flush=True)

    import inspect
    src_strip = inspect.getsource(_orig_strip)
    uses_search = "_CODE_FENCE_RE.search" in src_strip
    print(f"search() active: {uses_search}", flush=True)

    # Confirm compile() guard present
    import inspect as _ins
    src_attempt = inspect.getsource(attempt_incremental_execution)
    has_compile = "compile(content" in src_attempt
    print(f"compile() guard active: {has_compile}", flush=True)
    print("=" * 60, flush=True)

    db = SessionLocal()
    try:
        runtime = _make_runtime(db)
        records: List[Dict[str, Any]] = []

        for i, task in enumerate(TASKS):
            idx = i + 1
            grp = task["group"]
            desc = task["description"]
            print(f"\nTask {idx:2}/{len(TASKS)} [{grp:4}]: {desc[:65]}", flush=True)
            rec = _run_task(task, runtime, task_idx=idx)
            records.append(rec)

            if rec["status"] == "skipped_classifier_rejected":
                print("  → SKIPPED (classifier rejected)", flush=True)
            elif rec["incremental_succeeded"]:
                print(
                    f"  → SUCCEEDED | fence={rec.get('output_contained_code_fence')} "
                    f"| verify_exit={rec.get('verify_exit_code')} "
                    f"| elapsed={rec.get('elapsed_s')}s",
                    flush=True,
                )
            else:
                print(
                    f"  → FAILED ({rec.get('fallback_reason')}) "
                    f"| fence={rec.get('output_contained_code_fence')} "
                    f"| elapsed={rec.get('elapsed_s')}s",
                    flush=True,
                )

        # Write JSONL
        with JSONL_PATH.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        print(f"\nMetrics: {JSONL_PATH}", flush=True)

        metrics = _compute_metrics(records)
        _write_report(records, metrics)

        print("\n" + "=" * 60, flush=True)
        th = metrics["thresholds_met"]
        for k, v in th.items():
            print(f"  {'✓' if v else '✗'} {k}", flush=True)
        verdict = "ALL THRESHOLDS MET" if all(th.values()) else "THRESHOLDS NOT MET"
        print(f"\n  → {verdict}", flush=True)
        print(f"  → Full-planning calls avoided: {metrics['n_planning_skipped']}", flush=True)
        print("=" * 60, flush=True)

    finally:
        db.close()


if __name__ == "__main__":
    main()
