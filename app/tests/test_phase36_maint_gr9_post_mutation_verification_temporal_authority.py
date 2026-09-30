"""PHASE36-MAINT-GR9 — post-mutation verification temporal authority.

REENTRY-5: a repair task's Plan ran ``pytest`` in step 2 and mutated
``router.py`` in step 3.  ``validate_task_completion`` took the best
verification quality over every Plan step, so the pre-mutation pytest made
``has_independent_regression_test=True`` for a Candidate on which no relevant
test ran.

GR9 keeps the raw ``command_quality`` and adds Candidate applicability: a
step's verification applies only when no later Plan step mutated the
workspace (content-changing structured op, or Execution-recorded
``files_changed``).  Sufficiency is judged from applicable verification only.
Verdicts below come from the real Validator.  No provider is called.
"""

from __future__ import annotations

import pytest

from app.services.orchestration.execution.runtime import workspace_snapshot_key
from app.services.orchestration.types import ValidationVerdict
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_completion_verification_regressions import (
    _complete_task,
    _seed_legacy_finalize_ctx,
)
from app.tests.test_phase36_maint_gr7_candidate_semantic_verification_authority import (
    SUBSTANTIVE_NEEDS_REVIEW,
    SUBSTANTIVE_PASS,
    _Runtime,
    _seed_auto_publish,
)
from app.tests.test_phase36_maint_gr8_verification_sufficiency_auto_promotion import (
    HOLD_REASON,
    _assert_held,
)

REPAIR_PROMPT = "Fix the broken status behavior."
REPAIR_TITLE = "Fix status regression"
NON_REPAIR_PROMPT = "Add a status helper."
NON_REPAIR_TITLE = "Add status helper"
PYTEST = "python -m pytest tests/test_app.py -q"
SMOKE = 'python -c "import app"'
BEHAVIORAL = "python -c \"from app import status; assert status() == 'ready'\""
# REENTRY-5 steps 3/4: an import-only check that Phase 10L classifies as
# ``behavioral`` because it starts with ``from`` rather than ``import``.
REENTRY5_IMPORT_CHECK = "python -c \"from app import status; print('imported')\""
MUTATION = [
    {"op": "replace_in_file", "path": "app.py", "old": "'ready'", "new": "'ready'"}
]


def _step(number, verification="", ops=None):
    step = {
        "step_number": number,
        "description": f"step {number}",
        "verification": verification,
        "expected_files": ["app.py"],
    }
    if ops:
        step["ops"] = ops
    return step


def _verdict(
    tmp_path,
    plan,
    *,
    prompt=REPAIR_PROMPT,
    title=REPAIR_TITLE,
    step_changed_files=None,
    completion_verification_command=None,
):
    project_dir = tmp_path / "project"
    (project_dir / "tests").mkdir(parents=True)
    (project_dir / "app.py").write_text(
        "def status():\n    return 'ready'\n", encoding="utf-8"
    )
    (project_dir / "tests" / "test_app.py").write_text(
        "from app import status\n\n"
        "def test_status():\n"
        "    assert status() == 'ready'\n",
        encoding="utf-8",
    )
    evidence = {
        "summary_generated": True,
        "execution_results_count": len(plan),
        "reported_changed_files": ["app.py"],
        "change_set": {"modified_files": ["app.py"]},
    }
    if step_changed_files is not None:
        evidence["step_changed_files"] = step_changed_files
    if completion_verification_command:
        evidence["completion_verification_command"] = completion_verification_command
    return ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=plan,
        task_prompt=prompt,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title=title,
        completion_evidence=evidence,
    )


def _evidence(verdict):
    return verdict.details["validation_evidence"]


