#!/usr/bin/env python3
"""Run the existing first-slice orchestrator eval cases through the API queue."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
from urllib import error, request


SUPPORTED_CASES = frozenset(
    {
        "python_cli_small_feature",
        "medium_cli_multi_file_feature",
        "debug_import_error_repair",
        "checkpoint_resume_mid_task",
    }
)
TERMINAL_SESSION_STATUSES = frozenset(
    {
        "completed",
        "stopped",
        "failed",
        "cancelled",
        "canceled",
        "paused",
        "awaiting_input",
    }
)
FIXTURE_PROMPT_FILENAMES = ("task_prompt.txt", "prompt.txt")
STABLE_PRIMARY_FAILURE_PHASE_THRESHOLD = 0.8
TERMINAL_SUCCESS_EVENT = "task_completed"


class AuthExpiredError(RuntimeError):
    """Raised when the API bearer token expires during a long eval run."""


def _default_python(repo_root: Path) -> str:
    """Return the interpreter the scorer should use for verifier commands."""

    repo_venv_python = repo_root / "venv" / "bin" / "python"
    if repo_venv_python.is_file():
        return str(repo_venv_python)
    return sys.executable or "python3"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Manifest not found: {path}") from None
    if not isinstance(payload, dict):
        raise SystemExit(f"Manifest root must be an object: {path}")
    return payload


def _json_default(value: Any) -> str:
    return str(value)


def _select_case(manifest: dict[str, Any], case_id: str) -> dict[str, Any]:
    if case_id not in SUPPORTED_CASES:
        raise SystemExit(
            f"Unsupported case {case_id!r}; supported cases: {', '.join(sorted(SUPPORTED_CASES))}"
        )
    for case in manifest.get("cases") or []:
        if isinstance(case, dict) and case.get("case_id") == case_id:
            return case
    raise SystemExit(f"Case {case_id!r} not found in manifest")


def _extract_fenced_prompt_after_heading(markdown: str, heading: str) -> str | None:
    marker_index = markdown.lower().find(heading.lower())
    if marker_index < 0:
        return None
    after_heading = markdown[marker_index + len(heading) :]
    fence_index = after_heading.find("```")
    if fence_index < 0:
        return None
    after_opening_fence = after_heading[fence_index + 3 :]
    first_newline = after_opening_fence.find("\n")
    if first_newline < 0:
        return None
    prompt_start = first_newline + 1
    closing_fence = after_opening_fence.find("```", prompt_start)
    if closing_fence < 0:
        return None
    prompt = after_opening_fence[prompt_start:closing_fence].strip()
    return prompt or None


def _fixture_prompt(fixture_dir: Path) -> str | None:
    for filename in FIXTURE_PROMPT_FILENAMES:
        prompt_path = fixture_dir / filename
        if prompt_path.is_file():
            prompt = prompt_path.read_text(encoding="utf-8").strip()
            if prompt:
                return prompt

    readme_path = fixture_dir / "README.md"
    if not readme_path.is_file():
        return None
    readme = readme_path.read_text(encoding="utf-8")
    return _extract_fenced_prompt_after_heading(readme, "Suggested task prompt:")


def _task_prompt_for_case(case: dict[str, Any], fixture_dir: Path) -> tuple[str, str]:
    prompt = _fixture_prompt(fixture_dir)
    if prompt:
        return prompt, "fixture"
    return str(case.get("operator_prompt") or ""), "manifest"


def _api_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/api/v1"):
        return f"{base}/{path.lstrip('/')}"
    return f"{base}/api/v1/{path.lstrip('/')}"


def _request_json(
    method: str,
    base_url: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(
        _api_url(base_url, path), data=body, headers=headers, method=method
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise AuthExpiredError(
                f"auth_expired: {method} {path} returned HTTP 401. "
                "The worker may still be running independently; refusing to score "
                "until a fresh token can observe terminal state."
            ) from exc
        raise SystemExit(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise SystemExit(f"{method} {path} failed: {exc.reason}") from exc
    if not raw.strip():
        return {}
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise SystemExit(f"{method} {path} returned non-object JSON")
    return decoded


def _fresh_workspace(
    root: Path, case_id: str, fixture_dir: Path, timestamp: str
) -> Path:
    if not fixture_dir.is_dir():
        raise SystemExit(f"Fixture directory not found for {case_id}: {fixture_dir}")
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / f"{case_id.replace('_', '-')}-{timestamp}"
    if workspace.exists():
        raise SystemExit(f"Refusing to overwrite existing workspace: {workspace}")
    shutil.copytree(fixture_dir, workspace)
    _make_shared_workspace_tree(workspace)
    return workspace


def _make_shared_workspace_path(path: Path) -> None:
    if path.is_dir():
        path.chmod(0o777)
    elif path.exists():
        path.chmod(0o666)


def _make_shared_workspace_tree(path: Path) -> None:
    _make_shared_workspace_path(path)
    for child in path.rglob("*"):
        _make_shared_workspace_path(child)


def _wait_for_terminal_session(
    *,
    base_url: str,
    token: str,
    session_id: int,
    timeout_seconds: int,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_session: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_session = _request_json("GET", base_url, f"sessions/{session_id}", token)
        status = str(last_session.get("status") or "").lower()
        if status in TERMINAL_SESSION_STATUSES:
            return last_session
        time.sleep(poll_seconds)
    raise SystemExit(
        f"Timed out waiting for session {session_id} terminal state; "
        f"last status={last_session.get('status')!r}"
    )


def _event_journal_path(workspace: Path, session_id: int, task_id: int) -> Path:
    return workspace / ".openclaw/events" / f"session_{session_id}_task_{task_id}.jsonl"


def _event_types(path: Path) -> set[str]:
    event_types: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return event_types
    except OSError:
        return event_types
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event_type"):
            event_types.add(str(payload["event_type"]))
    return event_types


def _file_signature(path: Path) -> tuple[bool, int, int]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (False, 0, 0)
    return (True, stat.st_size, stat.st_mtime_ns)


def _required_terminal_event(session_status: str) -> str | None:
    status = str(session_status or "").lower()
    if status == "completed":
        return TERMINAL_SUCCESS_EVENT
    if status in {"failed", "cancelled", "canceled"}:
        return "task_failed"
    return None


def _wait_for_scoreable_event_journal(
    *,
    workspace: Path,
    session_id: int,
    task_id: int,
    session_status: str,
    timeout_seconds: float,
    stable_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    """Wait until scoring cannot race final event emission.

    Completed sessions must have a `task_completed` event before scoring. Other
    terminal states may not emit a single canonical task event, so the runner
    waits until the journal stops changing.
    """

    path = _event_journal_path(workspace, session_id, task_id)
    required_event = _required_terminal_event(session_status)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_signature: tuple[bool, int, int] | None = None
    stable_since: float | None = None
    observed_events: set[str] = set()

    while True:
        observed_events = _event_types(path)
        if required_event and required_event in observed_events:
            return {
                "event_journal_path": str(path),
                "required_terminal_event": required_event,
                "observed_terminal_event": required_event,
                "stabilized": True,
            }

        signature = _file_signature(path)
        now = time.monotonic()
        if signature == last_signature:
            if stable_since is None:
                stable_since = now
        else:
            stable_since = now
            last_signature = signature

        if not required_event and stable_since is not None:
            if now - stable_since >= stable_seconds:
                return {
                    "event_journal_path": str(path),
                    "required_terminal_event": None,
                    "observed_terminal_event": None,
                    "stabilized": True,
                }

        if now >= deadline:
            if required_event:
                raise SystemExit(
                    "terminal_event_missing: session reached "
                    f"{session_status!r}, but {required_event!r} was not observed in "
                    f"{path}. Refusing to score a potentially stale workspace."
                )
            raise SystemExit(
                f"Timed out waiting for event journal stabilization: {path}"
            )

        time.sleep(max(0.0, poll_seconds))


def _run_scorer(
    *,
    repo_root: Path,
    manifest: Path,
    case_id: str,
    workspace: Path,
    session_id: int,
    task_id: int,
    python: str,
    output: Path,
) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    _make_shared_workspace_path(output.parent)
    cmd = [
        python,
        str(repo_root / "scripts/score_orchestrator_eval_case.py"),
        "--manifest",
        str(manifest),
        "--case-id",
        case_id,
        "--project-dir",
        str(workspace),
        "--session-id",
        str(session_id),
        "--task-id",
        str(task_id),
        "--python",
        python,
        "--output",
        str(output),
    ]
    completed = subprocess.run(cmd, cwd=repo_root, check=False)
    if completed.returncode not in {0, 1}:
        raise subprocess.CalledProcessError(completed.returncode, cmd)
    _make_shared_workspace_path(output)
    return completed.returncode


def _git_sha(repo_root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    return sha or None


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _run_context_metadata(
    *,
    repo_root: Path,
    repeat_seed: str | None,
) -> dict[str, Any]:
    planner_model = _first_env(
        (
            "PLANNER_MODEL",
            "ORCHESTRATOR_AGENT_MODEL_FAMILY",
            "AGENT_MODEL",
            "OLLAMA_AGENT_MODEL",
        )
    )
    planning_repair_model = _first_env(
        (
            "PLANNING_REPAIR_MODEL",
            "ORCHESTRATOR_PLANNING_REPAIR_DIRECT_MODEL",
            "AGENT_MODEL",
        )
    )
    debug_repair_model = _first_env(
        (
            "DEBUG_REPAIR_MODEL",
            "PHASE7F_REPAIR_MODEL",
            "PLANNING_REPAIR_MODEL",
            "AGENT_MODEL",
        )
    )
    execution_model = _first_env(
        ("EXECUTION_MODEL", "OLLAMA_AGENT_MODEL", "AGENT_MODEL")
    )
    return {
        "git_sha": _git_sha(repo_root),
        "model": _first_env(
            (
                "ORCHESTRATOR_AGENT_MODEL_FAMILY",
                "AGENT_MODEL",
                "OLLAMA_AGENT_MODEL",
            )
        ),
        "backend": _first_env(("ORCHESTRATOR_AGENT_BACKEND", "AGENT_BACKEND")),
        "runtime_profile": _first_env(
            ("ORCHESTRATOR_RUNTIME_PROFILE", "RUNTIME_PROFILE")
        ),
        "planner_model": planner_model,
        "planner_backend": _first_env(
            ("PLANNING_BACKEND", "ORCHESTRATOR_PLANNING_BACKEND", "AGENT_BACKEND")
        ),
        "planning_repair_model": planning_repair_model,
        "planning_repair_backend": _first_env(
            (
                "PLANNING_REPAIR_BACKEND",
                "PLANNING_BACKEND",
                "ORCHESTRATOR_PLANNING_BACKEND",
                "AGENT_BACKEND",
            )
        ),
        "debug_repair_model": debug_repair_model,
        "debug_repair_backend": _first_env(
            (
                "DEBUG_REPAIR_BACKEND",
                "REPAIR_BACKEND",
                "AGENT_BACKEND",
            )
        ),
        "execution_model": execution_model,
        "execution_backend": _first_env(
            ("EXECUTION_BACKEND", "ORCHESTRATOR_EXECUTION_BACKEND", "AGENT_BACKEND")
        ),
        "evaluation_model": _first_env(("EVALUATION_MODEL",)),
        "evaluation_backend": _first_env(("EVALUATION_BACKEND",)),
        "repeat_seed": repeat_seed,
    }


def _clean_success(report: dict[str, Any]) -> bool:
    result = report.get("result") or {}
    return bool(result.get("clean_success"))


def _result_bool(report: dict[str, Any], name: str) -> bool:
    result = report.get("result") or {}
    return bool(result.get(name))


def _path_bool(report: dict[str, Any], name: str) -> bool:
    path_observability = report.get("path_observability") or {}
    return bool(path_observability.get(name))


def _path_any_bool(report: dict[str, Any], *names: str) -> bool:
    path_observability = report.get("path_observability") or {}
    return any(bool(path_observability.get(name)) for name in names)


def _primary_failure_phase(report: dict[str, Any]) -> str:
    path_observability = report.get("path_observability") or {}
    phase = path_observability.get("primary_failure_phase")
    if phase:
        return str(phase)
    if _clean_success(report):
        return "clean_success"
    return "unknown"


def _most_common(counter: Counter[str]) -> str | None:
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _blocker_key(report: dict[str, Any]) -> str:
    result = report.get("result") or {}
    blockers = result.get("blockers")
    if isinstance(blockers, list) and blockers:
        return str(blockers[0])
    return _primary_failure_phase(report)


def _rate(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return count / total


def _aggregate_score_readiness(
    score_readiness: list[dict[str, Any]],
    repeat_count: int,
) -> dict[str, Any]:
    readiness_count = len(score_readiness)
    stabilized_count = sum(
        1 for readiness in score_readiness if readiness.get("stabilized") is True
    )
    required_terminal_event_count = sum(
        1 for readiness in score_readiness if readiness.get("required_terminal_event")
    )
    required_terminal_event_observed_count = sum(
        1
        for readiness in score_readiness
        if readiness.get("required_terminal_event")
        and readiness.get("observed_terminal_event")
        == readiness.get("required_terminal_event")
    )
    terminal_event_observed_count = sum(
        1 for readiness in score_readiness if readiness.get("observed_terminal_event")
    )
    terminal_event_missing_count = (
        required_terminal_event_count - required_terminal_event_observed_count
    )
    journal_paths = [
        str(readiness["event_journal_path"])
        for readiness in score_readiness
        if readiness.get("event_journal_path")
    ]
    event_distribution = Counter(
        str(readiness["observed_terminal_event"])
        for readiness in score_readiness
        if readiness.get("observed_terminal_event")
    )
    return {
        "all_runs_scoreable": (
            readiness_count == repeat_count
            and stabilized_count == repeat_count
            and terminal_event_missing_count == 0
        ),
        "readiness_recorded_count": readiness_count,
        "stabilized_count": stabilized_count,
        "stabilization_missing_count": repeat_count - stabilized_count,
        "required_terminal_event_count": required_terminal_event_count,
        "required_terminal_event_observed_count": (
            required_terminal_event_observed_count
        ),
        "terminal_event_observed_count": terminal_event_observed_count,
        "terminal_event_missing_count": terminal_event_missing_count,
        "observed_terminal_event_distribution": dict(
            sorted(event_distribution.items())
        ),
        "journal_paths": journal_paths,
        "journal_path_count": len(journal_paths),
    }


def _aggregate_case_reports(
    *,
    case_id: str,
    reports: list[dict[str, Any]],
    report_paths: list[Path],
    run_context: dict[str, Any],
    score_readiness: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    repeat_count = len(reports)
    phase_distribution = Counter(_primary_failure_phase(report) for report in reports)
    blocker_distribution = Counter(_blocker_key(report) for report in reports)
    clean_success_count = sum(1 for report in reports if _clean_success(report))
    path_observed_count = sum(
        1 for report in reports if _result_bool(report, "path_observed")
    )
    intended_path_observed_count = sum(
        1 for report in reports if _path_bool(report, "intended_path_observed")
    )
    execution_reached_count = sum(
        1 for report in reports if _path_bool(report, "execution_reached")
    )
    debug_repair_reached_count = sum(
        1 for report in reports if _path_bool(report, "debug_repair_reached")
    )
    phase7f_used_count = sum(
        1
        for report in reports
        if _path_any_bool(
            report,
            "phase7f_used",
            "bounded_execution_debug_repair_used",
        )
    )
    phase7g_used_count = sum(
        1
        for report in reports
        if _path_any_bool(
            report,
            "phase7g_used",
            "diff_scoped_debug_repair_used",
        )
    )
    top_phase_count = phase_distribution.most_common(1)[0][1] if repeat_count else 0
    stable_primary_failure_phase = (
        _rate(top_phase_count, repeat_count) >= STABLE_PRIMARY_FAILURE_PHASE_THRESHOLD
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "tool": "scripts/evals/run_orchestrator_eval_slice.py",
        "case_id": case_id,
        "repeat_count": repeat_count,
        **run_context,
        "clean_success_count": clean_success_count,
        "clean_success_rate": _rate(clean_success_count, repeat_count),
        "primary_failure_phase_distribution": dict(sorted(phase_distribution.items())),
        "stable_primary_failure_phase": stable_primary_failure_phase,
        "path_observed_count": path_observed_count,
        "path_observed_rate": _rate(path_observed_count, repeat_count),
        "intended_path_observed_count": intended_path_observed_count,
        "intended_path_observed_rate": _rate(
            intended_path_observed_count, repeat_count
        ),
        "execution_reached_count": execution_reached_count,
        "execution_reached_rate": _rate(execution_reached_count, repeat_count),
        "debug_repair_reached_count": debug_repair_reached_count,
        "debug_repair_reached_rate": _rate(debug_repair_reached_count, repeat_count),
        "phase7f_used_count": phase7f_used_count,
        "bounded_execution_debug_repair_used_count": phase7f_used_count,
        "phase7g_used_count": phase7g_used_count,
        "diff_scoped_debug_repair_used_count": phase7g_used_count,
        "phase7f_exercised_rate": _rate(phase7f_used_count, repeat_count),
        "bounded_execution_debug_repair_exercised_rate": _rate(
            phase7f_used_count, repeat_count
        ),
        "phase7g_exercised_rate": _rate(phase7g_used_count, repeat_count),
        "diff_scoped_debug_repair_exercised_rate": _rate(
            phase7g_used_count, repeat_count
        ),
        "most_common_blocker": _most_common(blocker_distribution),
        "blocker_distribution": dict(sorted(blocker_distribution.items())),
        "score_readiness_summary": _aggregate_score_readiness(
            score_readiness or [],
            repeat_count,
        ),
        "run_report_paths": [str(path) for path in report_paths],
    }


def _write_json_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _make_shared_workspace_path(path.parent)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )
    _make_shared_workspace_path(path)


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Run existing orchestrator eval first-slice cases through the API queue."
    )
    case_group = parser.add_mutually_exclusive_group(required=True)
    case_group.add_argument("--case-id", choices=sorted(SUPPORTED_CASES))
    case_group.add_argument("--cases", nargs="+", choices=sorted(SUPPORTED_CASES))
    parser.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:8080/api/v1",
        help="Orchestrator API base URL. May include or omit /api/v1.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("ORCHESTRATOR_API_TOKEN"),
        help="Bearer token for the normal authenticated API.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repo_root / "scripts/evals/orchestrator-eval-v1-manifest.json",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=repo_root / "scripts/evals/fixtures",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path("/home/eric/projects"),
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=repo_root / "docs/roadmap/reports/evals",
    )
    parser.add_argument(
        "--python",
        "--venv-python",
        default=_default_python(repo_root),
        help=(
            "Python executable used to run the scorer and verifier commands. "
            "Defaults to ./venv/bin/python when present, otherwise this runner's "
            "current sys.executable."
        ),
    )
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument(
        "--event-stabilization-timeout-seconds",
        type=float,
        default=120.0,
        help=(
            "Maximum time to wait after API terminal state for the event journal "
            "to contain its terminal event before scoring."
        ),
    )
    parser.add_argument(
        "--event-stable-seconds",
        type=float,
        default=2.0,
        help=(
            "For non-success terminal states without a required final event, wait "
            "this long with no event journal changes before scoring."
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Run each selected case this many times before aggregating results.",
    )
    parser.add_argument(
        "--repeat-seed",
        help=(
            "Optional seed label recorded in aggregate reports. The current runner "
            "does not force model determinism from this value."
        ),
    )
    parser.add_argument(
        "--print-prompts",
        action="store_true",
        help="Print selected task prompts and exit without creating API records.",
    )
    return parser.parse_args()


def _run_case(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    manifest: dict[str, Any],
    case_id: str,
    token: str,
    run_index: int,
    repeat_count: int,
) -> dict[str, Any]:
    case = _select_case(manifest, case_id)
    fixture_dir = args.fixtures_dir / case_id
    task_prompt, prompt_source = _task_prompt_for_case(case, fixture_dir)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if repeat_count > 1:
        timestamp = f"{timestamp}-r{run_index:02d}"
    workspace = _fresh_workspace(
        args.workspace_root,
        case_id,
        fixture_dir,
        timestamp,
    )

    project = _request_json(
        "POST",
        args.api_base_url,
        "projects",
        token,
        {
            "name": f"Eval {case_id} {timestamp}",
            "description": f"Eval first-slice case {case_id}",
            "workspace_path": str(workspace),
        },
    )
    project_id = int(project["id"])
    task = _request_json(
        "POST",
        args.api_base_url,
        "tasks",
        token,
        {
            "project_id": project_id,
            "title": case_id.replace("_", " "),
            "description": task_prompt,
            "priority": 0,
            "plan_position": 1,
        },
    )
    task_id = int(task["id"])
    session = _request_json(
        "POST",
        args.api_base_url,
        "sessions",
        token,
        {
            "project_id": project_id,
            "name": f"Eval {case_id} {timestamp}",
            "execution_mode": "manual",
            "default_execution_profile": "full_lifecycle",
        },
    )
    session_id = int(session["id"])
    _request_json(
        "POST",
        args.api_base_url,
        f"sessions/{session_id}/tasks/{task_id}/run",
        token,
    )
    final_session = _wait_for_terminal_session(
        base_url=args.api_base_url,
        token=token,
        session_id=session_id,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    score_readiness = _wait_for_scoreable_event_journal(
        workspace=workspace,
        session_id=session_id,
        task_id=task_id,
        session_status=str(final_session.get("status") or ""),
        timeout_seconds=args.event_stabilization_timeout_seconds,
        stable_seconds=args.event_stable_seconds,
        poll_seconds=min(args.poll_seconds, 1.0),
    )
    report = args.reports_dir / (
        f"orchestrator-eval-v1-{case_id.replace('_', '-')}-queue-{timestamp}.json"
    )
    scorer_exit_code = _run_scorer(
        repo_root=repo_root,
        manifest=args.manifest,
        case_id=case_id,
        workspace=workspace,
        session_id=session_id,
        task_id=task_id,
        python=args.python,
        output=report,
    )
    return {
        "case_id": case_id,
        "workspace": str(workspace),
        "project_id": project_id,
        "session_id": session_id,
        "task_id": task_id,
        "session_status": final_session.get("status"),
        "report": str(report),
        "scorer_exit_code": scorer_exit_code,
        "prompt_source": prompt_source,
        "scorer_python": args.python,
        "score_readiness": score_readiness,
    }


def main() -> int:
    args = parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    repo_root = Path(__file__).resolve().parents[2]
    manifest = _load_json(args.manifest)
    case_ids = args.cases or [args.case_id]
    if args.print_prompts:
        prompt_preview = []
        for case_id in case_ids:
            case = _select_case(manifest, case_id)
            prompt, source = _task_prompt_for_case(case, args.fixtures_dir / case_id)
            prompt_preview.append(
                {
                    "case_id": case_id,
                    "prompt_source": source,
                    "prompt": prompt,
                }
            )
        print(json.dumps({"prompts": prompt_preview}, indent=2))
        return 0

    token = args.token
    if not token:
        raise SystemExit(
            "Missing --token. Pass a normal API bearer token or set ORCHESTRATOR_API_TOKEN."
        )

    run_timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_context = _run_context_metadata(
        repo_root=repo_root,
        repeat_seed=args.repeat_seed,
    )
    try:
        results = []
        aggregate_reports = []
        for case_id in case_ids:
            case_results = []
            for run_index in range(1, args.repeat + 1):
                case_results.append(
                    _run_case(
                        args=args,
                        repo_root=repo_root,
                        manifest=manifest,
                        case_id=case_id,
                        token=token,
                        run_index=run_index,
                        repeat_count=args.repeat,
                    )
                )
            results.extend(case_results)
            if args.repeat > 1:
                report_paths = [Path(result["report"]) for result in case_results]
                reports = [_load_json(path) for path in report_paths]
                aggregate_payload = _aggregate_case_reports(
                    case_id=case_id,
                    reports=reports,
                    report_paths=report_paths,
                    run_context=run_context,
                    score_readiness=[
                        result["score_readiness"] for result in case_results
                    ],
                )
                aggregate_report = args.reports_dir / (
                    f"orchestrator-eval-v1-{case_id.replace('_', '-')}-queue-"
                    f"{run_timestamp}-aggregate.json"
                )
                _write_json_report(aggregate_report, aggregate_payload)
                aggregate_reports.append(str(aggregate_report))
    except AuthExpiredError as exc:
        raise SystemExit(str(exc)) from None
    baseline_report = args.reports_dir / (
        f"orchestrator-eval-v1-baseline-queue-{run_timestamp}.json"
    )
    baseline_report.parent.mkdir(parents=True, exist_ok=True)
    _make_shared_workspace_path(baseline_report.parent)
    baseline_payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "tool": "scripts/evals/run_orchestrator_eval_slice.py",
        "manifest": {
            "path": str(args.manifest),
            "benchmark_id": manifest.get("benchmark_id"),
            "baseline_label": manifest.get("baseline_label"),
            "schema_version": manifest.get("schema_version"),
        },
        "case_ids": case_ids,
        "repeat": args.repeat,
        "run_context": run_context,
        "results": results,
        "aggregate_reports": aggregate_reports,
    }
    _write_json_report(baseline_report, baseline_payload)
    print(
        json.dumps(
            {**baseline_payload, "baseline_report": str(baseline_report)}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
