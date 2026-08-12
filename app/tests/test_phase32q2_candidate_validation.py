"""Phase 32Q-2 candidate truth authority invariants."""

from pathlib import Path

from app.services.orchestration.validation.candidate_checks import (
    candidate_delta_identity,
    select_candidate_verification,
    validate_candidate_delta,
)
from app.services.orchestration.types import (
    CandidateFinding,
    CandidateValidationResult,
    ValidationVerdict,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.phases.completion_repair import (
    _apply_completion_repair_ops_direct,
)


def test_changed_python_test_is_the_focused_candidate_verification(
    tmp_path: Path,
) -> None:
    test_path = tmp_path / "app" / "tests" / "test_utc_now_helper.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\ntestpaths = app/tests\n", encoding="utf-8"
    )

    selection = select_candidate_verification(
        project_dir=tmp_path,
        change_set={
            "added_files": [],
            "modified_files": ["app/tests/test_utc_now_helper.py"],
            "deleted_files": [],
        },
        plan=[],
        task_prompt="Fix utc_now and its focused regression test",
    )

    assert selection.source == "candidate_changed_python_tests"
    assert selection.paths == ("app/tests/test_utc_now_helper.py",)
    assert selection.command.endswith(" -m pytest app/tests/test_utc_now_helper.py")
    assert "app/tests/test_utc_now_helper.py" in selection.command
    assert selection.fallback is False


def test_candidate_result_derives_one_repair_decision_from_typed_findings() -> None:
    result = CandidateValidationResult.from_findings(
        profile="implementation",
        findings=[
            CandidateFinding(
                rule_id="focused_pytest_failed",
                source="pytest",
                category="test",
                severity="error",
                attribution="candidate_introduced",
                repairable=True,
                message="Focused candidate test failed",
                evidence={"command": "python -m pytest app/tests/test_example.py"},
            ),
            CandidateFinding(
                rule_id="baseline_placeholder_debt",
                source="task_contract",
                category="source",
                severity="warning",
                attribution="unchanged_baseline_debt",
                repairable=False,
                message="Untouched placeholder remains",
            ),
        ],
        candidate_identity="sha256:abc",
    )

    assert CandidateValidationResult is ValidationVerdict
    assert result.status == "repair_required"
    assert result.repairable is True
    assert [finding.rule_id for finding in result.repairable_findings] == [
        "focused_pytest_failed"
    ]
    assert result.validator_rule_ids == [
        "focused_pytest_failed",
        "baseline_placeholder_debt",
    ]
    assert result.to_dict()["candidate_identity"] == "sha256:abc"


def test_focused_candidate_pytest_surfaces_real_failure_without_suite_fallback(
    tmp_path: Path,
) -> None:
    test_path = tmp_path / "app" / "tests" / "test_utc_now_helper.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "from datetime import datetime\n\n"
        "def test_utc_now_helper():\n"
        "    assert datetime.timedelta(seconds=1)\n",
        encoding="utf-8",
    )

    result = validate_candidate_delta(
        project_dir=tmp_path,
        change_set={
            "added_files": ["app/tests/test_utc_now_helper.py"],
            "modified_files": [],
            "deleted_files": [],
        },
        plan=[],
        task_prompt="Repair utc_now datetime handling",
        include_static_checks=False,
    )

    assert result.selection.paths == ("app/tests/test_utc_now_helper.py",)
    assert result.commands_run == (result.selection.command,)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.rule_id == "focused_pytest_failed"
    assert finding.source == "pytest"
    assert finding.category == "test"
    assert finding.attribution == "candidate_introduced"
    assert finding.repairable is True
    assert finding.evidence["returncode"] == 1
    assert "has no attribute 'timedelta'" in finding.evidence["output"]
    assert "timed out" not in finding.evidence["output"].lower()
    assert not (tmp_path / ".pytest_cache").exists()
    assert not any(tmp_path.rglob("__pycache__"))


