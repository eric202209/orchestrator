"""Phase 40C-3 Planning JSON, capability, and evidence regressions."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.services.agents.agent_backends import BackendCapabilities
from app.services.agents.providers.openai_chat_adapter import (
    _structured_response_format,
)
from app.services.observability.planning_provider_evidence import (
    PlanningRepairResponseEvidence,
    planning_response_evidence_target_from_runtime,
)
from app.services.orchestration.error_handler import EnhancedErrorHandler
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.validation.rules.core_execution_capability import (
    render_planning_execution_capability_contract,
)
from app.services.orchestration.validation.rules.core_schema import (
    PLAN_REQUIRED_STEP_FIELDS,
    PLAN_SUPPORTED_SOURCE_OPS,
    planning_response_json_schema,
    validate_plan_schema,
)
from app.services.orchestration.validation.parsing import (
    parse_planning_json_document,
)
from app.services.orchestration.validation.parsing import extract_plan_steps


def _structured_runtime() -> SimpleNamespace:
    capabilities = BackendCapabilities(
        supports_planning=True,
        supports_step_execution=False,
        supports_debug_repair=False,
        supports_streaming=False,
        supports_checkpoint_resume=False,
        supports_tool_execution=False,
        supports_json_mode=False,
        supports_step_reasoning=True,
        supports_agent_workspace_binding=False,
    )
    return SimpleNamespace(
        backend_descriptor=SimpleNamespace(capabilities=capabilities)
    )


def _agent_runtime() -> SimpleNamespace:
    capabilities = BackendCapabilities(
        supports_planning=True,
        supports_step_execution=True,
        supports_debug_repair=True,
        supports_streaming=True,
        supports_checkpoint_resume=True,
        supports_tool_execution=True,
        supports_json_mode=True,
        supports_step_reasoning=True,
        supports_agent_workspace_binding=True,
    )
    return SimpleNamespace(
        backend_descriptor=SimpleNamespace(capabilities=capabilities)
    )


def test_raw_newlines_tabs_and_control_bytes_are_parsed_literally():
    raw = (
        '[{"step_number":1,"description":"write source","commands":[],'
        '"verification":null,"rollback":null,"expected_files":["normalize.py"],'
        '"ops":[{"op":"write_file","path":"normalize.py",'
        '"content":"def normalize(value):\n\treturn value\x01"}]}]'
    )
    handler = EnhancedErrorHandler()

    success, parsed, strategy = handler.attempt_json_parsing(raw, context="planning")

    assert success is True
    assert parsed[0]["ops"][0]["content"] == "def normalize(value):\n\treturn value\x01"
    assert "bounded raw-control JSON normalization" in strategy
    metadata = handler.last_planning_parse_metadata
    assert metadata["failure_category"] == "invalid_control_character"
    assert metadata["serialization_normalization_applied"] is True
    assert len(metadata["normalized_structure_sha256"]) == 64


def test_escaped_newlines_and_valid_json_remain_strict_and_unchanged():
    payload = [{"step_number": 1, "ops": [{"content": "line one\nline two"}]}]
    valid = json.dumps(payload)
    handler = EnhancedErrorHandler()

    success, parsed, strategy = handler.attempt_json_parsing(valid, context="planning")

    assert success is True
    assert parsed == payload
    assert strategy == ""
    assert (
        handler.last_planning_parse_metadata["serialization_normalization_applied"]
        is False
    )
    assert handler.last_planning_parse_metadata["failure_category"] is None


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ('[{"step_number":1,"description":"unfinished', "truncated_json"),
        ("The repair is complete.", "invalid_value"),
    ],
)
def test_truncated_json_and_prose_are_not_accepted(text: str, category: str):
    document = parse_planning_json_document(text)
    assert document.success is False
    assert document.failure_category == category

    handler = EnhancedErrorHandler()
    success, parsed, _strategy = handler.attempt_json_parsing(text, context="planning")
    assert success is False
    assert parsed is None
    assert handler.last_planning_parse_metadata["failure_category"] == category


def test_repair_parser_uses_the_same_bounded_raw_control_tolerance():
    raw_plan = (
        '[{"step_number":1,"description":"write source","commands":[],'
        '"verification":null,"rollback":null,"expected_files":["normalize.py"],'
        '"ops":[{"op":"write_file","path":"normalize.py",'
        '"content":"def normalize(value):\n    return value"}]}]'
    )

    normalized = PlannerService._normalize_repair_json_array_output(raw_plan)

    assert normalized is not None
    assert json.loads(normalized)[0]["ops"][0]["content"] == (
        "def normalize(value):\n    return value"
    )
    assert (
        PlannerService._normalize_repair_json_array_output(
            '[{"step_number":1,"description":"truncated'
        )
        is None
    )
    assert (
        PlannerService._normalize_repair_json_array_output("The repair is complete.")
        is None
    )
    assert PlannerService._normalize_repair_json_array_output('{"steps": []}') is None


def test_invalid_top_level_missing_fields_and_malformed_operations_fail_schema():
    assert validate_plan_schema(42)["valid"] is False
    assert extract_plan_steps(42) is None

    missing_fields = [{"step_number": 1, "description": "missing fields"}]
    result = validate_plan_schema(missing_fields)
    assert result["valid"] is False
    assert result["details"]["missing_required_fields"] == {
        1: ["commands", "expected_files", "rollback", "verification"]
    }

    malformed_operation = [
        {
            "step_number": 1,
            "description": "malformed operation",
            "commands": [],
            "verification": None,
            "rollback": None,
            "expected_files": ["normalize.py"],
            "ops": [{"op": "replace_in_file", "path": "normalize.py", "old": "x"}],
        }
    ]
    assert validate_plan_schema(malformed_operation)["valid"] is False


def test_execution_contract_is_derived_from_structured_executor(tmp_path: Path):
    contract = render_planning_execution_capability_contract(
        runtime_service=_structured_runtime(), project_dir=tmp_path
    )

    assert "Resolved topology: structured orchestrator" in contract
    assert "Plan ops from {append_file, replace_in_file, write_file}" in contract
    assert "rg --files ." in contract
    assert "pytest -q tests" in contract
    assert "python -m py_compile module.py" in contract
    assert "project-importing inline Python are not available" in contract
    assert (
        "preserve the objective, source scope, and verification checks"
        in contract.lower()
    )
    assert 'python -c "from package' not in contract


def test_execution_contract_distinguishes_tool_capable_runtime(tmp_path: Path):
    contract = render_planning_execution_capability_contract(
        runtime_service=_agent_runtime(), project_dir=tmp_path
    )

    assert "Resolved topology: agent runtime" in contract
    assert "supports_tool_execution" in contract
    assert "configured runtime's declared capabilities" in contract
    assert "Resolved topology: structured orchestrator" not in contract


def test_prompt_contract_is_prepended_before_task_content(tmp_path: Path):
    prompt = PlannerService.include_execution_capability_contract(
        "Task objective: repair normalize.py.",
        runtime_service=_structured_runtime(),
        project_dir=tmp_path,
    )

    assert prompt.startswith("## EXECUTION CAPABILITY CONTRACT")
    assert prompt.index("## EXECUTION CAPABILITY CONTRACT") < prompt.index(
        "Task objective"
    )


def test_planning_repair_prompt_retains_actionable_capability_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured_prompts: list[str] = []

    async def _capture_repair_prompt(
        _runtime_service: Any, prompt: str, _timeout: int, **_kwargs: Any
    ) -> dict[str, Any]:
        captured_prompts.append(prompt)
        return {"output": "[]", "diagnostics": {}}

    monkeypatch.setattr(
        PlannerService,
        "_invoke_repair_prompt",
        staticmethod(_capture_repair_prompt),
    )
    malformed = json.dumps(
        [
            {
                "step_number": 1,
                "description": "inspect source",
                "commands": ['python -c "from package.module import value"'],
                "verification": None,
                "rollback": None,
                "expected_files": ["package/module.py"],
                "ops": [],
            }
        ]
    )
    PlannerService.repair_output(
        runtime_service=SimpleNamespace(),
        task_description="Repair package/module.py and verify the fix.",
        malformed_output=malformed,
        project_dir=tmp_path,
        timeout_seconds=30,
        logger=logging.getLogger("phase40c3-test"),
        emit_live=lambda *_args, **_kwargs: None,
        reason="unsupported_step_command",
        rejection_reasons=[
            "unsupported_step_command: rejected 'python -c from package'; "
            "structured runtime has no command tools. Use Plan ops for edits; "
            "keep goal, scope, checks."
        ],
    )

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert prompt.startswith("## EXECUTION CAPABILITY CONTRACT")
    assert "this runtime has no native command tools" in prompt
    assert "python -m pytest -q tests" in prompt
    assert "Use Plan ops for edits" in prompt
    assert "keep goal, scope, checks" in prompt
    assert "drop work" in prompt
    assert "relabel an edit as verification" in prompt


def test_json_schema_mode_is_derived_from_plan_schema_and_provider_adapter():
    schema = planning_response_json_schema()
    step_schema = schema["items"]
    assert set(step_schema["required"]) == PLAN_REQUIRED_STEP_FIELDS | {"ops"}
    assert step_schema["additionalProperties"] is False
    operation_schema = step_schema["properties"]["ops"]["items"]["anyOf"]
    operation_names = {
        branch["properties"]["op"]["enum"][0] for branch in operation_schema
    }
    assert operation_names == PLAN_SUPPORTED_SOURCE_OPS
    assert "delete_file" not in operation_names
    assert _structured_response_format(schema)["type"] == "json_schema"


def test_repair_requests_schema_only_from_verified_chat_adapter(
    monkeypatch: pytest.MonkeyPatch,
):
    seen_options = []

    class _RepairRuntime:
        backend_descriptor = SimpleNamespace(name="openai_chat_completions")

        async def invoke_prompt(self, _prompt: str, **kwargs: Any) -> dict[str, Any]:
            seen_options.append(kwargs["invocation_options"])
            return {"output": "[]", "diagnostics": {}}

    repair_runtime = _RepairRuntime()
    monkeypatch.setattr(settings, "PLANNING_JSON_SCHEMA_MODE_ENABLED", True)
    monkeypatch.setattr(
        "app.services.agents.agent_runtime.create_agent_runtime",
        lambda *_args, **_kwargs: repair_runtime,
    )

    result = asyncio.run(
        PlannerService._invoke_repair_prompt(
            SimpleNamespace(db=object(), session_id=10, task_id=20),
            "Return one Plan array.",
            30,
        )
    )

    assert result["output"] == "[]"
    assert len(seen_options) == 1
    assert seen_options[0].response_schema == planning_response_json_schema()
    assert seen_options[0].stream is False


def test_planning_response_evidence_retains_contract_parse_and_validation(
    tmp_path: Path,
):
    path = tmp_path / "provider-response.json"
    evidence = PlanningRepairResponseEvidence(
        path,
        correlation_id="planning-response-test",
        backend="openai_chat_completions",
        model="qwen3-coder:30b",
        role="planning",
        endpoint="http://host.docker.internal:11434/v1/chat/completions",
        logical_timeout_seconds=30,
        transport_timeout_seconds=30,
        capture_scope="planning_generation",
    )
    prompt = (
        "## EXECUTION CAPABILITY CONTRACT\n"
        "Resolved topology: structured orchestrator.\n\n"
        "Task objective: repair normalize.py."
    )
    response = '[{"step_number":1,"ops":[{"content":"line one\nline two"}]}]'
    evidence.start(
        prompt=prompt,
        payload_keys=["model", "messages", "response_format"],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "plan", "schema": {"type": "array"}},
        },
    )
    evidence.record_http_response(
        status_code=200,
        content_type="application/json",
        raw_body=b'{"choices":[]}',
    )
    evidence.record_envelope(
        {
            "model": "qwen3-coder:30b",
            "choices": [
                {
                    "message": {"role": "assistant", "content": response},
                    "finish_reason": "stop",
                }
            ],
        }
    )
    evidence.record_content_stages(
        assistant_content=response,
        extracted_content=response,
        adapter_normalized_content=response,
        planner_visible_content=response,
        transformed=False,
        classification="unchanged_exact_contract",
    )
    evidence.record_planning_parse(
        status="accepted",
        failure_category="invalid_control_character",
        serialization_normalization_applied=True,
        normalized_structure=[{"step_number": 1}],
    )
    evidence.record_plan_validation(
        schema_validation={"status": "passed", "errors": []},
        final_plan_validation={"status": "accepted", "reasons": []},
    )
    evidence.complete()

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["capture_scope"] == "planning_generation"
    assert document["request"]["execution_capability_contract"]["present"] is True
    assert document["request"]["response_format"]["type"] == "json_schema"
    assert len(document["request"]["response_format"]["schema_sha256"]) == 64
    assert document["representations"]["planner_visible_content"]["value"] == response
    assert document["planning_parse"]["serialization_normalization_applied"] is True
    assert document["plan_validation"]["schema_validation"]["status"] == "passed"
    assert document["plan_validation"]["final_plan_validation"]["status"] == "accepted"


def test_planning_response_capture_is_opt_in(monkeypatch: pytest.MonkeyPatch):
    runtime = SimpleNamespace(
        get_backend_metadata=lambda: {"backend": "openai_chat_completions"}
    )
    monkeypatch.setattr(settings, "PLANNING_PROVIDER_RESPONSE_EVIDENCE_CAPTURE", False)
    options = PlannerService.planning_generation_invocation_options(runtime)
    assert options is not None
    assert options.provider_response_evidence_path is None

    monkeypatch.setattr(settings, "PLANNING_PROVIDER_RESPONSE_EVIDENCE_CAPTURE", True)
    monkeypatch.setattr(settings, "PLANNING_JSON_SCHEMA_MODE_ENABLED", True)
    monkeypatch.setattr(
        "app.services.orchestration.planning.planner.planning_response_evidence_target_from_runtime",
        lambda _runtime: (
            "/dev/runtime/planning-evidence/provider-response.json",
            "call-1",
        ),
    )
    options = PlannerService.planning_generation_invocation_options(runtime)
    assert options is not None
    assert options.provider_response_evidence_path.endswith("provider-response.json")
    assert options.provider_response_evidence_correlation_id == "call-1"
    assert options.stream is None
    assert options.response_schema == planning_response_json_schema()


def test_planning_response_evidence_resolves_project_from_task_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.services.observability import planning_provider_evidence

    class _Query:
        def filter(self, *_args: Any) -> "_Query":
            return self

        def first(self) -> SimpleNamespace:
            return SimpleNamespace(project_id=8)

    class _Database:
        def query(self, _model: Any) -> _Query:
            return _Query()

    resolved: dict[str, Any] = {}

    def _resolve_location(*, db: Any, project_id: int, control_state_location: Any):
        resolved.update(db=db, project_id=project_id)
        return tmp_path

    monkeypatch.setattr(
        planning_provider_evidence,
        "_resolve_control_state_location",
        _resolve_location,
    )

    target = planning_response_evidence_target_from_runtime(
        SimpleNamespace(db=_Database(), task_id=17)
    )

    assert target is not None
    assert resolved["project_id"] == 8
    assert target[0].endswith(
        "planning-evidence/" + target[1] + "/provider-response.json"
    )
