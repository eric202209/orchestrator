from __future__ import annotations

from app.services.workspace.sandbox_contract import (
    SandboxContractError,
    build_docker_workspace_sandbox_plan,
)


def test_docker_sandbox_plan_mounts_task_workspace_only(tmp_path):
    project_root = tmp_path / "project"
    task_workspace = project_root / "task-1"
    task_workspace.mkdir(parents=True)

    plan = build_docker_workspace_sandbox_plan(
        project_root=project_root,
        task_workspace=task_workspace,
        image="python:3.12",
        environment={"OPENCLAW_SANDBOX": "1"},
    )

    assert plan.engine == "docker"
    assert plan.workspace_dir == str(task_workspace.resolve())
    assert plan.mounts[0].source == str(task_workspace.resolve())
    assert plan.mounts[0].target == "/workspace"
    assert plan.mounts[0].mode == "rw"
    assert plan.docker_run_args(["pytest", "-q"]) == [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--mount",
        (f"type=bind,source={task_workspace.resolve()}," "target=/workspace,rw"),
        "--env",
        "OPENCLAW_SANDBOX=1",
        "--workdir",
        "/workspace",
        "python:3.12",
        "pytest",
        "-q",
    ]


def test_docker_sandbox_plan_rejects_nested_or_missing_workspaces(tmp_path):
    project_root = tmp_path / "project"
    nested_workspace = project_root / "group" / "task-1"
    nested_workspace.mkdir(parents=True)

    try:
        build_docker_workspace_sandbox_plan(
            project_root=project_root,
            task_workspace=nested_workspace,
        )
    except SandboxContractError as error:
        assert "direct child" in str(error)
    else:
        raise AssertionError("nested workspace should be rejected")

    try:
        build_docker_workspace_sandbox_plan(
            project_root=project_root,
            task_workspace=project_root / "missing-task",
        )
    except SandboxContractError as error:
        assert "existing directory" in str(error)
    else:
        raise AssertionError("missing workspace should be rejected")
