"""Phase 33D-9R — accepted Plan identity across execution debug retries."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from app.models import Project, Session as SessionModel, Task
from app.services.orchestration.phases.execution_loop import (
    _restore_authority_bound_step,
)
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.state.persistence import (
    load_accepted_path_authority,
    record_validation_verdict,
)
from app.services.orchestration.types import CandidateValidationResult
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    PathAuthorityError,
)


def _seed_task(db) -> tuple[int, int]:
    project = Project(name="Phase 33D-9R", workspace_path="/tmp/phase33d9r")
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name="Phase 33D-9R session",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    db.add(session)
    db.flush()
    task = Task(
        project_id=project.id,
        title="D9R identity reproduction",
        description="Preserve the accepted Plan through a debug retry",
        status="running",
    )
    db.add(task)
    db.flush()
    return int(session.id), int(task.id)


def _semantic_plan() -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Apply one exact semantic replacement",
            "commands": [],
            "verification": "python -c \"print('verify')\"",
            "rollback": None,
            "expected_files": ["app/name.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "app/name.py",
                    "selector": {
                        "schema_version": "source-region/1",
                        "canonical_path": "app/name.py",
                        "expected_source_version": "version",
                        "start_byte": 0,
                        "end_byte": 4,
                        "selected_region_sha256": "a" * 64,
                        "derivation_kind": "exact_region",
                    },
                    "new": "new()",
                }
            ],
        }
    ]


def test_d9r_debug_retry_restores_the_accepted_plan_before_candidate(
    db_session, tmp_path: Path
):
    accepted_plan = _semantic_plan()
    authority = AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(accepted_plan),
        workspace_identity=str(tmp_path.resolve()),
        maximum_scope_digest="b" * 64,
        grants=(),
    )
    session_id, task_id = _seed_task(db_session)
    state = OrchestrationState(
        session_id=str(session_id),
        task_description="Preserve the accepted Plan",
        project_name="phase33d9r",
        task_id=task_id,
        plan=deepcopy(accepted_plan),
    )
    state._project_dir_override = str(tmp_path)
    verdict = CandidateValidationResult(
        stage="plan",
        status="accepted",
        profile="mutation",
        details={"accepted_path_authority": authority.to_dict()},
    )
    record_validation_verdict(
        db_session,
        session_id=session_id,
        task_id=task_id,
        orchestration_state=state,
        verdict=verdict,
    )
    db_session.commit()

    # This is the D9 shape: a command-fix retry replaces the semantic operation
    # with a shell command and changes verification after APA mint.
    mutated_plan = deepcopy(accepted_plan)
    mutated_plan[0]["ops"] = []
    mutated_plan[0]["commands"] = ["sed -i app/name.py"]
    mutated_plan[0]["expected_files"] = []
    mutated_plan[0]["verification"] = "python -m ast_check"
    assert accepted_plan_identity(mutated_plan) != accepted_plan_identity(accepted_plan)

    loaded = load_accepted_path_authority(
        db_session,
        task_id=task_id,
        session_id=session_id,
        task_execution_id=None,
        plan=accepted_plan,
        workspace_identity=str(tmp_path.resolve()),
    )
    assert loaded.authority_identity == authority.authority_identity
    with pytest.raises(PathAuthorityError, match="authority_plan_identity_mismatch"):
        load_accepted_path_authority(
            db_session,
            task_id=task_id,
            session_id=session_id,
            task_execution_id=None,
            plan=mutated_plan,
            workspace_identity=str(tmp_path.resolve()),
        )

    # The retry uses an execution-local step, while Candidate sees the
    # unchanged accepted Plan again.
    assert (
        _restore_authority_bound_step(mutated_plan, accepted_plan, step_index=0) is True
    )
    assert mutated_plan == accepted_plan
    reloaded = load_accepted_path_authority(
        db_session,
        task_id=task_id,
        session_id=session_id,
        task_execution_id=None,
        plan=mutated_plan,
        workspace_identity=str(tmp_path.resolve()),
    )
    assert reloaded.authority_identity == authority.authority_identity
