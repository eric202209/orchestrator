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
    RuntimePlanningBriefProvider,
)
from app.services.planning.structured_task_plan_stage import (
    RuntimeStructuredTaskPlanProvider,
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
        calls.update(kwargs)
        return {"status": "completed", "output": '{"brief":true}'}

    monkeypatch.setattr(
        "app.services.planning.planning_brief_stage.invoke_runtime_prompt",
        fake_invoke,
    )
    request = PlanningBriefProviderInput(
        manifest_id="manifest:probe",
        manifest_hash="hash",
        manifest_schema_version="v1",
        sources=(),
        stage_configuration={},
    )

    assert RuntimePlanningBriefProvider(None).generate(request) == '{"brief":true}'
    assert calls["session_prefix"] == "planning-brief"
    assert calls["no_output_timeout_seconds"] == 320
    assert calls["timeout_seconds"] == 360


def test_task_plan_adapter_uses_shared_v2_contract(monkeypatch):
    calls = {}

    def fake_invoke(_db, _prompt, **kwargs):
        calls.update(kwargs)
        return {"status": "completed", "output": '{"tasks":[]}'}

    monkeypatch.setattr(
        "app.services.planning.structured_task_plan_stage.invoke_runtime_prompt",
        fake_invoke,
    )
    provider_input = SimpleNamespace(
        stage_configuration={},
        canonical_bytes=lambda: b"{}",
    )
    assert (
        RuntimeStructuredTaskPlanProvider(None).generate(provider_input)
        == '{"tasks":[]}'
    )
    assert calls["session_prefix"] == "structured-task-plan"
    assert calls["no_output_timeout_seconds"] == 320
    assert calls["timeout_seconds"] == 360
