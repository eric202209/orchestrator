import json
from pathlib import Path

from app.models import LogEntry, Project, Session as SessionModel
from app.services.orchestration.policy import (
    get_policy_profile,
    should_restore_workspace_on_failure,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.workspace.system_settings import (
    ADAPTATION_PROFILE_KEY,
    AGENT_BACKEND_KEY,
    LEGACY_WORKSPACE_ROOT_KEY,
    ORCHESTRATION_POLICY_PROFILE_KEY,
    WORKSPACE_REVIEW_POLICY_KEY,
    WORKSPACE_ROOT_KEY,
    get_effective_workspace_root,
    get_setting_value,
    set_setting_value,
)


def test_settings_can_persist_operator_backend_and_policy_profile(
    authenticated_client, db_session
):
    set_setting_value(db_session, AGENT_BACKEND_KEY, "local_openclaw")
    set_setting_value(db_session, ADAPTATION_PROFILE_KEY, "openclaw_default")
    set_setting_value(db_session, ORCHESTRATION_POLICY_PROFILE_KEY, "balanced")
    set_setting_value(db_session, WORKSPACE_REVIEW_POLICY_KEY, "hold_nontrivial")

    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "local_openclaw",
            "agent_adaptation_profile": "openclaw_default",
            "orchestration_policy_profile": "strict",
            "workspace_review_policy": "hold_all",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["system"]["agent_backend"] == "local_openclaw"
    assert payload["system"]["agent_adaptation_profile"] == "openclaw_default"
    assert payload["system"]["orchestration_policy_profile"] == "strict"
    assert payload["system"]["workspace_review_policy"] == "hold_all"
    assert payload["system"]["supported_backends"]
    assert payload["system"]["available_policy_profiles"]
    assert payload["system"]["available_adaptation_profiles"]
    assert "status" in payload["system"]["backend_health"]

    audit_entry = (
        db_session.query(LogEntry)
        .filter(LogEntry.message.like("System settings updated by%"))
        .order_by(LogEntry.id.desc())
        .first()
    )
    assert audit_entry is not None
    metadata = json.loads(audit_entry.log_metadata or "{}")
    assert metadata["event_type"] == "system_settings_updated"
    assert metadata["changes"]["orchestration_policy_profile"]["to"] == "strict"
    assert metadata["changes"]["workspace_review_policy"]["to"] == "hold_all"


def test_settings_reject_unknown_workspace_review_policy(authenticated_client):
    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "workspace_review_policy": "always_merge",
        },
    )

    assert response.status_code == 422


def test_workspace_root_save_keeps_legacy_workspace_key_in_sync(db_session):
    set_setting_value(db_session, WORKSPACE_ROOT_KEY, "/app/projects")

    assert get_setting_value(db_session, WORKSPACE_ROOT_KEY) == "/app/projects"
    assert get_setting_value(db_session, LEGACY_WORKSPACE_ROOT_KEY) == "/app/projects"


