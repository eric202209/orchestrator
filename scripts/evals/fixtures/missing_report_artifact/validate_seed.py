#!/usr/bin/env python3
"""Validate the seed fixture passes tests while missing the report artifact."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "reports" / "repair-summary.md"
REQUIRED_PATHS = [
    ROOT / "pyproject.toml",
    ROOT / "src" / "report_artifact" / "__init__.py",
    ROOT / "src" / "report_artifact" / "calculator.py",
    ROOT / "tests" / "test_calculator.py",
]


def main() -> int:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_PATHS if not path.exists()]
    if missing:
        print(f"Missing fixture paths: {', '.join(missing)}", file=sys.stderr)
        return 2
    if REPORT.exists():
        print("Seed fixture unexpectedly contains reports/repair-summary.md.")
        return 1

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        print((completed.stdout + completed.stderr)[-2000:], file=sys.stderr)
        return 1
    print("Seed fixture is valid: pytest passes and the required report is missing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
