"""Characterize direct_ollama chat response content shapes."""

from __future__ import annotations

import pytest

from app.services.agents.interfaces import AgentRuntimeError
from app.services.agents.providers import ollama_adapter
from app.services.agents.providers.ollama_adapter import (
    OllamaRuntime,
    _extract_ollama_chat_content,
    _strip_thinking,
)


def _body(content):
    return {"choices": [{"message": {"content": content}}]}


def test_direct_ollama_string_content_is_preserved():
    assert _extract_ollama_chat_content(_body("plain text")) == "plain text"


def test_direct_ollama_string_content_strips_thinking():
    assert _strip_thinking("<think>private reasoning</think>visible answer") == (
        "visible answer"
    )


def test_direct_ollama_none_content_is_deterministic_empty_text():
    assert _extract_ollama_chat_content(_body(None)) == ""
    assert _strip_thinking(None) == ""


def test_direct_ollama_dict_text_field_extracts_text():
    assert _extract_ollama_chat_content(_body({"text": "dict text"})) == "dict text"


def test_direct_ollama_dict_output_text_field_extracts_text():
    assert (
        _extract_ollama_chat_content(_body({"output_text": "output text"}))
        == "output text"
    )


def test_direct_ollama_nested_content_string_and_list_extracts_text():
    assert (
        _extract_ollama_chat_content(
            _body({"content": [{"text": "alpha "}, {"content": "beta"}]})
        )
        == "alpha beta"
    )


def test_direct_ollama_list_content_with_text_like_parts_is_concatenated():
    assert (
        _extract_ollama_chat_content(
            _body(
                [
                    {"type": "text", "text": "alpha "},
                    {"type": "output_text", "output_text": "beta "},
                    {"content": {"text": "gamma"}},
                ]
            )
        )
        == "alpha beta gamma"
    )


def test_direct_ollama_unsupported_dict_or_list_content_does_not_raise_type_error():
    assert _extract_ollama_chat_content(_body({"metadata": {"tokens": 3}})) == ""
    assert _extract_ollama_chat_content(_body([{"metadata": {"tokens": 3}}])) == ""
    assert _strip_thinking({"metadata": {"tokens": 3}}) == ""
    assert _strip_thinking([{"metadata": {"tokens": 3}}]) == ""


@pytest.mark.asyncio
async def test_direct_ollama_malformed_response_missing_choices_preserves_controlled_error(
    db_session, monkeypatch
):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "missing choices"}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(ollama_adapter.httpx, "AsyncClient", FakeAsyncClient)
    runtime = OllamaRuntime(db_session, session_id=None)

    with pytest.raises(AgentRuntimeError, match="'choices'") as exc_info:
        await runtime._chat(system="system", user="user")

    assert isinstance(exc_info.value.__cause__, KeyError)