def test_missing_candidate_delta_is_typed_unknown_without_workspace_scan(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "legacy.py"
    baseline.write_text("# TODO: unrelated historical placeholder\n", encoding="utf-8")

    result = ValidatorService.validate_task_completion(
        project_dir=tmp_path,
        plan=[],
        task_prompt="Implement candidate",
        execution_profile="full_lifecycle",
        completion_evidence={
            "candidate_delta_required": True,
            "summary_generated": True,
            "execution_results_count": 1,
        },
    )

    assert result.status == "unknown"
    assert result.accepted is False
    assert [finding.rule_id for finding in result.findings] == [
        "candidate_delta_unavailable"
    ]
    assert result.findings[0].attribution == "unknown"
    assert result.findings[0].category == "infrastructure"
    assert "placeholder" not in " ".join(result.reasons).lower()
    assert result.details["validated_files"] == []


def test_task_completion_result_owns_focused_test_truth(tmp_path: Path) -> None:
    source = tmp_path / "app" / "utc_now.py"
    test_path = tmp_path / "app" / "tests" / "test_utc_now_helper.py"
    source.parent.mkdir(parents=True)
    test_path.parent.mkdir(parents=True)
    source.write_text("def utc_now():\n    return None\n", encoding="utf-8")
    test_path.write_text(
        "from datetime import datetime\n\n"
        "def test_utc_now_helper():\n"
        "    assert datetime.timezone.utc\n"
        "    assert datetime.timedelta(seconds=1)\n",
        encoding="utf-8",
    )
    change_set = {
        "added_files": [],
        "modified_files": [
            "app/utc_now.py",
            "app/tests/test_utc_now_helper.py",
        ],
        "deleted_files": [],
    }

    result = ValidatorService.validate_task_completion(
        project_dir=tmp_path,
        plan=[
            {
                "step_number": 1,
                "description": "Implement app/utc_now.py",
                "expected_files": ["app/utc_now.py"],
                "verification": "python -m pytest app/tests/test_utc_now_helper.py",
            }
        ],
        task_prompt="Implement the UTC now helper",
        execution_profile="full_lifecycle",
        completion_evidence={
            "candidate_delta_required": True,
            "run_candidate_checks": True,
            "include_static_checks": False,
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": list(change_set["modified_files"]),
            "change_set": change_set,
        },
    )

    assert result.status == "repair_required"
    assert [finding.rule_id for finding in result.repairable_findings] == [
        "focused_pytest_failed"
    ]
    assert result.details["test_findings"][0]["evidence"]["returncode"] == 1
    assert result.details["focused_test_selection"]["paths"] == [
        "app/tests/test_utc_now_helper.py"
    ]


def test_static_checks_are_candidate_file_scoped(tmp_path: Path) -> None:
    candidate = tmp_path / "app" / "bad_style.py"
    untouched = tmp_path / "legacy.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("def answer( ):\n return 42\n", encoding="utf-8")
    untouched.write_text("def baseline( ):\n return 1\n", encoding="utf-8")

    run = validate_candidate_delta(
        project_dir=tmp_path,
        change_set={
            "added_files": ["app/bad_style.py"],
            "modified_files": [],
            "deleted_files": [],
        },
        plan=[],
        task_prompt="Add answer",
        include_static_checks=True,
    )

    sources = {finding.source for finding in run.findings}
    assert "black" in sources
    assert "flake8" in sources
    assert all("legacy.py" not in command for command in run.commands_run)
    assert all(
        finding.attribution == "candidate_introduced" for finding in run.findings
    )


def test_candidate_repair_rejects_undeclared_fourth_path_before_mutation(
    tmp_path: Path,
) -> None:
    authorized = tmp_path / "app" / "owned.py"
    authorized.parent.mkdir(parents=True)
    authorized.write_text("value = 1\n", encoding="utf-8")

    result = _apply_completion_repair_ops_direct(
        [{"op": "write_file", "path": "app/fourth.py", "content": "bad = 1\n"}],
        tmp_path,
        repair_authorized_scope={"app/owned.py"},
    )

    assert result["success"] is False
    assert result["applied"] == []
    assert any("outside repair_authorized_scope" in error for error in result["errors"])
    assert not (tmp_path / "app" / "fourth.py").exists()


def test_candidate_repair_batch_rolls_back_byte_identically(tmp_path: Path) -> None:
    target = tmp_path / "app" / "owned.py"
    target.parent.mkdir(parents=True)
    before = b"value = 1\n"
    target.write_bytes(before)

    result = _apply_completion_repair_ops_direct(
        [
            {
                "op": "replace_in_file",
                "path": "app/owned.py",
                "old": "value = 1",
                "new": "value = 2",
            },
            {
                "op": "replace_in_file",
                "path": "app/missing.py",
                "old": "x",
                "new": "y",
            },
        ],
        tmp_path,
        repair_authorized_scope={"app/owned.py", "app/missing.py"},
    )

    assert result["success"] is False
    assert result["applied"] == []
    assert target.read_bytes() == before
    assert not (tmp_path / "app" / "missing.py").exists()


def test_candidate_identity_includes_candidate_bytes(tmp_path: Path) -> None:
    candidate = tmp_path / "app" / "owned.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("value = 1\n", encoding="utf-8")
    change_set = {
        "target_path": str(tmp_path),
        "added_files": ["app/owned.py"],
        "modified_files": [],
        "deleted_files": [],
    }

    before = candidate_delta_identity(change_set)
    candidate.write_text("value = 2\n", encoding="utf-8")
    after = candidate_delta_identity(change_set)

    assert before != after


def test_changed_source_selects_deterministically_named_existing_regression(
    tmp_path: Path,
) -> None:
    source = tmp_path / "app" / "time_utils.py"
    regression = tmp_path / "app" / "tests" / "test_time_utils.py"
    source.parent.mkdir(parents=True)
    regression.parent.mkdir(parents=True)
    source.write_text("def utc_now():\n    return None\n", encoding="utf-8")
    regression.write_text("def test_utc_now():\n    assert True\n", encoding="utf-8")

    selection = select_candidate_verification(
        project_dir=tmp_path,
        change_set={
            "added_files": [],
            "modified_files": ["app/time_utils.py"],
            "deleted_files": [],
        },
        plan=[],
        task_prompt="Fix utc_now",
    )

    assert selection.source == "deterministic_existing_regression_tests"
    assert selection.paths == ("app/tests/test_time_utils.py",)


def test_repository_pytest_is_only_an_explicit_fallback(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    change_set = {"added_files": [], "modified_files": [], "deleted_files": []}

    default = select_candidate_verification(
        project_dir=tmp_path,
        change_set=change_set,
        plan=[],
        task_prompt="Verify configuration",
    )
    fallback = select_candidate_verification(
        project_dir=tmp_path,
        change_set=change_set,
        plan=[],
        task_prompt="Verify configuration",
        allow_broad_fallback=True,
    )

    assert default.command == ""
    assert fallback.command.endswith(" -m pytest")
    assert fallback.source == "explicit_repository_pytest_fallback"
    assert fallback.fallback is True


def test_candidate_repair_rolls_back_when_second_write_raises(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "app" / "first.py"
    second = tmp_path / "app" / "second.py"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"first = 1\n")
    second.write_bytes(b"second = 1\n")
    original_write_text = Path.write_text

    def fail_second(path: Path, content: str, **kwargs):
        if path == second:
            raise OSError("synthetic second-write failure")
        return original_write_text(path, content, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_second)
    result = _apply_completion_repair_ops_direct(
        [
            {"op": "write_file", "path": "app/first.py", "content": "first = 2\n"},
            {
                "op": "write_file",
                "path": "app/second.py",
                "content": "second = 2\n",
            },
        ],
        tmp_path,
        repair_authorized_scope={"app/first.py", "app/second.py"},
    )

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert result["applied"] == []
    assert first.read_bytes() == b"first = 1\n"
    assert second.read_bytes() == b"second = 1\n"
