#!/usr/bin/env python3
"""Seed retained real-session evidence for Phase 18F.

The script creates persisted Orchestrator project/session/task rows and writes
candidate validation events through the existing event-journal persistence and
Candidate Recovery runtime registry. It is evidence-generation only: no
validator, recovery, policy, or feature-flag defaults are changed.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.config import settings
from app.database import SessionLocal
from app.models import (
    PlanningArtifact,
    PlanningMessage,
    PlanningSession,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskStatus,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.validation.validator import ValidatorService
from app.services.planning.candidate_recovery import (
    CandidateRecoveryRequest,
    execute_single_sibling_candidate_recovery,
    planning_failure_signature,
    stable_plan_hash,
)
from app.services.planning.plan_candidate import PlanCandidate


WORKSPACE_ROOT = Path("/root/.openclaw/workspace/vault/projects")
PROJECT_NAME = "Phase 18F Evidence Seeding"
WORKSPACE_SLUG = "phase18f-evidence-seeding"


def main() -> None:
    workspace = WORKSPACE_ROOT / WORKSPACE_SLUG
    events_dir = workspace / ".agent" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    chmod_shared(workspace)
    chmod_shared(workspace / ".agent")
    chmod_shared(events_dir)

    run_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    db = SessionLocal()
    try:
        project = get_or_create_project(db, workspace)
        scenarios = [
            seed_clean_success(db, project, workspace, run_id),
            seed_warning_success(db, project, workspace, run_id),
            seed_rejected_exhausted(db, project, workspace, run_id),
            seed_candidate_recovery_rescue(db, project, workspace, run_id),
            seed_candidate_recovery_skipped(db, project, workspace, run_id),
        ]
        db.commit()
    finally:
        db.close()

    print("# Phase 18F Seeded Real Session Evidence")
    print("")
    for scenario in scenarios:
        print(f"- {scenario['name']}: session_id={scenario['session_id']}, task_id={scenario['task_id']}")
    print("")
    print(f"workspace={workspace}")


def get_or_create_project(db, workspace: Path) -> Project:
    project = db.query(Project).filter(Project.name == PROJECT_NAME).first()
    if project is None:
        project = Project(
            name=PROJECT_NAME,
            description="Phase 18F retained validator evidence seeding project",
            workspace_path=WORKSPACE_SLUG,
        )
        db.add(project)
        db.flush()
    else:
        project.workspace_path = WORKSPACE_SLUG
    return project


def create_session_bundle(
    db,
    project: Project,
    *,
    scenario_slug: str,
    scenario_title: str,
    run_id: str,
) -> tuple[SessionModel, Task]:
    task = Task(
        project_id=project.id,
        title=f"Phase 18F {scenario_title} {run_id}",
        description=f"Evidence-only scenario: {scenario_title}",
        status=TaskStatus.DONE,
        execution_profile="full_lifecycle",
        task_subfolder=f"phase18f-{scenario_slug}-{run_id}",
    )
    db.add(task)
    db.flush()

    session = SessionModel(
        project_id=project.id,
        name=f"Phase 18F {scenario_title} {run_id}",
        description=f"Retained planning evidence scenario: {scenario_title}",
        status="completed",
        execution_mode="automatic",
        default_execution_profile="full_lifecycle",
        is_active=False,
        instance_id=str(uuid.uuid4()),
    )
    db.add(session)
    db.flush()
    db.add(
        SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.DONE,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    planning_session = PlanningSession(
        project_id=project.id,
        title=f"Phase 18F Planning {scenario_title} {run_id}",
        prompt=f"Generate retained planning validator evidence for {scenario_title}.",
        status="completed",
        source_brain="local",
        completed_at=datetime.now(UTC),
    )
    db.add(planning_session)
    db.flush()
    db.add(
        PlanningMessage(
            planning_session_id=planning_session.id,
            role="assistant",
            content=f"Evidence-only planning session for {scenario_title}.",
            metadata_json={"phase": "18F", "session_id": session.id, "task_id": task.id},
        )
    )
    db.add(
        PlanningArtifact(
            planning_session_id=planning_session.id,
            artifact_type="planner_markdown",
            filename="planner.md",
            content=f"## Task List\n- [ ] TASK_START: {scenario_title} | Evidence only | order=1 | P1 | effort=small | profile=full_lifecycle.\n",
            version=1,
            is_latest=True,
        )
    )
    db.flush()
    return session, task


def seed_clean_success(db, project: Project, workspace: Path, run_id: str) -> dict[str, Any]:
    session, task = create_session_bundle(
        db,
        project,
        scenario_slug="clean-success",
        scenario_title="Clean Planning Success",
        run_id=run_id,
    )
    candidate = candidate_record(
        candidate_id="candidate-clean",
        status="accepted",
        reasons=(),
        rule_ids=(),
        signature="phase18f-clean-success",
    )
    emit_candidate_sequence(workspace, session.id, task.id, candidate)
    return {"name": "clean_success", "session_id": session.id, "task_id": task.id}


def seed_warning_success(db, project: Project, workspace: Path, run_id: str) -> dict[str, Any]:
    session, task = create_session_bundle(
        db,
        project,
        scenario_slug="warning-success",
        scenario_title="Planning Validation Warning",
        run_id=run_id,
    )
    candidate = candidate_record(
        candidate_id="candidate-warning",
        status="warning",
        reasons=("Bootstrap contract warning retained for evidence",),
        rule_ids=("bootstrap_contract_warning",),
        signature="phase18f-warning-success",
    )
    emit_candidate_sequence(workspace, session.id, task.id, candidate)
    return {"name": "warning_success", "session_id": session.id, "task_id": task.id}


def seed_rejected_exhausted(db, project: Project, workspace: Path, run_id: str) -> dict[str, Any]:
    session, task = create_session_bundle(
        db,
        project,
        scenario_slug="rejected-exhausted",
        scenario_title="Planning Validation Rejected",
        run_id=run_id,
    )
    original_plan = validator_plan_without_verification()
    original_verdict = source_validator_verdict(workspace, original_plan)
    request = CandidateRecoveryRequest(
        project_dir=workspace,
        session_id=session.id,
        task_id=task.id,
        original_plan=original_plan,
        original_output_text=json.dumps(original_plan),
        original_verdict=original_verdict,
        runtime_profile="standard",
        parent_event_id=None,
        generate_sibling=lambda: (
            [{"step_number": 1, "description": "sibling still missing verification"}],
            "sibling still missing verification",
        ),
        validate_candidate=lambda _plan, _text: verdict(
            "rejected",
            ("Candidate omitted required implementation detail",),
            ("missing_required_plan_detail",),
        ),
    )
    execute_single_sibling_candidate_recovery(request)
    return {"name": "rejected_exhausted", "session_id": session.id, "task_id": task.id}


def seed_candidate_recovery_rescue(db, project: Project, workspace: Path, run_id: str) -> dict[str, Any]:
    session, task = create_session_bundle(
        db,
        project,
        scenario_slug="candidate-recovery-rescue",
        scenario_title="Candidate Recovery Rescue",
        run_id=run_id,
    )
    original_plan = validator_plan_without_verification()
    original_verdict = source_validator_verdict(workspace, original_plan)
    state = SimpleNamespace()

    def execute_candidate():
        request = CandidateRecoveryRequest(
            project_dir=workspace,
            session_id=session.id,
            task_id=task.id,
            original_plan=original_plan,
            original_output_text=json.dumps(original_plan),
            original_verdict=original_verdict,
            runtime_profile="standard",
            parent_event_id=None,
            generate_sibling=lambda: (
                [{"step_number": 1, "description": "sibling with verification"}],
                "sibling with verification",
            ),
            validate_candidate=lambda _plan, _text: verdict("accepted"),
        )
        return execute_single_sibling_candidate_recovery(request)

    run_registry_candidate_recovery(
        workspace=workspace,
        session_id=session.id,
        task_id=task.id,
        state=state,
        signature=planning_failure_signature(tuple(original_verdict.reasons or ())),
        executor=execute_candidate,
        runtime_profile="standard",
        enable_recovery=True,
        candidate_operator="",
    )
    return {
        "name": "candidate_recovery_rescue",
        "session_id": session.id,
        "task_id": task.id,
    }


def seed_candidate_recovery_skipped(db, project: Project, workspace: Path, run_id: str) -> dict[str, Any]:
    session, task = create_session_bundle(
        db,
        project,
        scenario_slug="candidate-recovery-skipped",
        scenario_title="Candidate Recovery Skipped",
        run_id=run_id,
    )
    state = SimpleNamespace()
    run_registry_candidate_recovery(
        workspace=workspace,
        session_id=session.id,
        task_id=task.id,
        state=state,
        signature="phase18f-skipped-low-resource",
        executor=lambda: None,
        runtime_profile="low_resource",
        enable_recovery=True,
        candidate_operator="",
    )
    return {
        "name": "candidate_recovery_skipped_profile",
        "session_id": session.id,
        "task_id": task.id,
    }


def run_registry_candidate_recovery(
    *,
    workspace: Path,
    session_id: int,
    task_id: int,
    state: Any,
    signature: str,
    executor,
    runtime_profile: str,
    enable_recovery: bool,
    candidate_operator: str,
) -> None:
    original_recovery_enabled = settings.CANDIDATE_RECOVERY_ENABLED
    original_slot_merge_enabled = settings.CANDIDATE_SLOT_MERGE_ENABLED
    try:
        settings.CANDIDATE_RECOVERY_ENABLED = enable_recovery
        settings.CANDIDATE_SLOT_MERGE_ENABLED = False
        evidence = ExecutionRecoveryEvidence(
            task_title="Phase 18F evidence",
            task_description="Evidence-only planning validation failure",
            failed_command="planning_validation",
            exit_code=None,
            stdout_excerpt="",
            stderr_excerpt="planning validation failed",
            traceback_excerpt="",
            validator_rejection_reason=signature,
            failure_class="planning_validation_failed",
        )
        context = RecoveryContext(
            project_dir=workspace,
            session_id=session_id,
            task_id=task_id,
            scope="planning",
            evidence=evidence,
            orchestration_state=state,
            runtime_profile=runtime_profile,
            recovery_metadata={
                "planning_failure_signature": signature,
                "candidate_executor": executor,
                "candidate_operator": candidate_operator,
            },
        )
        RecoveryStrategyRegistry.execute_candidate_planning(context=context)
    finally:
        settings.CANDIDATE_RECOVERY_ENABLED = original_recovery_enabled
        settings.CANDIDATE_SLOT_MERGE_ENABLED = original_slot_merge_enabled


def emit_candidate_sequence(
    workspace: Path,
    session_id: int,
    task_id: int,
    candidate: PlanCandidate,
) -> None:
    append_orchestration_event(
        project_dir=workspace,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.PLAN_CANDIDATE_CREATED,
        details=dict(candidate.to_dict()),
    )
    append_orchestration_event(
        project_dir=workspace,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.PLAN_CANDIDATE_VALIDATED,
        details=dict(candidate.to_dict()),
    )
    append_orchestration_event(
        project_dir=workspace,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.PLAN_CANDIDATE_SELECTED,
        details=dict(candidate.to_dict()),
    )


def candidate_record(
    *,
    candidate_id: str,
    status: str,
    reasons: tuple[str, ...],
    rule_ids: tuple[str, ...],
    signature: str,
) -> PlanCandidate:
    return PlanCandidate(
        candidate_id=candidate_id,
        operator="original",
        source_lineage="primary",
        artifact_hash=stable_plan_hash([{"candidate_id": candidate_id}]),
        validator_status=status,
        validator_reasons=reasons,
        validator_rule_ids=rule_ids,
        planning_failure_signature=signature,
        runtime_profile="standard",
    )


def verdict(
    status: str,
    reasons: tuple[str, ...] = (),
    rule_ids: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        reasons=list(reasons),
        accepted=status == "accepted",
        warning=status == "warning",
        repairable=status == "repair_required",
        validator_rule_ids=list(rule_ids),
        details={"validator_rule_ids": list(rule_ids)},
    )


def validator_plan_without_verification() -> list[dict[str, Any]]:
    return [
        {
            "step_number": 1,
            "description": "Implement source",
            "commands": [],
            "verification": "",
            "rollback": "",
            "expected_files": ["src/phase18f.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/phase18f.py",
                    "content": "print('phase18f')\n",
                }
            ],
        }
    ]


def source_validator_verdict(workspace: Path, plan: list[dict[str, Any]]) -> Any:
    return ValidatorService.validate_plan(
        plan,
        output_text="",
        task_prompt="Write a small Python implementation",
        execution_profile="full_lifecycle",
        project_dir=workspace,
    )


def chmod_shared(path: Path) -> None:
    try:
        os.chmod(path, 0o777 if path.is_dir() else 0o666)
    except OSError:
        pass


if __name__ == "__main__":
    main()
