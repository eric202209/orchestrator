#!/usr/bin/env python3
"""Validate the seed fixture starts with the intended import failure."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
REQUIRED_PATHS = [
    ROOT / "pyproject.toml",
    ROOT / "src" / "import_repair" / "__init__.py",
    ROOT / "src" / "import_repair" / "formatter.py",
    ROOT / "tests" / "test_formatter.py",
]


def main() -> int:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_PATHS if not path.exists()]
    if missing:
        print(f"Missing fixture paths: {', '.join(missing)}", file=sys.stderr)
        return 2

    completed = subprocess.run(
        [sys.executable, "-c", "from import_repair import normalize_greeting"],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        print("Seed fixture unexpectedly imports; expected import failure.")
        return 1
    combined = f"{completed.stdout}\n{completed.stderr}"
    if "ModuleNotFoundError" not in combined and "ImportError" not in combined:
        print(combined[-2000:], file=sys.stderr)
        return 1
    if "import_repair.formatters" not in combined:
        print(combined[-2000:], file=sys.stderr)
        return 1
    print("Seed fixture is valid: package import fails on the intended import path error.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