def test_host_runtime_uses_env_workspace_root_for_container_setting(
    db_session, monkeypatch, tmp_path
):
    import app.services.workspace.system_settings as system_settings

    env_projects = tmp_path / "device-projects"
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("HOST_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(env_projects))
    monkeypatch.setattr(system_settings, "_running_in_container", lambda: False)
    set_setting_value(db_session, WORKSPACE_ROOT_KEY, "/app/projects")

    assert get_effective_workspace_root(db=db_session) == env_projects.resolve()


def test_host_runtime_prefers_explicit_host_workspace_for_container_root(
    db_session, monkeypatch, tmp_path
):
    import app.services.workspace.system_settings as system_settings

    host_projects = tmp_path / "host-projects"
    monkeypatch.setenv("HOST_WORKSPACE_ROOT", str(host_projects))
    monkeypatch.setattr(system_settings, "_running_in_container", lambda: False)
    set_setting_value(db_session, WORKSPACE_ROOT_KEY, "/app/projects")

    assert get_effective_workspace_root(db=db_session) == host_projects.resolve()


def test_host_runtime_prefers_workspace_root_before_openclaw_workspace(
    db_session, monkeypatch, tmp_path
):
    import app.services.workspace.system_settings as system_settings

    workspace_root = tmp_path / "workspace-root"
    openclaw_workspace = tmp_path / "openclaw-workspace"
    monkeypatch.delenv("HOST_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(openclaw_workspace))
    monkeypatch.setattr(system_settings, "_running_in_container", lambda: False)
    set_setting_value(db_session, WORKSPACE_ROOT_KEY, "/app/projects")

    assert get_effective_workspace_root(db=db_session) == workspace_root.resolve()


def test_container_runtime_keeps_container_workspace_root(db_session, monkeypatch):
    import app.services.workspace.system_settings as system_settings

    monkeypatch.setenv("HOST_WORKSPACE_ROOT", "/home/eric/projects")
    monkeypatch.setenv("WORKSPACE_ROOT", "/home/eric/other-projects")
    monkeypatch.setenv("OPENCLAW_WORKSPACE", "/home/eric/openclaw-projects")
    monkeypatch.setattr(system_settings, "_running_in_container", lambda: True)
    set_setting_value(db_session, WORKSPACE_ROOT_KEY, "/app/projects")

    assert get_effective_workspace_root(db=db_session) == Path("/app/projects")


def test_settings_reject_mismatched_backend_and_adaptation_profile(
    authenticated_client,
):
    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "openai_responses_api",
            "agent_adaptation_profile": "claude_strict_tools",
        },
    )

    assert response.status_code == 200
    assert (
        response.json()["system"]["agent_adaptation_profile"]
        == "openai_responses_default"
    )


def test_direct_ollama_save_normalizes_stale_openclaw_adaptation_profile(
    authenticated_client, db_session
):
    set_setting_value(db_session, AGENT_BACKEND_KEY, "direct_ollama")
    set_setting_value(db_session, WORKSPACE_ROOT_KEY, "/app/projects")

    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "direct_ollama",
            "workspace_root": "/app/projects",
            "agent_adaptation_profile": "openclaw_default",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["system"]["agent_backend"] == "direct_ollama"
    assert payload["system"]["agent_adaptation_profile"] == "ollama_default"


def test_settings_reject_windows_host_path_as_container_workspace_root(
    authenticated_client, db_session
):
    set_setting_value(db_session, AGENT_BACKEND_KEY, "direct_ollama")

    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "direct_ollama",
            "workspace_root": r"C:\Users\Example\Documents\Projects",
        },
    )

    assert response.status_code == 400
    assert "WORKSPACE_ROOT" in response.json()["detail"]


def test_settings_can_select_openai_backend(authenticated_client):
    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "openai_responses_api",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["system"]["agent_backend"] == "openai_responses_api"
    assert payload["system"]["agent_adaptation_profile"] == "openai_responses_default"


def test_settings_can_select_custom_direct_ollama_model(
    authenticated_client, db_session
):
    set_setting_value(db_session, WORKSPACE_ROOT_KEY, "/app/projects")

    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "direct_ollama",
            "workspace_root": "/app/projects",
            "agent_model_family": "deepseek-coder-v2:16b",
            "agent_adaptation_profile": "ollama_default",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["system"]["agent_backend"] == "direct_ollama"
    assert payload["system"]["agent_model_family"] == "deepseek-coder-v2:16b"


def test_settings_direct_ollama_default_model_comes_from_ollama_env(
    authenticated_client, db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "OLLAMA_AGENT_MODEL", "operator-default:latest")
    set_setting_value(db_session, WORKSPACE_ROOT_KEY, "/app/projects")

    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "direct_ollama",
            "workspace_root": "/app/projects",
            "agent_adaptation_profile": "ollama_default",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["system"]["agent_model_family"] == "operator-default:latest"


def test_direct_ollama_rejects_openclaw_default_workspace(
    authenticated_client, db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(
        settings, "OLLAMA_BASE_URL", "http://host.docker.internal:11434"
    )
    set_setting_value(
        db_session,
        WORKSPACE_ROOT_KEY,
        "/home/operator/.openclaw/workspace/vault/projects",
    )

    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "direct_ollama",
            "workspace_root": "/home/operator/.openclaw/workspace/vault/projects",
        },
    )

    assert response.status_code == 400
    assert "direct_ollama" in response.json()["detail"]
    assert "/app/projects" in response.json()["detail"]


