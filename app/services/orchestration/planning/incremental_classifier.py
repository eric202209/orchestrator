"""Incremental execution candidate classifier — Slice J.

Pure function: no LLM calls, no filesystem reads.
"""

from __future__ import annotations

import re
from typing import List

_DIAGNOSIS_KEYWORDS: frozenset = frozenset(
    ["failing", "error", "import", "debug", "resume"]
)
_MAX_DESCRIPTION_CHARS: int = 220

# File-path token: optional path components + filename + letter-starting extension.
# Extension must start with a letter so version numbers (e.g. 1.0.0, ext="0") are
# not matched.
_PATH_TOKEN_RE = re.compile(r"[a-zA-Z0-9_.-][a-zA-Z0-9_./-]*\.[a-zA-Z][a-zA-Z0-9]{0,9}")

# Explicit verification phrase.
_VERIFY_RE = re.compile(r"\bverify\b|\bcheck\b|\bconfirm\b", re.IGNORECASE)

# Content or constraint specification keywords.
_CONTENT_RE = re.compile(
    r"\bwith\b|\bedit only\b|do not\b|\bappend\b",
    re.IGNORECASE,
)


def _extract_file_paths(description: str) -> List[str]:
    """Return unique file-path-like tokens from description, preserving order."""
    candidates = _PATH_TOKEN_RE.findall(description)
    seen: set = set()
    result: List[str] = []
    for c in candidates:
        if c in seen:
            continue
        # Require alphabetic first character or a directory separator in the path.
        # This filters out tokens like "1.0" (digit-first, no slash).
        if c[0].isalpha() or "/" in c:
            seen.add(c)
            result.append(c)
    return result


def is_incremental_candidate(description: str) -> bool:
    """Return True if the task description satisfies all 5 incremental criteria.

    Criteria (all must be met):
    1. Names 1–2 explicit file path(s).
    2. Specifies content or constraints for each named file.
    3. States an explicit verification command.
    4. Contains no diagnosis keywords: failing, error, import, debug, resume.
    5. Description length ≤ 220 chars.
    """
    if not description:
        return False
    # Criterion 5: length
    if len(description) > _MAX_DESCRIPTION_CHARS:
        return False
    # Criterion 4: no diagnosis keywords
    desc_lower = description.lower()
    if any(kw in desc_lower for kw in _DIAGNOSIS_KEYWORDS):
        return False
    # Criterion 1: 1–2 explicit file paths
    paths = _extract_file_paths(description)
    if not paths or len(paths) > 2:
        return False
    # Criterion 2: content or constraint specified
    if not _CONTENT_RE.search(description):
        return False
    # Criterion 3: explicit verification phrase
    if not _VERIFY_RE.search(description):
        return False
    return True
