"""PHASE36-MAINT-GR10 — import-only verification evidence classification.

Phase 10L defines ``smoke_only`` as an import/startup/file-existence check and
``behavioral`` as exercising real behavior.  The classifier recognized an
import check only when the ``python -c`` program began with ``import``; the
REENTRY-5 steps 3/4 command ``python -c "from app.api.v1.router import
api_router; print(...)"`` fell through to the broad ``python \\S+`` rule and
was scored ``behavioral``.  After GR9 that was the only reason the exact
REENTRY-5 Candidate stayed ``verification_insufficient=false``.

GR10: a lone ``python -c`` whose program only imports and prints is
``smoke_only``.  Programs with any other statement keep the existing rules.
Verdicts come from the real classifier and Validator.  No provider is called.
"""

from __future__ import annotations

import pytest

from app.services.orchestration.validation.integrity import (
    classify_verification_command,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_phase36_maint_gr7_candidate_semantic_verification_authority import (
    SUBSTANTIVE_NEEDS_REVIEW,
    SUBSTANTIVE_PASS,
    _Runtime,
    _seed_auto_publish,
)
from app.tests.test_phase36_maint_gr8_verification_sufficiency_auto_promotion import (
    BEHAVIORAL as GR8_BEHAVIORAL_CONTROL,
    HOLD_REASON,
    _assert_held,
)

# Verbatim from REENTRY-5 task 284 steps 3/4 (db-records.json).
REENTRY5_IMPORT_CHECK = (
    'python -c "from app.api.v1.router import api_router; '
    "print('Router imported successfully')\""
)
REENTRY5_STEP1 = (
    "python -c \"import pathlib; p = pathlib.Path('app/api/v1/router.py'); "
    "assert p.exists(), 'router.py not found'; print('Found router.py')\""
)
REENTRY5_PYTEST = "python -m pytest app/tests/ -k permission -v --tb=short -q"
REPAIR_PROMPT = "Fix the broken status behavior."
REPAIR_TITLE = "Fix status regression"
MUTATION = [
    {"op": "replace_in_file", "path": "app.py", "old": "'ready'", "new": "'ready'"}
]


@pytest.mark.parametrize(
    ("label", "command"),
    [
        ("r1_import", 'python -c "import app.api.v1.router"'),
        ("r2_from_import", 'python -c "from app.api.v1.router import api_router"'),
        ("r3_reentry5", REENTRY5_IMPORT_CHECK),
        (
            "r3_print_object",
            'python -c "from app.api.v1.router import api_router; print(api_router)"',
        ),
        (
            "r4_observational_property",
            'python -c "from app.api.v1.router import api_router; '
            'print(len(api_router.routes))"',
        ),
        ("r4_print_call_result", 'python -c "from app import status; print(status())"'),
        ("python3", 'python3 -c "from app import status"'),
        ("single_quoted", "python -c 'from app import status; print(status)'"),
        ("env_prefix", 'PYTHONPATH=. python -c "from app import status"'),
        ("venv_python", 'venv/bin/python -c "from app import status"'),
    ],
)
def test_r1_r4_import_and_observation_only_is_smoke(label, command):
    assert classify_verification_command(command) == "smoke_only", label


@pytest.mark.parametrize(
    ("label", "command", "expected"),
    [
        ("r5_r6_gr8_control", GR8_BEHAVIORAL_CONTROL, "behavioral"),
        (
            "r5_exit_status_condition",
            'python -c "from app import status; import sys; '
            "sys.exit(0 if status() == 'ready' else 1)\"",
            "behavioral",
        ),
        (
            "r5_raise_condition",
            'python -c "from app import status\n'
            "if status() != 'ready': raise SystemExit(1)\"",
            "behavioral",
        ),
        ("exercises_code", 'python -c "from app.main import run; run()"', "behavioral"),
        ("r7_pytest", REENTRY5_PYTEST, "regression_test"),
        ("r7_unittest", "python -m unittest discover -s tests", "regression_test"),
        ("r8_py_compile", "python -m py_compile app.py", "smoke_only"),
        ("r8_leading_import", 'python -c "import app"', "smoke_only"),
        # Not upgraded by GR10: leading-import programs keep their 10L class.
        (
            "r8_leading_import_assert",
            "python -c \"import app; assert app.status() == 'ready'\"",
            "smoke_only",
        ),
        ("r8_compileall", "python -m compileall -q .", "behavioral"),
        ("r8_test_f", "test -f app.py", "smoke_only"),
        ("r9_curl", "curl -fsS http://localhost:8000/health", "insufficient"),
        ("r9_node", "node scripts/smoke.js", "behavioral"),
        ("r10_script", "python app.py --json", "behavioral"),
        ("r10_uv_run", "uv run app.py", "behavioral"),
        ("r10_build", "npm run build", "behavioral"),
        # Compound commands are outside the lone-``python -c`` rule.
        (
            "compound_unchanged",
            'cd app && python -c "from app import status"',
            "behavioral",
        ),
    ],
)
def test_r5_r10_other_classifications_are_preserved(label, command, expected):
    assert classify_verification_command(command) == expected, label


def _step(number, verification="", ops=None):
    step = {
        "step_number": number,
        "description": f"step {number}",
        "verification": verification,
        "expected_files": ["app.py"],
    }
    if ops:
        step["ops"] = ops
    return step


def _verdict(tmp_path, plan, *, prompt=REPAIR_PROMPT, title=REPAIR_TITLE):
    project_dir = tmp_path / "project"
    (project_dir / "tests").mkdir(parents=True)
    (project_dir / "app.py").write_text(
        "def status():\n    return 'ready'\n", encoding="utf-8"
    )
    (project_dir / "tests" / "test_app.py").write_text(
        "from app import status\n\n"
        "def test_status():\n"
        "    assert status() == 'ready'\n",
        encoding="utf-8",
    )
    return ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=plan,
        task_prompt=prompt,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title=title,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": len(plan),
            "reported_changed_files": ["app.py"],
            "change_set": {"modified_files": ["app.py"]},
        },
    )


