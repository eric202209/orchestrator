#!/usr/bin/env python3
"""Inspect an append-only orchestration event journal."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _event_path(project_dir: Path, session_id: int, task_id: int) -> Path:
    return (
        project_dir
        / ".agent"
        / "events"
        / f"session_{session_id}_task_{task_id}.jsonl"
    )


def read_journal(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    if not path.exists():
        raise SystemExit(f"Event journal not found: {path}")
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            malformed.append({"line": line_number, "error": str(exc)})
    return events, malformed


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize an event journal.")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--tail", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    path = _event_path(Path(args.project_dir), args.session_id, args.task_id)
    events, malformed = read_journal(path)
    type_counts = Counter(str(event.get("event_type")) for event in events)
    report = {
        "path": str(path),
        "event_count": len(events),
        "malformed_count": len(malformed),
        "event_type_counts": dict(sorted(type_counts.items())),
        "first_event": events[0] if events else None,
        "last_event": events[-1] if events else None,
        "tail": events[-args.tail :] if args.tail > 0 else [],
        "malformed": malformed,
    }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"Event journal: {path}")
    print(f"events={len(events)} malformed={len(malformed)}")
    print("Event Types:")
    for event_type, count in sorted(type_counts.items()):
        print(f"- {event_type}: {count}")
    print("Tail:")
    for event in report["tail"]:
        print(
            f"- {event.get('timestamp')} {event.get('event_type')} {event.get('event_id')}"
        )
    return 0 if events else 1


if __name__ == "__main__":
    raise SystemExit(main())