def test_direct_ollama_allows_openclaw_default_workspace_for_native_ubuntu(
    authenticated_client, db_session, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434")
    set_setting_value(
        db_session,
        WORKSPACE_ROOT_KEY,
        "/home/operator/.openclaw/workspace/vault/projects",
    )

    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "direct_ollama",
            "workspace_root": "/home/operator/.openclaw/workspace/vault/projects",
            "agent_adaptation_profile": "ollama_default",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["system"]["agent_backend"] == "direct_ollama"
    assert (
        payload["system"]["workspace_root"]
        .replace("\\", "/")
        .endswith("/.openclaw/workspace/vault/projects")
    )


def test_checkpoint_inspection_returns_validation_and_plan_preview(
    authenticated_client, db_session
):
    set_setting_value(db_session, AGENT_BACKEND_KEY, "local_openclaw")
    project = Project(name="Checkpoint Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(project_id=project.id, name="Checkpoint Session")
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    from app.services.workspace.checkpoint_service import CheckpointService

    checkpoint_service = CheckpointService(db_session)
    checkpoint_service.save_checkpoint(
        session.id,
        checkpoint_name="autosave_latest",
        context_data={
            "project_name": project.name,
            "task_subfolder": "task-one",
        },
        orchestration_state={
            "status": "executing",
            "plan": [
                {
                    "step_number": 1,
                    "description": "Create src/app.py",
                    "commands": ["python -m pytest"],
                    "expected_files": ["src/app.py"],
                }
            ],
            "validation_history": [
                {
                    "stage": "plan",
                    "status": "accepted",
                    "profile": "implementation",
                }
            ],
            "last_plan_validation": {
                "stage": "plan",
                "status": "accepted",
                "profile": "implementation",
            },
        },
        current_step_index=1,
        step_results=[{"step_number": 1, "status": "success"}],
    )
    db_session.commit()

    response = authenticated_client.get(
        f"/api/v1/sessions/{session.id}/checkpoints/autosave_latest"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["checkpoint_name"] == "autosave_latest"
    assert payload["summary"]["plan_step_count"] == 1
    assert payload["summary"]["completed_step_count"] == 1
    assert payload["latest_plan_validation"]["status"] == "accepted"
    assert payload["runtime_metadata"]["backend"] == "local_openclaw"
    assert payload["replay_source"]["mode"] == "inspection"
    assert payload["plan_preview"][0]["description"] == "Create src/app.py"


def test_validator_rejects_non_consecutive_steps_missing_commands_and_unsafe_paths():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 2,
                "description": "",
                "commands": [],
                "verification": "python -m pytest",
                "rollback": "git checkout -- .",
                "expected_files": ["../escape.py"],
            }
        ],
        output_text="[]",
        task_prompt="Implement a Python feature",
        execution_profile="full_lifecycle",
    )

    assert verdict.rejected is True
    assert "consecutive integers" in verdict.reasons[0]
    assert verdict.details["missing_description_steps"] == [2]
    assert verdict.details["missing_commands_steps"] == [2]
    assert verdict.details["unsafe_expected_files"] == ["../escape.py"]


def test_verification_plan_flags_invented_workspace_files(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "test").mkdir()
    (project_dir / "test" / "replay.spec.ts").write_text(
        "export const ok = true;\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Verify expected tests exist",
                "commands": ["ls test", "ls tests"],
                "verification": "test -f test/replay.spec.ts",
                "rollback": "true",
                "expected_files": ["test/replay.spec.ts", "tests/index.test.js"],
            }
        ],
        output_text="[]",
        task_prompt="Review the current project structure and verify expected tests.",
        execution_profile="review_only",
        project_dir=project_dir,
    )

    assert verdict.repairable is True
    assert "Verification/review plan references source files" in verdict.reasons[0]
    assert verdict.details["missing_workspace_expected_files"] == [
        "tests/index.test.js"
    ]


