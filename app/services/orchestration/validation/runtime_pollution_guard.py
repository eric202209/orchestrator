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

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Set

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


def _sha256_path(path: Path) -> str | None:
    """Hash a file or bounded directory tree for before/after evidence.

    Directory hashing is intentionally bounded. A project root can contain
    ``.git``, virtualenvs, or frontend dependencies; recursively hashing those
    trees on every provider invocation would itself become an operational
    defect. OpenClaw's scaffold directories are small and are hashed by
    content, while other directories use a deterministic immediate-entry
    summary.
    """

    try:
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        if path.is_dir():
            digest = hashlib.sha256()
            if path.name != ".openclaw":
                for child in sorted(path.iterdir()):
                    stat = child.stat()
                    digest.update(
                        f"{child.name}\0{child.is_dir()}\0{stat.st_size}\0"
                        f"{stat.st_mtime_ns}\n".encode("utf-8")
                    )
                return digest.hexdigest()
            file_count = 0
            byte_count = 0
            for child in sorted(path.rglob("*")):
                if not child.is_file():
                    continue
                if file_count >= 256 or byte_count >= 2 * 1024 * 1024:
                    digest.update(b"<bounded-directory-hash>")
                    break
                child_hash = _sha256_path(child)
                digest.update(str(child.relative_to(path)).encode("utf-8"))
                digest.update((child_hash or "").encode("ascii"))
                file_count += 1
                try:
                    byte_count += child.stat().st_size
                except OSError:
                    pass
            return digest.hexdigest()
    except OSError:
        return None
    return None


def _git_state(root: Path, relative_path: str) -> tuple[bool, bool]:
    """Return (ignored, tracked) without treating either as harmless."""

    try:
        ignored = (
            subprocess.run(
                ["git", "check-ignore", "--quiet", "--", relative_path],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            == 0
        )
        tracked = (
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", relative_path],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            == 0
        )
        return ignored, tracked
    except OSError:
        return False, False


def snapshot_workspace_entry_evidence(root: Path) -> Dict[str, Dict[str, Any]]:
    """Capture path/type/hash/git state for top-level runtime evidence."""

    evidence: Dict[str, Dict[str, Any]] = {}
    for entry in sorted(root.iterdir()) if root.exists() and root.is_dir() else []:
        relative = entry.name
        ignored, tracked = _git_state(root, relative)
        evidence[relative] = {
            "relative_path": relative,
            "path": str(entry),
            "file_type": "directory" if entry.is_dir() else "file",
            "sha256": _sha256_path(entry),
            "mtime_ns": entry.stat().st_mtime_ns,
            "ignored": ignored,
            "tracked": tracked,
        }
        if entry.name == ".openclaw" and entry.is_dir():
            state_file = entry / "workspace-state.json"
            if state_file.exists():
                nested = ".openclaw/workspace-state.json"
                nested_ignored, nested_tracked = _git_state(root, nested)
                evidence[nested] = {
                    "relative_path": nested,
                    "path": str(state_file),
                    "file_type": "file",
                    "sha256": _sha256_path(state_file),
                    "mtime_ns": state_file.stat().st_mtime_ns,
                    "ignored": nested_ignored,
                    "tracked": nested_tracked,
                }
    return evidence


def detect_runtime_pollution(
    *,
    before: Set[str] | Dict[str, Dict[str, Any]],
    after: Set[str] | Dict[str, Dict[str, Any]],
    canonical_root: Path | None = None,
    runtime_workspace: Path | None = None,
) -> Dict[str, object]:
    """Diff two top-level snapshots and classify any new entries.

    Detection is diff-based (new relative to this specific execution), not
    blacklist-based. The known-scaffold list only labels which new entries
    are unambiguous OpenClaw bootstrap pollution versus unclassified new
    top-level artifacts that warrant investigation but may be legitimate
    task output.
    """

    before_names = set(before)
    after_names = set(after)
    new_entries = set(after_names - before_names)
    if isinstance(before, dict) and isinstance(after, dict):
        changed_entries = {
            relative
            for relative in after_names & before_names
            if before[relative].get("sha256") != after[relative].get("sha256")
        }
        # A changed parent directory is redundant when a nested scaffold file
        # already identifies the precise provider-created boundary.
        changed_entries = {
            relative
            for relative in changed_entries
            if not any(
                other != relative and other.startswith(f"{relative}/")
                for other in changed_entries
            )
        }
        new_entries.update(changed_entries)
    new_entries = sorted(new_entries)
    known_scaffold_matches: List[str] = sorted(
        entry
        for entry in new_entries
        if entry in KNOWN_OPENCLAW_RUNTIME_SCAFFOLD_NAMES
        or entry.split("/", 1)[0] in KNOWN_OPENCLAW_RUNTIME_SCAFFOLD_NAMES
    )
    unclassified_new_entries: List[str] = sorted(
        entry for entry in new_entries if entry not in known_scaffold_matches
    )

    entries: List[Dict[str, Any]] = []
    execution_must_stop = False
    category = None
    canonical = canonical_root.resolve() if canonical_root else None
    runtime = runtime_workspace.resolve() if runtime_workspace else None
    for relative in new_entries:
        path = (
            (after[relative] or {}).get("path") if isinstance(after, dict) else None
        ) or (str(canonical / relative) if canonical else relative)
        path_obj = Path(path)
        if runtime and path_obj.resolve().is_relative_to(runtime):
            boundary = "runtime_workspace"
            location = "sandbox"
        elif canonical and path_obj.resolve().is_relative_to(canonical):
            boundary = "canonical_project_root"
            location = "canonical"
        else:
            boundary = "outside_declared_boundary"
            location = "outside"
        after_record = after.get(relative, {}) if isinstance(after, dict) else {}
        before_record = before.get(relative, {}) if isinstance(before, dict) else {}
        ignored = bool(after_record.get("ignored")) if after_record else False
        tracked = bool(after_record.get("tracked")) if after_record else False
        if canonical and boundary == "canonical_project_root":
            execution_must_stop = True
            if relative in known_scaffold_matches:
                category = "provider_scaffold_outside_runtime_workspace"
            elif category is None:
                category = "canonical_workspace_pollution_detected"
        elif boundary == "outside_declared_boundary":
            execution_must_stop = True
            category = category or "runtime_workspace_binding_mismatch"
        entries.append(
            {
                "path": str(path_obj),
                "relative_path": relative,
                "file_type": after_record.get("file_type")
                or ("directory" if path_obj.is_dir() else "file"),
                "creator_boundary": boundary,
                "canonical_or_sandbox": location,
                "before_sha256": before_record.get("sha256"),
                "after_sha256": after_record.get("sha256") or _sha256_path(path_obj),
                "before_mtime_ns": before_record.get("mtime_ns"),
                "after_mtime_ns": after_record.get("mtime_ns"),
                "ignored": ignored,
                "tracked": tracked,
                "cleanup_safe": boundary == "runtime_workspace",
                "execution_must_stop": boundary != "runtime_workspace",
            }
        )

    return {
        "pollution_detected": bool(new_entries),
        "new_top_level_entries": new_entries,
        "known_scaffold_matches": known_scaffold_matches,
        "unclassified_new_entries": unclassified_new_entries,
        "category": category,
        "entries": entries,
        "execution_must_stop": execution_must_stop,
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
