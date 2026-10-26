"""Mode B live execution: controlled campaigns through the development API.

A campaign pins one evaluated source identity, runtime configuration, model,
benchmark implementation, and case set. Every attempt reserves a durable slot
before any request is sent, so a crash can never exceed the budget. Attempts
go through the normal API (Project -> Task -> execute) and are observed, never
driven: the benchmark does not call Planning or Execution internals. Records
are built by the same loader that evaluates history, from the live database,
event journal, ChangeSet manifest, and retained provider evidence.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from scripts import dev_runtime

from . import identity
from .evidence_loader import (
    EvidenceRoots,
    HistoryLoader,
    load_registry,
    parse_acceptance_record,
)
from .metrics import compute_metrics, grouped_metrics
from .schema import validate_attempt, validate_case

PHASE = "phase40d2"
CAMPAIGN_SCHEMA = "benchmark.campaign.v1"
PREFLIGHT_SCHEMA = "benchmark.preflight.v1"
CAMPAIGN_ID = re.compile(r"^phase40d2-[a-z0-9][a-z0-9-]{0,40}$")
LOCK_NAME = ".live-runner.lock"
TERMINAL_TASK_STATUSES = frozenset({"done", "failed", "cancelled"})
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_POLL_SECONDS = 5.0
# An execute request whose response was lost is resolved by observing whether a
# TaskExecution appeared; it is never re-sent.
UNCONFIRMED_EXECUTE_GRACE_SECONDS = 120
ACCEPTANCE_TIMEOUT_SECONDS = 600
EVALUATOR = dev_runtime.EVALUATOR_EMAIL
EXPECTED_POLICY = {
    "WORKSPACE_REVIEW_POLICY": "hold_all",
    "ONE_SHOT_EXECUTION_ENABLED": "false",
    "LOW_RESOURCE_SINGLE_MODEL": "true",
}
# The benchmark may only read, create disposable Projects/Tasks, queue them, and
# stop a timed-out session. Promotion, ChangeSet accept/reject, retries, and
# every other mutating route are unreachable through this client.
ALLOWED_REQUESTS: tuple[tuple[str, str], ...] = (
    ("GET", r"/api/v1/auth/me"),
    ("POST", r"/api/v1/projects"),
    ("POST", r"/api/v1/tasks"),
    ("POST", r"/api/v1/tasks/\d+/execute"),
    ("GET", r"/api/v1/tasks/\d+"),
    ("GET", r"/api/v1/tasks/\d+/change-set"),
    ("POST", r"/api/v1/sessions/\d+/stop"),
)
# Terminal safety rejections of content the model proposed.
MODEL_PROPOSAL_REJECTIONS = frozenset(
    {
        "apa_unauthorized_path",
        "gr2_existing_file_rewrite",
        "python_syntax_invalid",
        "python_write_file_drops_definitions",
        "semantic_string_edit_rejected",
        "plan_output_contract",
        "debug_repair_output_contract",
        "unsupported_command",
        "task_scope",
        "missing_verification",
    }
)

STATUS_CAMPAIGN_CREATED = "CAMPAIGN_CREATED"
STATUS_PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
STATUS_CASE_COMPLETE = "CASE_COMPLETE"
STATUS_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
STATUS_CAMPAIGN_TERMINATED = "CAMPAIGN_TERMINATED"
STATUS_RESUME_CONFIRMATION_REQUIRED = "RESUME_CONFIRMATION_REQUIRED"
STATUS_INVALID = "INVALID_CONFIG"
STATUS_BUSY = "RUNNER_BUSY"


class LiveError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_new(path: Path, value: Any) -> None:
    """Create ``path`` exactly once; an existing outcome is never overwritten."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_replace(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def protected_output_problem(output: Path, repo_root: Path) -> str | None:
    """Refuse historical evidence directories and runtime control state."""

    resolved = output.resolve()
    for phase in load_registry()["phases"]:
        evidence_dir = (repo_root / phase["evidence_dir"]).resolve()
        if resolved.is_relative_to(evidence_dir):
            return f"refusing to write into historical evidence directory {phase['evidence_dir']}"
    runtime = (repo_root / ".dev-runtime" / "phase40b-r1").resolve()
    if resolved.is_relative_to(runtime):
        return "refusing to write into the development runtime"
    return None


