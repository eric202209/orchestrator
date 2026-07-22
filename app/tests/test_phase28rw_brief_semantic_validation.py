"""Phase 28R-W semantic contract and validator reachability tests."""

from __future__ import annotations

import copy

import pytest

from app.services.planning.input_manifest import build_input_manifest
from app.services.planning.planning_brief import validate_planning_brief
from app.services.planning.planning_brief_stage import (
    PlanningBriefProviderInput,
    PlanningBriefProviderOutputError,
    build_planning_brief_request,
    canonicalize_planning_brief_candidate,
    parse_planning_brief_candidate,
)
from app.services.planning.provider_contract import (
    BRIEF_SEMANTIC_AUTHORITY_INSTRUCTIONS,
    build_planning_brief_schema_contract,
)


def _manifest():
    return build_input_manifest(
        session_id=280,
        session_generation_id="phase28rw-generation",
        planning_request={
            "message_id": 1,
            "role": "user",
            "content": "Implement the bounded planning change.",
        },
        clarification_messages=[],
        project_metadata={"project_id": 280, "name": "bounded"},
        project_rules="Preserve existing behavior.",
        repository={"available": False, "workspace": "bounded"},
        runtime_configuration={"provider": "direct", "model": "test"},
        stage_configuration={"stages": [{"identifier": "planning_brief"}]},
        manifest_built_at="2026-07-22T00:00:00+00:00",
    )


def _candidate(manifest, *, requirement_type="functional"):
    source_id = manifest.sources[0].source_id
    return {
        "objective": {
            "statement": "Implement the bounded planning change.",
            "source_refs": [source_id],
        },
        "background": [],
        "scope": [
            {
                "classification": "in_scope",
                "statement": "The bounded planning change.",
                "source_refs": [source_id],
            }
        ],
        "requirements": [
            {
                "type": requirement_type,
                "statement": "Preserve the existing behavior.",
                "priority": "required",
                "source_refs": [source_id],
            }
        ],
        "constraints": [],
        "acceptance_criteria": [
            {
                "statement": "The bounded change is verified.",
                "verification_method": "Run the focused test.",
                "source_requirement_ids": ["requirements[0]"],
                "criticality": "required",
            }
        ],
        "architecture_context": [],
        "interface_contracts": [],
        "implementation_strategy": [
            {
                "statement": "Implement the bounded change.",
                "source_refs": [source_id],
                "requirement_ids": ["requirements[0]"],
                "constraint_ids": [],
            }
        ],
        "validation_strategy": [
            {
                "statement": "Run the focused validation.",
                "source_refs": [source_id],
                "acceptance_criterion_ids": ["acceptance_criteria[0]"],
                "requirement_ids": ["requirements[0]"],
            }
        ],
        "assumptions": [],
        "risks": [],
        "unresolved_questions": [],
        "operator_decisions": [],
    }


def _validated(candidate, manifest):
    brief = canonicalize_planning_brief_candidate(
        parse_planning_brief_candidate(candidate), manifest
    )
    return brief, validate_planning_brief(brief, input_manifest=manifest)


def test_contract_exposes_conditional_semantics_for_repeated_rules():
    contract = build_planning_brief_schema_contract()
    fields = contract["top_level"]["fields"]

    assert fields["requirements"]["record"]["fields"]["quality_attribute"][
        "conditional_semantics"
    ]
    assert fields["constraints"]["record"]["fields"]["enforcement"][
        "conditional_semantics"
    ]
    assert fields["unresolved_questions"]["record"]["fields"][
        "temporary_assumption_id"
    ]["conditional_semantics"]
    assert fields["operator_decisions"]["record"]["source_authority"]
    assert contract["semantic_authority"]


def test_rendered_prompt_places_semantic_authority_before_schema_contract():
    request = build_planning_brief_request(
        PlanningBriefProviderInput(
            manifest_id="manifest:phase28rw",
            manifest_hash="a" * 64,
            manifest_schema_version="protocol-v2-input-manifest/1.0",
            sources=(),
            stage_configuration={},
        )
    )
    assert request.prompt.count("SEMANTIC AUTHORITY (STRICT):") == 1
    assert BRIEF_SEMANTIC_AUTHORITY_INSTRUCTIONS in request.prompt
    assert request.prompt.index(
        BRIEF_SEMANTIC_AUTHORITY_INSTRUCTIONS
    ) < request.prompt.index("COMPLETE RECORD-LEVEL SCHEMA CONTRACT:")
    assert "Do not invent a numeric threshold" in request.prompt


