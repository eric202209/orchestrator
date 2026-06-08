"""Diagnostic probe: capture raw LLM output from incremental content generation.

Runs 3 tasks (2 known-failing, 1 known-succeeding), captures raw execute_task
output before any stripping. No file writes. No verification.

Run:
    PYTHONPATH=. python3 probe_incremental_output.py 2>/dev/null
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

os.environ["INCREMENTAL_EXECUTION_ENABLED"] = "true"

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.services.agents.openclaw_service import OpenClawSessionService  # noqa: E402
from app.services.orchestration.planning.incremental_classifier import (  # noqa: E402
    _extract_file_paths,
)
from app.services.performance_optimizations import optimize_prompt  # noqa: E402

PROBE_TASKS: List[Dict[str, str]] = [
    {
        "label": "helpers.py (known-failing, clamp)",
        "description": (
            "Create helpers.py with a function clamp(v, lo, hi) returning "
            "max(lo, min(v, hi)). Verify the file is valid Python."
        ),
    },
    {
        "label": "wrappers.py (known-failing, trivial identity)",
        "description": (
            "Create wrappers.py with a function identity(x) that returns x. "
            "Verify the file is valid Python."
        ),
    },
    {
        "label": "stringops.py (known-succeeding, reverse)",
        "description": (
            "Create stringops.py with a function reverse(s) that returns s[::-1]. "
            "Verify the file is valid Python."
        ),
    },
]


def _build_prompt(description: str, primary_file: str) -> str:
    return (
        f"Generate the content for the following file creation task.\n\n"
        f"Task: {description}\n"
        f"File to create: {primary_file}\n\n"
        f"Output ONLY the raw file content. No code fences, no explanation, "
        f"no markdown. Start directly with the content."
    )


def main() -> None:
    db = SessionLocal()
    try:
        runtime = OpenClawSessionService(db, session_id=None, task_id=None)

        for task in PROBE_TASKS:
            desc = task["description"]
            label = task["label"]
            file_paths = _extract_file_paths(desc)
            primary_file = file_paths[0] if file_paths else "unknown.py"
            prompt = _build_prompt(desc, primary_file)
            optimized = optimize_prompt(prompt, max_tokens=25000)

            print(f"\n{'='*60}")
            print(f"TASK: {label}")
            print(f"{'='*60}")
            print(f"--- Original prompt ({len(prompt)} chars) ---")
            print(prompt)
            print(f"\n--- Optimized prompt ({len(optimized)} chars) ---")
            print(optimized)

            result = asyncio.run(
                runtime.execute_task(prompt, timeout_seconds=240)
            )
            raw = (result.get("output") or "").strip()

            print(f"\n--- Raw output ({len(raw)} chars) ---")
            print(repr(raw[:600]))
            print(f"\n--- First 400 chars displayed ---")
            print(raw[:400])
            print(f"\n--- Status: {result.get('status')} ---")

    finally:
        db.close()


if __name__ == "__main__":
    main()
