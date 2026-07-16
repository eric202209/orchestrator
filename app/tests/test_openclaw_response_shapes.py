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


def test_planning_stderr_recovery_rejects_payload_owned_by_another_session():
    logs: list[tuple[str, str]] = []
    result = parse_openclaw_response(
        subprocess.CompletedProcess(
            args=["openclaw"],
            returncode=0,
            stdout="",
            stderr=json.dumps(
                {
                    "payloads": [{"text": "stale planning artifacts"}],
                    "meta": {"agentMeta": {"sessionId": "old-session"}},
                }
            ),
        ),
        lambda level, message, **_: logs.append((level, message)),
        expected_session_id="current-session",
    )

    assert result["status"] == "failed"
    assert result["output"] == ""
    assert result["response_session_id"] == "old-session"
    assert result["expected_session_id"] == "current-session"
    assert "provenance" in result["error"].lower()


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


# ---------------------------------------------------------------------------
# Additional coverage: plan-specified boundary cases
# ---------------------------------------------------------------------------


def test_top_level_json_array_produces_string_output():
    """Top-level JSON list is handled without TypeError."""
    result, _ = _parse_stdout(json.dumps([{"a": 1}, {"b": 2}]))

    assert isinstance(result["output"], str)
    assert isinstance(result["status"], str)


def test_aborted_payload_returns_failed():
    result, _ = _parse_stdout(
        json.dumps({"aborted": True, "finalAssistantVisibleText": "partial"})
    )

    assert result["status"] == "failed"


def test_planning_timeout_metadata_cannot_return_previous_response_as_success():
    logs: list[tuple[str, str]] = []
    result = parse_openclaw_response(
        subprocess.CompletedProcess(
            args=["openclaw"],
            returncode=0,
            stdout=json.dumps(
                {
                    "payloads": [{"text": "previous response"}],
                    "meta": {
                        "aborted": True,
                        "agentMeta": {"sessionId": "current-session"},
                    },
                }
            ),
            stderr="",
        ),
        lambda level, message, **_: logs.append((level, message)),
        expected_session_id="current-session",
    )

    assert result["status"] == "failed"
    assert result["output"] == "previous response"
    assert "aborted" in result["error"].lower()


def test_aborted_payload_with_timeout_text_sets_timeout_error():
    result, _ = _parse_stdout(json.dumps({"aborted": True, "text": "timeout occurred"}))

    assert result["status"] == "failed"
    assert "timed out" in result["error"].lower()


def test_empty_stdout_returns_failed():
    result, _ = _parse_stdout("")

    assert result["status"] == "failed"
    assert result["output"] == ""


def test_context_window_exceeded_in_stderr_returns_failed():
    result, _ = _parse_stdout("", stderr="context size has been exceeded")

    assert result["status"] == "failed"
    assert "context window exceeded" in result["error"].lower()


def test_nonzero_returncode_plain_text_returns_failed():
    result, _ = _parse_stdout("some partial output", returncode=1)

    assert result["status"] == "failed"


def test_output_field_always_string_across_all_shapes():
    """Regression: output must be str regardless of payload shape."""
    cases = [
        json.dumps({"finalAssistantVisibleText": "text"}),
        json.dumps({"payloads": [{"text": "hi"}]}),
        json.dumps({"unknown": "data"}),
        "plain text",
        json.dumps([1, 2, 3]),
        "{bad json",
        "",
    ]
    for case in cases:
        result, _ = _parse_stdout(case)
        assert isinstance(
            result["output"], str
        ), f"output must be str for input: {case!r}"


def test_debug_repair_responses_nested_content_extracts_text():
    body = {
        "output": [
            {
                "content": [
                    {"type": "output_text", "text": "nested extracted text"},
                ]
            }
        ]
    }
    assert (
        OpenClawSessionService._extract_responses_output_text(body)
        == "nested extracted text"
    )


def test_debug_repair_responses_output_text_field_takes_priority():
    body = {
        "output_text": "direct field",
        "output": [{"content": [{"text": "nested"}]}],
    }
    assert OpenClawSessionService._extract_responses_output_text(body) == "direct field"


def test_debug_repair_responses_empty_output_list_returns_empty():
    assert OpenClawSessionService._extract_responses_output_text({"output": []}) == ""


def test_debug_repair_chat_string_content():
    body = {"choices": [{"message": {"content": "assistant reply"}}]}
    assert (
        OpenClawSessionService._extract_chat_completion_content(body)
        == "assistant reply"
    )


def test_debug_repair_chat_list_content_concatenates():
    body = {
        "choices": [
            {"message": {"content": [{"text": "part one"}, {"text": " part two"}]}}
        ]
    }
    assert (
        OpenClawSessionService._extract_chat_completion_content(body)
        == "part one part two"
    )


def test_debug_repair_chat_list_content_skips_non_string_text():
    body = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"text": "valid"},
                        {"text": 42},
                        {"other": "no text key"},
                    ]
                }
            }
        ]
    }
    assert OpenClawSessionService._extract_chat_completion_content(body) == "valid"


def test_debug_repair_chat_empty_choices_returns_empty():
    assert (
        OpenClawSessionService._extract_chat_completion_content({"choices": []}) == ""
    )


def test_debug_repair_chat_non_dict_body_returns_empty():
    assert OpenClawSessionService._extract_chat_completion_content("text") == ""
    assert OpenClawSessionService._extract_chat_completion_content(None) == ""
