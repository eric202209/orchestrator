"""POST33-R1 provider-free Candidate Repair format/contract fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.phases.completion_flow import (
    _completion_repair_contract_summary,
    _extract_completion_repair_json_text,
)
from app.services.orchestration.phases.completion_repair import (
    _apply_completion_repair_ops_direct,
    _canonicalize_completion_repair_envelope,
    _completion_repair_invalid_paths,
    _repeats_prior_completion_failure,
)
from app.services.orchestration.phases.completion_repair_capsule import (
    CompletionRepairCapsule,
    build_bounded_completion_repair_prompt,
    classify_completion_repair_progress,
)
from app.services.orchestration.phases.execution_local_steps import (
    _is_simple_verification_command,
)
from app.services.orchestration.diagnostics.signature_guard import (
    check_completion_repair_signature_contract,
)
from app.services.orchestration.types import CandidateFinding, CandidateValidationResult
from app.services.orchestration.validation.candidate_checks import (
    validate_candidate_delta,
)


def _repair_step(*, path: str = "app/formatting.py", new: str = "value = 1\n") -> dict:
    return {
        "step_number": 5,
        "repair_type": "ops_fix",
        "description": "Fix the Candidate formatting/static finding",
        "ops": [
            {
                "op": "replace_in_file",
                "path": path,
                "old": "value=1\n",
                "new": new,
            }
        ],
        "verification": "python -m pytest -q",
        "expected_files": [path],
    }


def _envelope(**step_overrides: object) -> dict:
    step = _repair_step()
    step.update(step_overrides)
    return {"repair_step": step}


def _extract_and_parse(raw: str) -> tuple[str, object | None]:
    extracted = _extract_completion_repair_json_text(raw)
    if not extracted:
        return extracted, None
    try:
        return extracted, json.loads(extracted)
    except json.JSONDecodeError:
        return extracted, None


def _finding(rule_id: str, *, repairable: bool = True) -> CandidateFinding:
    return CandidateFinding(
        rule_id=rule_id,
        source="black" if rule_id == "candidate_black_failed" else "flake8",
        category="static",
        severity="error",
        attribution="candidate_introduced",
        repairable=repairable,
        message=f"Candidate-scoped {rule_id} failed",
        evidence={"paths": ["app/formatting.py"]},
    )


def test_valid_canonical_json_is_one_canonical_object() -> None:
    extracted, parsed = _extract_and_parse(json.dumps(_envelope()))
    assert extracted
    assert parsed == _envelope()
    assert _canonicalize_completion_repair_envelope(parsed, 5) is not None


def test_valid_json_inside_markdown_fence_is_extracted_without_fence() -> None:
    extracted, parsed = _extract_and_parse(
        "```json\n" + json.dumps(_envelope()) + "\n```"
    )
    assert extracted.startswith("{") and extracted.endswith("}")
    assert parsed == _envelope()


def test_bounded_leading_and_trailing_prose_is_accepted() -> None:
    extracted, parsed = _extract_and_parse(
        "Here is the one bounded repair:\n"
        + json.dumps(_envelope())
        + "\nNo other files are changed."
    )
    assert parsed == _envelope()
    assert _canonicalize_completion_repair_envelope(parsed, 5) is not None


def test_malformed_json_fails_at_extraction() -> None:
    extracted, parsed = _extract_and_parse('{"repair_step":{"ops":[}}')
    assert extracted == ""
    assert parsed is None


def test_truncated_json_fails_closed_instead_of_salvaging_nested_step() -> None:
    truncated = (
        '{"repair_step":{"description":"Fix static formatting",'
        '"ops":[{"op":"replace_in_file","path":"app/formatting.py"'
    )
    extracted, parsed = _extract_and_parse(truncated)
    assert extracted == ""
    assert parsed is None


def test_two_conflicting_json_objects_fail_closed() -> None:
    first = json.dumps(_envelope())
    second = json.dumps(
        _envelope(
            **{
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "app/formatting.py",
                        "old": "value=1\n",
                        "new": "value  =  1\n",
                    }
                ]
            }
        )
    )
    extracted, parsed = _extract_and_parse(first + "\n" + second)
    assert extracted == ""
    assert parsed is None


def test_task_192_shape_is_parseable_but_contract_invalid() -> None:
    raw = _envelope(
        commands=["python -m black app/services/project/name_formatter.py"],
        ops=None,
        expected_files=["app/services/project/name_formatter.py"],
    )
    extracted, parsed = _extract_and_parse(json.dumps(raw))
    assert parsed is not None
    assert _canonicalize_completion_repair_envelope(parsed, 5) is None


def test_task_195_shape_is_parse_failure() -> None:
    raw = (
        '{"repair_step":{"description":"Fix static formatting",'
        '"ops":[{"op":"replace_in_file","path":"app/workspace.py"'
    )
    extracted, parsed = _extract_and_parse(raw)
    assert extracted == ""
    assert parsed is None


def test_missing_or_unsupported_operations_fail_contract() -> None:
    missing = _envelope(ops=[])
    unsupported = _envelope(
        ops=[{"op": "run_shell", "path": "app/formatting.py", "content": "x"}]
    )
    assert _canonicalize_completion_repair_envelope(missing, 5) is None
    assert _canonicalize_completion_repair_envelope(unsupported, 5) is None


def test_alias_verification_command_is_canonicalized() -> None:
    raw = _envelope()
    raw["repair_step"].pop("verification")
    raw["repair_step"]["verification_command"] = "python -m pytest -q"
    canonical = _canonicalize_completion_repair_envelope(raw, 5)
    assert canonical is not None
    assert canonical["repair_step"]["verification"] == "python -m pytest -q"


def test_unknown_fields_do_not_become_executable_operations() -> None:
    canonical = _canonicalize_completion_repair_envelope(
        _envelope(unexpected_field="ignored"), 5
    )
    assert canonical is not None
    assert "unexpected_field" not in canonical["repair_step"]


def test_unsafe_and_out_of_contract_verification_commands_fail_closed() -> None:
    assert not _is_simple_verification_command("pytest; rm -rf app")
    assert not _is_simple_verification_command('python -c \'open("x", "w")\'')
    assert _is_simple_verification_command("python -m pytest -q")


def test_verification_command_matrix_matches_existing_safety_grammar(
    tmp_path: Path,
) -> None:
    authorized = tmp_path / "app/formatting.py"
    authorized.parent.mkdir(parents=True)
    authorized.write_text("value = 1\n", encoding="utf-8")
    unrelated = tmp_path / "app/other.py"
    unrelated.write_text("other = 1\n", encoding="utf-8")
    valid = [
        "python -m compileall app/formatting.py",
        "python3 -m compileall app/formatting.py",
        "pytest app/tests/test_example.py",
        "python -m pytest app/tests/test_example.py",
        "npm run build",
        "python -m compileall app/other.py",
    ]
    invalid = [
        "black --check app/formatting.py",
        "python -m black --check app/formatting.py",
        "flake8 app/formatting.py",
        "python -m flake8 app/formatting.py",
        "python -m compileall app/formatting.py && true",
        "python -m compileall app/formatting.py; true",
        "python -m compileall app/formatting.py | tee output.txt",
        "python -m compileall app/formatting.py > output.txt",
        f"python -m compileall {authorized.resolve()}",
        "python -m compileall ../app/formatting.py",
        "",
    ]
    assert all(
        _is_simple_verification_command(command, project_dir=tmp_path)
        for command in valid
    )
    assert not any(
        _is_simple_verification_command(command, project_dir=tmp_path)
        for command in invalid
    )


def test_active_repair_prompt_names_the_existing_verification_contract() -> None:
    prompt = build_bounded_completion_repair_prompt(
        CompletionRepairCapsule(
            validation_reasons=["Candidate-scoped black failed"],
            relevant_files=["app/formatting.py"],
            last_step_summary="Step 1 completed.",
            workspace_path="/tmp/project",
            task_prompt_excerpt="Format the candidate.",
        ),
        2,
    )
    assert "python[3] -m compileall <.py/dir>" in prompt
    assert "Avoid `black`/`flake8`" in prompt
    assert "metacharacters" in prompt


def test_repair_contract_summary_is_bounded_and_sanitized() -> None:
    summary = _completion_repair_contract_summary(
        extraction_status="present",
        parse_status="accepted",
        canonical_contract_status="accepted",
        verification_command="black --check app/formatting.py",
        verification_safety_status="rejected",
        verification_safety_reason="completion_repair_verification_command_unsafe",
        repair_step={"ops": [{"op": "replace_in_file", "path": "app/formatting.py"}]},
    )
    assert summary["extraction_status"] == "present"
    assert summary["parse_status"] == "accepted"
    assert summary["canonical_contract_status"] == "accepted"
    assert summary["verification_command_family"] == "black"
    assert summary["verification_safety_status"] == "rejected"
    assert summary["verification_safety_reason"] == (
        "completion_repair_verification_command_unsafe"
    )
    assert len(summary["verification_command_hash"]) == 64
    assert "verification_command" not in summary
    assert summary["repair_op_paths"] == ["app/formatting.py"]


def test_wrong_repair_signature_fails_before_application(tmp_path: Path) -> None:
    source = tmp_path / "app/formatting.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def answer(value: int, *, verbose: bool = False) -> int:\n"
        "    return value\n",
        encoding="utf-8",
    )
    result = check_completion_repair_signature_contract(
        project_dir=tmp_path,
        ops=[
            {
                "op": "replace_in_file",
                "path": "app/formatting.py",
                "old": "def answer(value: int, *, verbose: bool = False) -> int:",
                "new": "def answer(value: int) -> int:",
            }
        ],
    )
    assert result.violations
    assert result.violations[0].violation_type == "signature_changed"


def test_path_scope_rejects_wrong_readonly_traversal_and_case_aliases(
    tmp_path: Path,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app/formatting.py").write_text("value=1\n", encoding="utf-8")
    authorized = {"app/formatting.py"}
    for path in (
        "app/other.py",
        "app/readonly.py",
        "../app/formatting.py",
        "App/formatting.py",
    ):
        assert _completion_repair_invalid_paths(
            repair_step={"ops": [{"op": "write_file", "path": path, "content": "x"}]},
            project_dir=tmp_path,
            repair_authorized_scope=authorized,
        )


def test_valid_bounded_repair_revalidates_to_candidate_success(tmp_path: Path) -> None:
    candidate = tmp_path / "app/formatting.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("def answer( ):\n return 42\n", encoding="utf-8")
    change_set = {
        "added_files": [],
        "modified_files": ["app/formatting.py"],
        "deleted_files": [],
    }
    before = validate_candidate_delta(
        project_dir=tmp_path,
        change_set=change_set,
        plan=[],
        task_prompt="Format answer",
        include_static_checks=True,
    )
    assert any(f.rule_id == "candidate_black_failed" for f in before.findings)
    applied = _apply_completion_repair_ops_direct(
        [
            {
                "op": "replace_in_file",
                "path": "app/formatting.py",
                "old": "def answer( ):\n return 42\n",
                "new": "def answer():\n    return 42\n",
            }
        ],
        tmp_path,
        repair_authorized_scope={"app/formatting.py"},
    )
    assert applied["success"] is True
    after = validate_candidate_delta(
        project_dir=tmp_path,
        change_set=change_set,
        plan=[],
        task_prompt="Format answer",
        include_static_checks=True,
    )
    assert after.findings == ()


def test_valid_application_can_still_fail_candidate_revalidation(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "app/formatting.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("value=1\n", encoding="utf-8")
    applied = _apply_completion_repair_ops_direct(
        [
            {
                "op": "replace_in_file",
                "path": "app/formatting.py",
                "old": "value=1\n",
                "new": "def broken(:\n",
            }
        ],
        tmp_path,
        repair_authorized_scope={"app/formatting.py"},
    )
    assert applied["success"] is True
    after = validate_candidate_delta(
        project_dir=tmp_path,
        change_set={
            "added_files": [],
            "modified_files": ["app/formatting.py"],
            "deleted_files": [],
        },
        plan=[],
        task_prompt="Format value",
        include_static_checks=True,
    )
    assert after.findings


def test_no_progress_and_repeated_signature_remain_bounded() -> None:
    before = CandidateValidationResult.from_findings(
        profile="implementation",
        findings=[_finding("candidate_black_failed")],
        candidate_identity="sha256:before",
    )
    same = CandidateValidationResult.from_findings(
        profile="implementation",
        findings=[_finding("candidate_black_failed")],
        candidate_identity="sha256:before",
    )
    assert (
        classify_completion_repair_progress(before, same).value
        == "NO_PROGRESS_OR_REGRESSION"
    )
    state = SimpleNamespace(last_completion_validation=same)
    assert _repeats_prior_completion_failure(state, same) is True


def test_changed_output_with_identical_effective_mutation_is_no_progress() -> None:
    before = CandidateValidationResult.from_findings(
        profile="implementation",
        findings=[_finding("candidate_black_failed")],
        candidate_identity="sha256:before",
    )
    after = CandidateValidationResult.from_findings(
        profile="implementation",
        findings=[_finding("candidate_black_failed")],
        candidate_identity="sha256:after",
    )
    assert (
        classify_completion_repair_progress(before, after).value
        == "NO_PROGRESS_OR_REGRESSION"
    )


def test_repair_with_no_file_delta_is_no_progress_input(tmp_path: Path) -> None:
    target = tmp_path / "app/formatting.py"
    target.parent.mkdir(parents=True)
    content = "value = 1\n"
    target.write_text(content, encoding="utf-8")
    before = target.read_bytes()
    result = _apply_completion_repair_ops_direct(
        [{"op": "write_file", "path": "app/formatting.py", "content": content}],
        tmp_path,
        repair_authorized_scope={"app/formatting.py"},
    )
    assert result["success"] is True
    assert target.read_bytes() == before