@contextlib.contextmanager
def exclusive_runner(output_root: Path) -> Iterator[None]:
    """One live runner at a time; a second process fails instead of waiting."""

    output_root.mkdir(parents=True, exist_ok=True)
    with (output_root / LOCK_NAME).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LiveError(
                "runner_busy", "another live runner holds the lock"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# -- development API ---------------------------------------------------------


class DevApiClient:
    """HTTP client pinned to the isolated development API and an allowlist."""

    def __init__(
        self,
        config: dev_runtime.RuntimeConfig,
        token_source: Callable[[], str],
        http: Callable[..., tuple[int, Any]] | None = None,
    ):
        parts = urlsplit(config.api_url)
        if (
            parts.scheme != "http"
            or parts.hostname != "127.0.0.1"
            or parts.port != dev_runtime.API_PORT
            or config.api_url != f"http://127.0.0.1:{dev_runtime.API_PORT}"
        ):
            raise LiveError(
                "non_development_api_refused",
                f"{config.api_url} is not the isolated development API",
            )
        self.base = config.api_url
        self._token_source = token_source
        self._token: str | None = None
        self._http = http or dev_runtime._http_json
        self.audit: list[dict[str, Any]] = []

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        if not any(
            method == allowed_method and re.fullmatch(pattern, path)
            for allowed_method, pattern in ALLOWED_REQUESTS
        ):
            raise LiveError("request_not_allowed", f"{method} {path}")
        status: int = 0
        body: Any = None
        for attempt in range(2):
            if self._token is None:
                self._token = self._token_source()
            status, body = self._http(
                f"{self.base}{path}", method=method, token=self._token, payload=payload
            )
            if status == 401 and attempt == 0:
                self._token = None  # short-lived bearer expired; mint once more
                continue
            break
        self.audit.append(
            {"method": method, "path": path, "status": status, "at": _utc()}
        )
        return status, body


def evaluator_token_source(
    config: dev_runtime.RuntimeConfig, api_pid: Callable[[], int]
) -> Callable[[], str]:
    """Mint the existing evaluator's bearer the way the launcher does.

    The signing key is read from the owned API process at mint time and is
    held only in this call's memory; neither key nor token is persisted.
    """

    def mint() -> str:
        secret = identity.read_process_secret(api_pid(), "SECRET_KEY")
        if len(secret) < 32:
            raise LiveError("auth_unavailable", "API signing key unavailable")
        env = dev_runtime.build_environment(config, secret_key=secret)
        dev_runtime.validate_isolation(config, env)
        return dev_runtime._mint_dev_token(config, env)

    return mint


# -- runtime observation -----------------------------------------------------


def observe_runtime(config: dev_runtime.RuntimeConfig) -> dict[str, Any]:
    """Prove launcher ownership and read API/Worker identity from /proc."""

    status = dev_runtime.status_report(config)
    state = _read_json(config.root / dev_runtime.STATE_FILE)
    processes: dict[str, Any] = {}
    problems: list[str] = []
    for kind in ("api", "worker", "redis"):
        record = (state.get("processes") or {}).get(kind)
        if not record:
            problems.append(f"{kind}: not recorded by the launcher")
            continue
        ownership, reason = dev_runtime._process_identity(record)
        if ownership != "owned":
            problems.append(f"{kind}: {ownership} ({reason})")
            continue
        if kind != "redis":
            processes[kind] = identity.process_identity(int(record["pid"]))
            env = identity.read_process_environment(int(record["pid"]))
            try:
                dev_runtime.validate_isolation(config, env)
            except dev_runtime.RuntimeToolError as exc:
                problems.append(f"{kind}: isolation {exc}")
            env.clear()
    for flag in ("api_ready", "redis_ready", "worker_ready"):
        if status.get(flag) is not True:
            problems.append(f"{flag}=false")
    return {
        "status": status.get("status"),
        "ready": not problems,
        "problems": problems,
        "processes": processes,
        "queue": config.queue_name,
        "api_url": config.api_url,
    }


def model_identity(model: str) -> dict[str, Any]:
    """Read-only model listing from the authorized Ollama endpoint."""

    try:
        status, payload = dev_runtime._http_json(
            f"{dev_runtime.AUTHORIZED_OLLAMA_URL}/api/tags"
        )
    except OSError as exc:
        return {"model": model, "available": False, "error": type(exc).__name__}
    entry = next(
        (
            item
            for item in (payload or {}).get("models") or []
            if item.get("name") == model or item.get("model") == model
        ),
        None,
    )
    return {
        "model": model,
        "endpoint": f"{dev_runtime.AUTHORIZED_OLLAMA_URL}/v1",
        "available": status == 200 and entry is not None,
        "digest": (entry or {}).get("digest"),
        "size": (entry or {}).get("size"),
        "modified_at": (entry or {}).get("modified_at"),
        "details": (entry or {}).get("details"),
    }


def active_tasks(config: dev_runtime.RuntimeConfig) -> list[dict[str, Any]]:
    """Tasks that are queued or running anywhere in the development database."""

    if not config.database_file.is_file():
        return []
    conn = sqlite3.connect(f"file:{config.database_file}?mode=ro", uri=True)
    try:
        return [
            {"task_id": row[0], "status": row[1]}
            for row in conn.execute(
                "select id, status from tasks where lower(status) in ('pending', 'running')"
            )
        ]
    finally:
        conn.close()


def _db_rows(
    config: dev_runtime.RuntimeConfig, task_id: int
) -> dict[str, list[dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{config.database_file}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        executions = [
            dict(row)
            for row in conn.execute(
                "select * from task_executions where task_id=? order by id", (task_id,)
            )
        ]
        sessions = sorted(
            {row["session_id"] for row in executions if row["session_id"]}
        )
        return {
            "tasks": [
                dict(row)
                for row in conn.execute("select * from tasks where id=?", (task_id,))
            ],
            "task_executions": executions,
            "sessions": [
                dict(row)
                for session_id in sessions
                for row in conn.execute(
                    "select id, project_id, status, execution_mode, created_at, updated_at "
                    "from sessions where id=?",
                    (session_id,),
                )
            ],
        }
    finally:
        conn.close()


@dataclass
class LiveHooks:
    """Side-effecting dependencies, replaceable in deterministic tests."""

    observe_runtime: Callable[[dev_runtime.RuntimeConfig], dict[str, Any]] = (
        observe_runtime
    )
    source_identity: Callable[[Path], dict[str, Any]] = identity.source_identity
    benchmark_identity: Callable[[Path], dict[str, Any]] = identity.benchmark_identity
    dependency_identity: Callable[[], dict[str, Any]] = identity.dependency_identity
    model_identity: Callable[[str], dict[str, Any]] = model_identity
    active_tasks: Callable[[dev_runtime.RuntimeConfig], list[dict[str, Any]]] = (
        active_tasks
    )
    db_rows: Callable[..., dict[str, list[dict[str, Any]]]] = _db_rows
    api_factory: Callable[..., DevApiClient] | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


@dataclass
class LiveContext:
    repo_root: Path
    output_root: Path
    cases: dict[str, dict[str, Any]]
    hooks: LiveHooks = field(default_factory=LiveHooks)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    python: str = sys.executable

    @property
    def config(self) -> dev_runtime.RuntimeConfig:
        return dev_runtime.RuntimeConfig.for_repo(self.repo_root)

    def campaign_dir(self, campaign_id: str) -> Path:
        return self.output_root / "campaigns" / campaign_id

    def api(self, runtime: dict[str, Any]) -> DevApiClient:
        if self.hooks.api_factory is not None:
            return self.hooks.api_factory(self.config, runtime)
        api_pid = int(runtime["processes"]["api"]["pid"])
        return DevApiClient(
            self.config, evaluator_token_source(self.config, lambda: api_pid)
        )


# -- seeds and source identity -----------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check and result.returncode != 0:
        raise LiveError("git_failed", f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def seed_state(
    config: dev_runtime.RuntimeConfig, case: dict[str, Any]
) -> dict[str, Any]:
    """Read-only state of a canonical seed repository."""

    repo = config.root / "workspace" / case["seed"]["repository"]
    commit = case["seed"]["commit"]
    if not (repo / ".git").exists():
        return {"repository": str(repo), "present": False}
    return {
        "repository": str(repo),
        "present": True,
        "commit_present": _git(
            repo, "rev-parse", "--verify", f"{commit}^{{commit}}", check=False
        )
        == commit,
        "tree": _git(repo, "rev-parse", f"{commit}^{{tree}}", check=False),
        "head": _git(repo, "rev-parse", "HEAD", check=False),
        "refs_sha256": hashlib.sha256(
            _git(repo, "for-each-ref", "--format=%(refname) %(objectname)").encode()
        ).hexdigest(),
        "status_porcelain": _git(repo, "status", "--porcelain", check=False),
    }


def _seed_problems(case: dict[str, Any], state: dict[str, Any]) -> list[str]:
    if not state.get("present"):
        return ["canonical seed repository missing"]
    problems = []
    if not state["commit_present"]:
        problems.append("canonical seed commit unavailable")
    if state["tree"] != case["seed"]["tree"]:
        problems.append("canonical seed tree mismatch")
    return problems


def clone_seed(
    seed_repo: Path, destination: Path, case: dict[str, Any]
) -> dict[str, Any]:
    """Fresh, independent clone at the canonical commit; the seed is read only."""

    if destination.exists():
        raise LiveError("workspace_exists", str(destination))
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(seed_repo), str(destination)],
        capture_output=True,
        timeout=120,
        check=True,
    )
    # Never leave a writable path back to the canonical seed.
    _git(
        destination,
        "remote",
        "set-url",
        "origin",
        f"https://example.invalid/{destination.name}.git",
    )
    head = _git(destination, "rev-parse", "HEAD")
    tree = _git(destination, "rev-parse", "HEAD^{tree}")
    status = _git(destination, "status", "--porcelain")
    if head != case["seed"]["commit"] or tree != case["seed"]["tree"] or status:
        raise LiveError(
            "seed_clone_mismatch",
            f"head={head} tree={tree} dirty={bool(status)} at {destination}",
        )
    for path in [destination, *destination.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o060 | (0o010 if path.is_dir() else 0))
    return {"path": str(destination), "head": head, "tree": tree, "clean": True}


# -- campaign ----------------------------------------------------------------


def _compact_source(source: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in source.items() if key != "files"}


def _runtime_projection(runtime: dict[str, Any]) -> dict[str, Any]:
    processes = runtime.get("processes") or {}
    return {
        kind: {
            key: processes[kind][key]
            for key in (
                "pid",
                "start_ticks",
                "started_at_epoch",
                "cmdline",
                "exe",
                "cwd",
                "configuration_fingerprint",
            )
        }
        for kind in sorted(processes)
    }


def _config_fingerprint(runtime: dict[str, Any]) -> str | None:
    prints = {
        process["configuration_fingerprint"]
        for process in (runtime.get("processes") or {}).values()
    }
    return prints.pop() if len(prints) == 1 else None


def init_campaign(
    ctx: LiveContext, campaign_id: str, case_ids: list[str], max_attempts: int
) -> dict[str, Any]:
    if not CAMPAIGN_ID.fullmatch(campaign_id):
        return {
            "status": STATUS_INVALID,
            "errors": [f"campaign id must match {CAMPAIGN_ID.pattern}"],
        }
    if not 1 <= max_attempts <= 2:
        return {"status": STATUS_INVALID, "errors": ["max_attempts must be 1..2"]}
    problem = protected_output_problem(ctx.output_root, ctx.repo_root)
    if problem:
        return {"status": STATUS_INVALID, "errors": [problem]}
    directory = ctx.campaign_dir(campaign_id)
    if directory.exists():
        return {"status": STATUS_INVALID, "errors": [f"campaign exists: {directory}"]}
    errors = []
    for case_id in case_ids:
        case = ctx.cases.get(case_id)
        if case is None:
            errors.append(f"unknown case: {case_id}")
        elif validate_case(case) or case["status"] != "complete":
            errors.append(f"case not runnable: {case_id}")
        else:
            errors.extend(
                f"{case_id}: {item}"
                for item in _seed_problems(case, seed_state(ctx.config, case))
            )
    runtime = ctx.hooks.observe_runtime(ctx.config)
    if not runtime["ready"]:
        errors.extend(f"runtime: {item}" for item in runtime["problems"])
    fingerprint = _config_fingerprint(runtime) if runtime["ready"] else None
    if runtime["ready"] and fingerprint is None:
        errors.append("runtime: API and Worker configuration fingerprints differ")
    if errors:
        return {"status": STATUS_INVALID, "errors": errors}

    environment = runtime["processes"]["api"]["environment"]
    source = ctx.hooks.source_identity(ctx.repo_root)
    started = min(
        process["started_at_epoch"] for process in runtime["processes"].values()
    )
    model = environment.get("PLANNER_MODEL", "")
    manifest = {
        "schema": CAMPAIGN_SCHEMA,
        "campaign_id": campaign_id,
        "phase": PHASE,
        "created_at": _utc(),
        "cases": {
            case_id: {
                "definition_sha256": hashlib.sha256(
                    json.dumps(ctx.cases[case_id], sort_keys=True).encode()
                ).hexdigest(),
                "seed": ctx.cases[case_id]["seed"],
                "seed_state": seed_state(ctx.config, ctx.cases[case_id]),
                "acceptance_command": ctx.cases[case_id]["acceptance"]["command"],
            }
            for case_id in case_ids
        },
        "budget": {case_id: max_attempts for case_id in case_ids},
        "total_budget": max_attempts * len(case_ids),
        "attempt_policy": "run every budgeted attempt sequentially; no benchmark retries",
        "source_identity": source,
        "loaded_source_evidence": {
            "runtime_started_at_epoch": started,
            "latest_evaluated_mtime_epoch": source["latest_mtime_epoch"],
            "source_predates_runtime_processes": (
                source["latest_mtime_epoch"] is not None
                and source["latest_mtime_epoch"] < started
            ),
            "pythonpath_is_checkout": environment.get("PYTHONPATH")
            == str(ctx.repo_root),
            "limitation": "loaded bytes are not read from process memory; identity is proved by an unchanged evaluated tree that predates process start and is re-hashed before and after every attempt",
        },
        "benchmark_identity": ctx.hooks.benchmark_identity(ctx.repo_root),
        "dependency_identity": ctx.hooks.dependency_identity(),
        "runtime": {
            "configuration_fingerprint": fingerprint,
            "environment": environment,
            "processes": _runtime_projection(runtime),
            "queue": runtime["queue"],
            "api_url": runtime["api_url"],
        },
        "provider": ctx.hooks.model_identity(model),
    }
    policy_errors = _policy_problems(environment)
    if policy_errors or not manifest["provider"].get("available"):
        return {
            "status": STATUS_INVALID,
            "errors": policy_errors
            + ([] if manifest["provider"].get("available") else ["model unavailable"]),
        }
    _write_new(directory / "manifest.json", manifest)
    return {
        "status": STATUS_CAMPAIGN_CREATED,
        "campaign": campaign_id,
        "manifest": str(directory / "manifest.json"),
        "source_snapshot_sha256": source["snapshot_sha256"],
        "configuration_fingerprint": fingerprint,
    }


def _policy_problems(environment: dict[str, str]) -> list[str]:
    return [
        f"{name} must be {value}"
        for name, value in EXPECTED_POLICY.items()
        if str(environment.get(name, "")).lower() != value
    ]


def _terminate(directory: Path, reason: str, detail: Any) -> None:
    path = directory / "terminated.json"
    if not path.exists():
        _write_new(path, {"terminated_at": _utc(), "reason": reason, "detail": detail})


def _reservations(directory: Path, case_id: str) -> list[int]:
    root = directory / "attempts" / case_id
    if not root.is_dir():
        return []
    return sorted(
        int(item.name.split("-", 1)[1])
        for item in root.iterdir()
        if item.is_dir() and re.fullmatch(r"attempt-\d+", item.name)
    )


def _attempt_dir(directory: Path, case_id: str, number: int) -> Path:
    return directory / "attempts" / case_id / f"attempt-{number}"


def reserve_attempt(directory: Path, case_id: str, budget: int) -> int:
    """Durably claim the next slot; a claimed slot is never released."""

    number = len(_reservations(directory, case_id)) + 1
    if number > budget:
        raise LiveError("budget_exhausted", f"{case_id} budget {budget} used")
    path = _attempt_dir(directory, case_id, number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()  # raises if a concurrent or earlier process claimed it
    _write_new(path / "reservation.json", {"reserved_at": _utc(), "attempt": number})
    return number


class Progress:
    """Append-only, fsynced attempt progress used for crash recovery."""

    def __init__(self, path: Path):
        self.path = path

    def mark(self, step: str, **details: Any) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"step": step, "at": _utc(), **details}, sort_keys=True)
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())

    def steps(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        found: dict[str, dict[str, Any]] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            found[item["step"]] = item
        return found


def preflight(
    ctx: LiveContext,
    manifest: dict[str, Any],
    case_id: str,
    *,
    used: int,
) -> dict[str, Any]:
    """Every critical condition before inference; nothing here is an attempt."""

    from .runner import isolation_checks

    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: Any = None) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    case = ctx.cases.get(case_id)
    check(
        "case_schema",
        case is not None and not validate_case(case) and case["status"] == "complete",
    )
    if case is not None:
        definition = hashlib.sha256(
            json.dumps(case, sort_keys=True).encode()
        ).hexdigest()
        check(
            "case_definition_matches_campaign",
            definition == manifest["cases"].get(case_id, {}).get("definition_sha256"),
        )
        state = seed_state(ctx.config, case)
        pinned = manifest["cases"][case_id]["seed_state"]
        check(
            "seed_identity",
            not _seed_problems(case, state),
            _seed_problems(case, state),
        )
        check(
            "seed_unmodified_since_campaign",
            all(
                state.get(key) == pinned.get(key)
                for key in ("head", "refs_sha256", "status_porcelain")
            ),
        )
        command = case["acceptance"]["command"].split()
        check(
            "acceptance_command",
            command[:3] == ["python", "-m", "pytest"]
            and case["acceptance"]["requires_unchanged_tests"] is True,
            case["acceptance"]["command"],
        )
    source = ctx.hooks.source_identity(ctx.repo_root)
    mismatch = identity.source_mismatch(manifest["source_identity"], source)
    check("source_identity", not mismatch, mismatch[:20])
    bench = ctx.hooks.benchmark_identity(ctx.repo_root)
    check(
        "benchmark_identity",
        bench["sha256"] == manifest["benchmark_identity"]["sha256"],
    )
    runtime = ctx.hooks.observe_runtime(ctx.config)
    check("runtime_ready", runtime["ready"], runtime["problems"])
    fingerprint = _config_fingerprint(runtime) if runtime["ready"] else None
    check(
        "runtime_configuration",
        fingerprint == manifest["runtime"]["configuration_fingerprint"],
        fingerprint,
    )
    environment = (runtime.get("processes") or {}).get("api", {}).get(
        "environment"
    ) or {}
    check(
        "hold_policy", not _policy_problems(environment), _policy_problems(environment)
    )
    isolation = isolation_checks(ctx.config)
    check("development_isolation", all(item["passed"] for item in isolation), isolation)
    model = ctx.hooks.model_identity(manifest["provider"]["model"])
    check(
        "model_identity",
        model.get("available")
        and model.get("digest") == manifest["provider"].get("digest"),
        model.get("digest"),
    )
    busy = ctx.hooks.active_tasks(ctx.config)
    check("one_active_task", not busy, busy)
    check(
        "attempt_budget",
        used < manifest["budget"][case_id],
        {"used": used, "budget": manifest["budget"][case_id]},
    )
    check(
        "evidence_destination",
        protected_output_problem(ctx.output_root, ctx.repo_root) is None,
    )
    api = None
    if runtime["ready"]:
        try:
            api = ctx.api(runtime)
            status, me = api.request("GET", "/api/v1/auth/me")
            check(
                "authentication",
                status == 200 and (me or {}).get("email") == EVALUATOR,
                status,
            )
        except (LiveError, OSError, dev_runtime.RuntimeToolError) as exc:
            check("authentication", False, type(exc).__name__)
    else:
        check("authentication", False, "runtime not ready")
    return {
        "schema": PREFLIGHT_SCHEMA,
        "case_id": case_id,
        "at": _utc(),
        "passed": all(item["passed"] for item in checks),
        "source_identity_mismatch": bool(mismatch),
        "checks": checks,
        "runtime": runtime,
        "api": api,
    }


