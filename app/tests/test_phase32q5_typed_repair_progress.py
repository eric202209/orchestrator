"""Phase 32Q-5 regression tests for Candidate Repair truthfulness."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.execution.python_resolution import (
    resolve_project_python,
)
from app.services.orchestration.phases.completion_repair_capsule import (
    CompletionRepairProgress,
    build_bounded_completion_repair_prompt,
    build_completion_repair_capsule,
    classify_completion_repair_progress,
    completion_repair_finding_signature,
)
from app.services.orchestration.phases.completion_repair import (
    _repeats_prior_completion_failure,
)
from app.services.orchestration.types import CandidateFinding, CandidateValidationResult
from app.services.orchestration.validation.candidate_checks import _candidate_python


class _State:
    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.plan = []
        self.execution_results = []


def _finding(
    rule_id: str,
    *,
    paths: list[str],
    repairable: bool = True,
) -> CandidateFinding:
    return CandidateFinding(
        rule_id=rule_id,
        source="pytest" if rule_id == "focused_pytest_failed" else "black",
        category="test" if rule_id == "focused_pytest_failed" else "static",
        severity="error",
        attribution="candidate_introduced",
        repairable=repairable,
        message=f"{rule_id} for {' and '.join(paths)}",
        evidence={
            "command": f"python -m pytest {' '.join(paths)}",
            "returncode": 1,
            "output": f"failure in {' '.join(paths)}",
            "paths": paths,
            "internal_only": "must not reach the provider",
        },
    )


def _verdict(
    findings: list[CandidateFinding], identity: str, *, status: str = "repair_required"
) -> CandidateValidationResult:
    return CandidateValidationResult(
        stage="task_completion",
        status=status,
        profile="implementation",
        findings=findings,
        candidate_identity=identity,
    )


def test_typed_repair_objectives_preserve_path_mapping_and_render_all_rule(
    tmp_path: Path,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "time_utils.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "app" / "tests").mkdir()
    (tmp_path / "app" / "tests" / "test_utc_now_helper.py").write_text(
        "def test_now():\n    assert True\n", encoding="utf-8"
    )
    pytest_finding = _finding(
        "focused_pytest_failed", paths=["app/tests/test_utc_now_helper.py"]
    )
    black_finding = _finding("candidate_black_failed", paths=["app/time_utils.py"])
    validation = _verdict([pytest_finding, black_finding], "sha256:before")

    capsule = build_completion_repair_capsule(
        task_prompt="repair candidate",
        completion_validation=validation,
        orchestration_state=_State(tmp_path),
    )
    objectives = {entry["rule_id"]: entry for entry in capsule.repair_objectives}
    assert objectives["focused_pytest_failed"]["candidate_paths"] == [
        "app/tests/test_utc_now_helper.py"
    ]
    assert objectives["candidate_black_failed"]["candidate_paths"] == [
        "app/time_utils.py"
    ]
    assert objectives["focused_pytest_failed"]["source"] == "pytest"
    assert objectives["focused_pytest_failed"]["evidence"]["returncode"] == 1
    assert "internal_only" not in objectives["focused_pytest_failed"]["evidence"]

    prompt = build_bounded_completion_repair_prompt(capsule, 1)
    assert "repair all actionable repairable findings represented below" in prompt
    assert '"rule_id": "focused_pytest_failed"' in prompt
    assert '"candidate_paths": [' in prompt
    assert "internal_only" not in prompt


def test_progress_semantics_cover_resolved_partial_no_progress_and_regression() -> None:
    pytest_finding = _finding(
        "focused_pytest_failed", paths=["app/tests/test_utc_now_helper.py"]
    )
    black_finding = _finding("candidate_black_failed", paths=["app/time_utils.py"])
    flake8_finding = _finding("candidate_flake8_failed", paths=["app/time_utils.py"])
    before = _verdict([pytest_finding, black_finding, flake8_finding], "sha256:before")

    assert (
        classify_completion_repair_progress(
            before,
            _verdict([], "sha256:after", status="accepted"),
        )
        == CompletionRepairProgress.RESOLVED
    )
    assert (
        classify_completion_repair_progress(
            before,
            _verdict([black_finding, flake8_finding], "sha256:after"),
        )
        == CompletionRepairProgress.PARTIAL_PROGRESS
    )
    assert (
        classify_completion_repair_progress(
            before,
            _verdict([pytest_finding, black_finding, flake8_finding], "sha256:after"),
        )
        == CompletionRepairProgress.NO_PROGRESS_OR_REGRESSION
    )
    compile_finding = _finding(
        "candidate_python_compile_failed", paths=["app/time_utils.py"]
    )
    assert (
        classify_completion_repair_progress(
            before,
            _verdict([black_finding, flake8_finding, compile_finding], "sha256:after"),
        )
        == CompletionRepairProgress.NO_PROGRESS_OR_REGRESSION
    )
    assert (
        classify_completion_repair_progress(
            before,
            _verdict([black_finding, flake8_finding], "sha256:before"),
        )
        == CompletionRepairProgress.NO_PROGRESS_OR_REGRESSION
    )


def test_no_local_venv_uses_the_worker_interpreter_for_both_authorities(
    tmp_path: Path,
) -> None:
    assert _candidate_python(tmp_path) == resolve_project_python(tmp_path)
    assert resolve_project_python(tmp_path) == sys.executable


def test_q6_typed_finding_signature_is_stable_and_partial_input_is_not_a_repeat() -> (
    None
):
    first = _finding("candidate_black_failed", paths=["app/time_utils.py"])
    changed_evidence = CandidateFinding(
        rule_id=first.rule_id,
        source=first.source,
        category=first.category,
        severity=first.severity,
        attribution=first.attribution,
        repairable=first.repairable,
        message="diagnostic prose changed",
        evidence={"output": "different run output"},
    )
    prior = _verdict([first], "sha256:before")
    current = _verdict([changed_evidence], "sha256:after")
    current.details["completion_repair_progress"] = "PARTIAL_PROGRESS"
    state = SimpleNamespace(last_completion_validation=prior.to_dict())

    assert completion_repair_finding_signature(prior) == (
        completion_repair_finding_signature(current)
    )
    assert _repeats_prior_completion_failure(state, current) is False

    current.details["completion_repair_progress"] = "NO_PROGRESS_OR_REGRESSION"
    assert _repeats_prior_completion_failure(state, current) is True
