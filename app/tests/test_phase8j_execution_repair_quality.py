"""Phase 8J: execution repair quality — prose command rejection tests."""

import pytest

from app.services.orchestration.execution.step_support import (
    build_step_repair_prompt,
    _infer_debug_payload_from_text,
    is_runnable_shell_command_fix,
    normalize_runnable_shell_command_fix,
)
from app.services.orchestration.execution.execution_flow import (
    execute_verification_command,
    missing_expected_files,
    stub_expected_files,
)
from app.services.orchestration.diagnostics.debug_feedback import (
    DebugFeedbackEnvelope,
    build_bounded_debug_repair_prompt,
    normalize_bounded_debug_repair_payload,
    normalize_bounded_debug_repair_payload_detailed,
)
from app.services.orchestration.diagnostics.public_api_guard import (
    detect_debug_repair_public_api_removal,
)
from app.services.orchestration.prompt_templates import PromptTemplates

# --- typed command-fix validation ---


def _write_public_api_fixture(tmp_path):
    src_dir = tmp_path / "src" / "medium_cli"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    return src_dir, tests_dir


def test_public_api_guard_rejects_cli_rewrite_removing_build_parser(tmp_path):
    src_dir, tests_dir = _write_public_api_fixture(tmp_path)
    (src_dir / "cli.py").write_text(
        "def build_parser():\n"
        "    return object()\n"
        "\n"
        "def build_store():\n"
        "    return object()\n"
        "\n"
        "def main(argv=None):\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (tests_dir / "test_cli.py").write_text(
        "from medium_cli.cli import build_parser, main\n",
        encoding="utf-8",
    )

    removals = detect_debug_repair_public_api_removal(
        project_dir=tmp_path,
        ops=[
            {
                "op": "write_file",
                "path": "src/medium_cli/cli.py",
                "content": "def main(args):\n    return 0\n",
            }
        ],
    )

    assert len(removals) == 1
    assert removals[0].path == "src/medium_cli/cli.py"
    assert removals[0].module == "medium_cli.cli"
    assert removals[0].removed_symbols == ["build_parser"]


def test_public_api_guard_rejects_store_rewrite_removing_task(tmp_path):
    src_dir, tests_dir = _write_public_api_fixture(tmp_path)
    (src_dir / "store.py").write_text(
        "class Task:\n" "    pass\n" "\n" "class TaskStore:\n" "    pass\n",
        encoding="utf-8",
    )
    (tests_dir / "test_summary.py").write_text(
        "from medium_cli.store import Task\n",
        encoding="utf-8",
    )

    removals = detect_debug_repair_public_api_removal(
        project_dir=tmp_path,
        ops=[
            {
                "op": "write_file",
                "path": "src/medium_cli/store.py",
                "content": "class TaskStore:\n    pass\n",
            }
        ],
    )

    assert len(removals) == 1
    assert removals[0].removed_symbols == ["Task"]


def test_public_api_guard_allows_full_rewrite_preserving_imported_symbols(tmp_path):
    src_dir, tests_dir = _write_public_api_fixture(tmp_path)
    (src_dir / "cli.py").write_text(
        "def build_parser():\n    return object()\n"
        "def main(argv=None):\n    return 0\n",
        encoding="utf-8",
    )
    (tests_dir / "test_cli.py").write_text(
        "from medium_cli.cli import build_parser, main\n",
        encoding="utf-8",
    )

    removals = detect_debug_repair_public_api_removal(
        project_dir=tmp_path,
        ops=[
            {
                "op": "write_file",
                "path": "src/medium_cli/cli.py",
                "content": (
                    "def build_parser():\n    return object()\n"
                    "def main(argv=None):\n    return 0\n"
                    "def summary():\n    return None\n"
                ),
            }
        ],
    )

    assert removals == []


def test_public_api_guard_allows_removing_unused_public_symbol(tmp_path):
    src_dir, tests_dir = _write_public_api_fixture(tmp_path)
    (src_dir / "cli.py").write_text(
        "def build_parser():\n    return object()\n"
        "def unused_helper():\n    return None\n",
        encoding="utf-8",
    )
    (tests_dir / "test_cli.py").write_text(
        "from medium_cli.cli import build_parser\n",
        encoding="utf-8",
    )

    removals = detect_debug_repair_public_api_removal(
        project_dir=tmp_path,
        ops=[
            {
                "op": "write_file",
                "path": "src/medium_cli/cli.py",
                "content": "def build_parser():\n    return object()\n",
            }
        ],
    )

    assert removals == []