def test_validator_flags_duplicated_root_paths_in_plan_commands_and_expected_files():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Create frontend structure",
                "commands": [
                    "mkdir -p frontend/src/frontend/src/components",
                    "touch backend/src/backend/src/index.ts",
                ],
                "verification": "test -d frontend/src/frontend/src/components",
                "rollback": "rm -rf frontend/src/frontend/src backend/src/backend/src",
                "expected_files": [
                    "frontend/src/frontend/src/main.tsx",
                    "backend/src/backend/src/index.ts",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Implement frontend and backend foundations",
        execution_profile="full_lifecycle",
    )

    assert verdict.repairable is True
    assert "repeats workspace root segments" in " ".join(verdict.reasons)
    assert verdict.details["duplicated_root_paths"] == {
        1: ["frontend/src/frontend/src", "backend/src/backend/src"]
    }


def test_validator_rejects_parent_directory_traversal_in_plan_commands():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Verify backend toolchain",
                "commands": ["cd ../backend && npx tsx --version"],
                "verification": "cd ../backend && test -f package.json",
                "rollback": None,
                "expected_files": ["backend/package.json"],
            }
        ],
        output_text="[]",
        task_prompt="Set up frontend and backend in one workspace",
        execution_profile="full_lifecycle",
    )

    assert verdict.rejected is True
    assert "parent-directory paths outside the task workspace" in " ".join(
        verdict.reasons
    )
    assert verdict.details["unsafe_command_paths"] == {1: ["../backend"]}


def test_validator_rejects_absolute_helper_script_paths_in_plan_commands():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Browse Langfuse docs",
                "commands": ["python3 /root/browse.py https://langfuse.com"],
                "verification": "echo docs reviewed",
                "rollback": None,
                "expected_files": [],
            }
        ],
        output_text="[]",
        task_prompt="Review Langfuse docs before implementation",
        execution_profile="full_lifecycle",
    )

    assert verdict.rejected is True
    assert "parent-directory paths outside the task workspace" in " ".join(
        verdict.reasons
    )
    assert verdict.details["unsafe_command_paths"] == {1: ["/root/browse.py"]}


def test_validator_rejects_absolute_cd_preamble_in_verification_command():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Run workflow tests",
                "commands": ["python3 -m pytest tests/test_workflow.py -q"],
                "verification": (
                    "cd /root/.openclaw/workspace && "
                    "python3 -m pytest tests/test_workflow.py -v"
                ),
                "rollback": None,
                "expected_files": [],
            }
        ],
        output_text="[]",
        task_prompt="Build a distributed workflow health checker",
        execution_profile="full_lifecycle",
    )

    assert verdict.rejected is True
    assert "parent-directory paths outside the task workspace" in " ".join(
        verdict.reasons
    )
    assert verdict.details["unsafe_command_paths"] == {1: ["/root/.openclaw/workspace"]}


def test_validator_flags_fullstack_workflow_phase_order_drift():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Install backend Python dependencies and verify core imports",
                "commands": [".venv/bin/pip install -r requirements.txt"],
                "verification": ".venv/bin/python -c \"from app.main import app; print('backend imports OK')\"",
                "rollback": "rm -rf .venv",
                "expected_files": ["app/main.py", "requirements.txt"],
            },
            {
                "step_number": 2,
                "description": "Install frontend Node dependencies and run TypeScript type-check",
                "commands": [
                    "cd frontend && npm install",
                    "cd frontend && npx tsc --noEmit",
                ],
                "verification": "cd frontend && npx tsc --noEmit",
                "rollback": "cd frontend && rm -rf node_modules",
                "expected_files": ["frontend/package.json", "frontend/src/main.tsx"],
            },
            {
                "step_number": 3,
                "description": "Wire API config: verify frontend proxy target matches backend port and CORS allows frontend origin",
                "commands": ['grep "localhost:8080" frontend/vite.config.ts'],
                "verification": ".venv/bin/python -c \"from app.config import Settings; print('cors aligned')\"",
                "rollback": None,
                "expected_files": ["frontend/vite.config.ts", "app/config.py"],
            },
        ],
        output_text="[]",
        task_prompt="Set up frontend and backend with clean architecture in one workspace",
        execution_profile="full_lifecycle",
        workflow_profile="fullstack_scaffold",
    )

    assert verdict.repairable is True
    assert "workflow phase order" in " ".join(verdict.reasons)
    assert verdict.details["workflow_phase_violations"] == [2]