def _save_preflight(directory: Path, result: dict[str, Any], label: str) -> Path:
    stored = {
        key: value for key, value in result.items() if key not in {"api", "runtime"}
    }
    stored["runtime_processes"] = (
        _runtime_projection(result["runtime"])
        if result["runtime"].get("processes")
        else {}
    )
    path = directory / "preflight" / f"{label}.json"
    _write_new(path, stored)
    return path


def run_case(
    ctx: LiveContext,
    campaign_id: str,
    case_id: str,
    max_attempts: int,
    *,
    resume_campaign: str | None = None,
) -> dict[str, Any]:
    directory = ctx.campaign_dir(campaign_id)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return {
            "status": STATUS_INVALID,
            "errors": [f"no campaign manifest: {manifest_path}"],
        }
    try:
        with exclusive_runner(ctx.output_root):
            return _run_case_locked(
                ctx, directory, case_id, max_attempts, resume_campaign
            )
    except LiveError as exc:
        if exc.code == "runner_busy":
            return {"status": STATUS_BUSY, "errors": [str(exc)]}
        raise


def _run_case_locked(
    ctx: LiveContext,
    directory: Path,
    case_id: str,
    max_attempts: int,
    resume_campaign: str | None,
) -> dict[str, Any]:
    manifest = _read_json(directory / "manifest.json")
    campaign_id = manifest["campaign_id"]
    if (directory / "terminated.json").exists():
        return {
            "status": STATUS_CAMPAIGN_TERMINATED,
            "detail": _read_json(directory / "terminated.json"),
        }
    budget = manifest["budget"].get(case_id)
    if budget is None or max_attempts != budget:
        return {
            "status": STATUS_INVALID,
            "errors": [f"{case_id} budget is {budget}; requested {max_attempts}"],
        }
    reserved = _reservations(directory, case_id)
    if reserved and resume_campaign != campaign_id:
        return {
            "status": STATUS_RESUME_CONFIRMATION_REQUIRED,
            "detail": f"{case_id} already has attempts {reserved}; pass --resume-campaign {campaign_id}",
        }

    outcomes = []
    for number in reserved:
        attempt_dir = _attempt_dir(directory, case_id, number)
        if (attempt_dir / "record.json").exists():
            continue
        result = preflight(ctx, manifest, case_id, used=0)
        _save_preflight(
            directory, result, f"{case_id}-attempt-{number}-resume-{int(time.time())}"
        )
        if result["source_identity_mismatch"]:
            _terminate(directory, "source_identity_mismatch", case_id)
            return {
                "status": STATUS_CAMPAIGN_TERMINATED,
                "reason": "source_identity_mismatch",
            }
        if not result["runtime"]["ready"]:
            return {
                "status": STATUS_PREFLIGHT_FAILED,
                "attempt": number,
                "checks": result["checks"],
            }
        outcomes.append(
            _execute_attempt(ctx, manifest, directory, case_id, number, result)
        )

    while len(_reservations(directory, case_id)) < budget:
        used = len(_reservations(directory, case_id))
        result = preflight(ctx, manifest, case_id, used=used)
        _save_preflight(directory, result, f"{case_id}-attempt-{used + 1}")
        if result["source_identity_mismatch"]:
            _terminate(directory, "source_identity_mismatch", case_id)
            return {
                "status": STATUS_CAMPAIGN_TERMINATED,
                "reason": "source_identity_mismatch",
                "attempts": outcomes,
            }
        if not result["passed"]:
            return {
                "status": STATUS_PREFLIGHT_FAILED,
                "is_benchmark_attempt": False,
                "failed_checks": [
                    item["check"] for item in result["checks"] if not item["passed"]
                ],
                "attempts": outcomes,
            }
        number = reserve_attempt(directory, case_id, budget)
        outcomes.append(
            _execute_attempt(ctx, manifest, directory, case_id, number, result)
        )
        if (directory / "terminated.json").exists():
            return {"status": STATUS_CAMPAIGN_TERMINATED, "attempts": outcomes}
    return {
        "status": STATUS_CASE_COMPLETE if outcomes else STATUS_BUDGET_EXHAUSTED,
        "campaign": campaign_id,
        "case_id": case_id,
        "attempts": outcomes,
    }


