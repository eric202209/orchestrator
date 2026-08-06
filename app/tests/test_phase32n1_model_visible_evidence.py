"""Phase 32N-1 — model-visible source evidence carries no duplicated telemetry.

`assemble_planning_prompt` used to append the readable source block *and*
inject the structured materialization dictionary into the prompt envelope
context, which every profile renderer serializes into the model-visible prompt.
That duplicated, in JSON and without excerpts, the same records the readable
block already carried.

These tests pin that the model-visible surface keeps every field a planner can
act on, that the withdrawn provenance is still retained internally, and that
the duplicate is gone.
"""

from __future__ import annotations

import pytest

from app.services.model_adaptation.renderers import (
    render_openclaw_prompt,
    render_qwen_compact_json_prompt,
)
from app.services.model_adaptation.schemas import PromptEnvelope
from app.services.orchestration.planning.source_materialization import (
    PlannerSourceMaterialization,
)

from app.tests.test_phase32n1_repair_budget_authority import (  # noqa: E402
    attempt9_shape,  # re-exported fixture
)

__all__ = ["attempt9_shape"]

# Fields that exist only as audit bookkeeping: byte offsets and counters a
# planner cannot act on, and which the readable block never rendered.
TELEMETRY_ONLY_FIELDS = (
    "start_byte",
    "end_byte",
    "target_match_start",
    "target_match_end",
    "target_match_count",
    "included_prompt_length",
    "included_source_bytes",
    "full_source_bytes",
    "source_length_chars",
    "materialized_source_bytes",
    "target_materialized_file_count",
    "expected_file_count",
)

# Fields a planner must still see to make a grounded exact edit.
MODEL_VISIBLE_ESSENTIALS = (
    "relative_path",
    "status",
    "expected",
    "creation_authorized",
    "version_identity",
    "content_hash",
    "visible_lines",
    "target_hint",
    "content",
)


def _planning_envelope(prompt_body: str) -> PromptEnvelope:
    """The envelope shape `assemble_planning_prompt` now builds."""

    return PromptEnvelope(
        objective="Generate a machine-runnable JSON execution plan for the requested task.",
        execution_mode="planning",
        instructions=[
            "Do not implement anything yet.",
            "Return a sequential JSON plan only.",
        ],
        context={
            "Project Directory": "/workspace",
            "Execution Profile": "full_lifecycle",
            "Workflow Profile": "default",
        },
        expected_output="JSON array of orchestration step objects.",
        prompt_body=prompt_body,
    )


def test_structured_prompt_metadata_surface_is_removed():
    """The model-visible provenance dictionary no longer exists."""

    assert not hasattr(PlannerSourceMaterialization, "to_prompt_metadata")


def test_assemble_planning_prompt_no_longer_injects_materialization_context():
    import inspect

    from app.services.orchestration.context import assembly

    source = inspect.getsource(assembly.assemble_planning_prompt)
    assert "to_prompt_metadata" not in source
    assert "Planner Source Materialization" not in source


def test_model_visible_block_retains_every_actionable_field(attempt9_shape):
    materialization = attempt9_shape["materialization"]
    block = materialization.to_prompt_block()
    target = materialization.file_map()["app/services/workspace/context_service.py"]

    for label in MODEL_VISIBLE_ESSENTIALS:
        assert f"{label}:" in block or label == "relative_path"

    assert "app/services/workspace/context_service.py" in block
    assert target.version_identity in block
    assert target.content_hash in block
    assert target.content in block, "the source excerpt bytes must be unchanged"
    assert f"visible_lines: {target.start_line}-{target.end_line}" in block
    assert f"target_hint: {target.target_hint}" in block
    assert "creation_authorized: true" in block
    assert "new_file_authorized_for_creation" in block


def test_rendered_prompt_carries_no_duplicated_materialization_telemetry(
    attempt9_shape,
):
    materialization = attempt9_shape["materialization"]
    envelope = _planning_envelope(materialization.to_prompt_block())

    for rendered in (
        render_openclaw_prompt(envelope),
        render_qwen_compact_json_prompt(envelope),
    ):
        assert "Planner Source Materialization" not in rendered
        for field in TELEMETRY_ONLY_FIELDS:
            assert (
                field not in rendered
            ), f"{field!r} is audit telemetry and must not reach the model"
        # The evidence itself is still there.
        assert "CURRENT SOURCE MATERIALIZATION" in rendered
        assert "app/services/workspace/context_service.py" in rendered


def test_internal_provenance_retains_the_withdrawn_fields(attempt9_shape):
    """Everything removed from the prompt is still available internally."""

    materialization = attempt9_shape["materialization"]
    metadata = materialization.to_metadata()

    for field in (
        "materialized_source_bytes",
        "expected_file_count",
        "target_materialized_file_count",
        "maximum_total_source_bytes",
        "workspace_identity",
    ):
        assert field in metadata

    target = next(
        item
        for item in metadata["files"]
        if item["relative_path"] == "app/services/workspace/context_service.py"
    )
    for field in (
        "start_byte",
        "end_byte",
        "start_line",
        "end_line",
        "target_match_start",
        "target_match_end",
        "target_match_count",
        "included_prompt_length",
        "included_source_bytes",
        "full_source_bytes",
        "source_length_chars",
        "content_hash",
        "version_identity",
        "selection_strategy",
        "priority",
    ):
        assert field in target, f"{field} must remain in internal provenance"

    # to_metadata() stays content-free; per-file to_dict() keeps the bytes.
    assert "content" not in target
    record = materialization.file_map()["app/services/workspace/context_service.py"]
    assert record.to_dict()["content"] == record.content

    # The real workspace identity is retained internally rather than masked.
    assert metadata["workspace_identity"] == materialization.workspace_identity


def test_removing_the_duplicate_reclaims_model_visible_characters(attempt9_shape):
    """Measure the model-visible saving on the retained Attempt 9 shape."""

    materialization = attempt9_shape["materialization"]
    block = materialization.to_prompt_block()

    # Reproduce the withdrawn duplicate exactly as it used to be built.
    masked = "current isolated task workspace"
    legacy = materialization.to_metadata()
    legacy["workspace_identity"] = masked
    legacy["files"] = [
        {**item, "workspace_identity": masked} for item in legacy["files"]
    ]

    before = render_openclaw_prompt(
        PromptEnvelope(
            objective="o",
            execution_mode="planning",
            instructions=[],
            context={"Planner Source Materialization": legacy},
            expected_output="e",
            prompt_body=block,
        )
    )
    after = render_openclaw_prompt(
        PromptEnvelope(
            objective="o",
            execution_mode="planning",
            instructions=[],
            context={},
            expected_output="e",
            prompt_body=block,
        )
    )

    assert len(after) < len(before)
    # The duplicate was a substantial share of the model-visible prompt.
    assert len(before) - len(after) > 3_000
    # No source evidence was lost to obtain that saving.
    assert materialization.to_prompt_block() in after


@pytest.mark.parametrize("compaction_level", [0, 1, 2, 3, 4])
def test_repair_projection_never_renders_the_telemetry_dictionary(
    attempt9_shape, compaction_level
):
    from app.services.orchestration.planning.source_materialization import (
        render_repair_source_materialization,
    )

    rendered = render_repair_source_materialization(
        attempt9_shape["materialization"],
        rejected_paths=("app/services/workspace/context_service.py",),
        compaction_level=compaction_level,
    )
    assert "Planner Source Materialization" not in rendered
    assert "target_match_start" not in rendered
    assert "included_prompt_length" not in rendered
