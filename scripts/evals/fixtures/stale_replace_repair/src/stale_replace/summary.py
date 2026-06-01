from __future__ import annotations

from collections import Counter


def render_inventory(items: list[str]) -> str:
    counts = Counter(item.strip().lower() for item in items if item.strip())
    lines = [f"item={name}; quantity={count}" for name, count in counts.items()]
    return "\n".join(lines)
