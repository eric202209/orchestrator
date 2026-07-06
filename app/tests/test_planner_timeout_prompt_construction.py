import logging
import json

import pytest
from app.services.orchestration.planning.planner import (
    PlannerService,
    MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD,
    PlanningRepairOutputContractViolation,
)

from app.services.orchestration.planning.repair_evidence import (
    record_pending_planning_repair_triplet,
    write_failed_planning_repair_triplet,
)

from app.tests.planner_timeout_test_helpers import _valid_three_step_plan


def test_initial_planning_prompt_contains_valid_json_contract_example():
    from app.services.orchestration.prompt_templates import PromptTemplates

    prompt = PromptTemplates.build_planning_prompt(
        "Build a small React page",
        project_context="empty workspace",
        project_dir="/tmp/project",
    )

    assert prompt.startswith(
        "Return ONLY a valid JSON array. First character must be `[`. Last must be `]`.\n"
        "No prose. No markdown fences. No plan.json. No explanation."
    )
    assert "Valid Minimal JSON Example:" in prompt
    assert '"step_number": 1' in prompt
    assert '"description": "Inspect the current workspace"' in prompt
    assert '"commands": ["rg --files . | sort"]' in prompt
    assert (
        "No background processes, &, nohup, disown, dev servers, or long commands"
        in prompt
    )
    assert (
        "Verification must use `python -c`, `python -m`, `npm run build`, `node -e`, or a project test command"
        in prompt
    )
    assert "must prove behavior or content using current workspace evidence" in prompt
    assert "If a scaffold command is genuinely required" in prompt
    assert "use `ops` for any follow-up source edits" in prompt
    assert "Never use heredoc syntax" in prompt
    assert "Optional `ops` may contain these operations" in prompt
    assert "append_file, delete_file, mkdir, replace_in_file, write_file" in prompt
    assert '"op": "write_file"' in prompt
    assert '"commands": []' in prompt
    assert "no extra keys except optional `ops`" in prompt
    assert "No markdown. No prose." in prompt
    assert 'Objects like {"steps": [...]} instead of a top-level array' in prompt


def test_planning_prompt_with_operator_guidance_includes_precedence_before_task():
    from app.services.orchestration.planning.prompt_contracts import (
        OPERATOR_GUIDANCE_PRECEDENCE_LINE,
    )
    from app.services.orchestration.prompt_templates import PromptTemplates

    prompt = PromptTemplates.build_planning_prompt(
        "Add function add_label(label: str, labels: list[str] = []) -> list[str].",
        project_context=(
            "=== WORKING MEMORY ===\n\n"
            "Operator Guidance\n"
            "  - Never use mutable default arguments. Use None and initialize inside the function.\n"
        ),
        project_dir="/tmp/project",
    )

    assert OPERATOR_GUIDANCE_PRECEDENCE_LINE in prompt
    assert prompt.index(OPERATOR_GUIDANCE_PRECEDENCE_LINE) < prompt.index("**Task:**")


def test_planning_prompt_without_operator_guidance_omits_precedence_line():
    from app.services.orchestration.planning.prompt_contracts import (
        OPERATOR_GUIDANCE_PRECEDENCE_LINE,
    )
    from app.services.orchestration.prompt_templates import PromptTemplates

    prompt = PromptTemplates.build_planning_prompt(
        "Build a small React page",
        project_context="empty workspace",
        project_dir="/tmp/project",
    )

    assert OPERATOR_GUIDANCE_PRECEDENCE_LINE not in prompt


