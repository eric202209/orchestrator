"""Phase 32P-2 — gating completion validation receives change-set evidence (D2).

Phase 32O-4 scoped the placeholder rule to candidate-changed lines, resolving
the baseline from `completion_evidence["change_set"]`. The gating
`validate_task_completion` call in CompletionCoordinator did not forward that
evidence, so the gate still scanned whole files and could reject a candidate
for pre-existing baseline debt it never touched — consuming the completion
repair budget before the real failure was ever reached.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.services.orchestration.coordinators.completion_coordinator import (
    _build_gating_change_set,
)
from app.services.orchestration.validation.validator import ValidatorService

BASELINE_WITH_DEBT = (
    "def existing_helper():\n"
    "    # TODO: revisit this placeholder implementation later\n"
    "    return 1\n"
)


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, *args: Any, **_kwargs: Any) -> None:
        self.warnings.append(str(args))


class _RecordingTaskService:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def build_task_execution_change_set(
        self, project: Any, task: Any, **kwargs: Any
    ) -> Any:
        self.calls.append({"project": project, "task": task, **kwargs})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _RaisingTaskService(_RecordingTaskService):
    pass


def _obj(identifier: int = 177) -> SimpleNamespace:
    return SimpleNamespace(id=identifier)


def test_gating_change_set_is_built_from_existing_builder(tmp_path: Path) -> None:
    change_set = {"snapshot_path": "/snap", "added_files": ["a.py"]}
    task_service = _RecordingTaskService(change_set)

    result = _build_gating_change_set(
        task_service=task_service,
        project=_obj(12),
        task=_obj(177),
        task_execution_id=256,
        project_dir=tmp_path,
        preserve_project_root_rules=False,
        logger=_Logger(),
    )

    assert result == change_set
    assert len(task_service.calls) == 1
    call = task_service.calls[0]
    assert call["task_execution_id"] == 256
    assert call["target_dir"] == tmp_path
    assert call["preserve_project_root_rules"] is False
    assert "177" in call["snapshot_key"]


def test_gating_change_set_is_none_without_task_execution(tmp_path: Path) -> None:
    task_service = _RecordingTaskService({"snapshot_path": "/snap"})

    result = _build_gating_change_set(
        task_service=task_service,
        project=_obj(12),
        task=_obj(177),
        task_execution_id=None,
        project_dir=tmp_path,
        preserve_project_root_rules=True,
        logger=_Logger(),
    )

    assert result is None
    assert task_service.calls == []


def test_gating_change_set_is_none_without_project(tmp_path: Path) -> None:
    task_service = _RecordingTaskService({"snapshot_path": "/snap"})

    result = _build_gating_change_set(
        task_service=task_service,
        project=None,
        task=_obj(177),
        task_execution_id=256,
        project_dir=tmp_path,
        preserve_project_root_rules=True,
        logger=_Logger(),
    )

    assert result is None
    assert task_service.calls == []


def test_gating_change_set_failure_falls_back_to_none(tmp_path: Path) -> None:
    """A builder failure must not fail the gate; it degrades to whole-file mode."""

    task_service = _RaisingTaskService(RuntimeError("snapshot missing"))
    logger = _Logger()

    result = _build_gating_change_set(
        task_service=task_service,
        project=_obj(12),
        task=_obj(177),
        task_execution_id=256,
        project_dir=tmp_path,
        preserve_project_root_rules=True,
        logger=logger,
    )

    assert result is None
    assert logger.warnings


def test_gating_change_set_rejects_non_dict_builder_result(tmp_path: Path) -> None:
    task_service = _RecordingTaskService(["not", "a", "dict"])

    result = _build_gating_change_set(
        task_service=task_service,
        project=_obj(12),
        task=_obj(177),
        task_execution_id=256,
        project_dir=tmp_path,
        preserve_project_root_rules=True,
        logger=_Logger(),
    )

    assert result is None


def _validation_workspace(tmp_path: Path) -> tuple[Path, Path, list[dict[str, Any]]]:
    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "workspace"
    for root in (snapshot, workspace):
        (root / "app").mkdir(parents=True)
    (snapshot / "app" / "legacy.py").write_text(BASELINE_WITH_DEBT, encoding="utf-8")
    # The candidate edits legacy.py somewhere unrelated to the pre-existing TODO.
    (workspace / "app" / "legacy.py").write_text(
        BASELINE_WITH_DEBT + "\n\ndef candidate_addition():\n    return 2\n",
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


def test_forwarded_change_set_stops_pre_existing_debt_from_blocking(
    tmp_path: Path,
) -> None:
    snapshot, workspace, plan = _validation_workspace(tmp_path)
    change_set = {
        "snapshot_path": str(snapshot),
        "target_path": str(workspace),
        "added_files": [],
        "modified_files": ["app/legacy.py"],
        "deleted_files": [],
    }

    without_evidence = _validate(workspace, plan, None)
    with_evidence = _validate(workspace, plan, change_set)

    placeholder_reasons = [
        reason
        for reason in without_evidence.reasons
        if "placeholder" in reason.lower() or "TODO" in reason
    ]
    assert placeholder_reasons, "baseline debt should block without change-set evidence"
    assert not [
        reason
        for reason in with_evidence.reasons
        if "placeholder" in reason.lower() or "TODO" in reason
    ]


def test_candidate_introduced_placeholder_still_blocks_with_change_set(
    tmp_path: Path,
) -> None:
    """Phase 32O-4 delta semantics must be preserved, not weakened."""

    snapshot, workspace, plan = _validation_workspace(tmp_path)
    (workspace / "app" / "legacy.py").write_text(
        BASELINE_WITH_DEBT
        + "\n\ndef candidate_addition():\n"
        + "    # TODO: candidate left this placeholder behind\n"
        + "    return 2\n",
        encoding="utf-8",
    )
    change_set = {
        "snapshot_path": str(snapshot),
        "target_path": str(workspace),
        "added_files": [],
        "modified_files": ["app/legacy.py"],
        "deleted_files": [],
    }

    verdict = _validate(workspace, plan, change_set)

    assert [
        reason
        for reason in verdict.reasons
        if "placeholder" in reason.lower() or "TODO" in reason
    ]
