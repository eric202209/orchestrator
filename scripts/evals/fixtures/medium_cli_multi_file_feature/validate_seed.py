#!/usr/bin/env python3
"""Validate the seed fixture shape before an orchestrator run."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
REQUIRED_PATHS = [
    ROOT / "pyproject.toml",
    ROOT / "src" / "medium_cli" / "cli.py",
    ROOT / "src" / "medium_cli" / "formatting.py",
    ROOT / "src" / "medium_cli" / "store.py",
    ROOT / "tests" / "test_cli.py",
    ROOT / "tests" / "test_store.py",
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
        print("Seed fixture unexpectedly passes; expected missing summary feature.")
        return 1
    combined = f"{completed.stdout}\n{completed.stderr}"
    if "summary" not in combined and "NotImplementedError" not in combined:
        print(combined[-2000:], file=sys.stderr)
        return 1
    print("Seed fixture is valid: pytest fails on the missing summary feature.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