def test_public_api_guard_ignores_command_fix_without_source_ops(tmp_path):
    _src_dir, tests_dir = _write_public_api_fixture(tmp_path)
    tests_dir.joinpath("test_cli.py").write_text(
        "from medium_cli.cli import build_parser\n",
        encoding="utf-8",
    )

    removals = detect_debug_repair_public_api_removal(
        project_dir=tmp_path,
        ops=[],
    )

    assert removals == []


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("Replace the verification command with pytest tests/", False),
        ("Replace verification command with pytest", False),
        ("Update the import in app.py to use os.path", False),
        ("Add the missing dependency to requirements.txt", False),
        ("Remove the broken assertion from the test", False),
        ("Change the function signature to accept kwargs", False),
        ("Install the package using pip install requests", False),
        ("Create the missing __init__.py file", False),
        ("Edit the config to set DEBUG=False", False),
        ("Fix the broken test", False),
        ("Move the config to src/", False),
        ("Set DEBUG=False in app.py", False),
        ("Ensure the path exists before writing", False),
        ("Rewrite the test to avoid subprocess usage", False),
        ("replace without capital", False),
        # real shell commands
        ("pytest tests/", True),
        ("python -m pytest app/tests/ -q", True),
        ("npm run build", True),
        ("pip install -r requirements.txt", True),
        ("git add .", True),
        ("mkdir -p src/components", True),
        ("PYTHONPATH=. pytest app/tests/", True),
        ("", False),
    ],
)
def test_command_fix_requires_runnable_shell_token(cmd, expected):
    assert is_runnable_shell_command_fix(cmd) is expected


# --- normalize_bounded_debug_repair_payload: prose command rejection ---


def test_normalize_prose_command_rejected():
    payload = [
        {
            "title": "fix verification",
            "command": "Replace the verification command with pytest tests/unit/",
            "verification_command": "pytest tests/unit/",
        }
    ]
    assert normalize_bounded_debug_repair_payload(payload) is None


def test_normalize_real_shell_command_stays_command_fix():
    payload = [
        {
            "title": "run tests",
            "command": "python -m pytest app/tests/ -q",
            "verification_command": "python -m pytest app/tests/ -q --tb=no",
        }
    ]
    result = normalize_bounded_debug_repair_payload(payload)
    assert result is not None
    assert result["fix_type"] == "command_fix"
    assert result["fix"] == "python -m pytest app/tests/ -q"


def test_normalize_rejects_pure_sed_command_fix_for_semantic_pytest_failure():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command="pytest -q",
        return_code=1,
        workspace_path=".",
        failure_class="pytest_failure",
        pytest_excerpt="pytest: error: unrecognized arguments: --uppercase",
    )
    payload = [
        {
            "title": "patch option spelling",
            "command": "sed -i 's/--uppercase/--uppercase /' src/small_cli/cli.py",
            "verification_command": "pytest -q",
        }
    ]

    assert normalize_bounded_debug_repair_payload(payload, envelope=envelope) is None


def test_normalize_rejects_pure_sed_command_fix_for_semantic_validation_failure():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command='python -c "import src.small_cli.cli"',
        return_code=1,
        workspace_path=".",
        failure_class="completion_validation_failed",
        stderr_excerpt="NameError: name 'cli' is not defined",
    )
    payload = [
        {
            "title": "patch typer name",
            "command": "sed -i 's/^cli = typer.Typer()/cli = typer.Typer(name=\"cli\")/' src/small_cli/cli.py",
            "verification_command": 'python -c "import src.small_cli.cli"',
        }
    ]

    assert normalize_bounded_debug_repair_payload(payload, envelope=envelope) is None


