"""Shared placeholder-content policy for orchestration validation."""

from __future__ import annotations

from pathlib import Path

PLACEHOLDER_FIXTURE_PATH_PARTS = frozenset(
    {
        "fixture",
        "fixtures",
        "sample",
        "samples",
        "test_data",
        "testdata",
    }
)


def path_allows_placeholder_fixture_content(path: Path | str) -> bool:
    candidate = Path(str(path or "").strip())
    parts = {part.lower() for part in candidate.parts}
    if parts.intersection(PLACEHOLDER_FIXTURE_PATH_PARTS):
        return True
    return candidate.name.lower().startswith(("sample.", "fixture."))
