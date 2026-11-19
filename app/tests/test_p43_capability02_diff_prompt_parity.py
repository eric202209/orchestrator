"""P43-CAPABILITY-02 diff-scoped Debug Repair prompt parity controls.

The fixture is the P43-PRODUCT-07-R2 (TaskExecution 406) workspace after the
accepted Plan's steps 1-2 plus the retained failure envelope. The rendered
prompt is a reconstruction through the production builders; it is not claimed
byte-identical to the provider request.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.orchestration.diagnostics.debug_feedback import (
    DebugFeedbackEnvelope,
    bounded_debug_repair_ops_authority_rejection,
    build_bounded_debug_repair_changed_file_context,
    build_bounded_debug_repair_prompt_with_metadata,
    build_debug_source_contract,
    parse_bounded_debug_repair_output_strict,
)
from app.services.orchestration.diagnostics.diff_capsule import (
    build_bounded_diff_repair_prompt,
    build_diff_capsule,
)
from app.services.orchestration.diagnostics.evidence_capsule import (
    collect_workspace_evidence,
)
from app.services.orchestration.phases.execution_debug_repair import (
    bounded_debug_repair_command_authority_rejection,
    derive_bounded_debug_repair_generated_test_paths,
)
from app.services.orchestration.phases.execution_local_steps import (
    local_step_commands_are_executable,
)
from app.services.orchestration.prompt_templates import StepResult
from app.tests.test_p43_product06_generated_test_repair_authority import (
    PLAN as P06_PLAN,
    _authority as p06_authority,
    _state as p06_state,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
)
from types import SimpleNamespace

FIXTURE = (
    Path(__file__).parent / "fixtures" / "p43_capability02_r2_debug_repair_shape.json"
)
GENERATED = "tests/test_pagination_regression.py"
ACCEPTANCE = "tests/test_pagekit_acceptance.py"
EXISTING = "tests/test_paging.py"
SOURCE = "pagekit/paging.py"
DO_NOT_EDIT_TESTS = "- Do not edit tests or verifier commands."
PRODUCT06_WORDING = (
    "- Existing baseline tests and protected acceptance files are immutable; "
    "only the exact authorized generated-test paths below may be repaired."
)


def _fixture() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for path, content in data["files"].items():
        assert (
            hashlib.sha256(content.encode("utf-8")).hexdigest() == data["sha256"][path]
        )
    return data


def _workspace(tmp_path: Path, data: dict) -> Path:
    root = tmp_path / "406"
    for path, content in data["files"].items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _envelope(data: dict, workspace: Path) -> DebugFeedbackEnvelope:
    return DebugFeedbackEnvelope(**data["envelope"], workspace_path=str(workspace))


def _r2_prompt(tmp_path: Path, *, generated=(GENERATED,), **kwargs):
    data = _fixture()
    workspace = _workspace(tmp_path, data)
    envelope = _envelope(data, workspace)
    evidence = collect_workspace_evidence(
        envelope.failure_class,
        str(workspace),
        failure_context=envelope.stderr_excerpt,
    )
    capsule = build_diff_capsule(
        pre_checksum={},
        project_dir=workspace,
        changed_files=envelope.changed_files,
        envelope=envelope,
    )
    context, metadata = build_bounded_debug_repair_changed_file_context(
        envelope,
        prior_source_paths=[SOURCE],
        allowed_source_paths=[SOURCE],
        allowed_generated_test_paths=generated,
    )
    targets = [
        path
        for path in metadata[
            "bounded_execution_debug_repair_changed_file_context_paths"
        ]
        if path == SOURCE or path in generated
    ]
    prompt = build_bounded_diff_repair_prompt(
        capsule,
        evidence,
        envelope=envelope,
        source_context=context,
        repair_target_paths=targets,
        complete_source_paths=[],
        authorized_generated_test_paths=generated,
        **kwargs,
    )
    return prompt, data, evidence, capsule, metadata, envelope


def _contract_section(prompt: str) -> str:
    start = prompt.index("Debug source contract:")
    end = prompt.index("Repair target paths from the accepted plan", start)
    return prompt[start:end]


def test_reconstruction_matches_retained_r2_manifest(tmp_path):
    _prompt, data, evidence, capsule, metadata, _envelope_ = _r2_prompt(tmp_path)
    metrics = data["retained_metrics"]
    assert evidence.total_chars == metrics["evidence_chars_total"]
    assert capsule.diff_line_count == metrics["diff_capsule_line_count"]
    assert (
        metadata["bounded_execution_debug_repair_changed_file_context_chars"]
        == metrics["changed_file_context_chars"]
    )
    assert capsule.primary_file == GENERATED


def test_diff_prompt_grounds_source_target_in_accepted_prior_source(tmp_path):
    prompt, *_ = _r2_prompt(tmp_path, prior_source_paths=[SOURCE])
    contract = _contract_section(prompt)
    assert f"  - {SOURCE}" in contract
    assert "  - pagekit/__init__.py" not in contract


def test_diff_prompt_has_no_contradictory_test_immutability_with_authority(tmp_path):
    prompt, *_ = _r2_prompt(tmp_path)
    contract = _contract_section(prompt)
    assert DO_NOT_EDIT_TESTS not in prompt
    assert "- Existing tests are the failing contract." not in contract
    assert PRODUCT06_WORDING in contract
    assert "may itself be incorrect" in contract
    # Positive-control visibility of the facts needed for a correct repair.
    assert (
        "def paginate(items: Sequence[Any], number: int, per_page: int) -> Page:"
        in prompt
    )
    assert "page = paginate(items, 3, 4)" in prompt
    assert "Authorized generated-test paths" in prompt
    assert f"- {GENERATED}\n" in prompt
    assert "Existing baseline tests, protected acceptance files" in prompt


def _write_generated_first_workspace(root: Path) -> None:
    (root / "pagekit").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pagekit" / "__init__.py").write_text(
        "from pagekit.paging import paginate\n", encoding="utf-8"
    )
    (root / "pagekit" / "paging.py").write_text(
        "def paginate(items, number, per_page):\n"
        "    start = (number - 1) * per_page\n"
        "    return list(items[start : start + per_page])\n",
        encoding="utf-8",
    )
    # Sorts before the trusted test, so its assertions would fill the slots.
    (root / "tests" / "test_a_generated_regression.py").write_text(
        "from pagekit import paginate\n\n\n"
        "def test_generated():\n"
        "    assert paginate([1, 2, 3], 2, 2) == [7, 7]\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_trusted.py").write_text(
        "from pagekit import paginate\n\n\n"
        "def test_trusted():\n"
        "    assert paginate([1, 2, 3], 1, 2) == [1, 2]\n",
        encoding="utf-8",
    )


def _generated_first_envelope(root: Path) -> DebugFeedbackEnvelope:
    failure = (
        "tests/test_a_generated_regression.py:6: AssertionError\n"
        "E       assert [3] == [7, 7]\n"
    )
    return DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=2,
        failure_phase="execution",
        failed_command="python3 -m pytest tests/test_a_generated_regression.py -q",
        stderr_excerpt=failure,
        pytest_excerpt=failure,
        changed_files=["tests/test_a_generated_regression.py"],
        expected_files=["tests/test_a_generated_regression.py"],
        workspace_path=str(root),
        failure_class="pytest_failure",
    )


def test_generated_test_assertions_are_not_expected_behavior(tmp_path):
    root = tmp_path / "ws"
    _write_generated_first_workspace(root)
    envelope = _generated_first_envelope(root)
    generated = ("tests/test_a_generated_regression.py",)

    capsule = build_diff_capsule(
        pre_checksum={},
        project_dir=root,
        changed_files=envelope.changed_files,
        envelope=envelope,
    )
    prompt = build_bounded_diff_repair_prompt(
        capsule,
        envelope=envelope,
        repair_target_paths=["pagekit/paging.py", *generated],
        authorized_generated_test_paths=generated,
    )
    contract = _contract_section(prompt)
    assert "should equal [7, 7]" not in contract
    assert "should equal [1, 2]" in contract


def test_source_contract_excludes_generated_test_assertions(tmp_path):
    root = tmp_path / "ws"
    _write_generated_first_workspace(root)
    contract = build_debug_source_contract(
        _generated_first_envelope(root),
        authorized_generated_test_paths=("tests/test_a_generated_regression.py",),
    )
    behavior = contract.split("Expected behavior", 1)[1]
    assert "[7, 7]" not in behavior
    assert "should equal [1, 2]" in behavior


def test_without_generated_authority_wording_and_behavior_are_unchanged(tmp_path):
    root = tmp_path / "ws"
    _write_generated_first_workspace(root)
    envelope = _generated_first_envelope(root)
    contract = build_debug_source_contract(envelope)
    assert "- Existing tests are the failing contract." in contract
    assert DO_NOT_EDIT_TESTS in contract
    assert PRODUCT06_WORDING not in contract
    assert "may itself be incorrect" not in contract
    # Pre-existing behavior: every test file contributes expected behavior.
    assert "should equal [7, 7]" in contract

    prompt, *_ = _r2_prompt(tmp_path, generated=())
    assert DO_NOT_EDIT_TESTS in prompt
    assert "6. Do not modify tests or rewrite unrelated files." in prompt
    assert "Authorized generated-test paths" not in prompt


def test_bounded_prompt_keeps_product06_wording(tmp_path):
    data = _fixture()
    workspace = _workspace(tmp_path, data)
    envelope = _envelope(data, workspace)
    with_authority = build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        prior_source_paths=[SOURCE],
        authorized_generated_test_paths=(GENERATED,),
    ).prompt
    without_authority = build_bounded_debug_repair_prompt_with_metadata(
        envelope, prior_source_paths=[SOURCE]
    ).prompt
    assert PRODUCT06_WORDING in with_authority
    assert DO_NOT_EDIT_TESTS not in with_authority
    assert "Authorized generated-test repair contract:" in with_authority
    assert DO_NOT_EDIT_TESTS in without_authority
    assert "Authorized generated-test repair contract:" not in without_authority


def _repair_output(path: str, command: str) -> str:
    return json.dumps(
        [
            {
                "title": "Correct the generated regression call",
                "repair_type": "ops_fix",
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": path,
                        "old": "    page = paginate(items, 3, 4)",
                        "new": "    page = paginate(items, 4, 3)",
                    }
                ],
                "verification_command": command,
            }
        ]
    )


def test_one_line_generated_test_fix_admissible_and_r2_command_rejected(tmp_path):
    data = _fixture()
    workspace = _workspace(tmp_path, data)
    envelope = _envelope(data, workspace)
    accepted_step = data["accepted_plan"][1]
    accepted_command = accepted_step["verification"]

    def admitted(command: str) -> bool:
        return local_step_commands_are_executable(
            project_dir=workspace, commands=[command], verification_command=command
        )

    valid = parse_bounded_debug_repair_output_strict(
        _repair_output(GENERATED, accepted_command),
        envelope=envelope,
        source_edit_context=False,
        authorized_generated_test_paths=(GENERATED,),
    )
    assert valid.payload is not None
    assert (
        bounded_debug_repair_command_authority_rejection(
            valid.payload, accepted_step, command_is_admitted=admitted
        )
        is None
    )

    r2_command = data["r2_debug_repair_command"]
    r2_like = parse_bounded_debug_repair_output_strict(
        _repair_output(GENERATED, r2_command),
        envelope=envelope,
        source_edit_context=False,
        authorized_generated_test_paths=(GENERATED,),
    )
    rejection = bounded_debug_repair_command_authority_rejection(
        r2_like.payload, accepted_step, command_is_admitted=admitted
    )
    assert rejection == f"debug_repair_command_not_authorized: {r2_command}"


@pytest.mark.parametrize(
    ("path", "projection"),
    [
        (GENERATED, ()),  # no generated-test authority
        (ACCEPTANCE, (GENERATED,)),  # protected acceptance
        (EXISTING, (GENERATED,)),  # existing baseline test
        ("tests/test_other_generated.py", (GENERATED,)),  # unrelated path
    ],
)
def test_test_paths_outside_the_projection_stay_unauthorized(
    tmp_path, path, projection
):
    data = _fixture()
    workspace = _workspace(tmp_path, data)
    result = parse_bounded_debug_repair_output_strict(
        _repair_output(path, data["accepted_plan"][1]["verification"]),
        envelope=_envelope(data, workspace),
        source_edit_context=False,
        authorized_generated_test_paths=projection,
    )
    assert result.payload is None
    assert result.rejection_reason == "unauthorized_source_path"


def test_legacy_or_cross_execution_provenance_creates_no_projection(tmp_path):
    (tmp_path / "pagekit").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pagekit" / "paging.py").write_text(
        "def paginate(items):\n    return list(items)\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_pagination_regression.py").write_text(
        "def test_page():\n    assert [8, 9] == [9]\n", encoding="utf-8"
    )
    authority = p06_authority(tmp_path)
    task = SimpleNamespace(workspace_status="isolated", promoted_at=None)
    current = p06_state(tmp_path).execution_results[0]
    legacy = StepResult(
        step_number=1,
        status="failed",
        files_changed=list(current.files_changed),
    )
    for result, execution_id in ((legacy, 404), (current, 405)):
        state = p06_state(tmp_path)
        state.execution_results = [result]
        assert (
            derive_bounded_debug_repair_generated_test_paths(
                accepted_path_authority=authority,
                accepted_plan=P06_PLAN,
                orchestration_state=state,
                current_step_result=result,
                task_execution_id=execution_id,
                task_execution_status="running",
                project_dir=tmp_path,
                task=task,
            )
            == ()
        )
    # Ops authority still requires a grant even when a projection names a path.
    assert (
        bounded_debug_repair_ops_authority_rejection(
            [
                {
                    "op": "replace_in_file",
                    "path": "tests/test_unknown.py",
                    "old": "a",
                    "new": "b",
                }
            ],
            authority=authority,
            project_dir=tmp_path,
            generated_test_paths=("tests/test_unknown.py",),
        )
        == "unauthorized_source_path"
    )
    assert accepted_plan_identity(P06_PLAN)


def test_execution_loop_passes_prior_source_paths_to_diff_prompt(
    db_session, tmp_path, monkeypatch
):
    from app.services.orchestration.phases import execution_loop
    from app.tests.test_p43_product07_t1_debug_repair_routing import (
        test_diff_scoped_debug_repair_uses_role_owned_runtime_first as t1_scenario,
    )

    helper_results = []
    builder_kwargs = []
    real_helper = execution_loop._bounded_debug_repair_prior_source_paths
    real_builder = execution_loop.build_bounded_diff_repair_prompt

    def spy_helper(*args, **kwargs):
        result = real_helper(*args, **kwargs)
        helper_results.append(result)
        return result

    def spy_builder(*args, **kwargs):
        builder_kwargs.append(kwargs)
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(
        execution_loop, "_bounded_debug_repair_prior_source_paths", spy_helper
    )
    monkeypatch.setattr(execution_loop, "build_bounded_diff_repair_prompt", spy_builder)

    t1_scenario(db_session, tmp_path, monkeypatch)

    assert len(builder_kwargs) == 1
    assert "prior_source_paths" in builder_kwargs[0]
    assert builder_kwargs[0]["prior_source_paths"] == helper_results[0]
