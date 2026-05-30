"""Tiny message-printing CLI used by the orchestrator eval fixture."""

from __future__ import annotations

import argparse


def format_message(message: str) -> str:
    return message


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print a message.")
    parser.add_argument("message", help="Message to print")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    print(format_message(args.message))
    return 0
