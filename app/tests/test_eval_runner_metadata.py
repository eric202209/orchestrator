from pathlib import Path

from scripts.evals.run_orchestrator_eval_slice import _run_context_metadata


def test_eval_runner_records_architecture_named_lane_metadata(monkeypatch):
    monkeypatch.setenv("PLANNER_MODEL", "gpt-5.5")
    monkeypatch.setenv("PLANNING_BACKEND", "openai_responses_api")
    monkeypatch.setenv("PLANNING_REPAIR_MODEL", "gpt-5.5")
    monkeypatch.setenv("DEBUG_REPAIR_BACKEND", "openai_responses_api")
    monkeypatch.setenv("DEBUG_REPAIR_MODEL", "gpt-5.5")
    monkeypatch.setenv("EXECUTION_BACKEND", "local_openclaw")
    monkeypatch.setenv("EXECUTION_MODEL", "qwen3.6")

    metadata = _run_context_metadata(repo_root=Path("."), repeat_seed=None)

    assert metadata["planner_model"] == "gpt-5.5"
    assert metadata["planner_backend"] == "openai_responses_api"
    assert metadata["planning_repair_model"] == "gpt-5.5"
    assert metadata["planning_repair_backend"] == "openai_responses_api"
    assert metadata["debug_repair_model"] == "gpt-5.5"
    assert metadata["debug_repair_backend"] == "openai_responses_api"
    assert metadata["execution_model"] == "qwen3.6"
    assert metadata["execution_backend"] == "local_openclaw"
