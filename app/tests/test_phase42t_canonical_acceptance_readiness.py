"""P42-V17-T provider-free canonical acceptance readiness regressions."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.services.orchestration.validation.candidate_checks import (
    validate_candidate_delta,
)
from app.services.workspace.task_sandbox_allocator import (
    allocate_task_sandbox,
    dispose_task_sandbox,
)


ACCEPTANCE_PROMPT = """Implement Inventory Reservation Support.

Independent acceptance contract (required):
- `python -m pytest tests/test_inventory_acceptance.py -q`
"""


def _init_git_project(root: Path, *, tracked_acceptance: bool) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    (root / "tests/test_inventory_acceptance.py").write_text(
        "def test_acceptance_fixture_is_present():\n    assert True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    if tracked_acceptance:
        subprocess.run(
            ["git", "add", "tests/test_inventory_acceptance.py"],
            cwd=root,
            check=True,
        )
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=root, check=True)
    return root


def test_git_sandbox_materializes_tracked_acceptance_at_bound_execution_path(
    tmp_path,
):
    project = _init_git_project(tmp_path / "git-project", tracked_acceptance=True)
    runtime_root = tmp_path / "runtime"

    sandbox = allocate_task_sandbox(
        project,
        project_id=178,
        task_execution_id=901,
        runtime_root=runtime_root,
    )
    try:
        assert sandbox.path == runtime_root / "tasks" / "178" / "901"
        assert sandbox.read_metadata()["task_execution_id"] == 901
        assert (sandbox.path / "tests/test_inventory_acceptance.py").is_file()
        assert not (project / "tests/test_inventory_acceptance.py").is_symlink()
    finally:
        dispose_task_sandbox(sandbox, project_root=project)


def test_git_sandbox_does_not_materialize_untracked_acceptance_file(tmp_path):
    project = _init_git_project(tmp_path / "git-project", tracked_acceptance=False)
    runtime_root = tmp_path / "runtime"

    sandbox = allocate_task_sandbox(
        project,
        project_id=178,
        task_execution_id=902,
        runtime_root=runtime_root,
    )
    try:
        assert (project / "tests/test_inventory_acceptance.py").is_file()
        assert not (sandbox.path / "tests/test_inventory_acceptance.py").exists()
    finally:
        dispose_task_sandbox(sandbox, project_root=project)


def test_non_git_sandbox_copies_preexisting_acceptance_file(tmp_path):
    project = tmp_path / "plain-project"
    (project / "tests").mkdir(parents=True)
    (project / "tests/test_inventory_acceptance.py").write_text(
        "def test_acceptance_fixture_is_present():\n    assert True\n",
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runtime"

    sandbox = allocate_task_sandbox(
        project,
        project_id=178,
        task_execution_id=903,
        runtime_root=runtime_root,
    )
    try:
        assert sandbox.is_git is False
        assert (sandbox.path / "tests/test_inventory_acceptance.py").is_file()
    finally:
        dispose_task_sandbox(sandbox)


def test_required_acceptance_runs_in_candidate_workspace_and_records_identity(
    tmp_path,
):
    candidate = tmp_path / "candidate"
    canonical = tmp_path / "canonical"
    for root, marker in ((candidate, "candidate\n"), (canonical, "canonical\n")):
        (root / "tests").mkdir(parents=True)
        (root / "candidate-only.txt").write_text(marker, encoding="utf-8")
    (candidate / "tests/test_generated.py").write_text(
        "def test_generated():\n    assert True\n", encoding="utf-8"
    )
    (candidate / "tests/test_inventory_acceptance.py").write_text(
        "from pathlib import Path\n\n"
        "def test_candidate_workspace_is_authoritative():\n"
        "    root = Path(__file__).resolve().parents[1]\n"
        "    assert Path.cwd().resolve() == root\n"
        "    assert (root / 'candidate-only.txt').read_text() == 'candidate\\n'\n",
        encoding="utf-8",
    )
    snapshot = tmp_path / "snapshot"
    shutil.copytree(candidate, snapshot)

    run = validate_candidate_delta(
        project_dir=candidate,
        change_set={
            "target_path": str(candidate),
            "snapshot_path": str(snapshot),
            "modified_files": ["candidate-only.txt"],
            "added_files": [],
            "deleted_files": [],
        },
        plan=[
            {
                "step_number": 1,
                "description": "Run generated checks",
                "verification": "python -m pytest tests/test_generated.py -q",
                "expected_files": ["candidate-only.txt"],
            }
        ],
        task_prompt=ACCEPTANCE_PROMPT,
        include_static_checks=False,
        task_acceptance_enabled=True,
        timeout_seconds=30,
    )

    assert run.findings == ()
    assert run.task_acceptance["results"][0]["outcome"] == "passed"
    assert run.task_acceptance["integrity"]["status"] == "verified"
