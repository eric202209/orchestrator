from __future__ import annotations

import json

from app.services.model_adaptation.renderers import render_qwen_compact_json_prompt
from app.services.model_adaptation.schemas import PromptEnvelope
from app.services.orchestration.context.assembly import render_adapted_runtime_prompt
from app.services.orchestration.validation.parsing import (
    _find_json_substring,
    extract_structured_text,
)


def test_find_json_substring_skips_prose_brace_before_array():
    text = 'The test.py is {failing}. Fix: [{"command": "pytest -q"}]'

    assert _find_json_substring(text) == '[{"command": "pytest -q"}]'


def test_find_json_substring_skips_prose_brace_before_object():
    text = 'The module is {broken}. Fix: {"step_number": 2, "commands": ["true"]}'

    assert _find_json_substring(text) == '{"step_number": 2, "commands": ["true"]}'


def test_find_json_substring_returns_none_without_valid_json():
    assert _find_json_substring("The test.py is {still failing") is None


def test_find_json_substring_skips_nested_content_dict_when_source_starts_array():
    text = """[
      {
        "step_number": 1,
        "description": "Create script",
        "commands": [],
        "verification": "python -m py_compile csv_summary.py",
        "rollback": null,
        "expected_files": ["csv_summary.py"],
        "ops": [
          {
            "op": "write_file",
            "path": "csv_summary.py",
            "content": "ERR = {"error": "File not found"}\\n"
          }
        ]
      }
    ]"""

    assert _find_json_substring(text) is None


def test_find_json_substring_skips_nested_content_dict_inside_fenced_array():
    text = """```json
[
  {
    "step_number": 1,
    "description": "Create script",
    "commands": [],
    "verification": "python -m py_compile csv_summary.py",
    "rollback": null,
    "expected_files": ["csv_summary.py"],
    "ops": [
      {
        "op": "write_file",
        "path": "csv_summary.py",
        "content": "ERR = {"error": "File not found"}\\n"
      }
    ]
  }
]
```"""

    assert _find_json_substring(text) is None


def test_extract_structured_text_preserves_raw_plan_array_with_content_json():
    text = """```json
[
  {
    "step_number": 1,
    "description": "Create script",
    "commands": [],
    "verification": "python -m py_compile csv_summary.py",
    "rollback": null,
    "expected_files": ["csv_summary.py"],
    "ops": [
      {
        "op": "write_file",
        "path": "csv_summary.py",
        "content": "ERR = {"error": "File not found"}\\n"
      }
    ]
  }
]
```"""

    extracted = extract_structured_text(text)

    assert extracted.lstrip().startswith("[")
    assert '"step_number": 1' in extracted
    assert '"content": "ERR = {"error": "File not found"}\\n"' in extracted


def test_render_adapted_runtime_prompt_direct_returns_body_unchanged():
    prompt_body = "Return bare JSON only.\n[]"

    rendered = render_adapted_runtime_prompt(
        None,
        objective="Should be skipped",
        execution_mode="debug",
        prompt_body=prompt_body,
        instructions=["Should be skipped"],
        context={"Project": "skipped"},
        expected_output="Skipped",
        direct=True,
    )

    assert rendered == prompt_body


def test_qwen_compact_json_prompt_front_loads_response_start_key():
    rendered = render_qwen_compact_json_prompt(
        PromptEnvelope(
            objective="Generate repair",
            execution_mode="repair",
            instructions=["Return JSON"],
            context={},
            expected_output="JSON array of repair steps.",
            prompt_body="[]",
        )
    )

    payload = json.loads(rendered)
    assert list(payload.keys())[0] == "response_start"
    assert payload["response_start"] == "["