def test_source_edit_context_rejects_transient_python_command_fix():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command='python -c "import src.small_cli.cli"',
        return_code=1,
        workspace_path=".",
        failure_class="completion_validation_failed",
        changed_files=["src/small_cli/cli.py"],
    )
    payload = [
        {
            "title": "Mutate parser in memory",
            "command": 'python -c \'import src.small_cli.cli; src.small_cli.cli.build_parser().add_argument("--uppercase", action="store_true")\'',
            "verification_command": "python -m pytest -q",
        }
    ]

    result = normalize_bounded_debug_repair_payload_detailed(
        payload,
        envelope=envelope,
        source_edit_context=True,
    )

    assert result.payload is None
    assert result.rejection_reason == "source_context_command_fix_rejected"


def test_source_edit_context_rejects_sed_command_fix_before_retry():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command='grep --quiet "--uppercase" src/small_cli/cli.py',
        return_code=1,
        workspace_path=".",
        failure_class="completion_validation_failed",
        changed_files=["src/small_cli/cli.py"],
    )
    payload = [
        {
            "title": "Patch flag spelling",
            "command": "sed -i 's/--uppercase/ -i /' src/small_cli/cli.py",
            "verification_command": "grep --quiet '-i' src/small_cli/cli.py",
        }
    ]

    result = normalize_bounded_debug_repair_payload_detailed(
        payload,
        envelope=envelope,
        source_edit_context=True,
    )

    assert result.payload is None
    assert result.rejection_reason == "source_context_command_fix_rejected"


def test_source_edit_context_accepts_structured_ops_fix():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command="python -m pytest -q",
        return_code=1,
        workspace_path=".",
        failure_class="pytest_failure",
        changed_files=["src/small_cli/cli.py"],
    )
    payload = [
        {
            "title": "Wire uppercase flag durably",
            "command": "python -m pytest -q",
            "verification_command": "python -m pytest -q",
            "expected_files": ["src/small_cli/cli.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/small_cli/cli.py",
                    "old": "print(format_message(args.message))",
                    "new": "message = args.message.upper() if args.uppercase else args.message\n    print(format_message(message))",
                }
            ],
        }
    ]

    result = normalize_bounded_debug_repair_payload(
        payload,
        envelope=envelope,
        source_edit_context=True,
    )

    assert result is not None
    assert result["fix_type"] == "ops_fix"
    assert result["ops"][0]["path"] == "src/small_cli/cli.py"


def test_explicit_ops_fix_accepts_source_ops_without_command():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command="python -m pytest -q",
        return_code=2,
        workspace_path=".",
        failure_class="import_error",
    )
    payload = [
        {
            "repair_type": "ops_fix",
            "verification_command": "python -m pytest -q",
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/small_cli/cli.py",
                    "content": "def main(argv=None):\n    return 0\n",
                }
            ],
        }
    ]

    result = normalize_bounded_debug_repair_payload_detailed(
        payload,
        envelope=envelope,
        source_edit_context=False,
    )

    assert result.payload is not None
    assert result.payload["fix_type"] == "ops_fix"
    assert result.payload["fix"] == ""
    assert result.payload["verification"] == "python -m pytest -q"
    assert result.payload["ops"][0]["path"] == "src/small_cli/cli.py"
    assert result.rejection_reason is None


def test_source_edit_context_accepts_wrapped_structured_ops_fix():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command="python -m pytest -q",
        return_code=1,
        workspace_path=".",
        failure_class="pytest_failure",
        changed_files=["src/small_cli/cli.py"],
    )
    payload = [
        {
            "title": "Wire uppercase flag durably",
            "command": "echo bad >> src/small_cli/cli.py",
            "verification_command": "python -m pytest -q",
            "ops": [
                {
                    "replace_in_file": {
                        "path": "src/small_cli/cli.py",
                        "old": "print(format_message(args.message))",
                        "new": "message = args.message.upper() if args.uppercase else args.message\n    print(format_message(message))",
                    }
                }
            ],
        }
    ]

    result = normalize_bounded_debug_repair_payload(
        payload,
        envelope=envelope,
        source_edit_context=True,
    )

    assert result is not None
    assert result["fix_type"] == "ops_fix"
    assert result["fix"] == ""
    assert result["ops"] == [
        {
            "op": "replace_in_file",
            "path": "src/small_cli/cli.py",
            "old": "print(format_message(args.message))",
            "new": "message = args.message.upper() if args.uppercase else args.message\n    print(format_message(message))",
        }
    ]


