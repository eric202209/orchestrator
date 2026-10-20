"""Disabled compatibility entry point; release installation is APT/dpkg only."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "custom protected-release installation is disabled; use the independently "
        "authenticated APT repository and exact package version",
        file=sys.stderr,
    )
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
