"""Phase 40D-2 controlled live benchmark execution tests.

A fake development API stands in for the API and Worker: on execute it writes
the database rows, event journal, provider evidence, and held ChangeSet that
the real runtime produces. No Ollama, Redis, or Worker is used, and no process
other than a test-spawned child is ever signalled.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import dev_runtime
from scripts.benchmark import cli, identity, live, runner
from scripts.benchmark.schema import validate_attempt

REPO = Path(__file__).resolve().parents[2]
CASE_ID = "toy-fix"
FIXED_SOURCE = "def add(a, b):\n    return a + b\n"
BUGGY_SOURCE = "def add(a, b):\n    return a - b\n"
POLICY_ENV = {
    "WORKSPACE_REVIEW_POLICY": "hold_all",
    "ONE_SHOT_EXECUTION_ENABLED": "false",
    "LOW_RESOURCE_SINGLE_MODEL": "true",
    "PLANNER_MODEL": "qwen3-coder:30b",
    "SECRET_KEY": "<redacted:set>",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _db_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        message,
    )


class FakeOrchestrator:
    """Development API + Worker double backed by a dev-shaped runtime root."""

    def __init__(self, repo_root: Path, scenario: str = "fix"):
        self.repo_root = repo_root
        self.config = dev_runtime.RuntimeConfig.for_repo(repo_root)
        self.scenario = scenario
        self.calls: list[tuple[str, str, dict | None]] = []
        self.terminal = True
        self.on_execute = None
        self.next_project = 100
        self.next_task = 500
        self.unauthorized_once = False
        (self.config.root / "database").mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.config.database_file)
        conn.executescript(
            """
            create table if not exists projects (id integer primary key, workspace_path text);
            create table if not exists tasks (id integer primary key, project_id integer, status text,
                created_at text, error_message text, description text, promoted_at text, title text);
            create table if not exists task_executions (id integer primary key, task_id integer,
                session_id integer, configuration_fingerprint text, planner_model text,
                executor_model text, planning_backend text, execution_backend text, started_at text,
                completed_at text, status text, failure_category text, attempt_number integer);
            create table if not exists sessions (id integer primary key, project_id integer, status text,
                execution_mode text, created_at text, updated_at text);
            """
        )
        conn.commit()
        conn.close()

    def _db(self, sql: str, *params) -> list:
        conn = sqlite3.connect(self.config.database_file)
        try:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def http(self, url, *, method="GET", token=None, payload=None):
        assert url.startswith("http://127.0.0.1:18740/")
        assert token == "dev-token"
        path = url.split("18740", 1)[1]
        self.calls.append((method, path, payload))
        if self.unauthorized_once:
            self.unauthorized_once = False
            return 401, None
        if path == "/api/v1/auth/me":
            return 200, {"email": dev_runtime.EVALUATOR_EMAIL}
        if method == "POST" and path == "/api/v1/projects":
            if self.scenario == "project_error":
                return 500, None
            self.next_project += 1
            self._db(
                "insert into projects values (?, ?)",
                self.next_project,
                payload["workspace_path"],
            )
            return 201, {"id": self.next_project}
        if method == "POST" and path == "/api/v1/tasks":
            self.next_task += 1
            self._db(
                "insert into tasks (id, project_id, status, created_at, description, title) values (?,?,?,?,?,?)",
                self.next_task,
                payload["project_id"],
                "PENDING",
                _db_time(datetime.now(timezone.utc).timestamp()),
                payload["description"],
                payload["title"],
            )
            return 201, {"id": self.next_task}
        if method == "POST" and path.endswith("/execute"):
            task_id = int(path.split("/")[4])
            return 202, self._execute(task_id)
        if method == "GET" and path.endswith("/change-set"):
            return 200, {
                "change_set": {"status": "captured"},
                "review_decision": {"decision": "hold", "policy": "hold_all"},
            }
        if method == "GET" and path.startswith("/api/v1/tasks/"):
            task_id = int(path.split("/")[4])
            (status,) = self._db("select status from tasks where id=?", task_id)[0]
            execution = self._db(
                "select id, session_id from task_executions where task_id=? order by id desc",
                task_id,
            )
            return 200, {
                "id": task_id,
                "status": status.lower(),
                "session_id": execution[0][1] if execution else None,
                "latest_execution_identity": (
                    {"task_execution_id": execution[0][0]} if execution else None
                ),
            }
        if method == "POST" and path.endswith("/stop"):
            return 200, {}
        raise AssertionError(f"unexpected request {method} {path}")

    def _execute(self, task_id: int) -> dict:
        (project_id,) = self._db("select project_id from tasks where id=?", task_id)[0]
        execution_id = task_id
        session_id = task_id + 1000
        now = datetime.now(timezone.utc).timestamp()
        status = "RUNNING"
        candidate: dict[str, str] = {}
        events = [
            ("phase_started", {"phase": "planning"}),
            ("phase_finished", {"phase": "planning", "status": "accepted"}),
            ("phase_started", {"phase": "executing"}),
        ]
        if self.scenario in {"fix", "edits_tests", "unsafe_path", "wrong_fix"}:
            candidate["pkg/mod.py"] = (
                BUGGY_SOURCE if self.scenario == "wrong_fix" else FIXED_SOURCE
            )
            if self.scenario == "edits_tests":
                candidate["tests/test_mod.py"] = "def test_nothing():\n    pass\n"
            events += [
                ("step_finished", {"status": "completed"}),
                ("task_completed", {}),
            ]
            status = "DONE"
        elif self.scenario == "done_no_candidate":
            events += [
                ("step_finished", {"status": "completed"}),
                ("task_completed", {}),
            ]
            status = "DONE"
        else:
            events += [
                ("step_finished", {"status": "failed", "error": "1 failed"}),
                (
                    "phase_finished",
                    {"phase": "executing", "status": "verification_failed"},
                ),
                ("task_failed", {"error": "verification failed"}),
            ]
            status = "FAILED"
        if not self.terminal:
            status = "RUNNING"
        control = self.config.root / "runtime/control/projects" / str(project_id)
        journal = control / "events" / f"session_{session_id}_task_{task_id}.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(
            "".join(
                json.dumps({"event_type": kind, "details": details}) + "\n"
                for kind, details in events
            )
        )
        (journal.parent / f"session_{session_id}_task_{task_id}.jsonl.lock").write_text(
            ""
        )
        response = (
            control
            / "planning-evidence"
            / "planning-response-abc"
            / "provider-response.json"
        )
        response.parent.mkdir(parents=True, exist_ok=True)
        response.write_text(
            json.dumps(
                {
                    "correlation": {"provider_call_id": "planning-response-abc"},
                    "provider": {"role": "planning", "model": "qwen3-coder:30b"},
                    "capture_scope": "planning",
                    "request": {"response_format": {"type": "json_schema"}},
                    "planning_parse": {"status": "accepted"},
                    "plan_validation": {
                        "schema_validation": {"status": "passed"},
                        "final_plan_validation": {"status": "accepted"},
                    },
                    "timing": {
                        "request_started_utc": datetime.fromtimestamp(
                            now + 1, timezone.utc
                        ).isoformat()
                    },
                }
            )
        )
        change_dir = control / "change-sets" / str(execution_id)
        files = change_dir / "files"
        files.mkdir(parents=True, exist_ok=True)
        copied = []
        for relative, content in candidate.items():
            (files / relative).parent.mkdir(parents=True, exist_ok=True)
            (files / relative).write_text(content)
            copied.append(relative)
        if self.scenario == "unsafe_path":
            copied.append("../escape.py")
        (change_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "artifact_path": str(files),
                    "copied_files": sorted(copied),
                    "deleted_files": [],
                    "task_execution_id": execution_id,
                    "task_id": task_id,
                    "project_id": project_id,
                }
            )
        )
        self._db("update tasks set status=? where id=?", status, task_id)
        self._db(
            "insert into task_executions values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            execution_id,
            task_id,
            session_id,
            "fp",
            "qwen3-coder:30b",
            "qwen3-coder:30b",
            "openai_chat_completions",
            "openai_chat_completions",
            _db_time(now),
            _db_time(now + 30) if self.terminal else None,
            status,
            None if status == "DONE" else "execution_failure",
            1,
        )
        self._db(
            "insert into sessions values (?,?,?,?,?,?)",
            session_id,
            project_id,
            "completed",
            "automatic",
            _db_time(now),
            _db_time(now),
        )
        if self.on_execute:
            self.on_execute()
        return {
            "task_id": task_id,
            "session_id": session_id,
            "task_execution_id": execution_id,
            "celery_task_id": "fake-celery",
        }

    def request_count(self, method: str, suffix: str) -> int:
        return sum(1 for m, p, _ in self.calls if m == method and p.endswith(suffix))


@pytest.fixture
def world(tmp_path):
    repo_root = tmp_path / "checkout"
    (repo_root / "app").mkdir(parents=True)
    (repo_root / "app" / "core.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    _commit_all(repo_root, "checkout")
    config = dev_runtime.RuntimeConfig.for_repo(repo_root)
    seed = config.root / "workspace" / "toy-seed"
    (seed / "pkg").mkdir(parents=True)
    (seed / "tests").mkdir()
    (seed / "pkg" / "__init__.py").write_text("")
    (seed / "pkg" / "mod.py").write_text(BUGGY_SOURCE)
    (seed / "tests" / "test_mod.py").write_text(
        "from pkg.mod import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _commit_all(seed, "seed")
    case = {
        "schema_version": "benchmark.case.v1",
        "case_id": CASE_ID,
        "title": "toy",
        "status": "complete",
        "incomplete_reasons": [],
        "original_task": {
            "title": "Fix add",
            "description": "add() subtracts. Fix pkg/mod.py; do not edit tests/.",
            "origin_task_id": 1,
            "evidence_ref": None,
        },
        "seed": {
            "repository": "toy-seed",
            "commit": _git(seed, "rev-parse", "HEAD"),
            "tree": _git(seed, "rev-parse", "HEAD^{tree}"),
            "evidence_ref": None,
        },
        "baseline": {
            "command": "python -m pytest -q tests",
            "expected_failed_tests": ["test_add"],
            "expected_failed": 1,
            "expected_passed": 0,
            "evidence_ref": None,
        },
        "acceptance": {
            "command": "python -m pytest -q tests",
            "requires_unchanged_tests": True,
        },
        "authorized_source_paths": ["pkg/"],
        "protected_test_paths": ["tests/"],
        "behavioral_contract": "add returns the sum",
        "historical_evidence_refs": [],
    }
    fake = FakeOrchestrator(repo_root)
    head = _git(repo_root, "rev-parse", "HEAD")
    state = {
        "source": {
            "entering_head": head,
            "worktree_state": "evaluated-clean:0000",
            "committed_as": None,
            "snapshot_sha256": "s1",
            "latest_mtime_epoch": 1.0,
            "files": {"app/core.py": "h1"},
        },
        "ready": True,
        "active": [],
    }
    tokens: list[str] = []

    def observe(_config):
        process = {
            "pid": os.getpid(),
            "start_ticks": 1,
            "started_at_epoch": 2.0,
            "cmdline": ["python"],
            "exe": sys.executable,
            "cwd": str(repo_root),
            "environment": dict(POLICY_ENV, PYTHONPATH=str(repo_root)),
            "configuration_fingerprint": "cfg-1",
        }
        return {
            "status": "ready" if state["ready"] else "stopped",
            "ready": state["ready"],
            "problems": [] if state["ready"] else ["api_ready=false"],
            "processes": (
                {"api": process, "worker": dict(process)} if state["ready"] else {}
            ),
            "queue": "phase40b-r1-dev",
            "api_url": "http://127.0.0.1:18740",
        }

    def token_source():
        tokens.append("minted")
        return "dev-token"

    clock = {"now": 0.0}
    hooks = live.LiveHooks(
        observe_runtime=observe,
        source_identity=lambda _root: copy.deepcopy(state["source"]),
        benchmark_identity=lambda _root: {"sha256": "bench-1", "files": {}},
        dependency_identity=lambda: {"python": "3", "pip_freeze_sha256": "deps"},
        model_identity=lambda model: {
            "model": model,
            "available": True,
            "digest": "digest-1",
            "endpoint": "http://host.docker.internal:11434/v1",
        },
        active_tasks=lambda config: state["active"] + live.active_tasks(config),
        api_factory=lambda config, _runtime: live.DevApiClient(
            config, token_source, http=fake.http
        ),
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        monotonic=lambda: clock["now"],
    )
    ctx = live.LiveContext(
        repo_root=repo_root,
        output_root=repo_root / "docs/roadmap/reports/evidence/phase40d2",
        cases={CASE_ID: case},
        hooks=hooks,
        timeout_seconds=60,
        poll_seconds=5,
    )

    class World:
        pass

    w = World()
    w.ctx, w.fake, w.state, w.case, w.seed, w.tokens = (
        ctx,
        fake,
        state,
        case,
        seed,
        tokens,
    )
    w.campaign = "phase40d2-test"
    return w


def _init(world, attempts=2):
    outcome = live.init_campaign(world.ctx, world.campaign, [CASE_ID], attempts)
    assert outcome["status"] == live.STATUS_CAMPAIGN_CREATED, outcome
    return world.ctx.campaign_dir(world.campaign)


def _run(world, attempts=2, resume=None):
    return live.run_case(
        world.ctx, world.campaign, CASE_ID, attempts, resume_campaign=resume
    )


def _record(world, number=1) -> dict:
    path = (
        world.ctx.campaign_dir(world.campaign)
        / "attempts"
        / CASE_ID
        / f"attempt-{number}"
        / "record.json"
    )
    return json.loads(path.read_text())


def _seed_snapshot(seed: Path) -> tuple:
    return (
        _git(seed, "rev-parse", "HEAD"),
        _git(seed, "status", "--porcelain"),
        _git(seed, "for-each-ref"),
        _git(seed, "remote", "-v"),
    )


# -- end to end ---------------------------------------------------------------


def test_live_attempt_runs_through_api_and_independent_acceptance(world):
    before = _seed_snapshot(world.seed)
    directory = _init(world, attempts=1)
    outcome = _run(world, attempts=1)
    assert outcome["status"] == live.STATUS_CASE_COMPLETE, outcome
    record = _record(world)
    assert validate_attempt(record) == []
    assert record["attempted"] is True
    assert record["identity"]["campaign_id"] == world.campaign
    assert (
        record["identity"]["attempt_id"]
        == f"phase40d2:{world.campaign}:{CASE_ID}:attempt-1"
    )
    assert record["identity"]["source_identity"]["snapshot_sha256"] == "s1"
    assert "files" not in record["identity"]["source_identity"]
    assert record["identity"]["provider_identity"]["model_digest"] == "digest-1"
    assert record["identity"]["provider_identity"]["correlation"]["ambiguous"] is False
    assert record["planning"]["outcome"] == "accepted"
    assert record["execution"]["task_status"] == "DONE"
    assert record["change_set"]["changed_paths"] == ["pkg/mod.py"]
    assert record["change_set"]["review_decision"]["decision"] == "hold"
    assert record["change_set"]["promoted"] is False
    assert record["acceptance"]["attempted"] is True
    assert record["acceptance"]["result"] == "passed"
    assert record["acceptance"]["passed"] == 1
    assert record["acceptance"]["exit_code"] == 0
    assert record["reliability"]["independently_accepted"] is True
    assert record["reliability"]["failure_stage"] == "none"
    attempt = directory / "attempts" / CASE_ID / "attempt-1"
    acceptance = json.loads((attempt / "acceptance" / "acceptance.json").read_text())
    assert acceptance["integrity"]["valid"] and acceptance["promoted"] is False
    assert acceptance["seed_clone"]["head"] == world.case["seed"]["commit"]
    assert (attempt / "evidence" / "control" / "change-sets").is_dir()
    assert not list((attempt / "evidence").rglob("*.lock"))
    # The API request carried the original task verbatim and nothing promoted it.
    created = [p for m, path, p in world.fake.calls if path == "/api/v1/tasks"]
    assert created == [
        {
            "project_id": 101,
            "title": world.case["original_task"]["title"],
            "description": world.case["original_task"]["description"],
        }
    ]
    assert world.fake.request_count("POST", "/execute") == 1
    assert not any("promote" in p or "accept" in p for _, p, _ in world.fake.calls)
    # Canonical seed untouched; the disposable workspace cannot push to it.
    assert _seed_snapshot(world.seed) == before
    workspace = world.ctx.config.root / "workspace" / f"{world.campaign}-{CASE_ID}-a1"
    assert "example.invalid" in _git(workspace, "remote", "get-url", "origin")
    # The independent clone is disposable and removed after recording.
    clones = world.ctx.repo_root / ".dev-runtime/benchmark/phase40d2" / world.campaign
    assert not list(clones.glob("*-acceptance"))


@pytest.mark.parametrize(
    "scenario, accepted, stage",
    [
        ("done_no_candidate", False, "no_candidate"),
        ("wrong_fix", False, "acceptance"),
        ("edits_tests", False, "acceptance"),
        ("verification_failure", False, "verification"),
    ],
)
def test_done_or_local_pass_is_not_benchmark_success(world, scenario, accepted, stage):
    world.fake.scenario = scenario
    _init(world, attempts=1)
    _run(world, attempts=1)
    record = _record(world)
    assert record["reliability"]["independently_accepted"] is accepted
    assert record["reliability"]["failure_stage"] == stage
    metrics = live.campaign_metrics(world.ctx, world.campaign)["metrics"]
    assert metrics["acceptance"]["end_to_end_accepted_rate"]["numerator"] == 0
    if scenario == "done_no_candidate":
        assert record["acceptance"]["attempted"] is False
        assert record["acceptance"]["independent_acceptance_attempted"] is False
    if scenario == "edits_tests":
        assert record["acceptance"]["tests_modified"] is True
    if scenario == "verification_failure":
        assert record["reliability"]["attribution"]["kinds"] == ["unknown"]


def test_unsafe_change_set_path_fails_integrity_without_running_tests(world):
    world.fake.scenario = "unsafe_path"
    _init(world, attempts=1)
    _run(world, attempts=1)
    record = _record(world)
    assert record["acceptance"]["integrity"]["valid"] is False
    assert record["acceptance"]["exit_code"] is None
    assert record["reliability"]["independently_accepted"] is False
    assert not (world.ctx.repo_root.parent / "escape.py").exists()


# -- budget, restart, duplicate submission -------------------------------------


def test_budget_is_enforced_and_cannot_grow_after_restart(world):
    directory = _init(world, attempts=2)
    assert _run(world)["status"] == live.STATUS_CASE_COMPLETE
    assert world.fake.request_count("POST", "/execute") == 2
    again = _run(world, resume=world.campaign)
    assert again["status"] == live.STATUS_BUDGET_EXHAUSTED
    assert world.fake.request_count("POST", "/execute") == 2
    with pytest.raises(live.LiveError):
        live.reserve_attempt(directory, CASE_ID, 2)
    bigger = live.run_case(
        world.ctx, world.campaign, CASE_ID, 3, resume_campaign=world.campaign
    )
    assert bigger["status"] == live.STATUS_INVALID


def test_resume_requires_explicit_campaign_confirmation(world):
    _init(world, attempts=2)
    directory = world.ctx.campaign_dir(world.campaign)
    live.reserve_attempt(directory, CASE_ID, 2)
    outcome = _run(world)
    assert outcome["status"] == live.STATUS_RESUME_CONFIRMATION_REQUIRED
    assert world.fake.calls == []


def test_crash_after_execute_request_resumes_without_resubmitting(world):
    directory = _init(world, attempts=1)
    number = live.reserve_attempt(directory, CASE_ID, 1)
    attempt = directory / "attempts" / CASE_ID / f"attempt-{number}"
    progress = live.Progress(attempt / "progress.jsonl")
    progress.mark("started", worker_log_offset=0, runtime_processes={})
    clone = live.clone_seed(
        world.seed,
        world.ctx.config.root / "workspace" / f"{world.campaign}-{CASE_ID}-a1",
        world.case,
    )
    progress.mark("workspace_prepared", **clone)
    api = live.DevApiClient(world.ctx.config, lambda: "dev-token", http=world.fake.http)
    _, project = api.request("POST", "/api/v1/projects", {"workspace_path": "x"})
    progress.mark("project_created", project_id=project["id"])
    _, task = api.request(
        "POST",
        "/api/v1/tasks",
        {"project_id": project["id"], "title": "t", "description": "d"},
    )
    progress.mark("task_created", task_id=task["id"])
    progress.mark("execute_requested")
    api.request("POST", f"/api/v1/tasks/{task['id']}/execute", {})
    # -- the runner process dies here, before recording the 202 response --
    outcome = _run(world, attempts=1, resume=world.campaign)
    assert outcome["status"] == live.STATUS_BUDGET_EXHAUSTED or outcome["attempts"]
    assert world.fake.request_count("POST", "/execute") == 1
    assert world.fake.request_count("POST", "/api/v1/tasks") == 1
    record = _record(world)
    assert record["attempted"] is True and record["identity"]["task_id"] == task["id"]


def test_lost_execute_request_is_resolved_not_resent(world):
    directory = _init(world, attempts=1)
    live.reserve_attempt(directory, CASE_ID, 1)
    attempt = directory / "attempts" / CASE_ID / "attempt-1"
    progress = live.Progress(attempt / "progress.jsonl")
    progress.mark("started", worker_log_offset=0, runtime_processes={})
    api = live.DevApiClient(world.ctx.config, lambda: "dev-token", http=world.fake.http)
    _, project = api.request("POST", "/api/v1/projects", {"workspace_path": "x"})
    progress.mark("workspace_prepared")
    progress.mark("project_created", project_id=project["id"])
    _, task = api.request(
        "POST",
        "/api/v1/tasks",
        {"project_id": project["id"], "title": "t", "description": "d"},
    )
    progress.mark("task_created", task_id=task["id"])
    progress.mark("execute_requested")  # crashed before the request left
    world.fake._db("update tasks set status='PENDING' where id=?", task["id"])
    world.state["active"] = []
    outcome = live.run_case(
        world.ctx, world.campaign, CASE_ID, 1, resume_campaign=world.campaign
    )
    assert world.fake.request_count("POST", "/execute") == 0
    record = _record(world)
    assert record["attempted"] is False
    assert record["not_attempted_reason"].startswith("infrastructure:")
    assert outcome["attempts"][0]["attempted"] is False


def test_infrastructure_failure_before_submission_is_not_an_engineering_attempt(world):
    world.fake.scenario = "project_error"
    _init(world, attempts=1)
    _run(world, attempts=1)
    record = _record(world)
    assert record["attempted"] is False
    assert "project_create_failed" in record["not_attempted_reason"]
    metrics = live.campaign_metrics(world.ctx, world.campaign)["metrics"]
    assert metrics["slots"] == {"total": 1, "attempted": 0, "not_attempted": 1}


def test_timeout_is_recorded_and_session_stopped(world):
    world.fake.terminal = False
    _init(world, attempts=1)
    _run(world, attempts=1)
    record = _record(world)
    assert record["reliability"]["failure_stage"] == "timeout"
    assert any(
        item["code"] == "observation_timeout"
        for item in record["reliability"]["evidence_limitations"]
    )
    assert world.fake.request_count("POST", "/stop") == 1


# -- fail-closed preflight -----------------------------------------------------


def test_source_identity_change_before_attempt_terminates_campaign(world):
    _init(world)
    world.state["source"]["snapshot_sha256"] = "s2"
    world.state["source"]["files"]["app/core.py"] = "h2"
    outcome = _run(world)
    assert outcome["status"] == live.STATUS_CAMPAIGN_TERMINATED
    assert [call for call in world.fake.calls if call[0] != "GET"] == []
    assert not (world.ctx.campaign_dir(world.campaign) / "attempts").exists()
    assert (world.ctx.campaign_dir(world.campaign) / "terminated.json").is_file()
    assert (
        _run(world, resume=world.campaign)["status"] == live.STATUS_CAMPAIGN_TERMINATED
    )


def test_source_identity_change_during_attempt_stops_campaign(world):
    _init(world)

    def mutate():
        world.state["source"]["files"]["app/core.py"] = "edited-mid-run"

    world.fake.on_execute = mutate
    outcome = _run(world)
    assert outcome["status"] == live.STATUS_CAMPAIGN_TERMINATED
    assert world.fake.request_count("POST", "/execute") == 1
    assert outcome["attempts"][0]["source_identity_after"] == "MISMATCH"
    assert _record(world)["attempted"] is True  # the submitted attempt is preserved


@pytest.mark.parametrize(
    "mutation, failed_check",
    [
        (lambda w: w.state.__setitem__("ready", False), "runtime_ready"),
        (
            lambda w: w.state.__setitem__(
                "active", [{"task_id": 9, "status": "RUNNING"}]
            ),
            "one_active_task",
        ),
    ],
)
def test_preflight_failure_is_not_an_attempt(world, mutation, failed_check):
    directory = _init(world)
    mutation(world)
    outcome = _run(world)
    assert outcome["status"] == live.STATUS_PREFLIGHT_FAILED
    assert outcome["is_benchmark_attempt"] is False
    assert failed_check in outcome["failed_checks"]
    assert not (directory / "attempts").exists()
    assert list((directory / "preflight").glob("*.json"))


def test_campaign_init_rejects_incomplete_case_and_bad_policy(world):
    world.ctx.cases[CASE_ID]["status"] = "incomplete"
    assert (
        live.init_campaign(world.ctx, world.campaign, [CASE_ID], 2)["status"]
        == live.STATUS_INVALID
    )
    world.ctx.cases[CASE_ID]["status"] = "complete"
    assert (
        live.init_campaign(world.ctx, "Bad Id", [CASE_ID], 2)["status"]
        == live.STATUS_INVALID
    )
    assert (
        live.init_campaign(world.ctx, world.campaign, [CASE_ID], 3)["status"]
        == live.STATUS_INVALID
    )
    POLICY_ENV["WORKSPACE_REVIEW_POLICY"] = "auto"
    try:
        outcome = live.init_campaign(world.ctx, world.campaign, [CASE_ID], 2)
    finally:
        POLICY_ENV["WORKSPACE_REVIEW_POLICY"] = "hold_all"
    assert outcome["status"] == live.STATUS_INVALID
    assert any("WORKSPACE_REVIEW_POLICY" in item for item in outcome["errors"])


def test_one_live_runner_at_a_time(world):
    _init(world)
    with live.exclusive_runner(world.ctx.output_root):
        assert _run(world)["status"] == live.STATUS_BUSY
    assert world.fake.calls == []


# -- API client, authentication, isolation -------------------------------------


def test_client_cannot_reach_production_api_or_promotion_routes(world):
    production = dev_runtime.RuntimeConfig(
        repo_root=world.ctx.repo_root,
        root=world.ctx.config.root,
        api_port=8000,
    )
    with pytest.raises(live.LiveError, match="non_development_api_refused"):
        live.DevApiClient(production, lambda: "dev-token", http=world.fake.http)
    client = live.DevApiClient(
        world.ctx.config, lambda: "dev-token", http=world.fake.http
    )
    for method, path in [
        ("POST", "/api/v1/tasks/5/promote"),
        ("POST", "/api/v1/tasks/5/accept"),
        ("POST", "/api/v1/tasks/5/change-set/accept"),
        ("POST", "/api/v1/tasks/5/retry"),
        ("DELETE", "/api/v1/projects/5"),
        ("GET", "/api/v1/tasks/5/../../admin"),
    ]:
        with pytest.raises(live.LiveError, match="request_not_allowed"):
            client.request(method, path)
    assert world.fake.calls == []


def test_expired_token_is_reminted_and_never_recorded(world):
    minted = []

    def source():
        minted.append(1)
        return "dev-token"

    client = live.DevApiClient(world.ctx.config, source, http=world.fake.http)
    world.fake.unauthorized_once = True
    status, me = client.request("GET", "/api/v1/auth/me")
    assert status == 200 and me["email"] == dev_runtime.EVALUATOR_EMAIL
    assert len(minted) == 2
    assert "dev-token" not in json.dumps(client.audit)


def test_runner_live_requires_campaign(world, capsys):
    config = runner.build_run_config(CASE_ID, 1, live=True)
    outcome = runner.run_live(
        config,
        repo_root=world.ctx.repo_root,
        campaign_id=None,
        output_root=world.ctx.output_root,
    )
    assert outcome["status"] == runner.STATUS_INVALID_CONFIG
    assert (
        runner.dry_run(config, repo_root=REPO)["status"] == runner.STATUS_INVALID_CONFIG
    )
    code = cli.main(["run", "--case", "workflow-a", "--max-attempts", "1", "--live"])
    assert code == cli.EXIT_INVALID
    assert "requires --campaign" in capsys.readouterr().out


# -- identity, redaction, evidence ---------------------------------------------


def test_source_identity_tracks_loaded_source_not_tests(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app" / "tests").mkdir(parents=True)
    (repo / "app" / "core.py").write_text("A = 1\n")
    (repo / "app" / "tests" / "test_core.py").write_text("")
    (repo / "requirements.txt").write_text("x\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _commit_all(repo, "c")
    clean = identity.source_identity(repo)
    assert clean["worktree_state"].startswith("evaluated-clean:")
    assert set(clean["files"]) == {"app/core.py", "requirements.txt"}
    (repo / "app" / "tests" / "test_core.py").write_text("x = 2\n")
    (repo / "app" / "tests" / "test_new.py").write_text("")
    assert identity.source_mismatch(clean, identity.source_identity(repo)) == []
    (repo / "app" / "core.py").write_text("A = 2\n")
    (repo / "app" / "extra.py").write_text("")
    changed = identity.source_identity(repo)
    assert changed["worktree_state"].startswith("evaluated-dirty:")
    assert changed["evaluated_untracked"] == ["app/extra.py"]
    assert identity.source_mismatch(clean, changed) == [
        "snapshot_sha256",
        "file:app/core.py",
        "file:app/extra.py",
    ]


def test_runtime_identity_never_carries_secret_values(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SECRET_KEY": "s" * 40,
        "OPENAI_API_KEY": "",
        "WORKSPACE_REVIEW_POLICY": "hold_all",
    }
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=env,
        start_new_session=True,
    )
    try:
        observed = identity.process_identity(child.pid)
    finally:
        child.kill()  # only the child this test spawned
        child.wait(timeout=10)
    text = json.dumps(observed)
    assert "s" * 40 not in text
    assert observed["environment"]["SECRET_KEY"] == "<redacted:set>"
    assert observed["environment"]["OPENAI_API_KEY"] == "<empty>"
    rotated = dict(observed["environment"], SECRET_KEY="<redacted:set>")
    assert (
        identity.configuration_fingerprint(rotated)
        == observed["configuration_fingerprint"]
    )
    evidence = tmp_path / "log.txt"
    evidence.write_text(f"token={'t' * 30}\n")
    assert live.redact_secrets([evidence], ["t" * 30, ""]) == ["log.txt"]
    assert "t" * 30 not in evidence.read_text()


def test_provider_correlation_flags_ambiguity(tmp_path):
    response = tmp_path / "planning-evidence" / "r1" / "provider-response.json"
    response.parent.mkdir(parents=True)
    response.write_text(
        json.dumps(
            {
                "correlation": {"provider_call_id": "r1"},
                "timing": {"request_started_utc": "2026-10-26T10:00:00+00:00"},
            }
        )
    )
    inside = datetime(2026, 10, 26, 9, 59, tzinfo=timezone.utc).timestamp()
    ok = live.provider_evidence(tmp_path, inside, inside + 120, task_count=1)
    assert ok["ambiguous"] is False and ok["responses"][0]["within_execution_window"]
    shared = live.provider_evidence(tmp_path, inside, inside + 120, task_count=2)
    assert shared["ambiguous"] is True
    late = live.provider_evidence(tmp_path, inside - 600, inside - 300, task_count=1)
    assert late["ambiguous"] is True and late["outside_window"] == ["r1"]


def test_outputs_never_overwrite_or_enter_historical_evidence(world):
    historical = world.ctx.repo_root / "docs/roadmap/reports/evidence/phase40c4"
    assert live.protected_output_problem(historical / "x", world.ctx.repo_root)
    assert live.protected_output_problem(
        world.ctx.config.root / "anything", world.ctx.repo_root
    )
    protected = live.LiveContext(
        repo_root=world.ctx.repo_root,
        output_root=historical,
        cases=world.ctx.cases,
        hooks=world.ctx.hooks,
    )
    assert (
        live.init_campaign(protected, world.campaign, [CASE_ID], 1)["status"]
        == live.STATUS_INVALID
    )
    _init(world, attempts=1)
    assert (
        live.init_campaign(world.ctx, world.campaign, [CASE_ID], 1)["status"]
        == live.STATUS_INVALID
    )
    _run(world, attempts=1)
    record = (
        world.ctx.campaign_dir(world.campaign)
        / "attempts"
        / CASE_ID
        / "attempt-1"
        / "record.json"
    )
    with pytest.raises(FileExistsError):
        live._write_new(record, {})


def test_campaign_metrics_carry_denominators(world):
    _init(world, attempts=2)
    world.fake.on_execute = lambda: setattr(
        world.fake, "scenario", "verification_failure"
    )
    _run(world)
    metrics = live.campaign_metrics(world.ctx, world.campaign)
    assert metrics["records"] == 2
    e2e = metrics["metrics"]["acceptance"]["end_to_end_accepted_rate"]
    assert (e2e["numerator"], e2e["denominator"]) == (1, 2)
    assert e2e["denominator_definition"]
    assert metrics["per_case"][CASE_ID]["slots"]["attempted"] == 2
    assert metrics["metrics"]["reliability"]["failure_distribution_by_stage"] == {
        "none": 1,
        "verification": 1,
    }
    assert "not a statistical reliability estimate" in metrics["sample_size_note"]


def test_close_campaign_records_cleanup_without_touching_processes(world):
    _init(world, attempts=1)
    result = live.close_campaign(world.ctx, world.campaign, stop_runtime=False)
    assert result["runtime_status"] == "stopped"
    assert result["source_identity_unchanged"] is True
    assert all(all(item.values()) for item in result["seeds_unchanged"].values())
    assert (world.ctx.campaign_dir(world.campaign) / "cleanup.json").is_file()


def test_canonical_case_files_are_unchanged_by_live_mode():
    cases = json.loads((REPO / "scripts/benchmark/cases/workflow-a.json").read_text())
    assert cases["seed"]["commit"] == "6cd8ab904f695634e1b1b0b8c6496f09f55bc062"
    from scripts.benchmark.evidence_loader import load_cases

    loaded = load_cases()
    assert {key for key, case in loaded.items() if case["status"] == "complete"} == {
        "workflow-a",
        "workflow-b",
        "workflow-c",
    }