# -- one attempt -------------------------------------------------------------


def _execute_attempt(
    ctx: LiveContext,
    manifest: dict[str, Any],
    directory: Path,
    case_id: str,
    number: int,
    pre: dict[str, Any],
) -> dict[str, Any]:
    case = ctx.cases[case_id]
    attempt_dir = _attempt_dir(directory, case_id, number)
    progress = Progress(attempt_dir / "progress.jsonl")
    steps = progress.steps()
    api: DevApiClient = pre["api"]
    config = ctx.config
    name = f"{manifest['campaign_id']}-{case_id}-a{number}"
    worker_log = config.root / "logs" / "worker.log"
    if "started" not in steps:
        progress.mark(
            "started",
            worker_log_offset=worker_log.stat().st_size if worker_log.is_file() else 0,
            runtime_processes=_runtime_projection(pre["runtime"]),
        )
        steps = progress.steps()

    submission: dict[str, Any] = {}
    try:
        if "execute_requested" not in steps:
            if "workspace_prepared" not in steps:
                seed = config.root / "workspace" / case["seed"]["repository"]
                clone = clone_seed(seed, config.root / "workspace" / name, case)
                progress.mark("workspace_prepared", **clone)
            if "project_created" not in steps:
                status, project = api.request(
                    "POST",
                    "/api/v1/projects",
                    {
                        "name": f"Phase 40D2 {manifest['campaign_id']} {case_id} attempt {number}",
                        "description": f"Disposable Phase 40D-2 benchmark Project ({case_id}).",
                        "branch": "main",
                        "workspace_path": name,
                    },
                )
                if status != 201:
                    raise LiveError("project_create_failed", f"HTTP {status}")
                progress.mark("project_created", project_id=project["id"])
            steps = progress.steps()
            if "task_created" not in steps:
                status, task = api.request(
                    "POST",
                    "/api/v1/tasks",
                    {
                        "project_id": steps["project_created"]["project_id"],
                        "title": case["original_task"]["title"],
                        "description": case["original_task"]["description"],
                    },
                )
                if status != 201:
                    raise LiveError("task_create_failed", f"HTTP {status}")
                progress.mark("task_created", task_id=task["id"])
            steps = progress.steps()
            progress.mark("execute_requested")
            try:
                status, queued = api.request(
                    "POST",
                    f"/api/v1/tasks/{steps['task_created']['task_id']}/execute",
                    {},
                )
            except OSError as exc:
                status, queued = 0, {"error": type(exc).__name__}
            if status == 202:
                progress.mark(
                    "execute_accepted",
                    task_execution_id=queued["task_execution_id"],
                    session_id=queued["session_id"],
                    celery_task_id=queued["celery_task_id"],
                )
            else:
                progress.mark("execute_response_unconfirmed", http_status=status)
    except (LiveError, OSError, subprocess.SubprocessError) as exc:
        progress.mark("infrastructure_failure", error=str(exc))
        return _finalize(
            ctx,
            manifest,
            attempt_dir,
            case,
            number,
            api,
            submitted=False,
            infra=str(exc),
        )

    steps = progress.steps()
    task_id = steps["task_created"]["task_id"]
    submission = {
        "project_id": steps["project_created"]["project_id"],
        "task_id": task_id,
    }
    observed = _observe(ctx, api, task_id, progress)
    if observed.get("unsubmitted"):
        progress.mark(
            "not_submitted",
            detail="no TaskExecution was observed for the execute request",
        )
        return _finalize(
            ctx,
            manifest,
            attempt_dir,
            case,
            number,
            api,
            submitted=False,
            infra="execute request produced no TaskExecution",
        )
    return _finalize(
        ctx,
        manifest,
        attempt_dir,
        case,
        number,
        api,
        submitted=True,
        observed=observed,
        submission=submission,
    )


