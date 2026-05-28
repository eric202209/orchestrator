"""Greeting formatting helpers."""

from __future__ import annotations


def normalize_greeting(name: str) -> str:
    cleaned = " ".join(name.split()).title()
    return f"Hello, {cleaned}!"
