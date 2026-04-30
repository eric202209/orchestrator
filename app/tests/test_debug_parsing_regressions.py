from __future__ import annotations

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
    assert debug_data["fix_type"] == "command_fix"
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
    assert strategy in {"", "Found JSON in text", "Extracted from mixed content"}


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


def test_extract_plan_steps_from_summary_text_recovers_markdown_table_plan():
    text = """
Plan written -> `vault/projects/garden-story-microsite/plan.json`

**5-step plan:**

| # | Step | Files |
|---|------|-------|
| 1 | Create `css/` + `images/` dirs | — |
| 2 | Generate `images/flower-bg.svg` (decorative botanical SVG) | `images/flower-bg.svg` |
| 3 | Write `css/style.css` (SVG bg, centered overlay, CTA styles) | `css/style.css` |
| 4 | Write `index.html` (title + intro + CTA section) | `index.html` |
| 5 | Verify all files exist, non-empty, cross-references intact | — |
"""

    plan = extract_plan_steps_from_summary_text(text)

    assert plan is not None
    assert len(plan) == 5
    assert plan[0]["commands"] == ["mkdir -p css/ images/"]
    assert plan[1]["commands"][0].startswith("write images/flower-bg.svg:")
    assert plan[3]["expected_files"] == ["index.html"]
