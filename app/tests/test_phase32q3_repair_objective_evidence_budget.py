"""Phase 32Q-3 — Candidate Repair objective integrity, evidence fidelity, budget.

Three defects made Candidate Repair provider qualification an unclean
model-fit experiment:

1. Objective integrity — a change set without a usable pre-candidate snapshot
   passed the `candidate_delta_unavailable` gate (which only checked that a
   dict was present), after which the whole-file placeholder scan reported
   pre-existing baseline debt as `placeholder_content` /
   `candidate_introduced` / `repairable=true`. That was the first repair
   reason shown to the provider in Phase 32Q-2V3A.

2. Evidence fidelity — the prompt preferred byte-exact `replace_in_file`
   anchors while `CURRENT FILE CONTENT` could be truncated, so the provider
   could be asked for an anchor it was never shown.

3. Budget ownership — the Candidate Repair generation budget was a
   provider-independent application constant instead of a deployment fact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.services.orchestration.phases.completion_repair_capsule import (
    MAX_SOURCE_CONTENT_PER_FILE_CHARS,
    _SOURCE_TRUNCATED_MARKER,
    CompletionRepairCapsule,
    _read_bounded_source_contents,
    build_bounded_completion_repair_prompt,
    build_completion_repair_capsule,
    source_visibility_label,
)
from app.services.orchestration.policy import (
    COMPLETION_REPAIR_TIMEOUT_DEFAULT_SECONDS,
    resolve_completion_repair_timeout_seconds,
)
from app.services.orchestration.validation.validator import ValidatorService

BASELINE_DEBT = (
    "def existing_helper():\n" "    # For now, return placeholder\n" "    return 1\n"
)


# ---------------------------------------------------------------------------
# Q3-A — repair objective integrity
# ---------------------------------------------------------------------------


def _fixture(tmp_path: Path) -> tuple[Path, Path, list[dict[str, Any]]]:
    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "workspace"
    for root in (snapshot, workspace):
        (root / "app").mkdir(parents=True)
    (snapshot / "app" / "legacy.py").write_text(BASELINE_DEBT, encoding="utf-8")
    (workspace / "app" / "legacy.py").write_text(
        BASELINE_DEBT + "\n\ndef candidate_addition():\n    return 2\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Extend app/legacy.py with candidate_addition",
            "expected_files": ["app/legacy.py"],
            "verification": "python -m compileall app/legacy.py",
        }
    ]
    return snapshot, workspace, plan


def _validate(workspace: Path, plan: list[dict[str, Any]], change_set: Any) -> Any:
    evidence: dict[str, Any] = {
        "summary_generated": True,
        "execution_results_count": 1,
        "reported_changed_files": ["app/legacy.py"],
    }
    if change_set is not None:
        evidence["change_set"] = change_set
    return ValidatorService.validate_task_completion(
        project_dir=workspace,
        plan=plan,
        task_prompt="Extend the legacy helper with candidate_addition",
        execution_profile="implementation",
        workspace_consistency={},
        title="Extend legacy helper",
        description="Extend legacy helper",
        relaxed_mode=False,
        completion_evidence=evidence,
        validation_severity="standard",
        workflow_stage="implementation",
        is_first_ordered_task=True,
    )


def _placeholder_findings(verdict: Any) -> list[Any]:
    return [
        finding
        for finding in verdict.findings
        if "todo or placeholder markers" in finding.message.lower()
    ]


def test_baseline_debt_is_not_a_repairable_candidate_objective(tmp_path: Path) -> None:
    """Required test 1: pre-existing debt is not candidate-introduced."""

    snapshot, workspace, plan = _fixture(tmp_path)
    verdict = _validate(
        workspace,
        plan,
        {
            "snapshot_path": str(snapshot),
            "target_path": str(workspace),
            "added_files": [],
            "modified_files": ["app/legacy.py"],
            "deleted_files": [],
        },
    )

    assert _placeholder_findings(verdict) == []
    assert "placeholder_content" not in (
        verdict.details.get("validator_rule_ids") or []
    )


def test_candidate_introduced_placeholder_still_blocks(tmp_path: Path) -> None:
    """Required test 2: a marker the candidate wrote is still repairable."""

    snapshot, workspace, plan = _fixture(tmp_path)
    (workspace / "app" / "legacy.py").write_text(
        BASELINE_DEBT
        + "\n\ndef candidate_addition():\n"
        + "    # TODO: candidate left this placeholder behind\n"
        + "    return 2\n",
        encoding="utf-8",
    )

    verdict = _validate(
        workspace,
        plan,
        {
            "snapshot_path": str(snapshot),
            "target_path": str(workspace),
            "added_files": [],
            "modified_files": ["app/legacy.py"],
            "deleted_files": [],
        },
    )

    findings = _placeholder_findings(verdict)
    assert findings, "candidate-introduced placeholder must still block"
    assert findings[0].attribution == "candidate_introduced"
    assert findings[0].repairable is True


def test_candidate_added_file_placeholder_remains_attributable(
    tmp_path: Path,
) -> None:
    """An added file has no baseline, but every line is candidate-authored."""

    snapshot, workspace, plan = _fixture(tmp_path)
    (workspace / "app" / "brand_new.py").write_text(
        "def brand_new():\n    # TODO: finish this\n    return None\n",
        encoding="utf-8",
    )
    plan[0]["expected_files"] = ["app/legacy.py", "app/brand_new.py"]

    verdict = _validate(
        workspace,
        plan,
        {
            "snapshot_path": str(snapshot),
            "target_path": str(workspace),
            "added_files": ["app/brand_new.py"],
            "modified_files": ["app/legacy.py"],
            "deleted_files": [],
        },
    )

    findings = _placeholder_findings(verdict)
    assert [finding.attribution for finding in findings] == ["candidate_introduced"]
    assert findings[0].repairable is True


@pytest.mark.parametrize(
    "snapshot_path",
    [None, "missing"],
    ids=["no_snapshot_path", "snapshot_root_absent"],
)
def test_missing_delta_evidence_fails_closed_without_pretending_attribution(
    tmp_path: Path, snapshot_path: str | None
) -> None:
    """Required test 4: no delta evidence, no claimed attribution, no repair."""

    _, workspace, plan = _fixture(tmp_path)
    change_set: dict[str, Any] = {
        "target_path": str(workspace),
        "added_files": [],
        "modified_files": ["app/legacy.py"],
        "deleted_files": [],
    }
    if snapshot_path == "missing":
        change_set["snapshot_path"] = str(tmp_path / "does-not-exist")

    verdict = _validate(workspace, plan, change_set)

    findings = _placeholder_findings(verdict)
    assert findings, "the reason must still fail the gate"
    assert findings[0].attribution == "unknown"
    assert findings[0].repairable is False
    assert findings[0].evidence == {"delta_evidence": "unavailable"}
    assert verdict.status == "rejected"
    assert not [
        finding
        for finding in verdict.repairable_findings
        if "placeholder" in finding.message.lower()
    ]
    assert verdict.details["unattributable_placeholder_reasons"]


def test_failure_irrelevant_authorized_file_is_not_promoted_by_baseline_debt(
    tmp_path: Path,
) -> None:
    """Required test 3: baseline debt alone must not create a repair objective."""

    snapshot, workspace, plan = _fixture(tmp_path)
    # A second candidate-authorized file that only carries pre-existing debt.
    (snapshot / "app" / "unrelated.py").write_text(BASELINE_DEBT, encoding="utf-8")
    (workspace / "app" / "unrelated.py").write_text(
        BASELINE_DEBT + "\n\ndef touched():\n    return 3\n", encoding="utf-8"
    )
    plan[0]["expected_files"] = ["app/legacy.py", "app/unrelated.py"]

    verdict = _validate(
        workspace,
        plan,
        {
            "snapshot_path": str(snapshot),
            "target_path": str(workspace),
            "added_files": [],
            "modified_files": ["app/legacy.py", "app/unrelated.py"],
            "deleted_files": [],
        },
    )

    assert not [
        finding
        for finding in verdict.repairable_findings
        if "unrelated.py" in finding.message
    ]


def test_placeholder_delta_baseline_reports_evidence_availability(
    tmp_path: Path,
) -> None:
    snapshot, workspace, _ = _fixture(tmp_path)
    candidate = workspace / "app" / "legacy.py"
    added = workspace / "app" / "added.py"
    added.write_text("x = 1\n", encoding="utf-8")

    usable = {
        "snapshot_path": str(snapshot),
        "added_files": ["app/added.py"],
        "modified_files": ["app/legacy.py"],
    }
    text, available = ValidatorService._placeholder_delta_baseline(
        candidate, workspace, usable
    )
    assert available is True
    assert text == BASELINE_DEBT

    # Candidate-added: no baseline exists, and the whole file is the delta.
    assert ValidatorService._placeholder_delta_baseline(added, workspace, usable) == (
        "",
        True,
    )

    # No change set and no snapshot root are both "evidence unavailable".
    assert ValidatorService._placeholder_delta_baseline(candidate, workspace, None) == (
        None,
        False,
    )
    assert ValidatorService._placeholder_delta_baseline(
        candidate, workspace, {"modified_files": ["app/legacy.py"]}
    ) == (None, False)


# ---------------------------------------------------------------------------
# Q3-B — repair evidence fidelity and operation executability
# ---------------------------------------------------------------------------


class _State:
    def __init__(self, project_dir: Path, expected: list[str]) -> None:
        self.project_dir = project_dir
        self.plan = [
            {"step_number": 1, "description": "step", "expected_files": expected}
        ]
        self.execution_results: list[Any] = []


class _Validation:
    def __init__(self, reasons: list[str], expected: list[str]) -> None:
        self.reasons = reasons
        self.details = {"expected_core_files": expected}


def _prompt_for(capsule: CompletionRepairCapsule) -> str:
    return build_bounded_completion_repair_prompt(capsule, 4)


def test_complete_and_truncated_source_are_labelled(tmp_path: Path) -> None:
    """Required test 6: truncation cannot be silent."""

    small = tmp_path / "app" / "small.py"
    large = tmp_path / "app" / "large.py"
    small.parent.mkdir(parents=True)
    small.write_text("def small():\n    return 1\n", encoding="utf-8")
    large.write_text(
        "# " + "L" * (MAX_SOURCE_CONTENT_PER_FILE_CHARS * 2), encoding="utf-8"
    )

    contents = _read_bounded_source_contents(tmp_path, ["app/small.py", "app/large.py"])
    capsule = CompletionRepairCapsule(
        validation_reasons=["pytest failed"],
        relevant_files=["app/small.py", "app/large.py"],
        last_step_summary="",
        workspace_path=str(tmp_path),
        task_prompt_excerpt="",
        source_file_contents=contents,
    )
    prompt = _prompt_for(capsule)

    assert "--- app/small.py --- [COMPLETE]" in prompt
    assert "--- app/large.py --- [TRUNCATED: only the first" in prompt
    assert _SOURCE_TRUNCATED_MARKER in prompt


def test_every_exact_anchor_file_is_fully_visible(tmp_path: Path) -> None:
    """Required test 5: an offered exact anchor is always constructible."""

    small = tmp_path / "app" / "small.py"
    large = tmp_path / "app" / "large.py"
    small.parent.mkdir(parents=True)
    small_text = "def small():\n    return 1\n"
    large_text = "# " + "L" * (MAX_SOURCE_CONTENT_PER_FILE_CHARS * 2)
    small.write_text(small_text, encoding="utf-8")
    large.write_text(large_text, encoding="utf-8")

    contents = _read_bounded_source_contents(tmp_path, ["app/small.py", "app/large.py"])
    for rel_path, content in contents.items():
        label = source_visibility_label(content)
        if label == "[COMPLETE]":
            assert content == (tmp_path / rel_path).read_text(encoding="utf-8")
        else:
            assert content.endswith(_SOURCE_TRUNCATED_MARKER)


def test_operation_rules_forbid_anchors_on_invisible_source(tmp_path: Path) -> None:
    capsule = CompletionRepairCapsule(
        validation_reasons=["pytest failed"],
        relevant_files=["app/a.py"],
        last_step_summary="",
        workspace_path=str(tmp_path),
        task_prompt_excerpt="",
        source_file_contents={"app/a.py": "x = 1\n"},
    )
    prompt = _prompt_for(capsule)

    assert "copy old character-for-character from a file marked [COMPLETE]" in prompt
    assert "Never emit replace_in_file or write_file for an existing file" in prompt
    assert (
        "Prefer replace_in_file for targeted in-place edits in files marked" in prompt
    )


def test_source_budget_prioritises_finding_implicated_files(tmp_path: Path) -> None:
    """Required test 6 (companion): truncation is finding-aware."""

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "bulky.py").write_text(
        "# " + "B" * (MAX_SOURCE_CONTENT_PER_FILE_CHARS * 2), encoding="utf-8"
    )
    (tmp_path / "app" / "failing.py").write_text(
        "def failing():\n    return 1\n", encoding="utf-8"
    )

    capsule = build_completion_repair_capsule(
        task_prompt="fix it",
        completion_validation=_Validation(
            ["Focused candidate pytest failed for app/failing.py"],
            ["app/bulky.py", "app/failing.py"],
        ),
        orchestration_state=_State(tmp_path, ["app/bulky.py", "app/failing.py"]),
    )

    assert capsule.relevant_files[0] == "app/failing.py"
    assert (
        source_visibility_label(capsule.source_file_contents["app/failing.py"])
        == "[COMPLETE]"
    )


# ---------------------------------------------------------------------------
# Q3-C — deployment-owned generation budget
# ---------------------------------------------------------------------------


def test_budget_resolves_from_deployment_configuration() -> None:
    """Required test 10: deterministic resolution from one authority."""

    deployment = Settings(COMPLETION_REPAIR_TIMEOUT_SECONDS=300)
    assert resolve_completion_repair_timeout_seconds(deployment) == 300


def test_deployment_profiles_may_bind_different_budgets() -> None:
    """Required test 11: profiles differ; Candidate Repair semantics do not."""

    resolved = {
        profile: resolve_completion_repair_timeout_seconds(
            Settings(RUNTIME_PROFILE=profile, COMPLETION_REPAIR_TIMEOUT_SECONDS=300)
        )
        for profile in ("standard", "medium", "low_resource", "compact_local")
    }

    assert resolved["standard"] == 300
    assert resolved["medium"] == 90
    assert resolved["low_resource"] == 60
    assert resolved["compact_local"] == 60


def test_unset_budget_uses_the_bounded_default() -> None:
    """Required test 12: an unset budget is bounded, never unbounded."""

    assert resolve_completion_repair_timeout_seconds(object()) == (
        COMPLETION_REPAIR_TIMEOUT_DEFAULT_SECONDS
    )
    assert Settings().COMPLETION_REPAIR_TIMEOUT_SECONDS == (
        COMPLETION_REPAIR_TIMEOUT_DEFAULT_SECONDS
    )


@pytest.mark.parametrize("value", [0, -1, 10_000])
def test_invalid_budget_configuration_fails_closed(value: int) -> None:
    """Required test 12: an invalid budget is a startup failure."""

    with pytest.raises(ValueError):
        Settings(COMPLETION_REPAIR_TIMEOUT_SECONDS=value)
