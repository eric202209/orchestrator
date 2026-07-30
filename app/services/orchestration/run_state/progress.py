"""Canonical metadata contract for durable execution-progress evidence."""

from __future__ import annotations

from typing import Any, Mapping


EXECUTION_PROGRESS_METADATA_KEY = "counts_as_execution_progress"


def execution_progress_metadata(
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a durable event as evidence of meaningful original execution work."""

    payload = dict(metadata or {})
    payload[EXECUTION_PROGRESS_METADATA_KEY] = True
    return payload