def _observe(
    ctx: LiveContext, api: DevApiClient, task_id: int, progress: Progress
) -> dict[str, Any]:
    steps = progress.steps()
    if "terminal" in steps:
        return steps["terminal"]
    began = ctx.hooks.monotonic()
    grace = min(UNCONFIRMED_EXECUTE_GRACE_SECONDS, ctx.timeout_seconds)
    deadline = began + ctx.timeout_seconds
    accepted = "execute_accepted" in steps
    polls = 0
    last: dict[str, Any] = {}
    while True:
        try:
            status, task = api.request("GET", f"/api/v1/tasks/{task_id}")
        except OSError as exc:
            status, task = 0, {"error": type(exc).__name__}
        polls += 1
        if status == 200:
            execution = task.get("latest_execution_identity") or {}
            last = {
                "task_status": str(task.get("status") or "").lower(),
                "task_execution_id": execution.get("task_execution_id"),
                "session_id": task.get("session_id"),
            }
            if last["task_status"] in TERMINAL_TASK_STATUSES:
                progress.mark("terminal", polls=polls, **last)
                return progress.steps()["terminal"]
            if (
                not accepted
                and last["task_execution_id"] is None
                and ctx.hooks.monotonic() - began >= grace
            ):
                return {**last, "unsubmitted": True}
        if ctx.hooks.monotonic() >= deadline:
            break
        ctx.hooks.sleep(ctx.poll_seconds)
    stop = None
    if last.get("session_id"):
        try:
            stop = api.request("POST", f"/api/v1/sessions/{last['session_id']}/stop")[0]
        except OSError:
            stop = None
    progress.mark("timeout", polls=polls, session_stop_http=stop, **last)
    return {**last, "timed_out": True}


def _copy_tree(source: Path, destination: Path) -> list[Path]:
    if not source.is_dir():
        return []
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("*.lock"),
        dirs_exist_ok=False,
    )
    return sorted(path for path in destination.rglob("*") if path.is_file())


def _worker_log_segment(
    config: dev_runtime.RuntimeConfig, start: int, destination: Path
) -> Path | None:
    log = config.root / "logs" / "worker.log"
    if not log.is_file():
        return None
    with log.open("rb") as stream:
        stream.seek(start)
        destination.write_bytes(stream.read())
    return destination


def redact_secrets(paths: list[Path], secrets: list[str]) -> list[str]:
    """Replace any secret value found in evidence; return redacted file names."""

    redacted = []
    needles = [secret.encode() for secret in secrets if secret and len(secret) >= 16]
    for path in paths:
        data = path.read_bytes()
        if any(needle in data for needle in needles):
            for needle in needles:
                data = data.replace(needle, b"<redacted>")
            path.write_bytes(data)
            redacted.append(path.name)
    return redacted


