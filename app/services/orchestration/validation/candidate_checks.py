"""Candidate-owned focused verification selection and execution."""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.services.orchestration.execution.python_resolution import (
    ProjectPythonResolutionError,
    resolve_project_python,
)
from app.services.orchestration.types import CandidateFinding

from .path_authority import PathDeclarationError, declare
from .workspace_guard import TaskWorkspaceViolationError, normalize_path_reference


@dataclass(frozen=True)
class CandidateVerificationSelection:
    command: str
    source: str
    paths: tuple[str, ...]
    fallback: bool = False


@dataclass(frozen=True)
class CandidateStaticPolicy:
    """Project-owned admission for optional candidate style tools."""

    black_admitted: bool = False
    black_source: str | None = None
    flake8_admitted: bool = False
    flake8_source: str | None = None

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {
            "black": {
                "admitted": self.black_admitted,
                "source": self.black_source,
            },
            "flake8": {
                "admitted": self.flake8_admitted,
                "source": self.flake8_source,
            },
        }


@dataclass(frozen=True)
class CandidateCheckRun:
    selection: CandidateVerificationSelection
    findings: tuple[CandidateFinding, ...]
    commands_run: tuple[str, ...]
    static_policy: CandidateStaticPolicy = field(default_factory=CandidateStaticPolicy)
    # PR20-A1: present only when task acceptance verification is enabled.
    task_acceptance: dict[str, Any] | None = None


@dataclass(frozen=True)
class TaskAcceptanceCommand:
    text: str
    pytest_args: tuple[str, ...]


@dataclass(frozen=True)
class TaskAcceptanceExtraction:
    """Pytest commands stated in a task's explicit ``Acceptance criteria:``."""

    section_found: bool
    extracted: tuple[str, ...]
    admitted: tuple[TaskAcceptanceCommand, ...]
    refused: tuple[dict[str, str], ...]
    ignored: tuple[dict[str, str], ...]


_ACCEPTANCE_HEADING = re.compile(r"^\s*acceptance criteria:(.*)$", re.IGNORECASE)
# A later unindented, non-list "Heading:" line closes the acceptance section.
_SECTION_HEADING = re.compile(r"^[A-Za-z][^`]*:\s*$")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_PYTEST_WORD = re.compile(r"(?<![\w.-])pytest(?![\w./-])")
_SHELL_METACHARACTERS = frozenset(";&|<>$`(){}[]*?!~#\\'\"\n")
_PYTEST_FLAGS = frozenset(
    {
        "-q",
        "-qq",
        "-v",
        "-vv",
        "-x",
        "-s",
        "-ra",
        "--quiet",
        "--verbose",
        "--exitfirst",
        "--tb=auto",
        "--tb=long",
        "--tb=short",
        "--tb=line",
        "--tb=native",
        "--tb=no",
    }
)


def _acceptance_sections(task_prompt: str) -> list[str]:
    sections: list[str] = []
    current: list[str] | None = None
    for line in str(task_prompt or "").splitlines():
        heading = _ACCEPTANCE_HEADING.match(line)
        if heading:
            if current is not None:
                sections.append("\n".join(current))
            current = [heading.group(1)]
        elif current is not None:
            if _SECTION_HEADING.match(line):
                sections.append("\n".join(current))
                current = None
            else:
                current.append(line)
    if current is not None:
        sections.append("\n".join(current))
    return sections


