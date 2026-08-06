"""Deterministic, Orchestrator-owned exact anchors for operation-level repair.

The repair provider must never author ``replace_in_file.old``.  Phase 32N-3C
proved why: the model reproduced the correct intent but reconstructed the anchor
from memory, dropping the blank lines that separate the file's import groups, so
the text it claimed to be replacing did not occur in the pinned source at all.

Every anchor offered to the model is therefore derived here, by deterministic
code, from the version-fenced source itself.  An anchor is a verbatim byte slice
of that source; the model only chooses one by identifier and supplies the
replacement text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MAX_ANCHORS_PER_OPERATION = 3

DERIVATION_VERIFIED_ORIGINAL = "verified_original_anchor"
DERIVATION_MINIMAL_DIVERGENT = "minimal_divergent_realignment"
DERIVATION_BLANK_LINE_TOLERANT = "blank_line_tolerant_realignment"


@dataclass(frozen=True)
class SourceAnchor:
    """One exact, uniquely occurring slice of the pinned source."""

    anchor_id: str
    step_number: int
    operation_index: int
    relative_path: str
    version_identity: str
    text: str
    derivation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "step_number": self.step_number,
            "operation_index": self.operation_index,
            "relative_path": self.relative_path,
            "version_identity": self.version_identity,
            "derivation": self.derivation,
            "old": self.text,
        }


def _significant(lines: list[str]) -> list[str]:
    return [line for line in lines if line.strip()]


def _unique_realignment(full_source: str, wanted: list[str]) -> str | None:
    """Return the exact source slice whose significant lines equal ``wanted``.

    Blank lines inside the matched window are preserved verbatim, which is the
    whole point: the model's whitespace is discarded and the file's own
    whitespace is restored.  A window that occurs zero times or more than once
    is rejected, so an ambiguous anchor is never offered.
    """

    if not wanted:
        return None
    source_lines = full_source.splitlines(keepends=True)
    significant_positions = [
        index for index, line in enumerate(source_lines) if line.strip()
    ]
    significant_text = [
        source_lines[index].rstrip("\n") for index in significant_positions
    ]
    matches: list[str] = []
    span = len(wanted)
    for offset in range(len(significant_text) - span + 1):
        if significant_text[offset : offset + span] != wanted:
            continue
        start = significant_positions[offset]
        end = significant_positions[offset + span - 1]
        slice_text = "".join(source_lines[start : end + 1])
        if slice_text.endswith("\n"):
            slice_text = slice_text[:-1]
        matches.append(slice_text)
    if len(matches) != 1:
        return None
    return matches[0]


def _minimal_divergent_lines(old_lines: list[str], new_lines: list[str]) -> list[str]:
    """Trim leading and trailing lines the operation does not actually change."""

    prefix = 0
    limit = min(len(old_lines), len(new_lines))
    while prefix < limit and old_lines[prefix] == new_lines[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < limit - prefix
        and old_lines[len(old_lines) - 1 - suffix]
        == new_lines[len(new_lines) - 1 - suffix]
    ):
        suffix += 1
    return old_lines[prefix : len(old_lines) - suffix]


def derive_operation_anchors(
    *,
    step_number: int,
    operation_index: int,
    relative_path: str,
    version_identity: str,
    original_old: Any,
    original_new: Any,
    full_source: Any,
) -> tuple[SourceAnchor, ...]:
    """Derive up to ``MAX_ANCHORS_PER_OPERATION`` exact anchors, best first.

    Three bounded strategies are tried, in the order a repair should prefer:

    1. the original anchor itself, when it is still exactly and uniquely
       present — the failure then concerns the replacement, not the anchor;
    2. the smallest region the operation actually changes, realigned onto the
       real source so its blank-line structure is authentic;
    3. the whole region the original anchor addressed, realigned the same way.

    Anything ambiguous or absent is dropped rather than guessed at.
    """

    if not isinstance(full_source, str) or not full_source:
        return ()
    if not isinstance(original_old, str) or not original_old:
        return ()
    new_text = original_new if isinstance(original_new, str) else ""

    old_significant = _significant(original_old.splitlines())
    new_significant = _significant(new_text.splitlines())

    candidates: list[tuple[str, str]] = []
    if full_source.count(original_old) == 1:
        candidates.append((DERIVATION_VERIFIED_ORIGINAL, original_old))

    minimal = _minimal_divergent_lines(old_significant, new_significant)
    if minimal and minimal != old_significant:
        realigned_minimal = _unique_realignment(full_source, minimal)
        if realigned_minimal is not None:
            candidates.append((DERIVATION_MINIMAL_DIVERGENT, realigned_minimal))

    realigned_full = _unique_realignment(full_source, old_significant)
    if realigned_full is not None:
        candidates.append((DERIVATION_BLANK_LINE_TOLERANT, realigned_full))

    anchors: list[SourceAnchor] = []
    seen: set[str] = set()
    for derivation, text in candidates:
        if not text or text in seen:
            continue
        if full_source.count(text) != 1:
            continue
        seen.add(text)
        anchors.append(
            SourceAnchor(
                anchor_id=f"anchor-{step_number}-{operation_index}-{len(anchors) + 1}",
                step_number=step_number,
                operation_index=operation_index,
                relative_path=relative_path,
                version_identity=version_identity,
                text=text,
                derivation=derivation,
            )
        )
        if len(anchors) == MAX_ANCHORS_PER_OPERATION:
            break
    return tuple(anchors)