def test_r1_regression_test_before_mutation_does_not_establish_sufficiency(
    tmp_path,
):
    verdict = _verdict(tmp_path, [_step(1, PYTEST), _step(2, "", MUTATION)])
    evidence = _evidence(verdict)

    assert evidence["command_quality"] == "regression_test"
    assert evidence["applicable_command_quality"] == "missing"
    assert evidence["verification_invalidated_by_later_mutation"] is True
    assert evidence["has_independent_regression_test"] is False
    assert evidence["verification_insufficient"] is True
    assert verdict.accepted is False
    assert any("before a later mutation" in reason for reason in verdict.reasons)
    by_step = evidence["command_quality_by_step"]
    assert by_step[0]["applies_to_candidate"] is False
    assert by_step[0]["invalidated_by_step"] == 2


def test_r2_mutation_then_regression_test_establishes_sufficiency(tmp_path):
    verdict = _verdict(tmp_path, [_step(1, "", MUTATION), _step(2, PYTEST)])
    evidence = _evidence(verdict)

    assert verdict.status == "accepted"
    assert evidence["applicable_command_quality"] == "regression_test"
    assert evidence["verification_invalidated_by_later_mutation"] is False
    assert evidence["has_independent_regression_test"] is True
    assert evidence["verification_insufficient"] is False


def test_r3_same_step_verification_applies_after_the_step_mutation(tmp_path):
    # Execution applies a step's ops, then its commands, then runs the declared
    # verification, so same-step verification observes the mutated workspace.
    verdict = _verdict(tmp_path, [_step(1, PYTEST, MUTATION)])
    evidence = _evidence(verdict)

    assert verdict.status == "accepted"
    assert evidence["command_quality_by_step"][0]["applies_to_candidate"] is True
    assert evidence["has_independent_regression_test"] is True
    assert evidence["verification_insufficient"] is False


def test_r4_post_mutation_test_establishes_applicable_evidence(tmp_path):
    verdict = _verdict(
        tmp_path, [_step(1, PYTEST), _step(2, "", MUTATION), _step(3, PYTEST)]
    )
    evidence = _evidence(verdict)

    assert verdict.status == "accepted"
    assert [e["applies_to_candidate"] for e in evidence["command_quality_by_step"]] == [
        False,
        True,
        True,
    ]
    assert evidence["verification_invalidated_by_later_mutation"] is False
    assert evidence["has_independent_regression_test"] is True


def test_r5_intermediate_test_does_not_cover_a_later_mutation(tmp_path):
    verdict = _verdict(
        tmp_path,
        [_step(1, "", MUTATION), _step(2, PYTEST), _step(3, "", MUTATION)],
    )
    evidence = _evidence(verdict)

    assert evidence["command_quality_by_step"][1]["invalidated_by_step"] == 3
    assert evidence["has_independent_regression_test"] is False
    assert evidence["verification_insufficient"] is True
    assert verdict.accepted is False


def test_r6_final_post_mutation_test_establishes_sufficiency(tmp_path):
    verdict = _verdict(
        tmp_path,
        [
            _step(1, "", MUTATION),
            _step(2, PYTEST),
            _step(3, "", MUTATION),
            _step(4, PYTEST),
        ],
    )
    evidence = _evidence(verdict)

    assert verdict.status == "accepted"
    assert evidence["has_independent_regression_test"] is True
    assert evidence["verification_insufficient"] is False


def test_r7_no_mutation_keeps_existing_semantics(tmp_path):
    verdict = _verdict(tmp_path, [_step(1, PYTEST), _step(2, SMOKE)])
    evidence = _evidence(verdict)

    assert verdict.status == "accepted"
    assert evidence["applicable_command_quality"] == "regression_test"
    assert evidence["verification_invalidated_by_later_mutation"] is False
    assert evidence["has_independent_regression_test"] is True


def test_r7_mkdir_is_not_a_candidate_producing_mutation(tmp_path):
    verdict = _verdict(
        tmp_path,
        [_step(1, PYTEST), _step(2, "", [{"op": "mkdir", "path": "pkg"}])],
    )

    assert _evidence(verdict)["has_independent_regression_test"] is True