def _admit_pytest_command(
    text: str, project_dir: Path | None
) -> tuple[TaskAcceptanceCommand | None, str]:
    if any(char in _SHELL_METACHARACTERS for char in text):
        return None, "shell_metacharacter"
    tokens = text.split()
    if tokens[:1] == ["pytest"]:
        args = tokens[1:]
    elif tokens[:1] in (["python"], ["python3"]) and tokens[1:3] == ["-m", "pytest"]:
        args = tokens[3:]
    else:
        return None, "unsupported_executable"
    admitted_args: list[str] = []
    for arg in args:
        if arg.startswith("-"):
            if arg not in _PYTEST_FLAGS:
                return None, "unsupported_pytest_option"
            admitted_args.append(arg)
            continue
        raw_path, separator, node = arg.partition("::")
        if "=" in raw_path:
            return None, "unsupported_argument"
        if Path(raw_path).is_absolute():
            return None, "absolute_path"
        if ".." in Path(raw_path).parts:
            return None, "path_traversal"
        path = raw_path
        if project_dir is not None:
            try:
                path = normalize_path_reference(raw_path, project_dir.resolve())
            except TaskWorkspaceViolationError:
                return None, "path_escapes_workspace"
            if Path(path).is_absolute():
                return None, "path_escapes_workspace"
        admitted_args.append(f"{path}{separator}{node}")
    return TaskAcceptanceCommand(text=text, pytest_args=tuple(admitted_args)), ""


def task_acceptance_pytest_commands(
    task_prompt: str, *, project_dir: Path | None = None
) -> TaskAcceptanceExtraction:
    """Extract pytest commands quoted in the task's explicit acceptance section.

    Only backtick-quoted spans inside an ``Acceptance criteria:`` section are
    considered, and only spans naming ``pytest`` are command candidates.
    Nothing is executed here; with ``project_dir`` path arguments must also
    resolve (symlinks included) inside the workspace.
    """

    sections = _acceptance_sections(task_prompt)
    extracted: list[str] = []
    admitted: list[TaskAcceptanceCommand] = []
    refused: list[dict[str, str]] = []
    ignored: list[dict[str, str]] = []
    for section in sections:
        for span in _CODE_SPAN.findall(section):
            text = span.strip()
            if not text:
                continue
            if not _PYTEST_WORD.search(text):
                ignored.append({"text": text, "reason": "not_pytest_command"})
                continue
            extracted.append(text)
            command, reason = _admit_pytest_command(text, project_dir)
            if command is None:
                refused.append({"command": text, "reason": reason})
            elif command not in admitted:
                admitted.append(command)
    return TaskAcceptanceExtraction(
        section_found=bool(sections),
        extracted=tuple(extracted),
        admitted=tuple(admitted),
        refused=tuple(refused),
        ignored=tuple(ignored),
    )


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _has_config_section(path: Path, section: str) -> bool:
    try:
        parser = configparser.ConfigParser(interpolation=None)
        with path.open(encoding="utf-8") as config_file:
            parser.read_file(config_file)
        return parser.has_section(section)
    except (configparser.Error, OSError, UnicodeError):
        return False


def discover_candidate_static_policy(project_dir: Path) -> CandidateStaticPolicy:
    """Admit optional style gates only from root project-owned configuration."""

    black_source: str | None = None
    pyproject = project_dir / "pyproject.toml"
    if _is_file(pyproject):
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            document = {}
        tool = document.get("tool")
        if isinstance(tool, Mapping) and isinstance(tool.get("black"), Mapping):
            black_source = "pyproject.toml:[tool.black]"

    flake8_source: str | None = None
    flake8_file = project_dir / ".flake8"
    if _is_file(flake8_file):
        flake8_source = ".flake8"
    else:
        for name in ("setup.cfg", "tox.ini"):
            config_path = project_dir / name
            if _is_file(config_path) and _has_config_section(config_path, "flake8"):
                flake8_source = f"{name}:[flake8]"
                break

    return CandidateStaticPolicy(
        black_admitted=black_source is not None,
        black_source=black_source,
        flake8_admitted=flake8_source is not None,
        flake8_source=flake8_source,
    )


