"""Phase 10T: planning_flow.py decomposition and ProjectIndex tests."""

from __future__ import annotations

import json
from pathlib import Path


# ---------------------------------------------------------------------------
# D1 — planning_verification exports
# ---------------------------------------------------------------------------


def test_planning_verification_exports_all_helpers():
    from app.services.orchestration.phases import planning_verification

    for name in (
        "_python_exists_verification_command",
        "_python_file_contains_verification_command",
        "_grep_quiet_verification_target",
        "_commands_are_weak_expected_file_verification",
        "_strengthen_weak_expected_file_verifications",
    ):
        assert hasattr(planning_verification, name), f"missing: {name}"


def test_python_exists_verification_command_encodes_paths():
    from app.services.orchestration.phases.planning_verification import (
        _python_exists_verification_command,
    )

    cmd = _python_exists_verification_command(["a.py", "b.py"])
    assert cmd.startswith("python -c ")
    assert "a.py" in cmd
    assert "b.py" in cmd
    assert "sys.exit" in cmd


def test_python_file_contains_verification_command_encodes_needle():
    from app.services.orchestration.phases.planning_verification import (
        _python_file_contains_verification_command,
    )

    cmd = _python_file_contains_verification_command("out.txt", "hello world")
    assert cmd.startswith("python -c ")
    assert "out.txt" in cmd
    assert "hello world" in cmd


def test_grep_quiet_verification_target_parses_command():
    from app.services.orchestration.phases.planning_verification import (
        _grep_quiet_verification_target,
    )

    result = _grep_quiet_verification_target("grep -q PATTERN ./some/file.txt")
    assert result is not None
    path, needle = result
    assert needle == "PATTERN"
    assert path == "some/file.txt"


def test_grep_quiet_verification_target_returns_none_for_bad_input():
    from app.services.orchestration.phases.planning_verification import (
        _grep_quiet_verification_target,
    )

    assert _grep_quiet_verification_target("ls -la") is None
    assert _grep_quiet_verification_target("") is None


def test_commands_are_weak_returns_false_for_empty():
    from app.services.orchestration.phases.planning_verification import (
        _commands_are_weak_expected_file_verification,
    )

    assert _commands_are_weak_expected_file_verification([]) is False
    assert _commands_are_weak_expected_file_verification("not a list") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# D2 — planning_knowledge exports
# ---------------------------------------------------------------------------


def test_planning_knowledge_exports_retrieve_and_log():
    from app.services.orchestration.phases import planning_knowledge

    for name in ("_retrieve_knowledge", "_log_knowledge_usage"):
        assert hasattr(planning_knowledge, name), f"missing: {name}"


def test_looks_like_verification_only_task_positive():
    from app.services.orchestration.phases.planning_knowledge import (
        _looks_like_verification_only_task,
    )

    assert _looks_like_verification_only_task(
        "Improve task verification", "add content-aware checks to prove the output"
    )


def test_looks_like_verification_only_task_negative_has_implementation():
    from app.services.orchestration.phases.planning_knowledge import (
        _looks_like_verification_only_task,
    )

    assert not _looks_like_verification_only_task(
        "Create new feature", "add new functionality and verification"
    )


def test_looks_like_verification_only_task_negative_no_markers():
    from app.services.orchestration.phases.planning_knowledge import (
        _looks_like_verification_only_task,
    )

    assert not _looks_like_verification_only_task(
        "Build API endpoint", "implement REST API"
    )


# ---------------------------------------------------------------------------
# D4 — ProjectIndex and build_project_index
# ---------------------------------------------------------------------------


def test_build_project_index_returns_project_index(tmp_path):
    from app.services.project.index_service import ProjectIndex, build_project_index

    (tmp_path / "main.py").write_text("# entry\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "models.py").write_text("# models\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_models.py").write_text("# test\n")

    idx = build_project_index(tmp_path)
    assert isinstance(idx, ProjectIndex)
    assert idx.project_dir == tmp_path
    assert isinstance(idx.generated_at, float)


def test_project_index_source_files_excludes_test_files(tmp_path):
    from app.services.project.index_service import build_project_index

    (tmp_path / "models.py").write_text("")
    (tmp_path / "test_models.py").write_text("")

    idx = build_project_index(tmp_path)
    assert "models.py" in idx.source_files
    assert "test_models.py" not in idx.source_files
    assert "test_models.py" in idx.test_files


def test_project_index_entry_points_detects_main_and_manage(tmp_path):
    from app.services.project.index_service import build_project_index

    (tmp_path / "main.py").write_text("")
    (tmp_path / "manage.py").write_text("")
    (tmp_path / "utils.py").write_text("")

    idx = build_project_index(tmp_path)
    assert "main.py" in idx.entry_points
    assert "manage.py" in idx.entry_points
    assert "utils.py" not in idx.entry_points


def test_project_index_package_roots_detects_init(tmp_path):
    from app.services.project.index_service import build_project_index

    pkg = tmp_path / "mypackage"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "module.py").write_text("")

    idx = build_project_index(tmp_path)
    assert "mypackage" in idx.package_roots


def test_build_project_index_excludes_venv_and_node_modules(tmp_path):
    from app.services.project.index_service import build_project_index

    (tmp_path / "app.py").write_text("")
    venv = tmp_path / "venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "site_file.py").write_text("")
    node = tmp_path / "node_modules" / "pkg"
    node.mkdir(parents=True)
    (node / "index.js").write_text("")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text("")

    idx = build_project_index(tmp_path)
    paths = idx.source_files + idx.test_files + idx.entry_points
    assert not any("venv" in p for p in paths)
    assert not any("node_modules" in p for p in paths)
    assert not any(".git" in p for p in paths)
