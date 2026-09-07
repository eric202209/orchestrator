"""PHASE35-PGP1 — bounded pre-Plan grounding prototype tests.

Provider-free. No production behavior is exercised or changed: the only
production modules imported are `repository_orientation` (read-only) and
`path_authority` (transitively, read-only).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.tests.prototypes.planning_grounding import fixtures as F
from app.tests.prototypes.planning_grounding import protocol as P
from app.tests.prototypes.planning_grounding.adapter import GenericGroundingAdapter
from app.tests.prototypes.planning_grounding.harness import (
    GroundingAdapter,
    GroundingHarness,
    replay,
    run_grounding,
)
from app.tests.prototypes.planning_grounding.structure import symbol_regions

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _run(fixture: F.GroundingFixture) -> P.GroundingOutcome:
    return run_grounding(REPO_ROOT, fixture.task_text, GenericGroundingAdapter())


def _overlaps(observation: P.GroundingObservation, fixture: F.GroundingFixture) -> bool:
    identity = observation.structural_identity
    if identity is None or observation.source_path != fixture.expected_path:
        return False
    return any(
        not (identity.region_end_byte <= start or identity.region_start_byte >= end)
        for start, end in fixture.expected_regions
    )


def _temp_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def _full_budget() -> P.Budget:
    return P.Budget(
        requests_remaining=P.MAX_GROUNDING_REQUESTS,
        evidence_bytes_remaining=P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES,
        files_remaining=P.MAX_DISTINCT_FILES,
        regions_remaining=P.MAX_PRIMARY_REGIONS,
    )


class _RecordingAdapter(GroundingAdapter):
    """Wraps the real adapter and records every request it was handed."""

    def __init__(self) -> None:
        self.inner = GenericGroundingAdapter()
        self.requests: list[P.GroundingRequest] = []

    def propose(self, request, observations):
        self.requests.append(request)
        return self.inner.propose(request, observations)


class _AlwaysAsksAdapter(GroundingAdapter):
    def propose(self, request, observations):
        action = P.GroundingAction(
            kind=P.ACTION_SEARCH_TEXT,
            mode=P.SEARCH_MODE_LITERAL,
            query="def ",
            scope_paths=request.orientation.paths[:1],
            max_results=1,
        )
        if observations:
            return P.GroundingDecision(
                decision=P.DECISION_NEED_MORE_EVIDENCE,
                next_action=action,
            )
        return action


# --------------------------------------------------------------------------
# Part 8 — the four natural-language fixtures
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture", [F.CASE_A, F.CASE_B, F.CASE_C], ids=lambda f: f.name
)
def test_positive_fixtures_acquire_and_cite_the_relevant_region(fixture):
    outcome = _run(fixture)

    assert outcome.decision.decision == P.DECISION_SUFFICIENT
    claim = outcome.decision.sufficiency
    assert claim is not None

    cited = {item.observation_id: item for item in outcome.observations}
    relevant = [
        cited[item]
        for item in claim.cited_observation_ids
        if _overlaps(cited[item], fixture)
    ]
    assert relevant, (
        f"{fixture.name}: cited evidence does not cover the known relevant region "
        f"{fixture.expected_regions} of {fixture.expected_path}"
    )
    assert fixture.expected_path in claim.relevant_source_paths
    # The cited evidence names a structural locator, not a byte guess.
    assert all(":" in locator for locator in claim.structural_locators)
    assert any(
        item.structural_identity is not None
        and item.structural_identity.name in fixture.known_expected_symbols
        for item in relevant
    )


def test_negative_control_stops_insufficient_grounding():
    outcome = _run(F.CASE_D)

    assert outcome.decision.decision == P.DECISION_INSUFFICIENT
    assert outcome.decision.stop_reason == P.STOP_INSUFFICIENT_GROUNDING
    assert outcome.decision.sufficiency is None
    # No head fallback, no guessed locator, no fabricated sufficiency.
    assert not any(
        item.structural_identity is not None
        and item.structural_identity.kind == "file_window"
        and item.structural_identity.region_start_byte == 0
        and item.structural_identity.region_end_byte >= 1900
        and len(outcome.observations) == 1
        for item in outcome.observations
    )
    assert outcome.plan_created is False


# --------------------------------------------------------------------------
# Part 9 — wrong-first-observation recovery
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", [F.CASE_B, F.CASE_C], ids=lambda f: f.name)
def test_wrong_first_observation_is_refined_not_accepted(fixture):
    outcome = _run(fixture)

    first = outcome.observations[0]
    assert not _overlaps(first, fixture), "FIRST_OBSERVATION_RELEVANT must be NO"

    claim = outcome.decision.sufficiency
    assert claim is not None
    assert (
        first.observation_id not in claim.cited_observation_ids
    ), "FIRST_OBSERVATION_MARKED_SUFFICIENT must be NO"

    assert len(outcome.requests) == 2
    assert (
        outcome.requests[0].replay_key() != outcome.requests[1].replay_key()
    ), "SECOND_REQUEST_DIFFERENT must be YES"
    assert outcome.requests[0].mode == P.SEARCH_MODE_LITERAL
    assert outcome.requests[1].mode == P.SEARCH_MODE_STRUCTURAL

    later = [item for item in outcome.observations[1:] if _overlaps(item, fixture)]
    assert later, "SECOND_OBSERVATION_RELEVANT must be YES"
    assert outcome.decision.decision == P.DECISION_SUFFICIENT


def test_case_c_first_observation_is_a_real_but_wrong_region_of_the_right_file():
    """The refinement is semantic, not a file correction."""

    outcome = _run(F.CASE_C)
    first = outcome.observations[0]
    assert first.source_path == F.CASE_C.expected_path
    assert first.structural_identity is not None
    assert first.structural_identity.kind in {"function", "class", "route"}
    assert not _overlaps(first, F.CASE_C)


# --------------------------------------------------------------------------
# Part 2 — provenance and authority
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", list(F.ALL_FIXTURES), ids=lambda f: f.name)
def test_observation_text_is_never_appended_to_task_text(fixture):
    adapter = _RecordingAdapter()
    outcome = run_grounding(REPO_ROOT, fixture.task_text, adapter)

    assert len(adapter.requests) >= 2
    for request in adapter.requests:
        assert request.task_text == fixture.task_text
        assert request.task_text_provenance == P.PROVENANCE_OPERATOR_TASK
    for observation in outcome.observations:
        head = observation.bounded_content[:60].decode("utf-8", "replace").strip()
        if len(head) > 20:
            assert head not in fixture.task_text


def test_provenance_survives_the_second_turn():
    adapter = _RecordingAdapter()
    outcome = run_grounding(REPO_ROOT, F.CASE_C.task_text, adapter)

    second = adapter.requests[1]
    assert second.turn == 2
    assert second.task_text == F.CASE_C.task_text
    assert second.task_text_provenance == P.PROVENANCE_OPERATOR_TASK
    assert second.prior_observation_ids == ("obs-1-1",)
    assert second.orientation.provenance == P.PROVENANCE_DETERMINISTIC_ORIENTATION

    for observation in outcome.observations:
        assert observation.provenance == P.PROVENANCE_HARNESS_OBSERVATION
        assert observation.action.provenance == P.PROVENANCE_MODEL_REQUEST
        assert observation.structural_identity is not None
        assert (
            observation.structural_identity.provenance
            == P.PROVENANCE_HARNESS_STRUCTURAL_RESOLUTION
        )


def test_model_output_is_never_relabelled_as_operator_authority():
    outcome = _run(F.CASE_C)
    for observation in outcome.observations:
        assert observation.action.provenance != P.PROVENANCE_OPERATOR_TASK
        assert observation.provenance != P.PROVENANCE_OPERATOR_TASK
    assert outcome.task_text == F.CASE_C.task_text


@pytest.mark.parametrize("fixture", list(F.ALL_FIXTURES), ids=lambda f: f.name)
def test_grounding_grants_no_mutation_authority(fixture):
    outcome = _run(fixture)

    assert outcome.plan_created is False
    assert outcome.apa_created is False
    assert outcome.mutation_authority_granted is False
    assert outcome.controlled_apply_reached is False

    claim_fields = set(P.SufficiencyClaim.__dataclass_fields__)
    forbidden = {
        "operation",
        "target_span",
        "accepted_paths",
        "grant",
        "apa",
        "version_fence",
    }
    assert not (claim_fields & forbidden)

    action_fields = set(P.GroundingAction.__dataclass_fields__)
    assert not (
        action_fields & {"write", "replace", "content", "patch", "operation", "command"}
    )


# --------------------------------------------------------------------------
# Part 3 — deterministic orientation
# --------------------------------------------------------------------------


def test_orientation_is_advisory_not_an_authoritative_locator():
    outcome = _run(F.CASE_C)

    assert outcome.orientation.available is True
    assert outcome.orientation.is_authoritative_locator is False
    assert outcome.orientation.provenance == P.PROVENANCE_DETERMINISTIC_ORIENTATION
    assert F.CASE_C.expected_path in outcome.orientation.paths
    # Orientation narrows; it does not identify the implementation target.
    assert len(outcome.orientation.paths) > 1
    assert outcome.orientation.literals[:3] == ("allow", "callers", "include")


# --------------------------------------------------------------------------
# Part 4 / Part 5 — lifecycle and hard bounds
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", list(F.ALL_FIXTURES), ids=lambda f: f.name)
def test_all_hard_bounds_hold_for_every_fixture(fixture):
    outcome = _run(fixture)

    assert len(outcome.requests) <= P.MAX_GROUNDING_REQUESTS
    assert sum(item.byte_count for item in outcome.observations) <= (
        P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES
    )
    assert (
        len({item.source_path for item in outcome.observations}) <= P.MAX_DISTINCT_FILES
    )
    assert len(outcome.observations) <= P.MAX_PRIMARY_REGIONS
    assert all(item.byte_count <= P.MAX_REGION_BYTES for item in outcome.observations)
    harness = GroundingHarness(REPO_ROOT)
    assert all(harness.in_scope(item.source_path) for item in outcome.observations)


def test_turn_bound_is_enforced_by_the_harness():
    outcome = run_grounding(REPO_ROOT, F.CASE_C.task_text, _AlwaysAsksAdapter())

    assert len(outcome.requests) == P.MAX_GROUNDING_REQUESTS
    assert outcome.rejections
    assert outcome.rejections[-1][1] == P.REJECT_TURN_BUDGET
    assert outcome.decision.decision == P.DECISION_INSUFFICIENT
    assert outcome.decision.stop_reason == P.STOP_INSUFFICIENT_GROUNDING


def test_byte_bound_truncates_evidence():
    harness = GroundingHarness(REPO_ROOT)
    action = P.GroundingAction(
        kind=P.ACTION_INSPECT_ROUTE,
        route_method="GET",
        route_path="/projects",
        scope_paths=("app/api/v1/endpoints/projects.py",),
    )
    tight = P.Budget(
        requests_remaining=1,
        evidence_bytes_remaining=120,
        files_remaining=4,
        regions_remaining=4,
    )
    observations = harness.execute(action, tight, set(), turn=1, next_index=1)

    assert len(observations) == 1
    assert observations[0].byte_count == 120
    assert observations[0].truncated is True

    exhausted = P.Budget(
        requests_remaining=1,
        evidence_bytes_remaining=0,
        files_remaining=4,
        regions_remaining=4,
    )
    assert harness.execute(action, exhausted, set(), turn=1, next_index=1) == []


def test_file_bound_and_region_bound_are_enforced(tmp_path):
    files = {
        f"pkg/module_{index}.py": (
            "def list_invoices_%d():\n    invoice = 1\n    ledger = 2\n    return invoice, ledger\n"
            % index
        )
        for index in range(6)
    }
    project = _temp_repo(tmp_path, files)
    harness = GroundingHarness(project)
    scope = tuple(sorted(files))
    action = P.GroundingAction(
        kind=P.ACTION_SEARCH_TEXT,
        mode=P.SEARCH_MODE_STRUCTURAL,
        terms=("invoices", "invoice", "ledger"),
        scope_paths=scope,
        max_results=6,
    )

    file_capped = P.Budget(
        requests_remaining=1,
        evidence_bytes_remaining=P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES,
        files_remaining=P.MAX_DISTINCT_FILES,
        regions_remaining=6,
    )
    observations = harness.execute(action, file_capped, set(), turn=1, next_index=1)
    assert len({item.source_path for item in observations}) == P.MAX_DISTINCT_FILES

    region_capped = P.Budget(
        requests_remaining=1,
        evidence_bytes_remaining=P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES,
        files_remaining=10,
        regions_remaining=2,
    )
    observations = harness.execute(action, region_capped, set(), turn=1, next_index=1)
    assert len(observations) == 2


@pytest.mark.parametrize(
    "path",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "venv/lib/python3.12/site-packages/fastapi/__init__.py",
        "app/api/v1/endpoints/does_not_exist.py",
    ],
)
def test_path_scope_is_enforced(path):
    harness = GroundingHarness(REPO_ROOT)
    action = P.GroundingAction(
        kind=P.ACTION_SEARCH_TEXT,
        mode=P.SEARCH_MODE_LITERAL,
        query="def ",
        scope_paths=(path,),
    )
    assert harness.validate(action, _full_budget()) == P.REJECT_PATH_OUTSIDE_SCOPE


def test_prototype_package_is_outside_grounding_scope():
    harness = GroundingHarness(REPO_ROOT)
    assert not harness.in_scope("app/tests/prototypes/planning_grounding/adapter.py")


def test_read_only_action_schema_is_enforced():
    harness = GroundingHarness(REPO_ROOT)
    budget = _full_budget()
    scope = ("app/api/v1/endpoints/projects.py",)

    unsupported = P.GroundingAction(kind="apply_change", scope_paths=scope)
    assert harness.validate(unsupported, budget) == P.REJECT_UNSUPPORTED_ACTION

    bad_mode = P.GroundingAction(
        kind=P.ACTION_SEARCH_TEXT, mode="regex", scope_paths=scope
    )
    assert harness.validate(bad_mode, budget) == P.REJECT_UNSUPPORTED_MODE

    empty = P.GroundingAction(
        kind=P.ACTION_SEARCH_TEXT,
        mode=P.SEARCH_MODE_LITERAL,
        query="  ",
        scope_paths=scope,
    )
    assert harness.validate(empty, budget) == P.REJECT_EMPTY_QUERY

    class _MutatingAction(P.GroundingAction):
        replace = "anything"

    mutating = _MutatingAction(
        kind=P.ACTION_SEARCH_TEXT,
        mode=P.SEARCH_MODE_LITERAL,
        query="def ",
        scope_paths=scope,
    )
    assert harness.validate(mutating, budget) == P.REJECT_MUTATION_FIELD


def test_there_is_never_a_third_grounding_request():
    for fixture in F.ALL_FIXTURES:
        outcome = _run(fixture)
        assert len(outcome.requests) <= 2
        assert len(outcome.budget_trace) <= P.MAX_GROUNDING_REQUESTS + 1
        assert outcome.final_budget.requests_remaining >= 0


# --------------------------------------------------------------------------
# Part 6 — structural resolution
# --------------------------------------------------------------------------


def test_route_resolves_to_its_handler_region():
    harness = GroundingHarness(REPO_ROOT)
    action = P.GroundingAction(
        kind=P.ACTION_INSPECT_ROUTE,
        route_method="GET",
        route_path="/projects",
        scope_paths=("app/api/v1/endpoints/projects.py",),
    )
    observations = harness.execute(action, _full_budget(), set(), turn=1, next_index=1)

    assert len(observations) == 1
    identity = observations[0].structural_identity
    assert identity is not None
    assert identity.kind == "route"
    assert identity.name == "get_projects"
    assert (identity.region_start_byte, identity.region_end_byte) == (3894, 5597)
    # The decorator is part of the region, not just the body.
    assert observations[0].bounded_content.startswith(b'@router.get("/projects")')


def test_symbol_resolves_to_its_definition_region():
    harness = GroundingHarness(REPO_ROOT)
    action = P.GroundingAction(
        kind=P.ACTION_INSPECT_SYMBOL,
        symbol_name="get_projects",
        scope_paths=("app/api/v1/endpoints/projects.py",),
    )
    observations = harness.execute(action, _full_budget(), set(), turn=1, next_index=1)

    assert len(observations) == 1
    identity = observations[0].structural_identity
    assert identity is not None
    assert identity.name == "get_projects"
    assert identity.region_start_byte == 3894


def test_repeated_generic_literal_does_not_force_first_occurrence(tmp_path):
    module = (
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        "def bootstrap_helper():\n"
        "    shared_call()\n"
        "    return None\n"
        "\n"
        "\n"
        '@router.get("/invoices")\n'
        "def list_invoices():\n"
        "    shared_call()\n"
        "    invoice = 1\n"
        "    ledger = 2\n"
        "    return invoice, ledger\n"
    )
    project = _temp_repo(tmp_path, {"pkg/routes.py": module})
    harness = GroundingHarness(project)
    scope = ("pkg/routes.py",)

    literal = harness.execute(
        P.GroundingAction(
            kind=P.ACTION_SEARCH_TEXT,
            mode=P.SEARCH_MODE_LITERAL,
            query="shared_call()",
            scope_paths=scope,
        ),
        _full_budget(),
        set(),
        turn=1,
        next_index=1,
    )
    assert literal[0].structural_identity is not None
    assert literal[0].structural_identity.name == "bootstrap_helper"
    assert literal[0].notes["match_count"] == 2
    assert literal[0].notes["selection_strategy"] == "first_literal_occurrence"

    structural = harness.execute(
        P.GroundingAction(
            kind=P.ACTION_SEARCH_TEXT,
            mode=P.SEARCH_MODE_STRUCTURAL,
            terms=("invoices", "invoice", "ledger"),
            http_methods=("GET",),
            scope_paths=scope,
            max_results=1,
        ),
        _full_budget(),
        set(),
        turn=1,
        next_index=1,
    )
    assert structural[0].structural_identity is not None
    assert structural[0].structural_identity.name == "list_invoices"
    assert structural[0].structural_identity.route_path == "/invoices"


def test_structural_survey_requires_identity_level_relevance(tmp_path):
    """A body-only literal match is never a structural candidate."""

    module = (
        "def unrelated_helper():\n"
        "    # mentions invoices, invoice and ledger only in passing\n"
        "    return ('invoices', 'invoice', 'ledger')\n"
    )
    project = _temp_repo(tmp_path, {"pkg/other.py": module})
    harness = GroundingHarness(project)
    observations = harness.execute(
        P.GroundingAction(
            kind=P.ACTION_SEARCH_TEXT,
            mode=P.SEARCH_MODE_STRUCTURAL,
            terms=("invoices", "invoice", "ledger"),
            scope_paths=("pkg/other.py",),
            max_results=4,
        ),
        _full_budget(),
        set(),
        turn=1,
        next_index=1,
    )
    assert len(observations) == 1
    assert observations[0].outcome == P.OBSERVATION_NOT_FOUND
    assert observations[0].structural_identity is None


def test_symbol_regions_cover_functions_classes_and_routes():
    raw = (REPO_ROOT / "app/api/v1/endpoints/projects.py").read_bytes()
    regions = symbol_regions("app/api/v1/endpoints/projects.py", raw)
    by_name = {region.name: region for region in regions}

    assert by_name["get_projects"].route_path == "/projects"
    assert by_name["get_projects"].http_method == "GET"
    assert by_name["retire_project"].http_method == "POST"
    assert by_name["_assert_unique_resolved_workspace"].route_path is None


# --------------------------------------------------------------------------
# Part 7 — adapter cannot contain fixture knowledge
# --------------------------------------------------------------------------


def test_adapter_contains_no_fixture_specific_knowledge():
    source = (Path(__file__).parent / "adapter.py").read_text()
    forbidden = [
        "get_projects",
        "projects.py",
        "tasks.py",
        "mobile.py",
        "get_all_tasks",
        "get_dashboard",
        "get_session_summary",
        "_build_task_counts",
        "retired",
        "APIRouter",
        "endpoints/",
        "CASE_A",
        "CASE_B",
        "CASE_C",
        "CASE_D",
        "expected_",
    ]
    for needle in forbidden:
        assert needle not in source, f"adapter.py leaks fixture knowledge: {needle}"
    assert "fixtures" not in source


def test_adapter_never_receives_expected_target_metadata():
    request_fields = set(P.GroundingRequest.__dataclass_fields__)
    assert request_fields == {
        "task_text",
        "task_text_provenance",
        "orientation",
        "prior_observation_ids",
        "remaining_budget",
        "turn",
    }
    fixture_fields = set(F.GroundingFixture.__dataclass_fields__)
    assert "expected_path" in fixture_fields
    assert not (fixture_fields & request_fields - {"task_text"})


# --------------------------------------------------------------------------
# Part 10 — sufficiency contract
# --------------------------------------------------------------------------


def test_sufficiency_requires_an_observation_citation():
    class _UncitedAdapter(GroundingAdapter):
        def propose(self, request, observations):
            return P.GroundingDecision(
                decision=P.DECISION_SUFFICIENT,
                sufficiency=P.SufficiencyClaim(
                    cited_observation_ids=(),
                    relevant_source_paths=("app/api/v1/endpoints/projects.py",),
                    structural_locators=("function:get_projects",),
                ),
            )

    outcome = run_grounding(REPO_ROOT, F.CASE_C.task_text, _UncitedAdapter())
    assert outcome.decision.decision == P.DECISION_INSUFFICIENT
    assert outcome.decision.rationale == "sufficiency_without_observation_citation"


def test_sufficiency_cannot_cite_an_observation_that_was_never_made():
    class _FabricatingAdapter(GroundingAdapter):
        def propose(self, request, observations):
            return P.GroundingDecision(
                decision=P.DECISION_SUFFICIENT,
                sufficiency=P.SufficiencyClaim(
                    cited_observation_ids=("obs-9-9",),
                    relevant_source_paths=(),
                    structural_locators=(),
                ),
            )

    outcome = run_grounding(REPO_ROOT, F.CASE_C.task_text, _FabricatingAdapter())
    assert outcome.decision.decision == P.DECISION_INSUFFICIENT
    assert outcome.decision.rationale == "sufficiency_cited_unknown_observation"


def test_sufficiency_cannot_be_declared_from_a_literal_match_alone():
    """A first-occurrence literal hit fails the adapter's own sufficiency test."""

    adapter = GenericGroundingAdapter()
    harness = GroundingHarness(REPO_ROOT)
    literal = harness.execute(
        P.GroundingAction(
            kind=P.ACTION_SEARCH_TEXT,
            mode=P.SEARCH_MODE_LITERAL,
            query="APIRouter()",
            scope_paths=("app/api/v1/endpoints/projects.py",),
        ),
        _full_budget(),
        set(),
        turn=1,
        next_index=1,
    )
    assert literal, "the literal is present in the file"
    terms = adapter._terms(F.CASE_C.task_text)
    assert not any(adapter._is_sufficient(item, terms) for item in literal)