def test_r7_completion_verification_command_always_applies(tmp_path):
    verdict = _verdict(
        tmp_path,
        [_step(1, "", MUTATION)],
        completion_verification_command=PYTEST,
    )
    evidence = _evidence(verdict)

    assert evidence["applicable_command_quality"] == "regression_test"
    assert evidence["has_independent_regression_test"] is True


def test_r8_static_checks_keep_their_classification(tmp_path):
    verdict = _verdict(
        tmp_path,
        [_step(1, "python -m compileall -q .", MUTATION), _step(2, "flake8 app.py")],
    )
    qualities = [
        entry["command_quality"]
        for entry in _evidence(verdict)["command_quality_by_step"]
    ]

    # Unchanged Phase 10L classification; running after the mutation does not
    # promote a static check to a regression test.
    assert qualities == ["behavioral", "insufficient"]
    assert _evidence(verdict)["has_independent_regression_test"] is False


def test_r9_smoke_only_after_mutation_is_still_insufficient(tmp_path):
    verdict = _verdict(tmp_path, [_step(1, PYTEST), _step(2, SMOKE, MUTATION)])
    evidence = _evidence(verdict)

    assert verdict.status == "warning" and verdict.accepted
    assert evidence["command_quality"] == "regression_test"
    assert evidence["applicable_command_quality"] == "smoke_only"
    assert evidence["verification_insufficient"] is True


def test_r10_non_repair_task_policy_is_unchanged(tmp_path):
    verdict = _verdict(
        tmp_path,
        [_step(1, PYTEST), _step(2, "", MUTATION)],
        prompt=NON_REPAIR_PROMPT,
        title=NON_REPAIR_TITLE,
    )
    evidence = _evidence(verdict)

    assert verdict.status == "accepted"
    assert evidence["requires_independent_evidence"] is False
    assert evidence["verification_invalidated_by_later_mutation"] is True
    assert evidence["verification_insufficient"] is False


def test_runtime_recorded_mutation_invalidates_earlier_verification(tmp_path):
    # A step without structured ops whose Execution result recorded a file
    # change (free-form runtime edit) is a candidate-producing mutation.
    verdict = _verdict(
        tmp_path,
        [_step(1, PYTEST), _step(2, SMOKE)],
        step_changed_files=[
            {"step_number": 1, "files_changed": []},
            {"step_number": 2, "files_changed": ["app.py"]},
        ],
    )
    evidence = _evidence(verdict)

    assert evidence["command_quality_by_step"][0]["invalidated_by_step"] == 2
    assert evidence["has_independent_regression_test"] is False
    assert evidence["verification_insufficient"] is True


def test_post_plan_step_results_are_not_attributed_to_plan_steps(tmp_path):
    # Completion-repair results are numbered after the Plan.  GR9 does not
    # model them (carried gap); they must not be matched to a Plan step.
    verdict = _verdict(
        tmp_path,
        [_step(1, "", MUTATION), _step(2, PYTEST)],
        step_changed_files=[{"step_number": 3, "files_changed": ["app.py"]}],
    )

    assert _evidence(verdict)["has_independent_regression_test"] is True


REENTRY5_PLAN = [
    _step(1, SMOKE),
    _step(2, PYTEST),
    _step(3, REENTRY5_IMPORT_CHECK, MUTATION),
    _step(4, REENTRY5_IMPORT_CHECK),
]


def test_r11_reentry5_exact_shape_no_longer_credits_the_pre_mutation_pytest(
    tmp_path,
):
    verdict = _verdict(tmp_path, REENTRY5_PLAN)
    evidence = _evidence(verdict)

    assert evidence["command_quality"] == "regression_test"
    assert evidence["requires_independent_evidence"] is True
    assert evidence["has_independent_regression_test"] is False
    assert evidence["verification_invalidated_by_later_mutation"] is True
    assert evidence["command_quality_by_step"][1]["invalidated_by_step"] == 3
    # Carried classifier gap, not GR9: the import-only ``from`` check is
    # classified ``behavioral``, which Phase 10L/GR8 accept as sufficient.
    assert evidence["applicable_command_quality"] == "behavioral"
    assert evidence["verification_insufficient"] is False


