"""PHASE35-PGP2 — provider-backed grounding acquisition trial.

The provider-free tests here assert the anti-cheating boundary: the trial
harness cannot perform semantic selection, and the model prompt cannot contain
the expected locator. The single provider-backed test exercises the configured
Planning model and is skipped when that model is not reachable.

No Plan is generated. No APA, C8, execution, ChangeSet, Controlled Apply, or
promotion is reached.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.tests.prototypes.planning_grounding import protocol as P
from app.tests.prototypes.planning_grounding.harness import run_grounding
from app.tests.prototypes.planning_grounding.model_adapter import (
    ACTION_MENU,
    SYSTEM_PROMPT,
    ModelGroundingAdapter,
    build_user_prompt,
    parse_model_reply,
)
from app.tests.prototypes.planning_grounding.provider_client import (
    planning_provider_identity,
    planning_provider_reachable,
)
from app.tests.prototypes.planning_grounding.structure import symbol_regions
from app.tests.prototypes.planning_grounding.trial_harness import (
    REJECT_STRUCTURAL_RANKING_DISABLED,
    TrialHarness,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

# The Phase35-A3 / A3-R1 operator wording, verbatim. No path, symbol, route,
# snippet, line number, byte offset, or operation type.
TRIAL_TASK_TEXT = (
    "Improve project browsing so users can choose which lifecycle states appear "
    "in the project list. The default view must continue to show only active, "
    "non-deleted projects. A caller may request active projects, retired "
    "projects, or both; retired results must remain visible for recovery and "
    "audit without exposing deleted projects. Existing name search, ordering, "
    "and pagination behavior must continue to work with each selection. "
    "Unsupported state values must return a clear client error. Add automated "
    "coverage for the default behavior, each supported selection, invalid "
    "input, and the preserved search, ordering, and pagination behavior."
)

# Evaluator-only expected identity. This never enters a model request.
EXPECTED_PATH = "app/api/v1/endpoints/projects.py"
EXPECTED_SYMBOL = "get_projects"
EXPECTED_METHOD = "GET"
EXPECTED_ROUTE = "/projects"


def expected_region():
    """Recomputed from the current source, not from historical byte offsets."""

    raw = (REPO_ROOT / EXPECTED_PATH).read_bytes()
    matches = [
        region
        for region in symbol_regions(EXPECTED_PATH, raw)
        if region.name == EXPECTED_SYMBOL
    ]
    assert len(matches) == 1
    return matches[0]


def _full_budget() -> P.Budget:
    return P.Budget(
        requests_remaining=P.MAX_GROUNDING_REQUESTS,
        evidence_bytes_remaining=P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES,
        files_remaining=P.MAX_DISTINCT_FILES,
        regions_remaining=P.MAX_PRIMARY_REGIONS,
    )


def _turn_one_prompt() -> str:
    harness = TrialHarness(REPO_ROOT)
    from app.tests.prototypes.planning_grounding.harness import derive_orientation

    orientation = derive_orientation(REPO_ROOT, TRIAL_TASK_TEXT)
    request = P.GroundingRequest(
        task_text=TRIAL_TASK_TEXT,
        task_text_provenance=P.PROVENANCE_OPERATOR_TASK,
        orientation=orientation,
        prior_observation_ids=(),
        remaining_budget=_full_budget(),
        turn=1,
    )
    assert harness is not None
    return build_user_prompt(request, ())


# --------------------------------------------------------------------------
# Anti-cheating boundary (provider-free)
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_trial_harness_refuses_the_task_term_ranking_survey():
    """PGP1's fixture-tuned ranking must not be the semantic decision-maker."""

    harness = TrialHarness(REPO_ROOT)
    survey = P.GroundingAction(
        kind=P.ACTION_SEARCH_TEXT,
        mode=P.SEARCH_MODE_STRUCTURAL,
        terms=("projects", "retired"),
        scope_paths=(EXPECTED_PATH,),
    )
    assert (
        harness.validate(survey, _full_budget()) == REJECT_STRUCTURAL_RANKING_DISABLED
    )

    with pytest.raises(AssertionError):
        harness._structural_candidates(survey)


