"""Phase 22C-0 -- OpenClaw Runtime Containment regression tests.

Covers the five bounded containment goals: fail-closed agent selection, the
git-mutation containment shim, runtime-pollution detection, the strengthened
completion-evidence guard, and the diagnostics fields added to support future
investigation.
"""

import json
import os
import stat
import subprocess
import time

import pytest

from app.services.agents.openclaw_service import (
    OpenClawAgentSelectionError,
    OpenClawSessionService,
)
from app.services.orchestration.validation.git_containment_guard import (
    BLOCKED_GIT_MUTATION_SUBCOMMANDS,
    build_git_containment_env,
    cleanup_git_containment_shim,
)
from app.services.orchestration.validation.runtime_pollution_guard import (
    detect_runtime_pollution,
    existing_known_scaffold_entries,
    snapshot_top_level_entries,
)
from app.services.orchestration.validation.workspace_guard import (
    has_recent_file_activity,
)


# ---------------------------------------------------------------------------
# Goal 1: fail closed when no OpenClaw agent matches the resolved workspace
# ---------------------------------------------------------------------------


def test_no_matching_agent_fails_closed(monkeypatch, tmp_path):
    project_root = tmp_path / "vault" / "projects" / "unregistered-project"
    project_root.mkdir(parents=True)
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {"id": "main", "workspace": str(tmp_path / "workspace")},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    service = object.__new__(OpenClawSessionService)
    logged = []
    service._log_entry = lambda level, message, **kwargs: logged.append(
        (level, message)
    )

    with pytest.raises(OpenClawAgentSelectionError) as exc_info:
        service._build_openclaw_agent_command(["openclaw"], cwd=str(project_root))

    assert str(project_root) in str(exc_info.value)
    assert any(level == "ERROR" for level, _ in logged)


def test_matching_agent_still_selected(monkeypatch, tmp_path):
    project_root = tmp_path / "vault" / "projects" / "orchestrator"
    project_root.mkdir(parents=True)
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {"id": "orchestrator", "workspace": str(project_root)},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    service = object.__new__(OpenClawSessionService)
    result = service._build_openclaw_agent_command(["openclaw"], cwd=str(project_root))

    assert result == ["openclaw", "agent", "--agent", "orchestrator"]
    assert service._last_selected_openclaw_agent_id == "orchestrator"