def candidate_delta_identity(
    change_set: Mapping[str, Any], *, project_dir: Path | None = None
) -> str:
    """Stable identity for the exact candidate file delta."""

    target_root = project_dir
    if target_root is None and change_set.get("target_path"):
        target_root = Path(str(change_set["target_path"]))
    content_digests: dict[str, str] = {}
    if target_root is not None:
        target_root = target_root.resolve()
        for key in ("added_files", "modified_files"):
            for raw_path in change_set.get(key) or []:
                try:
                    path = normalize_path_reference(str(raw_path), target_root)
                except TaskWorkspaceViolationError:
                    content_digests[str(raw_path)] = "invalid_path"
                    continue
                candidate = target_root / path
                content_digests[path] = (
                    hashlib.sha256(candidate.read_bytes()).hexdigest()
                    if candidate.is_file()
                    else "missing"
                )
    identity_payload = {
        key: sorted(str(path) for path in (change_set.get(key) or []))
        for key in ("added_files", "modified_files", "deleted_files")
    }
    identity_payload["content_sha256"] = content_digests
    digest = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"sha256:{digest}"


def candidate_observed_paths(change_set: Mapping[str, Any]) -> tuple[str, ...]:
    """Return canonical paths observed by the Change Set.

    This is an observation projection only.  It deliberately has no
    authorization vocabulary and cannot consult or manufacture a grant.
    Accepted Path Authority is supplied separately by the Candidate Validator.
    """

    paths: list[str] = []
    for key in ("added_files", "modified_files", "deleted_files"):
        for raw_path in change_set.get(key) or []:
            try:
                path = declare(str(raw_path)).value
            except PathDeclarationError:
                continue
            if path != "." and path not in paths:
                paths.append(path)
    return tuple(paths)


def _is_python_test(path: str) -> bool:
    name = Path(path).name
    return path.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def _pytest_selection(
    project_dir: Path,
    paths: Sequence[str],
    *,
    source: str,
    fallback: bool = False,
) -> CandidateVerificationSelection:
    python = shlex.quote(_candidate_python(project_dir))
    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    return CandidateVerificationSelection(
        command=f"{python} -m pytest {quoted_paths}".rstrip(),
        source=source,
        paths=tuple(paths),
        fallback=fallback,
    )


def _candidate_python(project_dir: Path) -> str:
    """Use candidate-local Python, otherwise the running Orchestrator environment."""

    return resolve_project_python(project_dir)


def _python_unavailable_finding(exc: ProjectPythonResolutionError) -> CandidateFinding:
    return CandidateFinding(
        rule_id="candidate_python_unavailable",
        source="python_resolution",
        category="infrastructure",
        severity="error",
        attribution="unknown",
        repairable=False,
        message="Candidate verification interpreter could not be resolved",
        evidence={"error": str(exc)},
    )


def select_candidate_verification(
    *,
    project_dir: Path,
    change_set: Mapping[str, Any],
    plan: Sequence[Mapping[str, Any]],
    task_prompt: str,
    allow_broad_fallback: bool = False,
    observed_scope: Sequence[str] | None = None,
    verification_scope: Sequence[str] | None = None,
) -> CandidateVerificationSelection:
    """Select the smallest deterministic test scope owned by the candidate."""

    observed_paths = tuple(
        candidate_observed_paths(change_set)
        if observed_scope is None
        else (str(path) for path in observed_scope)
    )
    scoped_paths = tuple(
        observed_paths
        if verification_scope is None
        else (str(path) for path in verification_scope)
    )
    changed_python_tests = tuple(
        path
        for path in observed_paths
        if _is_python_test(path) and (project_dir / path).is_file()
    )
    if changed_python_tests:
        return _pytest_selection(
            project_dir,
            changed_python_tests,
            source="candidate_changed_python_tests",
        )

    declared_tests: list[str] = []
    for step in plan:
        verification = str(step.get("verification") or "")
        try:
            tokens = shlex.split(verification)
        except ValueError:
            tokens = []
        for token in tokens:
            raw_path = token.split("::", 1)[0]
            if not _is_python_test(raw_path):
                continue
            try:
                path = normalize_path_reference(raw_path, project_dir)
            except TaskWorkspaceViolationError:
                continue
            if (project_dir / path).is_file() and path not in declared_tests:
                declared_tests.append(path)
    if declared_tests:
        return _pytest_selection(
            project_dir,
            declared_tests,
            source="plan_declared_focused_tests",
        )

    regression_tests: list[str] = []
    for changed_path in scoped_paths:
        path = Path(changed_path)
        if path.suffix != ".py" or _is_python_test(changed_path):
            continue
        test_name = f"test_{path.stem}.py"
        for candidate in (
            path.parent / "tests" / test_name,
            Path("tests") / test_name,
            path.parent / test_name,
        ):
            candidate_text = candidate.as_posix()
            if (
                project_dir / candidate
            ).is_file() and candidate_text not in regression_tests:
                regression_tests.append(candidate_text)
    if regression_tests:
        return _pytest_selection(
            project_dir,
            regression_tests,
            source="deterministic_existing_regression_tests",
        )

    if allow_broad_fallback and (
        (project_dir / "pytest.ini").is_file() or (project_dir / "tests").is_dir()
    ):
        return _pytest_selection(
            project_dir,
            (),
            source="explicit_repository_pytest_fallback",
            fallback=True,
        )

    return CandidateVerificationSelection(
        command="",
        source="no_trustworthy_focused_tests",
        paths=(),
        fallback=False,
    )


