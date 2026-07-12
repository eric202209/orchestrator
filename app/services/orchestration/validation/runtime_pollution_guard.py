"""Detects unexpected top-level runtime artifacts left by OpenClaw execution.

Phase 22C-0 containment. OpenClaw writes its own per-workspace
agent-identity/onboarding scaffold (`SOUL.md`, `USER.md`, `TOOLS.md`,
`HEARTBEAT.md`, `IDENTITY.md`, `.openclaw/`) into whatever directory an
agent's configured workspace points at. Prior fixes suppressed this by adding
each observed filename to `HYDRATION_EXCLUDED_NAMES`
(`app/services/workspace/workspace_paths.py`) -- an ever-growing blacklist
that only hides files git/hydration already knows about and says nothing
about a scaffold rename or a new file OpenClaw has never written before.

This module adds a second, non-blacklist detector: a plain before/after diff
of the project root's top-level entries for each execution. Any new entry is
reported; entries matching the known scaffold name set are called out
specifically because they are unambiguous (never legitimate task output), but
detection itself does not depend on that list -- an unrecognized new
top-level artifact is still surfaced.

This module only detects and reports. It does not delete or modify anything
in the project workspace -- removing files from a directory Orchestrator does
not own is itself a boundary violation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Set

# Scaffold names observed in dogfood cycles 1-3. Used only to escalate/label
# an already-detected new entry -- never as the sole detection mechanism.
KNOWN_OPENCLAW_RUNTIME_SCAFFOLD_NAMES = frozenset(
    {
        "SOUL.md",
        "USER.md",
        "TOOLS.md",
        "HEARTBEAT.md",
        "IDENTITY.md",
        "BOOTSTRAP.md",
        ".openclaw",
    }
)


def snapshot_top_level_entries(root: Path) -> Set[str]:
    """Return the names of top-level entries in ``root``, or empty if absent."""

    try:
        if not root.exists() or not root.is_dir():
            return set()
        return {entry.name for entry in root.iterdir()}
    except OSError:
        return set()


def detect_runtime_pollution(*, before: Set[str], after: Set[str]) -> Dict[str, object]:
    """Diff two top-level snapshots and classify any new entries.

    Detection is diff-based (new relative to this specific execution), not
    blacklist-based. The known-scaffold list only labels which new entries
    are unambiguous OpenClaw bootstrap pollution versus unclassified new
    top-level artifacts that warrant investigation but may be legitimate
    task output.
    """

    new_entries: List[str] = sorted(after - before)
    known_scaffold_matches: List[str] = sorted(
        entry for entry in new_entries if entry in KNOWN_OPENCLAW_RUNTIME_SCAFFOLD_NAMES
    )
    unclassified_new_entries: List[str] = sorted(
        entry for entry in new_entries if entry not in known_scaffold_matches
    )

    return {
        "pollution_detected": bool(new_entries),
        "new_top_level_entries": new_entries,
        "known_scaffold_matches": known_scaffold_matches,
        "unclassified_new_entries": unclassified_new_entries,
    }


def existing_known_scaffold_entries(root: Path) -> List[str]:
    """Report which known OpenClaw scaffold names are present right now.

    Complements the diff-based detector above: scaffold files written by an
    earlier run persist on disk (hydration/.gitignore exclusion hides them
    from git and validators, it does not remove them), so a pure before/after
    diff on a single run will not re-surface pollution that already landed in
    an earlier run. This reports current presence regardless of when it was
    written.
    """

    present = snapshot_top_level_entries(root)
    return sorted(present & KNOWN_OPENCLAW_RUNTIME_SCAFFOLD_NAMES)
