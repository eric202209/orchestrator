"""RepoMemory: stable per-project structure facts, population-only (no injection).

Populated after successful task completion alongside WorkingMemory.
No LLM calls, no embeddings, no external network.
Deterministic filesystem/config inspection only.

No prompt injection occurs. REPO_MEMORY_INJECTION_ENABLED is not yet defined.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_FILENAME = "repo_memory.json"
_ENTRY_POINTS_MAX = 5

_HASHABLE_CONFIG_FILES = [
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "pytest.ini",
    "setup.py",
    "Makefile",
]

_ENTRY_POINT_NAMES = frozenset(
    {
        "main.py",
        "manage.py",
        "app.py",
        "setup.py",
        "pyproject.toml",
        "package.json",
        "index.js",
        "index.ts",
    }
)

_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".agent",
        ".pytest_cache",
        "__pycache__",
        "dist",
        "node_modules",
        "venv",
    }
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass
class RepoMemory:
    schema_version: int
    project_dir: str
    last_updated: str
    invalidation_hashes: Dict[str, Optional[str]]
    project_type: Optional[str]  # python | node | mixed | None
    package_manager: Optional[str]  # pip | poetry | pipenv | npm | yarn | None
    source_root: Optional[str]
    test_root: Optional[str]
    test_command: Optional[str]
    build_command: Optional[str]
    entry_points: List[str] = field(default_factory=list)
    known_config_files: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> Optional[str]:
    """SHA256 hex digest of file content, or None if absent."""
    try:
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        pass
    return None


def _compute_hashes(project_dir: Path) -> Dict[str, Optional[str]]:
    return {name: _hash_file(project_dir / name) for name in _HASHABLE_CONFIG_FILES}


def _current_known_config_files(project_dir: Path) -> List[str]:
    return [name for name in _HASHABLE_CONFIG_FILES if (project_dir / name).is_file()]


def _detect_project_type(project_dir: Path) -> Optional[str]:
    python = any(
        (project_dir / f).exists()
        for f in ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile")
    )
    node = (project_dir / "package.json").exists()
    if python and node:
        return "mixed"
    if python:
        return "python"
    if node:
        return "node"
    return None


def _detect_package_manager(project_dir: Path) -> Optional[str]:
    # Strongest signals first.
    if (project_dir / "poetry.lock").exists():
        return "poetry"
    if (project_dir / "Pipfile").exists():
        return "pipenv"
    if (project_dir / "yarn.lock").exists():
        return "yarn"
    if (project_dir / "package-lock.json").exists():
        return "npm"
    if (project_dir / "requirements.txt").exists():
        return "pip"
    if (project_dir / "package.json").exists():
        return "npm"
    # pyproject.toml without a lock file — check for poetry section.
    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            if "[tool.poetry]" in pyproject.read_text(
                encoding="utf-8", errors="ignore"
            ):
                return "poetry"
        except Exception:
            pass
        return "pip"
    return None


def _detect_source_root(project_dir: Path) -> Optional[str]:
    for candidate in ("app", "src", "lib"):
        if (project_dir / candidate).is_dir():
            return candidate + "/"
    return None


def _detect_test_root(project_dir: Path) -> Optional[str]:
    for candidate in ("app/tests", "tests", "test", "spec"):
        if (project_dir / candidate).is_dir():
            return candidate + "/"
    return None


def _detect_test_command(project_dir: Path) -> Optional[str]:
    # pytest indicators
    if (project_dir / "pytest.ini").exists():
        return "pytest"
    if (project_dir / "conftest.py").exists():
        return "pytest"
    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            if "[tool.pytest" in pyproject.read_text(encoding="utf-8", errors="ignore"):
                return "pytest"
        except Exception:
            pass
    if any(
        (project_dir / f).exists() for f in ("requirements.txt", "setup.py", "Pipfile")
    ):
        return "pytest"
    # Node: read package.json scripts section.
    package_json = project_dir / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            if "test" in (data.get("scripts") or {}):
                if (project_dir / "yarn.lock").exists():
                    return "yarn test"
                return "npm test"
        except Exception:
            pass
    return None


def _detect_build_command(project_dir: Path) -> Optional[str]:
    makefile = project_dir / "Makefile"
    if not makefile.exists():
        return None
    try:
        content = makefile.read_text(encoding="utf-8", errors="ignore")
        if "\nbuild:" in content or content.startswith("build:"):
            return "make build"
        if "\nall:" in content or content.startswith("all:"):
            return "make"
    except Exception:
        pass
    return None


def _detect_entry_points(project_dir: Path) -> List[str]:
    found: List[str] = []
    for path in sorted(project_dir.rglob("*")):
        if len(found) >= _ENTRY_POINTS_MAX:
            break
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(project_dir)
        except ValueError:
            continue
        if any(
            part in _EXCLUDE_DIRS
            or part.startswith(".")
            or part.endswith(("-venv", "_venv"))
            for part in relative.parts
        ):
            continue
        if path.name in _ENTRY_POINT_NAMES:
            found.append(str(relative))
    return found[:_ENTRY_POINTS_MAX]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_repo_memory(project_dir: Any) -> RepoMemory:
    """Build a fresh RepoMemory snapshot from the current project directory.

    Deterministic. No LLM calls, no embeddings, no network.
    """
    p = Path(str(project_dir))
    return RepoMemory(
        schema_version=SCHEMA_VERSION,
        project_dir=str(p),
        last_updated=datetime.now(UTC).isoformat(),
        invalidation_hashes=_compute_hashes(p),
        project_type=_detect_project_type(p),
        package_manager=_detect_package_manager(p),
        source_root=_detect_source_root(p),
        test_root=_detect_test_root(p),
        test_command=_detect_test_command(p),
        build_command=_detect_build_command(p),
        entry_points=_detect_entry_points(p),
        known_config_files=_current_known_config_files(p),
    )


def load_repo_memory(project_dir: Any) -> Optional[RepoMemory]:
    """Load repo_memory.json and validate hashes. Returns None if stale or absent."""
    p = Path(str(project_dir))
    path = p / ".agent" / _FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None

    stored_hashes = data.get("invalidation_hashes")
    if not isinstance(stored_hashes, dict):
        return None
    if _compute_hashes(p) != stored_hashes:
        return None

    stored_known = set(data.get("known_config_files") or [])
    if set(_current_known_config_files(p)) != stored_known:
        return None

    try:
        return RepoMemory(
            schema_version=data["schema_version"],
            project_dir=data.get("project_dir", str(p)),
            last_updated=data.get("last_updated", ""),
            invalidation_hashes=stored_hashes,
            project_type=data.get("project_type"),
            package_manager=data.get("package_manager"),
            source_root=data.get("source_root"),
            test_root=data.get("test_root"),
            test_command=data.get("test_command"),
            build_command=data.get("build_command"),
            entry_points=list(data.get("entry_points") or []),
            known_config_files=list(data.get("known_config_files") or []),
        )
    except Exception:
        return None


def write_repo_memory(project_dir: Any, _logger: Any = None) -> Optional[RepoMemory]:
    """Build and atomically persist repo_memory.json under .agent/.

    Safe no-op if project_dir is missing or None.
    Never raises into the caller — logs warnings on failure.
    Replaces a corrupt existing file transparently.
    """
    log = _logger or logger
    try:
        if not project_dir:
            return None
        p = Path(str(project_dir))
        if not p.exists():
            return None
        openclaw_dir = p / ".agent"
        openclaw_dir.mkdir(parents=True, exist_ok=True)
        rm = build_repo_memory(p)
        data = asdict(rm)
        dest = openclaw_dir / _FILENAME
        # Atomic write: write to a temp file in the same directory, then rename.
        fd, tmp_path_str = tempfile.mkstemp(dir=str(openclaw_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp_path_str, str(dest))
        except Exception:
            try:
                os.unlink(tmp_path_str)
            except Exception:
                pass
            raise
        log.info("[REPO_MEMORY] Written to %s", dest)
        return rm
    except Exception as exc:
        log.warning("[REPO_MEMORY] Failed to write repo memory: %s", exc)
        return None