def test_validator_flags_write_pseudo_commands_and_background_processes():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Wire API config",
                "commands": ["write frontend/vite.config.ts: configure proxy"],
                "verification": "test -f frontend/vite.config.ts",
                "rollback": "rm -f frontend/vite.config.ts",
                "expected_files": ["frontend/vite.config.ts"],
            },
            {
                "step_number": 2,
                "description": "Verify backend startup",
                "commands": ["cd backend && npx tsx src/index.ts &"],
                "verification": "curl -s http://localhost:3001/health",
                "rollback": 'pkill -f "tsx src/index.ts"',
                "expected_files": [],
            },
        ],
        output_text="[]",
        task_prompt="Set up frontend and backend with clean architecture",
        execution_profile="full_lifecycle",
    )

    assert verdict.repairable is True
    joined = " ".join(verdict.reasons)
    assert "non-runnable pseudo-commands" in joined
    assert "background processes or long-running dev servers" in joined
    assert verdict.details["non_runnable_steps"] == [1]
    assert verdict.details["background_process_steps"] == [2]


def test_validator_does_not_flag_html_entities_as_background_processes():
    """HTML entities like &nbsp; and bare & in heredoc body must not trigger background-process check."""
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Create index.html with HTML entity in content",
                "commands": [
                    "cat > index.html << 'EOF'\n"
                    "<!DOCTYPE html><html><body>"
                    "<p>Spring&nbsp;&amp;&nbsp;Summer</p>"
                    "</body></html>\nEOF"
                ],
                "verification": "test -f index.html && echo OK",
                "rollback": "rm index.html",
                "expected_files": ["index.html"],
            },
            {
                "step_number": 2,
                "description": "Create page with bare ampersand in title",
                "commands": [
                    "cat > index.html << 'htmleof'\n"
                    "<!DOCTYPE html><html><head>"
                    "<title>Flowers & Seasons</title>"
                    "</head></html>\nhtmleof"
                ],
                "verification": "test -f index.html && echo OK",
                "rollback": "rm index.html",
                "expected_files": ["index.html"],
            },
        ],
        output_text="[]",
        task_prompt="Create a flower landing page",
        execution_profile="full_lifecycle",
    )

    assert (
        "background_process_steps" not in verdict.details
        or verdict.details.get("background_process_steps") == []
    )


def test_validator_allows_static_site_asset_roots_without_nested_project_false_positive():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Create asset folders",
                "commands": ["mkdir -p assets/css assets/js"],
                "verification": "ls -d assets/css assets/js",
                "rollback": "rm -rf assets",
                "expected_files": [],
            },
            {
                "step_number": 2,
                "description": "Create landing page files",
                "commands": [
                    "touch index.html",
                    "touch assets/css/styles.css",
                    "touch assets/js/main.js",
                    "touch assets/flower-background.svg",
                ],
                "verification": (
                    "test -f index.html && test -f assets/css/styles.css && "
                    "test -f assets/js/main.js && test -f assets/flower-background.svg"
                ),
                "rollback": "rm -f index.html assets/css/styles.css assets/js/main.js assets/flower-background.svg",
                "expected_files": [
                    "index.html",
                    "assets/css/styles.css",
                    "assets/js/main.js",
                    "assets/flower-background.svg",
                ],
            },
        ],
        output_text="[]",
        task_prompt="Create a one-page flower website with static assets",
        execution_profile="full_lifecycle",
    )

    assert "nested_project_root_steps" not in verdict.details
    assert "nested project folder" not in " ".join(verdict.reasons)


