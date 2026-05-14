from __future__ import annotations

import json

from app.services.prompt_templates import PromptTemplates, StepResult
from app.services.orchestration.execution.step_support import coerce_debug_step_result
from app.services.orchestration.validation.parsing import (
    extract_plan_steps_from_summary_text,
    extract_plan_steps,
    extract_structured_text,
)
from app.services.agents.openclaw_service import OpenClawSessionService


def test_debug_parser_recovers_prose_response_and_trims_bad_expected_files():
    raw_result = {
        "output": (
            "**Analysis:** The step failed because it expected a `README.md` file "
            "that doesn't exist in the workspace. This project is a minimal "
            "TypeScript/Vitest setup, so that file is not required.\n\n"
            "**Recommended Fix:** Retry the inspection step without expecting "
            "`README.md`.\n\n"
            "**Confidence:** High"
        )
    }
    step = {
        "commands": ["rg --files . | head -50", "ls -la"],
        "expected_files": ["package.json", "src", "README.md"],
        "verification": "Confirm project root structure",
    }

    success, debug_data, strategy = coerce_debug_step_result(
        raw_result,
        error_message="Step reported success but expected files are missing: README.md",
        step=step,
        extract_structured_text=extract_structured_text,
    )

    assert success is True
    assert strategy == "Inferred structured debug payload from prose"
    assert debug_data["fix_type"] == "code_fix"
    assert "README.md" in debug_data["analysis"]
    assert debug_data["expected_files"] == ["package.json", "src"]
    assert debug_data["confidence"] == "HIGH"


def test_debug_parser_still_accepts_json_payloads():
    raw_result = {
        "output": (
            '{"fix_type":"command_fix","analysis":"Use a workspace listing first",'
            '"fix":"rg --files . | head -50","confidence":"MEDIUM"}'
        )
    }

    success, debug_data, strategy = coerce_debug_step_result(
        raw_result,
        error_message="read failed",
        step={"commands": ["read guessed-file.ts"]},
        extract_structured_text=extract_structured_text,
    )

    assert success is True
    assert debug_data["fix_type"] == "command_fix"
    assert debug_data["fix"] == "rg --files . | head -50"
    assert strategy in {
        "",
        "Found JSON in text",
        "Extracted from mixed content",
        "Parsed full debug JSON",
    }


def test_debug_parser_demotes_json_command_fix_with_non_runnable_fix():
    raw_result = {
        "output": (
            '{"fix_type":"command_fix","analysis":"Need code edit",'
            '"fix":"Update the test to use pytest","confidence":"MEDIUM"}'
        )
    }

    success, debug_data, _ = coerce_debug_step_result(
        raw_result,
        error_message="pytest failed",
        step={"commands": ["pytest"]},
        extract_structured_text=extract_structured_text,
    )

    assert success is True
    assert debug_data["fix_type"] == "code_fix"


def test_debug_parser_promotes_json_code_fix_with_runnable_fix():
    raw_result = {
        "output": (
            '{"fix_type":"code_fix","analysis":"Use a direct package update",'
            '"fix":"cd /tmp/project && node -e \\"console.log(1)\\"",'
            '"confidence":"HIGH"}'
        )
    }

    success, debug_data, _ = coerce_debug_step_result(
        raw_result,
        error_message="replace_in_file old text not found",
        step={"ops": [{"op": "replace_in_file", "path": "package.json"}]},
        extract_structured_text=extract_structured_text,
    )

    assert success is True
    assert debug_data["fix_type"] == "command_fix"
    assert debug_data["fix"] == 'cd /tmp/project && node -e "console.log(1)"'


def test_debug_parser_accepts_typed_structured_op_repair():
    raw_result = {
        "output": (
            '{"fix_type":"replace_op","analysis":"Use a complete file rewrite",'
            '"replacement_ops":[{"op":"write_file","path":"package.json",'
            '"content":"{\\"version\\":\\"1.1.0\\"}\\n"}],'
            '"confidence":"HIGH"}'
        )
    }

    success, debug_data, _ = coerce_debug_step_result(
        raw_result,
        error_message="replace_in_file old text not found in package.json",
        step={"ops": [{"op": "replace_in_file", "path": "package.json"}]},
        extract_structured_text=extract_structured_text,
    )

    assert success is True
    assert debug_data["fix_type"] == "ops_fix"
    assert debug_data["ops"] == [
        {
            "op": "write_file",
            "path": "package.json",
            "content": '{"version":"1.1.0"}\n',
        }
    ]