def test_minimal_planning_prompt_carries_operator_guidance_before_task(tmp_path):
    from app.services.orchestration.planning.prompt_contracts import (
        OPERATOR_GUIDANCE_PRECEDENCE_LINE,
    )

    prompt = PlannerService.build_minimal_planning_prompt(
        "Add function add_label(label: str, labels: list[str] = []) -> list[str].",
        project_dir=tmp_path,
        project_context=(
            "=== WORKING MEMORY ===\n\n"
            "Operator Guidance\n"
            "  - Never use mutable default arguments. Use None and initialize inside the function.\n"
        ),
    )

    assert OPERATOR_GUIDANCE_PRECEDENCE_LINE in prompt
    assert "Never use mutable default arguments" in prompt
    assert prompt.index(OPERATOR_GUIDANCE_PRECEDENCE_LINE) < prompt.index("Task:")


def test_minimal_planning_prompt_without_operator_guidance_omits_precedence_line(
    tmp_path,
):
    from app.services.orchestration.planning.prompt_contracts import (
        OPERATOR_GUIDANCE_PRECEDENCE_LINE,
    )

    prompt = PlannerService.build_minimal_planning_prompt(
        "Build a small React page",
        project_dir=tmp_path,
        project_context="empty workspace",
    )

    assert OPERATOR_GUIDANCE_PRECEDENCE_LINE not in prompt


def test_planning_repair_prompt_preserves_existing_source_materialization(tmp_path):
    (tmp_path / "src" / "small_cli").mkdir(parents=True)
    (tmp_path / "src" / "small_cli" / "__init__.py").write_text("")
    (tmp_path / "src" / "small_cli" / "cli.py").write_text(
        "def main(argv=None):\n    return 0\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cli.py").write_text(
        "from small_cli.cli import main\n\n"
        "def test_main_returns_zero():\n"
        "    assert main([]) == 0\n"
    )
    rejected_plan = [
        {
            "step_number": 1,
            "description": "Update CLI source and tests",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/small_cli/cli.py",
                    "content": "def main(argv=None):\n    return 0\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_cli.py",
                    "content": "from small_cli.cli import main\n",
                },
            ],
            "verification": "python -m pytest",
            "rollback": None,
            "expected_files": ["src/small_cli/cli.py", "tests/test_cli.py"],
        }
    ]

    prompt = PlannerService.build_planning_repair_prompt(
        "Add a small Python CLI feature and tests.",
        malformed_output=json.dumps(rejected_plan),
        project_dir=tmp_path,
        rejection_reasons=["plan_contains_immediate_repair_issues"],
    )

    assert "Source materialization preservation contract:" in prompt
    assert "the repaired plan must preserve those materialization obligations" in prompt
    assert (
        "Do not remove write_file, append_file, or replace_in_file operations" in prompt
    )
    assert (
        "A repaired plan that removes required source/test materialization is invalid"
        in prompt
    )
    assert "src/small_cli/cli.py" in prompt
    assert "tests/test_cli.py" in prompt


def test_failed_planning_repair_triplet_evidence_is_redacted(tmp_path):
    previous_plan = [
        {
            "step_number": 1,
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/small_cli/cli.py",
                    "content": "def main(): return 0",
                }
            ],
        }
    ]
    repaired_plan = [
        {
            "step_number": 1,
            "commands": ["python -m pytest"],
            "ops": [],
        }
    ]

    record_pending_planning_repair_triplet(
        project_dir=tmp_path,
        session_id=11,
        task_id=22,
        repair_attempt=1,
        previous_plan_text=json.dumps(previous_plan),
        repair_prompt=(
            "Repair this plan. OPENAI_API_KEY=sk-testsecret1234567890 "
            "Authorization: Bearer abc.def"
        ),
        repaired_plan_text=json.dumps(repaired_plan),
        metadata={"api_key": "sk-anothersecret123456"},
    )

    artifact_ref = write_failed_planning_repair_triplet(
        project_dir=tmp_path,
        session_id=11,
        task_id=22,
        repair_attempt=1,
        previous_plan=previous_plan,
        repaired_plan=repaired_plan,
        repaired_output_text=json.dumps(repaired_plan),
        arbitration={
            "outcome": "regressed",
            "arbitration_action": "reject_materialization_regression",
            "regression_labels": ["removed_materialization"],
        },
    )

    assert artifact_ref is not None
    artifact_path = (
        tmp_path
        / ".agent"
        / "planning-repair-evidence"
        / ("session_11_task_22_repair_attempt_1_failed.json")
    )
    payload = json.loads(artifact_path.read_text())
    serialized = json.dumps(payload)

    assert payload["artifact_type"] == "planning_repair_failed_arbitration_triplet"
    assert payload["previous_plan"][0]["ops"][0]["path"] == "src/small_cli/cli.py"
    assert payload["repaired_plan"][0]["ops"] == []
    assert "repair_prompt" in payload
    assert "sk-testsecret1234567890" not in serialized
    assert "sk-anothersecret123456" not in serialized
    assert "abc.def" not in serialized
    assert "<redacted>" in serialized


