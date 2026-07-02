"""Phase 17B: Tests for ReflectionRetryStrategy."""

from __future__ import annotations

from app.services.orchestration.recovery.failure_event import make_failure_event
from app.services.orchestration.recovery.strategies.reflection_retry import (
    RecoveryResult,
    ReflectionRetryStrategy,
)


def _failure_event(failure_class: str = "unknown_failure"):
    return make_failure_event(
        failure_class=failure_class,
        source="unknown",
        error_message="something went wrong",
        session_id=1,
        task_id=1,
    )


# ── return type ───────────────────────────────────────────────────────────────


def test_execute_returns_recovery_result():
    result = ReflectionRetryStrategy.execute(
        failure_event=_failure_event(),
        llm_callable=lambda _: "apply the fix",
    )
    assert isinstance(result, RecoveryResult)


def test_execute_with_llm_callable_succeeds():
    result = ReflectionRetryStrategy.execute(
        failure_event=_failure_event(),
        llm_callable=lambda _: "here is the corrective action",
    )
    assert result.success is True
    assert result.outcome == "success"
    assert result.strategy == "retry_with_reflection"


def test_execute_sets_failure_class():
    result = ReflectionRetryStrategy.execute(
        failure_event=_failure_event("debug_parse_error"),
        llm_callable=lambda _: "fix the parse issue",
    )
    assert result.failure_class == "debug_parse_error"


def test_execute_captures_llm_output():
    result = ReflectionRetryStrategy.execute(
        failure_event=_failure_event(),
        llm_callable=lambda _: "check the JSON format",
    )
    assert result.llm_output is not None
    assert "check the JSON format" in result.llm_output


def test_execute_records_duration():
    result = ReflectionRetryStrategy.execute(
        failure_event=_failure_event(),
        llm_callable=lambda _: "output",
    )
    assert isinstance(result.duration_ms, int)
    assert result.duration_ms >= 0


# ── no llm_callable ───────────────────────────────────────────────────────────


def test_execute_skipped_when_no_llm_callable():
    result = ReflectionRetryStrategy.execute(
        failure_event=_failure_event(),
        llm_callable=None,
    )
    assert result.success is False
    assert result.outcome == "skipped"
    assert result.error == "no_llm_callable"


# ── LLM failure ───────────────────────────────────────────────────────────────


def test_execute_handles_llm_raising():
    def _bad_llm(_prompt: str) -> str:
        raise RuntimeError("LLM call failed")

    result = ReflectionRetryStrategy.execute(
        failure_event=_failure_event(),
        llm_callable=_bad_llm,
    )
    assert result.success is False
    assert result.outcome == "failed"
    assert "llm_callable_raised" in (result.error or "")


# ── NO_RECOVERY_POSSIBLE sentinel ─────────────────────────────────────────────


def test_execute_fails_on_no_recovery_sentinel():
    result = ReflectionRetryStrategy.execute(
        failure_event=_failure_event(),
        llm_callable=lambda _: "NO_RECOVERY_POSSIBLE",
    )
    assert result.success is False
    assert result.outcome == "failed"
    assert result.error == "no_recovery_possible"


def test_execute_fails_on_empty_output():
    result = ReflectionRetryStrategy.execute(
        failure_event=_failure_event(),
        llm_callable=lambda _: "",
    )
    assert result.success is False
    assert result.outcome == "failed"


# ── one retry only (no recursion) ────────────────────────────────────────────


def test_execute_calls_llm_exactly_once():
    call_count = [0]

    def _counting_llm(_prompt: str) -> str:
        call_count[0] += 1
        return "fix applied"

    ReflectionRetryStrategy.execute(
        failure_event=_failure_event(),
        llm_callable=_counting_llm,
    )
    assert call_count[0] == 1


# ── prompt content ────────────────────────────────────────────────────────────


def test_reflection_prompt_contains_failure_class():
    received_prompts = []

    def _capturing_llm(prompt: str) -> str:
        received_prompts.append(prompt)
        return "ok"

    ReflectionRetryStrategy.execute(
        failure_event=_failure_event("debug_parse_error"),
        llm_callable=_capturing_llm,
    )
    assert received_prompts
    assert "debug_parse_error" in received_prompts[0]


def test_reflection_prompt_includes_error_message():
    received_prompts = []
    ev = make_failure_event(
        failure_class="unknown_failure",
        source="unknown",
        error_message="ValueError: bad input",
    )

    def _capturing_llm(prompt: str) -> str:
        received_prompts.append(prompt)
        return "ok"

    ReflectionRetryStrategy.execute(failure_event=ev, llm_callable=_capturing_llm)
    assert "ValueError: bad input" in received_prompts[0]
