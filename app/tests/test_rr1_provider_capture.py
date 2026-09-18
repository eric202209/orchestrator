"""RR1 deterministic regressions: all-lane provider capture and accounting.

Covers RR1 sections 12 (lane inventory), 13 (correlated evidence),
14 (capture neutrality), 15 (0/1/N/UNKNOWN accounting), 24 (per-lane capture)
and 25 (unexpected-provider detection).

No real provider call is made anywhere in this module: every case uses a
synthetic provider boundary installed on the real adapter classes.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from app.services.agents.interfaces import AgentRuntimeError
from app.services.research.rr1 import lanes
from app.services.research.rr1.provider_capture import (
    CALLS_MANY,
    CALLS_NONE,
    CALLS_ONE,
    CALLS_UNKNOWN,
    EVIDENCE_LEVEL_BOUNDED_TEXT,
    ProviderCapture,
    provider_lane,
    research_correlation,
)

pytestmark = [pytest.mark.unit, pytest.mark.critical_regression]


class _FakeDescriptor:
    name = "synthetic-backend"
    preferred_retry_strategy = None


class _FakeConfiguration:
    backend_name = "synthetic-backend"
    model_family = "synthetic-model"
    adaptation_profile = "synthetic-profile"


class _SyntheticRuntime:
    """Stands in for a real adapter class; never reaches a provider."""

    backend_descriptor = _FakeDescriptor()
    runtime_configuration = _FakeConfiguration()
    backend_role = "planning"

    def __init__(self, *, output="synthetic output", failure=None):
        self.session_id = 11
        self.task_id = 22
        self.task_execution_id = 33
        self.project_id = 44
        self._output = output
        self._failure = failure
        self.received: list[dict] = []

    async def invoke_prompt(self, prompt, **kwargs):
        self.received.append({"prompt": prompt, **kwargs})
        if self._failure is not None:
            raise self._failure
        return {"status": "completed", "output": self._output}

    async def execute_task(self, prompt, timeout_seconds=300, **kwargs):
        # Mirrors OpenAIResponsesRuntime: delegates to invoke_prompt.
        return await self.invoke_prompt(
            prompt, timeout_seconds=timeout_seconds, session_prefix="direct"
        )


@pytest.fixture
def synthetic_runtime_target(monkeypatch):
    """Point the capture installer at a synthetic adapter class."""

    module = types.ModuleType("rr1_synthetic_provider_module")
    module._SyntheticRuntime = _SyntheticRuntime
    import sys

    monkeypatch.setitem(sys.modules, "rr1_synthetic_provider_module", module)
    monkeypatch.setattr(
        lanes,
        "PROVIDER_CAPABLE_RUNTIMES",
        (("rr1_synthetic_provider_module", "_SyntheticRuntime"),),
    )
    import app.services.research.rr1.provider_capture as capture_module

    monkeypatch.setattr(
        capture_module,
        "PROVIDER_CAPABLE_RUNTIMES",
        (("rr1_synthetic_provider_module", "_SyntheticRuntime"),),
    )
    yield


# ---------------------------------------------------------------------------
# Section 12 -- lane inventory
# ---------------------------------------------------------------------------


class TestLaneInventory:
    def test_inventory_is_definitive_and_self_describing(self):
        evidence = lanes.lane_inventory_evidence()
        assert evidence["lane_count"] == len(lanes.PROVIDER_LANES)
        assert evidence["lane_count"] >= 13
        assert set(evidence["provider_entry_methods"]) == {
            "invoke_prompt",
            "execute_task",
            "execute_task_with_streaming",
        }
        assert evidence["unattributed_lane"] == lanes.LANE_UNATTRIBUTED

    def test_inventory_covers_every_lane_the_prompt_requires(self):
        required = {
            "planning_initial",
            "planning_repair",
            "completion_repair",
            "debug_repair",
            "grounding",
            "read_only_discovery",
            "replan_failure_summary",
            "failure_reflection",
        }
        assert required <= set(lanes.LANE_IDS)

    def test_capture_does_not_assume_rer01o_planning_repair_coverage(self):
        # More than one lane must exist beyond the RER-01O capture lane, and
        # the entry-method set must exceed invoke_prompt alone.
        assert len(lanes.PROVIDER_LANES) > 1
        assert len(lanes.PROVIDER_ENTRY_METHODS) > 1

    def test_unknown_prefix_maps_to_unattributed(self):
        assert (
            lanes.lane_for_session_prefix("a-prefix-nobody-declared")
            == lanes.LANE_UNATTRIBUTED
        )
        assert lanes.lane_for_session_prefix(None) == lanes.LANE_UNATTRIBUTED

    def test_every_real_adapter_class_and_method_resolves(self):
        """The inventory must describe the code as it actually is."""

        import importlib
        import inspect

        found = 0
        for module_name, class_name in lanes.PROVIDER_CAPABLE_RUNTIMES:
            module = importlib.import_module(module_name)
            runtime_class = getattr(module, class_name)
            methods = [
                name
                for name in lanes.PROVIDER_ENTRY_METHODS
                if inspect.iscoroutinefunction(getattr(runtime_class, name, None))
            ]
            assert methods, f"{class_name} exposes no provider entry method"
            found += len(methods)
        assert found >= len(lanes.PROVIDER_CAPABLE_RUNTIMES)


# ---------------------------------------------------------------------------
# Sections 13 / 24 -- correlated capture
# ---------------------------------------------------------------------------


class TestCorrelatedCapture:
    def test_capture_answers_every_required_question(self, synthetic_runtime_target):
        runtime = _SyntheticRuntime(output="a plan")
        with ProviderCapture(
            research_run_id="rr1-run-1", evidence_level=EVIDENCE_LEVEL_BOUNDED_TEXT
        ) as capture:
            with research_correlation("corr-1"), provider_lane("planning_repair"):
                asyncio.run(
                    runtime.invoke_prompt(
                        "repair this plan",
                        timeout_seconds=45,
                        session_prefix="planning-repair",
                    )
                )

        (record,) = capture.accounting.records
        assert record.correlation_id == "corr-1"  # which run caused it
        assert record.lane_id == "planning_repair"  # which lane
        assert record.research_run_id == "rr1-run-1"
        assert record.session_id == 11 and record.task_id == 22
        assert record.task_execution_id == 33
        assert record.backend == "synthetic-backend"  # backend resolved
        assert record.model_family == "synthetic-model"
        assert record.role == "planning"
        assert record.timeout_seconds == 45.0  # timeout configuration
        assert record.started_at and record.ended_at  # begin/end
        assert record.duration_seconds is not None
        assert record.transport_completed is True  # transport completed
        assert record.content_returned is True  # content returned
        assert record.result_representation == "dict"  # representation
        assert record.parse_verdict == "text_output"  # normalization verdict
        assert record.prompt_excerpt == "repair this plan"
        assert record.output_excerpt == "a plan"

    def test_metadata_only_evidence_level_retains_no_text(
        self, synthetic_runtime_target
    ):
        runtime = _SyntheticRuntime(output="secret plan text")
        with ProviderCapture(research_run_id="rr1-run-1") as capture:
            with research_correlation("corr-1"):
                asyncio.run(
                    runtime.invoke_prompt("sensitive prompt", session_prefix="planning")
                )
        (record,) = capture.accounting.records
        assert record.prompt_excerpt is None
        assert record.output_excerpt is None
        assert record.prompt_chars == len("sensitive prompt")
        assert record.output_chars == len("secret plan text")

    def test_failed_transport_is_captured_not_lost(self, synthetic_runtime_target):
        runtime = _SyntheticRuntime(failure=AgentRuntimeError("provider exploded"))
        with ProviderCapture() as capture:
            with research_correlation("corr-1"), provider_lane("planning_initial"):
                with pytest.raises(AgentRuntimeError):
                    asyncio.run(
                        runtime.invoke_prompt("plan", session_prefix="planning")
                    )
        (record,) = capture.accounting.records
        assert record.transport_completed is False
        assert record.content_returned is False
        assert record.error_type == "AgentRuntimeError"
        assert record.parse_verdict == "not_reached"
        assert capture.accounting.verdict == CALLS_ONE

    def test_one_correlation_identity_spans_every_lane_in_a_run(
        self, synthetic_runtime_target
    ):
        runtime = _SyntheticRuntime()
        with ProviderCapture() as capture:
            with research_correlation("corr-run-7"):
                for lane_id, prefix in (
                    ("planning_initial", "planning"),
                    ("completion_repair", "completion-summary"),
                    ("debug_repair", "debug-repair"),
                ):
                    with provider_lane(lane_id):
                        asyncio.run(runtime.invoke_prompt("p", session_prefix=prefix))
        assert {r.correlation_id for r in capture.accounting.records} == {"corr-run-7"}
        assert capture.accounting.by_lane() == {
            "planning_initial": 1,
            "completion_repair": 1,
            "debug_repair": 1,
        }


# ---------------------------------------------------------------------------
# Section 14 -- capture neutrality
# ---------------------------------------------------------------------------


class TestCaptureNeutrality:
    def test_capture_on_and_off_are_behaviorally_equivalent(
        self, synthetic_runtime_target
    ):
        kwargs = {
            "timeout_seconds": 45,
            "session_prefix": "planning-repair",
            "no_output_timeout_seconds": 20,
        }

        off_runtime = _SyntheticRuntime(output="deterministic")
        off_result = asyncio.run(off_runtime.invoke_prompt("prompt text", **kwargs))

        on_runtime = _SyntheticRuntime(output="deterministic")
        with ProviderCapture():
            on_result = asyncio.run(on_runtime.invoke_prompt("prompt text", **kwargs))

        assert on_result == off_result
        assert on_runtime.received == off_runtime.received, (
            "capture must not alter the prompt, payload, timeout, or options "
            "reaching the provider boundary"
        )

    def test_capture_returns_the_adapter_result_object_unchanged(
        self, synthetic_runtime_target
    ):
        sentinel = {"status": "completed", "output": "x"}

        class _Identity(_SyntheticRuntime):
            async def invoke_prompt(self, prompt, **kwargs):
                return sentinel

        runtime = _Identity()
        import sys

        sys.modules["rr1_synthetic_provider_module"]._SyntheticRuntime = _Identity
        with ProviderCapture():
            result = asyncio.run(runtime.invoke_prompt("p", session_prefix="planning"))
        assert result is sentinel

    def test_removal_restores_the_original_methods(self, synthetic_runtime_target):
        original = _SyntheticRuntime.invoke_prompt
        capture = ProviderCapture()
        capture.install()
        assert _SyntheticRuntime.invoke_prompt is not original
        capture.remove()
        assert _SyntheticRuntime.invoke_prompt is original


# ---------------------------------------------------------------------------
# Section 15 -- accounting
# ---------------------------------------------------------------------------


class TestProviderCallAccounting:
    def test_zero_calls_reports_none(self, synthetic_runtime_target):
        with ProviderCapture() as capture:
            pass
        assert capture.accounting.call_count == 0
        assert capture.accounting.verdict == CALLS_NONE

    def test_one_and_many_are_distinguished(self, synthetic_runtime_target):
        runtime = _SyntheticRuntime()
        with ProviderCapture() as capture:
            asyncio.run(runtime.invoke_prompt("p", session_prefix="planning"))
            assert capture.accounting.verdict == CALLS_ONE
            asyncio.run(runtime.invoke_prompt("p", session_prefix="planning"))
            asyncio.run(runtime.invoke_prompt("p", session_prefix="planning"))
        assert capture.accounting.call_count == 3
        assert capture.accounting.verdict == CALLS_MANY

    def test_incomplete_capture_is_never_reported_as_zero(
        self, synthetic_runtime_target
    ):
        capture = ProviderCapture()
        capture.install()
        capture.sink.record_gap("synthetic_uninstrumented_boundary")
        assert capture.accounting.call_count == 0
        assert (
            capture.accounting.verdict == CALLS_UNKNOWN
        ), "a capture gap must degrade to UNKNOWN, never collapse to zero"
        capture.remove()

    def test_uninstalled_capture_is_unknown_not_zero(self):
        capture = ProviderCapture()
        assert capture.accounting.verdict == CALLS_UNKNOWN

    def test_unresolvable_runtime_records_a_gap(self, monkeypatch):
        import app.services.research.rr1.provider_capture as capture_module

        monkeypatch.setattr(
            capture_module,
            "PROVIDER_CAPABLE_RUNTIMES",
            (("rr1_module_that_does_not_exist", "Missing"),),
        )
        capture = ProviderCapture()
        capture.install()
        assert capture.accounting.verdict == CALLS_UNKNOWN
        assert any(
            gap.startswith("unresolvable_runtime:")
            for gap in capture.accounting.capture_gaps
        )
        capture.remove()

    def test_delegating_entry_methods_count_as_one_provider_call(
        self, synthetic_runtime_target
    ):
        runtime = _SyntheticRuntime()
        with ProviderCapture() as capture:
            with research_correlation("corr-1"), provider_lane("execution_step"):
                asyncio.run(runtime.execute_task("do the step"))
        # execute_task delegates to invoke_prompt: two boundary entries, one
        # provider invocation.
        assert len(capture.accounting.records) == 2
        assert capture.accounting.call_count == 1
        assert capture.accounting.verdict == CALLS_ONE
        outer = [r for r in capture.accounting.records if not r.nested]
        assert len(outer) == 1
        assert outer[0].entry_method == "execute_task"
        assert outer[0].nested_entries == [
            "rr1_synthetic_provider_module._SyntheticRuntime.invoke_prompt"
        ]


# ---------------------------------------------------------------------------
# Section 25 -- unexpected provider detection
# ---------------------------------------------------------------------------


class TestUnexpectedProviderDetection:
    def test_provider_free_path_that_calls_a_provider_is_detected(
        self, synthetic_runtime_target
    ):
        """The BR2 certification-incident shape, reproduced provider-free."""

        runtime = _SyntheticRuntime()

        with ProviderCapture(research_run_id="rr1-cert") as capture:
            with research_correlation("corr-cert"):
                # A path the harness believed was provider-free. No lane is
                # declared, and nothing told the accounting layer to expect a
                # call.
                asyncio.run(
                    runtime.invoke_prompt(
                        "unexpected", session_prefix="completion-summary"
                    )
                )

        assert capture.accounting.verdict == CALLS_ONE
        assert capture.accounting.call_count > 0
        unexpected = capture.accounting.unexpected_calls
        assert len(unexpected) == 1
        assert (
            unexpected[0].declared_lane is None
        ), "detection must not depend on the harness having expected a call"
        # Attribution survives even though nothing declared the lane.
        assert unexpected[0].correlation_id == "corr-cert"
        assert unexpected[0].research_run_id == "rr1-cert"
        assert unexpected[0].lane_id == "completion_repair"
        assert unexpected[0].session_id == 11

    def test_unexpected_call_with_no_recognizable_prefix_is_still_counted(
        self, synthetic_runtime_target
    ):
        runtime = _SyntheticRuntime()
        with ProviderCapture() as capture:
            asyncio.run(
                runtime.invoke_prompt("x", session_prefix="a-lane-nobody-declared")
            )
        assert capture.accounting.call_count == 1
        assert capture.accounting.by_lane() == {lanes.LANE_UNATTRIBUTED: 1}
        assert len(capture.accounting.unexpected_calls) == 1

    def test_declared_lane_calls_are_not_flagged_unexpected(
        self, synthetic_runtime_target
    ):
        runtime = _SyntheticRuntime()
        with ProviderCapture() as capture:
            with provider_lane("planning_initial"):
                asyncio.run(runtime.invoke_prompt("p", session_prefix="planning"))
        assert capture.accounting.unexpected_calls == []