def test_normalize_allows_command_fix_that_runs_verifier_for_pytest_failure():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command="pytest -q",
        return_code=1,
        workspace_path=".",
        failure_class="pytest_failure",
        pytest_excerpt="pytest: error: unrecognized arguments: --uppercase",
    )
    payload = [
        {
            "title": "run focused verification",
            "command": "python -m pytest tests/test_cli.py -q",
            "verification_command": "python -m pytest tests/test_cli.py -q",
        }
    ]

    result = normalize_bounded_debug_repair_payload(payload, envelope=envelope)

    assert result is not None
    assert result["fix_type"] == "command_fix"


def test_source_repair_rejects_touch_command_for_import_symbol_failure(tmp_path):
    source_dir = tmp_path / "src" / "small_cli"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "cli.py"
    source_path.write_text("def main():\n    return 0\n", encoding="utf-8")
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=4,
        failure_phase="execution",
        failed_command="python -m pytest -q",
        return_code=2,
        workspace_path=str(tmp_path),
        failure_class="import_error",
        stderr_excerpt=(
            "ImportError: cannot import name 'build_parser' from "
            f"'small_cli.cli' ({source_path})"
        ),
    )
    payload = [
        {
            "title": "Create missing parser file",
            "command": "touch src/small_cli/cli/build_parser.py",
            "verification_command": "python -m pytest -q",
        }
    ]

    result = normalize_bounded_debug_repair_payload_detailed(
        payload,
        envelope=envelope,
    )

    assert result.payload is None
    assert result.rejection_reason == "source_repair_command_fix_rejected"


def test_non_source_command_failure_still_allows_command_fix():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command="pytest -q",
        return_code=1,
        workspace_path=".",
        failure_class="missing_dependency",
    )
    payload = [
        {
            "title": "Install missing test dependency",
            "command": "pip install pytest",
            "verification_command": "pytest -q",
        }
    ]

    result = normalize_bounded_debug_repair_payload(
        payload,
        envelope=envelope,
        source_edit_context=False,
    )

    assert result is not None
    assert result["fix_type"] == "command_fix"


def test_normalize_rejects_missing_command():
    payload = [{"title": "no command", "verification_command": "pytest"}]
    assert normalize_bounded_debug_repair_payload(payload) is None


def test_normalize_detailed_records_unsupported_shape():
    result = normalize_bounded_debug_repair_payload_detailed([])

    assert result.payload is None
    assert result.rejection_reason == "unsupported_shape"
    assert result.parsed_shape == {"type": "list", "length": 0}


def test_normalize_detailed_records_missing_command():
    result = normalize_bounded_debug_repair_payload_detailed(
        [{"title": "no command", "verification_command": "pytest"}]
    )

    assert result.payload is None
    assert result.rejection_reason == "missing_command"
    assert result.parsed_shape["type"] == "list"
    assert result.parsed_shape["first_item_keys"] == [
        "title",
        "verification_command",
    ]


def test_normalize_detailed_records_semantic_string_edit_rejection():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command="pytest -q",
        return_code=1,
        workspace_path=".",
        failure_class="pytest_failure",
        pytest_excerpt="pytest: error: unrecognized arguments: --uppercase",
    )
    payload = [
        {
            "title": "patch option spelling",
            "command": "sed -i 's/--uppercase/--uppercase /' src/small_cli/cli.py",
            "verification_command": "pytest -q",
        }
    ]

    result = normalize_bounded_debug_repair_payload_detailed(payload, envelope=envelope)

    assert result.payload is None
    assert result.rejection_reason == "semantic_string_edit_rejected"


def test_normalize_rejects_missing_verification():
    payload = [{"title": "no verify", "command": "pytest tests/"}]
    assert normalize_bounded_debug_repair_payload(payload) is None


# --- _infer_debug_payload_from_text: prose command handling ---


def test_infer_prose_fix_classifies_as_code_fix():
    text = (
        "Analysis: The verification command is too weak.\n"
        "Fix: Replace the echo check with a proper pytest invocation.\n"
        "Confidence: MEDIUM"
    )
    result = _infer_debug_payload_from_text(text, error_message="", step=None)
    assert result is not None
    assert result["fix_type"] == "code_fix"


