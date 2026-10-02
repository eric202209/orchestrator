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
import re
import subprocess
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Set

RUNTIME_POLLUTION_SCHEMA_VERSION = "runtime_pollution_provenance.v1"
GIT_TOPOLOGY_SCHEMA_VERSION = "git_topology.v1"
RUNTIME_POLLUTION_EVIDENCE_CAP = 64

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

# These are Orchestrator's own runtime files when the Orchestrator repository
# is itself the admitted Project Baseline. They are not provider artifacts:
# start.sh owns logs/, while the backend/Celery processes own SQLite's WAL and
# shared-memory sidecars. Keep the exception structural and exact so unknown
# provider-created entries and OpenClaw scaffold names remain fail-closed.
ORCHESTRATOR_RUNTIME_STATE_NAMES = frozenset(
    {"logs", "orchestrator.db-wal", "orchestrator.db-shm"}
)


def _is_orchestrator_project_root(root: Path | None) -> bool:
    if root is None:
        return False
    try:
        return (
            (root / "app" / "main.py").is_file()
            and (root / "app" / "celery_app.py").is_file()
            and (root / "orchestrator.db").is_file()
        )
    except OSError:
        return False


def _expected_orchestrator_runtime_entries(
    *,
    canonical_root: Path | None,
    new_entries: list[str],
    after: Set[str] | Dict[str, Dict[str, Any]],
) -> list[str]:
    """Return exact host-owned entries, never provider-created repo paths."""

    if not _is_orchestrator_project_root(canonical_root):
        return []
    expected: list[str] = []
    for relative in new_entries:
        if relative not in ORCHESTRATOR_RUNTIME_STATE_NAMES:
            continue
        record = after.get(relative, {}) if isinstance(after, dict) else {}
        path = Path(record.get("path") or relative)
        if (
            canonical_root is not None
            and path.resolve().parent != canonical_root.resolve()
        ):
            continue
        expected.append(relative)
    return sorted(expected)


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


def _resolve_git_path(root: Path, value: str) -> str:
    path = Path(value.strip())
    return str(path if path.is_absolute() else (root / path).resolve())


def _git_admin_inventory(
    *, common_dir: Path, worktree_git_dir: Path | None
) -> Dict[str, Dict[str, Any]]:
    """Capture bounded, deterministic Git-admin metadata, excluding objects."""

    inventory: Dict[str, Dict[str, Any]] = {}
    relevant_roots = {
        "HEAD",
        "ORIG_HEAD",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "config",
        "index",
        "packed-refs",
        "refs",
        "logs",
        "worktrees",
    }
    worktree_roots = {"HEAD", "index", "commondir", "gitdir", "logs"}
    candidates: list[tuple[Path, str]] = []
    for child in sorted(common_dir.iterdir()) if common_dir.is_dir() else []:
        if child.name in relevant_roots:
            candidates.append((child, "shared_common"))
    if worktree_git_dir and worktree_git_dir.is_dir():
        for child in sorted(worktree_git_dir.iterdir()):
            if child.name in worktree_roots:
                candidates.append((child, "worktree_specific"))

    seen: set[str] = set()
    for base, root_scope in candidates:
        paths = [base]
        if base.is_dir():
            paths.extend(sorted(base.rglob("*")))
        for path in paths:
            if not path.is_file() or str(path) in seen:
                continue
            try:
                relative = path.relative_to(common_dir)
            except ValueError:
                relative = path.name
            if relative.parts and relative.parts[0] == "objects":
                continue
            try:
                stat = path.stat()
                content_sha = _sha256_path(path)
            except OSError:
                continue
            scope = root_scope
            if worktree_git_dir:
                try:
                    if path.resolve().is_relative_to(worktree_git_dir.resolve()):
                        scope = "worktree_specific"
                except (OSError, ValueError):
                    pass
            if root_scope == "shared_common" and relative.parts[:1] == ("worktrees",):
                scope = "linked_worktree_admin"
            inventory[str(path)] = {
                "path": str(path),
                "relative_path": str(relative),
                "scope": scope,
                "sha256": content_sha,
                "mtime_ns": stat.st_mtime_ns,
            }
            seen.add(str(path))
            if len(inventory) >= 256:
                return inventory
    return inventory


