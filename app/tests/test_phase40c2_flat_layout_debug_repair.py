"""Phase 40C-2 Correction B: flat-layout debug-repair source recognition.

Bounded debug repair recognized source only under ``src/``.  An ``ops_fix``
against a flat-layout package (``configlib/loader.py``) was therefore never a
source edit and fell through to a misleading ``missing_command`` rejection, and
the repair prompt never showed the flat-layout file's current content.

Recognition now follows the canonical path rules; authorization (the accepted
plan's path authority), provenance, and verification stay independent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.diagnostics.debug_feedback import (
    DebugFeedbackEnvelope,
    _ops_touch_source_files,
    bounded_debug_repair_ops_authority_rejection,
    build_bounded_debug_repair_changed_file_context,
    debug_repair_source_path,
    normalize_bounded_debug_repair_payload_detailed,
)
from app.services.orchestration.phases.execution_debug_repair import (
    _bounded_debug_repair_prior_source_paths,
    _bounded_debug_repair_source_edit_context,
)
from app.tests.phase33c4_test_helpers import executor_test_authority

LOADER = "def load_config(text, schema):\n    return {}\n"

# Phase 40C-1 Workflow B, Task 6/10: the model's actual repair shape.
WORKFLOW_B_REPAIR = {
    "repair_type": "ops_fix",
    "ops": [
        {
            "op": "write_file",
            "path": "configlib/__init__.py",
            "content": (
                "import os\n\ndef load_config(text, schema, env=None, prefix=''):\n"
                "    return {k: v(text.get(k)) for k, v in schema.items()}\n"
            ),
        }
    ],
    "verification_command": "python -m pytest -q tests/test_env_overrides.py",
}


@pytest.fixture
def flat_repo(tmp_path: Path) -> Path:
    (tmp_path / "configlib").mkdir()
    (tmp_path / "configlib" / "__init__.py").write_text(
        "from .loader import load_config\n", encoding="utf-8"
    )
    (tmp_path / "configlib" / "loader.py").write_text(LOADER, encoding="utf-8")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_loader.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    return tmp_path


def _normalize(item: dict, *, source_edit_context: bool = False):
    return normalize_bounded_debug_repair_payload_detailed(
        [item], source_edit_context=source_edit_context
    )


def _replace_fix(path: str, verification: str | None = "python -m pytest -q"):
    item = {
        "repair_type": "ops_fix",
        "ops": [
            {
                "op": "replace_in_file",
                "path": path,
                "old": "return {}",
                "new": "return dict(schema)",
            }
        ],
    }
    if verification is not None:
        item["verification_command"] = verification
    return item


@pytest.mark.parametrize("path", ["configlib/loader.py", "src/pkg/mod.py"])
def test_flat_and_src_layout_source_repairs_are_recognized(flat_repo, path):
    result = _normalize(_replace_fix(path))

    assert result.rejection_reason is None
    assert result.payload["fix_type"] == "ops_fix"
    authority = executor_test_authority(flat_repo, result.payload["ops"])
    assert (
        bounded_debug_repair_ops_authority_rejection(
            result.payload["ops"], authority=authority, project_dir=flat_repo
        )
        is None
    )


def test_flat_layout_repair_without_verification_is_missing_verification():
    result = _normalize(_replace_fix("configlib/loader.py", verification=None))
    # Previously ``missing_command``: the edit was not recognized as source.
    assert result.rejection_reason == "missing_verification_command"


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_loader.py",
        "configlib/tests/test_inner.py",
        "configlib/test_loader.py",
        "conftest.py",
        "../configlib/loader.py",
        "configlib/../tests/test_loader.py",
        "/etc/passwd",
        "~/loader.py",
        ".git/config",
        ".agent/state.json",
        ".orchestrator/plan.json",
        "venv/lib/site.py",
        "node_modules/pkg/index.js",
    ],
)
def test_test_traversal_and_protected_paths_are_unauthorized(path):
    result = _normalize(_replace_fix(path), source_edit_context=True)
    assert result.rejection_reason == "unauthorized_source_path"
    assert result.payload is None


def test_lstrip_traversal_no_longer_reads_as_src_source():
    # ``"../src/x.py".lstrip("./")`` used to yield ``src/x.py``.
    assert debug_repair_source_path("../src/x.py") == ""
    assert not _ops_touch_source_files(
        [{"op": "write_file", "path": "../src/x.py", "content": "x"}]
    )
    assert debug_repair_source_path("./configlib/loader.py") == "configlib/loader.py"


def test_not_every_python_file_is_source():
    assert debug_repair_source_path("build/generated.py") == ""
    assert debug_repair_source_path("tests/helpers.py") == ""
    assert debug_repair_source_path("README.md") == ""


def test_symlink_escape_is_unauthorized(flat_repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    os.symlink(outside, flat_repo / "configlib" / "linked.py")

    assert debug_repair_source_path("configlib/linked.py", flat_repo) == ""
    ops = _replace_fix("configlib/linked.py")["ops"]
    authority = executor_test_authority(flat_repo, ops)
    assert (
        bounded_debug_repair_ops_authority_rejection(
            ops, authority=authority, project_dir=flat_repo
        )
        == "unauthorized_source_path"
    )
    assert outside.read_text(encoding="utf-8") == "SECRET = 1\n"


def test_source_outside_accepted_plan_authority_is_unauthorized(flat_repo):
    ops = _replace_fix("src/pkg/mod.py")["ops"]
    authority = executor_test_authority(
        flat_repo, [], existing_paths=["configlib/loader.py"]
    )
    assert (
        bounded_debug_repair_ops_authority_rejection(
            ops, authority=authority, project_dir=flat_repo
        )
        == "unauthorized_source_path"
    )


def test_workflow_b_repair_stays_rejected_for_its_actual_defects(flat_repo):
    result = _normalize(WORKFLOW_B_REPAIR)

    # Recognized as a source edit, so no misleading ``missing_command``.
    assert result.rejection_reason is None
    assert result.payload["fix_type"] == "ops_fix"
    ops = result.payload["ops"]

    # The accepted plan granted only configlib/loader.py.
    loader_only = executor_test_authority(
        flat_repo, [], existing_paths=["configlib/loader.py"]
    )
    assert (
        bounded_debug_repair_ops_authority_rejection(
            ops, authority=loader_only, project_dir=flat_repo
        )
        == "unauthorized_source_path"
    )
    # Even with a grant, a whole-file rewrite of an existing file whose content
    # the repair prompt never showed lacks provenance.
    granted = executor_test_authority(
        flat_repo, [], existing_paths=["configlib/__init__.py"]
    )
    assert (
        bounded_debug_repair_ops_authority_rejection(
            ops, authority=granted, project_dir=flat_repo
        )
        == "missing_source_provenance"
    )
    assert (
        bounded_debug_repair_ops_authority_rejection(
            ops,
            authority=granted,
            project_dir=flat_repo,
            complete_source_paths=["configlib/__init__.py"],
        )
        is None
    )


def test_unsupported_repair_operation_and_format_keep_existing_categories():
    unsupported = _normalize(
        {
            "repair_type": "ops_fix",
            "ops": [{"op": "chmod", "path": "configlib/loader.py"}],
            "verification_command": "python -m pytest -q",
        }
    )
    assert unsupported.rejection_reason == "invalid_ops_fix_ops"
    malformed = normalize_bounded_debug_repair_payload_detailed(
        {"repair_type": "ops_fix"}
    )
    assert malformed.rejection_reason == "unsupported_shape"


def _envelope(project_dir: Path, **fields) -> DebugFeedbackEnvelope:
    base = dict(
        task_execution_id=1,
        task_id=1,
        step_index=3,
        failure_phase="execution",
        failure_class="pytest_failure",
        failed_command="python -m pytest -q tests/test_env_overrides.py",
        return_code=1,
        changed_files=[],
        expected_files=[],
        workspace_path=str(project_dir),
    )
    base.update(fields)
    return DebugFeedbackEnvelope(**base)


def test_flat_layout_step_is_a_source_edit_context(flat_repo):
    step = {"ops": [{"op": "replace_in_file", "path": "configlib/loader.py"}]}
    assert _bounded_debug_repair_source_edit_context(step, None)
    test_only = {"ops": [{"op": "replace_in_file", "path": "tests/test_loader.py"}]}
    assert not _bounded_debug_repair_source_edit_context(test_only, None)


def test_repair_prompt_shows_flat_layout_source_content(flat_repo):
    state = SimpleNamespace(
        execution_results=[
            SimpleNamespace(step_number=1, files_changed=["configlib/loader.py"])
        ],
        plan=[],
    )
    prior = _bounded_debug_repair_prior_source_paths(state, 2)
    assert prior == ["configlib/loader.py"]

    rendered, metadata = build_bounded_debug_repair_changed_file_context(
        _envelope(flat_repo), prior_source_paths=prior
    )
    assert "--- configlib/loader.py" in rendered
    assert "def load_config" in rendered
    assert metadata[
        "bounded_execution_debug_repair_changed_file_context_complete_paths"
    ] == ["configlib/loader.py"]


def test_truncated_file_is_not_complete_provenance(flat_repo):
    (flat_repo / "configlib" / "loader.py").write_text(
        LOADER + "# pad\n" * 400, encoding="utf-8"
    )
    _, metadata = build_bounded_debug_repair_changed_file_context(
        _envelope(flat_repo), prior_source_paths=["configlib/loader.py"]
    )
    assert metadata["bounded_execution_debug_repair_changed_file_context_paths"] == [
        "configlib/loader.py"
    ]
    assert (
        metadata["bounded_execution_debug_repair_changed_file_context_complete_paths"]
        == []
    )


def test_workflow_b_raw_output_shape_round_trips_through_json():
    # The strict parser receives JSON text; the recognized shape is unchanged.
    parsed = json.loads(json.dumps([WORKFLOW_B_REPAIR]))
    result = normalize_bounded_debug_repair_payload_detailed(parsed)
    assert result.payload["ops"][0]["path"] == "configlib/__init__.py"
