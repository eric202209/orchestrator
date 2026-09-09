"""PHASE35-VCR1 durable, provider-free grounding capture proofs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingExecutor,
    GroundingOutcome,
    GroundingRunConfig,
    GroundingTaskReference,
    PlanningGroundingProviderAdapter,
)
from app.services.orchestration.planning.grounding.contracts import (
    MAX_PATH_CHARS,
    MAX_QUERY_CHARS,
    MAX_SCOPE_COUNT,
    parse_grounding_request,
)
from app.services.planning.providers.base import (
    PlanningProviderExecutionError,
    ProviderFailureOrigin,
)
from app.tests.evals.phase35_pvh1_validation_harness import (
    ACTION_CAPTURE_SCHEMA_VERSION,
    MAX_CAPTURED_PROMPT_BYTES,
    MAX_LEGAL_ACTION_BYTES,
    PROMPT_CAPTURE_SCHEMA_VERSION,
    RawRunCapture,
    ValidationArtifact,
    ValidationCaptureError,
    ProviderValidationHarness,
    exact_normalized_action,
    exact_provider_prompt,
)
from app.tests.test_phase35_pga1_grounding_provider_adapter import (
    FakePlanningProvider,
    _context,
)
from app.tests.test_phase35_pvh1_provider_validation_harness import (
    ScriptedPlanningProvider,
    _repo,
)

MAX_LEGAL_PATH = ("a/" * 15) + "x"


def _run(
    root: Path,
    label: str,
    files: dict[str, str],
    responses: list[object],
    artifact_path: Path,
):
    _repo(root, files)
    run_id = f"vcr1-{label}-run"
    harness = ProviderValidationHarness(labels=(label,))
    run = harness.start_run(label, grounding_run_id=run_id)
    provider = ScriptedPlanningProvider(responses)
    adapter = PlanningGroundingProviderAdapter(
        run.capture_provider(provider), event_sink=run.event_sink
    )
    config = GroundingRunConfig(
        grounding_run_id=run_id,
        task_reference=GroundingTaskReference(task_id=f"task-{label}"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="vcr1-snapshot",
        max_steps=4,
        max_exploration_provider_requests=4,
        operator_task="Find the relevant implementation without changing the repository.",
    )
    result = GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="vcr1-snapshot"),
        provider=adapter,
        config=config,
        event_sink=run.event_sink,
    ).run()
    record = run.finalize(result, artifact_path=artifact_path)
    return record, result, harness, provider, ValidationArtifact.load(artifact_path)


def _insufficient(reason: str = "provider-free capture proof") -> dict[str, str]:
    return {"decision": "INSUFFICIENT", "reason": reason}


def test_v1_search_text_retains_exact_query_scopes_action_and_digest(tmp_path):
    action = {"action": "search_text", "query": "rate limit", "scopes": ["app"]}
    record, result, _, _, artifact = _run(
        tmp_path,
        "V1",
        {"app/rate_limit.py": "def enforce():\n    return 'rate limit'\n"},
        [action, _insufficient()],
        tmp_path / "vcr1.json",
    )
    reloaded = exact_normalized_action(artifact, result.grounding_run_id, 1)
    assert reloaded == action
    assert record.provider_turns[0]["normalized_action"] == action
    assert record.provider_turns[0]["action_digest"] == result.requests[0].action_digest
    assert record.provider_turns[0]["request_id"] == result.requests[0].request_id


def test_v2_inspect_file_retains_exact_action(tmp_path):
    _, result, _, _, artifact = _run(
        tmp_path,
        "V2",
        {"app/example.py": "value = 1\n"},
        [{"action": "inspect_file", "path": "app/example.py"}, _insufficient()],
        tmp_path / "vcr2.json",
    )
    assert exact_normalized_action(artifact, result.grounding_run_id, 1) == {
        "action": "inspect_file",
        "path": "app/example.py",
    }


def test_v3_resolve_structure_retains_exact_relation_and_locator(tmp_path):
    action = {
        "action": "resolve_structure",
        "relation": "symbol_definition",
        "locator": {"path": "app/example.py", "name": "target"},
    }
    _, result, _, _, artifact = _run(
        tmp_path,
        "V3",
        {"app/example.py": "def target():\n    return True\n"},
        [action, _insufficient()],
        tmp_path / "vcr3.json",
    )
    assert exact_normalized_action(artifact, result.grounding_run_id, 1) == action


def test_v4_malformed_known_action_and_corrected_action_are_both_retained(tmp_path):
    malformed = {"action": "search_text", "query": "rate limit"}
    corrected = {"action": "search_text", "query": "rate limit", "scopes": ["app"]}
    record, result, _, _, artifact = _run(
        tmp_path,
        "V4",
        {"app/rate_limit.py": "rate limit\n"},
        [malformed, corrected, _insufficient()],
        tmp_path / "vcr4.json",
    )
    first = artifact._turn(result.grounding_run_id, 1)
    second = artifact._turn(result.grounding_run_id, 2)
    assert first["action_kind"] == "search_text"
    assert first["candidate_payload"] == malformed
    assert first["action_candidate_payload"] == malformed
    assert first["normalized_action"] is None
    assert first["request_id"] == "grounding-request-1"
    assert first["rejection_code"] == "invalid_request"
    assert second["normalized_action"] == corrected
    assert second["action_digest"] == result.requests[0].action_digest
    assert record.rejections[0]["code"] == "invalid_request"


def test_v5_unknown_oversized_action_is_bounded_but_recognizable(tmp_path):
    oversized = json.dumps({"action": "unknown_action", "padding": "x" * 100_000})
    _, result, _, _, artifact_path = _run(
        tmp_path,
        "V5",
        {"app/example.py": "value = 1\n"},
        [oversized],
        tmp_path / "vcr5.json",
    )
    turn = artifact_path._turn(result.grounding_run_id, 1)
    assert turn["action_kind"] == "unknown_action"
    assert turn["candidate_payload"] is None
    assert turn["candidate_sha256"] == hashlib.sha256(oversized.encode()).hexdigest()
    assert turn["candidate_length"] == len(oversized.encode())
    assert len(turn["candidate_prefix"]) <= 256
    assert (tmp_path / "vcr5.json").stat().st_size < 100_000


def test_v6_executor_rejection_retains_normalized_request_and_code(tmp_path):
    _repo(tmp_path, {"app/real.py": "value = 1\n"})
    outside = tmp_path.parent / "vcr1-outside.py"
    outside.write_text("value = 2\n", encoding="utf-8")
    link = tmp_path / "app/link.py"
    link.symlink_to(outside)
    import subprocess

    subprocess.run(["git", "add", "app/link.py"], cwd=tmp_path, check=True)
    record, result, _, _, artifact = _run(
        tmp_path,
        "V6",
        {},
        [{"action": "inspect_file", "path": "app/link.py"}],
        tmp_path / "vcr6.json",
    )
    assert artifact.exact_normalized_action(result.grounding_run_id, 1) == {
        "action": "inspect_file",
        "path": "app/link.py",
    }
    assert record.rejections[0]["code"] == "symlink_path"
    assert result.terminal_reason.value == "EXECUTOR_FAILURE"


def test_v7_executor_success_retains_observation_and_result(tmp_path):
    _, result, _, _, artifact = _run(
        tmp_path,
        "V7",
        {"app/example.py": "value = 1\n"},
        [{"action": "inspect_file", "path": "app/example.py"}, _insufficient()],
        tmp_path / "vcr7.json",
    )
    payload = artifact.to_dict()
    assert payload["results"][0]["requests"][0]["normalized_action"] == {
        "action": "inspect_file",
        "path": "app/example.py",
    }
    assert payload["results"][0]["observations"][0]["outcome"] == "FOUND"


def test_v8_provider_timeout_retains_prompt_request_identity_and_no_response(tmp_path):
    timeout = PlanningProviderExecutionError(
        classification="provider_timeout",
        detail="synthetic timeout",
        origin=ProviderFailureOrigin.INVOCATION,
    )
    record, result, _, provider, artifact = _run(
        tmp_path,
        "V8",
        {"app/example.py": "value = 1\n"},
        [timeout],
        tmp_path / "vcr8.json",
    )
    prompt = exact_provider_prompt(artifact, result.grounding_run_id, 1)
    assert prompt == provider.requests[0].prompt
    assert artifact._turn(result.grounding_run_id, 1)["response_present"] is False
    assert record.runtime_failures == ("provider_timeout",)


def test_v9_malformed_response_retains_exact_prompt_and_bounded_response(tmp_path):
    record, result, _, provider, artifact = _run(
        tmp_path,
        "V9",
        {"app/example.py": "value = 1\n"},
        ["not json"],
        tmp_path / "vcr9.json",
    )
    turn = artifact._turn(result.grounding_run_id, 1)
    assert (
        exact_provider_prompt(artifact, result.grounding_run_id, 1)
        == provider.requests[0].prompt
    )
    assert turn["candidate_raw_utf8"] == "not json"
    assert record.parser_failure_count == 1


def test_v10_artifact_is_written_before_report_normalization_failure(tmp_path):
    _repo(tmp_path, {"app/example.py": "value = 1\n"})
    harness = ProviderValidationHarness(labels=("V10",))
    run = harness.start_run("V10", "vcr1-V10-run")
    provider = ScriptedPlanningProvider(
        [{"action": "inspect_file", "path": "app/example.py"}]
    )
    adapter = PlanningGroundingProviderAdapter(
        run.capture_provider(provider), event_sink=run.event_sink
    )
    config = GroundingRunConfig(
        grounding_run_id="vcr1-V10-run",
        task_reference=GroundingTaskReference(task_id="task-V10"),
        workspace_identity=str(tmp_path.resolve()),
        snapshot_identity="vcr1-snapshot",
        max_steps=1,
        max_exploration_provider_requests=1,
        operator_task="Find the implementation.",
    )
    from app.services.orchestration.planning.grounding import GroundingCoordinator

    result = GroundingCoordinator(
        executor=GroundingExecutor(tmp_path, snapshot_identity="vcr1-snapshot"),
        provider=adapter,
        config=config,
        event_sink=run.event_sink,
    ).run()
    artifact = tmp_path / "vcr10.json"

    with pytest.raises(RuntimeError, match="report failure"):
        run.finalize(
            result,
            artifact_path=artifact,
            normalizer=lambda _raw: (_ for _ in ()).throw(
                RuntimeError("report failure")
            ),
        )
    loaded = ValidationArtifact.load(artifact)
    assert loaded.exact_provider_prompt("vcr1-V10-run", 1)
    assert loaded.exact_normalized_action("vcr1-V10-run", 1)["path"] == "app/example.py"


def test_v11_process_restart_reload_preserves_exact_action(tmp_path):
    _, result, _, _, artifact = _run(
        tmp_path,
        "V11",
        {"app/example.py": "value = 1\n"},
        [{"action": "inspect_file", "path": "app/example.py"}, _insufficient()],
        tmp_path / "vcr11.json",
    )
    reloaded = artifact
    assert reloaded.exact_normalized_action(result.grounding_run_id, 1) == {
        "action": "inspect_file",
        "path": "app/example.py",
    }


def test_v12_process_restart_reload_preserves_exact_prompt(tmp_path):
    _, result, _, provider, artifact_path = _run(
        tmp_path,
        "V12",
        {"app/example.py": "value = 1\n"},
        [{"action": "inspect_file", "path": "app/example.py"}],
        tmp_path / "vcr12.json",
    )
    assert (
        exact_provider_prompt(artifact_path, result.grounding_run_id, 1)
        == provider.requests[0].prompt
    )


def test_v13_action_digest_roundtrip_matches_production_parser(tmp_path):
    _, result, _, _, artifact = _run(
        tmp_path,
        "V13",
        {"app/example.py": "value = 1\n"},
        [{"action": "inspect_file", "path": "app/example.py"}],
        tmp_path / "vcr13.json",
    )
    payload = artifact.exact_normalized_action(result.grounding_run_id, 1)
    replay = parse_grounding_request(
        payload,
        grounding_run_id=result.grounding_run_id,
        request_id=result.requests[0].request_id,
    )
    assert replay.normalized_payload == result.requests[0].normalized_payload
    assert replay.action_digest == result.requests[0].action_digest


def test_v14_executor_replay_is_equivalent_from_artifact_only(tmp_path):
    _, result, _, _, artifact = _run(
        tmp_path,
        "V14",
        {"app/example.py": "value = 1\n"},
        [{"action": "inspect_file", "path": "app/example.py"}],
        tmp_path / "vcr14.json",
    )
    original = result.requests[0]
    replay = parse_grounding_request(
        artifact.exact_normalized_action(result.grounding_run_id, 1),
        grounding_run_id=original.grounding_run_id,
        request_id=original.request_id,
    )
    executor = GroundingExecutor(tmp_path, snapshot_identity="vcr1-snapshot")
    first = executor.execute(original)
    second = executor.execute(replay)
    assert first.outcome is second.outcome is GroundingOutcome.FOUND
    assert first.source_paths == second.source_paths
    assert first.result_count == second.result_count
    assert first.budget_delta == second.budget_delta
    assert [(hit.path, hit.line_number, hit.snippet) for hit in first.hits] == [
        (hit.path, hit.line_number, hit.snippet) for hit in second.hits
    ]


def test_v15_cpr_rejected_and_corrected_candidates_are_distinguishable(tmp_path):
    malformed = {"action": "search_text", "query": "rate limit"}
    corrected = {"action": "search_text", "query": "rate limit", "scopes": ["app"]}
    _, result, _, _, artifact = _run(
        tmp_path,
        "V15",
        {"app/rate_limit.py": "rate limit\n"},
        [malformed, corrected, _insufficient()],
        tmp_path / "vcr15.json",
    )
    rejected = artifact._turn(result.grounding_run_id, 1)
    accepted = artifact._turn(result.grounding_run_id, 2)
    assert rejected["candidate_payload"] == malformed
    assert rejected["normalized_action"] is None
    assert accepted["normalized_action"] == corrected
    assert rejected["action_digest"] is None
    assert accepted["action_digest"] != rejected["action_digest"]


def test_v16_first_prompt_has_no_evaluator_truth_or_metric_vocabulary(tmp_path):
    _, result, _, _, artifact = _run(
        tmp_path,
        "V16",
        {"app/example.py": "def target():\n    return True\n"},
        [{"action": "inspect_file", "path": "app/example.py"}],
        tmp_path / "vcr16.json",
    )
    prompt = artifact.exact_provider_prompt(result.grounding_run_id, 1)
    for forbidden in (
        "app/services/auth/rate_limit.py",
        "enforce_auth_rate_limit",
        "target_path_reached",
        "target_content_inspected",
        "target_structure_resolved",
        "FOUND_RELEVANT",
    ):
        assert forbidden not in prompt


def test_v17_transport_diagnostics_and_secrets_are_not_retained(tmp_path):
    _repo(tmp_path, {"app/example.py": "value = 1\n"})
    harness = ProviderValidationHarness(labels=("V17",))
    run = harness.start_run("V17", "vcr1-V17-run")
    run.event_sink(
        EventType.GROUNDING_PROVIDER_TURN,
        {
            "grounding_run_id": "vcr1-V17-run",
            "provider_request_id": "grounding-provider-request-0",
            "provider_provenance": {
                "headers": {"Authorization": "Bearer super-secret"},
                "api_token": "super-secret",
            },
            "detail": "Authorization: Bearer super-secret",
        },
    )
    artifact = tmp_path / "vcr17.json"
    run.finalize(artifact_path=artifact)
    serialized = artifact.read_text(encoding="utf-8")
    assert "Authorization" not in serialized
    assert "super-secret" not in serialized
    assert "headers" not in serialized


def test_v18_oversized_invalid_provider_output_remains_bounded(tmp_path):
    oversized = "x" * (MAX_CAPTURED_PROMPT_BYTES // 2)
    _, result, _, _, artifact = _run(
        tmp_path,
        "V18",
        {"app/example.py": "value = 1\n"},
        [oversized],
        tmp_path / "vcr18.json",
    )
    turn = artifact._turn(result.grounding_run_id, 1)
    assert turn["candidate_raw_utf8"] is None
    assert turn["candidate_length"] == len(oversized)
    assert len(turn["candidate_prefix"]) == 256
    assert (tmp_path / "vcr18.json").stat().st_size < 100_000


@pytest.mark.parametrize(
    "action",
    [
        {
            "action": "search_text",
            "query": "q" * MAX_QUERY_CHARS,
            "scopes": ["app", "tests", "scripts", "docs"],
        },
        {"action": "inspect_file", "path": MAX_LEGAL_PATH},
        {
            "action": "resolve_structure",
            "relation": "symbol_definition",
            "locator": {"path": MAX_LEGAL_PATH, "name": "target"},
        },
        {
            "action": "resolve_structure",
            "relation": "enclosing_symbol",
            "locator": {"path": MAX_LEGAL_PATH, "line": 1},
        },
        {
            "action": "resolve_structure",
            "relation": "mounted_route",
            "locator": {
                "path": MAX_LEGAL_PATH,
                "method": "GET",
                "decorator_path": "/" + "a" * (MAX_PATH_CHARS - 1),
            },
        },
    ],
)
def test_v19_every_exercised_legal_action_is_retained_exactly(tmp_path, action):
    run = ProviderValidationHarness(labels=("V19",)).start_run("V19", "pga1-run")
    provider = FakePlanningProvider([action])
    adapter = PlanningGroundingProviderAdapter(
        run.capture_provider(provider), event_sink=run.event_sink
    )
    adapter.decide(_context(tmp_path, provider=provider))
    artifact_path = tmp_path / "vcr19.json"
    run.persist_artifact(artifact_path)
    loaded = ValidationArtifact.load(artifact_path)
    normalized = loaded.exact_normalized_action("pga1-run", 0)
    assert normalized["action"] == action["action"]
    assert (
        len(json.dumps(normalized, ensure_ascii=False).encode())
        <= MAX_LEGAL_ACTION_BYTES
    )


def test_v20_serialization_is_deterministic_and_reloadable(tmp_path):
    _, result, harness, _, artifact = _run(
        tmp_path,
        "V20",
        {"app/example.py": "value = 1\n"},
        [{"action": "inspect_file", "path": "app/example.py"}],
        tmp_path / "vcr20.json",
    )
    first = artifact.to_json_bytes()
    second_path = tmp_path / "vcr20-second.json"
    harness.run("V20").persist_artifact(second_path)
    assert first == second_path.read_bytes()
    assert ValidationArtifact.load(second_path).exact_provider_prompt(
        result.grounding_run_id, 1
    )


def test_prompt_capture_ceiling_is_explicit_and_exact(tmp_path):
    run = ProviderValidationHarness(labels=("BOUND",)).start_run("BOUND", "pga1-run")
    provider = FakePlanningProvider([{"action": "inspect_file", "path": "app/a.py"}])
    adapter = PlanningGroundingProviderAdapter(
        run.capture_provider(provider), event_sink=run.event_sink
    )
    context = _context(tmp_path, provider=provider)
    adapter.decide(context)
    artifact = run.persist_artifact(tmp_path / "bound.json")
    turn = artifact._turn("pga1-run", 0)
    assert turn["prompt_capture_schema_version"] == PROMPT_CAPTURE_SCHEMA_VERSION
    assert turn["action_capture_schema_version"] == ACTION_CAPTURE_SCHEMA_VERSION
    assert turn["prompt_utf8_length"] <= MAX_CAPTURED_PROMPT_BYTES
