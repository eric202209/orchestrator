#!/usr/bin/env python3
"""Inspect checkpoint JSON files for one session."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _summarize(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    state = payload.get("orchestration_state") or {}
    context = payload.get("context") or {}
    return {
        "path": str(path),
        "checkpoint_name": payload.get("checkpoint_name") or path.stem,
        "task_id": context.get("task_id"),
        "status": state.get("status"),
        "current_step_index": state.get("current_step_index")
        or payload.get("current_step_index"),
        "plan_step_count": len(state.get("plan") or []),
        "step_result_count": len(payload.get("step_results") or []),
        "has_replay_overrides": bool(context.get("replay_overrides")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="List checkpoint summaries.")
    parser.add_argument("--checkpoint-root", default="checkpoints")
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = Path(args.checkpoint_root)
    candidates = list(root.glob(f"session_{args.session_id}_*.json"))
    session_dir = root / f"session_{args.session_id}"
    if session_dir.exists():
        candidates.extend(session_dir.glob("*.json"))
    summaries = [_summarize(path) for path in sorted(set(candidates))]
    report = {"session_id": args.session_id, "checkpoints": summaries}

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    print(f"Checkpoints for session {args.session_id}")
    for item in summaries:
        print(
            f"- {item['checkpoint_name']} task={item['task_id']} "
            f"status={item['status']} step={item['current_step_index']} "
            f"plan_steps={item['plan_step_count']} results={item['step_result_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