def provider_evidence(
    control_project: Path, start: float | None, end: float | None, task_count: int
) -> dict[str, Any]:
    """Summarize retained provider responses; correlation basis is explicit."""

    responses = []
    for path in sorted(
        control_project.glob("planning-evidence/*/provider-response.json")
    ):
        data = _read_json(path)
        timing = data.get("timing") or {}
        started = timing.get("request_started_utc")
        started_epoch = datetime.fromisoformat(started).timestamp() if started else None
        validation = data.get("plan_validation") or {}
        responses.append(
            {
                "path": str(path.relative_to(control_project)),
                "provider_call_id": (data.get("correlation") or {}).get(
                    "provider_call_id"
                ),
                "provider_request_correlation_id": (data.get("provider") or {}).get(
                    "provider_request_correlation_id"
                ),
                "capture_scope": data.get("capture_scope"),
                "role": (data.get("provider") or {}).get("role"),
                "model": (data.get("provider") or {}).get("model"),
                "http_status": (data.get("provider") or {}).get("http_status"),
                "finish_reason": (data.get("provider") or {}).get("finish_reason"),
                "structured_output_mode": (data.get("request") or {}).get(
                    "response_format"
                ),
                "parse_status": (data.get("planning_parse") or {}).get("status"),
                "parser_strategy": (data.get("planning_parse") or {}).get(
                    "parser_strategy"
                ),
                "schema_validation": (validation.get("schema_validation") or {}).get(
                    "status"
                ),
                "final_plan_validation": (
                    validation.get("final_plan_validation") or {}
                ).get("status"),
                "request_started_utc": started,
                "within_execution_window": (
                    None
                    if None in (start, end, started_epoch)
                    else start - 1 <= started_epoch <= end + 1
                ),
            }
        )
    outside = [
        item["provider_call_id"]
        for item in responses
        if item["within_execution_window"] is False
    ]
    return {
        "correlation_basis": "project-scoped evidence directory (one disposable Project, one Task, one TaskExecution) cross-checked against the execution window",
        "project_task_count": task_count,
        "ambiguous": task_count != 1 or bool(outside),
        "outside_window": outside,
        "debug_provider_responses_retained": False,
        "responses": responses,
    }


def _live_attribution(
    record: dict[str, Any], infra: str | None, timed_out: bool
) -> dict[str, Any]:
    reliability = record["reliability"]
    stage = reliability["failure_stage"]
    if infra:
        return {
            "basis": "rule:infrastructure_before_execution",
            "kinds": ["infrastructure"],
            "root_causes": [],
        }
    if timed_out:
        return {
            "basis": "rule:observation_timeout",
            "kinds": ["unknown"],
            "root_causes": [],
        }
    if stage == "none":
        return {"basis": "not_applicable", "kinds": [], "root_causes": []}
    if stage == "no_candidate":
        return {
            "basis": "rule:done_without_candidate",
            "kinds": ["unknown"],
            "root_causes": [],
        }
    terminal = set(reliability["terminal_safety_rejections"])
    if terminal and terminal <= MODEL_PROPOSAL_REJECTIONS:
        return {
            "basis": "rule:terminal_safety_rejection_of_model_proposal",
            "kinds": ["model_output"],
            "root_causes": [],
        }
    if stage == "acceptance":
        return {
            "basis": "rule:independent_acceptance_failure_of_model_candidate",
            "kinds": ["model_output"],
            "root_causes": [],
        }
    if (
        stage == "verification"
        and reliability["verification_failure"]
        and record["execution"]["source_mutation_captured"]
    ):
        return {
            "basis": "rule:verification_failure_of_model_candidate",
            "kinds": ["model_output"],
            "root_causes": [],
        }
    return reliability["attribution"]


