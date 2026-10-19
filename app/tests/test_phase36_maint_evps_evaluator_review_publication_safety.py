"""PHASE36-MAINT-EVPS — evaluator evidence and publication-safety seams."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.phases.completion_flow import _run_evaluator


HISTORICAL_REENTRY12_OUTPUT = (
    "SCORES: goal=3/3 regressions=2/2 quality=2/2 files=3/3 TOTAL: 10/10 "
    "VERDICT: PASS NOTES: The permissions router is correctly imported and included "
    "once at the proper location in router.py, with the duplicate premature call "
    "removed, syntax validated, and no regressions introduced."
)

DOGFOOD03_RETAINED_PARSER_INPUT = (
    "Let me verify the final state of the implementation and tests independently.\n"
    "SCORES: goal=3/3 regressions=2/2 quality=2/2 files=3/3 TOTAL: 10/10 "
    "VERDICT: PASS NOTES: All 5 acceptance tests pass independently; implementation "
    "uses only stdlib (re, unicodedata), preserves the function interface, and leaves "
    "tests untouched."
)


class _CapturingRuntime:
    def __init__(
        self,
        output: str = HISTORICAL_REENTRY12_OUTPUT,
        *,
        status: str = "completed",
        result: dict | None = None,
        raises: Exception | None = None,
    ):
        self.output = output
        self.status = status
        self.result = result
        self.raises = raises
        self.prompt = ""

    async def execute_task(self, prompt, timeout_seconds=None):
        self.prompt = prompt
        if self.raises is not None:
            raise self.raises
        if self.result is not None:
            return self.result
        return {"status": self.status, "output": self.output}


def _candidate_evidence(tmp_path: Path) -> tuple[dict, dict]:
    baseline = tmp_path / "baseline"
    artifact = tmp_path / "artifact"
    relative = "app/api/v1/router.py"
    before = baseline / relative
    after = artifact / relative
    before.parent.mkdir(parents=True)
    after.parent.mkdir(parents=True)
    before.write_text("from fastapi import APIRouter\napi_router = APIRouter()\n")
    after.write_text("from fastapi import APIRouter\n\napi_router = APIRouter()\n")
    return (
        {
            "change_set_id": 309,
            "task_execution_id": 383,
            "artifact_path": str(artifact),
            "artifact_manifest_path": str(artifact / "manifest.json"),
            "snapshot_path": str(baseline),
            "modified_files": [relative],
            "added_files": [],
            "deleted_files": [],
        },
        {
            "status": "accepted",
            "candidate_identity": "sha256:candidate",
            "details": {
                "validation_evidence": {
                    "command_quality": "smoke_only",
                    "applicable_command_quality": "smoke_only",
                    "verification_insufficient": True,
                    "requires_independent_evidence": True,
                    "has_independent_regression_test": False,
                    "command_quality_by_step": [
                        {
                            "step_number": 1,
                            "command": "grep -q permissions router.py",
                            "command_quality": "smoke_only",
                            "applies_to_candidate": True,
                        }
                    ],
                }
            },
        },
    )


def _run(runtime, *, candidate_evidence=None, verification_evidence=None):
    state = SimpleNamespace(
        reasoning_artifact={
            "intent": "Make the permissions API available at its public routes.",
            "planned_actions": ["Add permissions router inclusion"],
            "verification_plan": ["Run route verification"],
        },
        plan=[
            {
                "step_number": 1,
                "description": "Add permissions router inclusion",
                "verification": "grep -q permissions router.py",
                "expected_files": ["app/api/v1/router.py"],
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "app/api/v1/router.py",
                        "old": "old",
                        "new": "new",
                    }
                ],
            }
        ],
        execution_results=[
            {
                "step_number": 1,
                "step_title": "Add permissions router inclusion",
                "status": "success",
                "files_changed": ["app/api/v1/router.py"],
                "verification_output": "grep passed",
            }
        ],
        changed_files=["app/api/v1/router.py"],
        project_dir="/tmp/project",
        session_id=242,
        task_id=292,
    )
    return _run_evaluator(
        runtime_service=runtime,
        orchestration_state=state,
        prompt="Make the permissions API available at its public routes.",
        summary="The execution model says the duplicate premature call was removed.",
        candidate_evidence=(
            {"change_set": candidate_evidence} if candidate_evidence else None
        ),
        verification_evidence=verification_evidence,
        emit_live=lambda *_args, **_kwargs: None,
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    )


def test_reentry12_raw_response_remains_unknown():
    result = _run(_CapturingRuntime())

    assert result["verdict"] == "UNKNOWN"


def test_evaluator_receives_authoritative_noop_candidate_and_verification_evidence(
    tmp_path,
):
    candidate, verification = _candidate_evidence(tmp_path)
    runtime = _CapturingRuntime()

    _run(
        runtime,
        candidate_evidence=candidate,
        verification_evidence=verification,
    )

    assert "TASK_OBJECTIVE" in runtime.prompt
    assert "PLAN_INTENT" in runtime.prompt
    assert "ACTUAL_CANDIDATE_CHANGE" in runtime.prompt
    assert "VERIFICATION_EVIDENCE" in runtime.prompt
    assert "VERIFICATION_QUALITY" in runtime.prompt
    assert "smoke_only" in runtime.prompt
    assert "candidate_identity" in runtime.prompt
    assert "@@" in runtime.prompt
    assert "+\\n api_router" in runtime.prompt
    assert "Plan intent is not proof of implementation" in runtime.prompt


def test_evaluator_prompt_requires_canonical_standalone_verdict_format():
    runtime = _CapturingRuntime("VERDICT: PASS\n")

    _run(runtime)

    assert "Return only the following format." in runtime.prompt
    assert "Do not include a preamble" in runtime.prompt
    assert (
        "Do not include" in runtime.prompt and "Markdown code fence" in runtime.prompt
    )
    assert "Use exactly one score field per line" in runtime.prompt
    assert (
        "SCORES:\ngoal=X/3\nregressions=X/2\nquality=X/2\nfiles=X/3\n" in runtime.prompt
    )
    assert "TOTAL: X/10\nVERDICT: PASS or NEEDS_REVIEW\n" in runtime.prompt
    assert "exactly one standalone verdict line" in runtime.prompt
    assert "The verdict line must begin at the start of its own line" in runtime.prompt
    assert "Do not put a verdict inline with another field" in runtime.prompt
    assert "NOTES: <brief explanation>" in runtime.prompt
    assert "NOTES must be the final content line" in runtime.prompt
    assert "output only permitted trailing whitespace" in runtime.prompt


def test_dogfood03_exact_retained_parser_input_remains_unknown():
    assert (
        _run(_CapturingRuntime(DOGFOOD03_RETAINED_PARSER_INPUT))["verdict"] == "UNKNOWN"
    )


def test_malformed_embedded_pass_remains_unknown(tmp_path):
    runtime = _CapturingRuntime(
        "SCORES: 10/10 VERDICT: PASS NOTES: embedded in one line"
    )

    assert _run(runtime)["verdict"] == "UNKNOWN"


def test_completed_runtime_with_explicit_pass_is_pass():
    assert _run(_CapturingRuntime("VERDICT: PASS\n"))["verdict"] == "PASS"


def test_completed_runtime_with_needs_review_is_needs_review():
    assert (
        _run(_CapturingRuntime("VERDICT: NEEDS_REVIEW\n"))["verdict"] == "NEEDS_REVIEW"
    )


def test_completed_runtime_with_empty_output_is_unknown():
    assert _run(_CapturingRuntime(""))["verdict"] == "UNKNOWN"


def test_failed_runtime_pass_output_is_error():
    result = _run(_CapturingRuntime("VERDICT: PASS\n", status="failed"))

    assert result["verdict"] == "ERROR"


def test_failed_runtime_needs_review_output_is_error():
    result = _run(_CapturingRuntime("VERDICT: NEEDS_REVIEW\n", status="failed"))

    assert result["verdict"] == "ERROR"


def test_failed_runtime_empty_output_is_error():
    assert _run(_CapturingRuntime("", status="failed"))["verdict"] == "ERROR"


def test_missing_runtime_status_pass_output_is_error():
    result = _run(_CapturingRuntime(result={"output": "VERDICT: PASS\n"}))

    assert result["verdict"] == "ERROR"


def test_unrecognized_runtime_status_pass_output_is_error():
    result = _run(_CapturingRuntime("VERDICT: PASS\n", status="succeeded"))

    assert result["verdict"] == "ERROR"


def test_malformed_runtime_status_pass_output_is_error():
    result = _run(_CapturingRuntime("VERDICT: PASS\n", status={"state": "completed"}))

    assert result["verdict"] == "ERROR"


def test_evaluator_runtime_exception_is_error():
    result = _run(_CapturingRuntime(raises=TimeoutError("evaluator timed out")))

    assert result["verdict"] == "ERROR"


@pytest.mark.parametrize(
    ("runtime_status", "expected_verdict"),
    [("completed", "PASS"), ("failed", "ERROR")],
)
def test_evaluator_only_direct_call_uses_ephemeral_tool_denial_and_cleans_up(
    db_session,
    tmp_path,
    monkeypatch,
    isolated_openclaw_binding_root,
    runtime_status,
    expected_verdict,
):
    from app.models import TaskExecutionChangeSet
    from app.services.agents.openclaw_service import OpenClawSessionService
    from app.services.agents.runtime_configuration import (
        BackendRole,
        RoleRuntimeConfiguration,
    )
    from app.services.orchestration.coordinators.completion_coordinator import (
        CompletionCoordinator,
    )
    from app.tests.test_phase37_preb_f2b_binding_lifecycle import (
        _context,
        _persistent_state,
        _snapshot,
    )

    persistent_state, config_path = _persistent_state(tmp_path)
    persistent_before = _snapshot(persistent_state)
    context = _context(tmp_path, name="evaluator-only")
    product_root = context.project_workspace
    product_sentinel = product_root / "protected.txt"
    product_sentinel.write_text("unchanged", encoding="utf-8")
    product_before = {
        path.relative_to(product_root): path.read_bytes()
        for path in product_root.rglob("*")
        if path.is_file()
    }

    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))
    runtime = OpenClawSessionService(
        db_session,
        None,
        None,
        use_demo_mode=False,
        runtime_configuration=RoleRuntimeConfiguration(
            role=BackendRole.EXECUTION,
            backend_name="local_openclaw",
            model_family="qwen-local",
            adaptation_profile="openclaw_default",
        ),
    )
    runtime.bind_runtime_workspace(context)
    binding_dir = runtime._workspace_binding._tmp_dir
    observed = {}
    changesets_before = db_session.query(TaskExecutionChangeSet).count()

    try:

        async def _offline_execute_task(prompt, timeout_seconds=None):
            bound = json.loads(runtime._workspace_binding.config_path.read_text())
            agent = bound["agents"]["list"][0]
            observed["prompt"] = prompt
            observed["tool_deny"] = agent["tools"]["deny"]
            observed["workspace"] = agent["workspace"]
            return {"status": runtime_status, "output": "VERDICT: PASS\n"}

        runtime._apply_discovery_tool_suppression("PLANNING_DISCOVERY")
        runtime.execute_task = _offline_execute_task

        def _forbid_completion(*_args, **_kwargs):
            raise AssertionError("evaluator-only path entered CompletionCoordinator")

        monkeypatch.setattr(CompletionCoordinator, "complete_task", _forbid_completion)
        candidate, verification = _candidate_evidence(tmp_path)
        state = SimpleNamespace(
            reasoning_artifact={"intent": "Check the disposable candidate."},
            plan=[],
            execution_results=[],
            changed_files=["app/api/v1/router.py"],
            project_dir=str(tmp_path / "control-state"),
            session_id=None,
            task_id=None,
        )
        result = _run_evaluator(
            runtime_service=runtime,
            orchestration_state=state,
            prompt="Evaluate the disposable candidate.",
            summary="Offline evaluator-only regression.",
            candidate_evidence={"change_set": candidate},
            verification_evidence=verification,
            emit_live=lambda *_args, **_kwargs: None,
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        )
    finally:
        runtime.release_runtime_workspace_binding()
        shutil.rmtree(context.runtime_workspace, ignore_errors=True)

    assert result["verdict"] == expected_verdict
    assert observed["tool_deny"] == ["*"]
    assert observed["workspace"] == str(context.runtime_workspace)
    assert "ACTUAL_CANDIDATE_CHANGE" in observed["prompt"]
    assert db_session.query(TaskExecutionChangeSet).count() == changesets_before
    assert {
        path.relative_to(product_root): path.read_bytes()
        for path in product_root.rglob("*")
        if path.is_file()
    } == product_before
    assert _snapshot(persistent_state) == persistent_before
    assert not binding_dir.exists()
    assert not context.runtime_workspace.exists()
    assert runtime._workspace_binding is None


def test_final_review_projection_replaces_pre_evaluator_auto_promote(
    db_session, tmp_path, monkeypatch
):
    from app.models import TaskExecutionChangeSet
    from app.tests.test_phase36_maint_gr7_candidate_semantic_verification_authority import (
        _Runtime,
        _accepted,
        _seed_auto_publish,
    )

    result, ctx, _ = _seed_auto_publish(
        db_session,
        tmp_path,
        monkeypatch,
        _Runtime({"output": ""}),
        _accepted(),
    )

    assert result["status"] == "completed"
    record = (
        db_session.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == ctx.task_execution_id)
        .one()
    )
    assert record.review_decision["outcome"] == "hold_for_review"
    assert record.review_decision["reason"] == "evaluator_assessment_unavailable"
    assert record.review_decision["evaluator_pending"] is False
    assert record.review_decision["publication_eligible"] is False


def test_failed_runtime_pass_cannot_release_changeset_review_hold(
    db_session, tmp_path, monkeypatch
):
    from app.models import TaskExecutionChangeSet
    from app.tests.test_phase36_maint_gr7_candidate_semantic_verification_authority import (
        _Runtime,
        _accepted,
        _seed_auto_publish,
    )

    runtime = _Runtime({"status": "failed", "output": "VERDICT: PASS\n"})
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _accepted()
    )

    record = (
        db_session.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == ctx.task_execution_id)
        .one()
    )
    assert result["status"] == "completed"
    assert record.review_decision["outcome"] == "hold_for_review"
    assert record.review_decision["held_for_review"] is True
    assert record.review_decision["evaluator_verdict"] == "ERROR"
    assert record.review_decision["publication_eligible"] is False
    assert not (project_root / "README.md").exists()