def test_no_cwd_does_not_fail_closed(monkeypatch, tmp_path):
    """Planning-only calls with no resolved project cwd keep the old lenient
    behavior -- there is no real workspace at stake to fail closed over."""

    config_path = tmp_path / "openclaw.json"
    config_path.write_text(json.dumps({"agents": {"list": []}}), encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    service = object.__new__(OpenClawSessionService)
    result = service._build_openclaw_agent_command(["openclaw"], cwd=None)

    assert result == ["openclaw", "agent"]


def test_strict_v2_no_cwd_fails_closed_before_default_agent(monkeypatch, tmp_path):
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(json.dumps({"agents": {"list": []}}), encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    service = object.__new__(OpenClawSessionService)
    service._log_entry = lambda level, message, **kwargs: None

    with pytest.raises(OpenClawAgentSelectionError):
        service._build_openclaw_agent_command(
            ["openclaw"], cwd=None, strict_provider_result=True
        )


def test_strict_v2_explicit_agent_is_selected(monkeypatch, tmp_path):
    project_root = tmp_path / "planning-agent"
    project_root.mkdir()
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {"id": "planning", "workspace": str(project_root)},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    service = object.__new__(OpenClawSessionService)
    result = service._build_openclaw_agent_command(
        ["openclaw"], cwd=str(project_root), strict_provider_result=True
    )

    assert result == ["openclaw", "agent", "--agent", "planning"]


def test_strict_v2_no_matching_agent_fails_closed(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {"id": "other", "workspace": str(tmp_path / "other")},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))
    service = object.__new__(OpenClawSessionService)
    service._log_entry = lambda level, message, **kwargs: None

    with pytest.raises(OpenClawAgentSelectionError):
        service._build_openclaw_agent_command(
            ["openclaw"], cwd=str(project_root), strict_provider_result=True
        )


def test_strict_provider_controls_are_written_to_ephemeral_config(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {"model": "openai/qwen-local"},
                    "list": [
                        {
                            "id": "planning",
                            "workspace": str(tmp_path),
                            "model": "openai/qwen-local",
                        }
                    ],
                },
                "models": {
                    "providers": {
                        "openai": {"models": [{"id": "qwen-local", "reasoning": True}]}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))
    service = object.__new__(OpenClawSessionService)

    controls = service._configure_strict_provider_controls("planning")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    agent = config["agents"]["list"][0]
    model = config["models"]["providers"]["openai"]["models"][0]

    assert agent["params"] == {"temperature": 0, "maxTokens": 16_384}
    assert model["compat"]["thinkingFormat"] == "qwen-chat-template"
    assert controls["reasoning_request_parameter"].endswith("=false")
    assert controls["temperature"] == 0
    assert controls["top_p"]["configured"] is False


def test_explicit_dedicated_planning_agent_binds_without_project_match(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {"protocolV2PlanningAgent": "planning"},
                    "list": [
                        {
                            "id": "planning",
                            "workspace": str(tmp_path / "dedicated"),
                            "model": "openai/qwen-local",
                        }
                    ],
                },
                "models": {
                    "providers": {
                        "openai": {"models": [{"id": "qwen-local", "reasoning": True}]}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    service = object.__new__(OpenClawSessionService)
    service._openclaw_config_path_override = None
    service._strict_planning_config_dir = None
    service._last_selected_openclaw_agent_id = None
    service._workspace_binding = None
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    runtime_workspace = tmp_path / "runtime"
    runtime_workspace.mkdir()
    service._bind_dedicated_strict_planning_agent(runtime_workspace, "planning")
    try:
        assert (
            service._find_openclaw_agent_for_workspace(str(runtime_workspace))
            == "planning"
        )
        assert service._last_selected_openclaw_agent_id == "planning"
        assert service._openclaw_config_path_override != config_path
    finally:
        service._release_dedicated_strict_planning_agent()


# ---------------------------------------------------------------------------
# Goal 2: git-mutation containment shim
# ---------------------------------------------------------------------------


def _run_with_shim(shim_dir, args, cwd):
    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_git_shim_blocks_commit(git_repo):
    env, shim_dir = build_git_containment_env()
    try:
        assert shim_dir is not None
        (git_repo / "new.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=git_repo, env=env, check=True)
        result = subprocess.run(
            ["git", "commit", "-m", "unauthorized"],
            cwd=git_repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "blocked" in (result.stdout + result.stderr).lower()

        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
        ).stdout.strip()
        assert head_before  # commit did not create a new HEAD
    finally:
        cleanup_git_containment_shim(shim_dir)


@pytest.mark.parametrize("subcommand", BLOCKED_GIT_MUTATION_SUBCOMMANDS)
def test_git_shim_blocks_all_listed_mutation_subcommands(git_repo, subcommand):
    env, shim_dir = build_git_containment_env()
    try:
        assert shim_dir is not None
        result = subprocess.run(
            ["git", subcommand],
            cwd=git_repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "blocked" in (result.stdout + result.stderr).lower()
    finally:
        cleanup_git_containment_shim(shim_dir)


def test_git_shim_blocks_branch_delete_but_allows_branch_list(git_repo):
    env, shim_dir = build_git_containment_env()
    try:
        subprocess.run(
            ["git", "branch", "feature-x"], cwd=git_repo, env=env, check=True
        )
        listing = subprocess.run(
            ["git", "branch"], cwd=git_repo, env=env, capture_output=True, text=True
        )
        assert listing.returncode == 0
        assert "feature-x" in listing.stdout

        delete = subprocess.run(
            ["git", "branch", "-D", "feature-x"],
            cwd=git_repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert delete.returncode != 0
        assert "blocked" in (delete.stdout + delete.stderr).lower()
    finally:
        cleanup_git_containment_shim(shim_dir)


def test_git_shim_allows_read_only_commands(git_repo):
    env, shim_dir = build_git_containment_env()
    try:
        for args in (["status"], ["log", "--oneline"], ["diff"], ["show", "HEAD"]):
            result = subprocess.run(
                ["git", *args],
                cwd=git_repo,
                env=env,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, (args, result.stderr)
    finally:
        cleanup_git_containment_shim(shim_dir)


def test_git_shim_allows_config_read_blocks_config_write(git_repo):
    env, shim_dir = build_git_containment_env()
    try:
        read = subprocess.run(
            ["git", "config", "--get", "user.name"],
            cwd=git_repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert read.returncode == 0

        write = subprocess.run(
            ["git", "config", "user.email", "attacker@example.com"],
            cwd=git_repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert write.returncode != 0
        assert "blocked" in (write.stdout + write.stderr).lower()
    finally:
        cleanup_git_containment_shim(shim_dir)


def test_git_shim_dir_is_cleaned_up():
    env, shim_dir = build_git_containment_env()
    assert shim_dir is not None
    assert shim_dir.exists()
    cleanup_git_containment_shim(shim_dir)
    assert not shim_dir.exists()


def test_git_shim_missing_git_binary_is_non_fatal(monkeypatch):
    monkeypatch.setattr(
        "app.services.orchestration.validation.git_containment_guard.shutil.which",
        lambda *a, **k: None,
    )
    env, shim_dir = build_git_containment_env(base_env={"PATH": "/nonexistent"})
    assert shim_dir is None
    assert env["PATH"] == "/nonexistent"


# ---------------------------------------------------------------------------
# Goal 3: runtime pollution detection (not solely a filename blacklist)
# ---------------------------------------------------------------------------


def test_pollution_detects_known_scaffold_and_unknown_new_entries(tmp_path):
    before = snapshot_top_level_entries(tmp_path)
    (tmp_path / "SOUL.md").write_text("x", encoding="utf-8")
    (tmp_path / "SOME_NEW_UNKNOWN_SCAFFOLD.dat").write_text("x", encoding="utf-8")
    after = snapshot_top_level_entries(tmp_path)

    result = detect_runtime_pollution(before=before, after=after)

    assert result["pollution_detected"] is True
    assert "SOUL.md" in result["known_scaffold_matches"]
    assert "SOME_NEW_UNKNOWN_SCAFFOLD.dat" in result["unclassified_new_entries"]
    assert "SOME_NEW_UNKNOWN_SCAFFOLD.dat" not in result["known_scaffold_matches"]


def test_pollution_detection_is_diff_based_not_blacklist_only(tmp_path):
    """A file OpenClaw has never named before still gets flagged as new --
    detection does not depend on the known-scaffold list."""

    before = snapshot_top_level_entries(tmp_path)
    (tmp_path / "NEVER_SEEN_BEFORE.md").write_text("x", encoding="utf-8")
    after = snapshot_top_level_entries(tmp_path)

    result = detect_runtime_pollution(before=before, after=after)
    assert result["pollution_detected"] is True
    assert result["known_scaffold_matches"] == []
    assert result["unclassified_new_entries"] == ["NEVER_SEEN_BEFORE.md"]


def test_pollution_no_new_entries_is_clean(tmp_path):
    (tmp_path / "existing.txt").write_text("x", encoding="utf-8")
    before = snapshot_top_level_entries(tmp_path)
    after = snapshot_top_level_entries(tmp_path)

    result = detect_runtime_pollution(before=before, after=after)
    assert result["pollution_detected"] is False
    assert result["new_top_level_entries"] == []


def test_existing_scaffold_entries_detected_even_without_a_diff(tmp_path):
    """Scaffold files persist across runs (hydration/.gitignore hides them
    from git, it does not delete them); a same-run diff alone would miss
    already-present pollution, so presence is reported independently."""

    (tmp_path / "USER.md").write_text("x", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("x", encoding="utf-8")

    assert existing_known_scaffold_entries(tmp_path) == ["USER.md"]


# ---------------------------------------------------------------------------
# Goal 4: completion validation must not treat missing evidence as success
# ---------------------------------------------------------------------------


def test_workspace_guard_fails_closed_when_no_evidence_at_all(tmp_path):
    service = object.__new__(OpenClawSessionService)
    logged = []
    service._log_entry = lambda level, message, **kwargs: logged.append(
        (level, message)
    )

    started_at = time.time() + 1  # nothing in tmp_path is newer than "now + 1s"
    result = service._apply_reported_workspace_guard(
        {"status": "completed", "output": "done"},
        reported_workspace_dir=None,
        expected_project_root=str(tmp_path),
        execution_started_at_epoch=started_at,
    )

    assert result["status"] == "failed"
    assert result["workspace_contract_failed"] is True
    assert result["workspace_evidence_missing"] is True
    assert any(level == "ERROR" for level, _ in logged)


def test_workspace_guard_accepts_file_activity_fallback_when_dir_missing(tmp_path):
    service = object.__new__(OpenClawSessionService)
    service._log_entry = lambda level, message, **kwargs: None

    started_at = time.time() - 5
    (tmp_path / "written_by_task.py").write_text("print(1)\n", encoding="utf-8")

    result = service._apply_reported_workspace_guard(
        {"status": "completed", "output": "done"},
        reported_workspace_dir=None,
        expected_project_root=str(tmp_path),
        execution_started_at_epoch=started_at,
    )

    assert result["status"] == "completed"
    assert result["workspace_evidence_source"] == "file_activity_fallback"


def test_workspace_guard_still_rejects_reported_dir_outside_root(tmp_path):
    """The existing fail-closed branch (reported dir present but wrong) is
    unchanged by this phase -- only the missing-evidence branch is new."""

    service = object.__new__(OpenClawSessionService)
    service._log_entry = lambda level, message, **kwargs: None
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    root = tmp_path / "project"
    root.mkdir()

    result = service._apply_reported_workspace_guard(
        {"status": "completed", "output": "done"},
        reported_workspace_dir=str(outside),
        expected_project_root=str(root),
    )

    assert result["status"] == "failed"
    assert result["workspace_contract_failed"] is True


def test_workspace_guard_passes_through_non_completed_status(tmp_path):
    service = object.__new__(OpenClawSessionService)
    service._log_entry = lambda level, message, **kwargs: None

    result = service._apply_reported_workspace_guard(
        {"status": "failed", "error": "boom"},
        reported_workspace_dir=None,
        expected_project_root=str(tmp_path),
        execution_started_at_epoch=time.time(),
    )

    assert result["status"] == "failed"
    assert result["error"] == "boom"
    assert "workspace_evidence_missing" not in result


def test_has_recent_file_activity(tmp_path):
    started_at = time.time()
    assert has_recent_file_activity(tmp_path, started_at + 5) is False
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    assert has_recent_file_activity(tmp_path, started_at - 5) is True


def test_has_recent_file_activity_prunes_ignored_dirs(tmp_path):
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    started_at = time.time() - 5
    (ignored / "just_installed.js").write_text("x", encoding="utf-8")

    assert has_recent_file_activity(tmp_path, started_at) is False


# ---------------------------------------------------------------------------
# Goal 5: diagnostics
# ---------------------------------------------------------------------------


def test_invocation_metadata_includes_phase22c0_diagnostic_fields():
    metadata = OpenClawSessionService._openclaw_invocation_metadata(
        full_cmd=["openclaw", "agent", "--agent", "orchestrator", "--message", "hi"],
        prompt="hi",
        timeout_seconds=60,
        cwd="/some/project/root",
        invocation_kind="execution",
        expected_project_root="/some/project/root",
        openclaw_version="openclaw 1.2.3",
        git_containment_active=True,
    )

    assert metadata["selected_agent"] == "orchestrator"
    assert metadata["expected_project_root"] == "/some/project/root"
    assert metadata["openclaw_version"] == "openclaw 1.2.3"
    assert metadata["git_containment_active"] is True


def test_openclaw_version_resolution_is_cached(monkeypatch):
    service = object.__new__(OpenClawSessionService)
    service._resolve_openclaw_command = lambda: ["/bin/echo", "openclaw"]

    call_count = {"n": 0}
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        call_count["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(
        "app.services.agents.openclaw_service.subprocess.run", counting_run
    )
    monkeypatch.setattr(
        "app.services.agents.openclaw_service._OPENCLAW_VERSION_CACHE", {}
    )

    first = service._resolve_openclaw_cli_version()
    second = service._resolve_openclaw_cli_version()

    assert first == second
    assert call_count["n"] == 1
