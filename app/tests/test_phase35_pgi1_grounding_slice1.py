"""Provider-free tests for PHASE35-PGI1 Slice 1 grounding mechanics."""

from __future__ import annotations

from dataclasses import fields
import inspect
from pathlib import Path
import subprocess

import pytest

from app.services.orchestration.planning.grounding import (
    GroundingBudgetAccounting,
    GroundingBudgetDelta,
    GroundingBudgetLimits,
    GroundingBudgetSnapshot,
    GroundingExecutionError,
    GroundingExecutor,
    GroundingOutcome,
    GroundingRequestRejection,
    GroundingActionKind,
    parse_grounding_request,
)


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, shell=False)
    return tmp_path


def _request(payload: dict[str, object], request_id: str = "request-1"):
    return parse_grounding_request(
        payload,
        grounding_run_id="grounding-run-1",
        request_id=request_id,
    )


def _inspect(root: Path, path: str = "app/main.py", request_id: str = "inspect-1"):
    return GroundingExecutor(root).execute(
        _request({"action": "inspect_file", "path": path}, request_id)
    )


def _resolve(root: Path, relation: str, locator: dict[str, object], request_id: str):
    return GroundingExecutor(root).execute(
        _request(
            {"action": "resolve_structure", "relation": relation, "locator": locator},
            request_id,
        )
    )


def test_action_union_is_closed_and_provenance_is_explicit():
    request = _request({"action": "search_text", "query": "needle", "scopes": ["app"]})

    assert request.action_identity == GroundingActionKind.SEARCH_TEXT.value
    assert request.provenance == "model_request"
    assert request.action.provenance == "model_request"
    assert request.normalized_payload["scopes"] == ("app",)
    forbidden = {
        "write",
        "replace",
        "old_text",
        "new_text",
        "mutation_offsets",
        "accepted_path_set",
        "apa_grant",
        "c8_authority",
        "task_text",
    }
    for action_type in type(request.action).__mro__[:1]:
        assert not forbidden.intersection(field.name for field in fields(action_type))


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "shell", "command": "pwd"},
        {"action": "search_text", "query": "needle", "scopes": ["app"], "extra": True},
        {"action": "inspect_file", "path": "app/main.py", "write": "bad"},
        {
            "action": "resolve_structure",
            "relation": "semantic_search",
            "locator": {"path": "app/main.py", "name": "main"},
        },
        {
            "action": "resolve_structure",
            "relation": "symbol_definition",
            "locator": {"path": "app/main.py", "name": "main", "task_text": "bad"},
        },
    ],
)
def test_unknown_actions_relations_and_fields_reject_fail_closed(payload):
    with pytest.raises(GroundingRequestRejection):
        _request(payload)


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/tmp/outside.py", "app/../outside.py", "app\\main.py"],
)
def test_path_validation_rejects_traversal_and_absolute_escape(path):
    with pytest.raises(GroundingRequestRejection):
        _request({"action": "inspect_file", "path": path})


def test_search_is_bounded_deterministic_and_truthfully_negative(tmp_path):
    root = _git_repo(
        tmp_path,
        {
            "src/b.py": "other = 'needle'\n",
            "src/a.py": "first = 'needle'\nsecond = 'needle'\n",
        },
    )
    executor = GroundingExecutor(root)
    request = _request(
        {"action": "search_text", "query": "needle", "scopes": ["src"]},
        "search-1",
    )
    observation = executor.execute(request)

    assert observation.outcome is GroundingOutcome.FOUND
    assert [(hit.path, hit.line_number) for hit in observation.hits] == [
        ("src/a.py", 1),
        ("src/a.py", 2),
        ("src/b.py", 1),
    ]
    assert observation.budget_delta.repository_actions == 1
    assert observation.budget_delta.positive_regions == 3
    assert observation.budget_delta.source_evidence_bytes == len(
        observation.bounded_content
    )
    assert observation.truncated is False
    assert observation.structural_facts["result_order"] == "path_line"

    negative = executor.execute(
        _request(
            {"action": "search_text", "query": "does-not-exist", "scopes": ["src"]},
            "search-2",
        )
    )
    assert negative.outcome is GroundingOutcome.NOT_FOUND
    assert negative.hits == ()
    assert negative.bounded_content == b""
    assert negative.budget_delta.repository_actions == 1
    assert negative.budget_delta.positive_regions == 0
    assert negative.budget_delta.source_evidence_bytes == 0


def test_search_result_and_snippet_bounds_are_mechanical(tmp_path):
    content = "\n".join(f"value_{index} = 'needle'" for index in range(30)) + "\n"
    root = _git_repo(tmp_path, {"src/many.py": content})
    observation = GroundingExecutor(root).execute(
        _request({"action": "search_text", "query": "needle", "scopes": ["src"]})
    )

    assert observation.outcome is GroundingOutcome.FOUND
    assert len(observation.hits) == 20
    assert observation.truncated is True
    assert len(observation.bounded_content) <= 8192
    assert all(len(hit.snippet) <= 240 for hit in observation.hits)


