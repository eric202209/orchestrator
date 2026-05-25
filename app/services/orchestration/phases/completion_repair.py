"""Completion repair helpers for the task completion phase."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from app.services.orchestration.context.assembly import (
    collect_workspace_inventory_paths,
)
from app.services.orchestration.policy import COMPLETION_VERIFICATION_TIMEOUT_SECONDS
from app.services.orchestration.types import FailureEnvelope, ValidationVerdict
from app.services.orchestration.validation.validator import ValidatorService
from app.services.workspace.path_display import render_workspace_path_for_prompt

RELATIVE_PATH_TOKEN_RE = re.compile(
    r"(?<![\w./-])("
    r"(?:src|tests|fixtures|scripts|config)/[A-Za-z0-9_./-]+"
    r"|(?:package\.json|tsconfig\.json|vitest\.config\.ts|jest\.config\.js|README\.md|\.env\.example)"
    r")(?![\w./-])"
)


def _build_completion_repair_workspace_summary(
    *,
    project_dir: Path,
    completion_validation: Any,
    max_files: int = 80,
) -> str:
    expected_files = (
        list((completion_validation.details or {}).get("expected_core_files", []) or [])
        if completion_validation
        else []
    )
    existing_files = [
        path
        for path in collect_workspace_inventory_paths(project_dir, max_files=max_files)
        if Path(path).suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".json", ".sh"}
    ][:max_files]

    similar_map: dict[str, list[str]] = {}
    lowered_existing = [(item, item.lower()) for item in existing_files]
    for expected in expected_files[:20]:
        expected_name = Path(expected).stem.lower().replace("-", "_")
        expected_parent = str(Path(expected).parent).lower()
        matches: list[str] = []
        for existing, lowered in lowered_existing:
            existing_name = Path(existing).stem.lower().replace("-", "_")
            if expected_name == existing_name:
                matches.append(existing)
                continue
            if (
                expected_parent
                and expected_parent in lowered
                and any(
                    token and token in existing_name
                    for token in expected_name.split("_")
                )
            ):
                matches.append(existing)
        if matches:
            similar_map[expected] = matches[:5]

    lines = [
        "Current workspace inventory:",
        *[f"- {path}" for path in existing_files[:max_files]],
    ]
    if expected_files:
        lines.append("Expected core files from the current accepted plan:")
        lines.extend(f"- {path}" for path in expected_files[:20])
    if similar_map:
        lines.append(
            "Existing files that look structurally similar to missing expected files:"
        )
        for expected, matches in similar_map.items():
            lines.append(f"- {expected} -> {', '.join(matches)}")
    return "\n".join(lines)


def _extract_relative_paths_from_text(raw_text: str) -> set[str]:
    text = str(raw_text or "")
    return {match.group(1).strip() for match in RELATIVE_PATH_TOKEN_RE.finditer(text)}


def _collect_created_paths_from_commands(
    commands: list[str],
) -> tuple[set[str], set[str]]:
    created_files: set[str] = set()
    created_dirs: set[str] = set()
    for command in commands:
        text = str(command or "").strip()
        if not text:
            continue
        for match in re.finditer(r"(?:cat|tee)\s*>\s*([A-Za-z0-9_./-]+)", text):
            created_files.add(match.group(1).strip())
        if text.startswith("touch "):
            for token in text.split()[1:]:
                cleaned = token.strip()
                if cleaned and ("/" in cleaned or "." in cleaned):
                    created_files.add(cleaned)
        if text.startswith("mkdir -p "):
            for token in text.split()[2:]:
                cleaned = token.strip()
                if cleaned:
                    created_dirs.add(cleaned.rstrip("/"))
        if text.startswith("mv "):
            parts = text.split()
            if len(parts) >= 3:
                created_files.add(parts[-1].strip())
    return created_files, created_dirs


def _completion_repair_invalid_paths(
    *,
    repair_step: Dict[str, Any],
    project_dir: Path,
    completion_validation: Any,
) -> list[str]:
    inventory_files = set(collect_workspace_inventory_paths(project_dir))
    expected_files = set(
        list((completion_validation.details or {}).get("expected_core_files", []) or [])
    )
    commands = [str(command) for command in repair_step.get("commands", []) or []]
    created_files, created_dirs = _collect_created_paths_from_commands(commands)
    referenced_paths: set[str] = set()
    for text in commands + [
        str(repair_step.get("verification") or ""),
        str(repair_step.get("rollback") or ""),
    ]:
        referenced_paths.update(_extract_relative_paths_from_text(text))

    invalid: list[str] = []
    for path in sorted(referenced_paths):
        if (
            path in inventory_files
            or path in expected_files
            or path in created_files
            or any(
                path == directory or path.startswith(f"{directory}/")
                for directory in created_dirs
            )
        ):
            continue
        invalid.append(path)
    return invalid


def _extract_reported_changed_files(output_text: str, project_dir: Path) -> list[str]:
    reported: list[str] = []
    text = str(output_text or "")
    patterns = [
        r"`([^`]+)`",
        r"[-*]\s+([A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx|json|sh))",
        r"(src/[A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx))",
        r"(tests/[A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx))",
        r"(vitest\.config\.ts|jest\.config\.js|package\.json|tsconfig\.json|\.env\.example)",
    ]
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = match.group(1).strip()
            if not candidate or candidate in seen:
                continue
            resolved = (project_dir / candidate).resolve()
            if resolved.exists() and resolved.is_file():
                seen.add(candidate)
                reported.append(candidate)
    return reported


def _detect_completion_verification_command(
    project_dir: Path,
) -> tuple[Optional[str], Optional[str]]:
    package_json = project_dir / "package.json"
    if package_json.exists():
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            package_data = {}
        scripts = (
            package_data.get("scripts", {}) if isinstance(package_data, dict) else {}
        )
        has_tests = any(
            candidate.exists()
            for candidate in [
                project_dir / "tests",
                project_dir / "test",
                project_dir / "src" / "tests",
            ]
        )
        if (
            has_tests
            and isinstance(scripts, dict)
            and str(scripts.get("test") or "").strip()
        ):
            test_script = str(scripts.get("test") or "").strip()
            if (project_dir / "pnpm-lock.yaml").exists():
                return (
                    _augment_completion_verification_command("pnpm test", test_script),
                    "package.json test script via pnpm",
                )
            if (project_dir / "yarn.lock").exists():
                return (
                    _augment_completion_verification_command("yarn test", test_script),
                    "package.json test script via yarn",
                )
            return (
                _augment_completion_verification_command("npm test", test_script),
                "package.json test script via npm",
            )

    if any(
        candidate.exists()
        for candidate in [project_dir / "pytest.ini", project_dir / "tests"]
    ):
        if (project_dir / "pyproject.toml").exists() or (
            project_dir / "tests"
        ).exists():
            return (
                f"{shlex.quote(_completion_verification_python(project_dir))} -m pytest",
                "python test suite detected",
            )

    return None, None


def _completion_verification_python(project_dir: Path) -> str:
    """Prefer the nearest project/repo virtualenv for Python verification."""

    candidates = [
        project_dir / "venv" / "bin" / "python",
        project_dir / ".venv" / "bin" / "python",
    ]
    for parent in [project_dir, *project_dir.parents]:
        candidates.extend(
            [
                parent / "venv" / "bin" / "python",
                parent / ".venv" / "bin" / "python",
            ]
        )

    # Also search from the process working directory. This covers the case
    # where the project lives outside the orchestrator tree (e.g. tmp_path in
    # tests, or a workspace directory mounted separately in Docker) but the
    # orchestrator itself has a local venv adjacent to its working directory.
    process_cwd = Path.cwd()
    project_ancestors = {project_dir, *project_dir.parents}
    if process_cwd not in project_ancestors:
        candidates.extend(
            [
                process_cwd / "venv" / "bin" / "python",
                process_cwd / ".venv" / "bin" / "python",
            ]
        )

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)

    return sys.executable or "python3"


def _augment_completion_verification_command(command: str, test_script: str) -> str:
    normalized_command = str(command or "").strip()
    normalized_script = str(test_script or "").strip().lower()

    if not normalized_command or not normalized_script:
        return normalized_command

    if ".openclaw" in normalized_script:
        return normalized_command

    if "vitest" in normalized_script and "--exclude" not in normalized_script:
        return f"{normalized_command} -- --exclude=.openclaw/**"

    if re.search(r"(^|\s)jest(\s|$)", normalized_script) and (
        "--testpathignorepatterns" not in normalized_script
    ):
        return f"{normalized_command} -- --testPathIgnorePatterns=.openclaw/"

    return normalized_command


def _execute_completion_verification(
    *,
    project_dir: Path,
    command: str,
    timeout_seconds: int = COMPLETION_VERIFICATION_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    try:
        if any(token in command for token in (";", "&&", "||", "|", "$(", "`")):
            return {
                "success": False,
                "returncode": None,
                "output": (
                    "Completion verification command was rejected because it contains "
                    "unsafe shell metacharacters"
                ),
            }
        argv = shlex.split(command, posix=True)
        if not argv:
            return {
                "success": False,
                "returncode": None,
                "output": "Completion verification command was empty after parsing",
            }
        env = dict(os.environ)
        raw_pythonpath = env.get("PYTHONPATH", "")
        if raw_pythonpath:
            caller_cwd = Path.cwd()
            absolute_entries = []
            for entry in raw_pythonpath.split(os.pathsep):
                p = Path(entry)
                resolved = (caller_cwd / p).resolve() if not p.is_absolute() else p
                if resolved.exists():
                    absolute_entries.append(str(resolved))
            if absolute_entries:
                env["PYTHONPATH"] = os.pathsep.join(absolute_entries)
            else:
                env.pop("PYTHONPATH", None)
        pythonpath_entries = [str(project_dir.resolve())]
        if env.get("PYTHONPATH"):
            pythonpath_entries.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
        completed = subprocess.run(
            argv,
            cwd=str(project_dir),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
        output = "\n".join(
            part
            for part in [completed.stdout.strip(), completed.stderr.strip()]
            if part
        ).strip()
        return {
            "success": completed.returncode == 0,
            "returncode": completed.returncode,
            "output": output[:6000],
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "returncode": None,
            "output": f"Completion verification timed out after {timeout_seconds}s",
        }
    except ValueError as exc:
        return {
            "success": False,
            "returncode": None,
            "output": f"Completion verification command could not be parsed: {exc}",
        }


def _classify_completion_verification_failure(
    *,
    command: str,
    source: Optional[str],
    verification_output: str,
    completion_validation: Any,
) -> Optional[ValidationVerdict]:
    normalized_output = str(verification_output or "").strip()
    lowered = normalized_output.lower()
    missing_dependency_markers = (
        "jest: not found",
        "vitest: not found",
        "mocha: not found",
        "tsx: not found",
        "ts-node: not found",
        "command not found",
        "cannot find module",
    )
    repairable_test_failure_markers = (
        "failed to load url",
        "does the file exist?",
        "failed suites",
        "no test suite found",
        "module not found",
        "cannot find module",
        "no module named",
        "modulenotfounderror",
    )

    if not any(marker in lowered for marker in missing_dependency_markers):
        if "timed out" in lowered:
            return None
        if not any(marker in lowered for marker in repairable_test_failure_markers):
            return None

    expected_core_files = (
        list((completion_validation.details or {}).get("expected_core_files", []) or [])
        if completion_validation
        else []
    )
    preview = normalized_output[:400]
    if any(marker in lowered for marker in missing_dependency_markers):
        reason = (
            "Completion verification could not run because the test runner or project "
            f"dependencies are missing or not installed for `{command}`"
        )
        failure_class = "missing_dependency"
    else:
        reason = (
            "Completion verification found a repairable test/module issue under "
            f"`{command}`"
        )
        failure_class = (
            "module_not_found"
            if (
                "no module named" in lowered
                or "modulenotfounderror" in lowered
                or "module not found" in lowered
                or "cannot find module" in lowered
            )
            else "completion_validation_failed"
        )
    if preview:
        reason += f": {preview}"

    return ValidationVerdict(
        stage="completion_verification",
        status="repair_required",
        profile=(getattr(completion_validation, "profile", None) or "implementation"),
        reasons=[reason],
        details={
            "expected_core_files": expected_core_files[:20],
            "verification_command": command,
            "verification_source": source or "auto-detected",
            "verification_output_preview": preview,
            "completion_repair_source": "final_completion_verification",
            "failure_class": failure_class,
        },
    )


def _build_completion_repair_prompt(
    *,
    task_prompt: str,
    completion_validation: Any,
    project_dir: Any,
    prior_results_summary: str,
    project_context: str,
    next_step_number: int,
    workspace_inventory: str,
    failure_envelope: Optional[FailureEnvelope] = None,
) -> str:
    prompt_project_dir = render_workspace_path_for_prompt(project_dir)
    failure_block = (
        "\n\nNormalized execution error:\n"
        + failure_envelope.to_prompt_block(max_chars=1800)
        if failure_envelope is not None
        else ""
    )
    return f"""Return one minimal JSON repair step to fix completion validation issues. Output JSON object only.