def build_git_topology(root: Path) -> Dict[str, Any] | None:
    """Resolve linked-worktree Git paths and bounded admin metadata."""

    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return None
    values: Dict[str, str] = {}
    for key, args in (
        ("git_dir", ["rev-parse", "--git-dir"]),
        ("git_common_dir", ["rev-parse", "--git-common-dir"]),
        ("show_toplevel", ["rev-parse", "--show-toplevel"]),
    ):
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except OSError:
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        values[key] = _resolve_git_path(root, result.stdout)

    git_path = root / ".git"
    git_dir = Path(values["git_dir"])
    common_dir = Path(values["git_common_dir"])
    worktree_git_dir = git_dir if git_dir != common_dir else None
    try:
        git_path_type = (
            "file"
            if git_path.is_file()
            else "directory" if git_path.is_dir() else "missing"
        )
        indirection = (
            git_path.read_text(encoding="utf-8").strip()[:512]
            if git_path.is_file()
            else None
        )
    except OSError:
        git_path_type = "unreadable"
        indirection = None
    return {
        "schema_version": GIT_TOPOLOGY_SCHEMA_VERSION,
        "root": str(root),
        "git_path": str(git_path),
        "git_path_type": git_path_type,
        "git_indirection": indirection,
        "git_dir": str(git_dir),
        "git_common_dir": str(common_dir),
        "worktree_git_dir": str(worktree_git_dir) if worktree_git_dir else None,
        "shared_common_dir": str(common_dir),
        "linked_worktree": worktree_git_dir is not None,
        "git_dir_digest": _sha256_path(git_dir),
        "git_common_dir_digest": _sha256_path(common_dir),
        "admin_inventory": _git_admin_inventory(
            common_dir=common_dir, worktree_git_dir=worktree_git_dir
        ),
    }