def test_minimal_and_ultra_minimal_planning_prompts_include_contract_example():
    minimal = PlannerService.build_minimal_planning_prompt(
        "Build a small React page",
        project_dir=__import__("pathlib").Path("/tmp/project"),
    )
    ultra = PlannerService.build_ultra_minimal_planning_prompt(
        "Build a small React page",
        project_dir=__import__("pathlib").Path("/tmp/project"),
    )

    for prompt in (minimal, ultra):
        assert prompt.startswith(
            "Return ONLY a valid JSON array. First character must be `[`. Last must be `]`.\n"
            "No prose. No markdown fences. No plan.json. No explanation."
        )
        assert "Valid minimal JSON example:" in prompt
        assert '"step_number": 1' in prompt
        assert '"commands": ["rg --files . | sort"]' in prompt
        assert "optional" in prompt
        assert "ops" in prompt
        assert (
            "no other keys" in prompt or "no extra keys except optional `ops`" in prompt
        )
        assert "No markdown. No prose." in prompt


def test_planning_repair_normalizes_fenced_json_array(tmp_path):
    events = []
    plan_json = json.dumps(_valid_three_step_plan())

    class Runtime:
        async def invoke_prompt(self, *args, **kwargs):
            return {"output": f"```json\n{plan_json}\n```"}

    result = PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a small Python utility",
        malformed_output="not json",
        project_dir=tmp_path,
        timeout_seconds=10,
        logger=logging.getLogger("test.planning_repair_normalizes_fenced_json_array"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="plan_validation_failed",
    )

    assert result["output"] == plan_json
    assert any(
        metadata.get("reason") == "planning_repair_fenced_json_normalized"
        for _, _, metadata in events
    )


def test_planning_repair_normalizes_fenced_json_array_with_trailing_text(tmp_path):
    events = []
    plan_json = json.dumps(_valid_three_step_plan())

    class Runtime:
        async def invoke_prompt(self, *args, **kwargs):
            return {
                "output": (
                    "```json\n"
                    f"{plan_json}\n"
                    "```\n"
                    "This plan now satisfies the contract."
                )
            }

    result = PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Update an existing static site",
        malformed_output="not json",
        project_dir=tmp_path,
        timeout_seconds=10,
        logger=logging.getLogger(
            "test.planning_repair_normalizes_fenced_json_array_with_trailing_text"
        ),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="single_step_full_lifecycle_plan",
    )

    assert result["output"] == plan_json
    assert any(
        metadata.get("reason") == "planning_repair_fenced_json_normalized"
        for _, _, metadata in events
    )


def test_planning_repair_still_rejects_prose_output(tmp_path):
    class Runtime:
        async def invoke_prompt(self, *args, **kwargs):
            return {"output": "Here is the repaired plan: []"}

    with pytest.raises(PlanningRepairOutputContractViolation):
        PlannerService.repair_output(
            runtime_service=Runtime(),
            task_description="Build a small Python utility",
            malformed_output="not json",
            project_dir=tmp_path,
            timeout_seconds=10,
            logger=logging.getLogger("test.planning_repair_still_rejects_prose_output"),
            emit_live=lambda *args, **kwargs: None,
            reason="plan_validation_failed",
        )


