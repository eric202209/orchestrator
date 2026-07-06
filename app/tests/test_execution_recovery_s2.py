"""Phase 13B-S2: Tests for step-scope recovery patch generation.

Verifies:
  - replace_in_file patch succeeds (rerun exits 0)
  - create_file patch succeeds for missing import/module
  - Prose/markdown response rejected (FAILED, no ATTEMPTED)
  - Patch outside allowed scope rejected
  - Test deletion / skip marker rejected
  - Repeated patch hash rejected
  - Rerun command nonzero fails
  - Test preservation violated (validator rejection)
  - Budget stops at 2
  - Completion scope still disabled
  - Existing ABORT path unchanged on recovery failure
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.execution_recovery_service import (
    RECOVERY_BUDGET,
    ExecutionRecoveryService,
)
from app.services.orchestration.recovery.recovery_patch import (
    RecoveryPatch,
    build_recovery_prompt,
    parse_recovery_patch,
    validate_recovery_patch,
    apply_recovery_patch,
)
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.orchestration.prompt_templates import OrchestrationState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(**kwargs) -> OrchestrationState:
    defaults = dict(session_id="s2", task_description="implement feature X")
    defaults.update(kwargs)
    return OrchestrationState(**defaults)


def _make_evidence(
    project_dir: Path, source_file: str = "src/foo.py", **kwargs
) -> ExecutionRecoveryEvidence:
    defaults = dict(
        task_title="My task",
        task_description="implement feature X",
        failed_command=f"pytest tests/test_foo.py -x",
        exit_code=1,
        stdout_excerpt="collected 1 item",
        stderr_excerpt=f"FAILED tests/test_foo.py::test_bar - ImportError: cannot import name 'Foo' from '{source_file}'",
        traceback_excerpt=f"ImportError: cannot import name 'Foo'\n  File \"{source_file}\", line 1",
        changed_files=[source_file],
        failure_class="import_error",
    )
    defaults.update(kwargs)
    return ExecutionRecoveryEvidence(**defaults)


def _patch_json(**kwargs) -> str:
    """Build a JSON string as if returned by the LLM."""
    defaults = {
        "patch_type": "replace_in_file",
        "path": "src/foo.py",
        "old": "PLACEHOLDER_OLD",
        "new": "PLACEHOLDER_NEW",
        "rerun_command": "pytest tests/test_foo.py -x",
    }
    defaults.update(kwargs)
    return json.dumps(defaults)


def _make_llm(response: str):
    """Return a callable that always returns `response`."""

    def _llm(_prompt):
        return response

    return _llm


def _make_runner(returncode: int = 0, stdout: str = "1 passed", stderr: str = ""):
    """Return a command_runner mock that returns fixed values."""

    def _runner(_cmd):
        return returncode, stdout, stderr

    return _runner


def _events(tmp_path, event_type):
    return read_orchestration_events(
        tmp_path, session_id=2, task_id=2, event_type_filter=event_type
    )


def _call(
    tmp_path, state, evidence, llm_callable=None, command_runner=None, scope="step"
):
    return ExecutionRecoveryService.attempt_recovery(
        project_dir=tmp_path,
        session_id=2,
        task_id=2,
        evidence=evidence,
        orchestration_state=state,
        scope=scope,
        step_index=1,
        llm_callable=llm_callable,
        command_runner=command_runner,
    )


# ---------------------------------------------------------------------------
# replace_in_file patch succeeds
# ---------------------------------------------------------------------------


def test_replace_in_file_patch_succeeds(tmp_path):
    # Create the source file with a wrong import.
    src = tmp_path / "src"
    src.mkdir()
    source_file = src / "foo.py"
    source_file.write_text("from bar import Baz\n\nclass Foo:\n    pass\n")

    evidence = _make_evidence(tmp_path, source_file="src/foo.py")
    state = _make_state()

    patch_json = _patch_json(
        patch_type="replace_in_file",
        path="src/foo.py",
        old="from bar import Baz",
        new="from baz import Baz",
        rerun_command="pytest tests/test_foo.py -x",
    )
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "success"
    assert result["patch_type"] == "replace_in_file"
    assert result["patch_path"] == "src/foo.py"

    # File was patched.
    assert "from baz import Baz" in source_file.read_text()

    # Events emitted.
    attempted = _events(tmp_path, EventType.EXECUTION_RECOVERY_ATTEMPTED)
    succeeded = _events(tmp_path, EventType.EXECUTION_RECOVERY_SUCCEEDED)
    assert len(attempted) == 1
    assert len(succeeded) == 1
    assert attempted[0]["details"]["patch_type"] == "replace_in_file"
    assert succeeded[0]["details"]["rerun_exit_code"] == 0

    # Budget incremented.
    assert state.execution_recovery_attempts == 1


def test_replace_in_file_patch_rollsback_on_rerun_failure(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    source_file = src / "foo.py"
    source_file.write_text("from bar import Baz\n")

    evidence = _make_evidence(tmp_path, source_file="src/foo.py")
    state = _make_state()

    patch_json = _patch_json(
        patch_type="replace_in_file",
        path="src/foo.py",
        old="from bar import Baz",
        new="from baz import Baz",
    )
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(1, stdout="", stderr="FAILED"),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "rerun_still_failing"
    # File should be rolled back to original.
    assert "from bar import Baz" in source_file.read_text()
    failed = _events(tmp_path, EventType.EXECUTION_RECOVERY_FAILED)
    assert len(failed) == 1
    assert failed[0]["details"]["stop_reason"] == "rerun_still_failing"
    assert failed[0]["details"]["rerun_exit_code"] == 1


# ---------------------------------------------------------------------------
# create_file patch succeeds for missing import/module
# ---------------------------------------------------------------------------


def test_create_file_patch_succeeds(tmp_path):
    # Missing __init__.py scenario: the traceback mentions the missing module.
    pkg = tmp_path / "mypackage"
    pkg.mkdir()
    evidence = _make_evidence(
        tmp_path,
        source_file="mypackage/module.py",
        failure_class="module_not_found",
        traceback_excerpt="ModuleNotFoundError: No module named 'mypackage.utils'\n  File \"mypackage/module.py\", line 1",
        stderr_excerpt="ModuleNotFoundError: No module named 'mypackage.utils'\n  mypackage/utils.py",
    )
    state = _make_state()

    patch_json = _patch_json(
        patch_type="create_file",
        path="mypackage/utils.py",
        new="def helper():\n    return True\n",
        rerun_command="pytest tests/test_foo.py -x",
    )
    del json.loads(patch_json)["old"]  # simulate no 'old' field
    patch_json = json.dumps(
        {
            "patch_type": "create_file",
            "path": "mypackage/utils.py",
            "new": "def helper():\n    return True\n",
            "rerun_command": "pytest tests/test_foo.py -x",
        }
    )

    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "success"
    assert result["patch_type"] == "create_file"
    assert (tmp_path / "mypackage" / "utils.py").exists()

    succeeded = _events(tmp_path, EventType.EXECUTION_RECOVERY_SUCCEEDED)
    assert len(succeeded) == 1


# ---------------------------------------------------------------------------
# Prose/markdown response rejected
# ---------------------------------------------------------------------------


def test_prose_response_rejected(tmp_path):
    evidence = _make_evidence(tmp_path)
    state = _make_state()

    prose = "The fix is to update the import statement in src/foo.py to use the correct module."
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(prose),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "prose_response"

    # No ATTEMPTED event emitted (failure happened before patch apply).
    attempted = _events(tmp_path, EventType.EXECUTION_RECOVERY_ATTEMPTED)
    assert len(attempted) == 0

    failed = _events(tmp_path, EventType.EXECUTION_RECOVERY_FAILED)
    assert len(failed) == 1
    assert failed[0]["details"]["stop_reason"] == "prose_response"
    # Budget was consumed.
    assert state.execution_recovery_attempts == 1


def test_markdown_fenced_invalid_json_rejected(tmp_path):
    evidence = _make_evidence(tmp_path)
    state = _make_state()

    bad = "```json\n{not valid json at all}\n```"
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(bad),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "failed"
    assert state.execution_recovery_attempts == 1


# ---------------------------------------------------------------------------
# Patch outside allowed scope rejected
# ---------------------------------------------------------------------------


def test_unrelated_patch_path_rejected(tmp_path):
    evidence = _make_evidence(tmp_path)
    state = _make_state()

    patch_json = _patch_json(
        patch_type="replace_in_file",
        path="completely/unrelated/file.py",
        old="old text",
        new="new text",
    )
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "unrelated_patch"
    failed = _events(tmp_path, EventType.EXECUTION_RECOVERY_FAILED)
    assert failed[0]["details"]["stop_reason"] == "unrelated_patch"


def test_patch_in_venv_rejected(tmp_path):
    evidence = _make_evidence(tmp_path, source_file="venv/lib/foo.py")
    state = _make_state()

    patch_json = _patch_json(
        patch_type="replace_in_file",
        path="venv/lib/foo.py",
        old="old",
        new="new",
    )
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "excluded_path"


def test_patch_outside_project_dir_rejected(tmp_path):
    evidence = _make_evidence(tmp_path)
    state = _make_state()

    patch_json = _patch_json(
        patch_type="replace_in_file",
        path="/etc/passwd",
        old="root",
        new="hacked",
    )
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "path_outside_project"


def test_dangerous_rerun_command_rejected(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("x = 1\n")

    evidence = _make_evidence(tmp_path)
    state = _make_state()

    patch_json = _patch_json(
        patch_type="replace_in_file",
        path="src/foo.py",
        old="x = 1",
        new="x = 2",
        rerun_command="pytest tests/ && rm -rf /tmp",
    )
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "failed"
    # Starts with 'pytest ' so passes the prefix check, but '&&' is a denied pattern.
    assert result["reason"] == "dangerous_rerun_command"


# ---------------------------------------------------------------------------
# Test deletion rejected
# ---------------------------------------------------------------------------


def test_skip_marker_in_test_file_rejected(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    test_file = tests / "test_foo.py"
    test_file.write_text("def test_bar():\n    assert 1 == 1\n")

    evidence = _make_evidence(
        tmp_path,
        source_file="tests/test_foo.py",
        traceback_excerpt="FAILED tests/test_foo.py::test_bar",
        stderr_excerpt="tests/test_foo.py::test_bar FAILED",
    )
    state = _make_state()

    # Patch tries to add a skip marker to the test.
    patch_json = json.dumps(
        {
            "patch_type": "replace_in_file",
            "path": "tests/test_foo.py",
            "old": "def test_bar():\n    assert 1 == 1",
            "new": "import pytest\n\n@pytest.mark.skip\ndef test_bar():\n    assert 1 == 1",
            "rerun_command": "pytest tests/test_foo.py -x",
        }
    )
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "test_preservation_violated"
    # File unchanged.
    assert "@pytest.mark.skip" not in test_file.read_text()


# ---------------------------------------------------------------------------
# Repeated patch rejected
# ---------------------------------------------------------------------------


def test_repeated_patch_hash_rejected(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    source_file = src / "foo.py"
    source_file.write_text("from bar import Baz\n")

    state = _make_state()
    evidence1 = _make_evidence(
        tmp_path, source_file="src/foo.py", stderr_excerpt="error1 src/foo.py"
    )
    evidence2 = _make_evidence(
        tmp_path, source_file="src/foo.py", stderr_excerpt="error2 src/foo.py"
    )

    patch_json = _patch_json(
        patch_type="replace_in_file",
        path="src/foo.py",
        old="from bar import Baz",
        new="from baz import Baz",
    )

    # First attempt: rerun fails so it rolls back.
    _call(
        tmp_path,
        state,
        evidence1,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(1),
    )
    assert state.execution_recovery_attempts == 1
    # Restore the file (rollback should have done this).
    source_file.write_text("from bar import Baz\n")

    # Second attempt with the exact same patch (different failure sig so passes should_attempt).
    result = _call(
        tmp_path,
        state,
        evidence2,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "repeated_patch"
    failed = _events(tmp_path, EventType.EXECUTION_RECOVERY_FAILED)
    assert any(e["details"]["stop_reason"] == "repeated_patch" for e in failed)


# ---------------------------------------------------------------------------
# Rerun command nonzero fails
# ---------------------------------------------------------------------------


def test_rerun_nonzero_returns_failed(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    source_file = src / "foo.py"
    source_file.write_text("from bar import Baz\n")

    evidence = _make_evidence(tmp_path, source_file="src/foo.py")
    state = _make_state()

    patch_json = _patch_json(
        patch_type="replace_in_file",
        path="src/foo.py",
        old="from bar import Baz",
        new="from baz import Baz",
    )
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(2, stdout="", stderr="still broken"),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "rerun_still_failing"
    failed = _events(tmp_path, EventType.EXECUTION_RECOVERY_FAILED)
    assert failed[-1]["details"]["rerun_exit_code"] == 2
    # File should be rolled back.
    assert "from bar import Baz" in source_file.read_text()


# ---------------------------------------------------------------------------
# Validator rejection (test preservation after apply)
# ---------------------------------------------------------------------------


def test_post_apply_test_preservation_violated(tmp_path):
    """Patch writes an assertion-weakening pattern to a test file.

    scan_python_test_text catches skip markers in the 'new' content
    during validate_recovery_patch, so this fires BEFORE apply.
    Status should be failed with reason test_preservation_violated.
    """
    tests = tmp_path / "tests"
    tests.mkdir()
    test_file = tests / "test_bar.py"
    test_file.write_text("def test_something():\n    assert True\n")

    # Patch replaces the test body with assert True (which passes trivially).
    evidence = _make_evidence(
        tmp_path,
        source_file="tests/test_bar.py",
        failure_class="pytest_failure",
        traceback_excerpt="FAILED tests/test_bar.py::test_something",
        stderr_excerpt="tests/test_bar.py assertion error",
    )
    state = _make_state()

    # The 'new' content has a skip marker — caught by scan_python_test_text.
    patch_json = json.dumps(
        {
            "patch_type": "write_file",
            "path": "tests/test_bar.py",
            "new": "import pytest\n\n@pytest.mark.skip(reason='broken')\ndef test_something():\n    assert True\n",
            "rerun_command": "pytest tests/test_bar.py -x",
        }
    )
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm(patch_json),
        command_runner=_make_runner(0),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "test_preservation_violated"
    # File must not have been modified.
    assert "@pytest.mark.skip" not in test_file.read_text()


# ---------------------------------------------------------------------------
# Budget stops at 2
# ---------------------------------------------------------------------------


def test_budget_stops_at_two(tmp_path):
    src = tmp_path / "src"
    src.mkdir()

    state = _make_state()

    for i in range(3):
        f = src / f"file{i}.py"
        f.write_text("import x\n")
        evidence = _make_evidence(
            tmp_path,
            source_file=f"src/file{i}.py",
            stderr_excerpt=f"error in src/file{i}.py iteration {i}",
            traceback_excerpt=f"ImportError src/file{i}.py",
        )
        patch_json = _patch_json(
            patch_type="replace_in_file",
            path=f"src/file{i}.py",
            old="import x",
            new=f"import y_{i}",
        )
        result = _call(
            tmp_path,
            state,
            evidence,
            llm_callable=_make_llm(patch_json),
            command_runner=_make_runner(
                1
            ),  # always fails so budget consumed without success
        )
        if i < RECOVERY_BUDGET:
            assert result["status"] == "failed"
        else:
            assert result["status"] == "skipped"
            assert result["reason"] == "budget_exhausted"

    assert state.execution_recovery_attempts == RECOVERY_BUDGET


# ---------------------------------------------------------------------------
# Completion scope still disabled
# ---------------------------------------------------------------------------


def test_completion_scope_still_disabled(tmp_path):
    evidence = _make_evidence(
        tmp_path,
        failure_class="completion_validation_failed",
    )
    state = _make_state()

    result = ExecutionRecoveryService.attempt_recovery(
        project_dir=tmp_path,
        session_id=2,
        task_id=2,
        evidence=evidence,
        orchestration_state=state,
        scope="completion",
        step_index=None,
        llm_callable=_make_llm("{}"),  # would succeed if scope were enabled
        command_runner=_make_runner(0),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "completion_scope_disabled"

    succeeded = _events(tmp_path, EventType.EXECUTION_RECOVERY_SUCCEEDED)
    assert len(succeeded) == 0


# ---------------------------------------------------------------------------
# Existing ABORT path unchanged on recovery failure
# ---------------------------------------------------------------------------


def test_abort_path_unchanged_on_recovery_failure(tmp_path):
    """Recovery failure must return status='failed', not raise, not abort the process.

    The caller (execution_loop.py) checks result['status'] != 'success' and
    falls through to the existing ABORT logic. This test verifies the contract.
    """
    evidence = _make_evidence(tmp_path)
    state = _make_state()

    # LLM returns prose → recovery fails.
    result = _call(
        tmp_path,
        state,
        evidence,
        llm_callable=_make_llm("This is a helpful explanation but not JSON."),
        command_runner=_make_runner(0),
    )

    # Recovery returned a clean failure dict — caller can check and ABORT.
    assert result["status"] == "failed"
    assert "reason" in result
    assert result.get("status") != "success"

    # No SUCCEEDED event.
    succeeded = _events(tmp_path, EventType.EXECUTION_RECOVERY_SUCCEEDED)
    assert len(succeeded) == 0


# ---------------------------------------------------------------------------
# parse_recovery_patch unit tests
# ---------------------------------------------------------------------------


def test_parse_valid_replace_in_file():
    raw = json.dumps(
        {
            "patch_type": "replace_in_file",
            "path": "src/foo.py",
            "old": "old text",
            "new": "new text",
            "rerun_command": "pytest tests/",
        }
    )
    patch, err = parse_recovery_patch(raw)
    assert patch is not None
    assert err == ""
    assert patch.patch_type == "replace_in_file"
    assert patch.old == "old text"


def test_parse_valid_create_file():
    raw = json.dumps(
        {
            "patch_type": "create_file",
            "path": "src/new_module.py",
            "new": "class Foo:\n    pass\n",
            "rerun_command": "pytest tests/",
        }
    )
    patch, err = parse_recovery_patch(raw)
    assert patch is not None
    assert patch.patch_type == "create_file"


def test_parse_prose_returns_error():
    _, err = parse_recovery_patch(
        "The problem is an import error. Fix it by changing the path."
    )
    assert err != ""


def test_parse_markdown_fenced_json():
    raw = '```json\n{"patch_type": "write_file", "path": "x.py", "new": "y", "rerun_command": "pytest"}\n```'
    patch, err = parse_recovery_patch(raw)
    assert patch is not None
    assert patch.path == "x.py"


def test_parse_missing_old_for_replace():
    raw = json.dumps(
        {
            "patch_type": "replace_in_file",
            "path": "src/foo.py",
            "new": "new text",
            "rerun_command": "pytest",
        }
    )
    _, err = parse_recovery_patch(raw)
    assert err == "missing_old_for_replace"


# ---------------------------------------------------------------------------
# validate_recovery_patch unit tests
# ---------------------------------------------------------------------------


def test_validate_allowed_command(tmp_path):
    evidence = _make_evidence(tmp_path)
    patch = RecoveryPatch(
        "replace_in_file", "src/foo.py", "old", "new", "pytest tests/test_foo.py -x"
    )
    ok, _ = validate_recovery_patch(patch, evidence, tmp_path)
    assert ok is True


def test_validate_disallowed_command(tmp_path):
    evidence = _make_evidence(tmp_path)
    patch = RecoveryPatch(
        "replace_in_file", "src/foo.py", "old", "new", "bash -c 'rm -rf /'"
    )
    ok, reason = validate_recovery_patch(patch, evidence, tmp_path)
    assert ok is False
    assert reason in ("disallowed_rerun_command", "dangerous_rerun_command")


def test_validate_path_traversal_rejected(tmp_path):
    evidence = _make_evidence(tmp_path)
    patch = RecoveryPatch(
        "create_file", "../../../etc/cron.d/evil", "", "content", "pytest"
    )
    ok, reason = validate_recovery_patch(patch, evidence, tmp_path)
    assert ok is False


def test_validate_scope_via_traceback(tmp_path):
    """A file not in changed_files but mentioned in the traceback is in scope."""
    evidence = _make_evidence(
        tmp_path,
        changed_files=["src/other.py"],
        traceback_excerpt="ImportError in src/foo.py line 5",
        stderr_excerpt="src/foo.py ImportError",
    )
    patch = RecoveryPatch(
        "replace_in_file", "src/foo.py", "old", "new", "pytest tests/"
    )
    ok, reason = validate_recovery_patch(patch, evidence, tmp_path)
    assert ok is True, f"Expected valid, got reason={reason}"


# ---------------------------------------------------------------------------
# apply_recovery_patch unit tests
# ---------------------------------------------------------------------------


def test_apply_replace_in_file(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\ny = 2\n")
    patch = RecoveryPatch("replace_in_file", "mod.py", "x = 1", "x = 10", "pytest")
    ok, err, rollback = apply_recovery_patch(patch, tmp_path)
    assert ok
    assert "x = 10" in f.read_text()
    rollback()
    assert "x = 1" in f.read_text()
    assert "x = 10" not in f.read_text()


def test_apply_replace_old_text_not_found(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    patch = RecoveryPatch("replace_in_file", "mod.py", "z = 99", "z = 100", "pytest")
    ok, err, _ = apply_recovery_patch(patch, tmp_path)
    assert not ok
    assert err == "old_text_not_found"


def test_apply_create_file(tmp_path):
    patch = RecoveryPatch(
        "create_file", "new_pkg/utils.py", "", "def fn(): pass\n", "pytest"
    )
    ok, err, rollback = apply_recovery_patch(patch, tmp_path)
    assert ok
    assert (tmp_path / "new_pkg" / "utils.py").exists()
    rollback()
    assert not (tmp_path / "new_pkg" / "utils.py").exists()


def test_apply_create_file_already_exists(tmp_path):
    f = tmp_path / "existing.py"
    f.write_text("existing content\n")
    patch = RecoveryPatch("create_file", "existing.py", "", "new content\n", "pytest")
    ok, err, _ = apply_recovery_patch(patch, tmp_path)
    assert not ok
    assert err == "file_already_exists"


# ---------------------------------------------------------------------------
# build_recovery_prompt smoke test
# ---------------------------------------------------------------------------


def test_build_recovery_prompt_contains_key_fields(tmp_path):
    evidence = _make_evidence(tmp_path)
    prompt = build_recovery_prompt(evidence)
    assert "import_error" in prompt
    assert "pytest tests/test_foo.py -x" in prompt
    assert "src/foo.py" in prompt
    assert "patch_type" in prompt
    assert "rerun_command" in prompt


def test_build_recovery_prompt_with_requested_symbols(tmp_path):
    evidence = _make_evidence(
        tmp_path,
        requested_symbols=["MyClass", "MyHelper"],
        failure_class="missing_requested_symbol",
    )
    prompt = build_recovery_prompt(evidence)
    assert "MyClass" in prompt
    assert "MyHelper" in prompt
