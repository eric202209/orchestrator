"""Provider-free regressions for the one-turn pre-Planning observation seam."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from app.services.orchestration.context.assembly import assemble_planning_prompt
from app.services.orchestration.planning.read_only_discovery import (
    MAX_FILE_BYTES,
    MAX_OBSERVATION_BYTES,
    MAX_SEARCH_RESULTS,
    DiscoveryContractError,
    DiscoveryObservation,
    DiscoveryTurnGuard,
    SearchHit,
    build_discovery_prompt,
    execute_discovery_request,
    parse_discovery_request,
    render_discovery_observation,
)
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
    materialized_source_content,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _search(query: str = "scheduled_task_execution"):
    request = parse_discovery_request(
        json.dumps(
            {
                "action": "search_text",
                "query": query,
                "paths": ["app/tasks", "app/tests"],
            }
        )
    )
    return request, execute_discovery_request(REPO_ROOT, request)


def test_task_218_search_reveals_maintenance_and_focused_test_without_writes():
    sentinel = REPO_ROOT / "app/tasks/maintenance.py"
    before = sentinel.read_bytes()

    _request, observation = _search()

    assert observation.result_count <= MAX_SEARCH_RESULTS
    assert not observation.result_count == 0
    paths = set(observation.materialization_paths())
    assert "app/tasks/maintenance.py" in paths
    assert "app/tests/test_maintenance_task_session_cleanup.py" in paths
    assert sentinel.read_bytes() == before
    rendered = render_discovery_observation(observation)
    assert "app/tasks/maintenance.py" in rendered
    assert "test_maintenance_task_session_cleanup.py" in rendered
    assert len(rendered.encode("utf-8")) <= MAX_OBSERVATION_BYTES


def test_task_218_observation_becomes_current_source_materialization():
    _request, observation = _search()
    materialization = materialize_planner_source_context(
        REPO_ROOT,
        task_description=(
            "Fix scheduled task timestamp handling\n\n"
            + "\n".join(hit.snippet for hit in observation.hits)[:2000]
        ),
        supporting_paths=observation.materialization_paths(),
    )

    maintenance = materialization.file_map()["app/tasks/maintenance.py"]
    focused_test = materialization.file_map()[
        "app/tests/test_maintenance_task_session_cleanup.py"
    ]
    assert materialization.available
    assert maintenance.content and "scheduled_task_execution" in maintenance.content
    # The existing 5,000-byte source budget may omit the test body, but the
    # bounded provider-safe materialization still represents its discovered
    # path and the observation block carries the matching test lines.
    assert focused_test.relative_path.endswith(
        "test_maintenance_task_session_cleanup.py"
    )
    assert focused_test.status
    assert maintenance.version_identity
    assert "target_id" not in render_discovery_observation(observation)


def test_observation_is_injected_into_existing_final_planning_prompt(tmp_path):
    target = tmp_path / "maintenance.py"
    target.write_text(
        "def scheduled_task_execution():\n    return 1\n", encoding="utf-8"
    )
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description="Fix the discovered maintenance implementation.",
        supporting_paths=["maintenance.py"],
    )
    observation = DiscoveryObservation(
        action="search_text",
        status="completed",
        hits=(
            SearchHit(
                path="maintenance.py",
                line_number=1,
                snippet="def scheduled_task_execution():",
            ),
        ),
    )
    state = OrchestrationState(
        session_id="discovery-prompt-test",
        task_description="Fix the discovered maintenance implementation.",
        project_name="discovery-prompt-test",
        project_context="Existing workspace.",
        task_id=1,
    )
    state._project_dir_override = str(tmp_path)
    ctx = SimpleNamespace(
        db=None,
        prompt="Fix the discovered maintenance implementation.",
        execution_profile="full_lifecycle",
        workflow_profile="default",
        planning_adaptation_profile="local_qwen_json_array",
        orchestration_state=state,
        planner_source_materialization=materialization,
        read_only_observation=observation,
        planner_contract=None,
    )

    prompt = assemble_planning_prompt(ctx, {"file_count": 1, "source_file_count": 1})

    assert "## READ-ONLY OBSERVATION" in prompt
    assert "maintenance.py:1: def scheduled_task_execution()" in prompt
    assert "CURRENT SOURCE MATERIALIZATION" in prompt
    assert "def scheduled_task_execution" in prompt


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"action":"search_text","query":"x","paths":["app/tasks"],"old":"x"}',
        '{"action":"write_file","path":"app/x.py"}',
        '{"action":"shell","command":"rg x app/tasks"}',
        '{"action":"search_text","query":"x; rm -rf .","paths":["app/tasks"]}',
        '{"action":"search_text","query":"x","paths":["/etc"]}',
        '{"action":"read_file","path":"../outside.py"}',
    ],
)
def test_malformed_unsafe_or_mutating_discovery_fails_closed(payload):
    with pytest.raises(DiscoveryContractError):
        parse_discovery_request(payload)


def test_stop_is_terminal_request_and_has_no_action_payload():
    request = parse_discovery_request('{"action":"stop"}')
    observation = execute_discovery_request(REPO_ROOT, request)
    assert observation.action == "stop"
    assert observation.status == "stopped"
    assert observation.materialization_paths() == ()


def test_duplicate_turn_is_rejected_before_a_second_action():
    guard = DiscoveryTurnGuard()
    guard.claim()
    with pytest.raises(DiscoveryContractError, match="already_used"):
        guard.claim()


def test_empty_search_is_bounded_no_result_evidence():
    _request, observation = _search(f"a-string-that-is-not-present-{uuid.uuid4().hex}")
    assert observation.result_count == 0
    assert observation.reason == "no_matches"
    assert observation.materialization_paths() == ()
    assert "result_count: 0" in render_discovery_observation(observation)


def test_search_uses_shell_false_and_caps_many_results(monkeypatch):
    calls = []
    output = "\n".join(
        f"app/tasks/maintenance.py:{index}:scheduled_task_execution"
        for index in range(1, MAX_SEARCH_RESULTS + 6)
    ).encode()

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=output, stderr=b"")

    monkeypatch.setattr(
        "app.services.orchestration.planning.read_only_discovery.subprocess.run",
        fake_run,
    )
    request = parse_discovery_request(
        '{"action":"search_text","query":"scheduled_task_execution","paths":["app/tasks"]}'
    )
    observation = execute_discovery_request(REPO_ROOT, request)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert kwargs["shell"] is False
    assert argv[0].endswith("rg")
    assert observation.result_count == MAX_SEARCH_RESULTS
    assert observation.truncated
    assert (
        len(render_discovery_observation(observation).encode()) <= MAX_OBSERVATION_BYTES
    )


def test_invented_search_result_fails_closed_without_materialization(monkeypatch):
    def fake_run(_argv, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=b"app/tasks/nonexistent_218.py:1:invented",
            stderr=b"",
        )

    monkeypatch.setattr(
        "app.services.orchestration.planning.read_only_discovery.subprocess.run",
        fake_run,
    )
    request = parse_discovery_request(
        '{"action":"search_text","query":"x","paths":["app/tasks"]}'
    )
    with pytest.raises(DiscoveryContractError, match="missing"):
        execute_discovery_request(REPO_ROOT, request)


def test_read_file_is_relative_bounded_and_current(tmp_path):
    source = tmp_path / "large.py"
    source.write_text("line\n" * 2000, encoding="utf-8")
    request = parse_discovery_request('{"action":"read_file","path":"large.py"}')

    observation = execute_discovery_request(tmp_path, request)

    assert observation.paths == ("large.py",)
    assert observation.content
    assert len(observation.content.encode("utf-8")) <= MAX_FILE_BYTES
    assert observation.truncated


def test_read_file_rejects_symlink_segment(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(outside)
    request = parse_discovery_request('{"action":"read_file","path":"link.py"}')

    with pytest.raises(DiscoveryContractError, match="symlink"):
        execute_discovery_request(tmp_path, request)


def test_source_race_observation_does_not_override_current_materialization(tmp_path):
    source = tmp_path / "current.py"
    source.write_text("before", encoding="utf-8")
    request = parse_discovery_request('{"action":"read_file","path":"current.py"}')
    observation = execute_discovery_request(tmp_path, request)
    source.write_text("after", encoding="utf-8")

    materialization = materialize_planner_source_context(
        tmp_path,
        task_description="Read current.py",
        expected_paths=observation.materialization_paths(),
    )

    assert observation.content == "before"
    assert (
        materialized_source_content(materialization, "current.py", tmp_path) == "after"
    )


def test_discovery_prompt_is_small_and_excludes_executable_plan_contract():
    prompt = build_discovery_prompt(
        "Find the current scheduled_task_execution implementation and its focused test.",
        "A bounded existing workspace is admitted.",
    )
    assert len(prompt) < 2500
    assert "READ-ONLY DISCOVERY ONLY" in prompt
    assert "Return exactly one JSON object" in prompt
    assert "final executable Plan" not in prompt
    assert "old/new" in prompt