def test_validator_still_flags_true_nested_project_root_layouts():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Create nested app inside the current workspace",
                "commands": [
                    "mkdir -p flower-site/src flower-site/public",
                    "touch flower-site/package.json flower-site/src/main.js flower-site/public/index.html",
                ],
                "verification": "test -f flower-site/package.json && test -f flower-site/src/main.js",
                "rollback": "rm -rf flower-site",
                "expected_files": [
                    "flower-site/package.json",
                    "flower-site/src/main.js",
                    "flower-site/public/index.html",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Build a flower website in the current project workspace",
        execution_profile="full_lifecycle",
    )

    assert verdict.repairable is True
    assert verdict.details["nested_project_root_steps"] == [1]
    assert "nested project folder" in " ".join(verdict.reasons)


def test_policy_profile_lookup_falls_back_to_balanced():
    profile = get_policy_profile("does-not-exist")

    assert profile.name == "balanced"


def test_policy_profiles_change_validation_outcomes():
    plan = [
        {
            "step_number": 1,
            "description": "Update src/app.py",
            "commands": ["python -m pytest"],
            "verification": "echo done",
            "rollback": "git checkout -- src/app.py",
            "expected_files": ["src/app.py"],
        }
    ]

    balanced = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Implement a Python feature",
        execution_profile="full_lifecycle",
        validation_severity="standard",
    )
    strict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Implement a Python feature",
        execution_profile="full_lifecycle",
        validation_severity="high",
    )

    assert balanced.status == "repair_required"
    assert "weak_verification" in balanced.details["semantic_violation_codes"]
    assert strict.status == "rejected"


def test_policy_profiles_change_workspace_restore_behavior():
    assert not should_restore_workspace_on_failure(
        "planning parse error", policy_profile="balanced"
    )
    assert should_restore_workspace_on_failure(
        "planning parse error", policy_profile="strict"
    )


# ── Phase 10M: runtime lane doctor ───────────────────────────────────────────


def test_lane_doctor_host_with_writable_root_returns_ok(monkeypatch, tmp_path):
    import app.services.workspace.system_settings as system_settings

    writable_root = tmp_path / "projects"
    writable_root.mkdir()
    monkeypatch.setattr(system_settings, "_running_in_container", lambda: False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(writable_root))

    from app.services.workspace.system_settings import diagnose_runtime_lane

    result = diagnose_runtime_lane()
    assert result["verdict"] == "ok"
    assert result["runtime"] == "host"
    assert result["workspace_writable"] is True
    assert result["container_path_on_host"] is False
    assert result["reasons"] == []


def test_lane_doctor_host_uses_host_workspace_root_without_db_setting(
    monkeypatch, tmp_path
):
    import app.services.workspace.system_settings as system_settings

    host_root = tmp_path / "host-projects"
    workspace_root = tmp_path / "workspace-root"
    host_root.mkdir()
    workspace_root.mkdir()
    monkeypatch.setattr(system_settings, "_running_in_container", lambda: False)
    monkeypatch.setenv("HOST_WORKSPACE_ROOT", str(host_root))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)

    from app.services.workspace.system_settings import diagnose_runtime_lane

    result = diagnose_runtime_lane()
    assert result["verdict"] == "ok"
    assert result["effective_workspace_root"] == str(host_root.resolve())


