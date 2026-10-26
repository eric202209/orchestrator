"""Mode B: controlled benchmark runs against the isolated development runtime.

The dry run is side-effect free: it checks isolation, the pinned canonical
seed, and the planned disposable workspaces, and is never reported as a
benchmark attempt. Live runs (Phase 40D-2) execute inside a pinned campaign;
see ``live.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts import dev_runtime

from .evidence_loader import load_cases
from .schema import RUN_CONFIG_SCHEMA_VERSION, validate_case, validate_run_config

STATUS_DRY_RUN_PREPARED = "DRY_RUN_PREPARED"
STATUS_DRY_RUN_BLOCKED = "DRY_RUN_BLOCKED"
STATUS_INVALID_CONFIG = "INVALID_CONFIG"
PRODUCTION_QUEUE_NAMES = frozenset({"celery", ""})
WORKSPACE_PREFIX = "phase40d"


def build_run_config(case_id: str, max_attempts: int, live: bool) -> dict[str, Any]:
    return {
        "schema_version": RUN_CONFIG_SCHEMA_VERSION,
        "case_id": case_id,
        "profile": "isolated-development",
        "max_attempts": max_attempts,
        "mode": "live" if live else "dry_run",
    }


def _git_read(repo: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def isolation_checks(config: dev_runtime.RuntimeConfig) -> list[dict[str, Any]]:
    host = urlsplit(config.api_url).hostname
    return [
        {
            "check": "api_loopback",
            "passed": host in {"127.0.0.1", "localhost", "::1"},
            "detail": config.api_url,
        },
        {
            "check": "dedicated_queue",
            "passed": config.queue_name not in PRODUCTION_QUEUE_NAMES,
            "detail": config.queue_name,
        },
        {
            "check": "runtime_root_inside_dev_runtime",
            "passed": config.root.is_relative_to(config.repo_root / ".dev-runtime"),
            "detail": str(config.root),
        },
        {
            "check": "authorized_inference_endpoint_pinned",
            "passed": dev_runtime.AUTHORIZED_OLLAMA_URL
            == "http://host.docker.internal:11434",
            "detail": dev_runtime.AUTHORIZED_OLLAMA_URL,
        },
    ]


def _runtime_state(config: dev_runtime.RuntimeConfig) -> str:
    """Read the recorded runtime status without probing any process or port."""

    path = config.root / dev_runtime.STATE_FILE
    if not path.is_file():
        return "absent"
    try:
        return str(
            json.loads(path.read_text(encoding="utf-8")).get("status") or "unknown"
        )
    except (OSError, json.JSONDecodeError):
        return "unreadable"


def dry_run(
    run_config: dict[str, Any],
    *,
    repo_root: Path,
    cases: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and plan a controlled run without side effects."""

    errors = validate_run_config(run_config)
    if errors:
        return {
            "status": STATUS_INVALID_CONFIG,
            "is_benchmark_attempt": False,
            "errors": errors,
        }
    if run_config["mode"] != "dry_run":
        return {
            "status": STATUS_INVALID_CONFIG,
            "is_benchmark_attempt": False,
            "errors": ["live mode requires run_live and a campaign"],
        }

    cases = load_cases() if cases is None else cases
    blockers: list[str] = []
    case = cases.get(run_config["case_id"])
    if case is None:
        return {
            "status": STATUS_INVALID_CONFIG,
            "is_benchmark_attempt": False,
            "errors": [f"unknown case_id: {run_config['case_id']}"],
        }
    case_errors = validate_case(case)
    if case_errors:
        blockers.extend(f"case schema: {error}" for error in case_errors)
    if case["status"] != "complete":
        blockers.append(f"case incomplete: {'; '.join(case['incomplete_reasons'])}")

    config = dev_runtime.RuntimeConfig.for_repo(repo_root)
    checks = isolation_checks(config)
    blockers.extend(
        f"isolation: {check['check']}" for check in checks if not check["passed"]
    )

    seed_repo = config.root / "workspace" / case["seed"]["repository"]
    commit = _git_read(
        seed_repo, "rev-parse", "--verify", f"{case['seed']['commit']}^{{commit}}"
    )
    tree = _git_read(seed_repo, "rev-parse", f"{case['seed']['commit']}^{{tree}}")
    head = _git_read(seed_repo, "rev-parse", "HEAD")
    dirty = _git_read(seed_repo, "status", "--porcelain")
    seed = {
        "repository": str(seed_repo),
        "commit": case["seed"]["commit"],
        "commit_present": commit == case["seed"]["commit"],
        "tree_matches": tree == case["seed"]["tree"],
        "head": head,
        "worktree_changes": sorted(
            line.split(None, 1)[-1] for line in (dirty or "").splitlines() if line
        ),
        "used_as": "read-only clone source; never modified",
    }
    if not seed["commit_present"]:
        blockers.append("canonical seed commit unavailable")
    elif not seed["tree_matches"]:
        blockers.append("canonical seed tree mismatch")

    planned = []
    index = 1
    while len(planned) < run_config["max_attempts"]:
        name = f"{WORKSPACE_PREFIX}-{case['case_id']}-{index}"
        if not (config.root / "workspace" / name).exists():
            planned.append(name)
        index += 1

    runtime_status = _runtime_state(config)
    return {
        "status": STATUS_DRY_RUN_BLOCKED if blockers else STATUS_DRY_RUN_PREPARED,
        "is_benchmark_attempt": False,
        "run_config": run_config,
        "blockers": blockers,
        "isolation_checks": checks,
        "seed": seed,
        "runtime": {
            "root": str(config.root),
            "api_url": config.api_url,
            "queue": config.queue_name,
            "recorded_status": runtime_status,
            "probed": False,
        },
        "planned_attempts": [
            {
                "attempt_index": number,
                "disposable_workspace": name,
                "steps": [
                    f"clone {seed_repo.name} at {case['seed']['commit'][:12]} into workspace/{name}",
                    "POST /api/v1/projects (eval@local.dev, fresh Project)",
                    "POST /api/v1/tasks with the case's original description",
                    "POST /api/v1/tasks/{id}/execute (fresh Task and TaskExecution)",
                    "poll until terminal; one active inference task at a time",
                    "capture held ChangeSet manifest (WORKSPACE_REVIEW_POLICY=hold_all)",
                    "apply ChangeSet to a fresh seed clone and run the original acceptance command",
                ],
            }
            for number, name in enumerate(planned, start=1)
        ],
        "acceptance_command": case["acceptance"]["command"],
        "would_execute": False,
    }


def run_live(
    run_config: dict[str, Any],
    *,
    repo_root: Path,
    campaign_id: str | None,
    output_root: Path,
    resume_campaign: str | None = None,
    context: Any = None,
) -> dict[str, Any]:
    """Run a case's budgeted attempts inside an existing campaign; fail closed."""

    from . import live

    errors = validate_run_config(run_config)
    if run_config.get("mode") != "live":
        errors.append("mode must be live")
    if not campaign_id:
        errors.append("a live run requires --campaign (create one with campaign-init)")
    if errors:
        return {
            "status": STATUS_INVALID_CONFIG,
            "is_benchmark_attempt": False,
            "errors": errors,
        }
    ctx = context or live.LiveContext(
        repo_root=repo_root, output_root=output_root, cases=load_cases()
    )
    return live.run_case(
        ctx,
        campaign_id,
        run_config["case_id"],
        run_config["max_attempts"],
        resume_campaign=resume_campaign,
    )
