"""P42-V17-N: bounded debug repair safety corrections (provider-free).

R1  the stale-replace correction runs only on the role-owned, tool-less
    DEBUG_REPAIR runtime, never on the Execution runtime;
R1g the correction targets must be unchanged when the correction returns;
R2  changed-file context labels complete and truncated source explicitly.

Fixture: TE 395 runtime-workspace ``inventory/service.py`` after accepted plan
step 2, vendored with a SHA-256 pin so CI never reads gitignored evidence.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.agents.agent_backends import get_backend_descriptor
from app.services.agents.agent_runtime import BackendRole
from app.services.agents.interfaces import UnsupportedCapabilityError
from app.services.orchestration.diagnostics.debug_feedback import (
    build_bounded_debug_repair_changed_file_context,
    build_bounded_debug_repair_prompt_with_metadata,
    build_debug_feedback_envelope,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.phases import execution_debug_repair
from app.services.orchestration.phases.execution_debug_repair import (
    _bounded_debug_repair_stale_replace_issues,
    bounded_debug_repair_correction_guard_paths,
    bounded_debug_repair_correction_runtime_refusal,
    bounded_debug_repair_target_guard_failures,
    bounded_debug_repair_target_identities,
)
from app.services.orchestration.phases.execution_loop import execute_step_loop
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.orchestration.validation.path_authority import (
    PathObservationError,
)
from app.tests.test_phase7f_debug_feedback import (
    _extract_structured_text,
    _FakeDebugRepairRuntime,
    _FakeRuntime,
    _install_role_owned_debug_runtime,
    _make_run_context,
    _normalize_step,
)

FIXTURE = Path(__file__).parent / "fixtures" / "p42_v17n_te395_service_post_step2.json"
FIXTURE_CONTENT_SHA256 = (
    "e367219149cf0875fdd0509b3c6ba708f5e1241af57b5832e80cc3884b2e3ff7"
)
TE395_MODELS = (
    '"""Domain models for the baseline inventory library."""\n\n'
    "from dataclasses import dataclass\n\n\n@dataclass\nclass InventoryItem:\n"
    "    sku: str\n    total_stock: int = 0\n    reserved_stock: int = 0\n"
)
AVAILABLE_STOCK = (
    "    def available_stock(self, sku: str) -> int:\n"
    "        return self._items.get(sku, InventoryItem(sku=sku)).total_stock"
)

ORIGINAL_SOURCE = (
    "def value(total: int) -> int:\n" '    raise NotImplementedError("value missing")\n'
)
STEP_FAILURE = {
    "status": "failed",
    "output": "FAILED tests/test_demo.py::test_value",
    "error": "AssertionError: value missing",
    "returncode": 1,
}
STALE_PRIMARY = {
    "output": json.dumps(
        [
            {
                "repair_type": "ops_fix",
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "src/demo.py",
                        "old": 'def value(store: Store) -> int:\n    raise NotImplementedError("value missing")\n',
                        "new": "def value() -> int:\n    return 1\n",
                    }
                ],
                "verification_command": "python3 -m py_compile src/demo.py",
            }
        ]
    )
}
VALID_CORRECTION = {
    "output": json.dumps(
        [
            {
                "repair_type": "ops_fix",
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "src/demo.py",
                        "old": ORIGINAL_SOURCE,
                        "new": "def value(total: int) -> int:\n    return total\n",
                    }
                ],
                "verification_command": "python3 -m py_compile src/demo.py",
            }
        ]
    )
}
CORRECTED_MARKER = "    return total\n"


def _fixture_content() -> str:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["content"]


def _stale_scenario(db_session, tmp_path, monkeypatch, *, debug_runtime=None):
    runtime = _FakeRuntime([dict(STEP_FAILURE)])
    roles = None
    if debug_runtime is not None:
        roles = _install_role_owned_debug_runtime(monkeypatch, runtime, debug_runtime)
    ctx, _execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        expected_files=["src/demo.py"],
        step_overrides={
            "description": "Run project tests before implementation",
            "commands": ["custom-test-command"],
            "verification": "",
            "ops": [],
        },
    )
    source_path = ctx.orchestration_state.project_dir / "src" / "demo.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(ORIGINAL_SOURCE, encoding="utf-8")
    restores: list[str] = []

    def _restore(reason):
        # Stands in for the pre-run snapshot restore: record it and restore.
        restores.append(reason)
        if source_path.is_symlink() or source_path.is_dir():
            if source_path.is_dir() and not source_path.is_symlink():
                source_path.rmdir()
            else:
                source_path.unlink()
        source_path.write_text(ORIGINAL_SOURCE, encoding="utf-8")

    ctx = dataclasses.replace(ctx, restore_workspace_snapshot_if_needed=_restore)
    return SimpleNamespace(
        ctx=ctx,
        runtime=runtime,
        roles=roles,
        source_path=source_path,
        restores=restores,
    )


def _run(ctx):
    return execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )


def _last_rejection(ctx):
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    rejected = [e for e in events if e.get("event_type") == EventType.REPAIR_REJECTED]
    assert rejected, "expected a repair_rejected event"
    return rejected[-1]["details"]


# --------------------------------------------------------------------------
# R1: routing
# --------------------------------------------------------------------------


def test_primary_and_correction_both_use_role_owned_debug_repair_runtime(
    db_session, tmp_path, monkeypatch
):
    debug_runtime = _FakeDebugRepairRuntime([STALE_PRIMARY, VALID_CORRECTION])
    scenario = _stale_scenario(
        db_session, tmp_path, monkeypatch, debug_runtime=debug_runtime
    )

    result = _run(scenario.ctx)

    assert result["status"] == "completed", result
    assert scenario.roles and set(scenario.roles) == {BackendRole.DEBUG_REPAIR}
    # The Execution runtime saw only the step; never a repair or correction.
    assert len(scenario.runtime.prompts) == 1
    assert len(debug_runtime.prompts) == 2
    assert "Failed replace_in_file targets" in debug_runtime.prompts[1]
    # Role-owned invocation carries provider options, which a native-tool
    # OpenClaw adapter refuses to represent.
    assert all(option is not None for option in debug_runtime.options)
    assert scenario.source_path.read_text(encoding="utf-8").endswith(CORRECTED_MARKER)


def test_correction_fails_closed_without_role_owned_runtime(
    db_session, tmp_path, monkeypatch
):
    # No runtime_configuration: the legacy primary path still runs on the
    # Execution runtime, but the correction must not fall back to it.
    scenario = _stale_scenario(db_session, tmp_path, monkeypatch)
    scenario.runtime.responses.extend([dict(STALE_PRIMARY), dict(VALID_CORRECTION)])

    result = _run(scenario.ctx)

    assert result["status"] == "failed"
    assert len(scenario.runtime.prompts) == 2  # step + primary; no correction
    details = _last_rejection(scenario.ctx)
    assert (
        details["bounded_execution_debug_repair_rejection_reason"]
        == "correction_runtime_unavailable"
    )
    assert scenario.source_path.read_text(encoding="utf-8") == ORIGINAL_SOURCE


def test_correction_refuses_tool_capable_role_runtime(
    db_session, tmp_path, monkeypatch
):
    class _ToolCapableDebugRuntime(_FakeDebugRepairRuntime):
        backend_descriptor = get_backend_descriptor("local_openclaw")

    debug_runtime = _ToolCapableDebugRuntime([STALE_PRIMARY, VALID_CORRECTION])
    scenario = _stale_scenario(
        db_session, tmp_path, monkeypatch, debug_runtime=debug_runtime
    )

    result = _run(scenario.ctx)

    assert result["status"] == "failed"
    assert len(debug_runtime.prompts) == 1  # primary only; correction refused
    assert len(scenario.runtime.prompts) == 1
    assert (
        _last_rejection(scenario.ctx)["bounded_execution_debug_repair_rejection_reason"]
        == "correction_runtime_tool_capable"
    )


def test_correction_runtime_refusal_contract():
    no_tools = SimpleNamespace(
        backend_descriptor=get_backend_descriptor("openai_chat_completions")
    )
    assert bounded_debug_repair_correction_runtime_refusal(no_tools) is None
    assert (
        bounded_debug_repair_correction_runtime_refusal(None)
        == "correction_runtime_unavailable"
    )
    assert (
        bounded_debug_repair_correction_runtime_refusal(
            SimpleNamespace(backend_descriptor=None)
        )
        == "correction_runtime_capability_unknown"
    )
    for tool_backend in ("local_openclaw", "remote_openclaw_gateway"):
        assert (
            bounded_debug_repair_correction_runtime_refusal(
                SimpleNamespace(backend_descriptor=get_backend_descriptor(tool_backend))
            )
            == "correction_runtime_tool_capable"
        )


def test_openclaw_adapter_cannot_serve_role_owned_invocation_options():
    from app.services.agents.openclaw_service import OpenClawSessionService
    from app.services.agents.runtime_invocation import RuntimeInvocationOptions

    with pytest.raises(UnsupportedCapabilityError):
        import asyncio

        asyncio.run(
            OpenClawSessionService.invoke_prompt(
                SimpleNamespace(),
                "prompt",
                invocation_options=RuntimeInvocationOptions(timeout_seconds=1.0),
            )
        )


def test_invalid_correction_stays_rejected_and_bounded_to_one_attempt(
    db_session, tmp_path, monkeypatch
):
    debug_runtime = _FakeDebugRepairRuntime([STALE_PRIMARY, STALE_PRIMARY])
    scenario = _stale_scenario(
        db_session, tmp_path, monkeypatch, debug_runtime=debug_runtime
    )

    result = _run(scenario.ctx)

    assert result["status"] == "failed"
    assert len(debug_runtime.prompts) == 2  # one primary + exactly one correction
    assert (
        _last_rejection(scenario.ctx)["bounded_execution_debug_repair_rejection_reason"]
        == "stale_replace_after_correction"
    )
    assert scenario.source_path.read_text(encoding="utf-8") == ORIGINAL_SOURCE
    assert scenario.ctx.orchestration_state.debug_repair_task_execution_ids == [
        scenario.ctx.task_execution_id
    ]


# --------------------------------------------------------------------------
# R1 guard: target mutation during correction generation
# --------------------------------------------------------------------------


def _mutating_debug_runtime(mutate):
    # The correction is the 2nd invocation; mutate the target while it runs.
    def _on_invoke(call_number):
        if call_number == 2:
            mutate()

    return _FakeDebugRepairRuntime(
        [STALE_PRIMARY, VALID_CORRECTION], on_invoke=_on_invoke
    )


@pytest.mark.parametrize("mutation", ["modified", "deleted", "directory", "symlink"])
def test_target_change_during_correction_fails_closed(
    db_session, tmp_path, monkeypatch, mutation
):
    holder = {}
    outside = tmp_path / "outside_workspace.py"
    outside.write_text(ORIGINAL_SOURCE, encoding="utf-8")

    def _mutate():
        target = holder["path"]
        if mutation == "modified":
            # The "fix" the agent would have made itself; the returned op's old
            # text no longer matches, but the guard must reject first anyway.
            target.write_text(ORIGINAL_SOURCE + "\n", encoding="utf-8")
        elif mutation == "deleted":
            target.unlink()
        elif mutation == "directory":
            target.unlink()
            target.mkdir()
        else:  # same bytes behind a symlink that leaves the workspace
            target.unlink()
            target.symlink_to(outside)

    debug_runtime = _mutating_debug_runtime(_mutate)
    scenario = _stale_scenario(
        db_session, tmp_path, monkeypatch, debug_runtime=debug_runtime
    )
    holder["path"] = scenario.source_path

    result = _run(scenario.ctx)

    assert result["status"] == "failed"
    details = _last_rejection(scenario.ctx)
    assert (
        details["bounded_execution_debug_repair_rejection_reason"]
        == "correction_target_mutated"
    )
    assert details["correction_target_guard_failures"] == ["src/demo.py"]
    # No corrected operation was applied, the outside file was never written
    # through the symlink, and terminal restoration still ran.
    assert outside.read_text(encoding="utf-8") == ORIGINAL_SOURCE
    assert scenario.restores == ["debug parse error"]
    assert scenario.source_path.read_text(encoding="utf-8") == ORIGINAL_SOURCE


def test_unobservable_target_refuses_correction_before_invocation(
    db_session, tmp_path, monkeypatch
):
    real_observe = execution_debug_repair.observe
    calls = {"n": 0}

    def _observe(root, path, **kwargs):
        calls["n"] += 1
        raise PathObservationError("path_metadata_unreadable", "unreadable")

    debug_runtime = _FakeDebugRepairRuntime([STALE_PRIMARY, VALID_CORRECTION])
    scenario = _stale_scenario(
        db_session, tmp_path, monkeypatch, debug_runtime=debug_runtime
    )
    monkeypatch.setattr(execution_debug_repair, "observe", _observe)

    result = _run(scenario.ctx)

    monkeypatch.setattr(execution_debug_repair, "observe", real_observe)
    assert result["status"] == "failed"
    assert calls["n"] >= 1
    assert len(debug_runtime.prompts) == 1  # correction never invoked
    assert (
        _last_rejection(scenario.ctx)["bounded_execution_debug_repair_rejection_reason"]
        == "correction_target_unobservable"
    )


def test_guard_identity_unit_cases(tmp_path):
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "demo.py"
    target.write_text("A = 1\n", encoding="utf-8")
    debug_data = {
        "ops": [
            {"op": "replace_in_file", "path": "src/demo.py", "old": "x", "new": "y"},
            {"op": "write_file", "path": "src/new.py", "content": "B = 2\n"},
            {"op": "write_file", "path": "/abs/escape.py", "content": ""},
            {"op": "write_file", "path": "src/../escape.py", "content": ""},
        ]
    }
    paths = bounded_debug_repair_correction_guard_paths(
        debug_data, [{"path": "src/demo.py"}]
    )
    # Guarded paths are exactly the workspace paths the stale check resolves
    # (``_safe_relative_op_path``); traversal never becomes a guarded path.
    assert paths == ["src/demo.py", "src/new.py", "abs/escape.py"]

    before = bounded_debug_repair_target_identities(tmp_path, paths)
    # Unchanged (including a target missing both times) permits validation.
    assert bounded_debug_repair_target_guard_failures(before) == []
    assert (
        bounded_debug_repair_target_guard_failures(
            before, bounded_debug_repair_target_identities(tmp_path, paths)
        )
        == []
    )
    # Unexpected creation of a previously missing target is a change.
    (tmp_path / "src" / "new.py").write_text("B = 2\n", encoding="utf-8")
    assert bounded_debug_repair_target_guard_failures(
        before, bounded_debug_repair_target_identities(tmp_path, paths)
    ) == ["src/new.py"]


# --------------------------------------------------------------------------
# R2: explicit truncation (TE 395 fixture)
# --------------------------------------------------------------------------


def _te395_workspace(tmp_path):
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory" / "service.py").write_text(
        _fixture_content(), encoding="utf-8"
    )
    (tmp_path / "inventory" / "models.py").write_text(TE395_MODELS, encoding="utf-8")
    return build_debug_feedback_envelope(
        task_execution_id=395,
        task_id=305,
        step_index=4,
        failure_phase="execution",
        failed_command="python3 -m pytest -q tests/",
        stdout="FAILED tests/test_inventory.py::test_reserve_and_release_stock",
        workspace_path=str(tmp_path),
    )


def test_fixture_is_the_pinned_te395_post_step2_service():
    content = _fixture_content()
    assert hashlib.sha256(content.encode("utf-8")).hexdigest() == (
        FIXTURE_CONTENT_SHA256
    )
    assert len(content) == 1773


def test_te395_service_is_explicitly_truncated_and_models_complete(tmp_path):
    envelope = _te395_workspace(tmp_path)
    rendered, metadata = build_bounded_debug_repair_changed_file_context(
        envelope, prior_source_paths=["inventory/models.py", "inventory/service.py"]
    )
    content = _fixture_content()

    assert metadata["bounded_execution_debug_repair_changed_file_context_paths"] == [
        "inventory/models.py",
        "inventory/service.py",
    ]
    assert metadata[
        "bounded_execution_debug_repair_changed_file_context_complete_paths"
    ] == ["inventory/models.py"]
    assert metadata[
        "bounded_execution_debug_repair_changed_file_context_truncated_paths"
    ] == ["inventory/service.py"]
    assert metadata["bounded_execution_debug_repair_changed_file_context_chars"] <= 3000
    assert len(rendered) <= 3000

    service = rendered.split("--- inventory/service.py\n", 1)[1]
    label, rest = service.split("\n", 1)
    assert label == (
        "[TRUNCATED: 1197 of 1773 characters shown; not complete source truth]"
    )
    visible, boundary = rest.rsplit("\n", 1)
    assert boundary == (
        "[TRUNCATION BOUNDARY: 576 characters not shown; "
        "do not construct replace_in_file old text across this boundary]"
    )
    # The excerpt is an exact prefix of the current file within the
    # unchanged per-file budget, with no bare "..." continuation.
    assert content.startswith(visible)
    assert len(visible) == 1197 <= 1200
    assert not visible.endswith("...")
    assert AVAILABLE_STOCK in visible
    assert "def release_stock" not in visible

    models = rendered.split("--- inventory/models.py\n", 1)[1].split("\n--- ", 1)[0]
    assert models == f"[COMPLETE: all {len(TE395_MODELS)} characters shown]\n" + (
        TE395_MODELS.strip()
    )


def test_prompt_never_calls_truncated_excerpts_complete_source_truth(tmp_path):
    envelope = _te395_workspace(tmp_path)
    result = build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        prior_source_paths=["inventory/models.py", "inventory/service.py"],
    )

    assert "Treat these files as the current source truth" not in result.prompt
    assert "Only files labelled [COMPLETE] are full current source truth" in (
        result.prompt
    )
    assert result.prompt.count("[TRUNCATED: ") == 1
    assert result.prompt.count("[COMPLETE: ") == 1
    # A replace anchored on the visible bug site is still exact; one copied
    # across the boundary is stale.
    assert not _bounded_debug_repair_stale_replace_issues(
        [
            {
                "op": "replace_in_file",
                "path": "inventory/service.py",
                "old": AVAILABLE_STOCK,
                "new": "x",
            }
        ],
        tmp_path,
    )
