"""Phase 32N-3 red/green contract for bounded operation-level repair."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.operation_repair import (
    MAX_REJECTED_OPERATIONS,
    OperationRepairError,
    build_operation_repair_prompt,
    merge_and_validate_operation_repairs,
    merge_operation_repairs,
    parse_operation_repair_response,
    select_operation_repair_route,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.planning.slot_merge_operator import (
    SlotMergeInput,
    SlotMergeOperator,
)


TARGET = "pkg/current.py"
# The stale anchor drifts from the real file exactly the way Phase 32N-3C
# observed: the model dropped a blank line it could not recall.
STALE_OLD = "def value():\n    return 1\n"
CURRENT_OLD = "def value():\n\n    return 1\n"
REPLACEMENT_NEW = "def value():\n\n    return 2\n"
# The anchors Phase 32N-4 derives from CURRENT_OLD, smallest first.
MINIMAL_ANCHOR_ID = "anchor-1-1-1"
MINIMAL_ANCHOR_OLD = "    return 1"
MINIMAL_ANCHOR_NEW = "    return 2"


def _operation_repair_module():
    return importlib.import_module(
        "app.services.orchestration.planning.operation_repair"
    )


def _plan(old: str = STALE_OLD) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Update the existing value helper",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": TARGET,
                    "old": old,
                    "new": REPLACEMENT_NEW,
                }
            ],
            "verification": "python3 -m py_compile pkg/current.py",
            "rollback": None,
            "expected_files": [TARGET],
        },
        {
            "step_number": 2,
            "description": "Create an authorized companion file",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "pkg/new.py",
                    "content": "VALUE = 2\n",
                }
            ],
            "verification": "python3 -m py_compile pkg/new.py",
            "rollback": None,
            "expected_files": ["pkg/new.py"],
        },
    ]


def _repair_payload() -> str:
    return json.dumps(
        {
            "repairs": [
                {
                    "step_number": 1,
                    "operation_index": 1,
                    "anchor_id": MINIMAL_ANCHOR_ID,
                    "new": MINIMAL_ANCHOR_NEW,
                }
            ]
        }
    )


def _materialization(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "current.py").write_text(CURRENT_OLD, encoding="utf-8")
    return materialize_planner_source_context(
        tmp_path,
        expected_paths=[TARGET, "pkg/new.py"],
        task_description=(
            "Update pkg/current.py and create pkg/new.py with the requested value."
        ),
    )


def _finding(materialization, *, step_number=1, operation_index=1, path=TARGET):
    record = materialization.file_map()[path]
    return {
        "step_number": step_number,
        "operation_index": operation_index,
        "relative_path": path,
        "failure_code": "stale_old_text_absent_from_current_source",
        "visibility": "not_verified",
        "source_version_identity": record.version_identity,
    }


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def test_strict_operation_repair_schema_and_parser():
    module = _operation_repair_module()
    parsed = module.parse_operation_repair_response(_repair_payload())
    assert parsed.repairs[0].step_number == 1


def test_validator_preserves_structured_operation_identity(tmp_path):
    materialization = _materialization(tmp_path)
    plan = _plan()
    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Update pkg/current.py and create pkg/new.py",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        source_materialization=materialization,
    )

    findings = verdict.details["source_operation_findings"]
    assert findings == [
        {
            "step_number": 1,
            "operation_index": 1,
            "relative_path": TARGET,
            "failure_code": "stale_old_text_absent_from_current_source",
            "visibility": "not_verified",
            "visible_text_verified": False,
            "full_file_same_version_verified": False,
            "source_version_identity": findings[0]["source_version_identity"],
        }
    ]


def test_strict_operation_merger_replaces_one_operation(tmp_path):
    module = _operation_repair_module()
    materialization = _materialization(tmp_path)
    merged = module.merge_operation_repairs(
        original_plan=_plan(),
        rejected_operations=[
            {
                "step_number": 1,
                "operation_index": 1,
                "relative_path": TARGET,
                "failure_code": "stale_old_text_absent_from_current_source",
                "visibility": "not_verified",
                "source_version_identity": materialization.file_map()[
                    TARGET
                ].version_identity,
            }
        ],
        repairs=module.parse_operation_repair_response(_repair_payload()),
        source_materialization=materialization,
        project_dir=tmp_path,
    )
    assert merged[0]["ops"][0]["old"] == MINIMAL_ANCHOR_OLD


def test_routing_can_choose_operation_level_lane(tmp_path):
    module = _operation_repair_module()
    materialization = _materialization(tmp_path)
    decision = module.select_operation_repair_route(
        findings=[
            {
                "step_number": 1,
                "operation_index": 1,
                "relative_path": TARGET,
                "failure_code": "stale_old_text",
                "visibility": "full_file_verified",
                "source_version_identity": materialization.file_map()[
                    TARGET
                ].version_identity,
            }
        ],
        source_materialization=materialization,
        project_dir=tmp_path,
    )
    assert decision.lane == "operation_level"


def test_path_merges_and_revalidates_one_operation(tmp_path):
    module = _operation_repair_module()
    materialization = _materialization(tmp_path)
    result = module.merge_and_validate_operation_repairs(
        original_plan=_plan(),
        rejected_findings=[
            {
                "step_number": 1,
                "operation_index": 1,
                "relative_path": TARGET,
                "failure_code": "stale_old_text",
                "visibility": "full_file_verified",
                "source_version_identity": materialization.file_map()[
                    TARGET
                ].version_identity,
            }
        ],
        response_text=_repair_payload(),
        source_materialization=materialization,
        project_dir=tmp_path,
        validate_complete_plan=lambda plan: ValidatorService.validate_plan(
            plan,
            output_text=json.dumps(plan),
            task_prompt="Update pkg/current.py and create pkg/new.py",
            execution_profile="full_lifecycle",
            project_dir=tmp_path,
            source_materialization=materialization,
        ),
    )
    assert result.validator_verdict.accepted
    assert result.plan[1] == _plan()[1]


def test_legacy_slot_merge_remains_disabled_and_unchanged():
    original = _plan()
    repaired = _plan(CURRENT_OLD)
    result = SlotMergeOperator().merge(
        SlotMergeInput(
            parent_a_plan=original,
            parent_b_plan=repaired,
            parent_a_reasons=(
                "replace_in_file old text not found in workspace in steps [1]",
            ),
            parent_b_reasons=(),
        )
    )
    assert result.merged_plan[0]["ops"][0]["old"] == STALE_OLD


def test_legacy_slot_merge_unsafe_authority_remains_unchanged():
    original = _plan()
    injected = copy.deepcopy(original)
    injected.append(
        {
            "step_number": 9,
            "description": "unauthorized",
            "commands": [],
            "ops": [
                {"op": "write_file", "path": "pkg/unauthorized.py", "content": "x"}
            ],
            "verification": "true",
            "rollback": None,
            "expected_files": ["pkg/unauthorized.py"],
        }
    )
    result = SlotMergeOperator().merge(
        SlotMergeInput(
            parent_a_plan=original,
            parent_b_plan=injected,
            parent_a_reasons=("step 1 stale",),
            parent_b_reasons=(),
        )
    )
    assert result.merged_plan[-1]["ops"][0]["path"] == "pkg/unauthorized.py"


@pytest.mark.parametrize(
    "payload",
    [
        '{"repairs":[],"extra":true}',
        '{"repairs":[]}',
        '```json\n{"repairs":[]}\n```',
        '{"repairs":[{"step_number":1,"operation_index":1,'
        '"replacement_operation":{"op":"shell","path":"pkg/current.py"}}]}',
        '{"repairs":[{"step_number":1,"operation_index":1,"description":"x",'
        '"replacement_operation":{"op":"replace_in_file","path":"pkg/current.py",'
        '"old":"a","new":"b"}}]}',
        '{"repairs":[{"step_number":1,"operation_index":1,'
        '"replacement_operation":{"op":"replace_in_file","path":"pkg/current.py",'
        '"old":"a","new":"b"}},{"step_number":1,"operation_index":1,'
        '"replacement_operation":{"op":"replace_in_file","path":"pkg/current.py",'
        '"old":"a","new":"b"}}]}',
    ],
)
def test_parser_rejects_non_exact_response_contract(payload):
    with pytest.raises(OperationRepairError):
        parse_operation_repair_response(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda body: body["repairs"][0].update(step_number=9), "identity mismatch"),
        (
            lambda body: body["repairs"][0].update(operation_index=9),
            "identity mismatch",
        ),
        (
            lambda body: body["repairs"][0].update(anchor_id="anchor-1-1-9"),
            "unknown repair anchor",
        ),
        (
            lambda body: body["repairs"][0].update(old="absent text"),
            "schema",
        ),
        (
            # A typed operation, the pre-32N-4 shape, no longer merges: the
            # provider may not author ``old`` for a replace_in_file repair.
            lambda body: body["repairs"].__setitem__(
                0,
                {
                    "step_number": 1,
                    "operation_index": 1,
                    "replacement_operation": {
                        "op": "replace_in_file",
                        "path": TARGET,
                        "old": CURRENT_OLD,
                        "new": REPLACEMENT_NEW,
                    },
                },
            ),
            "must cite an authorized anchor_id",
        ),
        (
            lambda body: body["repairs"].__setitem__(
                0,
                {
                    "step_number": 1,
                    "operation_index": 1,
                    "replacement_operation": {
                        "op": "write_file",
                        "path": TARGET,
                        "content": "x",
                    },
                },
            ),
            "must cite an authorized anchor_id",
        ),
    ],
)
def test_merger_rejects_wrong_identity_path_type_and_stale_old(
    tmp_path, mutate, message
):
    materialization = _materialization(tmp_path)
    body = json.loads(_repair_payload())
    mutate(body)
    with pytest.raises(OperationRepairError, match=message):
        merge_operation_repairs(
            original_plan=_plan(),
            rejected_operations=[_finding(materialization)],
            repairs=parse_operation_repair_response(json.dumps(body)),
            source_materialization=materialization,
            project_dir=tmp_path,
        )


def test_merger_rejects_missing_and_extra_accepted_operation_repairs(tmp_path):
    materialization = _materialization(tmp_path)
    required = [_finding(materialization)]
    missing = {
        "repairs": [
            {
                "step_number": 2,
                "operation_index": 1,
                "replacement_operation": {
                    "op": "write_file",
                    "path": "pkg/new.py",
                    "content": "VALUE = 3\n",
                },
            }
        ]
    }
    with pytest.raises(OperationRepairError, match="identity mismatch"):
        merge_operation_repairs(
            original_plan=_plan(),
            rejected_operations=required,
            repairs=parse_operation_repair_response(json.dumps(missing)),
            source_materialization=materialization,
            project_dir=tmp_path,
        )
    extra = json.loads(_repair_payload())
    extra["repairs"].append(missing["repairs"][0])
    with pytest.raises(OperationRepairError, match="identity mismatch"):
        merge_operation_repairs(
            original_plan=_plan(),
            rejected_operations=required,
            repairs=parse_operation_repair_response(json.dumps(extra)),
            source_materialization=materialization,
            project_dir=tmp_path,
        )


def test_merge_preserves_accepted_bytes_and_all_ordering(tmp_path):
    materialization = _materialization(tmp_path)
    original = _plan()
    original_step_hashes = [_sha(step) for step in original]
    original_operation_hashes = [
        _sha(operation) for step in original for operation in step["ops"]
    ]
    merged = merge_operation_repairs(
        original_plan=original,
        rejected_operations=[_finding(materialization)],
        repairs=parse_operation_repair_response(_repair_payload()),
        source_materialization=materialization,
        project_dir=tmp_path,
    )

    assert [step["step_number"] for step in merged] == [1, 2]
    assert [len(step["ops"]) for step in merged] == [1, 1]
    assert _sha(merged[1]) == original_step_hashes[1]
    assert _sha(merged[1]["ops"][0]) == original_operation_hashes[1]
    assert _sha(merged[0]) != original_step_hashes[0]
    assert _canonical(original) == _canonical(_plan())


def test_version_mismatch_rejects_before_merge(tmp_path):
    materialization = _materialization(tmp_path)
    (tmp_path / TARGET).write_text("def value():\n    return 99\n", encoding="utf-8")
    with pytest.raises(OperationRepairError, match="version"):
        merge_operation_repairs(
            original_plan=_plan(),
            rejected_operations=[_finding(materialization)],
            repairs=parse_operation_repair_response(_repair_payload()),
            source_materialization=materialization,
            project_dir=tmp_path,
        )


def test_new_file_creation_authorization_remains_enforced(tmp_path):
    materialization = _materialization(tmp_path)
    original = [_plan()[1]]
    original[0]["step_number"] = 1
    record = materialization.file_map()["pkg/new.py"]
    finding = {
        "step_number": 1,
        "operation_index": 1,
        "relative_path": "pkg/new.py",
        "failure_code": "new_file_write_requires_repair",
        "visibility": "not_verified",
        "source_version_identity": None,
    }
    response = parse_operation_repair_response(
        json.dumps(
            {
                "repairs": [
                    {
                        "step_number": 1,
                        "operation_index": 1,
                        "replacement_operation": {
                            "op": "write_file",
                            "path": "pkg/new.py",
                            "content": "VALUE = 3\n",
                        },
                    }
                ]
            }
        )
    )
    assert record.creation_authorized
    unauthorized_record = dataclasses.replace(record, creation_authorized=False)
    unauthorized = dataclasses.replace(
        materialization,
        files=tuple(
            unauthorized_record if item.relative_path == "pkg/new.py" else item
            for item in materialization.files
        ),
    )
    with pytest.raises(OperationRepairError, match="creation is not authorized"):
        merge_operation_repairs(
            original_plan=original,
            rejected_operations=[finding],
            repairs=response,
            source_materialization=unauthorized,
            project_dir=tmp_path,
        )


def test_routing_falls_back_for_structural_unattributed_and_excessive_findings(
    tmp_path,
):
    materialization = _materialization(tmp_path)
    finding = _finding(materialization)
    assert (
        select_operation_repair_route(
            findings=[finding],
            source_materialization=materialization,
            project_dir=tmp_path,
            structural_failure=True,
        ).reason
        == "structural_plan_failure"
    )
    assert (
        select_operation_repair_route(
            findings=[{"failure_code": "stale"}],
            source_materialization=materialization,
            project_dir=tmp_path,
        ).reason
        == "unattributed_finding"
    )
    excessive = [
        {**finding, "step_number": index + 1}
        for index in range(MAX_REJECTED_OPERATIONS + 1)
    ]
    assert (
        select_operation_repair_route(
            findings=excessive,
            source_materialization=materialization,
            project_dir=tmp_path,
        ).reason
        == "operation_count_exceeds_bound"
    )
    (tmp_path / TARGET).write_text("changed after materialization\n", encoding="utf-8")
    assert (
        select_operation_repair_route(
            findings=[finding],
            source_materialization=materialization,
            project_dir=tmp_path,
        ).reason
        == "source_version_mismatch"
    )


def test_prompt_contains_only_rejected_operation_and_bounded_source(tmp_path):
    materialization = _materialization(tmp_path)
    prompt = build_operation_repair_prompt(
        task_constraints="Update only pkg/current.py.",
        original_plan=_plan(),
        rejected_findings=[_finding(materialization)],
        source_materialization=materialization,
        project_dir=tmp_path,
    )
    envelope = json.loads(prompt)
    assert len(envelope["rejected_operations"]) == 1
    assert envelope["rejected_operations"][0]["identity"] == {
        "step_number": 1,
        "operation_index": 1,
        "relative_path": TARGET,
    }
    assert "pkg/new.py" not in prompt
    assert _canonical(_plan()) not in prompt
    assert len(prompt) < 12_000


def test_invalid_merged_plan_fails_closed(tmp_path):
    materialization = _materialization(tmp_path)
    with pytest.raises(OperationRepairError, match="failed validation"):
        merge_and_validate_operation_repairs(
            original_plan=_plan(),
            rejected_findings=[_finding(materialization)],
            response_text=_repair_payload(),
            source_materialization=materialization,
            project_dir=tmp_path,
            validate_complete_plan=lambda plan: SimpleNamespace(accepted=False),
        )


def test_operation_provider_invocation_is_exactly_one_call(monkeypatch):
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
        repair_prompt="bounded prompt",
        timeout_seconds=240,
    )
    assert len(calls) == 1
    options = calls[0][1]["invocation_options"]
    assert options.max_output_tokens == 2048
    assert options.temperature == 0.0
    assert result["operation_repair_provider_call_count"] == 1


class _NoopAsyncContext:
    async def __aenter__(self):
        return {}

    async def __aexit__(self, exc_type, exc, traceback):
        return False
