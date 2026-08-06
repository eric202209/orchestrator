"""Phase 32N-1 — context-derived repair prompt budget.

Attempts 6, 7 and 9 each made **zero** repair provider calls: their required
complete-plan repair envelopes exceeded the fixed 8,000-character cap while the
admitted repair deployment declared 16,000+ tokens of context
(`MIN_REPAIR_CONTEXT_TOKENS`, `agent_runtime.py`). These tests pin the budget
authority that removes that blocker, and pin that a *genuine* overflow still
fails closed before any provider call.

The Attempt 7 and Attempt 9 shapes are reconstructed provider-free from
retained evidence. Excerpt bytes are re-derived from canonical repository
source at the recorded span offsets and asserted byte-identical to the
`content_hash` values recorded at runtime, so the reconstruction is faithful
rather than approximate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.config import settings
from app.services.orchestration.planning import repair_prompts
from app.services.orchestration.planning.repair_prompts import (
    PLANNING_REPAIR_PROMPT_MAX_CHARS_CEILING,
    REPAIR_PROMPT_MAX_CHARS,
    RequiredRepairSourceEvidenceExceeded,
    build_compact_stale_replace_repair_prompt,
    build_minimum_safe_stale_replace_repair_envelope,
    effective_repair_prompt_max_chars,
)
from app.services.orchestration.planning.source_materialization import (
    MaterializedSourceFile,
    MaterializedSourceSpan,
    PlannerSourceMaterialization,
    repair_projection_required_records,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = REPOSITORY_ROOT / "docs/roadmap/reports/evidence"
ATTEMPT9_EVIDENCE = EVIDENCE_ROOT / "phase32k1-attempt9-utc-now-20260805"
ATTEMPT7_EVIDENCE = EVIDENCE_ROOT / "phase32f1-attempt7-evidence-20260804"

# Retained runtime authority.
ATTEMPT9_PLAN_SHA256 = (
    "c7c203efc8c54a8352b3098f1d25cea13bf804be1041849cb4dda286ff41d31c"
)
ATTEMPT7_MATERIALIZATION_LOG_ENTRY_ID = 12855
REJECTION_REASONS = ["replace_in_file old text not found in workspace in steps [2]"]
REQUIRED_PATHS = (
    "app/time_utils.py",
    "app/services/workspace/context_service.py",
    "app/tests/test_utc_now_helper.py",
)
# The admitted repair-context floor verified at dispatch (agent_runtime.py).
ADMITTED_REPAIR_CONTEXT_FLOOR_TOKENS = 16_000


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compose_excerpt(relative_path: str, spans: list[tuple[int, int]]) -> str:
    """Re-derive one materialized excerpt exactly as `_compose_span_content` does."""

    encoded = (REPOSITORY_ROOT / relative_path).read_bytes()
    parts: list[str] = []
    if spans[0][0] > 0:
        parts.append("... [truncated]\n")
    for index, (start_byte, end_byte) in enumerate(spans):
        body = encoded[start_byte:end_byte].decode("utf-8", errors="ignore")
        parts.append(body)
        if index + 1 < len(spans):
            if not body.endswith("\n"):
                parts.append("\n")
            parts.append("... [truncated]\n")
    if spans[-1][1] < len(encoded):
        parts.append("\n... [truncated]")
    return "".join(parts)


def build_retained_materialization(record: dict) -> PlannerSourceMaterialization:
    """Rebuild a retained materialization, asserting excerpt fidelity."""

    files = []
    for entry in record["files"]:
        content = None
        if entry["status"] == "existing_file_with_materialized_source":
            spans = [
                (span["start_byte"], span["end_byte"])
                for span in entry.get("spans", [])
            ] or [(entry["start_byte"], entry["end_byte"])]
            content = _compose_excerpt(entry["relative_path"], spans)
            assert _sha256(content) == entry["content_hash"], (
                f"{entry['relative_path']} excerpt is not byte-identical to the "
                "retained runtime record; the reconstruction is not faithful"
            )
            assert len(content) == entry["included_prompt_length"]
        values = {
            key: value
            for key, value in entry.items()
            if key not in {"spans", "workspace_identity"}
        }
        values["workspace_identity"] = record["workspace_identity"]
        values["content"] = content
        values["spans"] = tuple(
            MaterializedSourceSpan(**span) for span in entry.get("spans", [])
        )
        files.append(MaterializedSourceFile(**values))
    return PlannerSourceMaterialization(
        workspace_identity=record["workspace_identity"],
        files=tuple(files),
        maximum_files=record["maximum_files"],
        maximum_bytes_per_file=record["maximum_bytes_per_file"],
        maximum_total_source_bytes=record["maximum_total_source_bytes"],
        materialized_source_bytes=record["materialized_source_bytes"],
        unavailable_reasons=tuple(record["unavailable_reasons"]),
    )


def _task_description(task_id: int) -> str:
    connection = sqlite3.connect(
        f"file:{REPOSITORY_ROOT / 'orchestrator.db'}?mode=ro", uri=True
    )
    try:
        row = connection.execute(
            "select description from tasks where id=?", (task_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None or not row[0]:
        pytest.skip(f"task {task_id} description is not present in this database")
    return row[0]


@pytest.fixture(scope="module")
def attempt9_shape() -> dict:
    record = json.loads((ATTEMPT9_EVIDENCE / "source-materialization.json").read_text())
    plan = json.loads((ATTEMPT9_EVIDENCE / "first-plan-raw.json").read_text())[
        "raw_response"
    ]
    assert _sha256(plan) == ATTEMPT9_PLAN_SHA256
    return {
        "materialization": build_retained_materialization(record),
        "plan": plan,
        "task_description": _task_description(176),
    }


@pytest.fixture(scope="module")
def attempt7_shape() -> dict:
    connection = sqlite3.connect(
        f"file:{REPOSITORY_ROOT / 'orchestrator.db'}?mode=ro", uri=True
    )
    try:
        row = connection.execute(
            "select log_metadata from log_entries where id=?",
            (ATTEMPT7_MATERIALIZATION_LOG_ENTRY_ID,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        pytest.skip("Attempt 7 materialization log entry is not present")
    record = json.loads(row[0])["source_materialization"]
    plan = json.dumps(
        json.loads(
            (
                ATTEMPT7_EVIDENCE / "phase32f1-attempt7-raw-first-plan-20260804.json"
            ).read_text()
        )
    )
    return {
        "materialization": build_retained_materialization(record),
        "plan": plan,
        "task_description": _task_description(174),
    }


def _envelope(shape: dict) -> str:
    envelope = build_minimum_safe_stale_replace_repair_envelope(
        task_description=shape["task_description"],
        malformed_output=shape["plan"],
        rejection_reasons=REJECTION_REASONS,
        prompt_profile="ollama_default",
        apply_prompt_profile=None,
        source_materialization=shape["materialization"],
    )
    assert envelope is not None
    return envelope.prompt


@pytest.fixture
def admitted_repair_context(monkeypatch) -> int:
    """Pin the admitted repair context so the budget never depends on local .env."""

    monkeypatch.setattr(
        settings,
        "PLANNING_REPAIR_CONTEXT_TOKENS",
        ADMITTED_REPAIR_CONTEXT_FLOOR_TOKENS,
    )
    return ADMITTED_REPAIR_CONTEXT_FLOOR_TOKENS


# --------------------------------------------------------------------------
# Budget authority
# --------------------------------------------------------------------------


def test_budget_falls_back_to_the_floor_when_no_context_is_declared(monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", None)
    assert effective_repair_prompt_max_chars() == REPAIR_PROMPT_MAX_CHARS == 8000


@pytest.mark.parametrize("declared", ["", "   ", 0, -1, "not-a-number"])
def test_budget_falls_back_to_the_floor_for_unusable_context(monkeypatch, declared):
    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", declared)
    assert effective_repair_prompt_max_chars() == REPAIR_PROMPT_MAX_CHARS


def test_budget_at_the_admitted_context_floor_exceeds_the_old_fixed_cap(
    admitted_repair_context,
):
    effective = effective_repair_prompt_max_chars()
    assert effective == 32_000
    assert effective > REPAIR_PROMPT_MAX_CHARS
    # Half the admitted context in characters, i.e. the other half is reserved
    # for the repair model's own complete-plan output.
    assert effective == int(admitted_repair_context * 4 * 0.5)


def test_budget_is_clamped_by_the_hard_ceiling_for_an_over_declared_context(
    monkeypatch,
):
    # The deployed .env declares 200,000 tokens while the provider reports a
    # max_model_len of 131,072. The ceiling must bound both.
    for declared in (131_072, 200_000, 10_000_000):
        monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", declared)
        assert (
            effective_repair_prompt_max_chars()
            == PLANNING_REPAIR_PROMPT_MAX_CHARS_CEILING
            == 32_000
        )


def test_a_pinned_cap_below_the_floor_stays_authoritative(monkeypatch):
    """The test-pinned true-overflow path must keep its exact behaviour."""

    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", 200_000)
    monkeypatch.setattr(repair_prompts, "PLANNING_REPAIR_PROMPT_MAX_CHARS", 200)
    assert effective_repair_prompt_max_chars() == 200


# --------------------------------------------------------------------------
# Retained Attempt 7 / Attempt 9 envelopes
# --------------------------------------------------------------------------


def test_attempt7_envelope_exceeds_the_old_cap_and_fits_the_derived_budget(
    attempt7_shape, admitted_repair_context
):
    prompt = _envelope(attempt7_shape)
    assert len(prompt) > REPAIR_PROMPT_MAX_CHARS
    assert len(prompt) <= effective_repair_prompt_max_chars()


def test_attempt9_envelope_exceeds_the_old_cap_and_fits_the_derived_budget(
    attempt9_shape, admitted_repair_context
):
    prompt = _envelope(attempt9_shape)
    assert len(prompt) > REPAIR_PROMPT_MAX_CHARS
    assert len(prompt) <= effective_repair_prompt_max_chars()


@pytest.mark.parametrize("shape_name", ["attempt7_shape", "attempt9_shape"])
def test_retained_envelope_reconstruction_is_byte_identical_when_repeated(
    request, shape_name, admitted_repair_context
):
    shape = request.getfixturevalue(shape_name)
    first = _envelope(shape)
    second = _envelope(shape)
    assert first == second
    assert _sha256(first) == _sha256(second)


@pytest.mark.parametrize("shape_name", ["attempt7_shape", "attempt9_shape"])
def test_production_repair_prompt_is_produced_and_carries_r0_grounding(
    request, shape_name, admitted_repair_context
):
    """The production path must emit a grounded prompt within the budget.

    Scope note: raising the budget does not change *which* candidate the
    compaction ladder prefers. The ladder still returns a compact candidate
    (carrying a truncated rejected-plan excerpt) before it reaches the
    complete-plan Candidate A envelope. Candidate-selection order is outside
    the Phase 32N-1 corrections; what is pinned here is that the emitted prompt
    fits the derived budget and retains every R0 grounding record.
    """

    shape = request.getfixturevalue(shape_name)
    result = build_compact_stale_replace_repair_prompt(
        task_description=shape["task_description"],
        project_dir=REPOSITORY_ROOT,
        malformed_output=shape["plan"],
        rejection_reasons=REJECTION_REASONS,
        prompt_profile="ollama_default",
        apply_prompt_profile=None,
        source_materialization=shape["materialization"],
    )
    assert not isinstance(result, RequiredRepairSourceEvidenceExceeded)
    assert isinstance(result, str) and result
    assert len(result) <= effective_repair_prompt_max_chars()

    target = shape["materialization"].file_map()[
        "app/services/workspace/context_service.py"
    ]
    assert target.content in result, "the R0 source excerpt must survive intact"
    assert target.version_identity in result
    assert target.content_hash in result
    for path in REQUIRED_PATHS:
        assert path in result


def test_attempt9_repair_prompt_retains_every_required_grounding_record(
    attempt9_shape, admitted_repair_context
):
    materialization = attempt9_shape["materialization"]
    prompt = _envelope(attempt9_shape)

    required = repair_projection_required_records(materialization, REQUIRED_PATHS)
    assert [item.relative_path for item, _ in required] == list(REQUIRED_PATHS)
    assert {priority for _, priority in required} == {"R0"}

    # R0 records, their version identity, hash and creation authorization.
    for path in REQUIRED_PATHS:
        assert path in prompt
    target = materialization.file_map()["app/services/workspace/context_service.py"]
    assert target.version_identity in prompt
    assert target.content_hash in prompt
    assert target.content in prompt, "the R0 source excerpt must survive intact"
    assert "new_file_authorized_for_creation" in prompt

    # The complete rejected plan remains present under the complete-plan
    # repair contract.
    minified = json.dumps(
        json.loads(attempt9_shape["plan"]), ensure_ascii=False, separators=(",", ":")
    )
    assert minified in prompt


def test_first_pass_and_repair_source_evidence_stay_consistent(attempt9_shape):
    """The same bytes and provenance must back both prompt surfaces."""

    materialization = attempt9_shape["materialization"]
    first_pass = materialization.to_prompt_block()
    repair_projection = repair_prompts.render_repair_source_materialization(
        materialization, rejected_paths=REQUIRED_PATHS, compaction_level=4
    )
    target = materialization.file_map()["app/services/workspace/context_service.py"]
    for fragment in (target.content, target.content_hash, target.version_identity):
        assert fragment in first_pass
        assert fragment in repair_projection


# --------------------------------------------------------------------------
# Genuine overflow still fails closed, with no provider call
# --------------------------------------------------------------------------


def test_genuine_required_evidence_overflow_still_fails_closed(
    attempt9_shape, monkeypatch
):
    """Required evidence larger than the derived budget must still stop."""

    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", None)
    monkeypatch.setattr(repair_prompts, "PLANNING_REPAIR_PROMPT_MAX_CHARS_CEILING", 900)
    assert effective_repair_prompt_max_chars() == REPAIR_PROMPT_MAX_CHARS

    materialization = attempt9_shape["materialization"]
    target_path = "app/services/workspace/context_service.py"
    target = materialization.file_map()[target_path]
    # An R0 record whose required excerpt alone cannot fit any budget.
    oversized = MaterializedSourceFile(
        **{
            **target.to_dict(),
            "content": "X" * 400_000,
            "spans": target.spans,
        }
    )
    inflated = PlannerSourceMaterialization(
        workspace_identity=materialization.workspace_identity,
        files=tuple(
            oversized if item.relative_path == target_path else item
            for item in materialization.files
        ),
        maximum_files=materialization.maximum_files,
        maximum_bytes_per_file=materialization.maximum_bytes_per_file,
        maximum_total_source_bytes=materialization.maximum_total_source_bytes,
        materialized_source_bytes=materialization.materialized_source_bytes,
    )

    result = build_compact_stale_replace_repair_prompt(
        task_description=attempt9_shape["task_description"],
        project_dir=REPOSITORY_ROOT,
        malformed_output=attempt9_shape["plan"],
        rejection_reasons=REJECTION_REASONS,
        prompt_profile="ollama_default",
        apply_prompt_profile=None,
        source_materialization=inflated,
    )

    assert isinstance(result, RequiredRepairSourceEvidenceExceeded)
    diagnostics = result.diagnostics
    assert diagnostics["reason"] == (
        "required_repair_source_evidence_exceeds_prompt_bound"
    )
    assert diagnostics["failure_owner"] == "repair_prompt_projection"
    assert diagnostics["compaction_levels_attempted"] == [0, 1, 2, 3, 4]
    assert diagnostics["lowest_optional_records_removed"] is True
    # The effective limit is reported, not the raw constant.
    assert diagnostics["prompt_limit"] == effective_repair_prompt_max_chars()
    assert diagnostics["effective_repair_prompt_limit"] == (
        effective_repair_prompt_max_chars()
    )
    assert diagnostics["repair_prompt_limit_floor"] == REPAIR_PROMPT_MAX_CHARS
    assert diagnostics["minimum_required_chars"] > diagnostics["prompt_limit"]


def test_genuine_overflow_reaches_no_provider(attempt9_shape, monkeypatch):
    """Fail-closed must happen during construction, before any provider call."""

    provider_calls: list[str] = []

    def _fail_on_provider(*args, **kwargs):  # pragma: no cover - must not run
        provider_calls.append("called")
        raise AssertionError("repair provider must not be invoked on overflow")

    for attribute in (
        "run_planning_repair",
        "call_planning_repair",
        "_call_repair_backend",
    ):
        if hasattr(repair_prompts, attribute):
            monkeypatch.setattr(repair_prompts, attribute, _fail_on_provider)

    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", None)
    materialization = attempt9_shape["materialization"]
    target_path = "app/services/workspace/context_service.py"
    target = materialization.file_map()[target_path]
    oversized = MaterializedSourceFile(
        **{**target.to_dict(), "content": "Y" * 400_000, "spans": target.spans}
    )
    inflated = PlannerSourceMaterialization(
        workspace_identity=materialization.workspace_identity,
        files=tuple(
            oversized if item.relative_path == target_path else item
            for item in materialization.files
        ),
    )

    result = build_compact_stale_replace_repair_prompt(
        task_description=attempt9_shape["task_description"],
        project_dir=REPOSITORY_ROOT,
        malformed_output=attempt9_shape["plan"],
        rejection_reasons=REJECTION_REASONS,
        prompt_profile="ollama_default",
        apply_prompt_profile=None,
        source_materialization=inflated,
    )

    assert isinstance(result, RequiredRepairSourceEvidenceExceeded)
    assert provider_calls == []