def test_inspect_file_returns_bounded_raw_source_and_generic_facts(tmp_path):
    source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/status')\n"
        "def status():\n"
        "    return {'ok': True}\n"
    )
    root = _git_repo(
        tmp_path,
        {"app/main.py": source, "app/large.py": "# padding\n" * 700},
    )
    first = _inspect(root)
    second = _inspect(root, request_id="inspect-2")
    large = _inspect(root, "app/large.py", "inspect-large")

    assert first.outcome is GroundingOutcome.FOUND
    assert len(first.bounded_content) <= 4096
    assert first.truncated is False
    assert first.structural_facts["parse_status"] == "ok"
    assert first.structural_facts["imports"] == ("from fastapi import APIRouter",)
    assert first.structural_facts["route_decorators"][0]["path"] == "/status"
    assert first.source_versions == second.source_versions
    assert first.source_hashes == second.source_hashes
    with pytest.raises(TypeError):
        first.source_versions["app/other.py"] = "forbidden"
    assert large.truncated is True
    assert large.structural_facts["parse_status"] == "source_truncated"
    assert "likely_target" not in first.structural_facts
    assert "relevance" not in first.structural_facts


def test_untracked_and_outside_reads_are_rejected(tmp_path):
    root = _git_repo(tmp_path, {"app/main.py": "def main():\n    pass\n"})
    (root / "app/untracked.py").write_text("secret = 1\n", encoding="utf-8")
    with pytest.raises(GroundingRequestRejection):
        _inspect(root, "app/untracked.py")
    with pytest.raises(GroundingRequestRejection):
        GroundingExecutor(root).execute(
            _request(
                {
                    "action": "search_text",
                    "query": "secret",
                    "scopes": ["app/untracked.py"],
                }
            )
        )


def test_missing_tracked_file_is_not_found(tmp_path):
    root = _git_repo(tmp_path, {"app/missing.py": "def old():\n    pass\n"})
    (root / "app/missing.py").unlink()
    observation = _inspect(root, "app/missing.py")
    assert observation.outcome is GroundingOutcome.NOT_FOUND
    assert observation.source_paths == ("app/missing.py",)


def test_symbol_definition_is_exact_and_ambiguous_when_duplicated(tmp_path):
    root = _git_repo(
        tmp_path,
        {
            "app/defs.py": (
                "literal = 'needle'\n"
                "def target():\n"
                "    return 'first'\n"
                "class Container:\n"
                "    def nested(self):\n"
                "        return 1\n"
            )
        },
    )
    found = _resolve(
        root,
        "symbol_definition",
        {"path": "app/defs.py", "name": "target"},
        "symbol-1",
    )
    assert found.outcome is GroundingOutcome.FOUND
    assert found.structural_identity.symbol_name == "target"
    assert found.structural_identity.start_line == 2
    assert found.structural_identity.relation.value == "symbol_definition"

    missing = _resolve(
        root,
        "symbol_definition",
        {"path": "app/defs.py", "name": "targeted"},
        "symbol-2",
    )
    assert missing.outcome is GroundingOutcome.NOT_FOUND

    (root / "app/defs.py").write_text(
        "def target():\n    pass\n\ndef target():\n    pass\n", encoding="utf-8"
    )
    ambiguous = _resolve(
        root,
        "symbol_definition",
        {"path": "app/defs.py", "name": "target"},
        "symbol-3",
    )
    assert ambiguous.outcome is GroundingOutcome.AMBIGUOUS
    assert ambiguous.structural_identity is None


def test_structural_parse_failure_is_an_execution_error(tmp_path):
    root = _git_repo(tmp_path, {"app/broken.py": "def broken(:\n    pass\n"})
    with pytest.raises(GroundingExecutionError):
        _resolve(
            root,
            "symbol_definition",
            {"path": "app/broken.py", "name": "broken"},
            "broken-structure",
        )


def test_enclosing_symbol_returns_smallest_exact_owner(tmp_path):
    root = _git_repo(
        tmp_path,
        {
            "app/nested.py": (
                "class Outer:\n"
                "    def inner(self):\n"
                "        value = 1\n"
                "        return value\n"
                "\n"
                "module_value = 2\n"
            )
        },
    )
    found = _resolve(
        root,
        "enclosing_symbol",
        {"path": "app/nested.py", "line": 3},
        "enclosing-1",
    )
    assert found.outcome is GroundingOutcome.FOUND
    assert found.structural_identity.symbol_name == "inner"
    assert found.structural_identity.start_line == 2
    no_owner = _resolve(
        root,
        "enclosing_symbol",
        {"path": "app/nested.py", "line": 6},
        "enclosing-2",
    )
    assert no_owner.outcome is GroundingOutcome.NOT_FOUND


