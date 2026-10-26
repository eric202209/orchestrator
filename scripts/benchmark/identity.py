"""Exact, non-secret identity for live benchmark campaigns.

Source identity hashes the files the development API and Worker load (``app/``
without its tests, ``requirements.txt``, and the workflow templates the app
reads from ``docs/``), not just HEAD. Runtime identity is read from the owned
processes' ``/proc`` entries and projected without secret values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SOURCE_IDENTITY_SCHEMA = "benchmark.source_identity.v1"
EVALUATED_INCLUDE = ("app/", "requirements.txt", "docs/workflow-templates/")
EVALUATED_EXCLUDE = ("app/tests/",)
BENCHMARK_FILES = ("scripts/benchmark/", "scripts/dev_runtime.py")
SECRET_NAME = re.compile(r"SECRET|PASSWORD|TOKEN|API_KEY|PRIVATE|CREDENTIAL", re.I)
# Process-specific values that differ between the API and the Worker or between
# restarts without changing what the evaluated system is configured to do.
VOLATILE_ENV = frozenset({"SECRET_KEY"})


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.decode()}")
    return result.stdout


def _evaluated(path: str) -> bool:
    return path.startswith(EVALUATED_INCLUDE) and not path.startswith(EVALUATED_EXCLUDE)


def _file_inventory(repo_root: Path, prefixes: tuple[str, ...]) -> list[str]:
    """Tracked plus untracked, non-ignored files, and git-ignored template files."""

    listed = _git(
        repo_root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    paths = {item for item in listed.decode().split("\0") if item}
    templates = repo_root / "docs" / "workflow-templates"
    if "docs/workflow-templates/" in prefixes and templates.is_dir():
        paths.update(
            str(path.relative_to(repo_root))
            for path in templates.rglob("*")
            if path.is_file()
        )
    return sorted(
        path
        for path in paths
        if path.startswith(prefixes)
        and "__pycache__" not in path
        and (repo_root / path).is_file()
    )


def source_identity(repo_root: Path) -> dict[str, Any]:
    """Hash the evaluated application source exactly as it exists on disk."""

    repo_root = repo_root.resolve()
    files = {
        path: _sha256_bytes((repo_root / path).read_bytes())
        for path in _file_inventory(repo_root, EVALUATED_INCLUDE)
        if _evaluated(path)
    }
    tracked_diff = _git(
        repo_root,
        "diff",
        "--binary",
        "HEAD",
        "--",
        "app",
        "requirements.txt",
        ":(exclude)app/tests",
    )
    staged_diff = _git(repo_root, "diff", "--binary", "--cached")
    untracked = sorted(
        item
        for item in _git(repo_root, "ls-files", "-z", "--others", "--exclude-standard")
        .decode()
        .split("\0")
        if item and _evaluated(item)
    )
    changed = sorted(
        item
        for item in _git(repo_root, "diff", "--name-only", "-z", "HEAD")
        .decode()
        .split("\0")
        if item and _evaluated(item)
    )
    snapshot = _sha256_bytes(
        json.dumps(sorted(files.items()), separators=(",", ":")).encode()
    )
    clean = not changed and not untracked
    return {
        "schema": SOURCE_IDENTITY_SCHEMA,
        "entering_head": _git(repo_root, "rev-parse", "HEAD").decode().strip(),
        "branch": _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").decode().strip(),
        "worktree_state": (
            f"evaluated-clean:{snapshot[:16]}"
            if clean
            else f"evaluated-dirty:{snapshot[:16]}"
        ),
        "committed_as": None,
        "evaluated_scope": {
            "include": list(EVALUATED_INCLUDE),
            "exclude": list(EVALUATED_EXCLUDE),
        },
        "evaluated_changed_vs_head": changed,
        "evaluated_untracked": untracked,
        "tracked_diff_sha256": _sha256_bytes(tracked_diff),
        "staged_diff_sha256": _sha256_bytes(staged_diff),
        "staged_diff_empty": not staged_diff,
        "file_count": len(files),
        "snapshot_sha256": snapshot,
        "latest_mtime_epoch": max(
            ((repo_root / path).stat().st_mtime for path in files), default=None
        ),
        "files": files,
    }


def source_mismatch(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Return the evaluated paths (or identity fields) that differ."""

    problems = [
        field
        for field in ("entering_head", "snapshot_sha256")
        if expected.get(field) != actual.get(field)
    ]
    before, after = expected.get("files") or {}, actual.get("files") or {}
    problems.extend(
        f"file:{path}"
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    )
    return problems


def benchmark_identity(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    files = {
        path: _sha256_bytes((repo_root / path).read_bytes())
        for path in _file_inventory(repo_root, BENCHMARK_FILES)
    }
    return {
        "files": files,
        "sha256": _sha256_bytes(
            json.dumps(sorted(files.items()), separators=(",", ":")).encode()
        ),
    }


def dependency_identity() -> dict[str, Any]:
    frozen = subprocess.run(
        [sys.executable, "-m", "pip", "freeze", "--all"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return {
        "python": sys.version.split()[0],
        "executable": str(Path(sys.executable).absolute()),
        "pip_freeze_sha256": (
            _sha256_bytes(frozen.stdout.encode()) if frozen.returncode == 0 else None
        ),
    }


def redact_environment(env: dict[str, str]) -> dict[str, str]:
    """Deterministic projection that never carries a secret value."""

    projected = {}
    for name in sorted(env):
        value = env[name]
        if SECRET_NAME.search(name):
            projected[name] = "<redacted:set>" if value else "<empty>"
        else:
            projected[name] = value
    return projected


def configuration_fingerprint(projection: dict[str, str]) -> str:
    stable = {
        name: value for name, value in projection.items() if name not in VOLATILE_ENV
    }
    return _sha256_bytes(json.dumps(stable, sort_keys=True).encode())


def read_process_environment(pid: int) -> dict[str, str]:
    """Full environment of an owned process, for in-memory checks only."""

    raw = Path(f"/proc/{pid}/environ").read_bytes()
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            name, value = item.split(b"=", 1)
            env[name.decode(errors="replace")] = value.decode(errors="replace")
    return env


def read_process_secret(pid: int, name: str) -> str:
    """Return one secret from an owned process's environment; never persisted."""

    return read_process_environment(pid).get(name, "")


def process_identity(pid: int) -> dict[str, Any]:
    """Observable identity of a running process, with its environment redacted."""

    proc = Path(f"/proc/{pid}")
    stat = (proc / "stat").read_text()
    fields = stat[stat.rfind(")") + 2 :].split()
    start_ticks = int(fields[19])
    boot = next(
        float(line.split()[1])
        for line in Path("/proc/stat").read_text().splitlines()
        if line.startswith("btime ")
    )
    projection = redact_environment(read_process_environment(pid))
    return {
        "pid": pid,
        "start_ticks": start_ticks,
        "started_at_epoch": round(boot + start_ticks / os.sysconf("SC_CLK_TCK"), 2),
        "cmdline": [
            part.decode(errors="replace")
            for part in (proc / "cmdline").read_bytes().split(b"\0")
            if part
        ],
        "exe": os.readlink(proc / "exe"),
        "cwd": os.readlink(proc / "cwd"),
        "environment": projection,
        "configuration_fingerprint": configuration_fingerprint(projection),
    }
