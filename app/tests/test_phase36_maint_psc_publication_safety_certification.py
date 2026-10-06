"""PHASE36-MAINT PSC — publication safety certification & manual release boundary.

The FPS repair is certified through the real ``execute_orchestration_task``
lifecycle: real worker try/except/finally, real CompletionCoordinator and
FailureCoordinator, real ChangeSet capture and real accept routes on an
isolated in-memory DB and temporary ProductRoot.  Only the provider-shaped
seams are deterministic stubs (accepted Planning result, one executed step,
evaluator output, validator verdicts).

Phase 25C-2 unified ``/change-set/accept`` (review acceptance) with
``/tasks/{id}/accept`` on one promotion authority; neither route nor any test
or recovery flow relies on releasing a failed run.  ``/change-set/accept`` had
no lifecycle gate, so a Candidate whose baseline-publish preflight rejected it
(Task/TaskExecution FAILED) was still published.  Both routes now require the
owning TaskExecution to have completed.  No provider is called.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1.endpoints.tasks import CHANGE_SET_EXECUTION_NOT_COMPLETED
from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
)
from app.services.orchestration.types import ValidationVerdict
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
)
from app.services.orchestration.validation.candidate_checks import (
    candidate_delta_identity,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    GrantClass,
    GrantProvenance,
    PathGrant,
    declare,
)
from app.tests.test_phase36_maint_fps_failure_path_publication_state import (
    _record,
    _seed,
)
from app.tests.test_project_baseline_regressions import _seed_publication_evidence

TARGET = "app.py"
BEFORE, AFTER = "VALUE = 1\n", "VALUE = 2\n"
PLAN = [
    {
        "step_number": 1,
        "description": "Write the candidate",
        "commands": ["true"],
        "verification": "true",
        "rollback": None,
        "expected_files": [TARGET],
        "ops": [{"op": "write_file", "path": TARGET, "content": AFTER}],
    }
]
EVALUATOR_PASS = {
    "output": "SCORES: goal=3/3 regressions=2/2 quality=2/2 files=3/3\n"
    "TOTAL: 10/10\nVERDICT: PASS\nNOTES: complete"
}


class _Runtime:
    """Deterministic runtime: summary and evaluator text, no provider."""

    def __init__(self, evaluator_output):
        self.evaluator_output = evaluator_output

    def get_backend_metadata(self):
        return {"backend": "local_openclaw", "model": "stub", "model_family": "stub"}

    async def get_session_context(self):
        return {}

    async def execute_task(self, prompt, timeout_seconds=None, **kwargs):
        if "independent QA evaluator" in prompt:
            return self.evaluator_output
        return {"output": "Task summary"}

    async def invoke_prompt(self, *args, **kwargs):
        raise AssertionError("provider/model invocation is forbidden")


def _run_worker(
    db,
    tmp_path,
    monkeypatch,
    *,
    planning="accepted",
    evaluator=None,
    interrupt=False,
    preflight="accepted",
):
    """Drive the real worker entry point end to end on isolated state."""

    import app.services.orchestration.phases.completion_flow as completion_flow
    import app.tasks.worker as worker
    from app.services.agents.agent_runtime import BackendRole
    from app.services.agents.runtime_configuration import RoleRuntimeConfiguration
    from app.services.orchestration.execution.runtime import workspace_snapshot_key
    from app.services.orchestration.prompt_templates import StepResult
    from app.services.orchestration.validation.validator import ValidatorService
    from app.services.tasks.service import TaskService

    root = tmp_path / "psc-product"
    root.mkdir(parents=True)
    (root / TARGET).write_text(BEFORE, encoding="utf-8")
    project = Project(name="psc-product", workspace_path=str(root))
    db.add(project)
    db.flush()
    session = SessionModel(project_id=project.id, name="psc", status="pending")
    task = Task(
        project_id=project.id,
        title="Update value",
        description="Set VALUE to 2 in app.py",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    db.add_all([session, task])
    db.commit()
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.PENDING,
    )
    db.add(execution)
    db.commit()
    ids = SimpleNamespace(task=task.id, te=execution.id, root=root)
    config = {
        role: RoleRuntimeConfiguration(
            role=role,
            backend_name="local_openclaw",
            model_family="stub",
            adaptation_profile="openclaw_default",
        )
        for role in (BackendRole.PLANNING, BackendRole.EXECUTION)
    }
    runtime = _Runtime(evaluator if evaluator is not None else {"output": ""})

    def planning_phase(*, ctx, **kwargs):
        if planning != "accepted":
            reason = "POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE: psc stub"
            ctx.orchestration_state.abort_reason = reason
            return {"status": "failed", "reason": reason}
        ctx.orchestration_state.plan = [dict(step) for step in PLAN]
        owner = ctx.db.query(Task).filter(Task.id == ctx.task_id).one()
        owner.steps = json.dumps(PLAN)
        authority = AcceptedPathAuthority.create(
            accepted_plan_identity=accepted_plan_identity(PLAN),
            workspace_identity=str(Path(ctx.orchestration_state.project_dir).resolve()),
            maximum_scope_digest="0" * 64,
            grants=[
                PathGrant(
                    path=declare(TARGET),
                    grant_class=GrantClass.EXISTING_MUTABLE,
                    provenance=GrantProvenance.ACCEPTED_PLAN,
                    baseline_content_hash="0" * 64,
                )
            ],
        )
        ctx.db.add(
            TaskCheckpoint(
                task_id=ctx.task_id,
                session_id=ctx.session_id,
                checkpoint_type="validation_plan",
                state_snapshot=json.dumps(
                    {
                        "stage": "plan",
                        "status": "accepted",
                        "details": {"accepted_path_authority": authority.to_dict()},
                    }
                ),
            )
        )
        ctx.db.commit()
        return {"status": "completed"}

    def step_loop(*, ctx, **kwargs):
        workspace = Path(ctx.orchestration_state.project_dir)
        (workspace / TARGET).write_text(AFTER, encoding="utf-8")
        ctx.orchestration_state.record_success(
            StepResult(step_number=1, status="success", files_changed=[TARGET])
        )
        return {"status": "completed"}

    def completion_verdict(**kwargs):
        change_set = (kwargs.get("completion_evidence") or {}).get("change_set")
        return ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="mutation",
            reasons=[],
            details={"expected_core_files": [TARGET]},
            candidate_identity=candidate_delta_identity(
                change_set or {}, project_dir=Path(kwargs["project_dir"])
            ),
        )

    def publish_verdict(**kwargs):
        return ValidationVerdict(
            stage="baseline_publish",
            status=preflight,
            profile="mutation",
            reasons=[] if preflight == "accepted" else ["psc_preflight_rejected"],
            details={},
        )

    for name, value in {
        "get_db_session": lambda: db,
        "resolve_runtime_configuration": lambda db, role: config[role],
        "create_agent_runtime": lambda *args, **kwargs: runtime,
        "_claim_queued_task_for_worker": lambda **kwargs: (True, "claimed", None, None),
        "_runtime_selection_details": lambda db, **kwargs: {},
        "_build_claimed_details": lambda **kwargs: {},
        "_run_start_runtime_identity": lambda *args: {"config": {}},
        "register_forced_termination_cleanup": lambda callback: lambda: None,
        "start_langfuse_observation": lambda **kwargs: nullcontext(None),
        "update_langfuse_observation": lambda *args, **kwargs: None,
        "flush_langfuse": lambda: None,
        "langfuse_tracing_enabled": lambda: False,
        "validate_runtime_provider_contract": lambda *args, **kwargs: {},
        "_execute_planning_phase": planning_phase,
        "_execute_step_loop": step_loop,
    }.items():
        monkeypatch.setattr(worker, name, value)
    monkeypatch.setattr(
        completion_flow,
        "get_effective_workspace_review_policy",
        lambda default_policy, db=None: "auto_publish_all",
    )
    monkeypatch.setattr(
        ValidatorService, "validate_task_completion", staticmethod(completion_verdict)
    )
    monkeypatch.setattr(
        ValidatorService, "validate_baseline_publish", staticmethod(publish_verdict)
    )
    if interrupt:

        def interrupted(**kwargs):
            # Fails after the EVPS pending projection was persisted and
            # outside _run_evaluator's own exception guard.
            raise RuntimeError("psc_interruption_after_evaluator_pending")

        monkeypatch.setattr(completion_flow, "_run_evaluator", interrupted)

    TaskService(db).create_workspace_snapshot(
        project,
        root,
        snapshot_key=workspace_snapshot_key(task.id, execution.id),
        preserve_project_root_rules=True,
    )
    try:
        result = worker.execute_orchestration_task.run(
            session_id=session.id,
            task_id=task.id,
            prompt="Set VALUE to 2 in app.py",
            timeout_seconds=60,
            task_execution_id=execution.id,
        )
    except Exception as exc:  # the worker re-raises after its finally
        result = {"status": "raised", "error": str(exc)}
    return result, ids


def _state(db, ids):
    db.expire_all()
    record = (
        db.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == ids.te)
        .one()
    )
    return SimpleNamespace(
        task=db.get(Task, ids.task).status.value,
        te=db.get(TaskExecution, ids.te).status.value,
        record=record,
        decision=dict(record.review_decision or {}),
        root=(ids.root / TARGET).read_text(encoding="utf-8"),
    )


def _accept_both(client, ids):
    body = {"task_execution_id": ids.te}
    change_set = client.post(f"/api/v1/tasks/{ids.task}/change-set/accept", json=body)
    task = client.post(f"/api/v1/tasks/{ids.task}/accept", json=body)
    return change_set, task


def _without_fps_preservation(monkeypatch):
    from app.services.tasks.service import TaskService

    original = TaskService.persist_task_execution_change_set

    def no_preserve(self, *args, **kwargs):
        kwargs["preserve_review_decision"] = False
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TaskService, "persist_task_execution_change_set", no_preserve)


# --- C1/C7: real worker evaluator hold survives terminal re-capture ---------


def test_c1_c7_real_worker_evaluator_hold_survives_terminal_recapture(
    db_session, db_session_factory, tmp_path, monkeypatch
):
    result, ids = _run_worker(db_session, tmp_path, monkeypatch)
    assert result["status"] == "completed"

    state = _state(db_session, ids)
    assert (state.task, state.te) == ("done", "done")
    assert state.record.modified_files == [TARGET]
    assert state.decision["outcome"] == "hold_for_review"
    assert state.decision["reason"] == "evaluator_assessment_unavailable"
    assert state.decision["evaluator_verdict"] == "UNKNOWN"
    assert state.decision["evaluator_pending"] is False
    assert state.decision["publication_eligible"] is False
    assert state.record.disposition == "captured" and state.root == BEFORE

    # C7: a fresh session (restart/reload) sees the same durable hold.
    fresh = db_session_factory()
    try:
        assert _state(fresh, ids).decision == state.decision
    finally:
        fresh.close()


def test_c1_control_terminal_recapture_runs_in_the_real_worker(
    db_session, tmp_path, monkeypatch
):
    # Disabling only FPS preservation regresses the stored projection, which
    # proves the worker's terminal finally re-capture executed.
    _without_fps_preservation(monkeypatch)
    _run_worker(db_session, tmp_path, monkeypatch)
    decision = _state(db_session, SimpleNamespace(**_last_ids(db_session))).decision
    assert decision["outcome"] == "auto_promote"
    assert decision.get("evaluator_verdict") is None


def _last_ids(db):
    record = db.query(TaskExecutionChangeSet).one()
    project_root = Path(db.get(Project, record.project_id).workspace_path)
    return {
        "task": record.task_id,
        "te": record.task_execution_id,
        "root": project_root,
    }


# --- C2/C5: interrupted evaluator never becomes publishable ----------------


def test_c2_c5_real_worker_pending_interruption_is_never_published(
    authenticated_client, db_session, tmp_path, monkeypatch
):
    result, ids = _run_worker(db_session, tmp_path, monkeypatch, interrupt=True)
    assert result["status"] == "raised"

    state = _state(db_session, ids)
    # The retry path restores the pre-run workspace before the terminal
    # re-capture, so the re-captured Candidate differs (C5): the old
    # projection is not inherited and the execution is not completed.
    assert state.record.modified_files == []
    assert state.decision["outcome"] == "execution_not_completed"
    assert state.decision["publication_eligible"] is False
    change_set, task = _accept_both(authenticated_client, ids)
    assert change_set.status_code == 409 and task.status_code == 409
    assert CHANGE_SET_EXECUTION_NOT_COMPLETED in change_set.json()["detail"]
    assert _state(db_session, ids).root == BEFORE


# --- C3: real worker Planning failure ---------------------------------------


def test_c3_real_worker_planning_failure_is_not_publishable(
    authenticated_client, db_session, tmp_path, monkeypatch
):
    _, ids = _run_worker(db_session, tmp_path, monkeypatch, planning="failed")

    state = _state(db_session, ids)
    assert state.decision["outcome"] == "execution_not_completed"
    assert state.decision["publication_eligible"] is False
    assert state.decision["configured_outcome"] == "auto_promote"
    change_set, task = _accept_both(authenticated_client, ids)
    assert change_set.status_code == 409 and task.status_code == 409
    assert _state(db_session, ids).root == BEFORE


# --- C4: successful governed completion still publishes ---------------------


def test_c4_real_worker_governed_success_publishes(
    authenticated_client, db_session, tmp_path, monkeypatch
):
    result, ids = _run_worker(
        db_session, tmp_path, monkeypatch, evaluator=EVALUATOR_PASS
    )
    assert result["status"] == "completed"

    state = _state(db_session, ids)
    assert (state.task, state.te) == ("done", "done")
    assert state.decision["evaluator_verdict"] == "PASS"
    assert state.decision["publication_eligible"] is True
    assert state.record.disposition == "promoted" and state.root == AFTER


def test_c4_completed_held_candidate_remains_operator_releasable(
    authenticated_client, db_session, tmp_path, monkeypatch
):
    # EVPS: /change-set/accept is the explicit human release of a completed
    # run's held Candidate; evaluator UNKNOWN does not forbid it.
    _, ids = _run_worker(db_session, tmp_path, monkeypatch)
    response = authenticated_client.post(
        f"/api/v1/tasks/{ids.task}/change-set/accept",
        json={"task_execution_id": ids.te},
    )
    assert response.status_code == 200
    assert _state(db_session, ids).root == AFTER


# --- PSC defect: preflight-rejected Candidate of a failed run ---------------


def test_preflight_rejected_failed_run_candidate_cannot_be_released(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    result, ids = _run_worker(
        db_session,
        tmp_path,
        monkeypatch,
        evaluator=EVALUATOR_PASS,
        preflight="rejected",
    )
    assert result == {
        "status": "failed",
        "reason": "baseline_publish_validation_failed",
    }

    state = _state(db_session, ids)
    assert (state.task, state.te) == ("failed", "failed")
    assert state.record.modified_files == [TARGET]  # validated Candidate kept
    assert state.decision["evaluator_verdict"] == "PASS"
    assert state.decision["outcome"] == "execution_not_completed"
    change_set, task = _accept_both(authenticated_client, ids)
    assert change_set.status_code == 409 and task.status_code == 409
    assert CHANGE_SET_EXECUTION_NOT_COMPLETED in change_set.json()["detail"]
    fresh = db_session_factory()
    try:
        reloaded = _state(fresh, ids)
        assert reloaded.record.disposition == "captured" and reloaded.root == BEFORE
    finally:
        fresh.close()


# --- C6/C8: manual-release matrix on both routes ----------------------------

D, F, P = TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.PENDING
FINAL_PASS = {
    "outcome": "auto_promote",
    "held_for_review": False,
    "publication_eligible": True,
    "evaluator_pending": False,
    "evaluator_verdict": "PASS",
}
HELD = {
    "outcome": "hold_for_review",
    "held_for_review": True,
    "publication_eligible": False,
    "evaluator_pending": False,
    "evaluator_verdict": "UNKNOWN",
}
PENDING = {**HELD, "evaluator_pending": True, "evaluator_verdict": None}
NOT_COMPLETED = CHANGE_SET_EXECUTION_NOT_COMPLETED
EVALUATOR_PENDING = "Evaluator assessment is still pending"
TASK_NOT_DONE = "Only completed tasks can be accepted"


@pytest.mark.parametrize(
    (
        "case",
        "task_status",
        "te_status",
        "decision",
        "change_set_result",
        "task_result",
    ),
    [
        ("MR1_completed_valid", D, D, FINAL_PASS, 200, 200),
        ("MR2_failed_evaluator_pass", F, F, FINAL_PASS, NOT_COMPLETED, TASK_NOT_DONE),
        ("MR3_failed_evaluator_nonpass", F, F, HELD, NOT_COMPLETED, TASK_NOT_DONE),
        (
            "MR4_failed_evaluator_pending",
            F,
            F,
            PENDING,
            EVALUATOR_PENDING,
            TASK_NOT_DONE,
        ),
        ("MR5_failed_no_evaluator", F, F, {}, NOT_COMPLETED, TASK_NOT_DONE),
        (
            "MR6_retry_pending_session_paused",
            P,
            F,
            FINAL_PASS,
            NOT_COMPLETED,
            TASK_NOT_DONE,
        ),
        (
            "MR7_task_done_execution_failed",
            D,
            F,
            FINAL_PASS,
            NOT_COMPLETED,
            NOT_COMPLETED,
        ),
        (
            "MR8_pass_before_final_review",
            F,
            F,
            {**PENDING, "evaluator_verdict": "PASS"},
            EVALUATOR_PENDING,
            TASK_NOT_DONE,
        ),
        (
            "MR9_failed_after_review_accepted",
            F,
            F,
            FINAL_PASS,
            NOT_COMPLETED,
            TASK_NOT_DONE,
        ),
        (
            "MR10_failed_bookkeeping_after_validation",
            F,
            F,
            {},
            NOT_COMPLETED,
            TASK_NOT_DONE,
        ),
    ],
)
def test_c6_c8_manual_release_matrix(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    case,
    task_status,
    te_status,
    decision,
    change_set_result,
    task_result,
):
    project, task, session, execution, service, root, key = _seed(
        db_session, tmp_path, status=D
    )
    service.persist_task_execution_change_set(
        project,
        task,
        session_id=session.id,
        task_execution_id=execution.id,
        snapshot_key=key,
        target_dir=root,
        status="done",
    )
    _seed_publication_evidence(
        db_session,
        project,
        task,
        execution,
        service.get_task_execution_change_set(task_execution_id=execution.id),
    )
    record = _record(db_session, execution.id)
    record.review_decision = {**record.review_decision, **decision}
    task.status = task_status
    task.workspace_status = "ready" if task_status == D else "blocked"
    execution.status = te_status
    if case.startswith("MR6"):
        db_session.get(SessionModel, session.id).status = "paused"
    db_session.commit()
    (root / "app.py").unlink()

    # C6: each API request opens its own DB session (conftest ``api_app``), so
    # every accept decision is made from persisted state only (reload).
    ids = SimpleNamespace(task=task.id, te=execution.id)
    change_set, task_accept = _accept_both(authenticated_client, ids)
    for response, expected in (
        (change_set, change_set_result),
        (task_accept, task_result),
    ):
        if expected == 200:
            assert response.status_code == 200, (case, response.json())
        else:
            assert response.status_code == 409, (case, response.json())
            assert expected in response.json()["detail"], case
    assert (root / "app.py").exists() is (change_set_result == 200), case


# --- C9/C10: no identity bypass; persisted eligibility grants nothing -------


@pytest.mark.parametrize("evidence", ["no_plan", "unvalidated", "rejected"])
def test_c9_c10_completed_execution_still_requires_publication_authority(
    authenticated_client, db_session, tmp_path, evidence
):
    project, task, session, execution, service, root, key = _seed(
        db_session, tmp_path, status=D
    )
    service.persist_task_execution_change_set(
        project,
        task,
        session_id=session.id,
        task_execution_id=execution.id,
        snapshot_key=key,
        target_dir=root,
        status="done",
    )
    if evidence != "no_plan":
        _seed_publication_evidence(
            db_session,
            project,
            task,
            execution,
            service.get_task_execution_change_set(task_execution_id=execution.id),
        )
        checkpoint = (
            db_session.query(TaskCheckpoint)
            .filter(TaskCheckpoint.checkpoint_type == "validation_task_completion")
            .one()
        )
        if evidence == "unvalidated":
            db_session.delete(checkpoint)
        else:
            checkpoint.state_snapshot = checkpoint.state_snapshot.replace(
                '"accepted"', '"rejected"'
            )
        db_session.commit()
    # C10: a persisted eligible/auto_promote projection grants nothing.
    assert _record(db_session, execution.id).review_decision["publication_eligible"]
    (root / "app.py").unlink()

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/change-set/accept",
        json={"task_execution_id": execution.id},
    )
    assert response.status_code == 409
    expected = (
        "Task has no accepted executable Plan"
        if evidence == "no_plan"
        else "publication_candidate_identity_unvalidated"
    )
    assert expected in response.json()["detail"]
    assert not (root / "app.py").exists()
    assert _record(db_session, execution.id).disposition == "captured"
