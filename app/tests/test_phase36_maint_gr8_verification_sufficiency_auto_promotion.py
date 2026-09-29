"""PHASE36-MAINT-GR8 — verification sufficiency and auto-promotion authority.

GR7 carried one condition. A repair task whose only verification is
smoke-only gets a completion verdict of status ``warning``, which counts as
accepted, with
``validation_evidence.verification_insufficient=True``. The Review decision
never read that flag, so an explicit evaluator PASS alone released automatic
publication.  Phase 10L introduced the flag with the stated intent that such a
result "may still be useful, but it should be marked as
verification_insufficient, not silently promoted".

GR8 keeps task completion and Candidate retention unchanged, and keeps the GR7
evaluator gate.  Automatic publication is additionally withheld when the
completion verdict reports ``verification_insufficient``: the workspace is held
for review with reason ``verification_insufficient_for_auto_promotion``.  The
verdicts below come from the real ``ValidatorService.validate_task_completion``.
No provider is called.
"""

from __future__ import annotations

import json

import pytest

from app.models import LogEntry, TaskExecutionChangeSet
from app.services.orchestration.types import CandidateFinding, ValidationVerdict
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_phase36_maint_gr7_candidate_semantic_verification_authority import (
    REENTRY3_EVALUATOR_OUTPUT,
    SUBSTANTIVE_NEEDS_REVIEW,
    SUBSTANTIVE_PASS,
    _Runtime,
    _seed_auto_publish,
)

REPAIR_PROMPT = "Fix the broken status behavior."
REPAIR_TITLE = "Fix status regression"
SMOKE_ONLY = "test -f app.py"
BEHAVIORAL = "python -c \"from app import status; assert status() == 'ready'\""
HOLD_REASON = "verification_insufficient_for_auto_promotion"


def _real_completion_verdict(tmp_path, *, prompt, title, verification):
    project_dir = tmp_path / "verdict-project"
    project_dir.mkdir()
    (project_dir / "app.py").write_text(
        "def status():\n    return 'ready'\n", encoding="utf-8"
    )
    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Update status",
                "verification": verification,
                "expected_files": ["app.py"],
            }
        ],
        task_prompt=prompt,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title=title,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["app.py"],
        },
    )
    return verdict


def _insufficient(tmp_path):
    verdict = _real_completion_verdict(
        tmp_path, prompt=REPAIR_PROMPT, title=REPAIR_TITLE, verification=SMOKE_ONLY
    )
    evidence = verdict.details["validation_evidence"]
    assert verdict.status == "warning" and verdict.accepted
    assert evidence["command_quality"] == "smoke_only"
    assert evidence["requires_independent_evidence"] is True
    assert evidence["verification_insufficient"] is True
    return verdict


def _sufficient(tmp_path):
    verdict = _real_completion_verdict(
        tmp_path, prompt=REPAIR_PROMPT, title=REPAIR_TITLE, verification=BEHAVIORAL
    )
    evidence = verdict.details["validation_evidence"]
    assert verdict.status == "accepted"
    assert evidence["requires_independent_evidence"] is True
    assert evidence["verification_insufficient"] is False
    return verdict


def _log_payload(db_session, ctx, message):
    entry = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_id == ctx.task_id)
        .filter(LogEntry.message.like(message + "%"))
        .one()
    )
    return json.loads(entry.log_metadata)


def _held_payload(db_session, ctx):
    return _log_payload(
        db_session, ctx, "[ORCHESTRATION] Held task workspace for manual review"
    )


def _assert_published(result, project_root):
    assert result["status"] == "completed"
    assert (project_root / "README.md").read_text(encoding="utf-8") == "after\n"


def _assert_held(db_session, result, ctx, project_root, reason):
    assert result["status"] == "completed"
    assert not (project_root / "README.md").exists()
    assert ctx.task.workspace_status == "ready"
    payload = _held_payload(db_session, ctx)
    assert payload["auto_publish_skipped"] is True
    assert payload["reason"] == reason
    assert payload["review_decision"]["held_for_review"] is True
    assert payload["review_decision"]["publication_eligible"] is False
    return payload


def test_r1_sufficient_verification_and_evaluator_pass_auto_publishes(
    db_session, tmp_path, monkeypatch
):
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, _ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _sufficient(tmp_path)
    )

    assert runtime.evaluator_calls == 1
    _assert_published(result, project_root)


