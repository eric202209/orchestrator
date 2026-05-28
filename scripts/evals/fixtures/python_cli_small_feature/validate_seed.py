#!/usr/bin/env python3
"""Validate the seed fixture shape before an orchestrator run."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
REQUIRED_PATHS = [
    ROOT / "pyproject.toml",
    ROOT / "src" / "small_cli" / "cli.py",
    ROOT / "tests" / "test_cli.py",
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
        print("Seed fixture unexpectedly passes; expected missing --uppercase feature.")
        return 1
    combined = f"{completed.stdout}\n{completed.stderr}"
    if "--uppercase" not in combined and "unrecognized arguments" not in combined:
        print(combined[-2000:], file=sys.stderr)
        return 1
    print("Seed fixture is valid: required files exist and pytest fails on missing --uppercase.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
