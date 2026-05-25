from __future__ import annotations

import logging

import pytest

from app.services.orchestration.validation.workspace_guard import (
    TaskWorkspaceViolationError,
    compute_workspace_checksum,
    detect_scope_violations,
    is_workspace_cd_escape,
    normalize_command,
    normalize_step,
)


def test_normalize_command_allows_dev_null_redirection(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    command = (
        "cat backend/package.json | python3 -m json.tool > /dev/null "
        "&& test -f backend/src/index.ts && echo 'Backend foundation verified'"
    )

    normalized = normalize_command(command, project_dir)

    assert "/dev/null" in normalized


def test_normalize_step_allows_verification_commands_that_sink_to_dev_null(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    logger = logging.getLogger("workspace-guard-test")

    step = {
        "step_number": 2,
        "description": "Verify backend foundation",
        "commands": [
            "mkdir -p backend/src && touch backend/src/index.ts backend/package.json"
        ],
        "verification": (
            "cat backend/package.json | python3 -m json.tool > /dev/null "
            "&& test -f backend/src/index.ts && echo 'Backend foundation verified'"
        ),
        "rollback": "rm -rf backend",
        "expected_files": ["backend/package.json", "backend/src/index.ts"],
    }

    normalized = normalize_step(step, project_dir, logger, step_index=2)

    assert normalized["verification"] is not None
    assert "/dev/null" in normalized["verification"]


def test_normalize_write_pseudo_command_allows_frontend_root_asset_literal(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    command = (
        "write frontend/index.html: HTML shell with root div and Vite module script "
        "entry pointing to /src/main.tsx"
    )

    normalized = normalize_command(command, project_dir)

    assert normalized.startswith("write frontend/index.html:")
    assert "/src/main.tsx" in normalized


def test_normalize_write_pseudo_command_allows_http_route_literals(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    command = (
        "write apps/backend/src/index.ts: minimal Express server that listens on PORT "
        "env var (default 3000) with a health-check GET /health returning {status: 'ok'}"
    )

    normalized = normalize_command(command, project_dir)

    assert normalized.startswith("write apps/backend/src/index.ts:")
    assert "GET /health" in normalized


def test_normalize_step_coerces_verification_and_rollback_lists(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    logger = logging.getLogger("workspace-guard-test")

    step = {
        "step_number": 3,
        "description": "Verify frontend setup",
        "commands": ["mkdir -p frontend/src && touch frontend/src/main.tsx"],
        "verification": ["test -f frontend/src/main.tsx", "echo ready"],
        "rollback": ["rm -f frontend/src/main.tsx", "rmdir frontend/src || true"],
        "expected_files": ["frontend/src/main.tsx"],
    }

    normalized = normalize_step(step, project_dir, logger, step_index=3)

    assert normalized["verification"] == "test -f frontend/src/main.tsx && echo ready"
    assert (
        normalized["rollback"]
        == "( rm -f frontend/src/main.tsx ) && ( rmdir frontend/src || true )"
    )


def test_normalize_step_strips_transient_expected_files(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    logger = logging.getLogger("workspace-guard-test")

    step = {
        "step_number": 4,
        "description": "Verify dev setup",
        "commands": ["echo ok"],
        "verification": "echo ok",
        "rollback": None,
        "expected_files": [
            "app/main.py",
            ".venv/bin/python",
            "frontend/dist/index.html",
            "frontend/src/main.tsx",
        ],
    }

    normalized = normalize_step(step, project_dir, logger, step_index=4)

    assert normalized["expected_files"] == ["app/main.py", "frontend/src/main.tsx"]


def test_normalize_step_preserves_root_for_each_list_verification_command(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "frontend").mkdir(parents=True)
    (project_dir / "backend").mkdir(parents=True)
    logger = logging.getLogger("workspace-guard-test")

    step = {
        "step_number": 5,
        "description": "Verify both TypeScript workspaces",
        "commands": ["echo ok"],
        "verification": [
            "cd frontend && npx tsc --noEmit",
            "cd backend && npx tsc --noEmit",
            "echo 'Both TS checks passed'",
        ],
        "rollback": None,
        "expected_files": ["frontend/src/main.tsx", "backend/src/index.ts"],
    }

    normalized = normalize_step(step, project_dir, logger, step_index=5)

    assert normalized["verification"] == (
        "( cd frontend && npx tsc --noEmit ) && "
        "( cd backend && npx tsc --noEmit ) && "
        "( echo 'Both TS checks passed' )"
    )


def test_normalize_command_repairs_workspace_relative_cd_target(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "service").mkdir(parents=True)

    normalized = normalize_command("cd ../service && npm install", project_dir)

    assert normalized == "cd service && npm install"


def test_normalize_command_allows_return_to_workspace_root_after_child_cd(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "frontend").mkdir(parents=True)

    normalized = normalize_command(
        "cd frontend && rm -rf node_modules package-lock.json && cd .. && rm -rf frontend",
        project_dir,
    )

    assert (
        normalized
        == "cd frontend && rm -rf node_modules package-lock.json && rm -rf frontend"
    )


def test_workspace_cd_escape_flags_workspace_relative_fragment(tmp_path):
    project_dir = tmp_path / "vault" / "projects" / "phase-7o-next"
    project_dir.mkdir(parents=True)

    command = "cd vault/projects/phase-7o-next/backend && python3 -m pytest -v"

    assert is_workspace_cd_escape(command, project_dir)
    with pytest.raises(TaskWorkspaceViolationError, match="workspace path"):
        normalize_command(command, project_dir)


def test_workspace_cd_escape_flags_absolute_workspace_path(tmp_path):
    project_dir = tmp_path / "vault" / "projects" / "phase-7o-next"
    project_dir.mkdir(parents=True)

    command = f"cd {project_dir}/backend && python3 -m pytest -v"

    assert is_workspace_cd_escape(command, project_dir)
    with pytest.raises(TaskWorkspaceViolationError, match="workspace path"):
        normalize_command(command, project_dir)


def test_workspace_cd_escape_allows_legitimate_relative_subdir(tmp_path):
    project_dir = tmp_path / "vault" / "projects" / "phase-7o-next"
    (project_dir / "src").mkdir(parents=True)

    command = "cd src && python3 -m pytest -v"

    assert not is_workspace_cd_escape(command, project_dir)
    assert normalize_command(command, project_dir) == command


def test_normalize_command_rejects_malformed_shell_quoting(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    with pytest.raises(TaskWorkspaceViolationError, match="malformed shell quoting"):
        normalize_command("echo 'unterminated", project_dir)


def test_normalize_command_repairs_unclosed_python_c_outer_quote(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    command = (
        'python -c "import importlib.util; '
        "spec = importlib.util.spec_from_file_location('app_config', 'app_config.py'); "
        "mod = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(mod); "
        "assert mod.FEATURE_FLAG == True, 'FEATURE_FLAG is not True'"
    )

    normalized = normalize_command(command, project_dir)

    assert normalized == f'{command}"'


def test_normalize_command_accepts_python_heredoc(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    command = """python3 - <<'PY'
import sys
print("Python", sys.version)
PY"""

    normalized = normalize_command(command, project_dir)

    assert normalized == command


def test_normalize_command_allows_quoted_sed_address_literals(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    command = (
        "sed -i '/--uppercase/d' src/small_cli/cli.py; "
        "sed -i '/if uppercase:/d' src/small_cli/cli.py; "
        "sed -i '/message = message.upper()/d' src/small_cli/cli.py"
    )

    normalized = normalize_command(command, project_dir)

    assert normalized == command


def test_normalize_command_still_rejects_sed_absolute_file_operand(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    with pytest.raises(TaskWorkspaceViolationError, match="Path escapes"):
        normalize_command("sed -i 's/a/b/' '/tmp/outside.py'", project_dir)


def test_normalize_command_allows_tilde_inside_jsx_content(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    command = (
        "printf 'export default function App() { return <main>Save ~20% tonight</main>; }\\n' "
        "> src/App.jsx"
    )

    normalized = normalize_command(command, project_dir)

    assert "Save ~20%" in normalized


def test_normalize_command_rejects_home_directory_path_token(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    with pytest.raises(TaskWorkspaceViolationError, match="Home-directory paths"):
        normalize_command("rm -rf ~/.cache/openclaw", project_dir)


def test_scope_audit_ignores_pytest_cache_side_effects(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    pre_checksum = compute_workspace_checksum(project_dir)

    cache_file = project_dir / ".pytest_cache" / "v" / "cache" / "nodeids"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text('["tests/test_config.py::test_feature_flag_is_true"]\n')

    assert detect_scope_violations(project_dir, [], pre_checksum) == []


def test_scope_audit_still_flags_unexpected_non_cache_files(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    pre_checksum = compute_workspace_checksum(project_dir)

    unexpected = project_dir / "scratch" / "debug.txt"
    unexpected.parent.mkdir(parents=True)
    unexpected.write_text("debug output\n")

    assert detect_scope_violations(project_dir, [], pre_checksum) == [
        "scratch/debug.txt"
    ]
