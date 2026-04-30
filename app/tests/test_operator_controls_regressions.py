import json

from app.models import LogEntry, Project, Session as SessionModel
from app.services.orchestration.policy import (
    get_policy_profile,
    should_restore_workspace_on_failure,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.workspace.system_settings import (
    ADAPTATION_PROFILE_KEY,
    AGENT_BACKEND_KEY,
    ORCHESTRATION_POLICY_PROFILE_KEY,
    set_setting_value,
)


def test_settings_can_persist_operator_backend_and_policy_profile(
    authenticated_client, db_session
):
    set_setting_value(db_session, AGENT_BACKEND_KEY, "local_openclaw")
    set_setting_value(db_session, ADAPTATION_PROFILE_KEY, "openclaw_default")
    set_setting_value(db_session, ORCHESTRATION_POLICY_PROFILE_KEY, "balanced")

    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "local_openclaw",
            "agent_adaptation_profile": "openclaw_default",
            "orchestration_policy_profile": "strict",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["system"]["agent_backend"] == "local_openclaw"
    assert payload["system"]["agent_adaptation_profile"] == "openclaw_default"
    assert payload["system"]["orchestration_policy_profile"] == "strict"
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


def test_settings_reject_mismatched_backend_and_adaptation_profile(
    authenticated_client,
):
    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={
            "agent_backend": "local_openclaw",
            "agent_adaptation_profile": "openai_responses_default",
        },
    )

    assert response.status_code == 400
    assert "not supported by backend" in response.json()["detail"]


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


def test_checkpoint_inspection_returns_validation_and_plan_preview(
    authenticated_client, db_session
):
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

    assert balanced.status == "warning"
    assert strict.status == "rejected"


def test_policy_profiles_change_workspace_restore_behavior():
    assert not should_restore_workspace_on_failure(
        "planning parse error", policy_profile="balanced"
    )
    assert should_restore_workspace_on_failure(
        "planning parse error", policy_profile="strict"
    )
