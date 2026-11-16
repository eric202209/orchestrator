"""PHASE34-PGR1 regressions for the bounded Planning generation contract.

PNO1 proved that the production Planning path
(``planning_flow`` -> ``PlannerService._execute_task_with_planning_lock`` ->
``OpenAIChatCompletionsRuntime.execute_task`` -> ``_chat``) sent **no**
``RuntimeInvocationOptions``.  The adapter therefore took its
``exact_contract=False`` branch, which emits neither a completion budget nor a
reasoning setting, so the provider default (reasoning enabled, budget =
``max_model_len - prompt``) applied and reasoning consumed the whole generation
before any Plan text was emitted.

Planning now declares its own generation contract.  Every test here is
provider-free: the outbound request is captured at the httpx boundary.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from app.config import settings
from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    _GENERIC_SYSTEM,
    _STEP_SYSTEM,
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.orchestration.planning.planner import (
    PLANNING_GENERATION_MAX_OUTPUT_TOKENS,
    PlannerService,
)

PLANNING_GATEWAY = "http://ai-gateway.test:8000/v1"
PLANNING_MODEL = "qwen-local"
PLANNING_PROFILE = "ollama_default"

# A Plan-shaped response: the contract repair must not touch plan semantics.
PLAN_RESPONSE = (
    '[{"step_number": 1, "description": "Create the module", '
    '"commands": [], "expected_files": ["src/greeting/formatting.py"]}]'
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _runtime(db, role: BackendRole, *, model: str = PLANNING_MODEL):
    configuration = RoleRuntimeConfiguration(
        role=role,
        backend_name="openai_chat_completions",
        model_family=model,
        adaptation_profile=PLANNING_PROFILE,
    )
    return OpenAIChatCompletionsRuntime(
        db, session_id=None, runtime_configuration=configuration
    )


def _planning_endpoint_configured(monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_DIRECT_BASE_URL", PLANNING_GATEWAY)
    monkeypatch.setattr(settings, "PLANNING_DIRECT_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_BASE_URL", PLANNING_GATEWAY)
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_TEMPERATURE", 0.1)
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_TOP_P", None)
    monkeypatch.setattr(settings, "OPENAI_CHAT_COMPLETIONS_REPEAT_PENALTY", None)


def _capture_dispatch(monkeypatch, response_content: str = PLAN_RESPONSE):
    captured: dict[str, object] = {}

    class _FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}
        content = b"{}"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {"content": response_content},
                        "finish_reason": "stop",
                    }
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update({"url": url, "headers": dict(headers or {}), "json": json})
            return _FakeResponse()

    monkeypatch.setattr(openai_chat_adapter.httpx, "AsyncClient", _FakeAsyncClient)
    return captured


class _FakeRuntimeService:
    """Only the surface the Planning generation policy reads."""

    def __init__(self, backend: str | None) -> None:
        self._backend = backend

    def get_backend_metadata(self) -> dict:
        if self._backend is None:
            raise RuntimeError("backend metadata unavailable")
        return {"backend": self._backend, "model_family": PLANNING_MODEL}


# ---------------------------------------------------------------------------
# Part 2 -- the declared policy
# ---------------------------------------------------------------------------


def test_planning_declares_bounded_no_reasoning_contract_for_chat_completions():
    options = PlannerService.planning_generation_invocation_options(
        _FakeRuntimeService("openai_chat_completions")
    )

    assert options is not None
    assert options.reasoning_enabled is False
    assert options.max_output_tokens == PLANNING_GENERATION_MAX_OUTPUT_TOKENS
    # The budget is the one the direct no-thinking planning route already uses.
    assert PLANNING_GENERATION_MAX_OUTPUT_TOKENS == 2048
    # Generation bounds only: nothing here changes the Plan contract itself.
    assert options.response_schema is None
    assert options.extra_provider_options is None
    assert options.system_prompt is None
    assert options.temperature is None
    assert options.timeout_seconds is None
    assert options.no_output_timeout_seconds is None


def test_planning_generation_policy_is_scoped_to_the_affected_backend():
    """D: no other backend/runtime inherits the Planning-only policy."""

    for backend in ("local_openclaw", "direct_ollama", "stub", "", None):
        assert (
            PlannerService.planning_generation_kwargs(_FakeRuntimeService(backend))
            == {}
        )
    assert PlannerService.planning_generation_kwargs(object()) == {}
    assert set(
        PlannerService.planning_generation_kwargs(
            _FakeRuntimeService("openai_chat_completions")
        )
    ) == {"invocation_options"}


# ---------------------------------------------------------------------------
# Part 1/3 -- the canonical Planning call sites declare the contract
# ---------------------------------------------------------------------------


def _planning_lock_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_execute_task_with_planning_lock"
    ]


def _declares_generation_contract(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg is not None:
            continue
        source = ast.dump(keyword.value)
        if "planning_generation_kwargs" in source:
            return True
    return False


def test_canonical_planning_calls_pass_explicit_invocation_options():
    """A: the production Planning invocations no longer run option-free."""

    flow_calls = _planning_lock_calls(
        REPO_ROOT / "app/services/orchestration/phases/planning_flow.py"
    )
    assert len(flow_calls) == 1
    assert _declares_generation_contract(flow_calls[0])

    planner_path = REPO_ROOT / "app/services/orchestration/planning/planner.py"
    planner_calls = _planning_lock_calls(planner_path)
    # The minimal and ultra-minimal Planning attempts are the same role and the
    # same generation contract; the definition site itself is not a call.
    assert len(planner_calls) == 2
    assert all(_declares_generation_contract(call) for call in planner_calls)

    # P42-V17-G: Discovery now reuses Planning's generation bounds (merged
    # with its own C4/C5 schema options) rather than running unbounded.
    discovery_path = (
        REPO_ROOT / "app/services/orchestration/planning/read_only_discovery.py"
    )
    discovery_calls = _planning_lock_calls(discovery_path)
    assert len(discovery_calls) == 1
    assert "planning_generation_invocation_options" in discovery_path.read_text()


def test_discovery_receives_bounded_contract_for_chat_completions(
    tmp_path, monkeypatch
):
    """P42-V17-G: Discovery carries the canonical Planning generation bounds."""

    from types import SimpleNamespace

    from app.services.orchestration.planning.read_only_discovery import (
        run_discovery_stage,
    )

    monkeypatch.setattr(settings, "LOW_RESOURCE_SINGLE_MODEL", False)
    captured: dict[str, object] = {}

    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            captured.update(kwargs)
            return {"status": "completed", "output": '{"action":"stop"}'}

    ctx = SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=_FakeRuntimeService("openai_chat_completions"),
        prompt="Inspect the existing implementation.",
        orchestration_state=SimpleNamespace(
            project_context="fixture", project_dir=tmp_path
        ),
        emit_live=lambda *args, **kwargs: None,
        session_id=1,
        task_id=2,
        task_execution_id=3,
    )
    run_discovery_stage(
        ctx=ctx,
        planning_timeout_seconds=120,
        extract_structured_text=lambda value: str(value),
        planner_service=_Planner,
        emit_phase_event=lambda *args, **kwargs: None,
    )

    options = captured["invocation_options"]
    assert options.reasoning_enabled is False
    assert options.max_output_tokens == PLANNING_GENERATION_MAX_OUTPUT_TOKENS
    assert options.response_schema is None
    assert options.extra_provider_options is None


def test_production_planning_invocation_reaches_provider_bounded(
    db_session, monkeypatch
):
    """B + C: the declared contract reaches the provider request.

    This replays the exact production shape: the canonical Planning kwargs plus
    the newly declared generation options, through
    ``_execute_task_with_planning_lock`` and the real adapter.
    """

    _planning_endpoint_configured(monkeypatch)
    captured = _capture_dispatch(monkeypatch)
    runtime = _runtime(db_session, BackendRole.PLANNING)
    planning_prompt = "Normalize customer-facing names. Return a JSON plan array."

    result = asyncio.run(
        PlannerService._execute_task_with_planning_lock(
            runtime,
            planning_prompt,
            timeout_seconds=240,
            reuse_task_session=False,
            diagnostic_label="PLANNING",
            diagnostic_metadata={"planning_attempt": "initial"},
            **PlannerService.planning_generation_kwargs(runtime),
        )
    )

    payload = captured["json"]
    # C -- bounded output budget on the wire.
    assert payload["max_tokens"] == PLANNING_GENERATION_MAX_OUTPUT_TOKENS
    # B -- reasoning disabled through the adapter's exact contract.
    assert payload["think"] is False
    assert payload["enable_thinking"] is False
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    # The Planning role's own system contract and prompt are unchanged.
    assert payload["messages"] == [
        {"role": "system", "content": _GENERIC_SYSTEM},
        {"role": "user", "content": planning_prompt},
    ]
    assert _STEP_SYSTEM not in str(payload)
    assert payload["stream"] is False
    assert payload["model"] == PLANNING_MODEL
    assert captured["url"] == f"{PLANNING_GATEWAY}/chat/completions"
    # E/F: the Plan text is returned verbatim for the existing parser.
    assert result["status"] == "completed"
    assert result["output"] == PLAN_RESPONSE


def test_generation_contract_changes_only_generation_fields(db_session, monkeypatch):
    """E/F/G/H: nothing but the generation bounds changes on the wire.

    Plan projection, CREATE_ONLY semantics, repair arbitration, APA/C8 path
    authority and the OpenClaw execution binding all read Planning's *output*.
    Proving the request differs from the pre-repair request only by the
    reasoning/budget fields shows none of their inputs moved.
    """

    _planning_endpoint_configured(monkeypatch)
    runtime = _runtime(db_session, BackendRole.PLANNING)
    planning_prompt = "Normalize customer-facing names. Return a JSON plan array."
    call_kwargs = dict(
        timeout_seconds=240,
        reuse_task_session=False,
        diagnostic_label="PLANNING",
        diagnostic_metadata={"planning_attempt": "initial"},
    )

    legacy_capture = _capture_dispatch(monkeypatch)
    asyncio.run(runtime.execute_task(planning_prompt, **call_kwargs))
    legacy_payload = dict(legacy_capture["json"])

    repaired_capture = _capture_dispatch(monkeypatch)
    asyncio.run(
        runtime.execute_task(
            planning_prompt,
            **call_kwargs,
            **PlannerService.planning_generation_kwargs(runtime),
        )
    )
    repaired_payload = dict(repaired_capture["json"])

    added = set(repaired_payload) - set(legacy_payload)
    removed = set(legacy_payload) - set(repaired_payload)
    changed = {
        key
        for key in set(legacy_payload) & set(repaired_payload)
        if legacy_payload[key] != repaired_payload[key]
    }
    assert added == {"max_tokens", "think", "enable_thinking", "chat_template_kwargs"}
    assert removed == set()
    assert changed == set()


def test_other_runtime_roles_keep_their_existing_request_shape(db_session, monkeypatch):
    """D: execution/repair invocations are untouched by the Planning policy."""

    _planning_endpoint_configured(monkeypatch)
    for role in (BackendRole.EXECUTION, BackendRole.REPAIR):
        captured = _capture_dispatch(monkeypatch)
        runtime = _runtime(db_session, role)
        asyncio.run(
            runtime.execute_task(
                "Execute step 1.",
                timeout_seconds=120,
                diagnostic_label="EXECUTION",
            )
        )
        payload = captured["json"]
        assert "max_tokens" not in payload
        assert "think" not in payload
        assert "enable_thinking" not in payload
        assert "chat_template_kwargs" not in payload
