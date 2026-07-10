"""Phase 23D-15 provenance-safe OpenClaw scaffold exclusion regressions."""

from pathlib import Path
from types import SimpleNamespace

from app.services.workspace.baseline_promotion_service import BaselinePromotionService
from app.services.workspace.changeset_service import ChangesetService
from app.services.workspace.workspace_paths import is_executor_runtime_scaffold


SCAFFOLD = "# AGENTS.md - Your Workspace\n\nThis folder is home. Treat it that way.\n"


def _service(tmp_path):
    service = ChangesetService(None)
    project_root = tmp_path / "project"
    project_root.mkdir()
    service.get_project_root = lambda _project: project_root
    return service, project_root, SimpleNamespace(id=1), SimpleNamespace(id=2)


def test_generated_agents_scaffold_is_not_captured_or_artifacted(tmp_path):
    service, root, project, task = _service(tmp_path)
    snapshot = root / ".agent" / "auto-snapshots" / "run"
    snapshot.mkdir(parents=True)
    (root / "AGENTS.md").write_text(SCAFFOLD, encoding="utf-8")
    (root / "requested.txt").write_text("task output\n", encoding="utf-8")

    change_set = service.build_task_execution_change_set(
        project,
        task,
        task_execution_id=3,
        snapshot_key="run",
        target_dir=root,
        preserve_project_root_rules=False,
    )

    assert change_set["added_files"] == ["requested.txt"]
    assert is_executor_runtime_scaffold(root / "AGENTS.md")
    service.persist_change_set_artifact(project, change_set, target_root=root)
    assert not (root / ".agent" / "change-sets" / "3" / "files" / "AGENTS.md").exists()


def test_generated_overwrite_preserves_existing_project_agents(tmp_path):
    service, root, project, task = _service(tmp_path)
    snapshot = root / ".agent" / "auto-snapshots" / "run"
    snapshot.mkdir(parents=True)
    (snapshot / "AGENTS.md").write_text("# Project instructions\n", encoding="utf-8")
    (root / "AGENTS.md").write_text(SCAFFOLD, encoding="utf-8")

    change_set = service.build_task_execution_change_set(
        project,
        task,
        task_execution_id=4,
        snapshot_key="run",
        target_dir=root,
        preserve_project_root_rules=False,
    )

    assert change_set["added_files"] == []
    assert change_set["modified_files"] == []
    assert change_set["deleted_files"] == []


def test_legitimate_project_agents_edit_remains_reviewable(tmp_path):
    service, root, project, task = _service(tmp_path)
    snapshot = root / ".agent" / "auto-snapshots" / "run"
    snapshot.mkdir(parents=True)
    (snapshot / "AGENTS.md").write_text("# Project instructions\n", encoding="utf-8")
    (root / "AGENTS.md").write_text(
        "# Project instructions\n\nNew rule.\n", encoding="utf-8"
    )

    change_set = service.build_task_execution_change_set(
        project,
        task,
        task_execution_id=5,
        snapshot_key="run",
        target_dir=root,
        preserve_project_root_rules=False,
    )

    assert change_set["modified_files"] == ["AGENTS.md"]


def test_promotion_defensively_skips_generated_agents_scaffold(tmp_path):
    service, root, project, task = _service(tmp_path)
    artifact = root / ".agent" / "change-sets" / "6" / "files"
    artifact.mkdir(parents=True)
    (artifact / "AGENTS.md").write_text(SCAFFOLD, encoding="utf-8")
    baseline = root / "baseline"
    promoter = BaselinePromotionService(None)
    promoter.get_project_root = lambda _project: root
    promoter.get_project_baseline_dir = lambda _project: baseline

    result = promoter.promote_change_set_into_baseline_unlocked(
        project,
        task,
        {
            "task_execution_id": 6,
            "added_files": ["AGENTS.md"],
            "modified_files": [],
            "deleted_files": [],
        },
    )

    assert result["files_copied"] == 0
    assert not (baseline / "AGENTS.md").exists()
