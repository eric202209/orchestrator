"""PHASE36-MAINT-GR7 — candidate semantic verification authority.

GR7 adjudicated what evidence may establish that an applied candidate is
semantically acceptable before auto-publication.  Deterministic
candidate-check failures were already authoritative: they make completion
validation non-accepting and the task fails before the evaluator runs.  The
reproduced defect was in the evaluator boundary.  ``_run_evaluator`` defaulted
its verdict to PASS, so empty or non-assessment output (REENTRY-3's retained
"Let me inspect the actual changes..." text) became ``QA verdict: PASS``.  The
coordinator also held only on NEEDS_REVIEW, so ERROR auto-published as well.

After the fix, PASS must be stated explicitly.  Other output is UNKNOWN, and
any non-PASS verdict holds the workspace for review.  These cases also pin
current deterministic check coverage and its limits (no baseline
differential, name-matched test selection only).  No provider is called.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import LogEntry
from app.services.orchestration.execution.runtime import workspace_snapshot_key
from app.services.orchestration.phases import completion_flow
from app.services.orchestration.phases.completion_flow import _run_evaluator
from app.services.orchestration.types import CandidateFinding, ValidationVerdict
from app.services.orchestration.validation import candidate_checks
from app.services.orchestration.validation.candidate_checks import (
    validate_candidate_delta,
)
from app.tests.test_completion_verification_regressions import (
    _complete_task,
    _seed_legacy_finalize_ctx,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REENTRY3_EVALUATOR_OUTPUT = (
    "Let me inspect the actual changes to assess whether the goal was met.\n"
    "Let me check if there's any git history or backup to understand what was "
    "actually changed."
)
SUBSTANTIVE_PASS = (
    "SCORES: goal=3/3 regressions=2/2 quality=2/2 files=3/3\n"
    "TOTAL: 10/10\n"
    "VERDICT: PASS\n"
    "NOTES: complete"
)
SUBSTANTIVE_NEEDS_REVIEW = (
    "SCORES: goal=1/3 regressions=0/2 quality=1/2 files=1/3\n"
    "TOTAL: 3/10\n"
    "VERDICT: NEEDS_REVIEW\n"
    "NOTES: regressions"
)
ROUTER_BASE = (
    "from fastapi import APIRouter\n"
    "from . import permissions\n"
    "\n"
    "api_router = APIRouter()\n"
    'api_router.include_router(permissions.router, prefix="/permissions")\n'
)
# REENTRY-4 shape: the ``APIRouter()`` region replaced by an include call.
ROUTER_REENTRY4 = ROUTER_BASE.replace(
    "api_router = APIRouter()",
    "api_router = api_router.include_router(\n    permissions.router,\n)",
)
ROUTER_FIXED = ROUTER_BASE.replace(', prefix="/permissions"', "")
PREFIX_TEST = (
    "from pathlib import Path\n\n\n"
    "def test_permissions_prefix_registered():\n"
    "    assert 'prefix=\"/permissions\"' in Path('app/router.py').read_text()\n"
)
PLAN_ASSERTION = (
    'python -c "import pathlib; '
    "assert 'prefix' not in pathlib.Path('app/router.py').read_text()\""
)


class _Runtime:
    def __init__(self, evaluator_output=None, *, raises=None):
        self.evaluator_output = evaluator_output
        self.raises = raises
        self.evaluator_calls = 0

    async def execute_task(self, prompt, timeout_seconds=None):
        if "independent QA evaluator" in prompt:
            self.evaluator_calls += 1
            if self.raises is not None:
                raise self.raises
            return self.evaluator_output
        return {"output": "Task summary"}

    def get_backend_metadata(self):
        return {"backend": "fake", "model_family": "test"}


def _evaluate(runtime):
    state = SimpleNamespace(
        execution_results=[],
        changed_files=["app/api/v1/router.py"],
        reasoning_artifact={},
        project_dir="/tmp/project",
        session_id=1,
        task_id=1,
    )
    return _run_evaluator(
        runtime_service=runtime,
        orchestration_state=state,
        prompt="Repair the permissions route",
        summary="done",
        emit_live=lambda *_args, **_kwargs: None,
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    )


@pytest.fixture
def _no_event_journal(monkeypatch):
    monkeypatch.setattr(
        completion_flow,
        "append_orchestration_event",
        lambda **_kwargs: {"event_id": "evt"},
    )


@pytest.mark.parametrize(
    ("label", "output", "expected"),
    [
        ("empty_output", {"output": ""}, "UNKNOWN"),
        ("reentry3_non_assessment", {"output": REENTRY3_EVALUATOR_OUTPUT}, "UNKNOWN"),
        (
            "timeout_shaped_result",
            {"status": "timeout", "output": "", "error": "timed out"},
            "UNKNOWN",
        ),
        ("template_echo", {"output": "VERDICT: PASS or NEEDS_REVIEW"}, "UNKNOWN"),
        ("pass_mentioned_in_prose", {"output": "I would PASS this."}, "UNKNOWN"),
        ("substantive_pass", {"output": SUBSTANTIVE_PASS}, "PASS"),
        ("markdown_pass", {"output": "**VERDICT:** PASS\n"}, "PASS"),
        (
            "substantive_needs_review",
            {"output": SUBSTANTIVE_NEEDS_REVIEW},
            "NEEDS_REVIEW",
        ),
        (
            "contradictory_verdicts",
            {"output": "VERDICT: PASS\nVERDICT: NEEDS_REVIEW"},
            "NEEDS_REVIEW",
        ),
    ],
)
def test_s5_r5_r6_r7_evaluator_pass_requires_explicit_assessment(
    _no_event_journal, label, output, expected
):
    assert _evaluate(_Runtime(output))["verdict"] == expected, label


def test_s6_r8_evaluator_exception_is_error_not_pass(_no_event_journal):
    result = _evaluate(_Runtime(raises=TimeoutError("evaluator timed out")))

    assert result["verdict"] == "ERROR"


def _seed_auto_publish(db_session, tmp_path, monkeypatch, runtime, verdict):
    ctx, execution, project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    ctx.runtime_service = runtime
    (workspace_dir / "README.md").write_text("before\n", encoding="utf-8")
    ctx.task_service.create_workspace_snapshot(
        ctx.project,
        workspace_dir,
        snapshot_key=workspace_snapshot_key(ctx.task_id, execution.id),
        preserve_project_root_rules=False,
    )
    (workspace_dir / "README.md").write_text("after\n", encoding="utf-8")
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.get_effective_workspace_review_policy",
        lambda default_policy, db=None: "auto_publish_all",
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: verdict,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_baseline_publish",
        lambda **kwargs: ValidationVerdict(
            stage="baseline_publish",
            status="accepted",
            profile="mutation",
            reasons=[],
            details={},
        ),
    )
    result = _complete_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )
    return result, ctx, project_root


def _accepted(status="accepted", reasons=()):
    return ValidationVerdict(
        stage="task_completion",
        status=status,
        profile="mutation",
        reasons=list(reasons),
        details={"expected_core_files": ["README.md"]},
    )


def _hold_payload(db_session, ctx):
    review_log = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_id == ctx.task_id)
        .filter(
            LogEntry.message == "[ORCHESTRATION] Held task workspace for manual review"
        )
        .one()
    )
    return json.loads(review_log.log_metadata)


@pytest.mark.parametrize(
    ("label", "runtime", "expected_reason"),
    [
        ("r7_empty", _Runtime({"output": ""}), "evaluator_assessment_unavailable"),
        (
            "reentry3_non_assessment",
            _Runtime({"output": REENTRY3_EVALUATOR_OUTPUT}),
            "evaluator_assessment_unavailable",
        ),
        (
            "r8_error",
            _Runtime(raises=TimeoutError("evaluator timed out")),
            "evaluator_assessment_unavailable",
        ),
        (
            "r6_needs_review",
            _Runtime({"output": SUBSTANTIVE_NEEDS_REVIEW}),
            "evaluator_needs_review",
        ),
    ],
)
def test_r6_r7_r8_non_pass_evaluator_holds_instead_of_auto_publishing(
    db_session, tmp_path, monkeypatch, label, runtime, expected_reason
):
    result, ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _accepted()
    )

    assert runtime.evaluator_calls == 1, label
    assert result["status"] == "completed"
    assert not (project_root / "README.md").exists()
    assert ctx.task.workspace_status == "ready"
    payload = _hold_payload(db_session, ctx)
    assert payload["auto_publish_skipped"] is True
    assert payload["reason"] == expected_reason


def test_r1_r5_explicit_evaluator_pass_preserves_auto_publication(
    db_session, tmp_path, monkeypatch
):
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, _ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, _accepted()
    )

    assert runtime.evaluator_calls == 1
    assert result["status"] == "completed"
    assert (project_root / "README.md").read_text(encoding="utf-8") == "after\n"


def test_s8_r9_smoke_only_warning_with_explicit_pass_still_publishes(
    db_session, tmp_path, monkeypatch
):
    # Pinned current policy (carried gap): Review does not consult the
    # completion verdict's smoke-only/verification_insufficient warning.
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, _ctx, project_root = _seed_auto_publish(
        db_session,
        tmp_path,
        monkeypatch,
        runtime,
        _accepted(
            status="warning",
            reasons=[
                "Repair task verification is smoke-only; independent behavioral "
                "evidence is weak"
            ],
        ),
    )

    assert result["status"] == "completed"
    assert (project_root / "README.md").read_text(encoding="utf-8") == "after\n"


def test_s7_r2_r5_deterministic_failure_blocks_before_evaluator_pass(
    db_session, tmp_path, monkeypatch
):
    finding = CandidateFinding(
        rule_id="candidate_flake8_failed",
        source="flake8",
        category="static",
        severity="error",
        attribution="candidate_introduced",
        repairable=False,
        message="Candidate-scoped flake8 failed",
    )
    verdict = ValidationVerdict(
        stage="task_completion",
        status="rejected",
        profile="mutation",
        reasons=[finding.message],
        details={"expected_core_files": ["README.md"]},
        findings=[finding],
    )
    runtime = _Runtime({"output": SUBSTANTIVE_PASS})
    result, _ctx, project_root = _seed_auto_publish(
        db_session, tmp_path, monkeypatch, runtime, verdict
    )

    assert result["status"] == "failed"
    assert runtime.evaluator_calls == 0
    assert not (project_root / "README.md").exists()


def _candidate_project(tmp_path, files, *, flake8=True):
    root = tmp_path / "candidate"
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    if flake8:
        shutil.copyfile(REPO_ROOT / ".flake8", root / ".flake8")
    subprocess.run(
        "git init -q && git add -A && "
        "git -c user.email=gr7@example.invalid -c user.name=gr7 commit -qm base",
        shell=True,
        cwd=root,
        check=True,
    )
    return root


def _check(root, changed, plan=()):
    return validate_candidate_delta(
        project_dir=root,
        change_set={"modified_files": list(changed)},
        plan=list(plan),
        task_prompt="Repair the permissions route",
    )


def _rule_ids(run):
    return [finding.rule_id for finding in run.findings]


def test_s0_r12_reentry4_shape_compiles_and_is_caught_only_by_admitted_flake8(
    tmp_path,
):
    compile(ROUTER_REENTRY4, "router.py", "exec")
    root = _candidate_project(tmp_path, {"app/router.py": ROUTER_BASE})
    (root / "app/router.py").write_text(ROUTER_REENTRY4, encoding="utf-8")

    run = _check(root, ["app/router.py"])

    assert run.selection.source == "no_trustworthy_focused_tests"
    assert _rule_ids(run) == ["candidate_flake8_failed"]
    assert "F821 undefined name 'api_router'" in run.findings[0].evidence["output"]


def test_s0_r12_reentry4_shape_without_project_flake8_is_undetected(tmp_path):
    root = _candidate_project(tmp_path, {"app/router.py": ROUTER_BASE}, flake8=False)
    (root / "app/router.py").write_text(ROUTER_REENTRY4, encoding="utf-8")

    assert _rule_ids(_check(root, ["app/router.py"])) == []


def test_s1_valid_narrow_mutation_has_no_findings(tmp_path):
    root = _candidate_project(tmp_path, {"app/router.py": ROUTER_BASE})
    (root / "app/router.py").write_text(ROUTER_FIXED, encoding="utf-8")

    assert _rule_ids(_check(root, ["app/router.py"])) == []


def test_s2_r13_unrelated_repository_test_is_not_selected(tmp_path):
    # REENTRY-3 shape: the Plan assertion passes, an existing repository test
    # fails, but no name-matched test exists, so none runs.
    root = _candidate_project(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/router.py": ROUTER_BASE,
            "tests/test_routes.py": PREFIX_TEST,
        },
    )
    (root / "app/router.py").write_text(ROUTER_FIXED, encoding="utf-8")
    assert subprocess.run(PLAN_ASSERTION, shell=True, cwd=root).returncode == 0

    run = _check(root, ["app/router.py"], [{"verification": PLAN_ASSERTION}])

    assert run.selection.source == "no_trustworthy_focused_tests"
    assert _rule_ids(run) == []


def test_s2_r13_name_matched_repository_test_failure_is_authoritative(tmp_path):
    root = _candidate_project(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/router.py": ROUTER_BASE,
            "tests/test_router.py": PREFIX_TEST,
        },
    )
    (root / "app/router.py").write_text(ROUTER_FIXED, encoding="utf-8")

    run = _check(root, ["app/router.py"], [{"verification": PLAN_ASSERTION}])

    assert run.selection.source == "deterministic_existing_regression_tests"
    assert _rule_ids(run) == ["focused_pytest_failed"]
    assert run.findings[0].severity == "error"
    assert run.findings[0].repairable is True


def test_r10_r11_no_baseline_differential_for_candidate_static_checks(tmp_path):
    # Pinned current semantics (carried gap): a failure already present at
    # baseline is reported exactly like a candidate-introduced one.
    root = _candidate_project(tmp_path, {"app/m.py": "x = undefined_name\n"})
    (root / "app/m.py").write_text("x = undefined_name\ny = 1\n", encoding="utf-8")
    baseline_only = _check(root, ["app/m.py"])

    root_new = _candidate_project(tmp_path / "new", {"app/m.py": "x = 1\n"})
    (root_new / "app/m.py").write_text("x = undefined_name\n", encoding="utf-8")
    introduced = _check(root_new, ["app/m.py"])

    for run in (baseline_only, introduced):
        assert _rule_ids(run) == ["candidate_flake8_failed"]
        assert run.findings[0].attribution == "candidate_introduced"


def test_r4_unavailable_verifier_is_non_repairable_error(tmp_path, monkeypatch):
    root = _candidate_project(tmp_path, {"app/router.py": ROUTER_BASE})
    (root / "app/router.py").write_text(ROUTER_FIXED, encoding="utf-8")

    def _fake_run(*, project_dir, command, timeout_seconds):
        if "flake8" in command:
            return 1, "No module named flake8"
        return 0, ""

    monkeypatch.setattr(candidate_checks, "_run_command", _fake_run)
    run = _check(root, ["app/router.py"])

    assert _rule_ids(run) == ["flake8_infrastructure_failure"]
    assert run.findings[0].severity == "error"
    assert run.findings[0].repairable is False
