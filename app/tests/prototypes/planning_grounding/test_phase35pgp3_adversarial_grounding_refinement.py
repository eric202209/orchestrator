"""PHASE35-PGP3 — adversarial grounding refinement trial.

The task, the evaluator target, the prompt, and the harness are frozen in this
module *before* any provider call. The model is given none of the evaluator
constants below.

One mechanical difference from the PGP2 contract: `scope_paths` is bounded by
Git-tracked repository scope rather than by the advisory orientation list,
because orientation is truncated at 39 entries and does not contain the
implementation for this task. Without that change the expected region is
unreachable and the refinement question cannot be asked at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tests.prototypes.planning_grounding import protocol as P
from app.tests.prototypes.planning_grounding.harness import (
    derive_orientation,
    run_grounding,
)
from app.tests.prototypes.planning_grounding.model_adapter import (
    ACTION_MENU,
    ACTION_MENU_OPEN_SCOPE,
    SYSTEM_PROMPT,
    OpenScopeModelGroundingAdapter,
    build_open_scope_user_prompt,
)
from app.tests.prototypes.planning_grounding.provider_client import (
    planning_provider_reachable,
)
from app.tests.prototypes.planning_grounding.structure import symbol_regions
from app.tests.prototypes.planning_grounding.trial_harness import TrialHarness

REPO_ROOT = Path(__file__).resolve().parents[4]

# --------------------------------------------------------------------------
# FROZEN experimental task — ordinary product/developer language.
# Names no path, symbol, class, route, line, byte range, or code literal.
# --------------------------------------------------------------------------

TRIAL_TASK_TEXT = (
    "Repeated failed sign-ins on the auth endpoints are currently held back per "
    "calling address, so one machine cannot keep guessing a password forever. "
    "That does not help when the guessing is spread across many machines "
    "against a single account: every address stays under the threshold and the "
    "account itself is never protected. The failure count should also be kept "
    "against the account being targeted, so a distributed guessing run against "
    "one account is held back once its combined failures cross the same "
    "threshold. Keep the existing threshold, the existing cooling-off window, "
    "and the existing error the caller already sees. Successful sign-ins and "
    "every other endpoint must behave exactly as they do today. Add regression "
    "coverage for the account-scoped refusal and for the unchanged success path."
)

# --------------------------------------------------------------------------
# FROZEN evaluator-only target. Never enters a model request.
# Established by reading the repository before the trial: the per-address
# scoping the task wants changed is built in `RateLimitBucket` and applied in
# `enforce_auth_rate_limit`; the auth endpoints only call the latter.
# --------------------------------------------------------------------------

EXPECTED_PATH = "app/services/auth/rate_limit.py"
EXPECTED_SYMBOLS = ("enforce_auth_rate_limit", "RateLimitBucket")
PRIMARY_EXPECTED_SYMBOL = "enforce_auth_rate_limit"

PLAUSIBLE_WRONG_PATH = "app/api/v1/endpoints/auth.py"
PLAUSIBLE_WRONG_SYMBOLS = (
    "get_tokens",
    "login_login_form",
    "session_login",
    "register",
    "refresh_token",
)


def _regions(path: str):
    raw = (REPO_ROOT / path).read_bytes()
    return {region.name: region for region in symbol_regions(path, raw)}


def expected_regions():
    found = _regions(EXPECTED_PATH)
    return {name: found[name] for name in EXPECTED_SYMBOLS}


def _full_budget() -> P.Budget:
    return P.Budget(
        requests_remaining=P.MAX_GROUNDING_REQUESTS,
        evidence_bytes_remaining=P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES,
        files_remaining=P.MAX_DISTINCT_FILES,
        regions_remaining=P.MAX_PRIMARY_REGIONS,
    )


def turn_one_prompt() -> str:
    orientation = derive_orientation(REPO_ROOT, TRIAL_TASK_TEXT)
    request = P.GroundingRequest(
        task_text=TRIAL_TASK_TEXT,
        task_text_provenance=P.PROVENANCE_OPERATOR_TASK,
        orientation=orientation,
        prior_observation_ids=(),
        remaining_budget=_full_budget(),
        turn=1,
    )
    return build_open_scope_user_prompt(request, ())


# --------------------------------------------------------------------------
# Part 3 / Part 4 — task and vocabulary-drift qualification (provider-free)
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_task_names_no_expected_locator():
    lowered = TRIAL_TASK_TEXT.lower()
    for needle in [
        "rate_limit",
        "rate limit",
        "ratelimit",
        "bucket",
        "enforce",
        "/tokens",
        "/login",
        "services/auth",
        "endpoints/auth.py",
        "client_id",
        "user_id",
    ]:
        assert needle not in lowered, f"task leaks the locator: {needle}"
    for symbol in EXPECTED_SYMBOLS + PLAUSIBLE_WRONG_SYMBOLS:
        assert symbol.lower() not in lowered


@pytest.mark.unit
def test_vocabulary_drift_is_materially_harder_than_pgp2():
    """Orientation must not hand the model the implementation."""

    orientation = derive_orientation(REPO_ROOT, TRIAL_TASK_TEXT)

    assert (
        EXPECTED_PATH not in orientation.paths
    ), "orientation surfaced the implementation path; drift is too weak"
    # The plausible wrong competitor *is* surfaced. That is the adversarial
    # setup: the visible candidate is not the implementation.
    assert PLAUSIBLE_WRONG_PATH in orientation.paths
    assert orientation.truncated
    assert orientation.entries_total > len(orientation.paths)


@pytest.mark.unit
def test_prompt_hides_the_evaluator_target():
    prompt = turn_one_prompt()
    regions = expected_regions()
    forbidden = [
        EXPECTED_PATH,
        "rate_limit",
        "RateLimitBucket",
        "enforce_auth_rate_limit",
        "client_id",
        str(regions[PRIMARY_EXPECTED_SYMBOL].start_byte),
        str(regions[PRIMARY_EXPECTED_SYMBOL].end_byte),
        "expected",
        "refine",
    ]
    for needle in forbidden:
        assert needle not in prompt, f"prompt leaks evaluator metadata: {needle}"
    assert TRIAL_TASK_TEXT in prompt


@pytest.mark.unit
def test_pgp3_prompt_differs_from_pgp2_only_in_the_scope_rule():
    """No semantic instruction was added: no refinement or sufficiency hint."""

    pgp2_lines = ACTION_MENU.splitlines()
    pgp3_lines = ACTION_MENU_OPEN_SCOPE.splitlines()
    removed = [line for line in pgp2_lines if line not in pgp3_lines]
    added = [line for line in pgp3_lines if line not in pgp2_lines]

    assert removed == ["- scope_paths must be chosen from the orientation list above."]
    assert all("scope_paths" in line or line.startswith("  ") for line in added)

    scaffolding = (SYSTEM_PROMPT + ACTION_MENU_OPEN_SCOPE).lower()
    for banned in [
        "do not repeat",
        "try something different",
        "service layer",
        "alias",
        "underlying",
        "as soon as possible",
        "wrapper",
        "helper",
        "refine",
    ]:
        assert banned not in scaffolding, f"prompt tuning leaked: {banned}"


@pytest.mark.unit
def test_evaluator_target_is_a_real_resolvable_region():
    regions = expected_regions()
    assert (
        regions[PRIMARY_EXPECTED_SYMBOL].end_byte
        > regions[PRIMARY_EXPECTED_SYMBOL].start_byte
    )
    competitors = _regions(PLAUSIBLE_WRONG_PATH)
    for name in PLAUSIBLE_WRONG_SYMBOLS:
        assert name in competitors
    # The competitor endpoints only *call* the enforcement; they do not
    # implement the scoping the task asks to change.
    raw = (REPO_ROOT / PLAUSIBLE_WRONG_PATH).read_bytes().decode()
    assert "enforce_auth_rate_limit(request," in raw
    assert "RateLimitBucket" not in raw


# --------------------------------------------------------------------------
# Provider-backed adversarial trial
# --------------------------------------------------------------------------


def run_trial():
    adapter = OpenScopeModelGroundingAdapter()
    outcome = run_grounding(
        REPO_ROOT, TRIAL_TASK_TEXT, adapter, harness=TrialHarness(REPO_ROOT)
    )
    return adapter, outcome


def observation_is_expected(observation) -> bool:
    identity = observation.structural_identity
    return (
        observation.source_path == EXPECTED_PATH
        and identity is not None
        and identity.name in EXPECTED_SYMBOLS
    )


@pytest.mark.live
@pytest.mark.skipif(
    not planning_provider_reachable(),
    reason="configured Planning model is not reachable; trial cannot be exercised",
)
def test_adversarial_trial_executes_within_bounds_and_grants_no_authority():
    """Bounds and authority hold regardless of whether the model succeeds."""

    _, outcome = run_trial()

    assert len(outcome.requests) <= P.MAX_GROUNDING_REQUESTS
    assert sum(item.byte_count for item in outcome.observations) <= (
        P.MAX_TOTAL_SOURCE_EVIDENCE_BYTES
    )
    assert len({item.source_path for item in outcome.observations}) <= (
        P.MAX_DISTINCT_FILES
    )
    assert len(outcome.observations) <= P.MAX_PRIMARY_REGIONS

    harness = TrialHarness(REPO_ROOT)
    assert all(harness.in_scope(item.source_path) for item in outcome.observations)

    assert outcome.observations[0].outcome == P.OBSERVATION_NOT_FOUND
    assert outcome.observations[0].notes["requested_path"] == "/auth/signin"
    assert outcome.assessments
    assert outcome.assessments[0].decision == P.DECISION_NEED_MORE_EVIDENCE

    assert outcome.plan_created is False
    assert outcome.apa_created is False
    assert outcome.mutation_authority_granted is False
    assert outcome.controlled_apply_reached is False
    assert outcome.task_text == TRIAL_TASK_TEXT
