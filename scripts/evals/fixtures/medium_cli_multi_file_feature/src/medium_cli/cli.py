"""Task-list CLI used by the orchestrator medium eval fixture."""

from __future__ import annotations

import argparse

from medium_cli.formatting import format_task_line
from medium_cli.store import TaskStore


DEFAULT_TASKS = (
    "write docs",
    "ship feature",
    "close ticket",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect a small task list.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List tasks")
    list_parser.add_argument("--completed", action="store_true", help="Show completed flags")

    return parser


def build_store() -> TaskStore:
    store = TaskStore()
    store.add("write docs", completed=True)
    store.add("ship feature", completed=False)
    store.add("close ticket", completed=True)
    return store


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = build_store()

    if args.command == "list":
        for task in store.all():
            print(format_task_line(task, include_status=args.completed))
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
