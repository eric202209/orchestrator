"""Helpers for presenting workspace paths consistently in prompts and UI text."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.services.workspace.system_settings import get_effective_workspace_root


def render_workspace_path_for_prompt(
    path: Optional[str | Path],
    *,
    db: Optional[Session] = None,
    preserve_external_paths: bool = False,
) -> str:
    """Render a path using the configured workspace root when possible.

    This keeps model-facing prompts aligned with the user-configured runtime
    root (for example `/root/.openclaw/workspace/...`) instead of leaking host
    paths like `/home/...`.
    """

    if not path:
        return "Current task workspace"

    raw_path = str(path).strip()
    normalized_raw = raw_path.replace("\\", "/")
    if normalized_raw.startswith("/"):
        workspace_root = str(get_effective_workspace_root(db=db)).replace("\\", "/")
        if normalized_raw == workspace_root or normalized_raw.startswith(
            f"{workspace_root}/"
        ):
            return normalized_raw
        if normalized_raw.startswith("/tmp/"):
            return normalized_raw
        if preserve_external_paths:
            return normalized_raw
        return normalized_raw.rstrip("/").split("/")[-1] or normalized_raw

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return str(candidate).replace("\\", "/")

    resolved = candidate.resolve()
    workspace_root = get_effective_workspace_root(db=db).resolve()

    try:
        relative = resolved.relative_to(workspace_root)
    except ValueError:
        normalized_resolved = str(resolved).replace("\\", "/")
        if normalized_resolved.startswith("/tmp/"):
            return normalized_resolved
        if (
            "/vault/projects/" in normalized_resolved
            or "/.openclaw/workspace/" in normalized_resolved
        ):
            return resolved.name or str(resolved)
        if preserve_external_paths:
            return str(resolved)
        return resolved.name or str(resolved)

    rendered = workspace_root / relative
    return str(rendered).replace("\\", "/")
