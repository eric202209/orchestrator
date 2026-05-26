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


def test_phase11b_diff_repair_prompt_includes_debug_source_contract(tmp_path):
    project_dir = tmp_path / "project"
    source_dir = project_dir / "src" / "small_cli"
    source_dir.mkdir(parents=True)
    (source_dir / "__init__.py").write_text("", encoding="utf-8")
    source = source_dir / "cli.py"
    source.write_text(
        "from __future__ import annotations\n"
        "\n"
        "import argparse\n"
        "\n"
        "def format_message(message: str) -> str:\n"
        "    return message\n"
        "\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        "    parser = argparse.ArgumentParser(description='Print a message.')\n"
        "    parser.add_argument('message')\n"
        "    return parser\n"
        "\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    args = build_parser().parse_args(argv)\n"
        "    print(format_message(args.message))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    tests_dir = project_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_cli.py").write_text(
        "from small_cli.cli import build_parser, format_message, main\n"
        "\n"
        "def test_uppercase_option_prints_uppercase_message(capsys):\n"
        '    assert main(["--uppercase", "hello"]) == 0\n'
        '    assert capsys.readouterr().out.strip() == "HELLO"\n',
        encoding="utf-8",
    )
    snapshot = snapshot_file_contents(project_dir, ["src/small_cli/cli.py"])
    source.write_text(
        "Tiny message-printing CLI used by the orchestrator eval fixture.\n"
        "\n"
        "from __future__ import annotations\n",
        encoding="utf-8",
    )
    envelope = _envelope(
        failed_command="python -m py_compile src/small_cli/cli.py",
        stdout="write_file src/small_cli/cli.py (599 chars)",
        stderr="src/small_cli/cli.py has Python syntax errors: invalid syntax",
        validator_reasons=["cli.py has Python syntax errors: invalid syntax"],
        changed_files=["src/small_cli/cli.py"],
        workspace_path=project_dir,
    )
    capsule = build_diff_capsule(
        pre_checksum=snapshot,
        project_dir=project_dir,
        changed_files=["src/small_cli/cli.py"],
        envelope=envelope,
    )

    assert capsule is not None
    assert envelope.failure_class == "syntax_error"

    prompt = build_bounded_diff_repair_prompt(capsule, envelope=envelope)

    assert "Unified diff capsule" in prompt
    assert "Debug source contract:" in prompt
    assert "Existing tests are the failing contract." in prompt
    assert "Do not edit tests or verifier commands." in prompt
    assert "Repair source code under the required target." in prompt
    assert "src/small_cli/cli.py" in prompt
    assert 'main(["--uppercase", "hello"]) should equal 0' in prompt
    assert 'printed output should equal "HELLO"' in prompt
    assert "No placeholder/pass/TODO/export-only fixes." in prompt
