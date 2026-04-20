from __future__ import annotations

from app.services.orchestration.step_support import coerce_debug_step_result
from app.services.orchestration.parsing import extract_structured_text


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