def _route_files() -> dict[str, str]:
    return {
        "app/api/v1/endpoints/auth.py": (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.post('/login')\n"
            "async def login():\n"
            "    return {'ok': True}\n"
        ),
        "app/api/v1/router.py": (
            "from fastapi import APIRouter\n"
            "from app.api.v1.endpoints.auth import router as auth_router\n"
            "api_router = APIRouter()\n"
            "api_router.include_router(auth_router, prefix='/auth')\n"
        ),
    }


def test_mounted_route_preserves_local_effective_and_handler_identity(tmp_path):
    root = _git_repo(tmp_path, _route_files())
    observation = _resolve(
        root,
        "mounted_route",
        {
            "path": "app/api/v1/endpoints/auth.py",
            "method": "POST",
            "decorator_path": "/login",
        },
        "route-1",
    )
    identity = observation.structural_identity
    assert observation.outcome is GroundingOutcome.FOUND
    assert identity.decorator_path == "/login"
    assert identity.effective_route_path == "/auth/login"
    assert identity.handler_name == "login"
    assert identity.handler_identity.endswith(":login")
    assert identity.mount_chain == ("app/api/v1/router.py:/auth",)
    assert "app/api/v1/router.py" in observation.source_versions


@pytest.mark.parametrize(
    "method,path",
    [("GET", "/login"), ("POST", "/log"), ("POST", "/login-extra")],
)
def test_mounted_route_requires_exact_method_and_path(tmp_path, method, path):
    root = _git_repo(tmp_path, _route_files())
    observation = _resolve(
        root,
        "mounted_route",
        {
            "path": "app/api/v1/endpoints/auth.py",
            "method": method,
            "decorator_path": path,
        },
        f"route-negative-{method}-{path}",
    )
    assert observation.outcome is GroundingOutcome.NOT_FOUND


def test_unprefixed_route_is_resolved_without_suffix_approximation(tmp_path):
    root = _git_repo(
        tmp_path,
        {
            "app/routes.py": (
                "from fastapi import APIRouter\n"
                "router = APIRouter()\n"
                "@router.get('/health')\n"
                "def health():\n"
                "    return {'ok': True}\n"
            )
        },
    )
    observation = _resolve(
        root,
        "mounted_route",
        {"path": "app/routes.py", "method": "GET", "decorator_path": "/health"},
        "route-unprefixed",
    )
    assert observation.outcome is GroundingOutcome.FOUND
    assert observation.structural_identity.effective_route_path == "/health"
    assert observation.structural_identity.mount_chain == ()


def test_route_resolution_composes_literal_local_router_prefix(tmp_path):
    files = _route_files()
    files["app/api/v1/endpoints/auth.py"] = files[
        "app/api/v1/endpoints/auth.py"
    ].replace("router = APIRouter()", "router = APIRouter(prefix='/local')")
    root = _git_repo(tmp_path, files)
    observation = _resolve(
        root,
        "mounted_route",
        {
            "path": "app/api/v1/endpoints/auth.py",
            "method": "POST",
            "decorator_path": "/login",
        },
        "route-local-prefix",
    )
    assert observation.outcome is GroundingOutcome.FOUND
    assert observation.structural_identity.local_router_prefix == "/local"
    assert observation.structural_identity.effective_route_path == "/auth/local/login"


def test_dynamic_mount_prefix_cannot_be_downgraded_to_local_route(tmp_path):
    files = _route_files()
    files["app/api/v1/router.py"] = (
        files["app/api/v1/router.py"]
        .replace(
            "api_router = APIRouter()",
            "prefix_value = '/dynamic'\napi_router = APIRouter()",
        )
        .replace("prefix='/auth'", "prefix=prefix_value")
    )
    root = _git_repo(tmp_path, files)
    observation = _resolve(
        root,
        "mounted_route",
        {
            "path": "app/api/v1/endpoints/auth.py",
            "method": "POST",
            "decorator_path": "/login",
        },
        "route-dynamic-prefix",
    )
    assert observation.outcome is GroundingOutcome.NOT_FOUND
    assert observation.structural_identity is None


def test_ambiguous_mounts_are_not_collapsed_to_first_mount(tmp_path):
    files = _route_files()
    files["app/api/v1/routes.py"] = (
        "from app.api.v1.endpoints.auth import router as auth_router\n"
        "other_router = object()\n"
        "other_router.include_router(auth_router, prefix='/v2')\n"
    )
    root = _git_repo(tmp_path, files)
    observation = _resolve(
        root,
        "mounted_route",
        {
            "path": "app/api/v1/endpoints/auth.py",
            "method": "POST",
            "decorator_path": "/login",
        },
        "route-ambiguous",
    )
    assert observation.outcome is GroundingOutcome.AMBIGUOUS
    assert observation.structural_identity is None
    assert len(observation.structural_facts["candidates"]) == 2


