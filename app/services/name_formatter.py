"""Helpers for keeping display names human-readable."""

import re


def humanize_display_name(value: str) -> str:
    """Convert slug-like display names into space-separated names."""
    text = (value or "").strip()
    if not text:
        return text

    if " " in text:
        return re.sub(r"\s+", " ", text).strip()

    if "-" in text or "_" in text:
        text = re.sub(r"[-_]+", " ", text)

    return re.sub(r"\s+", " ", text).strip()
