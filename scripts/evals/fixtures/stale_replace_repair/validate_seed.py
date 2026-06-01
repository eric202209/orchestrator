#!/usr/bin/env python3
"""Validate the seed fixture starts with the intended output mismatch."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
REQUIRED_PATHS = [
    ROOT / "pyproject.toml",
    ROOT / "src" / "stale_replace" / "__init__.py",
    ROOT / "src" / "stale_replace" / "summary.py",
    ROOT / "tests" / "test_summary.py",
]


def main() -> int:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_PATHS if not path.exists()]
    if missing:
        print(f"Missing fixture paths: {', '.join(missing)}", file=sys.stderr)
        return 2

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        print("Seed fixture unexpectedly passes; expected summary output mismatch.")
        return 1
    combined = f"{completed.stdout}\n{completed.stderr}"
    if "apple: 2" not in combined or "item=apple; quantity=2" not in combined:
        print(combined[-2000:], file=sys.stderr)
        return 1
    print("Seed fixture is valid: pytest fails on the intended stale summary output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