def _topology_summary(topology: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not topology:
        return None
    return {
        key: topology.get(key)
        for key in (
            "schema_version",
            "root",
            "git_path",
            "git_path_type",
            "git_indirection",
            "git_dir",
            "git_common_dir",
            "worktree_git_dir",
            "shared_common_dir",
            "linked_worktree",
        )
    }


def _git_topology_changed(
    before_topology: Dict[str, Any] | None,
    after_topology: Dict[str, Any] | None,
) -> bool:
    """Detect Git-admin changes while ignoring harmless worktree mtimes."""

    before_topology = before_topology or {}
    after_topology = after_topology or {}
    for key in (
        "git_path_type",
        "git_indirection",
        "git_dir",
        "git_common_dir",
        "worktree_git_dir",
        "shared_common_dir",
        "linked_worktree",
    ):
        if before_topology.get(key) != after_topology.get(key):
            return True
    before_inventory = before_topology.get("admin_inventory") or {}
    after_inventory = after_topology.get("admin_inventory") or {}
    for path in set(before_inventory) | set(after_inventory):
        before = before_inventory.get(path) or {}
        after = after_inventory.get(path) or {}
        if before.get("sha256") != after.get("sha256") or bool(before) != bool(after):
            return True
        scope = after.get("scope") or before.get("scope")
        if scope not in {"worktree_specific", "linked_worktree_admin"} and before.get(
            "mtime_ns"
        ) != after.get("mtime_ns"):
            return True
    return False


def _git_admin_changes(
    before_record: Dict[str, Any],
    after_record: Dict[str, Any],
    *,
    runtime_topology: Dict[str, Any] | None,
) -> List[Dict[str, Any]]:
    before_topology = before_record.get("git_topology") or {}
    after_topology = after_record.get("git_topology") or {}
    before_inventory = before_topology.get("admin_inventory") or {}
    after_inventory = after_topology.get("admin_inventory") or {}
    changes: List[Dict[str, Any]] = []
    for path in sorted(set(before_inventory) | set(after_inventory)):
        before = before_inventory.get(path) or {}
        after = after_inventory.get(path) or {}
        if (
            before.get("sha256") == after.get("sha256")
            and before.get("mtime_ns") == after.get("mtime_ns")
            and bool(before) == bool(after)
        ):
            continue
        scope = after.get("scope") or before.get("scope") or "shared_common"
        if scope == "linked_worktree_admin" and runtime_topology:
            runtime_git_dir = runtime_topology.get("worktree_git_dir")
            if runtime_git_dir:
                try:
                    if (
                        Path(path)
                        .resolve()
                        .is_relative_to(Path(runtime_git_dir).resolve())
                    ):
                        scope = "worktree_specific"
                except (OSError, ValueError):
                    pass
        changes.append(
            {
                "path": path,
                "relative_path": after.get("relative_path")
                or before.get("relative_path"),
                "scope": scope,
                "before_sha256": before.get("sha256"),
                "after_sha256": after.get("sha256"),
                "before_mtime_ns": before.get("mtime_ns"),
                "after_mtime_ns": after.get("mtime_ns"),
                "content_changed": before.get("sha256") != after.get("sha256"),
            }
        )
    return changes[:256]


def classify_git_admin_changes(
    changes: List[Dict[str, Any]], *, phase: str | None = None
) -> str | None:
    """Classify Git-admin changes without changing the containment decision."""

    if not changes:
        return None
    scopes = {str(change.get("scope") or "shared_common") for change in changes}
    if scopes <= {"worktree_specific"}:
        return "SG-A"
    expected_shared = all(
        str(change.get("relative_path") or "")
        .replace("\\", "/")
        .startswith(
            (
                "worktrees/",
                "refs/heads/orchestrator/task-",
                "logs/refs/heads/orchestrator/task-",
            )
        )
        and (
            str(change.get("relative_path") or "").split("/")[-1]
            in {"HEAD", "ORIG_HEAD", "commondir", "gitdir", "index"}
            or bool(
                re.search(
                    r"(?:^|/)orchestrator/task-\d+$",
                    str(change.get("relative_path") or "").replace("\\", "/"),
                )
            )
        )
        for change in changes
        if str(change.get("scope") or "shared_common") != "worktree_specific"
    )
    if phase in {"allocator", "lifecycle", "disposal"} and expected_shared:
        return "SG-B"
    dangerous = any(
        change.get("content_changed")
        and str(change.get("relative_path") or "").replace("\\", "/").split("/", 1)[0]
        in {"HEAD", "config", "index", "packed-refs", "refs", "logs"}
        for change in changes
    )
    return "SG-D" if dangerous else "SG-C"


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
    git_topology = build_git_topology(root)
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
        if entry.name == ".git" and git_topology:
            evidence[relative]["git_topology"] = git_topology
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
    phase: str | None = None,
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
            if (
                before[relative].get("sha256") != after[relative].get("sha256")
                or _git_topology_changed(
                    before[relative].get("git_topology"),
                    after[relative].get("git_topology"),
                )
            )
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
    canonical = canonical_root.resolve() if canonical_root else None
    expected_orchestrator_runtime_entries = _expected_orchestrator_runtime_entries(
        canonical_root=canonical,
        new_entries=new_entries,
        after=after,
    )
    new_entries = [
        entry
        for entry in new_entries
        if entry not in expected_orchestrator_runtime_entries
    ]
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
    runtime = runtime_workspace.resolve() if runtime_workspace else None
    runtime_topology = build_git_topology(runtime) if runtime else None
    shared_git_classifications: List[str] = []
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
        git_changes = _git_admin_changes(
            before_record, after_record, runtime_topology=runtime_topology
        )
        git_classification = classify_git_admin_changes(git_changes, phase=phase)
        if (
            not git_classification
            and relative == ".git"
            and boundary == "canonical_project_root"
        ):
            git_classification = "SG-D"
        if git_classification:
            shared_git_classifications.append(git_classification)
        is_git_admin = relative == ".git" or relative.startswith(".git/")
        if git_classification:
            safety_category = git_classification
        elif boundary == "canonical_project_root":
            safety_category = "SG-E"
        elif boundary == "runtime_workspace":
            safety_category = "runtime_workspace_source"
        else:
            safety_category = "runtime_workspace_binding_mismatch"
        if relative in known_scaffold_matches:
            scaffold_classification = "known_openclaw_scaffold"
        elif relative in expected_orchestrator_runtime_entries:
            scaffold_classification = "orchestrator_runtime_noise"
        else:
            scaffold_classification = "none"
        entries.append(
            {
                "schema_version": RUNTIME_POLLUTION_SCHEMA_VERSION,
                "path": str(path_obj),
                "absolute_path": str(path_obj),
                "relative_path": relative,
                "file_type": after_record.get("file_type")
                or ("directory" if path_obj.is_dir() else "file"),
                "entry_type": after_record.get("file_type")
                or ("directory" if path_obj.is_dir() else "file"),
                "creator_boundary": boundary,
                "boundary": boundary,
                "canonical_or_sandbox": location,
                "before_exists": bool(before_record),
                "after_exists": bool(after_record),
                "before_state": "present" if before_record else "absent",
                "after_state": "present" if after_record else "absent",
                "before_sha256": before_record.get("sha256"),
                "after_sha256": after_record.get("sha256") or _sha256_path(path_obj),
                "before_mtime_ns": before_record.get("mtime_ns"),
                "after_mtime_ns": after_record.get("mtime_ns"),
                "before_ignored": before_record.get("ignored"),
                "after_ignored": after_record.get("ignored"),
                "before_tracked": before_record.get("tracked"),
                "after_tracked": after_record.get("tracked"),
                "ignored": ignored,
                "tracked": tracked,
                "scaffold_classification": scaffold_classification,
                "noise_classification": (
                    scaffold_classification
                    if scaffold_classification != "none"
                    else "not_noise"
                ),
                "safety_category": safety_category,
                "is_git_admin": is_git_admin,
                "git_admin_classification": git_classification,
                "git_admin_changes": git_changes,
                "git_topology": _topology_summary(
                    after_record.get("git_topology")
                    or before_record.get("git_topology")
                ),
                "cleanup_safe": boundary == "runtime_workspace",
                "execution_must_stop": boundary != "runtime_workspace",
            }
        )

    return {
        "pollution_detected": bool(new_entries),
        "new_top_level_entries": new_entries,
        "known_scaffold_matches": known_scaffold_matches,
        "unclassified_new_entries": unclassified_new_entries,
        "expected_orchestrator_runtime_entries": expected_orchestrator_runtime_entries,
        "category": category,
        "entries": entries,
        "entry_count": len(entries),
        "boundary_counts": {
            boundary: sum(
                1 for entry in entries if entry.get("creator_boundary") == boundary
            )
            for boundary in {entry.get("creator_boundary") for entry in entries}
            if boundary
        },
        "shared_git_classifications": sorted(set(shared_git_classifications)),
        "execution_must_stop": execution_must_stop,
    }


def bound_runtime_pollution_evidence(
    pollution: Dict[str, Any],
    *,
    project_id: int | None = None,
    session_id: int | None = None,
    task_id: int | None = None,
    task_execution_id: int | None = None,
    execution_step: int | None = None,
    phase: str = "provider_initialization",
    timestamp: str | None = None,
    canonical_root: Path | str | None = None,
    runtime_root: Path | str | None = None,
    phase_timestamps: Dict[str, Any] | None = None,
    process_provenance: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Create a bounded durable provenance record without changing stop policy."""

    result = deepcopy(pollution or {})
    all_entries = list(result.get("entries") or [])
    required = [
        entry
        for entry in all_entries
        if entry.get("execution_must_stop")
        or entry.get("safety_category") in {"SG-D", "SG-E"}
    ]
    optional = [entry for entry in all_entries if entry not in required]
    required = sorted(required, key=lambda entry: str(entry.get("relative_path") or ""))
    optional = sorted(optional, key=lambda entry: str(entry.get("relative_path") or ""))
    retained = (
        required + optional[: max(0, RUNTIME_POLLUTION_EVIDENCE_CAP - len(required))]
    )
    retained = sorted(retained, key=lambda entry: str(entry.get("relative_path") or ""))
    truncated = len(retained) < len(all_entries)
    identity = {
        "schema_version": RUNTIME_POLLUTION_SCHEMA_VERSION,
        "project_id": project_id,
        "session_id": session_id,
        "task_id": task_id,
        "task_execution_id": task_execution_id,
        "execution_step": execution_step,
        "phase": phase,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "canonical_root": str(canonical_root) if canonical_root is not None else None,
        "runtime_root": str(runtime_root) if runtime_root is not None else None,
        "phase_timestamps": dict(phase_timestamps or {}),
        "process_provenance": list(process_provenance or [])[:16],
    }
    for entry in retained:
        for key, value in identity.items():
            if key in {"phase_timestamps", "process_provenance"}:
                continue
            entry.setdefault(key, value)
    result.update(identity)
    result["entries"] = retained
    result["new_top_level_entries"] = [
        entry.get("relative_path") for entry in retained if entry.get("relative_path")
    ]
    for nested_key in ("canonical_pollution", "runtime_pollution"):
        nested = result.get(nested_key)
        if isinstance(nested, dict):
            result[nested_key] = {
                "pollution_detected": bool(nested.get("pollution_detected")),
                "entry_count": int(
                    nested.get("entry_count") or len(nested.get("entries") or [])
                ),
                "execution_must_stop": bool(nested.get("execution_must_stop")),
                "category": nested.get("category"),
            }
    result["entry_count"] = len(all_entries)
    result["retained_entry_count"] = len(retained)
    result["evidence_cap"] = RUNTIME_POLLUTION_EVIDENCE_CAP
    result["evidence_truncated"] = truncated
    result.setdefault("git_topology", None)
    result["truncation_policy"] = (
        "retain_all_execution_stopping_entries_then_sorted_relative_paths"
    )
    return result


def build_runtime_pollution_provenance(
    pollution: Dict[str, Any], **kwargs: Any
) -> Dict[str, Any]:
    """Named schema builder used by runtime persistence and focused tests."""

    return bound_runtime_pollution_evidence(pollution, **kwargs)


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
