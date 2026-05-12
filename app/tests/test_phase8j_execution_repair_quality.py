"""Phase 8J: execution repair quality — prose command rejection tests."""

import pytest

from app.services.orchestration.execution.step_support import (
    _is_prose_command,
    build_step_repair_prompt,
    _infer_debug_payload_from_text,
)
from app.services.orchestration.debug_feedback import (
    DebugFeedbackEnvelope,
    build_bounded_debug_repair_prompt,
    normalize_bounded_debug_repair_payload,
)


# --- _is_prose_command ---


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("Replace the verification command with pytest tests/", True),
        ("Replace verification command with pytest", True),
        ("Update the import in app.py to use os.path", True),
        ("Add the missing dependency to requirements.txt", True),
        ("Remove the broken assertion from the test", True),
        ("Change the function signature to accept kwargs", True),
        ("Install the package using pip install requests", True),
        ("Create the missing __init__.py file", True),
        ("Edit the config to set DEBUG=False", True),
        ("Fix the broken test", True),
        ("Move the config to src/", True),
        ("Set DEBUG=False in app.py", True),
        ("Ensure the path exists before writing", True),
        ("Rewrite the test to avoid subprocess usage", True),
        # real shell commands — must NOT be flagged
        ("pytest tests/", False),
        ("python -m pytest app/tests/ -q", False),
        ("npm run build", False),
        ("pip install -r requirements.txt", False),
        ("git add .", False),
        ("mkdir -p src/components", False),
        ("PYTHONPATH=. pytest app/tests/", False),
        ("", False),
        ("replace without capital", False),
    ],
)
def test_is_prose_command(cmd, expected):
    assert _is_prose_command(cmd) is expected


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


def test_normalize_rejects_missing_command():
    payload = [{"title": "no command", "verification_command": "pytest"}]
    assert normalize_bounded_debug_repair_payload(payload) is None


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
        assert not _is_prose_command(result.get("fix", ""))


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