def test_sufficiency_claim_carries_paths_locators_and_versions():
    outcome = _run(F.CASE_C)
    claim = outcome.decision.sufficiency
    assert claim is not None
    assert claim.relevant_source_paths == ("app/api/v1/endpoints/projects.py",)
    assert claim.structural_locators == ("route:GET /projects -> get_projects",)
    assert set(claim.source_versions) == {"app/api/v1/endpoints/projects.py"}
    assert claim.source_versions["app/api/v1/endpoints/projects.py"].startswith(
        "sha256:"
    )


# --------------------------------------------------------------------------
# Part 11 — deterministic replay
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", list(F.ALL_FIXTURES), ids=lambda f: f.name)
def test_captured_requests_replay_identically(fixture):
    original = _run(fixture)
    replayed = replay(REPO_ROOT, original.requests)

    assert [item.replay_key() for item in replayed] == [
        item.replay_key() for item in original.observations
    ]
    assert [item.source_path for item in replayed] == [
        item.source_path for item in original.observations
    ]
    assert [
        (
            item.structural_identity.region_start_byte,
            item.structural_identity.region_end_byte,
        )
        for item in replayed
        if item.structural_identity
    ] == [
        (
            item.structural_identity.region_start_byte,
            item.structural_identity.region_end_byte,
        )
        for item in original.observations
        if item.structural_identity
    ]
    assert sum(item.byte_count for item in replayed) == sum(
        item.byte_count for item in original.observations
    )


@pytest.mark.parametrize("fixture", list(F.ALL_FIXTURES), ids=lambda f: f.name)
def test_the_lifecycle_itself_is_stable_across_runs(fixture):
    first = _run(fixture)
    second = _run(fixture)

    assert [item.replay_key() for item in first.requests] == [
        item.replay_key() for item in second.requests
    ]
    assert [item.replay_key() for item in first.observations] == [
        item.replay_key() for item in second.observations
    ]
    assert first.decision.decision == second.decision.decision
    assert first.final_budget == second.final_budget
