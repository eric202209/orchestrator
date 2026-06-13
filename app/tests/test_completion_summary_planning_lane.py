"""Tests for completion_summary._call_planning_lane backend fix.

Verifies that _generate_task_summary_with_fallback uses a direct HTTP
completion call (planning lane) instead of ctx.runtime_service.execute_task.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.orchestration.phases.completion_summary import (
    _call_planning_lane,
    _deterministic_task_summary,
    _generate_task_summary_with_fallback,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(state: Any = None) -> MagicMock:
    ctx = MagicMock()
    ctx.orchestration_state = state or MagicMock(execution_results=[], plan=[])
    ctx.emit_live = MagicMock()
    return ctx


def _chat_response(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"total_tokens": 50},
    }


# ---------------------------------------------------------------------------
# _call_planning_lane
# ---------------------------------------------------------------------------


class TestCallPlanningLane:
    def test_returns_content_from_response(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = _chat_response("LLM summary text")
        mock_resp.raise_for_status = MagicMock()

        async def _run():
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client_cls.return_value = mock_client
                return await _call_planning_lane("test prompt")

        result = asyncio.run(_run())
        assert result == "LLM summary text"

    def test_empty_choices_returns_empty_string(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choices": []}
        mock_resp.raise_for_status = MagicMock()

        async def _run():
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client_cls.return_value = mock_client
                return await _call_planning_lane("test prompt")

        result = asyncio.run(_run())
        assert result == ""

    def test_list_content_joined(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [
                {"message": {"content": [{"text": "part1"}, {"text": "part2"}]}}
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        async def _run():
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client_cls.return_value = mock_client
                return await _call_planning_lane("test prompt")

        result = asyncio.run(_run())
        assert result == "part1part2"

    def test_uses_planning_repair_settings(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = _chat_response("ok")
        mock_resp.raise_for_status = MagicMock()
        captured = {}

        async def _run():
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)

                async def _post(url, **kwargs):
                    captured["url"] = url
                    captured["model"] = kwargs.get("json", {}).get("model")
                    return mock_resp

                mock_client.post = _post
                mock_client_cls.return_value = mock_client
                with patch(
                    "app.services.orchestration.phases.completion_summary.settings"
                ) as mock_settings:
                    mock_settings.PLANNING_REPAIR_BASE_URL = (
                        "http://test-gateway:9999/v1"
                    )
                    mock_settings.PLANNING_REPAIR_MODEL = "test-model"
                    mock_settings.PLANNING_REPAIR_API_KEY = ""
                    return await _call_planning_lane("prompt")

        asyncio.run(_run())
        assert "test-gateway:9999" in captured["url"]
        assert captured["model"] == "test-model"

    def test_api_key_added_to_headers_when_set(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = _chat_response("ok")
        mock_resp.raise_for_status = MagicMock()
        captured_headers: dict = {}

        async def _run():
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)

                async def _post(url, *, headers=None, **kwargs):
                    captured_headers.update(headers or {})
                    return mock_resp

                mock_client.post = _post
                mock_client_cls.return_value = mock_client
                with patch(
                    "app.services.orchestration.phases.completion_summary.settings"
                ) as mock_settings:
                    mock_settings.PLANNING_REPAIR_BASE_URL = "http://x/v1"
                    mock_settings.PLANNING_REPAIR_MODEL = "m"
                    mock_settings.PLANNING_REPAIR_API_KEY = "secret-key"
                    return await _call_planning_lane("prompt")

        asyncio.run(_run())
        assert captured_headers.get("Authorization") == "Bearer secret-key"

    def test_no_auth_header_when_api_key_empty(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = _chat_response("ok")
        mock_resp.raise_for_status = MagicMock()
        captured_headers: dict = {}

        async def _run():
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)

                async def _post(url, *, headers=None, **kwargs):
                    captured_headers.update(headers or {})
                    return mock_resp

                mock_client.post = _post
                mock_client_cls.return_value = mock_client
                with patch(
                    "app.services.orchestration.phases.completion_summary.settings"
                ) as mock_settings:
                    mock_settings.PLANNING_REPAIR_BASE_URL = "http://x/v1"
                    mock_settings.PLANNING_REPAIR_MODEL = "m"
                    mock_settings.PLANNING_REPAIR_API_KEY = ""
                    return await _call_planning_lane("prompt")

        asyncio.run(_run())
        assert "Authorization" not in captured_headers


# ---------------------------------------------------------------------------
# _generate_task_summary_with_fallback — planning lane path
# ---------------------------------------------------------------------------


class TestGenerateWithFallback:
    def _patch_planning_lane(self, output: str):
        return patch(
            "app.services.orchestration.phases.completion_summary._call_planning_lane",
            new=AsyncMock(return_value=output),
        )

    def test_flag_off_returns_deterministic(self):
        ctx = _make_ctx()
        with patch.dict("os.environ", {}, clear=False):
            # Ensure flag is absent
            import os

            os.environ.pop("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY", None)
            result = _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")
        assert result["fallback"] is True
        assert "Task completed" in result["output"]

    def test_flag_on_calls_planning_lane_not_runtime_service(self):
        ctx = _make_ctx()
        ctx.runtime_service = MagicMock()

        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with self._patch_planning_lane("LLM generated summary"):
                result = _generate_task_summary_with_fallback(
                    ctx=ctx, summary_prompt="prompt"
                )

        ctx.runtime_service.execute_task.assert_not_called()
        assert result["output"] == "LLM generated summary"
        assert result.get("fallback") is not True

    def test_planning_lane_exception_triggers_fallback(self):
        ctx = _make_ctx()

        async def _fail(prompt):
            raise RuntimeError("connection refused")

        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with patch(
                "app.services.orchestration.phases.completion_summary._call_planning_lane",
                new=_fail,
            ):
                result = _generate_task_summary_with_fallback(
                    ctx=ctx, summary_prompt="prompt"
                )

        assert result["fallback"] is True
        assert "Task completed" in result["output"]
        assert "connection refused" in result.get("error", "")
        ctx.emit_live.assert_called_once()

    def test_empty_llm_output_triggers_fallback(self):
        ctx = _make_ctx()

        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with self._patch_planning_lane(""):
                result = _generate_task_summary_with_fallback(
                    ctx=ctx, summary_prompt="prompt"
                )

        assert result["fallback"] is True
        assert "Task completed" in result["output"]

    def test_timeout_triggers_fallback(self):
        ctx = _make_ctx()

        async def _slow(prompt):
            await asyncio.sleep(9999)
            return "never"

        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with patch(
                "app.services.orchestration.phases.completion_summary._call_planning_lane",
                new=_slow,
            ):
                with patch(
                    "app.services.orchestration.phases.completion_summary.SUMMARY_TIMEOUT_SECONDS",
                    0.01,
                ):
                    result = _generate_task_summary_with_fallback(
                        ctx=ctx, summary_prompt="prompt"
                    )

        assert result["fallback"] is True

    def test_runtime_service_never_called_when_flag_on(self):
        """Regression: ctx.runtime_service must never be touched by the summary path."""
        ctx = _make_ctx()
        ctx.runtime_service = MagicMock()

        with patch.dict("os.environ", {"ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1"}):
            with self._patch_planning_lane("summary text"):
                _generate_task_summary_with_fallback(ctx=ctx, summary_prompt="p")

        ctx.runtime_service.assert_not_called()
        ctx.runtime_service.execute_task.assert_not_called()