def test_source_identity_changes_and_old_observation_keeps_original(tmp_path):
    root = _git_repo(tmp_path, {"app/main.py": "def main():\n    return 1\n"})
    first = _inspect(root, request_id="version-1")
    (root / "app/main.py").write_text("def main():\n    return 2\n", encoding="utf-8")
    second = _inspect(root, request_id="version-2")

    assert first.source_versions["app/main.py"] != second.source_versions["app/main.py"]
    assert first.source_hashes["app/main.py"] != second.source_hashes["app/main.py"]
    assert b"return 1" in first.bounded_content
    assert b"return 2" in second.bounded_content


def test_budget_deltas_are_monotonic_and_limits_reject_without_observation(tmp_path):
    accounting = GroundingBudgetAccounting()
    first = GroundingBudgetDelta(
        repository_actions=1, source_evidence_bytes=10, positive_regions=1
    )
    second = GroundingBudgetDelta(repository_actions=1)
    after_first = accounting.apply(first)
    after_second = after_first.apply(second)
    assert after_first.snapshot.repository_actions == 1
    assert after_second.snapshot.repository_actions == 2
    assert after_second.snapshot.source_evidence_bytes == 10
    with pytest.raises(GroundingRequestRejection):
        after_second.apply(
            GroundingBudgetDelta(repository_actions=1),
            GroundingBudgetLimits(repository_actions=2),
        )

    root = _git_repo(tmp_path, {"app/main.py": "x = 1\n"})
    executor = GroundingExecutor(root)
    with pytest.raises(GroundingRequestRejection):
        executor.execute(
            _request({"action": "inspect_file", "path": "app/main.py"}),
            budget=GroundingBudgetSnapshot(repository_actions=1),
            limits=GroundingBudgetLimits(repository_actions=1),
        )


def test_outcomes_rejection_and_execution_error_are_distinct(tmp_path, monkeypatch):
    root = _git_repo(tmp_path, {"app/main.py": "needle = 1\n"})
    negative = GroundingExecutor(root).execute(
        _request({"action": "search_text", "query": "missing", "scopes": ["app"]})
    )
    assert negative.outcome is GroundingOutcome.NOT_FOUND
    (root / "app/untracked.py").write_text("secret = 1\n", encoding="utf-8")
    with pytest.raises(GroundingRequestRejection):
        _inspect(root, "app/untracked.py")

    monkeypatch.setattr(
        "app.services.orchestration.planning.grounding.executor.shutil.which",
        lambda _: None,
    )
    with pytest.raises(GroundingExecutionError):
        GroundingExecutor(root).execute(
            _request({"action": "search_text", "query": "needle", "scopes": ["app"]})
        )


def test_executor_has_no_task_text_or_provider_dependency_and_legacy_path_remains_available():
    signature = inspect.signature(GroundingExecutor.execute)
    assert "task_description" not in signature.parameters
    assert "operator_task" not in signature.parameters
    package_root = (
        Path(__file__).parents[1] / "services/orchestration/planning/grounding"
    )
    production_text = "\n".join(
        path.read_text(encoding="utf-8") for path in package_root.glob("*.py")
    )
    assert "PlannerService" not in production_text
    assert "OpenClaw" not in production_text
    assert "Ollama" not in production_text
    assert "task_description" not in production_text
    planning_flow = (
        Path(__file__).parents[1] / "services/orchestration/phases/planning_flow.py"
    )
    discovery = (
        Path(__file__).parents[1]
        / "services/orchestration/planning/read_only_discovery.py"
    )
    planning_text = planning_flow.read_text(encoding="utf-8")
    assert "ENABLE_TYPED_GROUNDING_COORDINATOR" in planning_text
    assert "prepare_discovery_context" in planning_text
    assert "planning.grounding" not in discovery.read_text(encoding="utf-8").lower()


def test_request_and_observation_contracts_exclude_mutation_authority_fields(tmp_path):
    root = _git_repo(tmp_path, {"app/main.py": "def main():\n    pass\n"})
    observation = _inspect(root)
    forbidden = {
        "write",
        "replace",
        "old_text",
        "new_text",
        "mutation_offsets",
        "accepted_path_set",
        "apa_grant",
        "c8_authority",
        "target_hint",
    }
    request_fields = {
        item.name
        for item in fields(
            type(_request({"action": "inspect_file", "path": "app/main.py"}))
        )
    }
    observation_fields = {item.name for item in fields(type(observation))}
    assert not forbidden.intersection(request_fields)
    assert not forbidden.intersection(observation_fields)
