"""Regression guards for Phase 24A-3 legacy-engine retirement."""

from pathlib import Path

from app.main import app
from app.services.agents.agent_backends import get_backend_descriptor
from app.services.agents.providers import get_runtime_factory


ROOT = Path(__file__).resolve().parents[2]


def test_legacy_session_execute_route_is_not_advertised():
    paths = set(app.openapi()["paths"])

    assert "/api/v1/sessions/{session_id}/execute" not in paths
    assert "/api/v1/tasks/{task_id}/execute" in paths
    assert "/api/v1/tasks/{task_id}/retry" in paths
    assert "/api/v1/sessions/{session_id}/tasks/{task_id}/run" in paths


def test_production_code_has_no_legacy_engine_symbols():
    production_files = [
        ROOT / "app",
        ROOT / "frontend" / "src",
        ROOT / "scripts",
    ]
    excluded = {"test_phase24a3_legacy_engine_retirement.py"}
    forbidden = (
        "execute_task_with_orchestration",
        "openclaw_orchestration",
        "execute_task_payload",
        "/sessions/{session_id}/execute",
    )

    for base in production_files:
        for path in base.rglob("*"):
            if (
                not path.is_file()
                or path.name in excluded
                or path.suffix not in {".py", ".ts", ".tsx", ".sh", ".ps1"}
            ):
                continue
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert not any(marker in text for marker in forbidden), path


def test_canonical_and_direct_execution_surfaces_remain_distinct():
    paths = set(app.openapi()["paths"])

    assert "/api/v1/tasks/{task_id}/execute" in paths
    assert "/api/v1/tasks/{task_id}/retry" in paths
    assert "/api/v1/sessions/{session_id}/tasks/{task_id}/run" in paths
    assert "/api/v1/sessions/{session_id}/execute" not in paths


def test_supported_provider_factories_remain_registered():
    for backend in (
        "local_openclaw",
        "remote_openclaw_gateway",
        "openai_responses_api",
        "openai_chat_completions",
        "direct_ollama",
    ):
        assert get_backend_descriptor(backend).name == backend
        assert get_runtime_factory(backend) is not None


def test_ollama_is_still_available_as_a_planning_provider():
    assert get_backend_descriptor("direct_ollama").name == "direct_ollama"
    assert get_runtime_factory("direct_ollama") is not None
