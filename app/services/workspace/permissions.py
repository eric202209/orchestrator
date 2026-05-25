"""Shared workspace filesystem permission helpers."""

from __future__ import annotations

from pathlib import Path


def ensure_shared_permissions(path: Path) -> None:
    """Make workspace files/directories editable by host users and agents."""

    try:
        current_mode = path.stat().st_mode
        shared_bits = 0o777 if path.is_dir() else 0o666
        path.chmod(current_mode | shared_bits)
    except FileNotFoundError:
        return
    except OSError:
        return


def ensure_shared_tree(path: Path) -> None:
    ensure_shared_permissions(path)
    if not path.is_dir():
        return
    try:
        for child in path.rglob("*"):
            ensure_shared_permissions(child)
    except OSError:
        return


def ensure_shared_path_to_root(path: Path, root: Path) -> None:
    """Apply shared permissions to a path and its parents up to root."""

    try:
        current = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return

    while True:
        if current.is_relative_to(resolved_root):
            ensure_shared_permissions(current)
        if current == resolved_root:
            break
        if not current.is_relative_to(resolved_root):
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