def _reentry5_smoke_verdict(tmp_path):
    # REENTRY-5 shape with the post-mutation checks classified as they are
    # (import-only): the only regression test ran before the mutation.
    verdict = _verdict(
        tmp_path,
        [_step(1, SMOKE), _step(2, PYTEST), _step(3, SMOKE, MUTATION), _step(4, SMOKE)],
    )
    evidence = _evidence(verdict)
    assert verdict.status == "warning" and verdict.accepted
    assert evidence["command_quality"] == "regression_test"
    assert evidence["has_independent_regression_test"] is False
    assert evidence["verification_insufficient"] is True
    return verdict


def test_r11_reentry5_shape_without_post_mutation_test_is_insufficient(tmp_path):
    _reentry5_smoke_verdict(tmp_path)


def test_r12_gr8_holds_on_evaluator_pass(db_session, tmp_path, monkeypatch):
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _reentry5_smoke_verdict(tmp_path)
    )

    assert runtime.evaluator_calls == 1
    payload = _assert_held(db_session, result, ctx, project_root, HOLD_REASON)
    decision = payload["review_decision"]
    assert decision["outcome"] == "hold_for_review"
    assert decision["evaluator_verdict"] == "PASS"
    assert decision["verification_insufficient"] is True


@pytest.mark.parametrize(
    ("label", "runtime", "reason"),
    [
        (
            "needs_review",
            _Runtime({"output": SUBSTANTIVE_NEEDS_REVIEW}),
            "evaluator_needs_review",
        ),
        ("unknown", _Runtime({"output": ""}), "evaluator_assessment_unavailable"),
        (
            "error",
            _Runtime(raises=TimeoutError("evaluator timed out")),
            "evaluator_assessment_unavailable",
        ),
    ],
)
def test_r13_gr7_non_pass_keeps_evaluator_reason(
    db_session, tmp_path, monkeypatch, label, runtime, reason
):
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _reentry5_smoke_verdict(tmp_path)
    )

    payload = _assert_held(db_session, result, ctx, project_root, reason)
    assert payload["review_decision"]["verification_insufficient"] is True, label


def test_r14_invalidated_verification_rejection_blocks_before_evaluator(
    db_session, tmp_path, monkeypatch
):
    verdict = _verdict(tmp_path, [_step(1, PYTEST), _step(2, "", MUTATION)])
    assert verdict.accepted is False
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, _ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, verdict
    )

    assert result["status"] == "failed"
    assert runtime.evaluator_calls == 0
    assert not (project_root / "README.md").exists()


def test_coordinator_passes_step_changed_files_to_every_completion_validation(
    db_session, tmp_path, monkeypatch
):
    ctx, execution, _project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    ctx.runtime_service = _Runtime({"output": SUBSTANTIVE_PASS})
    ctx.orchestration_state.execution_results[0].files_changed = ["README.md"]
    (workspace_dir / "README.md").write_text("before\n", encoding="utf-8")
    ctx.task_service.create_workspace_snapshot(
        ctx.project,
        workspace_dir,
        snapshot_key=workspace_snapshot_key(ctx.task_id, execution.id),
        preserve_project_root_rules=False,
    )
    (workspace_dir / "README.md").write_text("after\n", encoding="utf-8")
    captured: list[dict] = []

    def _capture(**kwargs):
        captured.append(dict(kwargs["completion_evidence"]))
        return ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="mutation",
            reasons=[],
            details={"validation_evidence": {"verification_insufficient": False}},
        )

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.get_effective_workspace_review_policy",
        lambda default_policy, db=None: "hold_nontrivial",
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        _capture,
    )
    _complete_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    # Gating validation and post_change_set_completion_validation.
    assert len(captured) == 2
    assert "change_set" in captured[1]
    for evidence in captured:
        assert evidence["step_changed_files"] == [
            {"step_number": 1, "files_changed": ["README.md"]}
        ]
