from __future__ import annotations

from pathlib import Path

from app.services.orchestration.executor import ExecutorService


def test_directory_read_failure_at_project_root_gets_inventory_first_hint(tmp_path):
    project_dir = tmp_path / "demo-project"
    project_dir.mkdir()

    hints = ExecutorService.tool_failure_correction_hints(
        [
            "[tools] read failed: EISDIR: illegal operation on a directory, read "
            f'raw_params={{"path":"{project_dir}"}}'
        ],
        project_dir,
    )

    combined = " ".join(hints)

    assert "Do not read" in combined
    assert "rg --files . | head -200" in combined
    assert f"{project_dir}/src/index.ts" in combined


def test_directory_read_failure_in_subdir_gets_targeted_listing_hint(tmp_path):
    project_dir = tmp_path / "demo-project"
    nested_dir = project_dir / "src" / "utils"
    nested_dir.mkdir(parents=True)

    hints = ExecutorService.tool_failure_correction_hints(
        [
            "[tools] read failed: EISDIR: illegal operation on a directory, read "
            f'raw_params={{"path":"{nested_dir}"}}'
        ],
        project_dir,
    )

    combined = " ".join(hints)

    assert "find ./src/utils -maxdepth 4 -type f | sort | head -200" in combined
    assert str(nested_dir) in combined


def test_directory_read_failure_at_project_root_short_circuits_to_discovery(tmp_path):
    project_dir = tmp_path / "demo-project"
    project_dir.mkdir()

    should_rewrite = ExecutorService.should_short_circuit_to_workspace_discovery(
        [
            "[tools] read failed: EISDIR: illegal operation on a directory, read "
            f'raw_params={{"path":"{project_dir}"}}'
        ],
        project_dir,
    )

    assert should_rewrite is True


def test_non_directory_tool_failure_does_not_short_circuit(tmp_path):
    project_dir = tmp_path / "demo-project"
    project_dir.mkdir()
    source_file = project_dir / "src" / "main.ts"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("export const ok = true;\n")

    should_rewrite = ExecutorService.should_short_circuit_to_workspace_discovery(
        [
            "[tools] read failed: ENOENT: no such file or directory, access "
            f'raw_params={{"path":"{source_file}"}}'
        ],
        project_dir,
    )

    assert should_rewrite is False