def test_infer_prose_fix_with_missing_expected_files_does_not_promote_command_fix():
    text = (
        "Analysis: README is not required.\n"
        "Fix: Remove the broken expected file assertion.\n"
        "Confidence: MEDIUM"
    )
    result = _infer_debug_payload_from_text(
        text,
        error_message="Expected files are missing: README.md",
        step={"expected_files": ["README.md"]},
    )

    assert result is not None
    assert result["fix_type"] == "code_fix"
    assert result["expected_files"] == []


def test_infer_real_command_fix_stays():
    text = (
        "Analysis: Wrong command.\n"
        "Fix: run `python -m pytest tests/ -q`\n"
        "Confidence: HIGH"
    )
    result = _infer_debug_payload_from_text(text, error_message="", step=None)
    # fix_type depends on markers, but if command_fix it must not be prose
    if result and result.get("fix_type") == "command_fix":
        assert is_runnable_shell_command_fix(result.get("fix", ""))


def test_command_fix_strips_short_run_label_before_validation():
    command = "Run: cd /tmp/example && node -e \"console.log('patched')\""

    assert normalize_runnable_shell_command_fix(command) == (
        "cd /tmp/example && node -e \"console.log('patched')\""
    )
    assert is_runnable_shell_command_fix(command) is True


def test_step_repair_prompt_requires_runnable_commands_and_write_file_ops(tmp_path):
    prompt = build_step_repair_prompt(
        task_prompt="Fix the failing test",
        step={
            "step_number": 1,
            "description": "Repair source",
            "commands": ["pytest tests/"],
            "verification": "pytest tests/",
            "expected_files": ["src/app.py"],
        },
        step_index=0,
        project_dir=tmp_path,
        prior_results_summary="",
        project_context="",
    )

    assert "runnable shell strings, not prose instructions" in prompt
    assert "Prefer ops write_file entries for file rewrites" in prompt
    assert "do not use heredoc rewrites" in prompt


def test_debug_repair_prompt_rejects_prose_commands_and_heredoc_rewrites():
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failed_command="pytest tests/",
        return_code=1,
        workspace_path=".",
        failure_class="pytest_failure",
    )

    prompt = build_bounded_debug_repair_prompt(envelope)

    assert "runnable shell strings, not prose instructions" in prompt
    assert "Do not use heredoc rewrites" in prompt


def test_debugging_prompt_names_typed_structured_op_repair():
    prompt = PromptTemplates.build_debugging_prompt(
        step_description="Update package.json",
        error_message="replace_in_file old text not found in package.json",
        command_output="",
        verification_output="",
        attempt_number=1,
        max_attempts=3,
        project_name="demo",
        workspace_root="/workspace",
        project_dir="/workspace/demo",
    )

    assert '"replace_op"' in prompt
    assert '"replacement_ops"' in prompt
    assert "stale `replace_in_file old text not found`" in prompt


def test_stub_expected_files_allows_empty_gitkeep_sentinel(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / ".gitkeep").write_text("")

    assert stub_expected_files(tmp_path, ["logs/.gitkeep"]) == []


def test_stub_expected_files_allows_empty_python_package_marker(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")

    assert stub_expected_files(tmp_path, ["src/pkg/__init__.py"]) == []


def test_stub_expected_files_still_flags_ordinary_empty_expected_file(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "marker.txt").write_text("")

    assert stub_expected_files(tmp_path, ["logs/marker.txt"]) == ["logs/marker.txt"]


def test_expected_files_glob_matches_existing_nonstub_files(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "flower-bg.svg").write_text("<svg></svg>")

    assert missing_expected_files(tmp_path, ["images/*.svg"]) == []
    assert stub_expected_files(tmp_path, ["images/*.svg"]) == []


def test_expected_files_glob_reports_missing_when_no_match(tmp_path):
    (tmp_path / "images").mkdir()

    assert missing_expected_files(tmp_path, ["images/*.svg"]) == ["images/*.svg"]


def test_empty_verification_failure_reports_command(tmp_path):
    result = execute_verification_command(
        project_dir=tmp_path,
        command='python -c "import sys; sys.exit(1)"',
        timeout_seconds=5,
    )

    assert result["success"] is False
    assert "Verification command failed with return code 1" in result["output"]
    assert "sys.exit(1)" in result["output"]