def _finalize(
    ctx: LiveContext,
    manifest: dict[str, Any],
    attempt_dir: Path,
    case: dict[str, Any],
    number: int,
    api: DevApiClient | None,
    *,
    submitted: bool,
    infra: str | None = None,
    observed: dict[str, Any] | None = None,
    submission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = ctx.config
    progress = Progress(attempt_dir / "progress.jsonl")
    steps = progress.steps()
    campaign_id = manifest["campaign_id"]
    evidence = attempt_dir / "evidence"
    observed = observed or {}
    slot: dict[str, Any] = {"slot": f"attempt-{number}", "case_id": case["case_id"]}
    copied: list[Path] = []
    acceptance_result: dict[str, Any] | None = None
    provider: dict[str, Any] | None = None
    change_set_api: Any = None

    for name in ("evidence", "acceptance"):
        partial = attempt_dir / name
        if partial.exists():  # an earlier finalize was interrupted; keep it aside
            partial.rename(attempt_dir / f"{name}.interrupted-{int(time.time())}")
    if submitted and submission:
        task_id = submission["task_id"]
        project_id = submission["project_id"]
        slot.update(task_id=task_id, project_id=project_id)
        rows = ctx.hooks.db_rows(config, task_id)
        evidence.mkdir(parents=True, exist_ok=True)
        _write_new(evidence / "db-rows.json", rows)
        copied.append(evidence / "db-rows.json")
        control = config.root / "runtime" / "control" / "projects" / str(project_id)
        if not (evidence / "control").exists():
            copied.extend(_copy_tree(control, evidence / "control"))
        segment = _worker_log_segment(
            config, int(steps["started"]["worker_log_offset"]), evidence / "worker.log"
        )
        if segment:
            copied.append(segment)
        if api is not None:
            try:
                status, change_set_api = api.request(
                    "GET", f"/api/v1/tasks/{task_id}/change-set"
                )
                change_set_api = (
                    change_set_api if status == 200 else {"http_status": status}
                )
            except OSError as exc:
                change_set_api = {"error": type(exc).__name__}
            _write_new(evidence / "change-set-api.json", change_set_api)
            copied.append(evidence / "change-set-api.json")
        executions = rows["task_executions"]
        execution = executions[-1] if executions else None
        start = _parse_time(execution.get("started_at")) if execution else None
        end = _parse_time(execution.get("completed_at")) if execution else None
        project_tasks = _project_task_count(config, project_id)
        provider = provider_evidence(control, start, end, project_tasks)
        _write_new(evidence / "provider-evidence.json", provider)
        copied.append(evidence / "provider-evidence.json")
        manifest_path = (
            control / "change-sets" / str(execution["id"]) / "manifest.json"
            if execution
            else None
        )
        if manifest_path is not None and manifest_path.is_file():
            change = _read_json(manifest_path)
            if change.get("copied_files") or change.get("deleted_files"):
                acceptance_result = independent_acceptance(
                    ctx,
                    case,
                    manifest_path,
                    attempt_dir / "acceptance",
                    campaign_id,
                    number,
                )
                slot["acceptance"] = {
                    "record": _rel(
                        attempt_dir / "acceptance" / "independent-acceptance.txt",
                        ctx.repo_root,
                    ),
                    "format": "key_values_then_pytest",
                    "method": "held ChangeSet overlaid on an independent fresh clone of the canonical seed (benchmark, not promotion)",
                }
        redacted = redact_secrets(
            [path for path in attempt_dir.rglob("*") if path.is_file()],
            _runtime_secrets(steps.get("started"))
            + [getattr(api, "_token", None) or ""],
        )
    else:
        redacted = []

    phase = {
        "phase": PHASE,
        "source_identity": _compact_source(manifest["source_identity"]),
        "provider": {"model": manifest["provider"]["model"]},
    }
    loader = HistoryLoader(
        EvidenceRoots(ctx.repo_root, ctx.output_root, config.root),
        {"phases": []},
        ctx.cases,
        live_database=True,
    )
    try:
        record = loader._attempt(phase, slot)
    finally:
        loader.db.close()
    if not submitted:
        record["attempted"] = False
        record["not_attempted_reason"] = f"infrastructure: {infra}"
        record["identity"]["project_id"] = (steps.get("project_created") or {}).get(
            "project_id"
        )
        record["identity"]["task_id"] = (steps.get("task_created") or {}).get("task_id")
    timed_out = bool(observed.get("timed_out"))
    record["identity"].update(
        attempt_id=f"{PHASE}:{campaign_id}:{case['case_id']}:attempt-{number}",
        campaign_id=campaign_id,
        attempt_number=number,
    )
    record["identity"]["runtime_identity"].update(
        runtime="isolated development runtime (.dev-runtime/phase40b-r1)",
        benchmark_configuration_fingerprint=manifest["runtime"][
            "configuration_fingerprint"
        ],
        processes=(steps.get("started") or {}).get("runtime_processes"),
        queue=manifest["runtime"]["queue"],
    )
    record["identity"]["provider_identity"].update(
        model=manifest["provider"]["model"],
        model_digest=manifest["provider"].get("digest"),
        endpoint=manifest["provider"].get("endpoint"),
    )
    record["identity"]["benchmark_identity_sha256"] = manifest["benchmark_identity"][
        "sha256"
    ]
    record["identity"]["case_definition_sha256"] = manifest["cases"][case["case_id"]][
        "definition_sha256"
    ]
    reliability = record["reliability"]
    limitations = list(reliability["evidence_limitations"])
    if provider is not None:
        record["identity"]["provider_identity"]["correlation"] = {
            key: provider[key]
            for key in ("correlation_basis", "ambiguous", "outside_window")
        }
        if provider["ambiguous"]:
            limitations.append(
                {
                    "code": "provider_correlation_ambiguous",
                    "statement": provider["correlation_basis"],
                }
            )
        if (record["execution"]["debug_repair_count"] or 0) > 0:
            limitations.append(
                {
                    "code": "debug_provider_response_not_retained",
                    "statement": "the application retains Planning provider responses only; DEBUG repair responses are summarized by journal events",
                }
            )
    if timed_out:
        limitations.append(
            {
                "code": "observation_timeout",
                "statement": f"no terminal Task status within {ctx.timeout_seconds}s",
            }
        )
        reliability["failure_stage"] = "timeout"
    if redacted:
        limitations.append(
            {"code": "secret_redacted", "statement": ", ".join(sorted(redacted))}
        )
    if change_set_api and isinstance(change_set_api, dict):
        decision = change_set_api.get("review_decision") or {}
        record["change_set"]["review_decision"] = decision
        record["change_set"]["api_status"] = (
            change_set_api.get("change_set") or {}
        ).get("status")
    if acceptance_result is not None:
        # The verdict covers integrity, scope, and unchanged tests, not just exit 0.
        record["acceptance"]["result"] = acceptance_result["result"]
        record["acceptance"]["integrity"] = acceptance_result["integrity"]
        record["acceptance"]["exit_code"] = acceptance_result["exit_code"]
        if acceptance_result["result"] != "passed":
            reliability["independently_accepted"] = False
    record["acceptance"]["independent_acceptance_attempted"] = (
        acceptance_result is not None
    )
    if record["attempted"] and reliability["failure_stage"] == "none":
        # A DONE Task is a success only through independent acceptance.
        if not record["execution"]["source_mutation_captured"]:
            reliability["failure_stage"] = "no_candidate"
        elif reliability["independently_accepted"] is not True:
            reliability["failure_stage"] = "acceptance"
    reliability["attribution"] = _live_attribution(record, infra, timed_out)
    reliability["evidence_limitations"] = sorted(
        limitations, key=lambda item: item["code"]
    )
    reliability["evidence_refs"] = sorted(
        set(reliability["evidence_refs"])
        | {_rel(path, ctx.repo_root) for path in copied}
    )
    errors = validate_attempt(record)
    if errors:
        raise LiveError("attempt_schema_invalid", "; ".join(errors))
    _write_new(attempt_dir / "record.json", record)
    progress.mark("record_written")

    after = ctx.hooks.source_identity(ctx.repo_root)
    mismatch = identity.source_mismatch(manifest["source_identity"], after)
    if mismatch:
        _terminate(
            attempt_dir.parents[2],
            "source_identity_mismatch_after_attempt",
            mismatch[:20],
        )
    return {
        "attempt": number,
        "attempt_id": record["identity"]["attempt_id"],
        "attempted": record["attempted"],
        "task_id": record["identity"]["task_id"],
        "task_execution_id": record["identity"]["task_execution_id"],
        "task_status": record["execution"]["task_status"],
        "failure_stage": reliability["failure_stage"],
        "independently_accepted": reliability["independently_accepted"],
        "record": str(attempt_dir / "record.json"),
        "source_identity_after": "unchanged" if not mismatch else "MISMATCH",
    }


def _runtime_secrets(started: dict[str, Any] | None) -> list[str]:
    secrets = []
    for process in ((started or {}).get("runtime_processes") or {}).values():
        try:
            secrets.append(
                identity.read_process_secret(int(process["pid"]), "SECRET_KEY")
            )
        except OSError:
            continue
    return secrets


def _parse_time(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).replace(" ", "T")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _project_task_count(config: dev_runtime.RuntimeConfig, project_id: int) -> int:
    if not config.database_file.is_file():
        return 0
    conn = sqlite3.connect(f"file:{config.database_file}?mode=ro", uri=True)
    try:
        return int(
            conn.execute(
                "select count(*) from tasks where project_id=?", (project_id,)
            ).fetchone()[0]
        )
    finally:
        conn.close()


# -- independent acceptance --------------------------------------------------


def _safe_relative(relative: str) -> Path | None:
    """Mirror BaselinePromotionService._safe_change_set_relative_path (lexical)."""

    path = Path(relative)
    if (
        not relative
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[:1] == (".git",)
    ):
        return None
    return path


def _containable(root: Path, relative: Path) -> bool:
    """Mirror _destination_is_containable: never write through a symlink."""

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _under(path: str, prefixes: list[str]) -> bool:
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix) for prefix in prefixes
    )


def _changed_paths(repo: Path) -> list[str]:
    raw = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return sorted(item[3:] for item in raw.split("\0") if len(item) > 3)


