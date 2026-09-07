"""PHASE35-GPC1 — bounded grounding protocol corrections.

Provider-free controls for truthful negative observations, effective mounted
route identity, and explicit post-observation sufficiency assessment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.tests.prototypes.planning_grounding import protocol as P
from app.tests.prototypes.planning_grounding.harness import (
    GroundingAdapter,
    GroundingHarness,
    replay,
    run_grounding,
)
from app.tests.prototypes.planning_grounding.model_adapter import (
    ACTION_MENU_OPEN_SCOPE,
    SYSTEM_PROMPT,
    _render_observations,
    build_open_scope_user_prompt,
    parse_model_reply,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
AUTH_PATH = "app/api/v1/endpoints/auth.py"
PROJECTS_PATH = "app/api/v1/endpoints/projects.py"


def _full_budget() -> P.Budget:
    return P.Budget(
        requests_remaining=P.MAX_GROUNDING_REQUESTS,
        evidence_bytes_remaining=P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES,
        files_remaining=P.MAX_DISTINCT_FILES,
        regions_remaining=P.MAX_PRIMARY_REGIONS,
    )


def _route(
    method: str, path: str, scope: tuple[str, ...] = (AUTH_PATH,)
) -> P.GroundingAction:
    return P.GroundingAction(
        kind=P.ACTION_INSPECT_ROUTE,
        route_method=method,
        route_path=path,
        scope_paths=scope,
    )


def _temp_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def _execute(
    harness: GroundingHarness, action: P.GroundingAction
) -> tuple[P.GroundingObservation, ...]:
    return tuple(harness.execute(action, _full_budget(), set(), turn=1, next_index=1))


class _NegativeThenSufficientAdapter(GroundingAdapter):
    def propose(self, request, observations):
        if not observations:
            return _route("POST", "/auth/signin")
        if request.remaining_budget.requests_remaining:
            return P.GroundingDecision(
                decision=P.DECISION_NEED_MORE_EVIDENCE,
                next_action=P.GroundingAction(
                    kind=P.ACTION_INSPECT_SYMBOL,
                    symbol_name="enforce_auth_rate_limit",
                    scope_paths=("app/services/auth/rate_limit.py",),
                ),
                rationale="the requested route was not found",
            )
        return P.GroundingDecision(
            decision=P.DECISION_SUFFICIENT,
            sufficiency=P.SufficiencyClaim(
                cited_observation_ids=(observations[-1].observation_id,),
                relevant_source_paths=(),
                structural_locators=(),
            ),
        )


def test_valid_zero_match_is_a_visible_bounded_negative_observation():
    harness = GroundingHarness(REPO_ROOT)
    observations = _execute(harness, _route("POST", "/auth/signin"))

    assert len(observations) == 1
    observation = observations[0]
    assert observation.outcome == P.OBSERVATION_NOT_FOUND
    assert observation.structural_identity is None
    assert observation.byte_count == 0
    assert observation.notes["requested_method"] == "POST"
    assert observation.notes["requested_path"] == "/auth/signin"
    assert observation.notes["requested_scope"] == (AUTH_PATH,)
    assert "/auth/login" not in repr(observation.notes)
    assert "rate_limit" not in repr(observation.notes)

    rendered = _render_observations(observations)
    assert "OBSERVATIONS: none yet." not in rendered
    assert "outcome: not_found" in rendered
    assert "POST /auth/signin" in rendered
    assert AUTH_PATH in rendered
    assert "/auth/login" not in rendered


def test_negative_observation_is_visible_to_the_next_model_turn():
    class RecordingAdapter(_NegativeThenSufficientAdapter):
        def __init__(self):
            self.requests = []

        def propose(self, request, observations):
            self.requests.append((request, tuple(observations)))
            return super().propose(request, observations)

    adapter = RecordingAdapter()
    outcome = run_grounding(REPO_ROOT, "protect sign-ins", adapter)

    assert len(adapter.requests) >= 2
    second_request, second_observations = adapter.requests[1]
    assert second_request.prior_observation_ids == ("obs-1-1",)
    assert second_observations[0].outcome == P.OBSERVATION_NOT_FOUND
    assert outcome.observations[0].action.provenance == P.PROVENANCE_MODEL_REQUEST


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/auth/signin"),
        ("GET", "/auth/login"),
        ("POST", "/auth/log"),
    ],
)
def test_wrong_mounted_route_or_method_is_an_explicit_negative(method, path):
    observations = _execute(GroundingHarness(REPO_ROOT), _route(method, path))
    assert len(observations) == 1
    assert observations[0].outcome == P.OBSERVATION_NOT_FOUND
    assert observations[0].notes["requested_method"] == method
    assert observations[0].notes["requested_path"] == path


def test_direct_route_without_prefix_preserves_local_and_effective_identity():
    observations = _execute(
        GroundingHarness(REPO_ROOT),
        _route("GET", "/projects", (PROJECTS_PATH,)),
    )
    identity = observations[0].structural_identity
    assert identity is not None
    assert identity.route_path == "/projects"
    assert identity.decorator_path == "/projects"
    assert identity.mounted_path == "/projects"


def test_router_prefix_and_decorator_path_are_composed_deterministically(tmp_path):
    project = _temp_repo(
        tmp_path,
        {
            "pkg/routes.py": (
                "from fastapi import APIRouter\n"
                "router = APIRouter(prefix='/auth')\n\n"
                "@router.post('/login')\n"
                "def login():\n"
                "    return None\n"
            )
        },
    )
    observations = _execute(
        GroundingHarness(project),
        _route("POST", "/auth/login", ("pkg/routes.py",)),
    )
    identity = observations[0].structural_identity
    assert identity is not None
    assert identity.decorator_path == "/login"
    assert identity.mounted_path == "/auth/login"
    assert identity.route_path == "/auth/login"


def test_production_parent_router_prefix_is_composed_for_auth():
    observations = _execute(GroundingHarness(REPO_ROOT), _route("POST", "/auth/login"))
    identity = observations[0].structural_identity
    assert identity is not None
    assert identity.name == "login_login_form"
    assert identity.decorator_path == "/login"
    assert identity.mounted_path == "/auth/login"


def test_route_resolution_has_no_suffix_match_and_is_replayable():
    harness = GroundingHarness(REPO_ROOT)
    action = _route("POST", "/auth/log")
    first = _execute(harness, action)
    second = _execute(harness, action)
    replayed = replay(REPO_ROOT, (action,))

    assert first[0].outcome == P.OBSERVATION_NOT_FOUND
    assert first == second
    assert first == replayed


def test_explicit_sufficiency_assessment_can_cite_relevant_existing_code():
    class RelevantAdapter(GroundingAdapter):
        def propose(self, request, observations):
            if not observations:
                return P.GroundingAction(
                    kind=P.ACTION_INSPECT_SYMBOL,
                    symbol_name="get_projects",
                    scope_paths=(PROJECTS_PATH,),
                )
            return P.GroundingDecision(
                decision=P.DECISION_SUFFICIENT,
                sufficiency=P.SufficiencyClaim(
                    cited_observation_ids=(observations[0].observation_id,),
                    relevant_source_paths=(),
                    structural_locators=(),
                ),
                rationale="the existing handler is the implementation area",
            )

    outcome = run_grounding(REPO_ROOT, "project browsing", RelevantAdapter())
    assert outcome.decision.decision == P.DECISION_SUFFICIENT
    assert outcome.assessments[-1].decision == P.DECISION_SUFFICIENT
    assert outcome.decision.sufficiency is not None
    assert outcome.decision.sufficiency.cited_observation_ids == ("obs-1-1",)


def test_negative_assessment_explicitly_requests_more_evidence():
    class NeedMoreAdapter(GroundingAdapter):
        def propose(self, request, observations):
            if not observations:
                return _route("POST", "/auth/signin")
            if request.remaining_budget.requests_remaining:
                return P.GroundingDecision(
                    decision=P.DECISION_NEED_MORE_EVIDENCE,
                    next_action=P.GroundingAction(
                        kind=P.ACTION_INSPECT_SYMBOL,
                        symbol_name="enforce_auth_rate_limit",
                        scope_paths=("app/services/auth/rate_limit.py",),
                    ),
                    rationale="the first hypothesis was not found",
                )
            return P.GroundingDecision(
                decision=P.DECISION_INSUFFICIENT,
                stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
            )

    outcome = run_grounding(REPO_ROOT, "protect account sign-ins", NeedMoreAdapter())
    assert outcome.assessments[0].decision == P.DECISION_NEED_MORE_EVIDENCE
    assert outcome.assessments[0].next_action is not None
    assert outcome.requests[1].symbol_name == "enforce_auth_rate_limit"
    assert outcome.decision.decision == P.DECISION_INSUFFICIENT


def test_duplicate_request_remains_mechanically_legal_within_the_bound():
    class DuplicateAdapter(GroundingAdapter):
        def propose(self, request, observations):
            action = _route("POST", "/auth/signin")
            if not observations:
                return action
            if request.remaining_budget.requests_remaining:
                return P.GroundingDecision(
                    decision=P.DECISION_NEED_MORE_EVIDENCE,
                    next_action=action,
                )
            return P.GroundingDecision(
                decision=P.DECISION_INSUFFICIENT,
                stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
            )

    outcome = run_grounding(REPO_ROOT, "protect sign-ins", DuplicateAdapter())
    assert len(outcome.requests) == P.MAX_GROUNDING_REQUESTS
    assert outcome.requests[0].replay_key() == outcome.requests[1].replay_key()
    assert not outcome.rejections


def test_need_more_evidence_wire_schema_is_explicit_and_provider_free():
    orientation = GroundingHarness(REPO_ROOT)
    task = "protect sign-ins"
    from app.tests.prototypes.planning_grounding.harness import derive_orientation

    parsed, error = parse_model_reply(
        '{"decision":"NEED_MORE_EVIDENCE","next_action":'
        '{"action":"inspect_symbol","symbol_name":"enforce_auth_rate_limit",'
        '"scope_paths":["app/services/auth/rate_limit.py"]},"why":"not found"}',
        derive_orientation(REPO_ROOT, task),
    )
    assert error is None
    assert parsed.decision == P.DECISION_NEED_MORE_EVIDENCE
    assert parsed.next_action is not None
    assert parsed.next_action.symbol_name == "enforce_auth_rate_limit"
    assert orientation is not None


def test_protocol_clarification_contains_no_task_navigation_hint():
    scaffolding = (SYSTEM_PROMPT + ACTION_MENU_OPEN_SCOPE).lower()
    assert "requested future behavior does not need to already exist" in scaffolding
    for forbidden in (
        "rate_limit.py",
        "auth/login",
        "failure tracking",
        "service layer",
        "try something different",
    ):
        assert forbidden not in scaffolding


def test_authority_and_bounds_remain_frozen():
    assert P.MAX_GROUNDING_REQUESTS == 2
    assert P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES == 12 * 1024
    assert P.MAX_DISTINCT_FILES == 4
    assert P.MAX_PRIMARY_REGIONS == 4

    outcome = run_grounding(
        REPO_ROOT,
        "protect sign-ins",
        _NegativeThenSufficientAdapter(),
    )
    assert outcome.plan_created is False
    assert outcome.apa_created is False
    assert outcome.mutation_authority_granted is False
    assert outcome.controlled_apply_reached is False