def test_lane_doctor_probe_does_not_delete_existing_lane_probe(monkeypatch, tmp_path):
    import app.services.workspace.system_settings as system_settings

    writable_root = tmp_path / "projects"
    writable_root.mkdir()
    existing_probe = writable_root / ".lane_probe"
    existing_probe.write_text("operator data", encoding="utf-8")
    monkeypatch.setattr(system_settings, "_running_in_container", lambda: False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(writable_root))
    monkeypatch.delenv("HOST_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)

    from app.services.workspace.system_settings import diagnose_runtime_lane

    result = diagnose_runtime_lane()
    assert result["workspace_writable"] is True
    assert existing_probe.read_text(encoding="utf-8") == "operator data"


def test_lane_doctor_container_path_on_host_returns_misconfigured(
    monkeypatch, tmp_path
):
    import app.services.workspace.system_settings as system_settings

    monkeypatch.setattr(system_settings, "_running_in_container", lambda: False)
    # Raw stored value is a container path; on host this should be flagged.
    monkeypatch.setenv("WORKSPACE_ROOT", "/app/projects")
    monkeypatch.delenv("HOST_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)

    from app.services.workspace.system_settings import diagnose_runtime_lane

    result = diagnose_runtime_lane()
    assert result["container_path_on_host"] is True
    assert result["verdict"] == "misconfigured"
    assert any("container" in r.lower() for r in result["reasons"])


def test_lane_doctor_container_runtime_keeps_container_path(monkeypatch, tmp_path):
    import app.services.workspace.system_settings as system_settings

    monkeypatch.setattr(system_settings, "_running_in_container", lambda: True)
    monkeypatch.setenv("WORKSPACE_ROOT", "/app/projects")

    from app.services.workspace.system_settings import diagnose_runtime_lane

    result = diagnose_runtime_lane()
    assert result["runtime"] == "container"
    assert result["container_path_on_host"] is False


def test_lane_doctor_unwritable_root_flagged(monkeypatch, tmp_path):
    import app.services.workspace.system_settings as system_settings

    missing_root = tmp_path / "does-not-exist"
    monkeypatch.setattr(system_settings, "_running_in_container", lambda: False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(missing_root))
    monkeypatch.delenv("HOST_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)

    from app.services.workspace.system_settings import diagnose_runtime_lane

    result = diagnose_runtime_lane()
    assert result["workspace_writable"] is False
    assert result["verdict"] in {"misconfigured", "warning"}
    assert any(
        "not writable" in r.lower() or "does not exist" in r.lower()
        for r in result["reasons"]
    )


# ── Phase 10M: prefer_typed_ops validator flag ────────────────────────────────


def test_plan_contract_violations_flags_python_c_content_write(monkeypatch):
    from app.services.orchestration.planning.planner import PlannerService

    plan = [
        {
            "step_number": 1,
            "description": "Write config via python -c",
            "commands": [
                "python -c \"from pathlib import Path; Path('src/config.py').write_text('# config')\""
            ],
            "verification": "python -m py_compile src/config.py",
            "rollback": None,
            "expected_files": ["src/config.py"],
        }
    ]
    issues = PlannerService.find_immediate_repair_step_issues(plan)
    assert 1 in issues.get("prefer_typed_ops_steps", [])


def test_plan_contract_violations_does_not_flag_verification_only_python_c(monkeypatch):
    from app.services.orchestration.planning.planner import PlannerService

    plan = [
        {
            "step_number": 1,
            "description": "Run module and verify",
            "commands": ["python src/main.py"],
            "verification": "python -c \"import pathlib; assert pathlib.Path('out.txt').exists()\"",
            "rollback": None,
            "expected_files": ["out.txt"],
        }
    ]
    issues = PlannerService.find_immediate_repair_step_issues(plan)
    assert 1 not in issues.get("prefer_typed_ops_steps", [])


def test_sanitizer_rewrites_safe_python_c_write_text_to_ops(tmp_path):
    from app.services.orchestration.planning.planner import PlannerService

    plan = [
        {
            "step_number": 1,
            "description": "Write file via python -c",
            "commands": [
                'python -c \'from pathlib import Path; Path("src/x.py").write_text("# content")\''
            ],
            "verification": "python -m py_compile src/x.py",
            "rollback": None,
            "expected_files": ["src/x.py"],
        }
    ]
    sanitized = PlannerService.sanitize_common_plan_issues(plan)
    step = sanitized[0]
    # Command removed, op promoted
    assert not any("write_text" in cmd for cmd in step["commands"])
    ops = step.get("ops", [])
    assert any(
        op.get("op") == "write_file" and op.get("path") == "src/x.py" for op in ops
    )


def test_sanitizer_leaves_ambiguous_python_c_unchanged():
    from app.services.orchestration.planning.planner import PlannerService

    plan = [
        {
            "step_number": 1,
            "description": "Complex write",
            "commands": [
                "python -c \"import sys; from pathlib import Path; Path(sys.argv[1]).write_text('x')\""
            ],
            "verification": "python -m py_compile src/out.py",
            "rollback": None,
            "expected_files": ["src/out.py"],
        }
    ]
    sanitized = PlannerService.sanitize_common_plan_issues(plan)
    step = sanitized[0]
    # Command must still be present (not rewritten)
    assert any("write_text" in cmd for cmd in step["commands"])
