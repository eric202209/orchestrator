"""Secret path and cross-project path detection."""

from __future__ import annotations

import re
from typing import Any

_SECRET_PATH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:^|/)\.env(?:\.[^/]+)?$"),  # .env, .env.local, .env.production
    re.compile(r"(?:^|/)\.ssh/"),  # ~/.ssh/*
    re.compile(r"(?:^|/)\.aws/credentials"),  # AWS credentials file
    re.compile(r"(?:^|/)\.gitconfig$"),  # git global config
    re.compile(r"(?:^|/)id_rsa$"),  # RSA private key
    re.compile(r"(?:^|/)id_ed25519$"),  # Ed25519 private key
    re.compile(r"(?:^|/)id_ecdsa$"),  # ECDSA private key
    re.compile(r"(?:^|/)\.netrc$"),  # FTP/HTTP credentials
    re.compile(r"/etc/passwd$"),  # system passwd
    re.compile(r"/etc/shadow$"),  # system shadow
    re.compile(r"(?:^|/)\.pgpass$"),  # PostgreSQL credentials
]


def is_secret_path(path: str) -> bool:
    """Return True if the path matches a known credential or secret file pattern."""
    normalized = (path or "").replace("\\", "/")
    return any(p.search(normalized) for p in _SECRET_PATH_PATTERNS)


def check_ops_for_secret_paths(ops: list[dict[str, Any]]) -> list[str]:
    """Return paths from a structured file ops list that match secret patterns."""
    found: list[str] = []
    for op in ops or []:
        path = str(op.get("path") or "")
        if is_secret_path(path):
            found.append(path)
    return found
