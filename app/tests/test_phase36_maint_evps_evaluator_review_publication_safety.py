"""PHASE36-MAINT-EVPS — evaluator evidence and publication-safety seams."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.phases.completion_flow import _run_evaluator


HISTORICAL_REENTRY12_OUTPUT = (
    "SCORES: goal=3/3 regressions=2/2 quality=2/2 files=3/3 TOTAL: 10/10 "
    "VERDICT: PASS NOTES: The permissions router is correctly imported and included "
    "once at the proper location in router.py, with the duplicate premature call "
    "removed, syntax validated, and no regressions introduced."
)


class _CapturingRuntime:
    def __init__(self, output: str = HISTORICAL_REENTRY12_OUTPUT):
        self.output = output
        self.prompt = ""

    async def execute_task(self, prompt, timeout_seconds=None):
        self.prompt = prompt
        return {"output": self.output}


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


def test_malformed_embedded_pass_remains_unknown(tmp_path):
    runtime = _CapturingRuntime(
        "SCORES: 10/10 VERDICT: PASS NOTES: embedded in one line"
    )

    assert _run(runtime)["verdict"] == "UNKNOWN"


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
