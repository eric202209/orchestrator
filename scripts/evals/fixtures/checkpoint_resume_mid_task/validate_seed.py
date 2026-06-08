#!/usr/bin/env python3
"""Validate the seed fixture starts from a partial checkpoint state."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
REQUIRED_PATHS = [
    ROOT / "pyproject.toml",
    ROOT / ".agent" / "events",
    ROOT / "docs" / "step-one.txt",
    ROOT / "src" / "resume_task" / "__init__.py",
    ROOT / "src" / "resume_task" / "workflow.py",
    ROOT / "tests" / "test_workflow.py",
]


def main() -> int:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_PATHS if not path.exists()]
    if missing:
        print(f"Missing fixture paths: {', '.join(missing)}", file=sys.stderr)
        return 2

    step_one = (ROOT / "docs" / "step-one.txt").read_text(encoding="utf-8").strip()
    if step_one != "step-one: complete":
        print("Unexpected step-one artifact contents.", file=sys.stderr)
        return 1

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from resume_task.workflow import build_status_report; build_status_report()",
        ],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        print("Seed fixture unexpectedly completes; expected incomplete step two.")
        return 1
    combined = f"{completed.stdout}\n{completed.stderr}"
    if "step-two: complete" not in combined and "NotImplementedError" not in combined:
        print(combined[-2000:], file=sys.stderr)
        return 1
    print("Seed fixture is valid: step one exists and step two is incomplete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