def _run_command(
    *, project_dir: Path, command: str, timeout_seconds: int
) -> tuple[int | None, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_ADDOPTS"] = (
        str(env.get("PYTEST_ADDOPTS") or "") + " -p no:cacheprovider"
    ).strip()
    with tempfile.TemporaryDirectory(prefix="candidate-validation-pycache-") as cache:
        env["PYTHONPYCACHEPREFIX"] = cache
        try:
            completed = subprocess.run(
                shlex.split(command),
                cwd=project_dir,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            output = "\n".join(
                str(part or "") for part in (exc.stdout, exc.stderr) if part
            )
            return None, (output + f"\nTimed out after {timeout_seconds}s").strip()
    output = "\n".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if part and part.strip()
    )
    return completed.returncode, output[:12000]


def _task_acceptance_finding(
    *,
    rule_id: str,
    category: str,
    message: str,
    evidence: dict[str, Any],
) -> CandidateFinding:
    # No baseline differential exists for task-wide acceptance commands, so a
    # failure cannot be attributed to the candidate and stays fail-closed.
    return CandidateFinding(
        rule_id=rule_id,
        source="task_acceptance",
        category=category,
        severity="error",
        attribution="unknown",
        repairable=False,
        message=message,
        evidence=evidence,
    )


def _run_task_acceptance(
    *,
    project_dir: Path,
    task_prompt: str,
    focused_command: str,
    focused_returncode: int | None,
    timeout_seconds: int,
    findings: list[CandidateFinding],
    commands_run: list[str],
) -> dict[str, Any]:
    """Run task-declared acceptance pytest commands in addition to focused tests."""

    extraction = task_acceptance_pytest_commands(task_prompt, project_dir=project_dir)
    for refusal in extraction.refused:
        findings.append(
            _task_acceptance_finding(
                rule_id="task_acceptance_command_refused",
                category="task_contract",
                message=(
                    "Task acceptance command was refused and cannot be verified: "
                    f"{refusal['command']}"
                ),
                evidence=dict(refusal),
            )
        )
    results: list[dict[str, Any]] = []
    python = None
    if extraction.admitted:
        try:
            python = shlex.quote(_candidate_python(project_dir))
        except ProjectPythonResolutionError as exc:
            if not any(
                finding.rule_id == "candidate_python_unavailable"
                for finding in findings
            ):
                findings.append(_python_unavailable_finding(exc))
    for admitted in extraction.admitted:
        if python is None:
            results.append(
                {"command": admitted.text, "executed": False, "outcome": "not_run"}
            )
            continue
        command = " ".join(
            [python, "-m", "pytest", *(shlex.quote(a) for a in admitted.pytest_args)]
        )
        result: dict[str, Any] = {
            "command": admitted.text,
            "executed_command": command,
        }
        if focused_command and command == focused_command:
            # Identical argv already ran as the focused selection; its result
            # (and any finding) is authoritative.
            returncode = focused_returncode
            result["covered_by"] = "focused_test_selection"
            output = ""
        else:
            commands_run.append(command)
            returncode, output = _run_command(
                project_dir=project_dir,
                command=command,
                timeout_seconds=timeout_seconds,
            )
        result["executed"] = True
        result["returncode"] = returncode
        if returncode == 0:
            result["outcome"] = "passed"
        elif returncode is None:
            result["outcome"] = "infrastructure_failure"
        else:
            result["outcome"] = "failed"
        results.append(result)
        if "covered_by" in result or returncode == 0:
            continue
        evidence = {
            "command": command,
            "task_command": admitted.text,
            "returncode": returncode,
            "output": output,
        }
        if returncode is None:
            findings.append(
                _task_acceptance_finding(
                    rule_id="task_acceptance_pytest_infrastructure_failure",
                    category="infrastructure",
                    message="Task acceptance pytest did not complete",
                    evidence=evidence,
                )
            )
        else:
            findings.append(
                _task_acceptance_finding(
                    rule_id="task_acceptance_pytest_failed",
                    category="test",
                    message=f"Task acceptance pytest failed: {admitted.text}",
                    evidence=evidence,
                )
            )
    return {
        "section_found": extraction.section_found,
        "extracted": list(extraction.extracted),
        "admitted": [command.text for command in extraction.admitted],
        "refused": [dict(refusal) for refusal in extraction.refused],
        "ignored": [dict(item) for item in extraction.ignored],
        "results": results,
    }