@pytest.mark.parametrize(
    ("label", "runtime", "reason"),
    [
        (
            "r2_needs_review",
            _Runtime({"output": SUBSTANTIVE_NEEDS_REVIEW}),
            "evaluator_needs_review",
        ),
        (
            "r3_unknown",
            _Runtime({"output": REENTRY3_EVALUATOR_OUTPUT}),
            "evaluator_assessment_unavailable",
        ),
        (
            "r4_error",
            _Runtime(raises=TimeoutError("evaluator timed out")),
            "evaluator_assessment_unavailable",
        ),
    ],
)
def test_r2_r3_r4_sufficient_verification_non_pass_evaluator_holds(
    db_session, tmp_path, monkeypatch, label, runtime, reason
):
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _sufficient(tmp_path)
    )

    payload = _assert_held(db_session, result, ctx, project_root, reason)
    assert payload["review_decision"]["verification_insufficient"] is False, label


def test_r5_r12_insufficient_verification_with_evaluator_pass_is_held(
    db_session, tmp_path, monkeypatch
):
    # REENTRY-3 shape: repair task, smoke-only verification accepted as a
    # warning, and an explicit evaluator PASS.
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _insufficient(tmp_path)
    )

    assert runtime.evaluator_calls == 1
    payload = _assert_held(db_session, result, ctx, project_root, HOLD_REASON)
    decision = payload["review_decision"]
    assert decision["verification_insufficient"] is True
    assert decision["evaluator_verdict"] == "PASS"
    assert decision["outcome"] == "hold_for_review"


@pytest.mark.parametrize(
    ("label", "runtime", "reason", "evaluator_verdict"),
    [
        (
            "r6_needs_review",
            _Runtime({"output": SUBSTANTIVE_NEEDS_REVIEW}),
            "evaluator_needs_review",
            "NEEDS_REVIEW",
        ),
        (
            "r7_unknown",
            _Runtime({"output": ""}),
            "evaluator_assessment_unavailable",
            "UNKNOWN",
        ),
        (
            "r7_error",
            _Runtime(raises=TimeoutError("evaluator timed out")),
            "evaluator_assessment_unavailable",
            "ERROR",
        ),
    ],
)
def test_r6_r7_r13_insufficient_verification_non_pass_evaluator_holds(
    db_session, tmp_path, monkeypatch, label, runtime, reason, evaluator_verdict
):
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _insufficient(tmp_path)
    )

    payload = _assert_held(db_session, result, ctx, project_root, reason)
    decision = payload["review_decision"]
    assert decision["verification_insufficient"] is True, label
    assert decision["evaluator_verdict"] == evaluator_verdict


def test_r8_deterministic_error_blocks_before_evaluator(
    db_session, tmp_path, monkeypatch
):
    finding = CandidateFinding(
        rule_id="focused_pytest_failed",
        source="pytest",
        category="test",
        severity="error",
        attribution="candidate_introduced",
        repairable=False,
        message="Focused candidate pytest failed",
    )
    verdict = ValidationVerdict(
        stage="task_completion",
        status="rejected",
        profile="mutation",
        reasons=[finding.message],
        details={"validation_evidence": {"verification_insufficient": False}},
        findings=[finding],
    )
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, _ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, verdict
    )

    assert result["status"] == "failed"
    assert runtime.evaluator_calls == 0
    assert not (project_root / "README.md").exists()


def test_r9_ordinary_warning_without_insufficiency_still_auto_publishes(
    db_session, tmp_path, monkeypatch
):
    verdict = ValidationVerdict(
        stage="task_completion",
        status="warning",
        profile="mutation",
        reasons=["Verification integrity warning: non-repair weak assertion"],
        details={
            "expected_core_files": ["README.md"],
            "validation_evidence": {
                "command_quality": "smoke_only",
                "requires_independent_evidence": False,
                "verification_insufficient": False,
            },
        },
    )
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, _ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, verdict
    )

    _assert_published(result, project_root)


def test_r9_non_repair_smoke_only_is_sufficient_by_existing_policy(tmp_path):
    verdict = _real_completion_verdict(
        tmp_path,
        prompt="Add a status helper.",
        title="Add status helper",
        verification=SMOKE_ONLY,
    )
    evidence = verdict.details["validation_evidence"]

    assert verdict.status == "accepted"
    assert evidence["command_quality"] == "smoke_only"
    assert evidence["verification_insufficient"] is False


def test_r10_r11_insufficient_hold_retains_candidate_and_completes_task(
    db_session, tmp_path, monkeypatch
):
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _insufficient(tmp_path)
    )

    assert result["status"] == "completed"
    db_session.refresh(ctx.task)
    assert ctx.task.workspace_status == "ready"
    assert ctx.task.promoted_at is None
    change_set = (
        db_session.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == ctx.task_execution_id)
        .one()
    )
    assert change_set.disposition != "promoted"
    assert "README.md" in (
        list(change_set.added_files or []) + list(change_set.modified_files or [])
    )
    assert not (project_root / "README.md").exists()
