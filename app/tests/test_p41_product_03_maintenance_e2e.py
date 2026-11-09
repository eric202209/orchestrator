"""P41-PRODUCT-03: representative maintenance Task through the production Worker.

A package-module bug fix plus an appended regression test, executed through the
mounted Task API, production Planning/Validator, structured file operations,
pytest verification, debug repair, and the Completion Coordinator. Only the
provider responses are fixed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from celery.result import allow_join_result
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.dependencies import get_current_active_user, get_current_user
from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskStatus,
    User,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
)
from app.services.session.session_runtime_service import queue_task_for_session
from app.tasks import worker
from app.tests.test_p41_product_01_deterministic_e2e import (
    _DeterministicProviderAdapter,
    _read_jsonl,
)

TASK_DESCRIPTION = (
    "Fix textkit/slug.py so slugify() never returns leading or trailing hyphens; "
    "for example slugify('Hello, World!') must return 'hello-world'. Keep the "
    "slugify(text) signature, add a regression test to tests/test_slug.py, and "
    "make sure the test suite passes."
)
SLUG_SOURCE = (
    "import re\n\n\n"
    "def slugify(text):\n"
    '    """Return a lowercase, hyphen-separated slug."""\n'
    "    text = text.strip().lower()\n"
    '    return re.sub(r"[^a-z0-9]+", "-", text)\n'
)
EXISTING_TEST = (
    "from textkit.slug import slugify\n\n\n"
    "def test_slugify_spaces():\n"
    '    assert slugify("Hello World") == "hello-world"\n'
)
REGRESSION_TEST = (
    "\n\n"
    "def test_slugify_strips_edge_hyphens():\n"
    '    assert slugify("Hello, World!") == "hello-world"\n'
    '    assert slugify("  --Already Slugged--  ") == "already-slugged"\n'
)
BUGGY_LINE = '    return re.sub(r"[^a-z0-9]+", "-", text)\n'
CORRECT_FIX = '    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")\n'
# Plausible but wrong: strips whitespace, not the hyphens the regression targets.
WRONG_FIX = '    return re.sub(r"[^a-z0-9]+", "-", text).strip()\n'
FIXED_SOURCE = SLUG_SOURCE.replace(BUGGY_LINE, CORRECT_FIX)
FOCUSED_TEST = "python3 -m pytest -q tests/test_slug.py"
SUITE_TEST = "python3 -m pytest -q tests"
DIFF_REPAIR_PROMPT_MARKER = "Unified diff capsule:"


def _maintenance_plan(fix_line: str) -> list[dict[str, Any]]:
    """A Planner-shaped multi-step plan: fix, add regression test, run suite."""

    return [
        {
            "step_number": 1,
            "description": "Strip leading and trailing hyphens in textkit.slug.slugify",
            "commands": [],
            "verification": FOCUSED_TEST,
            "rollback": None,
            "expected_files": ["textkit/slug.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "textkit/slug.py",
                    "old": BUGGY_LINE,
                    "new": fix_line,
                }
            ],
        },
        {
            "step_number": 2,
            "description": "Add a regression test for edge hyphens to tests/test_slug.py",
            "commands": [],
            "verification": FOCUSED_TEST,
            "rollback": None,
            "expected_files": ["tests/test_slug.py"],
            "ops": [
                {
                    "op": "append_file",
                    "path": "tests/test_slug.py",
                    "content": REGRESSION_TEST,
                }
            ],
        },
        {
            "step_number": 3,
            "description": "Run the full test suite",
            "commands": [SUITE_TEST],
            "verification": SUITE_TEST,
            "rollback": None,
            "expected_files": [],
        },
    ]


class _MaintenanceProviderAdapter(_DeterministicProviderAdapter):
    """Fixed responses; discovery reads the module, debug repair is scripted."""

    def __init__(self, plan: list[dict[str, Any]], repair: Any = None) -> None:
        super().__init__(plan=plan)
        self.repair = repair

    async def execute_task(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        if "READ-ONLY DISCOVERY ONLY" in prompt:
            self.calls.append({"kind": "discovery", "prompt": prompt})
            output = {"action": "read_file", "path": "textkit/slug.py"}
            return {"status": "completed", "output": json.dumps(output)}
        if self.repair is not None and (
            "debug repair" in prompt.lower() or DIFF_REPAIR_PROMPT_MARKER in prompt
        ):
            repair = self.repair(prompt) if callable(self.repair) else self.repair
            self.calls.append(
                {"kind": "debug_repair", "prompt": prompt, "response": repair}
            )
            return {"status": "completed", "output": json.dumps(repair)}
        return await super().execute_task(prompt, **kwargs)


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, check=True, capture_output=True, text=True
    ).stdout


def _slugify_probe(workspace: Path) -> str:
    code = "from textkit.slug import slugify; print(slugify('Hello, World!'))"
    return subprocess.run(
        [sys.executable, "-c", code], cwd=workspace, capture_output=True, text=True
    ).stdout


def _source_repair_derived_from_prompt(prompt: str) -> list[dict[str, Any]]:
    """Build an ops_fix only from exact source text supplied to the adapter."""

    marker = "--- textkit/slug.py\n"
    start = prompt.find(marker)
    if start < 0:
        return []
    start += len(marker)
    end = prompt.find("\n--- ", start)
    source = prompt[start : end if end >= 0 else None]
    old = next(
        (
            line
            for line in source.splitlines(keepends=True)
            if "re.sub(" in line and ".strip()" in line
        ),
        "",
    )
    if not old:
        return []
    new = old.replace(".strip()", '.strip("-")')
    return [
        {
            "title": "Strip edge hyphens in slugify",
            "repair_type": "ops_fix",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "textkit/slug.py",
                    "old": old,
                    "new": new,
                }
            ],
            "verification_command": FOCUSED_TEST,
        }
    ]


def _run_scenario(
    *,
    api_app,
    db_session_factory,
    isolated_workspace_root: Path,
    isolated_runtime_root: Path,
    monkeypatch,
    fix_line: str,
    repair: Any = None,
) -> dict[str, Any]:
    for setting, value in (
        ("INLINE_PLANNING", True),
        ("PLANNING_BACKEND", "openai_chat_completions"),
        ("EXECUTION_BACKEND", "openai_chat_completions"),
        ("PLANNER_MODEL", "qwen-local"),
        ("EXECUTION_MODEL", "qwen-local"),
        ("AGENT_MODEL", "qwen-local"),
        ("PLANNING_ADAPTATION_PROFILE", "ollama_default"),
        ("EXECUTION_ADAPTATION_PROFILE", "ollama_default"),
        ("EXECUTION_CONTEXT_TOKENS", 64_000),
        ("DEBUG_REPAIR_CONTEXT_TOKENS", 64_000),
        ("PLANNING_REPAIR_ENABLED", False),
        ("RUNTIME_WORKSPACE_ENABLED", False),
        ("DEMO_MODE", False),
    ):
        monkeypatch.setattr(settings, setting, value)

    db = db_session_factory()
    workspace = isolated_workspace_root / "p41-product-03-textkit"
    (workspace / "textkit").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "textkit" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "textkit" / "slug.py").write_text(SLUG_SOURCE, encoding="utf-8")
    (workspace / "tests" / "test_slug.py").write_text(EXISTING_TEST, encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "add", ".")
    _git(
        workspace,
        "-c",
        "user.email=p41-product-03@example.invalid",
        "-c",
        "user.name=P41-PRODUCT-03",
        "commit",
        "-qm",
        "disposable baseline",
    )
    baseline_sha = _git(workspace, "rev-parse", "HEAD").strip()
    # Precondition: the bug is real and the existing suite does not catch it.
    assert _slugify_probe(workspace) == "hello-world-\n"

    project = Project(
        name="P41 PRODUCT 03 disposable textkit",
        description="Temporary deterministic maintenance scenario",
        workspace_path=str(workspace),
        user_id=1,
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    # In-memory identity only; no User row, token, or live session is created.
    evaluator = User(
        id=1,
        email="eval@local.dev",
        hashed_password="offline-test-only",
        is_active=True,
    )
    api_app.dependency_overrides[get_current_user] = lambda: evaluator
    api_app.dependency_overrides[get_current_active_user] = lambda: evaluator
    client = TestClient(api_app)
    try:
        response = client.post(
            "/api/v1/tasks",
            json={
                "project_id": project.id,
                "title": "Fix slugify edge hyphens",
                "description": TASK_DESCRIPTION,
            },
        )
    finally:
        client.close()
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]

    session = SessionModel(
        project_id=project.id,
        name="P41 PRODUCT 03 disposable execution",
        description=TASK_DESCRIPTION,
        status="pending",
        execution_mode="manual",
        is_active=False,
        instance_id=f"p41-product-03-{uuid.uuid4()}",
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    provider = _MaintenanceProviderAdapter(_maintenance_plan(fix_line), repair)
    monkeypatch.setattr(worker, "create_agent_runtime", lambda *a, **k: provider)
    monkeypatch.setattr(worker, "get_db_session", db_session_factory)
    real_worker_task = worker.execute_orchestration_task
    dispatches: list[dict[str, Any]] = []

    def deliver(kwargs: dict[str, Any], task_id: str | None = None):
        celery_id = task_id or f"p41-product-03-{uuid.uuid4()}"
        dispatches.append({"kwargs": dict(kwargs), "celery_task_id": celery_id})
        applied = real_worker_task.apply(kwargs=kwargs, task_id=celery_id)
        with allow_join_result():
            outcome = applied.get(propagate=True, disable_sync_subtasks=False)
        return SimpleNamespace(id=celery_id, result=outcome)

    class InlineWorkerTask:
        @staticmethod
        def delay(**kwargs: Any):
            return deliver(kwargs)

        @staticmethod
        def apply_async(
            *, kwargs: dict[str, Any], task_id: str | None = None, **_options: Any
        ):
            return deliver(kwargs, task_id=task_id)

    monkeypatch.setattr(worker, "execute_orchestration_task", InlineWorkerTask)

    queue_result = queue_task_for_session(db, session, task_id, timeout_seconds=120)
    task_execution_id = queue_result["task_execution_id"]
    assert task_execution_id

    db.expire_all()
    task = db.get(Task, task_id)
    session = db.get(SessionModel, session.id)
    task_execution = db.get(TaskExecution, task_execution_id)
    session_task = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session.id, SessionTask.task_id == task_id)
        .one()
    )
    checkpoints = (
        db.query(TaskCheckpoint)
        .filter(
            TaskCheckpoint.session_id == session.id,
            TaskCheckpoint.task_id == task_id,
        )
        .order_by(TaskCheckpoint.id.asc())
        .all()
    )
    plan_verdicts = [
        json.loads(c.state_snapshot)
        for c in checkpoints
        if c.checkpoint_type == "validation_plan"
    ]
    event_path = next(
        isolated_runtime_root.rglob(f"session_{session.id}_task_{task_id}.jsonl")
    )
    events = _read_jsonl(event_path)
    verification_path = next(
        isolated_runtime_root.rglob(
            f"verification_session_{session.id}_task_{task_id}_"
            f"execution_{task_execution_id}.jsonl"
        ),
        None,
    )
    verification_records = (
        _read_jsonl(verification_path) if verification_path is not None else []
    )
    snapshot_path = next(
        isolated_runtime_root.rglob(
            f"session_{session.id}_task_{task_id}_state_snapshots.jsonl"
        ),
        None,
    )
    result = {
        "task_steps": json.loads(task.steps) if task.steps else None,
        "state_snapshots": (
            _read_jsonl(snapshot_path) if snapshot_path is not None else []
        ),
        "statuses": (task.status, session_task.status, task_execution.status),
        "session_status": session.status,
        "plan_verdicts": plan_verdicts,
        "events": events,
        "verification_records": verification_records,
        "provider": provider,
        "dispatch_count": len(dispatches),
        "task_count": db.query(Task).filter(Task.project_id == project.id).count(),
        "workspace": workspace,
        "baseline_sha": baseline_sha,
    }
    result["trace"] = {
        "statuses": [str(s) for s in result["statuses"]],
        "session": session.status,
        "verdicts": [(v.get("status"), v.get("reasons")) for v in plan_verdicts],
        "event_types": [e["event_type"] for e in events],
        "verification": [
            (
                r.get("declared_command"),
                r.get("verification_status"),
                r.get("process_exit_code"),
            )
            for r in verification_records
        ],
    }
    db.close()
    return result


def _event(result: dict[str, Any], event_type: str) -> dict[str, Any]:
    return next(e for e in result["events"] if e["event_type"] == event_type)


def _assert_plan_admitted(result: dict[str, Any]) -> None:
    verdicts = result["plan_verdicts"]
    assert verdicts, result["trace"]
    assert any(v["status"] in {"accepted", "warning"} for v in verdicts)
    assert result["dispatch_count"] == 1
    assert result["task_count"] == 1


def _assert_truthful_failure_restores_baseline(result: dict[str, Any]) -> None:
    trace = result["trace"]
    assert result["statuses"] == (TaskStatus.FAILED,) * 3, trace
    assert result["session_status"] != "completed", trace
    assert not [e for e in result["events"] if e["event_type"] == "task_completed"]
    workspace = result["workspace"]
    assert (workspace / "textkit" / "slug.py").read_text() == SLUG_SOURCE
    assert (workspace / "tests" / "test_slug.py").read_text() == EXISTING_TEST
    assert _slugify_probe(workspace) == "hello-world-\n"


@pytest.fixture
def scenario(
    api_app,
    db_session_factory,
    isolated_workspace_root,
    isolated_runtime_root,
    monkeypatch,
):
    def run(fix_line: str, repair: Any = None) -> dict[str, Any]:
        return _run_scenario(
            api_app=api_app,
            db_session_factory=db_session_factory,
            isolated_workspace_root=isolated_workspace_root,
            isolated_runtime_root=isolated_runtime_root,
            monkeypatch=monkeypatch,
            fix_line=fix_line,
            repair=repair,
        )

    return run


@pytest.mark.integration_contract
def test_maintenance_bugfix_with_regression_test_completes_truthfully(scenario):
    result = scenario(CORRECT_FIX)
    trace = result["trace"]
    _assert_plan_admitted(result)
    assert result["statuses"] == (TaskStatus.DONE,) * 3, trace
    assert result["session_status"] == "completed", trace

    events = result["events"]
    success_events = [e for e in events if e["event_type"] == "task_completed"]
    assert len(success_events) == 1, trace
    passed_suite_indexes = [
        index
        for index, event in enumerate(events)
        if event["event_type"] == "step_finished"
        and any(
            r.get("verification_status") == "passed"
            and r.get("declared_command") == SUITE_TEST
            for r in event.get("details", {}).get("verification_evidence_records", [])
        )
    ]
    assert passed_suite_indexes, trace
    assert max(passed_suite_indexes) < events.index(success_events[0]), trace
    for command in (FOCUSED_TEST, SUITE_TEST):
        assert any(
            r["declared_command"] == command
            and r["verification_status"] == "passed"
            and r["process_exit_code"] == 0
            for r in result["verification_records"]
        ), (command, trace)

    # Independent workspace verification: not inferred from Task DONE.
    workspace = result["workspace"]
    assert (workspace / "textkit" / "slug.py").read_text() == FIXED_SOURCE
    assert (workspace / "tests" / "test_slug.py").read_text() == (
        EXISTING_TEST + REGRESSION_TEST
    )
    assert _slugify_probe(workspace) == "hello-world\n"
    independent = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    assert independent.returncode == 0, independent.stdout + independent.stderr
    assert "2 passed" in independent.stdout
    changed = set(
        _git(workspace, "diff", "--name-only", result["baseline_sha"]).split()
    )
    assert changed == {"textkit/slug.py", "tests/test_slug.py"}
    untracked = _git(workspace, "ls-files", "--others", "--exclude-standard").split()
    # The product adds its managed workspace-guard .gitignore; nothing else.
    assert untracked == [".gitignore"], untracked


@pytest.mark.integration_contract
def test_wrong_fix_without_usable_repair_fails_truthfully_and_restores(scenario):
    result = scenario(WRONG_FIX)
    _assert_plan_admitted(result)
    _assert_truthful_failure_restores_baseline(result)
    assert any(
        r["declared_command"] == FOCUSED_TEST
        and r["verification_status"] == "failed"
        and r["process_exit_code"] != 0
        for r in result["verification_records"]
    ), result["trace"]


# A well-formed bounded source repair for WRONG_FIX: the production parser
# accepts it and the existing stale-replace guard finds its ``old`` text.
VALID_SOURCE_REPAIR = [
    {
        "title": "Strip edge hyphens in slugify",
        "repair_type": "ops_fix",
        "ops": [
            {
                "op": "replace_in_file",
                "path": "textkit/slug.py",
                "old": WRONG_FIX,
                "new": CORRECT_FIX,
            }
        ],
        "verification_command": FOCUSED_TEST,
    }
]

UNSUPPORTED_COMMAND_REPAIR = [
    {
        "title": "Strip edge hyphens in slugify",
        "command": "sed -i 's/\\.strip()/\\.strip(\"-\")/' textkit/slug.py",
        "verification_command": FOCUSED_TEST,
    }
]


class RepairedTaskFalseFailure(AssertionError):
    """A successful, fully verified debug repair was not allowed to complete."""


@pytest.mark.integration_contract
def test_successful_debug_repair_reaches_verified_completion(scenario):
    result = scenario(WRONG_FIX, VALID_SOURCE_REPAIR)
    trace = result["trace"]
    _assert_plan_admitted(result)
    events = result["events"]
    debug_call = next(
        call for call in result["provider"].calls if call["kind"] == "debug_repair"
    )
    assert DIFF_REPAIR_PROMPT_MARKER in debug_call["prompt"]
    assert "Primary file: tests/test_slug.py" in debug_call["prompt"]
    assert WRONG_FIX in debug_call["prompt"]
    assert debug_call["response"] == VALID_SOURCE_REPAIR

    # Preconditions: the failure is genuine and the production repair fixed it.
    assert any(
        r["declared_command"] == FOCUSED_TEST
        and r["verification_status"] == "failed"
        and r["process_exit_code"] != 0
        for r in result["verification_records"]
    ), trace
    assert any(e["event_type"] == "debug_repair_attempted" for e in events), trace
    assert not any(e["event_type"] == "repair_rejected" for e in events), trace
    assert [c["kind"] for c in result["provider"].calls].count("debug_repair") == 1
    final_record = result["verification_records"][-1]
    assert final_record["declared_command"] == SUITE_TEST, trace
    assert final_record["verification_status"] == "passed", trace
    assert final_record["process_exit_code"] == 0, trace
    workspace = result["workspace"]
    assert (workspace / "textkit" / "slug.py").read_text() == FIXED_SOURCE
    assert (workspace / "tests" / "test_slug.py").read_text() == (
        EXISTING_TEST + REGRESSION_TEST
    )
    assert _slugify_probe(workspace) == "hello-world\n"

    completion_verdicts = [
        e["details"]
        for e in events
        if e["event_type"] == "validation_result"
        and e["details"].get("stage") == "task_completion"
    ]
    successes = [e for e in events if e["event_type"] == "task_completed"]
    if result["statuses"] != (TaskStatus.DONE,) * 3 or len(successes) != 1:
        raise RepairedTaskFalseFailure(
            json.dumps(
                {
                    "statuses": [str(s) for s in result["statuses"]],
                    "task_completion": [
                        (v.get("status"), v.get("reasons")) for v in completion_verdicts
                    ],
                    "workspace_changes": _git(workspace, "status", "--short"),
                }
            )
        )
    assert result["session_status"] == "completed", trace
    assert all(v["status"] == "accepted" for v in completion_verdicts), trace
    assert completion_verdicts, trace
    for command in (FOCUSED_TEST, SUITE_TEST):
        assert any(
            r["declared_command"] == command
            and r["verification_status"] == "passed"
            and r["process_exit_code"] == 0
            for r in result["verification_records"]
        ), (command, trace)

    # P41-PRODUCT-04: the persisted Plan is still the accepted Plan, so its
    # identity matches the one bound into the accepted Path Authority.
    accepted_identities = [
        v["details"]["accepted_path_authority"]["accepted_plan_identity"]
        for v in result["plan_verdicts"]
        if v.get("status") == "accepted"
        and (v.get("details") or {}).get("accepted_path_authority")
    ]
    assert len(accepted_identities) == 1, trace
    assert result["task_steps"][0]["ops"] != VALID_SOURCE_REPAIR[0]["ops"]
    assert result["task_steps"][0]["ops"][0]["new"] == WRONG_FIX
    assert accepted_plan_identity(result["task_steps"]) == accepted_identities[0]

    # The repair that actually ran stays in durable execution evidence: the
    # append-only state snapshot journal recorded the repaired step's ops.
    repaired_ops = [
        step.get("ops")
        for snapshot in result["state_snapshots"]
        for step in snapshot.get("plan_steps") or []
        if isinstance(step, dict)
    ]
    assert VALID_SOURCE_REPAIR[0]["ops"] in repaired_ops, trace

    # Independent workspace verification: not inferred from Task DONE.
    independent = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    assert independent.returncode == 0, independent.stdout + independent.stderr
    assert "2 passed" in independent.stdout
    changed = set(
        _git(workspace, "diff", "--name-only", result["baseline_sha"]).split()
    )
    assert changed == {"textkit/slug.py", "tests/test_slug.py"}


@pytest.mark.integration_contract
def test_diff_scoped_prompt_rejects_unsupported_command_repair(scenario):
    result = scenario(WRONG_FIX, UNSUPPORTED_COMMAND_REPAIR)
    debug_call = next(
        call for call in result["provider"].calls if call["kind"] == "debug_repair"
    )
    prompt = debug_call["prompt"]
    rejected = [
        event
        for event in result["events"]
        if event["event_type"] == "repair_rejected"
        and event["details"].get("debug_repair_rejection_reason")
    ]

    # The command response remains rejected by the real production parser.
    assert DIFF_REPAIR_PROMPT_MARKER in prompt
    assert (
        "step object must include title, command, and verification_command"
        not in prompt
    )
    assert "Touch only files shown in the diff capsule" not in prompt
    assert "Primary file: tests/test_slug.py" in prompt
    assert '"repair_type":"ops_fix"' in prompt
    assert "- textkit/slug.py" in prompt
    assert debug_call["response"] == UNSUPPORTED_COMMAND_REPAIR
    assert rejected, result["trace"]
    assert rejected[-1]["details"]["debug_repair_rejection_reason"] == (
        "semantic_string_edit_rejected"
    )
    assert result["statuses"] == (TaskStatus.FAILED,) * 3
    assert not [
        event for event in result["events"] if event["event_type"] == "task_completed"
    ]


@pytest.mark.integration_contract
def test_prompt_supplied_source_is_sufficient_for_an_accepted_repair(scenario):
    result = scenario(WRONG_FIX, _source_repair_derived_from_prompt)
    debug_call = next(
        call for call in result["provider"].calls if call["kind"] == "debug_repair"
    )
    prompt = debug_call["prompt"]
    response = debug_call["response"]

    assert "--- textkit/slug.py\n" in prompt
    assert response, "The deterministic adapter must derive the repair from prompt text"
    repair = response[0]
    assert repair["ops"][0]["old"] in prompt
    assert repair["ops"][0]["new"] not in prompt
    assert repair["ops"][0]["path"] == "textkit/slug.py"
    assert "Primary file: tests/test_slug.py" in prompt
    assert "Complete current source files shown: textkit/slug.py" in prompt
    source_context_start = prompt.index("Current source context (bounded):\n") + len(
        "Current source context (bounded):\n"
    )
    source_context_end = prompt.index("\nRules:\n", source_context_start)
    assert len(prompt[source_context_start:source_context_end]) <= 3000
    attempt = next(
        event
        for event in result["events"]
        if event["event_type"] == "debug_repair_attempted"
    )
    assert attempt["details"]["diff_capsule_line_count"] <= 120
    assert not any(
        event["event_type"] == "repair_rejected" for event in result["events"]
    )
    assert result["statuses"] == (TaskStatus.DONE,) * 3, result["trace"]
    assert result["session_status"] == "completed", result["trace"]
    assert (
        len([e for e in result["events"] if e["event_type"] == "task_completed"]) == 1
    )
    assert (result["workspace"] / "textkit" / "slug.py").read_text() == FIXED_SOURCE
    assert (result["workspace"] / "tests" / "test_slug.py").read_text() == (
        EXISTING_TEST + REGRESSION_TEST
    )
    assert any(
        record["declared_command"] == FOCUSED_TEST
        and record["verification_status"] == "passed"
        and record["process_exit_code"] == 0
        for record in result["verification_records"]
    )
    assert any(
        record["declared_command"] == SUITE_TEST
        and record["verification_status"] == "passed"
        and record["process_exit_code"] == 0
        for record in result["verification_records"]
    )


@pytest.mark.integration_contract
def test_diff_scoped_prompt_keeps_unauthorized_source_paths_rejected(scenario):
    unauthorized_repair = [
        {
            "title": "Attempt an out-of-workspace repair",
            "repair_type": "ops_fix",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "../outside.py",
                    "old": "x = 1\n",
                    "new": "x = 2\n",
                }
            ],
            "verification_command": FOCUSED_TEST,
        }
    ]
    result = scenario(WRONG_FIX, unauthorized_repair)
    rejection = next(
        event
        for event in result["events"]
        if event["event_type"] == "repair_rejected"
        and event["details"].get("debug_repair_rejection_reason")
    )

    assert rejection["details"]["debug_repair_rejection_reason"] == (
        "unauthorized_source_path"
    )
    assert result["statuses"] == (TaskStatus.FAILED,) * 3
    assert not [
        event for event in result["events"] if event["event_type"] == "task_completed"
    ]