def validate_candidate_delta(
    *,
    project_dir: Path,
    change_set: Mapping[str, Any],
    plan: Sequence[Mapping[str, Any]],
    task_prompt: str,
    include_static_checks: bool = True,
    allow_broad_fallback: bool = False,
    timeout_seconds: int = 180,
    observed_scope: Sequence[str] | None = None,
    verification_scope: Sequence[str] | None = None,
    task_acceptance_enabled: bool = False,
) -> CandidateCheckRun:
    """Run candidate-owned checks and return typed, attributed findings."""

    findings: list[CandidateFinding] = []
    try:
        selection = select_candidate_verification(
            project_dir=project_dir,
            change_set=change_set,
            plan=plan,
            task_prompt=task_prompt,
            allow_broad_fallback=allow_broad_fallback,
            observed_scope=observed_scope,
            verification_scope=verification_scope,
        )
    except ProjectPythonResolutionError as exc:
        findings.append(_python_unavailable_finding(exc))
        selection = CandidateVerificationSelection(
            command="", source="candidate_python_unavailable", paths=()
        )
    static_policy = discover_candidate_static_policy(project_dir)
    commands_run: list[str] = []
    focused_returncode: int | None = None
    if selection.command:
        commands_run.append(selection.command)
        returncode, output = _run_command(
            project_dir=project_dir,
            command=selection.command,
            timeout_seconds=timeout_seconds,
        )
        focused_returncode = returncode
        if returncode is None:
            findings.append(
                CandidateFinding(
                    rule_id="focused_pytest_infrastructure_failure",
                    source="pytest",
                    category="infrastructure",
                    severity="error",
                    attribution="unknown",
                    repairable=False,
                    message="Focused candidate pytest did not complete",
                    evidence={
                        "command": selection.command,
                        "returncode": None,
                        "output": output,
                    },
                )
            )
        elif returncode != 0:
            findings.append(
                CandidateFinding(
                    rule_id="focused_pytest_failed",
                    source="pytest",
                    category="test",
                    severity="error",
                    attribution="candidate_introduced",
                    repairable=True,
                    message="Focused candidate pytest failed",
                    evidence={
                        "command": selection.command,
                        "returncode": returncode,
                        "output": output,
                    },
                )
            )

    task_acceptance = None
    if task_acceptance_enabled:
        task_acceptance = _run_task_acceptance(
            project_dir=project_dir,
            task_prompt=task_prompt,
            focused_command=selection.command,
            focused_returncode=focused_returncode,
            timeout_seconds=timeout_seconds,
            findings=findings,
            commands_run=commands_run,
        )

    if include_static_checks:
        observed_paths = tuple(
            candidate_observed_paths(change_set)
            if observed_scope is None
            else (str(path) for path in observed_scope)
        )
        python_paths = tuple(
            path
            for path in observed_paths
            if path.endswith(".py") and (project_dir / path).is_file()
        )
        quoted_python = None
        if python_paths:
            try:
                quoted_python = shlex.quote(_candidate_python(project_dir))
            except ProjectPythonResolutionError as exc:
                if not any(
                    finding.rule_id == "candidate_python_unavailable"
                    for finding in findings
                ):
                    findings.append(_python_unavailable_finding(exc))
        if quoted_python is not None:
            quoted_paths = " ".join(shlex.quote(path) for path in python_paths)
            static_commands = [
                (
                    "compileall",
                    "candidate_python_compile_failed",
                    f"{quoted_python} -m compileall -q {quoted_paths}",
                )
            ]
            if static_policy.black_admitted:
                static_commands.append(
                    (
                        "black",
                        "candidate_black_failed",
                        f"{quoted_python} -m black --check {quoted_paths}",
                    )
                )
            if static_policy.flake8_admitted:
                static_commands.append(
                    (
                        "flake8",
                        "candidate_flake8_failed",
                        f"{quoted_python} -m flake8 {quoted_paths}",
                    )
                )
            for source, rule_id, command in static_commands:
                commands_run.append(command)
                returncode, output = _run_command(
                    project_dir=project_dir,
                    command=command,
                    timeout_seconds=timeout_seconds,
                )
                if returncode == 0:
                    continue
                tool_unavailable = (
                    returncode is not None and f"No module named {source}" in output
                )
                findings.append(
                    CandidateFinding(
                        rule_id=(
                            f"{source}_infrastructure_failure"
                            if returncode is None or tool_unavailable
                            else rule_id
                        ),
                        source=source,
                        category=(
                            "infrastructure"
                            if returncode is None or tool_unavailable
                            else "static"
                        ),
                        severity="error",
                        attribution=(
                            "unknown"
                            if returncode is None or tool_unavailable
                            else "candidate_introduced"
                        ),
                        repairable=not (returncode is None or tool_unavailable),
                        message=(
                            f"Candidate-scoped {source} did not complete"
                            if returncode is None or tool_unavailable
                            else f"Candidate-scoped {source} failed"
                        ),
                        evidence={
                            "command": command,
                            "returncode": returncode,
                            "output": output,
                            "paths": list(python_paths),
                        },
                    )
                )

        if (project_dir / ".git").exists():
            changed_paths = observed_paths
            if changed_paths:
                quoted_paths = " ".join(shlex.quote(path) for path in changed_paths)
                command = f"git diff --check -- {quoted_paths}"
                commands_run.append(command)
                returncode, output = _run_command(
                    project_dir=project_dir,
                    command=command,
                    timeout_seconds=timeout_seconds,
                )
                if returncode != 0:
                    findings.append(
                        CandidateFinding(
                            rule_id="candidate_diff_check_failed",
                            source="git_diff_check",
                            category="static",
                            severity="error",
                            attribution="candidate_introduced",
                            repairable=True,
                            message="Candidate-scoped git diff check failed",
                            evidence={
                                "command": command,
                                "returncode": returncode,
                                "output": output,
                                "paths": list(changed_paths),
                            },
                        )
                    )
    return CandidateCheckRun(
        selection=selection,
        findings=tuple(findings),
        commands_run=tuple(commands_run),
        static_policy=static_policy,
        task_acceptance=task_acceptance,
    )