@pytest.mark.unit
def test_trial_literal_search_is_ordered_by_file_and_byte_not_relevance(tmp_path):
    module = (
        "def zeta_helper():\n"
        "    marker_literal()\n"
        "    return 1\n"
        "\n"
        "\n"
        "def alpha_helper():\n"
        "    marker_literal()\n"
        "    return 2\n"
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(module)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    harness = TrialHarness(tmp_path)
    observations = harness.execute(
        P.GroundingAction(
            kind=P.ACTION_SEARCH_TEXT,
            mode=P.SEARCH_MODE_LITERAL,
            query="marker_literal()",
            scope_paths=("pkg/mod.py",),
            max_results=4,
        ),
        _full_budget(),
        set(),
        turn=1,
        next_index=1,
    )
    names = [item.structural_identity.name for item in observations]
    assert names == ["zeta_helper", "alpha_helper"]
    assert observations[0].notes["selection_strategy"] == (
        "literal_occurrence_in_file_order"
    )


@pytest.mark.unit
def test_model_prompt_contains_no_expected_target_metadata():
    prompt = _turn_one_prompt()
    region = expected_region()

    forbidden = [
        "get_projects",
        "GET /projects",
        "@router.get",
        "APIRouter",
        "def get_projects",
        "retired_at",
        str(region.start_byte),
        str(region.end_byte),
        f"{region.start_line}-{region.end_line}",
        "positive control",
        "expected",
    ]
    for needle in forbidden:
        assert needle not in prompt, f"prompt leaks expected target metadata: {needle}"

    # The task text is present verbatim and unmodified.
    assert TRIAL_TASK_TEXT in prompt
    # Orientation is path-level only: the candidate file may appear, the
    # implementation region may not.
    assert EXPECTED_PATH in prompt


@pytest.mark.unit
def test_static_prompt_scaffolding_is_repository_agnostic():
    scaffolding = SYSTEM_PROMPT + ACTION_MENU
    for needle in ["project", "route /", "get_", "endpoints", "retired", "fastapi"]:
        assert needle.lower() not in scaffolding.lower(), needle


@pytest.mark.unit
def test_model_reply_parsing_covers_the_whole_action_and_decision_surface():
    from app.tests.prototypes.planning_grounding.harness import derive_orientation

    orientation = derive_orientation(REPO_ROOT, TRIAL_TASK_TEXT)

    action, error = parse_model_reply(
        '{"action": "inspect_route", "route_method": "GET", "route_path": "/x",'
        ' "scope_paths": ["app/api/v1/endpoints/projects.py"]}',
        orientation,
    )
    assert error is None and action.kind == P.ACTION_INSPECT_ROUTE

    action, error = parse_model_reply(
        '{"action": "inspect_symbol", "symbol_name": "foo"}', orientation
    )
    assert error is None and action.scope_paths == orientation.paths

    decision, error = parse_model_reply(
        '{"decision": "INSUFFICIENT", "why": "no evidence"}', orientation
    )
    assert error is None and decision.decision == P.DECISION_INSUFFICIENT

    parsed, error = parse_model_reply("I will look at the projects file.", orientation)
    assert parsed is None and error is not None


@pytest.mark.unit
def test_trial_creates_no_plan_or_mutation_authority_surface():
    assert not hasattr(TrialHarness, "apply")
    assert not hasattr(TrialHarness, "write")
    fields = set(P.GroundingAction.__dataclass_fields__)
    assert not (fields & {"write", "replace", "content", "patch", "operation"})


# --------------------------------------------------------------------------
# Provider-backed trial
# --------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not planning_provider_reachable(),
    reason="configured Planning model is not reachable; trial cannot be exercised",
)
def test_real_model_acquires_the_positive_control_region():
    region = expected_region()
    adapter = ModelGroundingAdapter()
    outcome = run_grounding(
        REPO_ROOT, TRIAL_TASK_TEXT, adapter, harness=TrialHarness(REPO_ROOT)
    )

    identity = planning_provider_identity()
    assert identity.model, "no Planning model configured"

    # The model, not the harness, chose the navigation.
    assert outcome.requests, "the model made no inspection request"
    first = outcome.requests[0]
    assert first.kind in {
        P.ACTION_SEARCH_TEXT,
        P.ACTION_INSPECT_SYMBOL,
        P.ACTION_INSPECT_ROUTE,
    }

    assert outcome.decision.decision == P.DECISION_SUFFICIENT
    claim = outcome.decision.sufficiency
    assert claim is not None and claim.cited_observation_ids

    by_id = {item.observation_id: item for item in outcome.observations}
    cited = [by_id[item] for item in claim.cited_observation_ids]

    def resolves_to_expected(observation) -> bool:
        structural = observation.structural_identity
        return (
            observation.source_path == EXPECTED_PATH
            and structural is not None
            and structural.name == EXPECTED_SYMBOL
            and structural.http_method == EXPECTED_METHOD
            and structural.route_path == EXPECTED_ROUTE
            and structural.region_start_byte == region.start_byte
            and structural.region_end_byte == region.end_byte
        )

    assert any(resolves_to_expected(item) for item in cited), (
        "cited evidence does not resolve to the current GET /projects -> "
        f"get_projects region ({region.start_byte}-{region.end_byte})"
    )
    # No unrelated observation is required to justify the final locator.
    assert all(resolves_to_expected(item) for item in cited)

    # Hard bounds intact.
    assert len(outcome.requests) <= P.MAX_GROUNDING_REQUESTS
    assert sum(item.byte_count for item in outcome.observations) <= (
        P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES
    )
    assert len({item.source_path for item in outcome.observations}) <= (
        P.MAX_DISTINCT_FILES
    )
    assert len(outcome.observations) <= P.MAX_PRIMARY_REGIONS

    # No authority was created.
    assert outcome.plan_created is False
    assert outcome.apa_created is False
    assert outcome.mutation_authority_granted is False
    assert outcome.controlled_apply_reached is False