def test_debug_parser_accepts_fenced_typed_structured_op_repair():
    raw_result = {
        "output": (
            "The replace op is stale.\n\n```json\n"
            "{\n"
            '  "fix_type": "replace_op",\n'
            '  "analysis": "Rewrite the small JSON file.",\n'
            '  "replacement_ops": [\n'
            '    {"op": "write_file", "path": "package.json", "content": "{\\n  \\"version\\": \\"1.1.0\\"\\n}\\n"}\n'
            "  ],\n"
            '  "confidence": "HIGH"\n'
            "}\n```"
        )
    }

    success, debug_data, strategy = coerce_debug_step_result(
        raw_result,
        error_message="replace_in_file old text not found in package.json",
        step={"ops": [{"op": "replace_in_file", "path": "package.json"}]},
        extract_structured_text=extract_structured_text,
    )

    assert success is True
    assert strategy in {"Parsed fenced debug JSON", "Parsed full debug JSON"}
    assert debug_data["fix_type"] == "ops_fix"
    assert debug_data["ops"][0]["op"] == "write_file"


def test_debug_parser_accepts_wrapped_fenced_typed_structured_op_repair():
    raw_result = {
        "output": json.dumps(
            {
                "projectContextChars": 15365,
                "nonProjectContextChars": 33281,
                "finalAssistantVisibleText": (
                    "```json\n"
                    "{\n"
                    '  "fix_type": "replace_op",\n'
                    '  "analysis": "Rewrite README.md because the exact replacement is stale.",\n'
                    '  "replacement_ops": [\n'
                    '    {"op": "write_file", "path": "README.md", "content": "# Demo\\n\\n## Changelog\\n- 1.1.0\\n"}\n'
                    "  ],\n"
                    '  "confidence": "HIGH"\n'
                    "}\n```"
                ),
            }
        )
    }

    success, debug_data, strategy = coerce_debug_step_result(
        raw_result,
        error_message="replace_in_file old text not found in README.md",
        step={"ops": [{"op": "replace_in_file", "path": "README.md"}]},
        extract_structured_text=extract_structured_text,
    )

    assert success is True
    assert strategy == "Parsed wrapped assistant debug JSON"
    assert debug_data["fix_type"] == "ops_fix"
    assert debug_data["ops"] == [
        {
            "op": "write_file",
            "path": "README.md",
            "content": "# Demo\n\n## Changelog\n- 1.1.0\n",
        }
    ]


def test_plan_revision_prompt_serializes_original_plan():
    prompt = PromptTemplates.build_plan_revision_prompt(
        original_plan=[
            {
                "step_number": 1,
                "description": "Add test coverage for formatter",
                "commands": ["npm test -- format"],
            }
        ],
        failed_steps=[
            StepResult(
                step_number=2,
                status="failed",
                error_message="Expected src/utils/format.test.ts to exist",
            )
        ],
        debug_analysis="The execution reported success but did not create the expected file.",
        completed_steps=[
            {"step_number": 1, "description": "Inspect formatter helpers"}
        ],
        workspace_root="/tmp/workspace",
        project_dir="/tmp/workspace/demo-project",
    )

    assert "Add test coverage for formatter" in prompt
    assert "Expected src/utils/format.test.ts to exist" in prompt


def test_extract_structured_text_prefers_final_assistant_visible_text():
    payload = {
        "meta": {"durationMs": 1234},
        "finalAssistantVisibleText": '```json\n[{"step_number":1,"description":"x","commands":[],"verification":null,"rollback":null,"expected_files":[]}]\n```',
    }

    text = extract_structured_text(payload)

    assert "step_number" in text