def independent_acceptance(
    ctx: LiveContext,
    case: dict[str, Any],
    manifest_path: Path,
    acceptance_dir: Path,
    campaign_id: str,
    number: int,
) -> dict[str, Any]:
    """Overlay a held ChangeSet on a fresh seed clone and run the original tests.

    The held artifact is only read. The canonical seed is only cloned. Nothing
    is promoted, and the Project workspace is never touched.
    """

    config = ctx.config
    acceptance_dir.mkdir(parents=True, exist_ok=False)
    workdir = (
        ctx.repo_root
        / ".dev-runtime"
        / "benchmark"
        / PHASE
        / campaign_id
        / f"{case['case_id']}-a{number}-acceptance"
    )
    workdir.parent.mkdir(parents=True, exist_ok=True)
    seed = config.root / "workspace" / case["seed"]["repository"]
    change = _read_json(manifest_path)
    files_root = manifest_path.parent / "files"
    problems: list[str] = []
    applied: dict[str, str | None] = {}
    clone = clone_seed(seed, workdir, case)
    if Path(change.get("artifact_path") or "").resolve() != files_root.resolve():
        problems.append("artifact_path does not match the manifest directory")
    artifact_files = (
        sorted(
            str(path.relative_to(files_root))
            for path in files_root.rglob("*")
            if path.is_file()
        )
        if files_root.is_dir()
        else []
    )
    copied = sorted(change.get("copied_files") or [])
    deleted = sorted(change.get("deleted_files") or [])
    if artifact_files != copied:
        problems.append(
            f"artifact files {artifact_files} differ from manifest {copied}"
        )
    for relative in copied + deleted:
        safe = _safe_relative(relative)
        if safe is None or not _containable(workdir, safe):
            problems.append(f"unsafe path: {relative}")
    if not problems:
        for relative in copied:
            source = files_root / relative
            if source.is_symlink():
                problems.append(f"artifact symlink: {relative}")
                continue
            destination = workdir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            applied[relative] = _sha256_file(destination)
        for relative in deleted:
            target = workdir / relative
            if target.is_file():
                target.unlink()
            applied[relative] = None
    changed = _changed_paths(workdir) if not problems else []
    protected = case["protected_test_paths"]
    test_paths = [path for path in changed if _under(path, protected)]
    source_paths = [path for path in changed if not _under(path, protected)]
    unauthorized = [
        path
        for path in source_paths
        if not _under(path, case["authorized_source_paths"])
    ]
    unexpected = sorted(set(changed) - set(copied) - set(deleted))
    if unexpected:
        problems.append(f"applied diff has paths outside the ChangeSet: {unexpected}")
    command = [ctx.python, *case["acceptance"]["command"].split()[1:]]
    exit_code: int | None = None
    output = ""
    test_tree_before = _git(workdir, "rev-parse", "HEAD:tests", check=False)
    if not problems:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(workdir),
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            run = subprocess.run(
                command,
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=ACCEPTANCE_TIMEOUT_SECONDS,
                check=False,
            )
            exit_code, output = run.returncode, run.stdout + run.stderr
        except subprocess.TimeoutExpired as exc:
            output = f"acceptance timed out after {ACCEPTANCE_TIMEOUT_SECONDS}s\n{exc.stdout or ''}"
    tests_after = [path for path in _changed_paths(workdir) if _under(path, protected)]
    lines = [
        f"command: {' '.join(command)}",
        f"exit_code: {'' if exit_code is None else exit_code}",
        f"tracked_diff: {json.dumps(source_paths)}",
        f"test_diff: {json.dumps(sorted(set(test_paths) | set(tests_after)))}",
        "",
        (
            output
            if not problems
            else "integrity check failed; acceptance tests were not run\n"
            + "\n".join(problems)
        ),
    ]
    record_path = acceptance_dir / "independent-acceptance.txt"
    record_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    diff = subprocess.run(
        ["git", "-C", str(workdir), "diff", "--binary"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    (acceptance_dir / "source.diff").write_text(diff, encoding="utf-8")
    parsed = parse_acceptance_record(record_path, "key_values_then_pytest")
    result = {
        "case_id": case["case_id"],
        "seed_clone": clone,
        "change_set_manifest": _rel(manifest_path, ctx.repo_root),
        "change_set_manifest_sha256": _sha256_file(manifest_path),
        "applied_files_sha256": applied,
        "changed_paths": changed,
        "source_paths": source_paths,
        "test_paths": sorted(set(test_paths) | set(tests_after)),
        "unauthorized_source_paths": unauthorized,
        "tests_tree_at_seed": test_tree_before,
        "command": command,
        "case_command": case["acceptance"]["command"],
        "exit_code": exit_code,
        "passed": parsed["passed"],
        "failed": parsed["failed"],
        "errors": parsed["errors"],
        "integrity": {"valid": not problems, "problems": problems},
        "result": (
            "passed"
            if not problems
            and exit_code == 0
            and not unauthorized
            and not test_paths
            and not tests_after
            else "failed"
        ),
        "promoted": False,
    }
    _write_new(acceptance_dir / "acceptance.json", result)
    shutil.rmtree(workdir)
    return result


# -- campaign results --------------------------------------------------------


def campaign_records(ctx: LiveContext, campaign_id: str) -> list[dict[str, Any]]:
    directory = ctx.campaign_dir(campaign_id)
    return [
        _read_json(path)
        for path in sorted(
            (directory / "attempts").glob("*/attempt-*/record.json"),
            key=lambda item: (item.parts[-3], int(item.parts[-2].split("-")[1])),
        )
    ]


def campaign_metrics(ctx: LiveContext, campaign_id: str) -> dict[str, Any]:
    records = campaign_records(ctx, campaign_id)
    manifest = _read_json(ctx.campaign_dir(campaign_id) / "manifest.json")
    per_case: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        per_case.setdefault(record["identity"]["case_id"], []).append(record)
    return {
        "schema": "benchmark.campaign_metrics.v1",
        "campaign_id": campaign_id,
        "source_snapshot_sha256": manifest["source_identity"]["snapshot_sha256"],
        "configuration_fingerprint": manifest["runtime"]["configuration_fingerprint"],
        "model": manifest["provider"]["model"],
        "budget": manifest["budget"],
        "records": len(records),
        "sample_size_note": "an initial engineering baseline of at most six attempts; not a statistical reliability estimate and not combined with Phase 40B-40C history",
        "metrics": compute_metrics(records),
        "per_case": {
            case_id: compute_metrics(items)
            for case_id, items in sorted(per_case.items())
        },
        "grouped": grouped_metrics(records),
        "attempts": [
            {
                "attempt_id": record["identity"]["attempt_id"],
                "attempted": record["attempted"],
                "task_id": record["identity"]["task_id"],
                "task_status": record["execution"]["task_status"],
                "failure_stage": record["reliability"]["failure_stage"],
                "attribution": record["reliability"]["attribution"],
                "independently_accepted": record["reliability"][
                    "independently_accepted"
                ],
            }
            for record in records
        ],
    }


def close_campaign(
    ctx: LiveContext, campaign_id: str, *, stop_runtime: bool
) -> dict[str, Any]:
    """Stop only launcher-owned processes and record what remains."""

    config = ctx.config
    directory = ctx.campaign_dir(campaign_id)
    manifest = _read_json(directory / "manifest.json")
    stopped = (
        dev_runtime.stop_runtime(config)
        if stop_runtime
        else dev_runtime.status_report(config)
    )
    ports = {
        str(port): dev_runtime._port_available(port)
        for port in (config.api_port, config.redis_port, config.qdrant_port)
    }
    source = ctx.hooks.source_identity(ctx.repo_root)
    seeds = {
        case_id: {
            key: seed_state(config, ctx.cases[case_id]).get(key)
            == item["seed_state"].get(key)
            for key in ("head", "refs_sha256", "status_porcelain", "tree")
        }
        for case_id, item in manifest["cases"].items()
    }
    result = {
        "closed_at": _utc(),
        "runtime_status": stopped.get("status"),
        "processes": stopped.get("processes"),
        "ports_closed": ports,
        "source_identity_unchanged": not identity.source_mismatch(
            manifest["source_identity"], source
        ),
        "seeds_unchanged": seeds,
        "evidence_preserved": True,
        "runtime_root_preserved": config.root.is_dir(),
    }
    _write_replace(directory / "cleanup.json", result)
    return result
