"""Helpers for a partially completed two-step workflow."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_step_one() -> str:
    return (ROOT / "docs" / "step-one.txt").read_text(encoding="utf-8").strip()


def load_step_two() -> str:
    raise NotImplementedError("resume from checkpoint and complete step two")


def build_status_report() -> str:
    return "\n".join([load_step_one(), load_step_two()])
