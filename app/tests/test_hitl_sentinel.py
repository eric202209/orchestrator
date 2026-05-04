"""Tests for hitl_sentinel.py — render/parse symmetry and edge cases."""

from __future__ import annotations

import pytest

from app.services.orchestration.hitl_sentinel import parse, render


class TestRender:
    def test_wraps_payload_in_delimiters(self):
        result = render({"intervention_type": "approval", "prompt": "ok?"})
        assert result.startswith("<<<HITL_REQUEST:")
        assert result.endswith(">>>")

    def test_compact_json_no_spaces(self):
        result = render({"a": 1, "b": 2})
        assert " " not in result

    def test_roundtrip(self):
        payload = {
            "intervention_type": "guidance",
            "prompt": "What path?",
            "context": {"step": 3},
        }
        assert parse(render(payload)) == payload


class TestParse:
    def test_returns_none_for_empty(self):
        assert parse("") is None
        assert parse(None) is None  # type: ignore[arg-type]

    def test_returns_none_when_no_sentinel(self):
        assert parse("some normal agent output") is None

    def test_extracts_approval(self):
        sentinel = '<<<HITL_REQUEST:{"intervention_type":"approval","prompt":"Delete prod?","context":{}}>>>'
        result = parse(sentinel)
        assert result == {
            "intervention_type": "approval",
            "prompt": "Delete prod?",
            "context": {},
        }

    def test_extracts_from_surrounding_text(self):
        output = 'I need confirmation.\n<<<HITL_REQUEST:{"intervention_type":"approval","prompt":"proceed?","context":{}}>>>\nwaiting.'
        result = parse(output)
        assert result is not None
        assert result["intervention_type"] == "approval"

    def test_returns_none_for_invalid_json(self):
        assert parse("<<<HITL_REQUEST:{bad json}>>>") is None

    def test_multiline_context(self):
        payload = {
            "intervention_type": "information",
            "prompt": "Check this",
            "context": {"files": ["a.py", "b.py"], "step": 2},
        }
        assert parse(render(payload)) == payload

    def test_all_intervention_types(self):
        for itype in ("approval", "guidance", "information"):
            payload = {"intervention_type": itype, "prompt": "msg", "context": {}}
            assert parse(render(payload))["intervention_type"] == itype
