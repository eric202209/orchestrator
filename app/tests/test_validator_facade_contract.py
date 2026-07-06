"""Phase 20J: ValidatorService facade contract freeze.

Prework for the validator rule split (docs/roadmap/refactoring-phases.md).
This test freezes the ValidatorService public method surface and the
verdict payload shape returned by the four `validate_*` entry points, so
that a future phase moving rule implementation into
`app/services/orchestration/validation/rules/` cannot silently change the
facade callers depend on.

No rule logic is exercised for correctness here — only shape and surface.
"""

from __future__ import annotations

import inspect

from app.services.orchestration.types import (
    PlanAccepted,
    PlanRejected,
    PlanRepairRequired,
    ValidationVerdict,
)
from app.services.orchestration.validation.validator import ValidatorService

# The exact set of public (non-underscore) callables on ValidatorService as
# of Phase 20J. Adding or removing a public method is a facade change and
# must update this set deliberately, not accidentally.
EXPECTED_PUBLIC_METHODS = {
    "assess_plan_workspace_compatibility",
    "build_failure_signature",
    "has_explicit_repair_intent",
    "infer_validation_profile",
    "persist_validation_result",
    "repair_requires_independent_evidence",
    "validate_baseline_publish",
    "validate_plan",
    "validate_plan_schema",
    "validate_reasoning_artifact",
    "validate_step_success",
    "validate_task_completion",
}

# The exact ValidationVerdict.to_dict() key set as of Phase 20J.
EXPECTED_VERDICT_DICT_KEYS = {
    "stage",
    "status",
    "profile",
    "reasons",
    "validator_rule_ids",
    "details",
    "used_small_model",
    "confidence",
}


def test_validator_service_public_method_surface_is_frozen():
    public_methods = {
        name
        for name, obj in inspect.getmembers(ValidatorService)
        if not name.startswith("_") and callable(obj)
    }
    assert public_methods == EXPECTED_PUBLIC_METHODS, (
        "ValidatorService public method surface changed. If this is an "
        "intentional facade change, update EXPECTED_PUBLIC_METHODS after "
        "confirming every caller was updated. "
        f"Added: {public_methods - EXPECTED_PUBLIC_METHODS}; "
        f"Removed: {EXPECTED_PUBLIC_METHODS - public_methods}"
    )


def _minimal_plan_with_write_op():
    return [
        {
            "step_number": 1,
            "description": "Implement source",
            "commands": [],
            "verification": "",
            "rollback": "",
            "expected_files": ["src/app.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "print('hello')\n",
                }
            ],
        }
    ]


def test_validate_plan_verdict_payload_shape_is_frozen(tmp_path):
    outcome = ValidatorService.validate_plan(
        _minimal_plan_with_write_op(),
        output_text="",
        task_prompt="Write a small Python implementation",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert isinstance(outcome, (PlanAccepted, PlanRepairRequired, PlanRejected))
    assert isinstance(outcome.verdict, ValidationVerdict)
    assert outcome.verdict.stage == "plan"

    payload = outcome.to_dict()
    assert set(payload.keys()) == EXPECTED_VERDICT_DICT_KEYS
    assert isinstance(payload["reasons"], list)
    assert isinstance(payload["validator_rule_ids"], list)
    assert isinstance(payload["details"], dict)

    # Delegated properties must round-trip to the underlying verdict.
    assert outcome.accepted == outcome.verdict.accepted
    assert outcome.status == outcome.verdict.status
    assert outcome.reasons == outcome.verdict.reasons
    assert outcome.details == outcome.verdict.details


def test_validate_step_success_verdict_payload_shape_is_frozen(tmp_path):
    verdict = ValidatorService.validate_step_success(
        project_dir=tmp_path,
        step={
            "step_number": 1,
            "description": "Write a file",
            "expected_files": [],
            "verification": "",
        },
        step_output="ok",
        missing_expected_files=[],
        tool_failures=[],
        validation_profile="implementation",
    )

    assert isinstance(verdict, ValidationVerdict)
    assert verdict.stage == "step_completion"
    payload = verdict.to_dict()
    assert set(payload.keys()) == EXPECTED_VERDICT_DICT_KEYS
    assert isinstance(payload["reasons"], list)
    assert isinstance(payload["details"], dict)


def test_validate_task_completion_verdict_payload_shape_is_frozen(tmp_path):
    verdict = ValidatorService.validate_task_completion(
        project_dir=tmp_path,
        plan=[{"step_number": 1, "description": "Do something"}],
        task_prompt="Do something",
        execution_profile="full_lifecycle",
    )

    assert isinstance(verdict, ValidationVerdict)
    assert verdict.stage == "task_completion"
    payload = verdict.to_dict()
    assert set(payload.keys()) == EXPECTED_VERDICT_DICT_KEYS
    assert isinstance(payload["reasons"], list)
    assert isinstance(payload["details"], dict)


def test_validate_baseline_publish_verdict_payload_shape_is_frozen(tmp_path):
    verdict = ValidatorService.validate_baseline_publish(
        validation_profile="implementation",
        baseline_path=str(tmp_path),
        baseline_file_count=1,
        missing_task_expected_files=[],
        missing_prior_expected_files=[],
    )

    assert isinstance(verdict, ValidationVerdict)
    assert verdict.stage == "baseline_publish"
    payload = verdict.to_dict()
    assert set(payload.keys()) == EXPECTED_VERDICT_DICT_KEYS
    assert isinstance(payload["reasons"], list)
    assert isinstance(payload["details"], dict)
