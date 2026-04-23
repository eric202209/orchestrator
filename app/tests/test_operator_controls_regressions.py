import json

from app.models import LogEntry, Project, Session as SessionModel
from app.services.orchestration.policy import (
    get_policy_profile,
    should_restore_workspace_on_failure,
)
from app.services.orchestration.validator import ValidatorService
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
