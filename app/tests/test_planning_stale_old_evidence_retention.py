"""Provider-free characterization of bounded stale-replace evidence."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from app.services.orchestration.phases.planning_support import (
    STALE_REPLACE_TEXT_MAX_CHARS,
    _extract_stale_replace_evidence_from_plan,
    _planning_invalid_commands_after_repair_details,
)
from app.services.orchestration.planning.source_materialization import (
    SOURCE_STATUS_EXISTING,
    MaterializedSourceFile,
    PlannerSourceMaterialization,
)
from app.services.orchestration.state.persistence import record_live_log


def _plan(old: str, new: str, **operation_fields):
    operation = {
        "op": "replace_in_file",
        "path": "src/example.py",
        "old": old,
        "new": new,
        **operation_fields,
    }
    return [{"step_number": 1, "ops": [operation]}]


def _materialization(path: str = "src/example.py"):
    record = MaterializedSourceFile(
        relative_path=path,
        workspace_identity="/tmp/project",
        content="visible excerpt",
        content_hash="excerpt-hash",
        version_identity="version-1",
        status=SOURCE_STATUS_EXISTING,
        truncated=True,
        source_length=20_000,
        source_length_chars=20_000,
        included_prompt_length=15,
    )
    return PlannerSourceMaterialization(
        workspace_identity="/tmp/project",
        files=(record,),
        materialized_source_bytes=15,
    )


class _DB:
    def add(self, row):
        self.row = row

    def commit(self):
        self.committed = True


def _evidence(plan, **kwargs):
    return _extract_stale_replace_evidence_from_plan(
        plan, stale_step_numbers=[1], **kwargs
    )[0]


def test_exact_stale_operation_is_bounded_and_survives_existing_log_json():
    plan = _plan("return old\n", "return new\n")
    item = _evidence(plan, source_materialization=_materialization())

    assert item["normalized_relative_path"] == "src/example.py"
    assert item["submitted_old"] == "return old\n"
    assert item["submitted_new"] == "return new\n"
    assert item["source_version_identity"] == "version-1"
    assert item["source_status"] == SOURCE_STATUS_EXISTING
    assert item["stale_old_failure_code"] == "stale_replace_in_file_old_text"

    db = _DB()
    record_live_log(
        db,
        session_id=7,
        task_id=8,
        level="ERROR",
        message="planning failure",
        metadata={
            "reason": "planning_invalid_commands_after_repair",
            "stale_replace_evidence": [item],
        },
    )
    persisted = json.loads(db.row.log_metadata)
    assert persisted["stale_replace_evidence"][0] == item
    assert db.committed is True


def test_blank_line_divergence_retains_submitted_old_unchanged():
    item = _evidence(_plan("def value():\n    return 1\n", "pass\n"))
    assert item["submitted_old"] == "def value():\n    return 1\n"
    assert item["submitted_old_truncated"] is False


def test_stitched_old_and_wrong_path_are_retained_without_source_fabrication():
    plan = _plan("first\nthird\n", "replacement\n", path="src/wrong.py")
    item = _evidence(plan)
    assert item["submitted_old"] == "first\nthird\n"
    assert item["normalized_relative_path"] == "src/wrong.py"
    assert item["source_status"] is None
    assert item["source_version_identity"] is None


def test_semantic_presence_flags_are_observational_only():
    item = _evidence(
        _plan(
            "old",
            "new",
            target_id="target-1",
            selector={"target_id": "target-1"},
        )
    )
    assert item["semantic_target_id_present"] is True
    assert item["semantic_selector_present"] is True


def test_large_text_is_prefix_suffix_bounded_with_length_and_hash():
    old = "α" * (STALE_REPLACE_TEXT_MAX_CHARS + 100)
    new = "new\n" * (STALE_REPLACE_TEXT_MAX_CHARS + 100)
    item = _evidence(_plan(old, new))

    assert len(item["submitted_old"]) <= STALE_REPLACE_TEXT_MAX_CHARS
    assert len(item["submitted_new"]) <= STALE_REPLACE_TEXT_MAX_CHARS
    assert item["submitted_old_truncated"] is True
    assert item["submitted_new_truncated"] is True
    assert item["submitted_old_length"] == len(old)
    assert item["submitted_new_length"] == len(new)
    assert (
        item["submitted_old_sha256"] == hashlib.sha256(old.encode("utf-8")).hexdigest()
    )
    assert (
        item["submitted_new_sha256"] == hashlib.sha256(new.encode("utf-8")).hexdigest()
    )


def test_unicode_and_empty_operation_text_are_serialized_truthfully():
    item = _evidence(_plan("", "λ → 值\n"))
    assert item["submitted_old"] == ""
    assert item["submitted_old_length"] == 0
    assert item["submitted_new"] == "λ → 值\n"
    assert json.loads(json.dumps(item, ensure_ascii=False)) == item


def test_missing_source_information_is_null_not_invented():
    item = _evidence(_plan("old", "new"))
    assert item["source_status"] is None
    assert item["source_version_identity"] is None


def test_success_path_does_not_create_stale_evidence():
    assert (
        _extract_stale_replace_evidence_from_plan(
            _plan("old", "new"), stale_step_numbers=[]
        )
        == []
    )


def test_terminal_failure_details_attach_exact_validator_finding():
    plan = _plan("old", "new")
    verdict = SimpleNamespace(
        details={
            "source_operation_findings": [
                {
                    "step_number": 1,
                    "operation_index": 1,
                    "failure_code": "stale_old_text_absent_from_current_source",
                    "source_version_identity": "version-2",
                }
            ]
        }
    )
    details = _planning_invalid_commands_after_repair_details(
        plan=plan,
        blocking_repair_issues={"stale_replace_ops_steps": [1]},
        blocking_plan_verdict=verdict,
        retry_state=SimpleNamespace(planning_root_cause="stale_replace"),
        model_lane_limitation={"runtime_rewrite_added": False},
        source_materialization=None,
    )
    item = details["stale_replace_evidence"][0]
    assert item["operation_index"] == 1
    assert item["source_version_identity"] == "version-2"
    assert details["stale_old_text"] == ["old"]


def _classify_later(item, current_source, available_paths=None):
    path = item["normalized_relative_path"]
    if available_paths is not None and path not in available_paths:
        return "wrong_path"
    old = item["submitted_old"]
    if current_source.count(old) > 1:
        return "ambiguous"
    if current_source.count(old) == 1:
        return "exact"
    old_lines = old.splitlines()
    source_lines = current_source.splitlines()
    if [line.strip() for line in old_lines] == [line.strip() for line in source_lines]:
        return "whitespace_or_indentation"
    significant_old = [line.strip() for line in old_lines if line.strip()]
    significant_source = [line.strip() for line in source_lines if line.strip()]
    if significant_old and significant_old == significant_source:
        return "blank_line_only"
    if significant_old and all(line in significant_source for line in significant_old):
        return "stitched_or_non_contiguous"
    return "absent"


def test_retained_fields_support_provider_free_future_characterization():
    blank = _evidence(_plan("first\nsecond\n", "new"))
    indent = _evidence(_plan("def f():\n  return 1\n", "new"))
    absent = _evidence(_plan("missing\nregion\n", "new"))
    stitched = _evidence(_plan("first\nthird\n", "new"))
    ambiguous = _evidence(_plan("same\n", "new"))
    wrong_path = _evidence(_plan("old\n", "new", path="missing.py"))

    assert _classify_later(blank, "first\n\nsecond\n") == "blank_line_only"
    assert (
        _classify_later(indent, "def f():\n    return 1\n")
        == "whitespace_or_indentation"
    )
    assert _classify_later(absent, "other\ncontent\n") == "absent"
    assert (
        _classify_later(stitched, "first\nsecond\nthird\n")
        == "stitched_or_non_contiguous"
    )
    assert _classify_later(wrong_path, "old\n", {"src/example.py"}) == "wrong_path"
    assert _classify_later(ambiguous, "same\nsame\n") == "ambiguous"
