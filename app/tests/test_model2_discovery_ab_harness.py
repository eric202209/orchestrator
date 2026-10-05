"""Provider-free controls for the POST33-MODEL2 evaluation-only seam."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.evals import model2_discovery_ab as harness
from app.services.orchestration.planning.read_only_discovery import (
    execute_discovery_request,
    materialize_observation_source_context,
    parse_discovery_request,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.execution.executor_workspace_binding import (
    EPHEMERAL_AGENT_ID,
)


def test_model2_packets_reuse_production_prompt_and_isolate_profile_envelope():
    packet = harness._prompt_packet("T222")
    baseline = harness._wire_prompt(packet, harness.ARMS["A"])
    candidate = harness._wire_prompt(packet, harness.ARMS["B"])
    profile = harness._wire_prompt(packet, harness.ARMS["C"])

    assert baseline == packet["discovery_prompt"]
    assert candidate == baseline
    assert json.loads(profile)["body"] == baseline
    assert profile != baseline


def test_model2_ephemeral_identity_and_pl18_do_not_mutate_product_state(tmp_path):
    if not harness.PERSISTENT_OPENCLAW_CONFIG.is_file():
        pytest.skip(
            "OpenClaw persistent configuration is unavailable in this CI environment"
        )
    before = harness._product_state()
    before_config = harness._persistent_config_fingerprint()
    runtime_workspace = tmp_path / "runtime"
    runtime_workspace.mkdir()
    service, identity = harness._configure_ephemeral_service(
        harness.ARMS["B"], runtime_workspace
    )
    try:
        # ORS1 owns normal sandbox identity; the legacy runner_agent_id is
        # compatibility-only and must not replace the synthetic agent.
        assert identity["agent_id"] == EPHEMERAL_AGENT_ID
        assert identity["model"] == "ollama/qwen3-coder:30b"
        assert identity["tools"] == {"deny": ["*"]}
    finally:
        service.release_runtime_workspace_binding()
        service._evaluation_db.close()

    assert harness._persistent_config_fingerprint() == before_config
    assert harness._product_state() == before


def test_model2_product_state_handles_uninitialized_database(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(harness, "SessionLocal", sessionmaker(bind=test_engine))

    state = harness._product_state()

    assert state
    assert all(value == 0 for value in state.values())


def test_model2_response_injection_uses_production_replay_chain():
    request = parse_discovery_request(
        '{"action":"search_text","query":"rate","paths":["app/services/tasks/tool_tracking.py"]}'
    )
    observation = execute_discovery_request(harness.REPOSITORY_ROOT, request)
    materialization = materialize_observation_source_context(
        project_dir=harness.REPOSITORY_ROOT,
        prompt=harness.TASKS["T222"]["task"],
        planner_contract=None,
        observation=observation,
        materialize=materialize_planner_source_context,
        source_cache={},
    )
    assert observation.hits
    assert any(
        item.relative_path == harness.TASKS["T222"]["target_path"]
        for item in materialization.files
    )


def test_model2_order_budget_and_raw_response_artifacts_are_bounded():
    assert harness.PROVIDER_CALL_BUDGET == 9
    assert len(harness.CALL_ORDER) == 9
    assert len(set(harness.CALL_ORDER)) == 9
    cells_root = harness.EVIDENCE_ROOT / "cells"
    if not cells_root.exists():
        pytest.skip(
            "MODEL2 raw response evidence is generated-only and is not checked in"
        )
    for packet, arm in harness.CALL_ORDER:
        cell = next(
            path
            for path in cells_root.iterdir()
            if f"-{packet}-" in path.name
            and path.name.endswith(harness.ARMS[arm]["name"])
        )
        assert (cell / "raw-provider.stdout").exists()
        assert (cell / "raw-provider.stderr").exists()


def test_model_identity_guard_rejects_openclaw_fallback_before_scoring():
    identity = {
        "agent_id": "orchestrator",
        "model": "openai/qwen-local",
        "provider_endpoint": "http://ai-gateway:8000/v1",
        "profile": "openclaw_default",
        "config_sha256": "ephemeral-config",
        "tools": {"deny": ["*"]},
    }
    with pytest.raises(harness.IdentityDriftError) as exc_info:
        harness._verify_runtime_identity(
            harness.ARMS["A"],
            identity=identity,
            diagnostics={"invocation": {"selected_agent": "orchestrator"}},
            parsed_runtime={"output_channel_used": "stderr"},
            raw_stdout="",
            raw_stderr=(
                "model fallback decision: decision=candidate_failed "
                "requested=openai/qwen-local candidate=openai/qwen-local "
                "reason=auth next=ollama/qwen3-coder:30b\n"
                "model fallback decision: decision=candidate_succeeded "
                "requested=openai/qwen-local candidate=ollama/qwen3-coder:30b "
                "reason=unknown next=none"
            ),
            prompt_hash="prompt-hash",
        )

    assert exc_info.value.proof["status"] == "INVALID_IDENTITY_DRIFT"
    assert exc_info.value.proof["effective_provider_model_ref"] == (
        "ollama/qwen3-coder:30b"
    )
    assert exc_info.value.proof["fallback_diagnostics"]


def test_model_identity_guard_fails_closed_when_effective_identity_is_missing():
    with pytest.raises(harness.IdentityDriftError) as exc_info:
        harness._verify_runtime_identity(
            harness.ARMS["A"],
            identity={
                "agent_id": "orchestrator",
                "provider_endpoint": "http://ai-gateway:8000/v1",
                "profile": "openclaw_default",
                "config_sha256": "ephemeral-config",
                "tools": {"deny": ["*"]},
            },
            diagnostics={},
            parsed_runtime={},
            raw_stdout="",
            raw_stderr="provider initialization failed",
            prompt_hash="prompt-hash",
        )

    assert exc_info.value.proof["status"] == "IDENTITY_UNVERIFIED"


def test_model_identity_guard_accepts_matching_runtime_provider_model():
    proof = harness._verify_runtime_identity(
        harness.ARMS["B"],
        identity={
            "agent_id": "orchestrator",
            "provider_endpoint": "http://host.docker.internal:11434",
            "profile": "openclaw_default",
            "config_sha256": "ephemeral-config",
            "tools": {"deny": ["*"]},
        },
        diagnostics={},
        parsed_runtime={"output_channel_used": "stderr"},
        raw_stdout="",
        raw_stderr=(
            '"agentMeta": {"provider": "ollama", ' '"model": "qwen3-coder:30b"}'
        ),
        prompt_hash="prompt-hash",
    )

    assert proof["status"] == "PASS"
    assert proof["effective_provider_model_ref"] == "ollama/qwen3-coder:30b"


def test_model_identity_guard_rejects_provider_metadata_from_failed_generation():
    with pytest.raises(harness.IdentityDriftError) as exc_info:
        harness._verify_runtime_identity(
            harness.ARMS["A"],
            identity={
                "agent_id": "orchestrator",
                "provider_endpoint": "http://ai-gateway:8000/v1",
                "profile": "openclaw_default",
                "config_sha256": "ephemeral-config",
                "tools": {"deny": ["*"]},
            },
            diagnostics={},
            parsed_runtime={"output_channel_used": "stderr"},
            raw_stdout='"isError": true',
            raw_stderr=('"agentMeta": {"provider": "openai", "model": "qwen-local"}'),
            prompt_hash="prompt-hash",
        )

    assert exc_info.value.proof["status"] == "IDENTITY_UNVERIFIED"
    assert exc_info.value.proof["generation_success"] is False


def test_model2_ephemeral_baseline_binding_adds_placeholder_and_disables_fallback(
    tmp_path,
):
    if not harness.PERSISTENT_OPENCLAW_CONFIG.is_file():
        pytest.skip(
            "OpenClaw persistent configuration is unavailable in this CI environment"
        )
    before_config = harness._persistent_config_fingerprint()
    runtime_workspace = tmp_path / "runtime"
    runtime_workspace.mkdir()
    service, identity = harness._configure_ephemeral_service(
        harness.ARMS["A"], runtime_workspace
    )
    try:
        bound_config = json.loads(
            service._openclaw_config_path().read_text(encoding="utf-8")
        )
        selected = next(
            agent
            for agent in bound_config["agents"]["list"]
            if agent["id"] == identity["agent_id"]
        )
        provider = bound_config["models"]["providers"]["openai"]
        assert selected["model"]["primary"] == "openai/qwen-local"
        assert selected["model"]["fallbacks"] == []
        assert provider["apiKey"] not in {"", "ollama-local"}
        assert identity["ephemeral_credential_source"] == (
            "ephemeral_config_placeholder"
        )
        assert identity["fallbacks"] == []
    finally:
        service.release_runtime_workspace_binding()
        service._evaluation_db.close()

    assert harness._persistent_config_fingerprint() == before_config
