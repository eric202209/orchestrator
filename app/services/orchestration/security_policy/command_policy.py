"""Shell command danger detection — shadow/audit mode."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandViolation:
    pattern_name: str
    matched_text: str
    risk_level: str  # "high" | "medium"


# Each entry: (name, compiled_regex, risk_level)
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "rm_recursive_root",
        re.compile(
            r"\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+/(?:\s|$)"
        ),
        "high",
    ),
    (
        "fork_bomb",
        re.compile(r":\(\)\s*\{\s*:\|:"),
        "high",
    ),
    (
        "curl_pipe_shell",
        re.compile(r"\bcurl\b[^|]*\|\s*(?:ba?sh|sh|zsh)\b"),
        "high",
    ),
    (
        "wget_pipe_shell",
        re.compile(r"\bwget\b[^|]*\|\s*(?:ba?sh|sh|zsh)\b"),
        "high",
    ),
    (
        "eval_subshell",
        re.compile(r'\beval\s+["$`(]'),
        "high",
    ),
    (
        "dd_destroy_disk",
        re.compile(r"\bdd\b.*\bof=/dev/(?:sd[a-z]|hd[a-z]|nvme)"),
        "high",
    ),
    (
        "mkfs",
        re.compile(r"\bmkfs\b"),
        "high",
    ),
    (
        "pip_install",
        re.compile(r"\bpip[23]?\s+install\b"),
        "medium",
    ),
    (
        "npm_install_global",
        re.compile(r"\bnpm\s+(?:install|i)\b.*(?:--global|-g)\b"),
        "medium",
    ),
    (
        "outbound_curl",
        re.compile(r"\bcurl\b\s+(?:https?|ftp)://"),
        "medium",
    ),
    (
        "outbound_wget",
        re.compile(r"\bwget\b\s+(?:https?|ftp)://"),
        "medium",
    ),
    (
        "chmod_world_writable",
        re.compile(r"\bchmod\s+(?:a\+rwx|0?777)\b"),
        "medium",
    ),
]

_HIGH_RISK_NAMES: frozenset[str] = frozenset(
    name for name, _, level in _PATTERNS if level == "high"
)


def check_command(command: str) -> list[CommandViolation]:
    """Return all violations found in a shell command string. Never raises."""
    if not command:
        return []
    violations: list[CommandViolation] = []
    for name, pattern, level in _PATTERNS:
        m = pattern.search(command)
        if m:
            violations.append(
                CommandViolation(
                    pattern_name=name,
                    matched_text=m.group(0),
                    risk_level=level,
                )
            )
    return violations


def is_high_risk(violations: list[CommandViolation]) -> bool:
    """True if any violation matches a high-risk pattern."""
    return any(v.pattern_name in _HIGH_RISK_NAMES for v in violations)
