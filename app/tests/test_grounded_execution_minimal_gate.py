"""Provider-free V1-B seam matrix."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Project, Session as SessionModel, SessionTask, Task, TaskStatus
from app.services.orchestration.grounded_execution import (
    GroundedExecutionError,
    admit_grounded_submission,
    revalidate_grounded_step,
)
from app.services.orchestration.validation import candidate_checks


def _submission(operations, verification=None):
    return {
        "execution_kind": "grounded_external_submission",
        "operations": operations,
        "verification": verification or {"kind": "none"},
    }


def _admit(root: Path, submission):
    return admit_grounded_submission(
        submission,
        project_dir=root,
        project_id=1,
        task_id=2,
        session_id=3,
        task_execution_id=4,
        attempt_number=1,
        session_instance_id="session-instance",
        prompt="grounded task",
        title="grounded task",
        description="grounded task",
    )


def _replace(path: str, quote: str, new: str):
    return {"op": "replace_in_file", "path": path, "quote": quote, "new": new}


def _seed_worker_job(db_session, root: Path, files: dict[str, str]):
    task_root = root / "task-1"
    task_root.mkdir(parents=True)
    for path, content in files.items():
        target = task_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    project = Project(name="grounded", workspace_path=str(root))
    db_session.add(project)
    db_session.commit()
    session = SessionModel(
        project_id=project.id,
        name="grounded-session",
        status="running",
        is_active=True,
        instance_id="grounded-instance",
    )
    db_session.add(session)
    db_session.commit()
    task = Task(
        project_id=project.id,
        title="grounded task",
        description="grounded task",
        status=TaskStatus.PENDING,
        task_subfolder="task-1",
    )
    db_session.add(task)
    db_session.commit()
    link = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.PENDING
    )
    db_session.add(link)
    db_session.commit()
    return project, session, task, link, task_root


def test_exact_replace_admission_reuses_phase33_authority(tmp_path: Path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    envelope, verdict = _admit(
        tmp_path,
        _submission([_replace("app.py", "value = 1", "value = 2\n")]),
    )

    assert verdict.accepted
    assert envelope["accepted_plan_identity"]
    assert envelope["accepted_path_authority"]["grants"]
    assert envelope["normalized_plan"][0]["ops"][0]["op"] == "replace_in_file"
    assert envelope["normalized_plan"][0]["commands"] == []


def test_new_file_admission_is_explicit_and_has_no_synthetic_target(tmp_path: Path):
    envelope, verdict = _admit(
        tmp_path,
        _submission([{"op": "create_file", "path": "new.py", "content": "x = 1\n"}]),
    )

    assert verdict.accepted
    assert envelope["grounding"][0]["kind"] == "new_file_creation"
    assert "target_id" not in envelope["grounding"][0]
    assert envelope["normalized_plan"][0]["ops"][0]["op"] == "write_file"


def test_multi_operation_is_ordered_one_operation_per_step(tmp_path: Path):
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8")
    envelope, _ = _admit(
        tmp_path,
        _submission(
            [
                _replace("a.py", "a = 1", "a = 2\n"),
                _replace("b.py", "b = 1", "b = 2\n"),
            ]
        ),
    )

    assert [len(step["ops"]) for step in envelope["normalized_plan"]] == [1, 1]
    assert [step["step_number"] for step in envelope["normalized_plan"]] == [1, 2]
    assert len(envelope["step_identities"]) == 2


@pytest.mark.parametrize(
    ("name", "source", "submission", "family"),
    [
        (
            "stale",
            "value = changed\n",
            _submission([_replace("app.py", "value = 1", "value = 2\n")]),
            "STALE_GROUNDING",
        ),
        (
            "ambiguous",
            "value = 1\nvalue = 1\n",
            _submission([_replace("app.py", "value = 1", "value = 2\n")]),
            "AMBIGUOUS_GROUNDING",
        ),
    ],
)
def test_exact_grounding_fails_closed_before_mutation(
    tmp_path: Path, name: str, source: str, submission, family: str
):
    del name
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    with pytest.raises(GroundedExecutionError) as error:
        _admit(tmp_path, submission)
    assert error.value.family == family
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == source


def test_out_of_scope_and_new_file_collision_fail_closed(tmp_path: Path):
    (tmp_path / "existing.py").write_text("x\n", encoding="utf-8")
    with pytest.raises(GroundedExecutionError) as out_of_scope:
        _admit(
            tmp_path,
            _submission([{"op": "create_file", "path": "../x", "content": "x"}]),
        )
    with pytest.raises(GroundedExecutionError) as collision:
        _admit(
            tmp_path,
            _submission([{"op": "create_file", "path": "existing.py", "content": "x"}]),
        )
    assert out_of_scope.value.family == "OUT_OF_SCOPE"
    assert collision.value.family == "CONFLICT"


def test_raw_command_is_rejected(tmp_path: Path):
    with pytest.raises(GroundedExecutionError) as error:
        _admit(
            tmp_path,
            _submission(
                [{"op": "create_file", "path": "new.py", "content": "x\n"}],
                {"kind": "raw_command", "command": "pytest"},
            ),
        )
    assert error.value.family == "COMMAND_REJECTED"


def test_source_revalidation_rejects_drift_without_replay(tmp_path: Path):
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8")
    envelope, _ = _admit(
        tmp_path,
        _submission(
            [
                _replace("a.py", "a = 1", "a = 2\n"),
                _replace("b.py", "b = 1", "b = 2\n"),
            ]
        ),
    )
    (tmp_path / "a.py").write_text("a = 2\n", encoding="utf-8")
    with pytest.raises(GroundedExecutionError) as error:
        revalidate_grounded_step(envelope, project_dir=tmp_path, step_index=0)
    assert error.value.family == "STALE_GROUNDING"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 2\n"


def test_focused_tests_are_bounded_and_use_shell_false(tmp_path: Path, monkeypatch):
    test_path = tmp_path / "tests" / "test_one.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_one(): pass\n", encoding="utf-8")
    calls = []

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return Completed()

    monkeypatch.setattr(candidate_checks.subprocess, "run", fake_run)
    result = candidate_checks.validate_candidate_delta(
        project_dir=tmp_path,
        change_set={"added_files": [], "modified_files": [], "deleted_files": []},
        plan=[],
        task_prompt="test",
        include_static_checks=False,
        verification_scope=("tests/test_one.py",),
        run_focused_tests=True,
    )

    assert result.findings == ()
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["cwd"] == tmp_path


def test_derived_static_scope_is_limited_to_authorized_plan_paths(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "allowed.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("x = 1\n", encoding="utf-8")
    captured = {}

    def fake_validate(**kwargs):
        captured.update(kwargs)

        class Checks:
            findings = ()
            commands_run = ()

        return Checks()

    monkeypatch.setattr(
        "app.services.orchestration.grounded_execution.validate_candidate_delta",
        fake_validate,
    )
    from app.services.orchestration.grounded_execution import run_grounded_verification

    run_grounded_verification(
        project_dir=tmp_path,
        change_set={
            "added_files": [],
            "modified_files": ["allowed.py", "unrelated.py"],
            "deleted_files": [],
        },
        plan=[{"expected_files": ["allowed.py"], "ops": []}],
        task_prompt="compile",
        policy={"kind": "derived_compile_static", "paths": []},
    )
    assert captured["observed_scope"] == ("allowed.py",)
    assert captured["include_static_checks"] is True


def test_worker_grounded_lane_skips_provider_planning_and_completes(
    db_session, isolated_workspace_root, monkeypatch
):
    import app.tasks.worker as worker

    project_root = isolated_workspace_root / "grounded-project"
    project, session, task, link, task_root = _seed_worker_job(
        db_session, project_root, {"app.py": "value = 1\n"}
    )
    baseline = project_root / "baseline.txt"
    baseline.write_text("unchanged\n", encoding="utf-8")
    del link
    monkeypatch.setattr(worker, "get_db_session", lambda: db_session)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider/planning path reached grounded lane")

    monkeypatch.setattr(worker, "resolve_runtime_configuration", forbidden)
    monkeypatch.setattr(worker, "create_agent_runtime", forbidden)
    monkeypatch.setattr(worker, "_execute_planning_phase", forbidden)
    monkeypatch.setattr(worker._CompletionCoordinator, "complete_task", forbidden)

    result = worker.execute_orchestration_task.apply(
        kwargs={
            "session_id": session.id,
            "task_id": task.id,
            "prompt": "replace",
            "grounded_submission": _submission(
                [_replace("app.py", "value = 1", "value = 2\n")]
            ),
        }
    ).get(propagate=False)

    assert result["public_state"] == "SUCCEEDED"
    assert result["publication_status"] == "not_published"
    assert (task_root / "app.py").read_text(encoding="utf-8") == "value = 2\n\n"
    assert baseline.read_text(encoding="utf-8") == "unchanged\n"


def test_worker_partial_work_is_terminal_and_does_not_replay(
    db_session, isolated_workspace_root, monkeypatch
):
    import app.services.orchestration.phases.execution_loop as execution_loop
    import app.tasks.worker as worker

    project, session, task, _link, task_root = _seed_worker_job(
        db_session,
        isolated_workspace_root / "partial-project",
        {"a.py": "a = 1\n", "b.py": "b = 1\n"},
    )
    session_id, task_id = session.id, task.id
    monkeypatch.setattr(worker, "get_db_session", lambda: db_session)
    original = execution_loop.ExecutorService.execute_file_ops
    calls = {"count": 0}

    def drift_after_first(*args, **kwargs):
        result = original(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            (task_root / "b.py").write_text("b = drift\n", encoding="utf-8")
        return result

    monkeypatch.setattr(
        execution_loop.ExecutorService, "execute_file_ops", drift_after_first
    )
    result = worker.execute_orchestration_task.apply(
        kwargs={
            "session_id": session_id,
            "task_id": task_id,
            "prompt": "multi",
            "grounded_submission": _submission(
                [
                    _replace("a.py", "a = 1", "a = 2\n"),
                    _replace("b.py", "b = 1", "b = 2\n"),
                ]
            ),
        }
    ).get(propagate=False)

    assert result["status"] == "failed"
    assert result["failure"]["family"] == "STALE_GROUNDING"
    assert result["partial_work"] is True
    assert calls["count"] == 1
    assert (task_root / "a.py").read_text(encoding="utf-8") == "a = 2\n\n"
    assert (task_root / "b.py").read_text(encoding="utf-8") == "b = drift\n"


def test_candidate_failure_does_not_touch_canonical_baseline(
    db_session, isolated_workspace_root, monkeypatch
):
    import app.tasks.worker as worker

    project, session, task, _link, task_root = _seed_worker_job(
        db_session,
        isolated_workspace_root / "candidate-failure-project",
        {"tests/test_failure.py": "def test_failure(): assert False\n"},
    )
    baseline = project.workspace_path
    baseline_file = Path(baseline) / "baseline.txt"
    baseline_file.write_text("unchanged\n", encoding="utf-8")
    monkeypatch.setattr(worker, "get_db_session", lambda: db_session)
    result = worker.execute_orchestration_task.apply(
        kwargs={
            "session_id": session.id,
            "task_id": task.id,
            "prompt": "run focused failure test",
            "grounded_submission": _submission(
                [{"op": "create_file", "path": "new.py", "content": "x = 1\n"}],
                {"kind": "focused_tests", "paths": ["tests/test_failure.py"]},
            ),
        }
    ).get(propagate=False)

    assert result["status"] == "failed"
    assert result["failure"]["family"] == "VALIDATION_FAILED"
    assert result["publication_status"] == "not_published"
    assert baseline_file.read_text(encoding="utf-8") == "unchanged\n"
    assert (task_root / "new.py").read_text(encoding="utf-8") == "x = 1\n"


def test_worker_pause_resume_uses_checkpoint_and_new_execution_attempt(
    db_session, isolated_workspace_root, monkeypatch
):
    import app.services.orchestration.phases.execution_loop as execution_loop
    import app.tasks.worker as worker
    from app.models import TaskExecution

    project, session, task, link, task_root = _seed_worker_job(
        db_session,
        isolated_workspace_root / "resume-project",
        {"a.py": "a = 1\n", "b.py": "b = 1\n"},
    )
    session_id, task_id, link_id = session.id, task.id, link.id
    monkeypatch.setattr(worker, "get_db_session", lambda: db_session)
    original = execution_loop.ExecutorService.execute_file_ops
    calls = {"count": 0}

    def pause_after_first(*args, **kwargs):
        result = original(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            session.status = "paused"
            db_session.commit()
        return result

    monkeypatch.setattr(
        execution_loop.ExecutorService, "execute_file_ops", pause_after_first
    )
    first = worker.execute_orchestration_task.apply(
        kwargs={
            "session_id": session.id,
            "task_id": task.id,
            "prompt": "multi",
            "grounded_submission": _submission(
                [
                    _replace("a.py", "a = 1", "a = 2\n"),
                    _replace("b.py", "b = 1", "b = 2\n"),
                ]
            ),
        }
    ).get(propagate=False)
    assert first["status"] == "cancelled"
    assert calls["count"] == 1

    session = db_session.get(SessionModel, session_id)
    task = db_session.get(Task, task_id)
    link = db_session.get(SessionTask, link_id)
    session.status = "running"
    session.is_active = True
    task.status = TaskStatus.PENDING
    task.error_message = None
    link.status = TaskStatus.PENDING
    db_session.commit()
    second = worker.execute_orchestration_task.apply(
        kwargs={
            "session_id": session_id,
            "task_id": task_id,
            "prompt": "multi",
            "resume_checkpoint_name": "autosave_latest",
        }
    ).get(propagate=False)

    assert second["public_state"] == "SUCCEEDED"
    assert calls["count"] == 2
    assert (task_root / "a.py").read_text(encoding="utf-8") == "a = 2\n\n"
    assert (task_root / "b.py").read_text(encoding="utf-8") == "b = 2\n\n"
    executions = (
        db_session.query(TaskExecution)
        .filter(TaskExecution.task_id == task_id)
        .order_by(TaskExecution.id)
        .all()
    )
    assert len(executions) == 2
    assert executions[0].planning_session_id is None
    assert executions[1].planning_session_id is None
    assert second["task_execution_id"] == executions[1].id
