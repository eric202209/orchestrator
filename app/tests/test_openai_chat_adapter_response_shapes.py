from app.services.agents.providers.openai_chat_adapter import (
    _extract_chat_completion_content,
    _strip_thinking,
)


def _body(content):
    return {"choices": [{"message": {"content": content}}]}


def test_openai_chat_extracts_string_content_unchanged():
    assert _extract_chat_completion_content(_body("plain text")) == "plain text"


def test_openai_chat_dict_content_does_not_raise_raw_type_error():
    assert _extract_chat_completion_content(_body({"unexpected": "shape"})) == ""


def test_openai_chat_list_content_does_not_raise_raw_type_error():
    assert _extract_chat_completion_content(_body([{"unexpected": "shape"}])) == ""


def test_openai_chat_dict_text_field_extracts_text():
    assert _extract_chat_completion_content(_body({"text": "dict text"})) == "dict text"


def test_openai_chat_list_of_text_parts_extracts_text():
    assert (
        _extract_chat_completion_content(
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


def test_openai_chat_unsupported_shape_is_deterministic_empty_text():
    assert _extract_chat_completion_content(_body({"metadata": {"tokens": 3}})) == ""
    assert _extract_chat_completion_content(_body([{"metadata": {"tokens": 3}}])) == ""


def test_openai_chat_strip_thinking_runs_after_text_normalization():
    content = {"text": "<think>private reasoning</think>visible answer"}

    assert _strip_thinking(_extract_chat_completion_content(_body(content))) == (
        "visible answer"
    )


def test_openai_chat_strip_thinking_defensively_handles_non_string_values():
    assert _strip_thinking({"text": "<think>hidden</think>shown"}) == "shown"
    assert _strip_thinking([{"text": "<think>hidden</think>"}, {"text": "shown"}]) == (
        "shown"
    )
