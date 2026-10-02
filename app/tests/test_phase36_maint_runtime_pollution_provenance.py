"""PHASE36-MAINT runtime-pollution provenance and shared-Git boundary tests."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

from app.models import Project, Session as SessionModel, Task, TaskExecution
from app.services.agents.interfaces import RuntimeBackendResult
from app.services.agents.runtime_adapters.base import runtime_result_from_mapping
from app.services.orchestration.execution.step_dispatch import (
    dispatch_execution_runtime_step,
)
from app.services.orchestration.validation.runtime_pollution_guard import (
    RUNTIME_POLLUTION_SCHEMA_VERSION,
    bound_runtime_pollution_evidence,
    build_git_topology,
    build_runtime_pollution_provenance,
    classify_git_admin_changes,
    detect_runtime_pollution,
    snapshot_workspace_entry_evidence,
)
from app.services.orchestration.phases.execution_loop import execute_step_loop
from app.services.workspace.task_sandbox_allocator import (
    allocate_task_sandbox,
    dispose_task_sandbox,
)


def _pollution(tmp_path: Path, *, boundary: str = "canonical_project_root"):
    canonical = tmp_path / "w1"
    runtime = tmp_path / "w2"
    canonical.mkdir()
    runtime.mkdir()
    before = snapshot_workspace_entry_evidence(
        canonical if boundary == "canonical_project_root" else runtime
    )
    target_root = canonical if boundary == "canonical_project_root" else runtime
    (target_root / "changed.py").write_text("after\n", encoding="utf-8")
    after = snapshot_workspace_entry_evidence(target_root)
    result = detect_runtime_pollution(
        before=before,
        after=after,
        canonical_root=canonical,
        runtime_workspace=runtime,
        phase="provider_initialization",
    )
    return canonical, runtime, result


def _durable(canonical, runtime, pollution):
    return build_runtime_pollution_provenance(
        pollution,
        project_id=11,
        session_id=22,
        task_id=33,
        task_execution_id=44,
        execution_step=2,
        phase="provider_initialization",
        canonical_root=canonical,
        runtime_root=runtime,
        phase_timestamps={"T0": "t0", "T1": "t1", "T2": "t2", "T7": "t7"},
        process_provenance=[
            {
                "pid": 123,
                "parent_pid": 99,
                "argv": ["openclaw", "agent", "--message", "<redacted>"],
                "cwd": str(runtime),
                "configured_workspace": str(runtime),
                "canonical_root": str(canonical),
                "runtime_root": str(runtime),
                "start_timestamp": "s",
                "end_timestamp": "e",
                "return_code": 0,
                "execution_step": 2,
                "provider_phase": "provider_initialization",
            }
        ],
    )


def test_p1_rich_w1_source_pollution_survives_durable_projection(tmp_path):
    canonical, runtime, result = _pollution(tmp_path)
    durable = _durable(canonical, runtime, result)
    assert durable["entries"][0]["absolute_path"].endswith("/w1/changed.py")
    assert durable["entries"][0]["safety_category"] == "SG-E"
    assert durable["execution_must_stop"] is True


def test_p2_rich_w2_pollution_survives_durable_projection(tmp_path):
    canonical, runtime, result = _pollution(tmp_path, boundary="runtime_workspace")
    durable = _durable(canonical, runtime, result)
    assert durable["entries"][0]["boundary"] == "runtime_workspace"
    assert durable["entries"][0]["execution_must_stop"] is False


def test_p3_absolute_and_relative_paths_are_retained(tmp_path):
    canonical, runtime, result = _pollution(tmp_path)
    entry = _durable(canonical, runtime, result)["entries"][0]
    assert entry["absolute_path"] == entry["path"]
    assert entry["relative_path"] == "changed.py"


def test_p4_before_after_hashes_and_state_are_retained(tmp_path):
    canonical, runtime, result = _pollution(tmp_path)
    entry = _durable(canonical, runtime, result)["entries"][0]
    assert entry["before_exists"] is False
    assert entry["after_exists"] is True
    assert entry["before_sha256"] is None
    assert entry["after_sha256"]
    assert entry["before_state"] == "absent"
    assert entry["after_state"] == "present"


def test_p5_boundary_and_schema_are_retained(tmp_path):
    canonical, runtime, result = _pollution(tmp_path)
    durable = _durable(canonical, runtime, result)
    assert durable["schema_version"] == RUNTIME_POLLUTION_SCHEMA_VERSION
    assert durable["boundary_counts"]["canonical_project_root"] == 1
    assert durable["entries"][0]["schema_version"] == RUNTIME_POLLUTION_SCHEMA_VERSION


def test_p6_phase_and_timestamps_are_retained(tmp_path):
    canonical, runtime, result = _pollution(tmp_path)
    durable = _durable(canonical, runtime, result)
    assert durable["phase"] == "provider_initialization"
    assert durable["phase_timestamps"]["T0"] == "t0"
    assert durable["phase_timestamps"]["T7"] == "t7"
    assert durable["timestamp"]


def test_p7_provider_process_correlation_is_retained_in_normalized_result(
    tmp_path,
):
    canonical, runtime, result = _pollution(tmp_path)
    durable = _durable(canonical, runtime, result)
    raw = {
        "status": "failed",
        "error": "runtime safety stop",
        "failure_category": "runtime_safety_stop",
        "runtime_pollution": durable,
    }
    normalized = runtime_result_from_mapping(
        raw, backend_id="local_openclaw", role="execution"
    )
    assert normalized.runtime_pollution["process_provenance"][0]["pid"] == 123
    assert normalized.runtime_pollution["process_provenance"][0]["cwd"] == str(runtime)


def test_p7_dispatch_attaches_durable_process_and_phase_correlation(tmp_path):
    canonical, runtime, pollution = _pollution(tmp_path)

    class FakeRuntime:
        execution_cwd_override = str(runtime)
        session_id = 22
        task_id = 33
        task_execution_id = 44

        async def execute_task(self, prompt, timeout_seconds):
            return {
                "status": "failed",
                "runtime_result": {
                    "project_id": 11,
                    "session_id": 22,
                    "task_id": 33,
                    "task_execution_id": 44,
                    "project_workspace": str(canonical),
                    "runtime_workspace": str(runtime),
                },
                "runtime_diagnostics": {
                    "process_pid": 123,
                    "return_code": 1,
                    "invocation": {
                        "cwd": str(runtime),
                        "args_redacted": [
                            "openclaw",
                            "agent",
                            "--message",
                            "<redacted>",
                        ],
                        "executable_path": "/usr/bin/openclaw",
                        "executable_args": [],
                        "subcommand": "agent",
                        "invocation_kind": "execution",
                    },
                },
                "runtime_pollution": pollution,
            }

        def normalize_execution_result(self, result, *, role, duration_seconds):
            return runtime_result_from_mapping(
                result,
                backend_id="fake",
                role=role,
                duration_seconds=duration_seconds,
            )

    runtime_service = FakeRuntime()
    runtime_service._runtime_provenance_context = {
        "execution_step": 2,
        "phase": "provider_initialization",
        "phase_timestamps": {"T0": "t0", "T1": "t1"},
    }
    outcome = dispatch_execution_runtime_step(
        runtime_service=runtime_service,
        prompt="provider-free",
        timeout_seconds=1,
        db=None,
        task_execution_id=None,
        execution_step=2,
    )
    durable = outcome.runtime_backend_result.runtime_pollution
    assert durable["phase_timestamps"]["T0"] == "t0"
    assert durable["phase_timestamps"]["T1"] == "t1"
    assert durable["phase_timestamps"]["T3"]
    assert durable["phase_timestamps"]["T5"]
    assert durable["process_provenance"][0]["pid"] == 123
    assert durable["process_provenance"][0]["cwd"] == str(runtime)
    assert durable["process_provenance"][0]["argv"][0] == "openclaw"


def test_p8_linked_worktree_git_topology_is_explicit(tmp_path):
    w1 = tmp_path / "product"
    w1.mkdir()
    (w1 / "app").mkdir()
    (w1 / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=w1, check=True)
    subprocess.run(
        ["git", "config", "user.email", "x@example.invalid"], cwd=w1, check=True
    )
    subprocess.run(["git", "config", "user.name", "x"], cwd=w1, check=True)
    subprocess.run(["git", "add", "."], cwd=w1, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=w1, check=True)
    sandbox = allocate_task_sandbox(
        w1, project_id=1, task_execution_id=2, runtime_root=tmp_path / "runtime"
    )
    try:
        topology = build_git_topology(sandbox.path)
        assert topology["git_dir"] != topology["git_common_dir"]
        assert topology["worktree_git_dir"] == topology["git_dir"]
        assert topology["shared_common_dir"] == topology["git_common_dir"]
        assert topology["git_path_type"] == "file"
        assert topology["git_indirection"]
    finally:
        dispose_task_sandbox(sandbox, project_root=w1)


def test_p9_legitimate_w2_git_baseline_is_not_new_pollution(tmp_path):
    canonical = tmp_path / "w1"
    canonical.mkdir()
    (canonical / "app").mkdir()
    (canonical / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=canonical, check=True)
    subprocess.run(
        ["git", "config", "user.email", "x@example.invalid"],
        cwd=canonical,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "x"], cwd=canonical, check=True)
    subprocess.run(["git", "add", "."], cwd=canonical, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=canonical, check=True)
    sandbox = allocate_task_sandbox(
        canonical,
        project_id=1,
        task_execution_id=2,
        runtime_root=tmp_path / "runtime",
    )
    try:
        before = snapshot_workspace_entry_evidence(sandbox.path)
        subprocess.run(["git", "status", "--short"], cwd=sandbox.path, check=True)
        after = snapshot_workspace_entry_evidence(sandbox.path)
    finally:
        dispose_task_sandbox(sandbox, project_root=canonical)
    result = detect_runtime_pollution(
        before=before,
        after=after,
        canonical_root=canonical,
        runtime_workspace=sandbox.path,
    )
    assert result["pollution_detected"] is False


def test_p10_legitimate_w2_app_baseline_is_not_new_pollution(tmp_path):
    canonical = tmp_path / "w1"
    runtime = tmp_path / "w2"
    canonical.mkdir()
    runtime.mkdir()
    (runtime / "app").mkdir()
    (runtime / "app" / "main.py").write_text("x\n", encoding="utf-8")
    before = snapshot_workspace_entry_evidence(runtime)
    after = snapshot_workspace_entry_evidence(runtime)
    result = detect_runtime_pollution(
        before=before,
        after=after,
        canonical_root=canonical,
        runtime_workspace=runtime,
    )
    assert result["pollution_detected"] is False


def test_p11_w1_source_mutation_remains_fail_closed(tmp_path):
    _, _, result = _pollution(tmp_path)
    assert result["execution_must_stop"] is True
    assert result["category"] == "canonical_workspace_pollution_detected"


def test_p12_w2_mutation_is_separate_from_canonical_stop(tmp_path):
    _, _, result = _pollution(tmp_path, boundary="runtime_workspace")
    assert result["execution_must_stop"] is False
    assert result["entries"][0]["boundary"] == "runtime_workspace"


def test_p13_shared_git_scope_classification_is_bounded():
    assert (
        classify_git_admin_changes(
            [{"scope": "worktree_specific", "content_changed": True}],
            phase="provider_initialization",
        )
        == "SG-A"
    )
    assert (
        classify_git_admin_changes(
            [{"scope": "shared_common", "content_changed": False}],
            phase="provider_initialization",
        )
        == "SG-C"
    )
    assert (
        classify_git_admin_changes(
            [
                {
                    "scope": "shared_common",
                    "relative_path": "config",
                    "content_changed": True,
                }
            ],
            phase="provider_initialization",
        )
        == "SG-D"
    )
    assert (
        classify_git_admin_changes(
            [
                {
                    "scope": "shared_common",
                    "relative_path": "worktrees/2/HEAD",
                    "content_changed": True,
                }
            ],
            phase="allocator",
        )
        == "SG-B"
    )
    assert (
        classify_git_admin_changes(
            [
                {
                    "scope": "shared_common",
                    "relative_path": "config",
                    "content_changed": True,
                }
            ],
            phase="allocator",
        )
        == "SG-D"
    )


def test_p14_gr11_terminal_remains_distinct():
    assert (
        'terminal_reason = "execution_post_structured_op_mutation"'
        in inspect.getsource(execute_step_loop)
    )


def test_p15_safety_stop_is_not_rewritten_by_durable_projection(tmp_path):
    canonical, runtime, result = _pollution(tmp_path)
    durable = _durable(canonical, runtime, result)
    assert durable["execution_must_stop"] is True
    assert durable["entries"][0]["execution_must_stop"] is True


def test_p16_cap_retains_safety_entries_and_reports_truncation(tmp_path):
    canonical = tmp_path / "w1"
    runtime = tmp_path / "w2"
    canonical.mkdir()
    runtime.mkdir()
    entries = [
        {
            "path": str(runtime / f"safe-{index}.txt"),
            "relative_path": f"safe-{index}.txt",
            "creator_boundary": "runtime_workspace",
            "execution_must_stop": False,
        }
        for index in range(100)
    ]
    entries.append(
        {
            "path": str(canonical / "must-stop.py"),
            "relative_path": "must-stop.py",
            "creator_boundary": "canonical_project_root",
            "execution_must_stop": True,
        }
    )
    bounded = bound_runtime_pollution_evidence(
        {
            "entries": entries,
            "execution_must_stop": True,
            "category": "canonical_workspace_pollution_detected",
        },
        project_id=1,
        session_id=2,
        task_id=3,
        task_execution_id=4,
        canonical_root=canonical,
        runtime_root=runtime,
    )
    assert bounded["evidence_truncated"] is True
    assert any(entry["relative_path"] == "must-stop.py" for entry in bounded["entries"])


def test_p17_sandbox_disposal_contract_remains_available():
    assert callable(dispose_task_sandbox)


def test_p18_runtime_result_contract_remains_additive():
    result = RuntimeBackendResult(
        backend_id="stub",
        role="execution",
        success=True,
        exit_reason="completed",
        output="ok",
        duration_seconds=0.1,
    )
    assert result.runtime_pollution is None


def test_durable_record_contains_all_identity_fields(tmp_path):
    canonical, runtime, result = _pollution(tmp_path)
    durable = _durable(canonical, runtime, result)
    assert {
        "project_id",
        "session_id",
        "task_id",
        "task_execution_id",
        "execution_step",
        "canonical_root",
        "runtime_root",
        "process_provenance",
        "git_topology",
    } <= durable.keys()
