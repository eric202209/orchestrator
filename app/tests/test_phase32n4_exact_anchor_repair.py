"""Phase 32N-4 red/green contract for Orchestrator-owned exact repair anchors."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from app.services.orchestration.planning.operation_repair import (
    AnchoredRepairEntry,
    OperationRepairError,
    build_operation_anchor_registry,
    build_operation_repair_prompt,
    merge_and_validate_operation_repairs,
    merge_operation_repairs,
    parse_operation_repair_response,
    select_operation_repair_route,
)
from app.services.orchestration.planning import (
    operation_repair as operation_repair_module,
)
from app.services.orchestration.planning.operation_repair_anchors import (
    DERIVATION_BLANK_LINE_TOLERANT,
    DERIVATION_MINIMAL_DIVERGENT,
    MAX_ANCHORS_PER_OPERATION,
    SourceAnchor,
    derive_operation_anchors,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.validation.validator import ValidatorService


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RETAINED_SHAPES = (
    REPOSITORY_ROOT / "app/tests/fixtures/phase32n1_retained_attempt_shapes.json"
)
TARGET = "app/services/workspace/context_service.py"

# The exact string the deployed Phase 32N-3 provider request returned as
# ``old``.  It is the original rejected anchor copied verbatim: every import
# group separator blank line of the real file is missing.
N3_RECONSTRUCTED_OLD = (
    "import json\n"
    "import logging\n"
    "from datetime import datetime\n"
    "from typing import Optional, Dict, Any, List\n"
    "from sqlalchemy.orm import Session as DBSession\n"
    "from sqlalchemy import func\n"
    "from app.models import (\n"
    "    SessionState,\n"
    "    ConversationHistory,\n"
    "    TaskCheckpoint,\n"
    ")\n"
    "logger = logging.getLogger(__name__)"
)

# The same region as it really exists in the pinned source, blank lines intact.
EXACT_FULL_ANCHOR = (
    "import json\n"
    "import logging\n"
    "from datetime import datetime\n"
    "from typing import Optional, Dict, Any, List\n"
    "\n"
    "from sqlalchemy.orm import Session as DBSession\n"
    "from sqlalchemy import func\n"
    "\n"
    "from app.models import (\n"
    "    SessionState,\n"
    "    ConversationHistory,\n"
    "    TaskCheckpoint,\n"
    ")\n"
    "\n"
    "logger = logging.getLogger(__name__)"
)

# The smallest region Attempt 9 actually changes, blank lines intact.
EXACT_MINIMAL_ANCHOR = (
    "from datetime import datetime\n"
    "from typing import Optional, Dict, Any, List\n"
    "\n"
    "from sqlalchemy.orm import Session as DBSession\n"
    "from sqlalchemy import func\n"
    "\n"
    "from app.models import (\n"
    "    SessionState,\n"
    "    ConversationHistory,\n"
    "    TaskCheckpoint,\n"
    ")"
)

# The replacement the model must supply for EXACT_MINIMAL_ANCHOR.
MINIMAL_ANCHOR_NEW = (
    "from typing import Optional, Dict, Any, List\n"
    "\n"
    "from sqlalchemy.orm import Session as DBSession\n"
    "from sqlalchemy import func\n"
    "\n"
    "from app.models import (\n"
    "    SessionState,\n"
    "    ConversationHistory,\n"
    "    TaskCheckpoint,\n"
    ")\n"
    "from app.time_utils import utc_now"
)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _attempt_context(tmp_path, attempt_name="attempt9"):
    fixture = json.loads(RETAINED_SHAPES.read_text(encoding="utf-8"))[attempt_name]
    plan = json.loads(fixture["plan"])
    target_path = tmp_path / TARGET
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPOSITORY_ROOT / TARGET, target_path)
    expected_paths = sorted(
        {
            operation["path"]
            for step in plan
            for operation in step.get("ops", [])
            if operation.get("path")
        }
    )
    materialization = materialize_planner_source_context(
        tmp_path,
        expected_paths=expected_paths,
        task_description=fixture["task_description"],
    )
    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=fixture["task_description"],
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    findings = verdict.details["source_operation_findings"]
    return fixture, plan, materialization, findings


def _validate(fixture, tmp_path, materialization):
    def _run(merged):
        return ValidatorService.validate_plan(
            merged,
            output_text=json.dumps(merged),
            task_prompt=fixture["task_description"],
            execution_profile="full_lifecycle",
            project_dir=tmp_path,
            source_materialization=materialization,
        )

    return _run


def _anchored(step_number, operation_index, anchor_id, new) -> str:
    return json.dumps(
        {
            "repairs": [
                {
                    "step_number": step_number,
                    "operation_index": operation_index,
                    "anchor_id": anchor_id,
                    "new": new,
                }
            ]
        }
    )


# --------------------------------------------------------------------------
# Anchor derivation
# --------------------------------------------------------------------------


def test_attempt9_exact_anchors_are_derived_with_real_blank_lines(tmp_path):
    """Requirements 1, 2 and 3: exact, blank-line-true, deterministic anchors."""

    _fixture, plan, materialization, findings = _attempt_context(tmp_path)
    source = (tmp_path / TARGET).read_text(encoding="utf-8")
    assert N3_RECONSTRUCTED_OLD not in source

    def derive():
        return build_operation_anchor_registry(
            original_plan=plan,
            rejected_findings=findings,
            source_materialization=materialization,
            project_dir=tmp_path,
        )

    first = derive()
    second = derive()
    anchors = first.by_identity[(2, 1)]

    assert [anchor.anchor_id for anchor in anchors] == [
        "anchor-2-1-1",
        "anchor-2-1-2",
    ]
    assert [anchor.anchor_id for anchor in second.by_identity[(2, 1)]] == [
        "anchor-2-1-1",
        "anchor-2-1-2",
    ]
    assert [anchor.text for anchor in anchors] == [
        EXACT_MINIMAL_ANCHOR,
        EXACT_FULL_ANCHOR,
    ]
    assert [anchor.derivation for anchor in anchors] == [
        DERIVATION_MINIMAL_DIVERGENT,
        DERIVATION_BLANK_LINE_TOLERANT,
    ]
    for anchor in anchors:
        assert anchor.text in source
        assert source.count(anchor.text) == 1
        # The blank lines the model dropped are present and authentic.
        assert "\n\n" in anchor.text
        assert anchor.relative_path == TARGET
        assert (anchor.step_number, anchor.operation_index) == (2, 1)
    assert len(anchors) <= MAX_ANCHORS_PER_OPERATION


def test_ambiguous_candidates_are_dropped_and_absence_fails_closed(tmp_path):
    """A repeated region is never offered; an underivable one refuses the lane."""

    source = "def value():\n\n    return 1\n\n\ndef value():\n\n    return 1\n"
    anchors = derive_operation_anchors(
        step_number=1,
        operation_index=1,
        relative_path="pkg/current.py",
        version_identity="v1",
        original_old="def value():\n    return 1",
        original_new="def value():\n    return 2",
        full_source=source,
    )
    assert anchors == ()

    assert (
        derive_operation_anchors(
            step_number=1,
            operation_index=1,
            relative_path="pkg/current.py",
            version_identity="v1",
            original_old="def missing():\n    return 9",
            original_new="def missing():\n    return 8",
            full_source="def value():\n    return 1\n",
        )
        == ()
    )


# --------------------------------------------------------------------------
# Response contract
# --------------------------------------------------------------------------


def test_provider_response_cannot_carry_old_text(tmp_path):
    """Requirements 4 and 15: ``old`` is unrepresentable for replace repairs."""

    _fixture, plan, materialization, findings = _attempt_context(tmp_path)
    prompt = build_operation_repair_prompt(
        task_constraints="ignored",
        original_plan=plan,
        rejected_findings=findings,
        source_materialization=materialization,
        project_dir=tmp_path,
    )
    envelope = json.loads(prompt)
    schema_entry = envelope["response_schema"]["repairs"][0]
    assert set(schema_entry) == {
        "step_number",
        "operation_index",
        "anchor_id",
        "new",
    }
    assert "old" not in schema_entry
    # The stale anchor the model copied verbatim in 32N-3C is withheld.
    assert (
        "old" not in envelope["rejected_operations"][0]["original_rejected_operation"]
    )
    assert [
        anchor["anchor_id"]
        for anchor in envelope["rejected_operations"][0]["authorized_anchors"]
    ] == ["anchor-2-1-1", "anchor-2-1-2"]
    for line in (
        "Do not reconstruct, paraphrase, widen or normalize source anchors.",
        "Select exactly one supplied anchor_id.",
        "Return replacement text only.",
        "Whitespace in the supplied source is authoritative.",
    ):
        assert line in envelope["instruction"]

    parsed = parse_operation_repair_response(
        _anchored(2, 1, "anchor-2-1-1", MINIMAL_ANCHOR_NEW)
    )
    assert isinstance(parsed.repairs[0], AnchoredRepairEntry)
    assert not hasattr(parsed.repairs[0], "old")

    with pytest.raises(OperationRepairError, match="invalid operation repair response"):
        parse_operation_repair_response(
            json.dumps(
                {
                    "repairs": [
                        {
                            "step_number": 2,
                            "operation_index": 1,
                            "anchor_id": "anchor-2-1-1",
                            "old": N3_RECONSTRUCTED_OLD,
                            "new": MINIMAL_ANCHOR_NEW,
                        }
                    ]
                }
            )
        )


def test_provider_supplied_old_text_is_rejected_for_replace_operations(tmp_path):
    """Requirement 15: the deployed 32N-3C response shape no longer merges."""

    fixture, plan, materialization, findings = _attempt_context(tmp_path)
    response = json.dumps(
        {
            "repairs": [
                {
                    "step_number": 2,
                    "operation_index": 1,
                    "replacement_operation": {
                        "op": "replace_in_file",
                        "path": TARGET,
                        "old": N3_RECONSTRUCTED_OLD,
                        "new": "anything",
                    },
                }
            ]
        }
    )
    with pytest.raises(OperationRepairError, match="must cite an authorized anchor_id"):
        merge_and_validate_operation_repairs(
            original_plan=plan,
            rejected_findings=findings,
            response_text=response,
            source_materialization=materialization,
            project_dir=tmp_path,
            validate_complete_plan=_validate(fixture, tmp_path, materialization),
        )


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("anchor_id", "new_text", "expected_old"),
    [
        ("anchor-2-1-1", MINIMAL_ANCHOR_NEW, EXACT_MINIMAL_ANCHOR),
    ],
)
def test_selected_anchor_reconstructs_a_valid_typed_operation(
    tmp_path, anchor_id, new_text, expected_old
):
    """Requirements 5, 6, 7 and 8."""

    fixture, plan, materialization, findings = _attempt_context(tmp_path)
    result = merge_and_validate_operation_repairs(
        original_plan=plan,
        rejected_findings=findings,
        response_text=_anchored(2, 1, anchor_id, new_text),
        source_materialization=materialization,
        project_dir=tmp_path,
        validate_complete_plan=_validate(fixture, tmp_path, materialization),
    )
    repaired = result.plan[1]["ops"][0]
    assert repaired == {
        "op": "replace_in_file",
        "path": TARGET,
        "old": expected_old,
        "new": new_text,
    }
    assert result.validator_verdict.accepted
    for step_index, step in enumerate(plan):
        for operation_index, operation in enumerate(step.get("ops", []), start=1):
            if (step["step_number"], operation_index) == (2, 1):
                continue
            assert _sha(result.plan[step_index]["ops"][operation_index - 1]) == _sha(
                operation
            )


def test_retained_attempt_reconstruction_is_twice_byte_identical(tmp_path):
    """Attempt 9 reconstructs deterministically under the anchored contract."""

    fixture, plan, materialization, findings = _attempt_context(tmp_path)
    runs = []
    prompts = []
    for _ in range(2):
        prompts.append(
            build_operation_repair_prompt(
                task_constraints=fixture["task_description"],
                original_plan=plan,
                rejected_findings=findings,
                source_materialization=materialization,
                project_dir=tmp_path,
            )
        )
        result = merge_and_validate_operation_repairs(
            original_plan=plan,
            rejected_findings=findings,
            response_text=_anchored(2, 1, "anchor-2-1-1", MINIMAL_ANCHOR_NEW),
            source_materialization=materialization,
            project_dir=tmp_path,
            validate_complete_plan=_validate(fixture, tmp_path, materialization),
        )
        assert result.validator_verdict.accepted, result.validator_verdict.reasons
        runs.append(_canonical(result.plan))
    assert prompts[0] == prompts[1]
    assert runs[0] == runs[1]


def test_attempt7_is_deterministically_ineligible_and_falls_back(tmp_path):
    """Requirement 19: an unanchorable operation refuses, it does not guess.

    Attempt 7's rejected ``old`` was fabricated whole — its first line does not
    occur in the file at all — so no exact anchor exists.  The lane must decline
    and leave the pre-existing complete-plan repair path to handle it.
    """

    _fixture, plan, materialization, findings = _attempt_context(tmp_path, "attempt7")
    route = select_operation_repair_route(
        findings=findings,
        source_materialization=materialization,
        project_dir=tmp_path,
    )
    assert route.lane == "operation_level"
    messages = []
    for _ in range(2):
        with pytest.raises(OperationRepairError) as excinfo:
            build_operation_repair_prompt(
                task_constraints="ignored",
                original_plan=plan,
                rejected_findings=findings,
                source_materialization=materialization,
                project_dir=tmp_path,
            )
        messages.append(str(excinfo.value))
    assert messages[0] == messages[1]
    assert "no exact source anchor is derivable" in messages[0]


# --------------------------------------------------------------------------
# Fail-closed invariants
# --------------------------------------------------------------------------


def _constant_anchor(anchor: SourceAnchor):
    def _derive(**_kwargs):
        return (anchor,)

    return _derive


def _merge_attempt9(tmp_path, response_text, **overrides):
    fixture, plan, materialization, findings = _attempt_context(tmp_path)
    return merge_and_validate_operation_repairs(
        original_plan=overrides.get("plan", plan),
        rejected_findings=findings,
        response_text=response_text,
        source_materialization=materialization,
        project_dir=tmp_path,
        validate_complete_plan=_validate(fixture, tmp_path, materialization),
    )


def test_unknown_anchor_rejects(tmp_path):
    with pytest.raises(OperationRepairError, match="unknown repair anchor"):
        _merge_attempt9(tmp_path, _anchored(2, 1, "anchor-9-9-9", MINIMAL_ANCHOR_NEW))


def test_anchor_from_another_operation_or_path_rejects(tmp_path, monkeypatch):
    """Requirements 10 and 11."""

    _fixture, plan, materialization, findings = _attempt_context(tmp_path)
    registry = build_operation_anchor_registry(
        original_plan=plan,
        rejected_findings=findings,
        source_materialization=materialization,
        project_dir=tmp_path,
    )
    genuine = registry.by_identity[(2, 1)][0]

    foreign_operation = dataclasses.replace(
        genuine, anchor_id="anchor-2-1-1", step_number=3, operation_index=4
    )
    foreign_path = dataclasses.replace(
        genuine, anchor_id="anchor-2-1-1", relative_path="app/time_utils.py"
    )

    for anchor, message in (
        (foreign_operation, "anchor belongs to another operation"),
        (foreign_path, "anchor belongs to another path"),
    ):
        monkeypatch.setattr(
            operation_repair_module,
            "derive_operation_anchors",
            _constant_anchor(anchor),
        )
        with pytest.raises(OperationRepairError, match=message):
            _merge_attempt9(
                tmp_path, _anchored(2, 1, "anchor-2-1-1", MINIMAL_ANCHOR_NEW)
            )


def test_source_version_change_rejects(tmp_path):
    """Requirement 12."""

    fixture, plan, materialization, findings = _attempt_context(tmp_path)
    target_path = tmp_path / TARGET
    target_path.write_text(
        target_path.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8"
    )
    with pytest.raises(OperationRepairError, match="not version fenced"):
        merge_and_validate_operation_repairs(
            original_plan=plan,
            rejected_findings=findings,
            response_text=_anchored(2, 1, "anchor-2-1-1", MINIMAL_ANCHOR_NEW),
            source_materialization=materialization,
            project_dir=tmp_path,
            validate_complete_plan=_validate(fixture, tmp_path, materialization),
        )


def test_anchor_absent_from_live_source_rejects(tmp_path, monkeypatch):
    """Requirement 13: the merge never trusts the registry without rechecking."""

    fabricated = SourceAnchor(
        anchor_id="anchor-2-1-1",
        step_number=2,
        operation_index=1,
        relative_path=TARGET,
        version_identity="",
        text="this text is not in the pinned source",
        derivation=DERIVATION_MINIMAL_DIVERGENT,
    )

    def _fake(**kwargs):
        return (
            dataclasses.replace(
                fabricated, version_identity=kwargs["version_identity"]
            ),
        )

    monkeypatch.setattr(operation_repair_module, "derive_operation_anchors", _fake)
    with pytest.raises(OperationRepairError, match="stale_old_text_absent"):
        _merge_attempt9(tmp_path, _anchored(2, 1, "anchor-2-1-1", MINIMAL_ANCHOR_NEW))


def test_ambiguous_anchor_rejects_at_merge(tmp_path, monkeypatch):
    """Requirement 14: uniqueness is rechecked against the fenced read."""

    def _fake(**kwargs):
        return (
            SourceAnchor(
                anchor_id="anchor-2-1-1",
                step_number=2,
                operation_index=1,
                relative_path=TARGET,
                version_identity=kwargs["version_identity"],
                text="    def ",
                derivation=DERIVATION_MINIMAL_DIVERGENT,
            ),
        )

    monkeypatch.setattr(operation_repair_module, "derive_operation_anchors", _fake)
    with pytest.raises(OperationRepairError, match="ambiguous or absent"):
        _merge_attempt9(tmp_path, _anchored(2, 1, "anchor-2-1-1", "    def "))


def test_changed_operation_type_rejects(tmp_path):
    """Requirement 16."""

    fixture, plan, materialization, findings = _attempt_context(tmp_path)
    response = json.dumps(
        {
            "repairs": [
                {
                    "step_number": 2,
                    "operation_index": 1,
                    "replacement_operation": {
                        "op": "write_file",
                        "path": TARGET,
                        "content": "rewritten",
                    },
                }
            ]
        }
    )
    with pytest.raises(OperationRepairError, match="must cite an authorized anchor_id"):
        merge_and_validate_operation_repairs(
            original_plan=plan,
            rejected_findings=findings,
            response_text=response,
            source_materialization=materialization,
            project_dir=tmp_path,
            validate_complete_plan=_validate(fixture, tmp_path, materialization),
        )


def test_duplicate_and_omitted_repairs_reject(tmp_path):
    """Requirement 17."""

    duplicated = json.dumps(
        {
            "repairs": [
                {
                    "step_number": 2,
                    "operation_index": 1,
                    "anchor_id": "anchor-2-1-1",
                    "new": MINIMAL_ANCHOR_NEW,
                },
                {
                    "step_number": 2,
                    "operation_index": 1,
                    "anchor_id": "anchor-2-1-2",
                    "new": MINIMAL_ANCHOR_NEW,
                },
            ]
        }
    )
    with pytest.raises(OperationRepairError, match="duplicate operation repair"):
        parse_operation_repair_response(duplicated)

    with pytest.raises(OperationRepairError, match="repair identity mismatch"):
        _merge_attempt9(tmp_path, _anchored(1, 1, "anchor-2-1-1", MINIMAL_ANCHOR_NEW))


def test_anchored_entry_is_rejected_for_non_replace_operations(tmp_path):
    """Requirement 20: other operation types keep the typed contract."""

    target = "pkg/new.py"
    (tmp_path / "pkg").mkdir()
    materialization = materialize_planner_source_context(
        tmp_path,
        expected_paths=[target],
        task_description="Create pkg/new.py with the requested value.",
    )
    record = materialization.file_map()[target]
    plan = [
        {
            "step_number": 1,
            "description": "Create an authorized companion file",
            "commands": [],
            "ops": [{"op": "write_file", "path": target, "content": "VALUE = 1\n"}],
            "verification": "python3 -m py_compile pkg/new.py",
            "rollback": None,
            "expected_files": [target],
        }
    ]
    findings = [
        {
            "step_number": 1,
            "operation_index": 1,
            "relative_path": target,
            "failure_code": "missing_source_materialization",
            "visibility": "not_verified",
            "source_version_identity": record.version_identity,
        }
    ]
    registry = build_operation_anchor_registry(
        original_plan=plan,
        rejected_findings=findings,
        source_materialization=materialization,
        project_dir=tmp_path,
    )
    assert registry.by_identity[(1, 1)] == ()
    assert registry.by_id == {}

    typed = json.dumps(
        {
            "repairs": [
                {
                    "step_number": 1,
                    "operation_index": 1,
                    "replacement_operation": {
                        "op": "write_file",
                        "path": target,
                        "content": "VALUE = 2\n",
                    },
                }
            ]
        }
    )
    merged = merge_operation_repairs(
        original_plan=plan,
        rejected_operations=findings,
        repairs=parse_operation_repair_response(typed),
        source_materialization=materialization,
        project_dir=tmp_path,
    )
    assert merged[0]["ops"][0]["content"] == "VALUE = 2\n"

    with pytest.raises(OperationRepairError, match="only valid for replace_in_file"):
        merge_operation_repairs(
            original_plan=plan,
            rejected_operations=findings,
            repairs=parse_operation_repair_response(
                _anchored(1, 1, "anchor-1-1-1", "VALUE = 2\n")
            ),
            source_materialization=materialization,
            project_dir=tmp_path,
        )


class _NoopAsyncContext:
    async def __aenter__(self):
        return {}

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def test_no_second_provider_request_occurs(monkeypatch):
    """Requirement 18: the anchored lane still spends exactly one request."""

    calls = []

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return {"status": "completed", "output": ""}

    monkeypatch.setattr(
        PlannerService,
        "_openclaw_planning_lock_async",
        staticmethod(lambda: _NoopAsyncContext()),
    )
    result = PlannerService.repair_operations(
        runtime_service=Runtime(),
        repair_prompt="bounded anchored prompt",
        timeout_seconds=240,
    )
    assert len(calls) == 1
    options = calls[0][1]["invocation_options"]
    assert options.max_output_tokens == 2048
    assert options.temperature == 0.0
    assert result["operation_repair_provider_call_count"] == 1
