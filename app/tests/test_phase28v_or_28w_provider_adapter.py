"""Focused Protocol v2 OpenClaw result-boundary regression tests."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from app.services.agents.runtime_adapters.openclaw_adapter import (
    OpenClawProviderContractError,
    parse_openclaw_provider_result,
)
from app.services.planning.planning_brief_stage import (
    PlanningBriefProviderInput,
    build_planning_brief_request,
)
from app.services.planning.providers.openclaw import OpenClawPlanningProvider
from app.services.planning.structured_task_plan_stage import (
    build_structured_task_plan_request,
)


def _envelope(text: str, *, meta: dict | None = None, payloads: list | None = None):
    return {
        "payloads": payloads if payloads is not None else [{"text": text}],
        "meta": meta or {"agentMeta": {"sessionId": "probe-session"}},
    }


def _process(
    *,
    stdout: str | bytes = "",
    stderr: str | bytes = "",
    returncode: int = 0,
):
    return subprocess.CompletedProcess(
        args=["openclaw", "agent", "--json"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_documented_envelope_on_stderr_is_extracted_without_stdout_fallback():
    result = parse_openclaw_provider_result(
        _process(stderr=json.dumps(_envelope('{"probe":"ok"}'))),
        expected_session_id="probe-session",
    )

    assert result["status"] == "completed"
    assert result["output"] == '{"probe":"ok"}'
    assert result["output_channel_used"] == "stderr"


def test_valid_stdout_result_preserves_stderr_as_diagnostics():
    result = parse_openclaw_provider_result(
        _process(
            stdout=json.dumps(_envelope('{"probe":"ok"}')),
            stderr="OpenClaw diagnostic: model selected",
        ),
        expected_session_id="probe-session",
    )

    assert result["output_channel_used"] == "stdout"
    assert result["provider_result_diagnostics"]["stderr_diagnostic_bytes"] > 0


def test_valid_envelope_after_runtime_stderr_diagnostics_is_extracted():
    diagnostic_prefix = "runtime diagnostic: provider selected\n"
    result = parse_openclaw_provider_result(
        _process(stderr=diagnostic_prefix + json.dumps(_envelope('{"probe":"ok"}'))),
        expected_session_id="probe-session",
    )

    assert result["output"] == '{"probe":"ok"}'
    assert result["output_channel_used"] == "stderr"
    assert result["provider_result_diagnostics"]["stderr_diagnostic_bytes"] == len(
        diagnostic_prefix.encode("utf-8")
    )


def test_empty_result_is_provider_result_missing():
    with pytest.raises(OpenClawProviderContractError) as caught:
        parse_openclaw_provider_result(_process())
    assert caught.value.classification == "provider_result_missing"


def test_multiple_final_payloads_are_ambiguous():
    with pytest.raises(OpenClawProviderContractError) as caught:
        parse_openclaw_provider_result(
            _process(
                stderr=json.dumps(
                    _envelope(
                        "ignored",
                        payloads=[{"text": "one"}, {"text": "two"}],
                    )
                )
            )
        )
    assert caught.value.classification == "provider_result_ambiguous"


def test_invalid_envelope_is_not_scraped_as_candidate():
    with pytest.raises(OpenClawProviderContractError) as caught:
        parse_openclaw_provider_result(
            _process(stderr=json.dumps({"payloads": [{"text": "not enough"}]}))
        )
    assert caught.value.classification == "provider_result_missing"


def test_nonzero_process_without_documented_result_is_process_failure():
    with pytest.raises(OpenClawProviderContractError) as caught:
        parse_openclaw_provider_result(
            _process(stderr="OpenClaw failed before producing a result", returncode=1)
        )
    assert caught.value.classification == "provider_process_failure"


def test_valid_result_with_diagnostics_remains_semantic_result():
    result = parse_openclaw_provider_result(
        _process(
            stdout=json.dumps(_envelope('{"ok":true}')),
            stderr="warning: diagnostic only",
            returncode=7,
        )
    )
    assert result["status"] == "completed"
    assert result["provider_result_diagnostics"]["process_warning"] is True


def test_aborted_timeout_result_is_provider_timeout():
    with pytest.raises(OpenClawProviderContractError) as caught:
        parse_openclaw_provider_result(
            _process(
                stderr=json.dumps(
                    _envelope(
                        "partial",
                        meta={"aborted": True, "stopReason": "timeout"},
                    )
                )
            )
        )
    assert caught.value.classification == "provider_timeout"


def test_truncated_result_is_missing_not_raw_candidate():
    with pytest.raises(OpenClawProviderContractError) as caught:
        parse_openclaw_provider_result(_process(stderr='{"payloads": ['))
    assert caught.value.classification == "provider_result_missing"


def test_oversized_result_is_bounded():
    with pytest.raises(OpenClawProviderContractError) as caught:
        parse_openclaw_provider_result(
            _process(stderr=json.dumps(_envelope("x" * (512 * 1024 + 1))))
        )
    assert caught.value.classification == "provider_output_failure"


def test_utf8_result_is_preserved_and_invalid_utf8_is_rejected():
    result = parse_openclaw_provider_result(
        _process(
            stderr=json.dumps(_envelope('{"message":"café ✓"}'), ensure_ascii=False)
        )
    )
    assert result["output"] == '{"message":"café ✓"}'

    with pytest.raises(OpenClawProviderContractError) as caught:
        parse_openclaw_provider_result(_process(stderr=b"\xff"))
    assert caught.value.classification == "provider_output_failure"


def test_arbitrary_stderr_is_never_a_candidate():
    with pytest.raises(OpenClawProviderContractError) as caught:
        parse_openclaw_provider_result(_process(stderr='{"probe":"not an envelope"}'))
    assert caught.value.classification == "provider_result_missing"


def test_planning_brief_adapter_uses_shared_v2_contract(monkeypatch):
    calls = {}

    def fake_invoke(_db, _prompt, **kwargs):
        calls["prompt"] = _prompt
        calls.update(kwargs)
        return {"status": "completed", "output": '{"brief":true}'}

    monkeypatch.setattr(
        "app.services.planning.providers.openclaw.invoke_runtime_prompt",
        fake_invoke,
    )
    request = PlanningBriefProviderInput(
        manifest_id="manifest:probe",
        manifest_hash="hash",
        manifest_schema_version="v1",
        sources=(),
        stage_configuration={},
    )

    response = OpenClawPlanningProvider(None).generate(
        build_planning_brief_request(request)
    )
    assert response.candidate_text == '{"brief":true}'
    assert response.provider_name == "openclaw"
    assert calls["session_prefix"] == "planning-brief"
    assert "no_output_timeout_seconds" not in calls
    assert calls["timeout_seconds"] == 360
    assert '"verification_method"' in calls["prompt"]
    assert '"impact_if_false"' in calls["prompt"]
    assert '"change_permission"' in calls["prompt"]
    assert "Return exactly one JSON object" in calls["prompt"]


def test_task_plan_adapter_uses_shared_v2_contract(monkeypatch):
    calls = {}

    def fake_invoke(_db, _prompt, **kwargs):
        calls["prompt"] = _prompt
        calls.update(kwargs)
        return {"status": "completed", "output": '{"tasks":[]}'}

    monkeypatch.setattr(
        "app.services.planning.providers.openclaw.invoke_runtime_prompt",
        fake_invoke,
    )
    provider_input = SimpleNamespace(
        stage_configuration={},
        schema_instructions={},
        canonical_bytes=lambda: b"{}",
        to_dict=lambda: {},
        brief_checkpoint_id="checkpoint:probe",
        brief_hash="brief-hash",
        manifest_id="manifest:probe",
        manifest_hash="manifest-hash",
        project_id=None,
    )
    response = OpenClawPlanningProvider(None).generate(
        build_structured_task_plan_request(provider_input)
    )
    assert response.candidate_text == '{"tasks":[]}'
    assert calls["session_prefix"] == "structured-task-plan"
    assert "no_output_timeout_seconds" not in calls
    assert calls["timeout_seconds"] == 360
    assert '"Task"' in calls["prompt"]
    assert '"Dependency"' in calls["prompt"]
    assert '"ExecutionGroup"' in calls["prompt"]
    assert '"IntentionalOmission"' in calls["prompt"]
