"""Phase 33C-1 publication trust-split regressions.

Publication used to answer two different questions with one filesystem-resolving
primitive: "is this candidate-declared path a valid declaration?" and "is this
pre-existing baseline entry product content?".  That conflation made an ordinary
hydrated toolchain symlink (`venv/bin/python3 -> /usr/bin/python3.12`) abort
publication preflight with `TaskWorkspaceViolationError` in Product Attempt 16.

These tests pin the three separated semantics simultaneously:

* trusted baseline inventory is classified/excluded before any target is followed;
* candidate declarations stay lexically fail-closed;
* promotion write destinations still fail closed on a symlink segment.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_guard import (
    TaskWorkspaceViolationError,
)
from app.services.workspace.baseline_promotion_service import BaselinePromotionService


def _preflight(baseline: Path, change_set=None, baseline_file_count=1):
    verdict = ValidatorService.validate_baseline_publish(
        validation_profile="implementation",
        baseline_path=str(baseline),
        baseline_file_count=baseline_file_count,
        missing_task_expected_files=[],
        missing_prior_expected_files=[],
        candidate_change_set=change_set if change_set is not None else {},
    )
    return verdict, verdict.details["preflight_candidate_projection"]


def _baseline_with_product_file(tmp_path: Path) -> Path:
    baseline = tmp_path / "baseline"
    (baseline / "app").mkdir(parents=True)
    (baseline / "app" / "real.py").write_text("print('real')\n", encoding="utf-8")
    return baseline


def _promoter(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    promoter = BaselinePromotionService(None)
    promoter.get_project_root = lambda _project: root
    promoter.get_project_baseline_dir = lambda _project: baseline
    return promoter, root, baseline, SimpleNamespace(id=1), SimpleNamespace(id=2)


# --- Scenario 1: Attempt-16 trusted toolchain symlink ------------------------


def test_trusted_toolchain_symlink_does_not_abort_publication_preflight(tmp_path):
    """The reproduced Attempt-16 blocker: an outward venv symlink must not raise."""

    external = tmp_path / "external-toolchain"
    external.mkdir()
    interpreter = external / "python3.12"
    interpreter.write_text("#!/bin/false\n", encoding="utf-8")

    baseline = _baseline_with_product_file(tmp_path)
    (baseline / "venv" / "bin").mkdir(parents=True)
    (baseline / "venv" / "bin" / "python3").symlink_to(interpreter)

    verdict, projection = _preflight(
        baseline, {"modified_files": ["app/real.py"]}, baseline_file_count=1
    )

    assert verdict.status == "accepted"
    assert projection["canonical_paths"] == ["app/real.py"]
    assert "venv/bin/python3" in projection["orchestration_internal_paths"]
    assert projection["projected_paths"] == ["app/real.py"]
    # The symlink is still a symlink: classification never followed it.
    assert (baseline / "venv" / "bin" / "python3").is_symlink()
    assert interpreter.read_text(encoding="utf-8") == "#!/bin/false\n"


def test_broken_trusted_toolchain_symlink_does_not_abort_preflight(tmp_path):
    """A dangling excluded symlink has no resolvable target at all."""

    baseline = _baseline_with_product_file(tmp_path)
    (baseline / "venv" / "bin").mkdir(parents=True)
    (baseline / "venv" / "bin" / "python3").symlink_to(
        tmp_path / "never-created" / "python3.12"
    )

    verdict, projection = _preflight(baseline, {"modified_files": ["app/real.py"]})

    assert verdict.status == "accepted"
    assert projection["canonical_paths"] == ["app/real.py"]
    assert "venv/bin/python3" in projection["orchestration_internal_paths"]


# --- Scenario 2: malicious product symlink -----------------------------------


def test_promotion_refuses_to_write_through_a_symlinked_directory_segment(tmp_path):
    promoter, root, baseline, project, task = _promoter(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "passwd"
    victim.write_text("original-secret\n", encoding="utf-8")

    artifact = root / ".agent" / "change-sets" / "7" / "files" / "some"
    artifact.mkdir(parents=True)
    (artifact / "passwd").write_text("attacker-content\n", encoding="utf-8")
    (baseline / "some").symlink_to(outside, target_is_directory=True)

    result = promoter.promote_change_set_into_baseline_unlocked(
        project,
        task,
        {
            "task_execution_id": 7,
            "added_files": ["some/passwd"],
            "modified_files": [],
            "deleted_files": [],
        },
    )

    assert result["files_copied"] == 0
    assert victim.read_text(encoding="utf-8") == "original-secret\n"


def test_promotion_refuses_to_write_through_a_symlinked_file_destination(tmp_path):
    promoter, root, baseline, project, task = _promoter(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "secret.txt"
    victim.write_text("original-secret\n", encoding="utf-8")

    artifact = root / ".agent" / "change-sets" / "8" / "files" / "app"
    artifact.mkdir(parents=True)
    (artifact / "real.py").write_text("attacker-content\n", encoding="utf-8")
    (baseline / "app").mkdir()
    (baseline / "app" / "real.py").symlink_to(victim)

    result = promoter.promote_change_set_into_baseline_unlocked(
        project,
        task,
        {
            "task_execution_id": 8,
            "added_files": [],
            "modified_files": ["app/real.py"],
            "deleted_files": [],
        },
    )

    assert result["files_copied"] == 0
    assert victim.read_text(encoding="utf-8") == "original-secret\n"


# --- Scenario 3: ordinary product content ------------------------------------


def test_ordinary_modified_and_new_product_paths_survive_preflight(tmp_path):
    baseline = _baseline_with_product_file(tmp_path)

    verdict, projection = _preflight(
        baseline,
        {"added_files": ["app/new_module.py"], "modified_files": ["app/real.py"]},
        baseline_file_count=1,
    )

    assert verdict.status == "accepted"
    assert projection["added_paths"] == ["app/new_module.py"]
    assert projection["modified_paths"] == ["app/real.py"]
    assert projection["projected_paths"] == ["app/new_module.py", "app/real.py"]


def test_candidate_declaration_does_not_require_filesystem_existence(tmp_path):
    """A declared added path is valid before it exists anywhere on disk."""

    baseline = _baseline_with_product_file(tmp_path)

    _, projection = _preflight(baseline, {"added_files": ["app/not_on_disk_yet.py"]})

    assert projection["added_paths"] == ["app/not_on_disk_yet.py"]
    assert not (baseline / "app" / "not_on_disk_yet.py").exists()


def test_promotion_copies_ordinary_modified_and_new_product_files(tmp_path):
    promoter, root, baseline, project, task = _promoter(tmp_path)
    artifact = root / ".agent" / "change-sets" / "9" / "files" / "app"
    artifact.mkdir(parents=True)
    (artifact / "real.py").write_text("updated\n", encoding="utf-8")
    (artifact / "new_module.py").write_text("created\n", encoding="utf-8")
    (baseline / "app").mkdir()
    (baseline / "app" / "real.py").write_text("original\n", encoding="utf-8")

    result = promoter.promote_change_set_into_baseline_unlocked(
        project,
        task,
        {
            "task_execution_id": 9,
            "added_files": ["app/new_module.py"],
            "modified_files": ["app/real.py"],
            "deleted_files": [],
        },
    )

    assert result["files_copied"] == 2
    assert (baseline / "app" / "real.py").read_text(encoding="utf-8") == "updated\n"
    assert (baseline / "app" / "new_module.py").read_text(
        encoding="utf-8"
    ) == "created\n"


# --- Scenario 4: exclusions --------------------------------------------------


@pytest.mark.parametrize(
    "excluded_relative_path",
    [
        "venv/lib/site.py",
        "node_modules/left-pad/index.js",
        ".agent/change-sets/1/manifest.json",
    ],
)
def test_existing_ownership_exclusions_remain_excluded(
    tmp_path, excluded_relative_path
):
    baseline = _baseline_with_product_file(tmp_path)
    excluded = baseline / excluded_relative_path
    excluded.parent.mkdir(parents=True, exist_ok=True)
    excluded.write_text("not product content\n", encoding="utf-8")

    _, projection = _preflight(baseline, {"modified_files": ["app/real.py"]})

    assert projection["canonical_paths"] == ["app/real.py"]
    assert excluded_relative_path in projection["orchestration_internal_paths"]


def test_excluded_candidate_declarations_are_dropped_not_published(tmp_path):
    baseline = _baseline_with_product_file(tmp_path)

    _, projection = _preflight(
        baseline,
        {"added_files": ["venv/bin/python3", "node_modules/x/index.js", "app/new.py"]},
    )

    assert projection["added_paths"] == ["app/new.py"]


def test_colon_in_a_relative_filename_is_not_a_drive_letter(tmp_path):
    """`a:b/file.txt` is a legal POSIX product path, not a Windows declaration."""

    baseline = _baseline_with_product_file(tmp_path)

    _, projection = _preflight(baseline, {"added_files": ["a:b/file.txt"]})

    assert projection["added_paths"] == ["a:b/file.txt"]


# --- Scenario 5: traversal / absolute declarations ---------------------------


@pytest.mark.parametrize(
    "unsafe_declaration",
    [
        "../outside",
        "app/../../outside",
        "/absolute/path",
        "~/home/path",
        "C:\\windows\\path",
        "",
        "   ",
    ],
)
def test_unsafe_candidate_declarations_still_fail_closed(tmp_path, unsafe_declaration):
    baseline = _baseline_with_product_file(tmp_path)

    with pytest.raises(TaskWorkspaceViolationError):
        _preflight(baseline, {"added_files": [unsafe_declaration]})


def test_unsafe_declaration_in_any_change_set_list_fails_closed(tmp_path):
    baseline = _baseline_with_product_file(tmp_path)

    for key in ("modified_files", "deleted_files"):
        with pytest.raises(TaskWorkspaceViolationError):
            _preflight(baseline, {key: ["../outside"]})


def test_promotion_still_refuses_traversal_and_absolute_declarations(tmp_path):
    promoter, root, baseline, project, task = _promoter(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("original-secret\n", encoding="utf-8")
    (root / ".agent" / "change-sets" / "10" / "files").mkdir(parents=True)

    result = promoter.promote_change_set_into_baseline_unlocked(
        project,
        task,
        {
            "task_execution_id": 10,
            "added_files": ["../outside/passwd", str(outside / "passwd")],
            "modified_files": [],
            "deleted_files": [],
        },
    )

    assert result["files_copied"] == 0
    assert (outside / "passwd").read_text(encoding="utf-8") == "original-secret\n"
