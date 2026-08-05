"""Phase 14B-1: CompletionCoordinator tests.

Covers the coordinator's orchestration decisions directly, not via the
finalize_successful_task shim. Each test mocks the algorithm delegates and
asserts the coordinator routes correctly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.coordinators.completion_coordinator import (
    CompletionCoordinator,
    CompletionOutcome,
)
from app.services.orchestration.review_policy import decide_change_set_review
from app.services.orchestration.state.execution_states import TerminalReason
from app.services.orchestration.types import ValidationVerdict


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_validation_verdict(
    *,
    status: str = "accepted",
    repairable: bool = False,
    warning: bool = False,
    accepted: bool = True,
    reasons: list | None = None,
) -> ValidationVerdict:
    v = ValidationVerdict(
        stage="task_completion",
        status=status,
        profile="implementation",
        reasons=reasons or [],
        details={"expected_core_files": ["app.py"]},
    )
    return v


def _make_ctx(tmp_path):
    """Build a minimal OrchestrationRunContext-like namespace for coordinator tests."""
    from app.services.orchestration.prompt_templates import OrchestrationState

    orch_state = OrchestrationState(
        session_id="1",
        task_description="test task",
        project_name="test-project",
        project_context="",
        task_id=1,
    )
    orch_state._project_dir_override = str(tmp_path)

    task = SimpleNamespace(
        id=1,
        title="Test task",
        description="",
        plan_position=1,
        status=MagicMock(value="done"),
        steps=None,
        current_step=0,
        task_subfolder=None,
        error_message=None,
        workspace_status=None,
        template_id=None,
    )
    session = SimpleNamespace(
        id=1,
        instance_id="inst-1",
        model_lane_label=None,
        repair_churn_stopped=False,
        repair_churn_trigger=None,
        project_id=1,
    )
    project = SimpleNamespace(id=1, name="test", workspace_path=str(tmp_path))

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    task_service = MagicMock()
    task_service.analyze_workspace_consistency.return_value = {}
    task_service.persist_task_execution_change_set.return_value = None
    task_service.change_set_review_decision.return_value = {
        "held_for_review": False,
        "outcome": "auto_promote",
        "reason": "no_significant_changes",
    }
    task_service.auto_publish_task_into_baseline.return_value = {
        "files_copied": 0,
        "auto_publish_skipped": False,
    }
    task_service.validate_task_baseline_materialization.return_value = {
        "baseline_path": str(tmp_path),
        "baseline_file_count": 0,
        "missing_expected_files": [],
        "consistency_issues": [],
        "consistency": {},
    }
    task_service.validate_project_baseline.return_value = {
        "missing_expected_files": [],
        "prior_expected_files": [],
    }

    runtime_service = MagicMock()
    runtime_service.get_backend_metadata.return_value = {
        "backend": "test",
        "model_family": "test",
    }

    ctx = SimpleNamespace(
        db=db,
        session=session,
        project=project,
        task=task,
        session_task_link=None,
        session_id=1,
        task_id=1,
        task_execution_id=None,
        session_instance_id="inst-1",
        prompt="Build a calculator",
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=orch_state,
        runtime_service=runtime_service,
        task_service=task_service,
        logger=MagicMock(),
        emit_live=MagicMock(),
        error_handler=MagicMock(),
        policy_profile_name="balanced",
        validation_severity="standard",
        completion_repair_budget=2,
        workflow_stage=None,
        restore_workspace_snapshot_if_needed=None,
        planning_backend="test",
        execution_backend="test",
        guidance_backend="test",
        guidance_model_name="test",
        guidance_model_family="test",
    )
    return ctx


def _NOOP_FN(*args, **kwargs):
    return None


def _patch_coordinator_delegates(
    monkeypatch, *, validation_verdict, repair_result=None
):
    """Patch all algorithm delegates the coordinator calls."""
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._generate_task_summary_with_fallback",
        lambda ctx, summary_prompt: {"output": "Task done", "pn_summary": "Task done"},
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._scope_workspace_consistency_to_task_changes",
        lambda ws, plan, reported_changed_files: ws,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: validation_verdict,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.record_validation_verdict",
        _NOOP_FN,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: (None, None),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.get_effective_workspace_review_policy",
        lambda default_policy, db=None: "auto_publish_all",
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.append_orchestration_event",
        _NOOP_FN,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.emit_phase_event",
        _NOOP_FN,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.assemble_task_summary_prompt",
        lambda ctx: "summary prompt",
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.TaskCompletionFinalizer",
        _make_mock_finalizer(),
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator._generate_task_summary_with_fallback",
        lambda ctx, summary_prompt: {"output": "Task done", "pn_summary": "Task done"},
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator._scope_workspace_consistency_to_task_changes",
        lambda ws, plan, reported_changed_files: ws,
    )
    # write_working_memory and post_write_check are deferred imports inside the method
    monkeypatch.setattr(
        "app.services.orchestration.working_memory.write_working_memory",
        _NOOP_FN,
    )
    if repair_result is not None:
        monkeypatch.setattr(
            "app.services.orchestration.phases.completion_flow._attempt_completion_repair",
            lambda ctx, completion_validation, save_orchestration_checkpoint_fn: repair_result,
        )


def _make_mock_finalizer():
    class _MockFinalizer:
        def __init__(self, db, task_service):
            pass

        def finalize_success(self, **kwargs):
            return {"promoted_workspace_archive_result": None}

    return _MockFinalizer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_outcome_dataclass_has_expected_fields():
    outcome = CompletionOutcome(status="completed", task_id=1, session_id=2)
    assert outcome.status == "completed"
    assert outcome.task_id == 1
    assert outcome.session_id == 2
    assert outcome.terminal_reason is None
    assert outcome.repair_attempted is False
    assert outcome.repair_succeeded is False
    assert outcome.verification_passed is False


def test_complete_task_validation_success(tmp_path, monkeypatch):
    """Coordinator returns completed when validation passes on first try."""
    ctx = _make_ctx(tmp_path)
    accepted_verdict = _make_validation_verdict(status="accepted", accepted=True)
    _patch_coordinator_delegates(monkeypatch, validation_verdict=accepted_verdict)

    with patch(
        "app.services.human_guidance.post_write_checker.run_post_write_check_if_enabled",
        _NOOP_FN,
    ):
        result = CompletionCoordinator().complete_task(
            ctx=ctx,
            write_project_state_snapshot_fn=_NOOP_FN,
            save_orchestration_checkpoint_fn=_NOOP_FN,
        )

    assert result["status"] == "completed"
    assert result["task_id"] == 1
    assert result["session_id"] == 1


def _completion_registered_contract(
    *,
    review_expectation="REVIEW_NOT_REQUIRED",
    publication_expectation="PUBLICATION_REQUIRED",
):
    review = {
        "contract_id": "ST23-REVIEW-001",
        "contract_version": "v1",
        "expectation": review_expectation,
        "scenario_id": "S1-2",
    }
    publication = {
        "contract_id": "ST23-PUBLICATION-001",
        "contract_version": "v1",
        "expectation": publication_expectation,
        "scenario_id": "S1-2",
    }
    return {
        "contract_source": "phase31_certification_runner",
        "contract_id": "ST23-PLANNER-001",
        "contract_version": "v1",
        "scenario_id": "S1-2",
        "review_expectation": review_expectation,
        "publication_expectation": publication_expectation,
        "review_contract": review,
        "publication_contract": publication,
        "registered_scenario_contract": {
            "scenario_id": "S1-2",
            "review_contract": review,
            "publication_contract": publication,
        },
    }


def test_completion_propagates_registered_intent_and_publishes_when_eligible(
    tmp_path, monkeypatch
):
    ctx = _make_ctx(tmp_path)
    ctx.task_execution_id = 42
    ctx.runs_in_canonical_baseline = True
    ctx.runtime_workspace_used = True
    ctx.planner_contract = _completion_registered_contract()
    ctx.task_service.persist_task_execution_change_set.return_value = {
        "changed_count": 2,
        "warning_flags": ["scaffold_or_test_surface_changed"],
    }
    ctx.task_service.change_set_review_decision.side_effect = decide_change_set_review
    ctx.task_service.promote_change_set_into_baseline.return_value = {
        "files_copied": 2,
        "auto_publish_skipped": False,
    }
    ctx.task_service.validate_task_baseline_materialization.return_value = {
        "baseline_path": str(tmp_path),
        "baseline_file_count": 2,
        "expected_files": ["app/time_utils.py"],
        "missing_expected_files": [],
        "consistency_issues": [],
    }
    ctx.task_service.validate_project_baseline.return_value = {
        "missing_expected_files": [],
        "prior_expected_files": [
            {"task_id": 9, "path": "prior.py", "baseline_present": True}
        ],
    }
    accepted_verdict = _make_validation_verdict(status="accepted", accepted=True)
    _patch_coordinator_delegates(monkeypatch, validation_verdict=accepted_verdict)
    validator_calls = []

    def validate_baseline_publish(**kwargs):
        validator_calls.append(kwargs)
        return accepted_verdict

    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.ValidatorService.validate_baseline_publish",
        validate_baseline_publish,
    )

    with patch(
        "app.services.human_guidance.post_write_checker.run_post_write_check_if_enabled",
        _NOOP_FN,
    ):
        result = CompletionCoordinator().complete_task(
            ctx=ctx,
            write_project_state_snapshot_fn=_NOOP_FN,
            save_orchestration_checkpoint_fn=_NOOP_FN,
        )

    assert result["status"] == "completed"
    persist_kwargs = ctx.task_service.persist_task_execution_change_set.call_args.kwargs
    review_kwargs = ctx.task_service.change_set_review_decision.call_args.kwargs
    assert persist_kwargs["planner_contract"] == ctx.planner_contract
    assert review_kwargs["planner_contract"] == ctx.planner_contract
    assert review_kwargs["template_review_policy"] is None
    assert ctx.task_service.promote_change_set_into_baseline.called
    ctx.task_service.auto_publish_task_into_baseline.assert_not_called()
    assert len(validator_calls) == 2
    assert validator_calls[0]["candidate_change_set"] is not None
    assert "candidate_change_set" not in validator_calls[1]
    assert validator_calls[0]["current_expected_files"] == ["app/time_utils.py"]
    assert validator_calls[1]["current_expected_files"] == ["app/time_utils.py"]
    assert validator_calls[0]["prior_expected_files"][0]["path"] == "prior.py"
    assert validator_calls[1]["prior_expected_files"][0]["path"] == "prior.py"


def test_rejected_baseline_publish_never_materializes_captured_runtime_candidate(
    tmp_path, monkeypatch
):
    """Regression seam for Phase 32J-1's failed auto-promotion escape.

    The provider-free lifecycle uses a disposable runtime candidate, captures
    its authorized three-file change set, then forces baseline validation to
    reject.  Canonical content must remain byte-identical; promotion is not a
    pre-validation side effect.
    """
    canonical = tmp_path / "canonical"
    runtime = tmp_path / "runtime"
    canonical.mkdir()
    runtime.mkdir()
    (canonical / "existing.py").write_text("before = 1\n", encoding="utf-8")
    (runtime / "existing.py").write_text("before = 2\n", encoding="utf-8")
    (runtime / "added_one.py").write_text("one = 1\n", encoding="utf-8")
    (runtime / "added_two.py").write_text("two = 2\n", encoding="utf-8")
    canonical_before = {"existing.py": (canonical / "existing.py").read_bytes()}

    ctx = _make_ctx(canonical)
    ctx.task_execution_id = 254
    ctx.runs_in_canonical_baseline = True
    ctx.runtime_workspace_used = True
    ctx.planner_contract = _completion_registered_contract()
    ctx.task_service.persist_task_execution_change_set.return_value = {
        "task_execution_id": 254,
        "target_path": str(runtime),
        "added_files": ["added_one.py", "added_two.py"],
        "modified_files": ["existing.py"],
        "deleted_files": [],
        "changed_count": 3,
        "warning_flags": [],
    }
    ctx.task_service.change_set_review_decision.side_effect = decide_change_set_review
    ctx.task_service.validate_task_baseline_materialization.return_value = {
        "baseline_path": str(canonical),
        "baseline_file_count": 1,
        "missing_expected_files": [],
        "consistency_issues": [],
        "consistency": {},
    }
    ctx.task_service.validate_project_baseline.return_value = {
        "missing_expected_files": [{"path": "historical.py"}]
    }
    rejected = _make_validation_verdict(status="repair_required", accepted=False)
    _patch_coordinator_delegates(
        monkeypatch, validation_verdict=_make_validation_verdict()
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.ValidatorService.validate_baseline_publish",
        lambda **kwargs: rejected,
    )

    def materialize_candidate(*_args, **_kwargs):
        for relative in ("existing.py", "added_one.py", "added_two.py"):
            (canonical / relative).write_bytes((runtime / relative).read_bytes())
        return {"files_copied": 3, "auto_publish_skipped": False}

    ctx.task_service.promote_change_set_into_baseline.side_effect = (
        materialize_candidate
    )
    with patch(
        "app.services.human_guidance.post_write_checker.run_post_write_check_if_enabled",
        _NOOP_FN,
    ):
        result = CompletionCoordinator().complete_task(
            ctx=ctx,
            write_project_state_snapshot_fn=_NOOP_FN,
            save_orchestration_checkpoint_fn=_NOOP_FN,
        )

    assert result["reason"] == "baseline_publish_validation_failed"
    assert (canonical / "existing.py").read_bytes() == canonical_before["existing.py"]
    assert not (canonical / "added_one.py").exists()
    assert not (canonical / "added_two.py").exists()
    ctx.task_service.promote_change_set_into_baseline.assert_not_called()


def test_baseline_publish_preflight_exception_never_materializes_candidate(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "existing.py").write_text("before = 1\n", encoding="utf-8")
    ctx = _make_ctx(canonical)
    ctx.task_execution_id = 255
    ctx.runs_in_canonical_baseline = True
    ctx.runtime_workspace_used = True
    ctx.planner_contract = _completion_registered_contract()
    ctx.task_service.persist_task_execution_change_set.return_value = {
        "task_execution_id": 255,
        "added_files": ["candidate.py"],
        "modified_files": [],
        "deleted_files": [],
        "changed_count": 1,
        "warning_flags": [],
    }
    ctx.task_service.change_set_review_decision.side_effect = decide_change_set_review
    ctx.task_service.validate_task_baseline_materialization.return_value = {
        "baseline_path": str(canonical),
        "baseline_file_count": 1,
        "missing_expected_files": [],
        "consistency_issues": [],
        "consistency": {},
    }
    ctx.task_service.validate_project_baseline.return_value = {
        "missing_expected_files": []
    }
    _patch_coordinator_delegates(
        monkeypatch, validation_verdict=_make_validation_verdict()
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.ValidatorService.validate_baseline_publish",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("preflight exploded")),
    )

    with patch(
        "app.services.human_guidance.post_write_checker.run_post_write_check_if_enabled",
        _NOOP_FN,
    ):
        result = CompletionCoordinator().complete_task(
            ctx=ctx,
            write_project_state_snapshot_fn=_NOOP_FN,
            save_orchestration_checkpoint_fn=_NOOP_FN,
        )

    assert result["status"] == "failed"
    assert result["reason"] == "baseline_publish_validation_failed"
    assert (canonical / "existing.py").read_text(encoding="utf-8") == "before = 1\n"
    assert not (canonical / "candidate.py").exists()
    ctx.task_service.promote_change_set_into_baseline.assert_not_called()


def test_completion_does_not_publish_when_registered_publication_is_not_required(
    tmp_path, monkeypatch
):
    ctx = _make_ctx(tmp_path)
    ctx.task_execution_id = 42
    ctx.runs_in_canonical_baseline = True
    ctx.runtime_workspace_used = True
    ctx.planner_contract = _completion_registered_contract(
        publication_expectation="PUBLICATION_NOT_REQUIRED"
    )
    ctx.task_service.persist_task_execution_change_set.return_value = {
        "changed_count": 1,
        "warning_flags": [],
    }
    ctx.task_service.change_set_review_decision.side_effect = decide_change_set_review
    accepted_verdict = _make_validation_verdict(status="accepted", accepted=True)
    _patch_coordinator_delegates(monkeypatch, validation_verdict=accepted_verdict)

    with patch(
        "app.services.human_guidance.post_write_checker.run_post_write_check_if_enabled",
        _NOOP_FN,
    ):
        result = CompletionCoordinator().complete_task(
            ctx=ctx,
            write_project_state_snapshot_fn=_NOOP_FN,
            save_orchestration_checkpoint_fn=_NOOP_FN,
        )

    assert result["status"] == "completed"
    ctx.task_service.promote_change_set_into_baseline.assert_not_called()
    ctx.task_service.auto_publish_task_into_baseline.assert_not_called()


def test_complete_task_verification_success(tmp_path, monkeypatch):
    """Coordinator passes through when verification command succeeds."""
    ctx = _make_ctx(tmp_path)
    accepted_verdict = _make_validation_verdict(status="accepted", accepted=True)
    _patch_coordinator_delegates(monkeypatch, validation_verdict=accepted_verdict)
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: ("pytest --tb=short", "python test suite"),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._execute_completion_verification",
        lambda project_dir, command: {
            "success": True,
            "returncode": 0,
            "output": "1 passed",
        },
    )

    with patch(
        "app.services.human_guidance.post_write_checker.run_post_write_check_if_enabled",
        _NOOP_FN,
    ):
        result = CompletionCoordinator().complete_task(
            ctx=ctx,
            write_project_state_snapshot_fn=_NOOP_FN,
            save_orchestration_checkpoint_fn=_NOOP_FN,
        )

    assert result["status"] == "completed"


def test_complete_task_completion_validation_failure(tmp_path, monkeypatch):
    """Coordinator aborts with COMPLETION_VALIDATION_FAILED when validation rejects."""
    ctx = _make_ctx(tmp_path)
    rejected_verdict = ValidationVerdict(
        stage="task_completion",
        status="rejected",
        profile="implementation",
        reasons=["No files changed"],
        details={},
    )
    _patch_coordinator_delegates(monkeypatch, validation_verdict=rejected_verdict)
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.persist_debug_feedback_envelope",
        _NOOP_FN,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.build_debug_feedback_envelope",
        lambda **kwargs: SimpleNamespace(
            failure_class="missing_files",
            eligible_for_debug_repair=False,
            stderr_excerpt="",
            return_code=None,
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.RecoveryStrategyRegistry.execute_recovery",
        lambda **kwargs: {"status": "skipped"},
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.mark_task_attempt_failed",
        _NOOP_FN,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.mark_session_paused",
        _NOOP_FN,
    )

    result = CompletionCoordinator().complete_task(
        ctx=ctx,
        write_project_state_snapshot_fn=_NOOP_FN,
        save_orchestration_checkpoint_fn=_NOOP_FN,
    )

    assert result["status"] == "failed"
    assert result["reason"] == TerminalReason.COMPLETION_VALIDATION_FAILED


def test_complete_task_completion_repair_success(tmp_path, monkeypatch):
    """Coordinator routes through repair and returns completed when repair succeeds."""
    ctx = _make_ctx(tmp_path)

    repairable_verdict = ValidationVerdict(
        stage="task_completion",
        status="repair_required",
        profile="implementation",
        reasons=["Missing output file"],
        details={},
    )
    accepted_after_repair = ValidationVerdict(
        stage="task_completion",
        status="accepted",
        profile="implementation",
        reasons=[],
        details={},
    )

    call_count = [0]

    def _side_effect_validator(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return repairable_verdict
        return accepted_after_repair

    _patch_coordinator_delegates(
        monkeypatch,
        validation_verdict=repairable_verdict,
        repair_result={"status": "success", "step": {"description": "fix"}},
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        _side_effect_validator,
    )

    with patch(
        "app.services.human_guidance.post_write_checker.run_post_write_check_if_enabled",
        _NOOP_FN,
    ):
        result = CompletionCoordinator().complete_task(
            ctx=ctx,
            write_project_state_snapshot_fn=_NOOP_FN,
            save_orchestration_checkpoint_fn=_NOOP_FN,
        )

    assert result["status"] == "completed"
    assert call_count[0] == 2


def test_complete_task_completion_repair_failure(tmp_path, monkeypatch):
    """Coordinator aborts with COMPLETION_REPAIR_FAILED when repair fails."""
    ctx = _make_ctx(tmp_path)
    repairable_verdict = ValidationVerdict(
        stage="task_completion",
        status="repair_required",
        profile="implementation",
        reasons=["Missing file"],
        details={},
    )
    _patch_coordinator_delegates(
        monkeypatch,
        validation_verdict=repairable_verdict,
        repair_result={"status": "failed", "reason": "repair_step_parse_failed"},
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.mark_task_attempt_failed",
        _NOOP_FN,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.mark_session_paused",
        _NOOP_FN,
    )

    result = CompletionCoordinator().complete_task(
        ctx=ctx,
        write_project_state_snapshot_fn=_NOOP_FN,
        save_orchestration_checkpoint_fn=_NOOP_FN,
    )

    assert result["status"] == "failed"
    assert result["reason"] == TerminalReason.COMPLETION_REPAIR_FAILED


def test_complete_task_verification_integrity_failure(tmp_path, monkeypatch):
    """Coordinator aborts with VERIFICATION_INTEGRITY_FAILED on change-set rejection."""
    ctx = _make_ctx(tmp_path)
    accepted_verdict = _make_validation_verdict(status="accepted", accepted=True)
    integrity_rejected = ValidationVerdict(
        stage="task_completion",
        status="rejected",
        profile="mutation",
        reasons=["Unexpected file deleted"],
        details={},
    )

    _patch_coordinator_delegates(monkeypatch, validation_verdict=accepted_verdict)

    call_count = [0]

    def _validator_side_effect(**kwargs):
        call_count[0] += 1
        if call_count[0] <= 1:
            return accepted_verdict
        return integrity_rejected

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        _validator_side_effect,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        _validator_side_effect,
    )
    # Override task_service to return a change_set so the integrity path runs
    fake_change_set = {"changed_count": 1, "warning_flags": ["deleted_files"]}
    ctx.task_service.persist_task_execution_change_set.return_value = fake_change_set
    ctx.task_execution_id = 42

    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.ValidatorService.validate_task_completion",
        _validator_side_effect,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.mark_task_attempt_failed",
        _NOOP_FN,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.completion_coordinator.mark_session_paused",
        _NOOP_FN,
    )

    result = CompletionCoordinator().complete_task(
        ctx=ctx,
        write_project_state_snapshot_fn=_NOOP_FN,
        save_orchestration_checkpoint_fn=_NOOP_FN,
    )

    assert result["status"] == "failed"
    assert result["reason"] == TerminalReason.VERIFICATION_INTEGRITY_FAILED
    ctx.task_service.promote_change_set_into_baseline.assert_not_called()
    ctx.task_service.auto_publish_task_into_baseline.assert_not_called()


def test_complete_task_writes_report_to_durable_project_root(tmp_path, monkeypatch):
    """Phase 24B-7: the task report must land in the durable project root,
    not in the disposable Task Execution Sandbox the coordinator ran in,
    because the virtual merge gate resolves reports against the project root."""
    durable_root = tmp_path / "durable-project"
    durable_root.mkdir()
    sandbox_dir = tmp_path / "runtime-sandbox"
    sandbox_dir.mkdir()

    ctx = _make_ctx(tmp_path)
    ctx.orchestration_state._project_dir_override = str(sandbox_dir)
    ctx.project.workspace_path = str(durable_root)

    accepted_verdict = _make_validation_verdict(status="accepted", accepted=True)
    _patch_coordinator_delegates(monkeypatch, validation_verdict=accepted_verdict)

    with patch(
        "app.services.human_guidance.post_write_checker.run_post_write_check_if_enabled",
        _NOOP_FN,
    ):
        result = CompletionCoordinator().complete_task(
            ctx=ctx,
            write_project_state_snapshot_fn=_NOOP_FN,
            save_orchestration_checkpoint_fn=_NOOP_FN,
            build_task_report_payload_fn=lambda db, task_id: {
                "task_id": task_id,
                "title": "Test task",
                "status": "done",
                "duration_seconds": 1,
                "structured_state": {},
                "logs": [],
            },
            render_task_report_fn=lambda payload, output_format: {
                "report": "# Task Report: Test task\n",
                "format": output_format,
            },
        )

    assert result["status"] == "completed"
    durable_report = durable_root / ".agent" / "task-reports" / "task_report_1.md"
    sandbox_report = sandbox_dir / ".agent" / "task-reports" / "task_report_1.md"
    assert durable_report.exists()
    assert not sandbox_report.exists()