Task:
{task_prompt[:2000]}

Working directory:
{prompt_project_dir}

Completion validation issues:
{json.dumps(completion_validation.reasons[:10], indent=2)}

Prior completed results:
{prior_results_summary[:2000]}

{failure_block}

Project context:
{project_context[:2500]}

Current workspace inventory:
{workspace_inventory[:5000]}

Rules:
1. Return a single JSON object with keys: step_number, description, commands, verification, rollback, expected_files
2. Keep the fix atomic and minimal
3. Use relative shell paths only
4. Do not use `..`, `~`, or absolute paths in commands
5. Do not create documentation files unless explicitly required
6. Do not create a new top-level project folder
7. Prefer fixing misplaced files, missing core files, or weak structure over rewriting the whole project
8. commands must be a non-empty JSON array
9. expected_files must list the files this repair should materialize or normalize
10. Use the workspace inventory above as the source of truth; do not assume older file names or architectures that are not present
11. Prefer renaming, moving, or normalizing existing files over creating parallel replacements
12. Do not read or modify a guessed file path unless it appears in the workspace inventory or your commands create it first

Output example:
{{
  "step_number": {next_step_number},
  "description": "Move generated test files into the workspace root and verify they load",
  "commands": ["mkdir -p tests", "mv nested/tests/*.spec.js tests/"],
  "verification": "test -f tests/event-chain.spec.js",
  "rollback": null,
  "expected_files": ["tests/event-chain.spec.js"]
}}
"""


def _normalize_completion_repair_step(
    raw_step: Dict[str, Any], next_step_number: int
) -> Dict[str, Any]:
    commands = raw_step.get("commands", [])
    if isinstance(commands, str):
        commands = [commands]
    if not isinstance(commands, list):
        commands = []

    expected_files = raw_step.get("expected_files", [])
    if isinstance(expected_files, str):
        expected_files = [expected_files]
    if not isinstance(expected_files, list):
        expected_files = []

    return {
        "step_number": raw_step.get("step_number") or next_step_number,
        "description": str(
            raw_step.get("description") or "Apply minimal completion repair"
        ),
        "commands": [
            str(command).strip() for command in commands if str(command).strip()
        ],
        "verification": raw_step.get("verification"),
        "rollback": raw_step.get("rollback"),
        "expected_files": [
            str(path).strip() for path in expected_files if str(path).strip()
        ],
    }


def _extract_completion_repair_step(
    parsed_data: Any, next_step_number: int
) -> Optional[Dict[str, Any]]:
    if isinstance(parsed_data, dict):
        step_like_keys = {
            "step_number",
            "description",
            "commands",
            "verification",
            "rollback",
            "expected_files",
        }
        if step_like_keys.intersection(parsed_data.keys()):
            return _normalize_completion_repair_step(parsed_data, next_step_number)

        for key in (
            "step",
            "repair_step",
            "completion_repair_step",
            "payload",
            "result",
        ):
            candidate = parsed_data.get(key)
            if isinstance(candidate, dict):
                extracted = _extract_completion_repair_step(candidate, next_step_number)
                if extracted:
                    return extracted

    if isinstance(parsed_data, list):
        for item in parsed_data:
            extracted = _extract_completion_repair_step(item, next_step_number)
            if extracted:
                return extracted

    return None


def _completion_failure_signature(completion_validation: Any) -> str:
    reasons = list(getattr(completion_validation, "reasons", []) or [])
    details = getattr(completion_validation, "details", {}) or {}
    existing = str(details.get("failure_signature") or "").strip()
    if existing:
        return existing
    return ValidatorService.build_failure_signature(reasons)


def _repeats_prior_completion_failure(
    orchestration_state: Any, completion_validation: Any
) -> bool:
    current_signature = _completion_failure_signature(completion_validation)
    if not current_signature:
        return False
    prior_signature = str(
        (
            (getattr(orchestration_state, "last_completion_validation", {}) or {})
            .get("details", {})
            .get("failure_signature")
        )
        or ""
    ).strip()
    return bool(prior_signature and prior_signature == current_signature)
