"""Verification command helpers for planning-phase step contracts."""

from __future__ import annotations

import json
import shlex
from typing import Any

from app.services.orchestration.validation.validator import ValidatorService


def _python_exists_verification_command(paths: list[str]) -> str:
    encoded_paths = json.dumps(paths)
    script = (
        "import pathlib,sys; "
        f"files={encoded_paths}; "
        "sys.exit(0 if all(pathlib.Path(p).exists() for p in files) else 1)"
    )
    return "python -c " + json.dumps(script)


def _python_file_contains_verification_command(path: str, needle: str) -> str:
    script = (
        "import pathlib,sys; "
        f"content=pathlib.Path({json.dumps(path)}).read_text(); "
        f"sys.exit(0 if {json.dumps(needle)} in content else 1)"
    )
    return "python -c " + json.dumps(script)


def _grep_quiet_verification_target(command: str) -> tuple[str, str] | None:
    try:
        tokens = shlex.split(str(command or ""), posix=True)
    except ValueError:
        return None
    if len(tokens) < 4 or tokens[0] != "grep" or "-q" not in tokens:
        return None
    quiet_index = tokens.index("-q")
    if quiet_index + 2 >= len(tokens):
        return None
    needle = tokens[quiet_index + 1]
    path = tokens[quiet_index + 2]
    if not needle or not path or path.startswith("-"):
        return None
    return path.lstrip("./"), needle


def _commands_are_weak_expected_file_verification(commands: Any) -> bool:
    if not isinstance(commands, list) or not commands:
        return False
    normalized_commands = [
        str(command or "").strip() for command in commands if str(command or "").strip()
    ]
    if len(normalized_commands) != len(commands):
        return False
    return all(
        ValidatorService._verification_is_weak(command)
        or " ".join(command.split()).startswith(("grep ", "test "))
        for command in normalized_commands
    )


def _strengthen_weak_expected_file_verifications(
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    strengthened: list[dict[str, Any]] = []
    for step in plan:
        updated = dict(step)
        expected_files = [
            str(path or "").strip().lstrip("./")
            for path in (updated.get("expected_files") or [])
            if str(path or "").strip()
        ]
        grep_target = None
        if expected_files and ValidatorService._verification_is_weak(
            updated.get("verification")
        ):
            grep_target = _grep_quiet_verification_target(
                str(updated.get("verification") or "")
            )
            if grep_target and grep_target[0] in expected_files:
                updated["verification"] = _python_file_contains_verification_command(
                    grep_target[0],
                    grep_target[1],
                )
        if (
            expected_files
            and _commands_are_weak_expected_file_verification(updated.get("commands"))
            and grep_target
        ):
            updated["commands"] = [str(updated.get("verification") or "").strip()]
        strengthened.append(updated)
    return strengthened
