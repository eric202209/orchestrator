"""GR1 provider-free tests for local discovery evidence retention."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.planning.discovery_contract_capture import (
    DiscoveryContractCapture,
)
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryContractError,
    parse_discovery_request,
    run_discovery_stage,
)
from app.services.workspace.control_state_paths import ControlStateLocation


def _context(tmp_path: Path) -> SimpleNamespace:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=object(),
        prompt="Inspect the existing implementation.",
        orchestration_state=SimpleNamespace(
            project_context="A bounded provider-free fixture.",
            project_dir=project_dir,
        ),
        control_state_location=ControlStateLocation(
            legacy_root=tmp_path / "legacy-project",
            project_id=36,
            control_root=tmp_path / "control" / "projects" / "36",
        ),
        emit_live=lambda *args, **kwargs: None,
        session_id=230,
        task_id=280,
        task_execution_id=371,
    )


def _planner(output: str):
    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            del args, kwargs
            return {"status": "completed", "output": output}

    return _Planner


def test_default_capture_retains_runtime_output_and_parser_rejection(tmp_path):
    ctx = _context(tmp_path)

    with pytest.raises(DiscoveryContractError, match="discovery_output_not_json"):
        run_discovery_stage(
            ctx=ctx,
            planning_timeout_seconds=120,
            extract_structured_text=lambda value: str(value),
            planner_service=_planner("Here is the action: not JSON"),
            emit_phase_event=lambda *args, **kwargs: None,
        )

    artifact_path = (
        tmp_path
        / "control"
        / "projects"
        / "36"
        / "discovery-contract-capture"
        / "session_230_task_280_execution_371.json"
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["request"]["transport"] == "discovery_runtime"
    assert artifact["stages"]["runtime_output"]["value"] == (
        "Here is the action: not JSON"
    )
    assert artifact["stages"]["parser_input"] == "Here is the action: not JSON"
    assert artifact["parser"] == {
        "success": False,
        "reason": "discovery_output_not_json",
        "action": None,
    }


def test_capture_redacts_secrets_but_retains_hash_and_shape(tmp_path):
    path = tmp_path / "capture.json"
    capture = DiscoveryContractCapture(path)
    capture.record_openclaw_cli_response(
        stdout='{"action":"stop","token":"secret-value"}',
        stderr="Authorization: Bearer ephemeral-token",
        return_code=0,
    )

    artifact = json.loads(path.read_text(encoding="utf-8"))
    stdout = artifact["response"]["raw_stdout"]
    stderr = artifact["response"]["raw_stderr"]
    assert stdout["sha256"]
    assert stderr["sha256"]
    assert "secret-value" not in stdout["value"]
    assert "ephemeral-token" not in stderr["value"]


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("not json", "discovery_output_not_json"),
        ('```json\n{"action":"stop"}\n```', "discovery_output_not_json"),
        ('{"action":"stop","extra":true}', "discovery_stop_has_extra_fields"),
        (
            '{"action":"search_text","query":"x","paths":[]}',
            "discovery_search_paths_invalid",
        ),
    ],
)
def test_invalid_discovery_representations_remain_fail_closed(value, reason):
    with pytest.raises(DiscoveryContractError, match=reason):
        parse_discovery_request(value)


def test_valid_canonical_discovery_json_remains_accepted():
    assert parse_discovery_request('{"action":"stop"}').action == "stop"