REENTRY5_PLAN = [
    _step(1, REENTRY5_STEP1),
    _step(2, REENTRY5_PYTEST),
    _step(3, REENTRY5_IMPORT_CHECK, MUTATION),
    _step(4, REENTRY5_IMPORT_CHECK),
]


def _reentry5_verdict(tmp_path):
    verdict = _verdict(tmp_path, REENTRY5_PLAN)
    evidence = verdict.details["validation_evidence"]
    assert verdict.status == "warning" and verdict.accepted
    assert evidence["command_quality"] == "regression_test"
    assert evidence["verification_invalidated_by_later_mutation"] is True
    assert evidence["applicable_command_quality"] == "smoke_only"
    assert evidence["requires_independent_evidence"] is True
    assert evidence["has_independent_regression_test"] is False
    assert evidence["verification_insufficient"] is True
    return verdict


def test_r11_r12_exact_reentry5_replay_is_insufficient(tmp_path):
    verdict = _reentry5_verdict(tmp_path)
    by_step = verdict.details["validation_evidence"]["command_quality_by_step"]

    # GR9 applicability is unchanged; only the post-mutation class moved.
    assert [(e["command_quality"], e["applies_to_candidate"]) for e in by_step] == [
        ("smoke_only", False),
        ("regression_test", False),
        ("smoke_only", True),
        ("smoke_only", True),
    ]
    assert any("smoke-only" in reason for reason in verdict.reasons)


def test_r13_valid_standalone_pass_cannot_override_insufficiency(
    db_session, tmp_path, monkeypatch
):
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _reentry5_verdict(tmp_path)
    )

    assert runtime.evaluator_calls == 1
    payload = _assert_held(db_session, result, ctx, project_root, HOLD_REASON)
    decision = payload["review_decision"]
    assert decision["outcome"] == "hold_for_review"
    assert decision["evaluator_verdict"] == "PASS"
    assert decision["verification_insufficient"] is True


@pytest.mark.parametrize(
    ("label", "runtime", "reason"),
    [
        (
            "needs_review",
            _Runtime({"output": SUBSTANTIVE_NEEDS_REVIEW}),
            "evaluator_needs_review",
        ),
        (
            "reentry5_inline_pass_unknown",
            _Runtime(
                {
                    "output": "SCORES: goal=3/3 TOTAL: 10/10 VERDICT: PASS "
                    "NOTES: all tests passing."
                }
            ),
            "evaluator_assessment_unavailable",
        ),
        (
            "error",
            _Runtime(raises=TimeoutError("evaluator timed out")),
            "evaluator_assessment_unavailable",
        ),
    ],
)
def test_r14_gr7_non_pass_keeps_evaluator_reason(
    db_session, tmp_path, monkeypatch, label, runtime, reason
):
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _reentry5_verdict(tmp_path)
    )

    payload = _assert_held(db_session, result, ctx, project_root, reason)
    assert payload["review_decision"]["verification_insufficient"] is True, label


def test_r15_post_mutation_behavioral_assertion_is_sufficient(tmp_path):
    verdict = _verdict(
        tmp_path,
        [_step(1, REENTRY5_PYTEST), _step(2, GR8_BEHAVIORAL_CONTROL, MUTATION)],
    )
    evidence = verdict.details["validation_evidence"]

    assert verdict.status == "accepted"
    assert evidence["applicable_command_quality"] == "behavioral"
    assert evidence["verification_insufficient"] is False


def test_r16_post_mutation_regression_test_is_independent_evidence(tmp_path):
    verdict = _verdict(
        tmp_path,
        [_step(1, REENTRY5_IMPORT_CHECK, MUTATION), _step(2, REENTRY5_PYTEST)],
    )
    evidence = verdict.details["validation_evidence"]

    assert verdict.status == "accepted"
    assert evidence["applicable_command_quality"] == "regression_test"
    assert evidence["has_independent_regression_test"] is True
    assert evidence["verification_insufficient"] is False


def test_r17_non_repair_task_is_still_accepted(tmp_path):
    verdict = _verdict(
        tmp_path,
        [_step(1, REENTRY5_IMPORT_CHECK, MUTATION)],
        prompt="Add a status helper.",
        title="Add status helper",
    )
    evidence = verdict.details["validation_evidence"]

    assert verdict.status == "accepted"
    assert evidence["command_quality"] == "smoke_only"
    assert evidence["requires_independent_evidence"] is False
    assert evidence["verification_insufficient"] is False