def test_openclaw_response_parser_recovers_final_assistant_visible_text():
    service = OpenClawSessionService.__new__(OpenClawSessionService)
    stdout = '{"stopReason":"stop","finalAssistantVisibleText":"```json\\n[{\\"step_number\\":1,\\"description\\":\\"x\\",\\"commands\\":[],\\"verification\\":null,\\"rollback\\":null,\\"expected_files\\":[]}]\\n```"}'
    completed = __import__("subprocess").CompletedProcess(
        args=["openclaw", "agent"],
        returncode=0,
        stdout=stdout,
        stderr="",
    )

    result = OpenClawSessionService._parse_openclaw_response(service, completed)

    assert result["status"] == "completed"
    assert "step_number" in result["output"]


def test_openclaw_response_parser_surfaces_aborted_payload_as_failure():
    service = OpenClawSessionService.__new__(OpenClawSessionService)
    service.session_model = None
    service._log_entry = lambda *args, **kwargs: None
    payload = '{"total":0,"aborted":true,"source":"run","generatedAt":1777555426260}'
    completed = __import__("subprocess").CompletedProcess(
        args=["openclaw", "agent"],
        returncode=0,
        stdout=payload,
        stderr="",
    )

    result = OpenClawSessionService._parse_openclaw_response(service, completed)

    assert result["status"] == "failed"
    assert result["error"]


def test_extract_plan_steps_can_unwrap_final_assistant_visible_text_string():
    payload = {
        "finalAssistantVisibleText": '```json\n[{"step_number":1,"description":"x","commands":[],"verification":null,"rollback":null,"expected_files":[]}]\n```'
    }

    plan = extract_plan_steps(payload)

    assert plan is not None
    assert len(plan) == 1
    assert plan[0]["step_number"] == 1


def test_extract_plan_steps_can_unwrap_stringified_wrapper_payload():
    wrapped = (
        '{"finalAssistantVisibleText":"```json\\n['
        '{\\"step_number\\":1,\\"description\\":\\"x\\",\\"commands\\":[],'
        '\\"verification\\":null,\\"rollback\\":null,\\"expected_files\\":[]}'
        ']\\n```"}'
    )

    plan = extract_plan_steps(wrapped)

    assert plan is not None
    assert len(plan) == 1
    assert plan[0]["description"] == "x"


def test_extract_structured_text_recovers_visible_text_from_partial_fragment():
    fragment = (
        '"total": 0\n'
        '"systemPrompt": {\n'
        '"finalAssistantVisibleText": "[\\n  {\\n'
        '    \\"step_number\\": 1,\\n'
        '    \\"description\\": \\"Inspect planning parser\\",\\n'
        '    \\"commands\\": [\\"rg parsing app/services/orchestration/validation\\"],\\n'
        '    \\"verification\\": \\"python3 -m pytest app/tests/test_debug_parsing_regressions.py -q\\",\\n'
        '    \\"rollback\\": null,\\n'
        '    \\"expected_files\\": []\\n'
        '  }\\n]"\n'
    )

    text = extract_structured_text(fragment)
    plan = extract_plan_steps(text)
    direct_plan = extract_plan_steps(fragment)

    assert text.startswith("[\n")
    assert plan is not None
    assert plan[0]["description"] == "Inspect planning parser"
    assert direct_plan is not None
    assert direct_plan[0]["description"] == "Inspect planning parser"


def test_extract_plan_steps_from_summary_text_recovers_markdown_table_plan():
    text = """
Plan written -> `vault/projects/static-page/plan.json`

**5-step plan:**

| # | Step | Files |
|---|------|-------|
| 1 | Create `css/` + `images/` dirs | — |
| 2 | Generate `images/page-art.svg` (decorative SVG) | `images/page-art.svg` |
| 3 | Write `css/style.css` (asset bg, centered overlay, CTA styles) | `css/style.css` |
| 4 | Write `index.html` (title + intro + CTA section) | `index.html` |
| 5 | Verify all files exist, non-empty, cross-references intact | — |
"""

    plan = extract_plan_steps_from_summary_text(text)

    assert plan is not None
    assert len(plan) == 5
    assert plan[0]["commands"] == ["mkdir -p css/ images/"]
    assert plan[1]["commands"][0].startswith("write images/page-art.svg:")
    assert plan[3]["expected_files"] == ["index.html"]
