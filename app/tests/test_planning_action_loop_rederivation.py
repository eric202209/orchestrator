"""PLANNING_ACTION_LOOP_VS_EXECUTABLE_PLAN_REDERIVATION — bounded repairs.

Covers the three provider-free repairs made by this gate:
1. the production minimal Planning prompt no longer contradicts itself;
2. the model-profile suffix no longer collides with the base rule numbering;
3. a weak verification on a step that already declares `write_file` content is
   derived deterministically instead of being sent back to the provider.
"""

from __future__ import annotations

import collections
import re
from pathlib import Path

from app.services.orchestration.phases.planning_verification import (
    _strengthen_weak_expected_file_verifications,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.planning_prompts import (
    build_minimal_planning_prompt,
    build_ultra_minimal_planning_prompt,
)
from app.services.orchestration.validation.validator import ValidatorService


def _minimal_prompt(tmp_path: Path, profile: str = "local_qwen_json_array") -> str:
    return build_minimal_planning_prompt(
        "Prevent empty project names at the API boundary.",
        tmp_path,
        prompt_profile=profile,
        apply_prompt_profile=PlannerService.apply_prompt_profile,
    )


def _duplicate_rule_numbers(prompt: str) -> list[str]:
    numbers = re.findall(r"^\s*(\d+[a-z]?)\.", prompt, re.M)
    return sorted(
        number for number, count in collections.Counter(numbers).items() if count > 1
    )


def test_minimal_prompt_no_longer_forbids_implementation(tmp_path):
    prompt = _minimal_prompt(tmp_path)
    assert "Do not implement anything." not in prompt
    # The steps the same prompt still requires are implementation steps.
    assert "file-mutating `ops` entry" in prompt


def test_ultra_minimal_prompt_rule_numbers_are_unique(tmp_path):
    prompt = build_ultra_minimal_planning_prompt(
        "Prevent empty project names at the API boundary.",
        tmp_path,
        prompt_profile="local_qwen_small_json_array",
        apply_prompt_profile=PlannerService.apply_prompt_profile,
    )
    assert _duplicate_rule_numbers(prompt) == []


def test_minimal_prompt_rule_numbers_are_unique(tmp_path):
    assert _duplicate_rule_numbers(_minimal_prompt(tmp_path)) == []


def test_profile_suffix_is_unnumbered():
    prompt = PlannerService.apply_prompt_profile(
        "1. base rule\n", prompt_profile="local_qwen_small_json_array"
    )
    assert "Output discipline for this model:" in prompt
    assert "- Return only a JSON array of steps." in prompt
    assert _duplicate_rule_numbers(prompt) == []


def test_default_profile_appends_nothing():
    assert PlannerService.apply_prompt_profile("1. base rule\n") == "1. base rule\n"


def test_weak_verification_derived_from_write_file_content():
    step = {
        "step_number": 1,
        "description": "Reject empty project names",
        "commands": [],
        "verification": "cat app/validators.py",
        "rollback": None,
        "expected_files": ["app/validators.py"],
        "ops": [
            {
                "op": "write_file",
                "path": "app/validators.py",
                "content": "def reject_empty_project_name(name):\n    return bool(name)\n",
            }
        ],
    }
    assert ValidatorService._verification_is_weak(step["verification"])

    (strengthened,) = _strengthen_weak_expected_file_verifications([step])

    assert not ValidatorService._verification_is_weak(strengthened["verification"])
    assert "def reject_empty_project_name(name):" in strengthened["verification"]
    assert "app/validators.py" in strengthened["verification"]


def test_strong_verification_is_left_alone():
    step = {
        "step_number": 1,
        "description": "Reject empty project names",
        "commands": [],
        "verification": "python -m pytest tests/ -q",
        "rollback": None,
        "expected_files": ["app/validators.py"],
        "ops": [
            {
                "op": "write_file",
                "path": "app/validators.py",
                "content": "def reject_empty_project_name(name):\n    return bool(name)\n",
            }
        ],
    }

    (strengthened,) = _strengthen_weak_expected_file_verifications([step])

    assert strengthened["verification"] == "python -m pytest tests/ -q"


def test_no_derivation_without_a_declared_write_file_op():
    step = {
        "step_number": 1,
        "description": "Reject empty project names",
        "commands": ["cat app/validators.py"],
        "verification": "cat app/validators.py",
        "rollback": None,
        "expected_files": ["app/validators.py"],
    }

    (strengthened,) = _strengthen_weak_expected_file_verifications([step])

    assert strengthened["verification"] == "cat app/validators.py"


def test_no_derivation_when_op_path_is_not_expected():
    step = {
        "step_number": 1,
        "description": "Reject empty project names",
        "commands": [],
        "verification": "cat app/validators.py",
        "rollback": None,
        "expected_files": ["app/validators.py"],
        "ops": [
            {
                "op": "write_file",
                "path": "app/other.py",
                "content": "def reject_empty_project_name(name):\n    return bool(name)\n",
            }
        ],
    }

    (strengthened,) = _strengthen_weak_expected_file_verifications([step])

    assert strengthened["verification"] == "cat app/validators.py"