def test_minimal_prompt_retry_emits_prompt_size_diagnostics(tmp_path, monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    monkeypatch.setattr(
        planner_module,
        "OPENCLAW_PLANNING_LOCK_PATH",
        tmp_path / "planning.lock",
    )
    events = []
    captured = {}

    class Runtime:
        async def execute_task(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return {"status": "completed", "output": "[]"}

    PlannerService.retry_with_minimal_prompt(
        runtime_service=Runtime(),
        task_description="Build a small Python health checker",
        project_dir=tmp_path,
        timeout_seconds=300,
        logger=logging.getLogger("test.minimal_prompt_size_diagnostics"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="dense_planning_context",
        workflow_profile="default",
    )

    retry_metadata = next(
        metadata
        for _, _, metadata in events
        if metadata.get("retry") == "minimal_prompt_first"
    )
    attempt_metadata = next(
        metadata
        for _, _, metadata in events
        if metadata.get("strategy") == "minimal_prompt" and metadata.get("attempt") == 2
    )

    assert retry_metadata["minimal_prompt_chars"] == len(captured["prompt"])
    assert (
        retry_metadata["minimal_prompt_estimated_tokens"]
        == (len(captured["prompt"]) + 3) // 4
    )
    assert retry_metadata["minimal_prompt_token_threshold"] == (
        MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD
    )
    assert retry_metadata["ultra_dense_planning_context"] is False
    assert (
        attempt_metadata["minimal_prompt_estimated_tokens"]
        == retry_metadata["minimal_prompt_estimated_tokens"]
    )
    assert captured["kwargs"]["diagnostic_label"] == "MINIMAL_PLANNING"
    assert captured["kwargs"]["diagnostic_metadata"]["planning_attempt"] == "minimal"
    assert (
        captured["kwargs"]["diagnostic_metadata"]["minimal_prompt_estimated_tokens"]
        == retry_metadata["minimal_prompt_estimated_tokens"]
    )


def test_minimal_prompt_retry_skips_ultra_when_planner_has_no_model_output(
    tmp_path, monkeypatch
):
    events = []
    calls = []

    async def timeout_without_model_output(cls, runtime_service, prompt, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        exc = TimeoutError("Task execution failed: Task timed out after 209s")
        exc.runtime_diagnostics = {
            "timed_out": True,
            "timeout_seconds": 209,
            "duration_seconds": 239.5,
            "stdout_chars": 0,
            "stderr_chars": 78,
            "output_channel_used": "none",
            "stderr_contains_model_content": False,
            "stderr_contains_only_logs": True,
        }
        raise exc

    monkeypatch.setattr(
        PlannerService,
        "_execute_task_with_planning_lock",
        classmethod(timeout_without_model_output),
    )

    with pytest.raises(TimeoutError):
        PlannerService.retry_with_minimal_prompt(
            runtime_service=object(),
            task_description="Create an SVG and add it to index.html",
            project_dir=tmp_path,
            timeout_seconds=300,
            logger=logging.getLogger("test.no_model_output_planning_timeout"),
            emit_live=lambda level, message, metadata=None: events.append(
                (level, message, metadata or {})
            ),
            reason="dense_planning_context",
            workflow_profile="default",
        )

    assert len(calls) == 1
    assert calls[0]["kwargs"]["diagnostic_label"] == "MINIMAL_PLANNING"
    assert not any(
        metadata.get("strategy") == "ultra_minimal_prompt" for _, _, metadata in events
    )
    failure_event = next(
        metadata
        for level, _, metadata in events
        if level == "ERROR" and metadata.get("reason") == "planner_no_model_output"
    )
    assert failure_event["output_channel_used"] == "none"
    assert failure_event["stderr_contains_model_content"] is False
