#!/usr/bin/env python3
"""Phase 31 launch precondition F11: auto-commit daemon scoping check.

Phase 30F finding F11 (carried forward through Phase 30L-1's freeze review)
requires confirming that no external, non-Claude-Code process can commit
changes to the orchestrator repository (or, by the same policy, any Phase
31 target repository) during a certification run. This environment has no
process, cron, or systemd-timer visibility into the daemon responsible for
the 177+ historical "update" commits recorded since Phase 24 (first
observed at Phase 30C-V, commit `9bd8d13`) -- it runs outside this
container's inspectable scope. This script therefore cannot *disable* the
daemon; it detects its fingerprint (an empty-message-less commit whose
subject is exactly "update", authored by the operator's own git identity)
and reports whether it has fired inside a declared lookback window, plus
whether the working tree is currently dirty in a way the daemon could
sweep up.

This is a detection/monitoring check, not a guarantee. A PASSED result
means: no daemon-signature commit landed in the lookback window and the
working tree is clean. It does not prove the daemon is disabled -- only
that it did not fire recently. Treat repeated PASSED results across a
Phase 31 run's duration as the operational evidence Phase 30F's F11 policy
asked for; a single pre-run PASSED check is a necessary, not sufficient,
precondition.

Exit code 0: no daemon-signature commit in the lookback window AND working
tree clean (no uncommitted changes at check time).
Exit code 1: a daemon-signature commit was found inside the lookback
window, or the working tree is dirty.

Usage:
    python3 scripts/maintenance/phase31_launch_precondition_f11_autocommit_daemon.py
    python3 scripts/maintenance/phase31_launch_precondition_f11_autocommit_daemon.py \
        --lookback-minutes 120 --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

DAEMON_SUBJECT_FINGERPRINT = "update"


def _run(cmd: list[str]) -> str:
    return subprocess.run(
        cmd, capture_output=True, text=True, check=True
    ).stdout.strip()


def check_daemon(lookback_minutes: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    log_output = _run(
        [
            "git",
            "log",
            f"--since={since.isoformat()}",
            "--format=%H|%an|%aI|%s",
        ]
    )

    daemon_commits = []
    for line in log_output.splitlines():
        if not line:
            continue
        commit_hash, author, commit_date, subject = line.split("|", 3)
        if subject.strip() == DAEMON_SUBJECT_FINGERPRINT:
            daemon_commits.append(
                {
                    "commit": commit_hash,
                    "author": author,
                    "date": commit_date,
                    "subject": subject,
                }
            )

    status_output = _run(["git", "status", "--porcelain"])
    working_tree_dirty = bool(status_output)

    passed = len(daemon_commits) == 0 and not working_tree_dirty

    return {
        "lookback_minutes": lookback_minutes,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "daemon_commits_in_window": daemon_commits,
        "working_tree_dirty": working_tree_dirty,
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=60,
        help="How far back to scan git history for daemon-signature commits "
        "(default: 60).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    args = parser.parse_args()

    result = check_daemon(args.lookback_minutes)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["passed"]:
            print(
                f"F11 PASSED: no daemon-signature commit in the last "
                f"{args.lookback_minutes} minute(s); working tree clean. "
                "This is a point-in-time signal, not proof of disablement -- "
                "re-run through the Phase 31 window."
            )
        else:
            print("F11 FAILED:")
            if result["daemon_commits_in_window"]:
                print(
                    f"  - {len(result['daemon_commits_in_window'])} "
                    "daemon-signature commit(s) found in the lookback window:"
                )
                for commit in result["daemon_commits_in_window"]:
                    print(f"      {commit}")
            if result["working_tree_dirty"]:
                print(
                    "  - working tree is dirty; an uncommitted change could "
                    "be swept up by the daemon before the operator reviews it."
                )

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
