from __future__ import annotations

from app.services.orchestration.diagnostics.debug_feedback import (
    build_debug_feedback_envelope,
)
from app.services.orchestration.diagnostics.diff_capsule import (
    DIFF_LINE_LIMIT,
    build_bounded_diff_repair_prompt,
    build_diff_capsule,
    snapshot_file_contents,
)


def _envelope(**overrides):
    defaults = {
        "task_execution_id": 1,
        "task_id": 2,
        "step_index": 1,
        "failure_phase": "execution",
        "failed_command": "pytest tests/test_demo.py",
        "return_code": 1,
        "stdout": "FAILED tests/test_demo.py::test_value",
        "stderr": "AssertionError: expected 2",
        "validator_reasons": [],
        "changed_files": ["src/demo.py"],
        "workspace_path": "",
    }
    defaults.update(overrides)
    return build_debug_feedback_envelope(**defaults)


def test_build_diff_capsule_returns_single_file_capsule(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "src").mkdir(parents=True)
    source = project_dir / "src" / "demo.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = snapshot_file_contents(project_dir, ["src/demo.py"])
    source.write_text("VALUE = 2\n", encoding="utf-8")

    capsule = build_diff_capsule(
        pre_checksum=snapshot,
        project_dir=project_dir,
        changed_files=["src/demo.py"],
        envelope=_envelope(workspace_path=project_dir),
    )

    assert capsule is not None
    assert capsule.primary_file == "src/demo.py"
    assert "-VALUE = 1" in capsule.diff_text
    assert "+VALUE = 2" in capsule.diff_text
    assert capsule.failure_line == "AssertionError: expected 2"
    assert capsule.changed_file_count == 1


def test_build_diff_capsule_returns_none_for_zero_changed_files(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    capsule = build_diff_capsule(
        pre_checksum={},
        project_dir=project_dir,
        changed_files=[],
        envelope=_envelope(changed_files=[], workspace_path=project_dir),
    )

    assert capsule is None


def test_build_diff_capsule_caps_diff_lines(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    source = project_dir / "big.py"
    source.write_text("\n".join(f"OLD_{i}" for i in range(200)), encoding="utf-8")
    snapshot = snapshot_file_contents(project_dir, ["big.py"])
    source.write_text("\n".join(f"NEW_{i}" for i in range(200)), encoding="utf-8")

    capsule = build_diff_capsule(
        pre_checksum=snapshot,
        project_dir=project_dir,
        changed_files=["big.py"],
        envelope=_envelope(
            stdout="FAILED tests/test_big.py::test_big",
            stderr="AssertionError: big.py:5",
            changed_files=["big.py"],
            workspace_path=project_dir,
        ),
    )

    assert capsule is not None
    assert capsule.diff_line_count == DIFF_LINE_LIMIT


def test_build_diff_capsule_returns_none_for_binary_primary_file(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    binary = project_dir / "image.bin"
    binary.write_bytes(b"\xff\xfe\x00")

    capsule = build_diff_capsule(
        pre_checksum={},
        project_dir=project_dir,
        changed_files=["image.bin"],
        envelope=_envelope(
            stderr="SyntaxError: image.bin:1",
            changed_files=["image.bin"],
            workspace_path=project_dir,
        ),
    )

    assert capsule is None


def test_bounded_diff_repair_prompt_is_minimal(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "src").mkdir(parents=True)
    source = project_dir / "src" / "demo.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = snapshot_file_contents(project_dir, ["src/demo.py"])
    source.write_text("VALUE = 2\n", encoding="utf-8")
    capsule = build_diff_capsule(
        pre_checksum=snapshot,
        project_dir=project_dir,
        changed_files=["src/demo.py"],
        envelope=_envelope(
            stdout="FULL STDOUT SHOULD NOT BE INCLUDED",
            stderr="AssertionError: expected 2",
            workspace_path=project_dir,
        ),
    )

    prompt = build_bounded_diff_repair_prompt(capsule)

    assert "Unified diff capsule" in prompt
    assert "AssertionError: expected 2" in prompt
    assert "-VALUE = 1" in prompt
    assert "+VALUE = 2" in prompt
    assert "FULL STDOUT SHOULD NOT BE INCLUDED" not in prompt
    assert "session history" in prompt
