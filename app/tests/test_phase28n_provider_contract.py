from dataclasses import fields

from app.services.planning.planning_brief_stage import (
    PLANNING_BRIEF_CANDIDATE_FIELDS,
    PLANNING_BRIEF_CANDIDATE_RECORD_TYPES,
    parse_planning_brief_candidate,
)
from app.services.planning.provider_contract import (
    build_planning_brief_schema_contract,
    build_structured_task_plan_schema_contract,
)
from app.services.planning.structured_task_plan import (
    Dependency,
    EffortEstimate,
    ExecutionGroup,
    ExecutionProfile,
    IntentionalOmission,
    Task,
    Traceability,
    WorkItem,
)
from app.services.planning.structured_task_plan_stage import (
    STRUCTURED_TASK_PLAN_CANDIDATE_FIELDS,
    parse_structured_task_plan_candidate,
)


def _assert_record_coverage(contract, record_type):
    record = contract.get("record", contract)
    domain_fields = {item.name for item in fields(record_type)} - {"id"}
    described_fields = set(record["fields"])
    assert described_fields == domain_fields
    assert (
        set(record["required_fields"]) | set(record["optional_fields"]) == domain_fields
    )
    assert "id" in record["forbidden_fields"]


def test_planning_brief_contract_covers_every_parser_record_field():
    contract = build_planning_brief_schema_contract()
    top_level = contract["top_level"]
    assert top_level["required_fields"] == list(PLANNING_BRIEF_CANDIDATE_FIELDS)
    assert set(top_level["fields"]) == set(PLANNING_BRIEF_CANDIDATE_FIELDS)
    for collection, record_type in PLANNING_BRIEF_CANDIDATE_RECORD_TYPES.items():
        _assert_record_coverage(top_level["fields"][collection], record_type)

    acceptance = top_level["fields"]["acceptance_criteria"]["record"]["fields"]
    assumption = top_level["fields"]["assumptions"]["record"]["fields"]
    interface = top_level["fields"]["interface_contracts"]["record"]["fields"]
    assert "verification_method" in acceptance
    assert "impact_if_false" in assumption
    assert "change_permission" in interface
    assert set(contract["forbidden_legacy_fields"]) >= {
        "title",
        "description",
        "objectives",
        "deliverables",
        "timeline",
    }


def test_structured_task_plan_contract_covers_all_nested_parser_records():
    contract = build_structured_task_plan_schema_contract()
    assert contract["top_level"]["required_fields"] == list(
        STRUCTURED_TASK_PLAN_CANDIDATE_FIELDS
    )
    record_types = {
        "Task": Task,
        "Dependency": Dependency,
        "ExecutionGroup": ExecutionGroup,
        "IntentionalOmission": IntentionalOmission,
        "WorkItem": WorkItem,
        "Traceability": Traceability,
        "EffortEstimate": EffortEstimate,
        "ExecutionProfile": ExecutionProfile,
    }
    for name, record_type in record_types.items():
        _assert_record_coverage(contract["additional_record_types"][name], record_type)
    assert "TASK-NNN" in contract["forbidden_fields"]
    assert "topology" in contract["application_owned_fields"]
    assert "cycle" in contract["graph_constraints"]["dependencies"]


def test_strict_parsers_still_reject_legacy_and_application_owned_fields():
    try:
        parse_planning_brief_candidate({"title": "legacy"})
    except Exception as exc:
        assert "unknown fields" in str(exc)
    else:
        raise AssertionError("legacy Planning Brief shape was accepted")

    try:
        parse_structured_task_plan_candidate(
            {
                "tasks": [],
                "dependencies": [],
                "execution_groups": [],
                "intentional_omissions": [],
                "topology": {},
            }
        )
    except Exception as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("application-owned Task Plan field was accepted")
