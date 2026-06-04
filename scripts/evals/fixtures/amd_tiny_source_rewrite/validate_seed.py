#!/usr/bin/env python3
"""Validate the AMD tiny fixture starts from one failing source behavior."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parent
    required = [
        root / "pyproject.toml",
        root / "src" / "amd_tiny" / "__init__.py",
        root / "src" / "amd_tiny" / "formatting.py",
        root / "tests" / "test_formatting.py",
    ]
    missing = [path.relative_to(root).as_posix() for path in required if not path.exists()]
    if missing:
        print(f"Missing fixture paths: {', '.join(missing)}", file=sys.stderr)
        return 1

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    if result.returncode == 0:
        print("Seed fixture unexpectedly passes; expected format_label mismatch.")
        return 1

    print("Seed fixture is valid: pytest fails on the intended formatter behavior.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
