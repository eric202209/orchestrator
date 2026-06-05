"""Characterize OpenAI Responses API output text extraction shapes."""

from __future__ import annotations

import pytest

from app.services.agents.interfaces import AgentRuntimeError
from app.services.agents.providers.openai_adapter import _extract_output_text


def test_extracts_output_text_shape():
    assert _extract_output_text({"output_text": "done"}) == "done"


def test_preserves_standard_string_output_text_behavior():
    assert _extract_output_text({"output_text": "  done\n"}) == "  done\n"


def test_extracts_list_content_text_parts():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "hello "},
                    {"type": "output_text", "text": "world"},
                ],
            }
        ]
    }
    assert _extract_output_text(payload) == "hello world"


def test_ignores_non_text_parts_when_text_parts_exist():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "refusal", "refusal": "no"},
                    {"type": "output_text", "text": "allowed text"},
                ],
            }
        ]
    }
    assert _extract_output_text(payload) == "allowed text"


def test_empty_output_raises_controlled_error():
    with pytest.raises(AgentRuntimeError, match="returned no text output"):
        _extract_output_text({"output": []})


def test_malformed_structured_output_raises_controlled_error():
    malformed_payloads = [
        {"output": {"text": "not a supported Responses shape"}},
        {"output": [{"content": {"text": "not a list"}}]},
        {"output": [{"content": [{"text": {"nested": "dict"}}]}]},
        {"content": {"text": "top-level content is unsupported"}},
    ]
    for payload in malformed_payloads:
        with pytest.raises(AgentRuntimeError, match="returned no text output"):
            _extract_output_text(payload)


def test_refusal_only_output_raises_controlled_error():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "refusal", "refusal": "cannot comply"},
                ],
            }
        ]
    }
    with pytest.raises(AgentRuntimeError, match="returned no text output"):
        _extract_output_text(payload)
