"""Extract stable failure signatures from exceptions for knowledge matching."""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from pydantic import BaseModel

# Patterns stripped from error messages to produce stable normalized_message
_STRIP_PATTERNS = [
    re.compile(r"/[^\s]+"),  # file paths
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),  # UUIDs
    re.compile(r"\bline\s+\d+\b", re.IGNORECASE),  # line numbers
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*"),  # timestamps
    re.compile(r"\b\d+\b"),  # bare integers (port numbers, IDs)
]

_NORMALIZED_MAX_CHARS = 200


class FailureSignature(BaseModel):
    phase: str
    error_type: str
    tool_name: Optional[str]
    normalized_message: str
    retry_count: int

    def signature_hash(self) -> str:
        """Stable hash over phase + error_type + tool_name + normalized_message.

        retry_count is execution state — excluded from hash so retries share
        the same signature and can be matched against known failure memories.
        """
        raw = f"{self.phase}:{self.error_type}:{self.tool_name or ''}:{self.normalized_message}"
        return hashlib.sha256(raw.encode()).hexdigest()


def extract(
    exc: Exception,
    phase: str,
    tool_name: Optional[str],
    retry_count: int,
) -> FailureSignature:
    error_type = type(exc).__name__
    raw_message = str(exc)

    normalized = raw_message.lower()
    for pattern in _STRIP_PATTERNS:
        normalized = pattern.sub("", normalized)
    # Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = normalized[:_NORMALIZED_MAX_CHARS]

    return FailureSignature(
        phase=phase,
        error_type=error_type,
        tool_name=tool_name,
        normalized_message=normalized,
        retry_count=retry_count,
    )