def test_valid_functional_brief_is_a_reachable_success_path():
    manifest = _manifest()
    _brief, acceptance = _validated(_candidate(manifest), manifest)
    assert acceptance.schema_valid
    assert acceptance.semantically_valid
    assert acceptance.protocol_acceptable


def test_quality_attribute_rule_accepts_valid_and_rejects_missing_attribute():
    manifest = _manifest()
    valid = _candidate(manifest, requirement_type="non_functional")
    valid["requirements"][0]["quality_attribute"] = "reliability"
    _brief, acceptance = _validated(valid, manifest)
    assert acceptance.semantically_valid

    invalid = _candidate(manifest, requirement_type="non_functional")
    _brief, acceptance = _validated(invalid, manifest)
    assert any(issue.code == "missing_quality_attribute" for issue in acceptance.errors)


def test_operator_decision_requires_operator_message_source():
    manifest = _manifest()
    valid = _candidate(manifest)
    valid["operator_decisions"] = [
        {
            "statement": "The operator selected the bounded approach.",
            "decision": "Use the bounded approach.",
            "source_refs": [manifest.sources[0].source_id],
        }
    ]
    _brief, acceptance = _validated(valid, manifest)
    assert acceptance.semantically_valid

    invalid = copy.deepcopy(valid)
    invalid["operator_decisions"][0]["source_refs"] = [manifest.sources[3].source_id]
    _brief, acceptance = _validated(invalid, manifest)
    assert any(issue.code == "operator_source_required" for issue in acceptance.errors)


def test_non_blocking_question_requires_linked_temporary_assumption():
    manifest = _manifest()
    valid = _candidate(manifest)
    source_id = manifest.sources[0].source_id
    valid["assumptions"] = [
        {
            "statement": "The bounded approach remains the operator intent.",
            "source_refs": [source_id],
            "confidence": "medium",
            "impact_if_false": "The operator must select another approach.",
        }
    ]
    valid["unresolved_questions"] = [
        {
            "statement": "Should the operator select another approach?",
            "classification": "non_blocking",
            "allowed_resolver_roles": ["operator"],
            "source_refs": [source_id],
            "temporary_assumption_id": "assumptions[0]",
        }
    ]
    _brief, acceptance = _validated(valid, manifest)
    assert acceptance.semantically_valid

    invalid = copy.deepcopy(valid)
    invalid["unresolved_questions"][0]["temporary_assumption_id"] = None
    _brief, acceptance = _validated(invalid, manifest)
    assert any(
        issue.code == "question_assumption_required" for issue in acceptance.errors
    )


def test_must_constraint_requires_more_than_model_review():
    manifest = _manifest()
    valid = _candidate(manifest)
    source_id = manifest.sources[0].source_id
    valid["constraints"] = [
        {
            "type": "architecture",
            "statement": "The change must preserve the existing boundary.",
            "severity": "must",
            "enforcement": "deterministic",
            "source_refs": [source_id],
            "applies_to_refs": ["objective"],
        }
    ]
    _brief, acceptance = _validated(valid, manifest)
    assert acceptance.semantically_valid

    invalid = copy.deepcopy(valid)
    invalid["constraints"][0]["enforcement"] = "model_review"
    _brief, acceptance = _validated(invalid, manifest)
    assert any(
        issue.code == "must_constraint_not_deterministic" for issue in acceptance.errors
    )


def test_source_references_cannot_be_fabricated():
    manifest = _manifest()
    invalid = _candidate(manifest)
    invalid["objective"]["source_refs"] = ["source:planning_request:" + "0" * 32]
    with pytest.raises(
        PlanningBriefProviderOutputError, match="unknown manifest source"
    ):
        canonicalize_planning_brief_candidate(
            parse_planning_brief_candidate(invalid), manifest
        )


def test_blocking_uncertainty_is_preserved_without_silent_assumption():
    manifest = _manifest()
    candidate = _candidate(manifest)
    source_id = manifest.sources[0].source_id
    candidate["unresolved_questions"] = [
        {
            "statement": "Which bounded approach should the operator choose?",
            "classification": "blocking",
            "allowed_resolver_roles": ["operator"],
            "source_refs": [source_id],
        }
    ]
    _brief, acceptance = _validated(candidate, manifest)
    assert acceptance.schema_valid
    assert acceptance.semantically_valid
    assert not acceptance.protocol_acceptable
