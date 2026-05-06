#!/usr/bin/env python3
"""Capture a read-only replay report fixture for semantic regression tests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.services.orchestration.replay import reconstruct_execution_state
from app.tests.report_semantic_assertions import semantic_replay_report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize a replay report from real session event evidence."
    )
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--checkpoint-dir")
    parser.add_argument("--compare-workspace", action="store_true")
    parser.add_argument(
        "--semantic", action="store_true", help="Store stable semantic subset"
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    report = reconstruct_execution_state(
        project_dir=Path(args.project_dir),
        session_id=args.session_id,
        task_id=args.task_id,
        checkpoint_dir=Path(args.checkpoint_dir) if args.checkpoint_dir else None,
        compare_workspace=args.compare_workspace,
    )
    payload: dict[str, Any] = (
        semantic_replay_report(report) if args.semantic else report
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
