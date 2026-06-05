from __future__ import annotations

import json
import subprocess

from app.services.agents.openclaw_response import parse_openclaw_response
from app.services.agents.openclaw_service import OpenClawSessionService


def _parse_stdout(stdout: str, *, returncode: int = 0, stderr: str = ""):
    logs: list[tuple[str, str]] = []

    result = parse_openclaw_response(
        subprocess.CompletedProcess(
            args=["openclaw"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
        lambda level, message, **_: logs.append((level, message)),
    )

    return result, logs


def test_plain_stdout_text_produces_string_output():
    result, logs = _parse_stdout("plain assistant text")

    assert result["status"] == "completed"
    assert result["output"] == "plain assistant text"
    assert isinstance(result["output"], str)
    assert logs == [("WARN", "Failed to parse JSON, using raw output")]


def test_openclaw_payload_extracts_first_visible_text_payload():
    result, _ = _parse_stdout(
        json.dumps({"payloads": [{"text": "visible assistant text"}]})
    )

    assert result["status"] == "completed"
    assert result["output"] == "visible assistant text"
    assert isinstance(result["output"], str)


def test_payloads_support_visible_text_keys_beyond_text():
    for key in (
        "finalAssistantVisibleText",
        "final_assistant_visible_text",
        "output_text",
        "content_text",
    ):
        result, _ = _parse_stdout(json.dumps({"payloads": [{key: f"{key} value"}]}))

        assert result["status"] == "completed"
        assert result["output"] == f"{key} value"
        assert isinstance(result["output"], str)


def test_payloads_skip_non_dict_noise_and_extract_later_visible_text():
    result, _ = _parse_stdout(
        json.dumps(
            {
                "payloads": [
                    "progress log",
                    {"content": [{"type": "output_text", "text": "final text"}]},
                ]
            }
        )
    )

    assert result["status"] == "completed"
    assert result["output"] == "final text"
    assert isinstance(result["output"], str)


def test_malformed_json_stdout_is_controlled_raw_text_result():
    result, _ = _parse_stdout('{"payloads": [')

    assert result["status"] == "completed"
    assert result["output"] == '{"payloads": ['
    assert isinstance(result["output"], str)


def test_unsupported_dict_payload_is_controlled_string_output():
    result, _ = _parse_stdout(json.dumps({"metadata": {"only": 1}}))

    assert result["status"] == "completed"
    assert isinstance(result["output"], str)
    assert result["output"]
    assert not result["output"].startswith("Execution error:")


def test_service_parse_openclaw_response_delegates_to_safe_parser():
    service = object.__new__(OpenClawSessionService)
    service._log_entry = lambda *_, **__: None

    result = service._parse_openclaw_response(
        subprocess.CompletedProcess(
            args=["openclaw"],
            returncode=0,
            stdout=json.dumps({"finalAssistantVisibleText": "service text"}),
            stderr="",
        )
    )

    assert result["status"] == "completed"
    assert result["output"] == "service text"
    assert isinstance(result["output"], str)


def test_debug_repair_responses_helper_rejects_unsupported_shape_as_empty():
    assert OpenClawSessionService._extract_responses_output_text(["bad"]) == ""
    assert OpenClawSessionService._extract_responses_output_text({"output": {}}) == ""


def test_debug_repair_chat_helper_rejects_unsupported_content_shape_as_empty():
    body = {"choices": [{"message": {"content": {"text": "unsupported"}}}]}

    assert OpenClawSessionService._extract_chat_completion_content(body) == ""
