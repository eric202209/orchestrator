from __future__ import annotations

import pytest

from app.services.orchestration.replay import (
    COMPATIBILITY_VERSION,
    REDUCER_VERSION,
    reconstruct_execution_state,
)
from app.tests.replay_fixture_corpus import (
    REPLAY_FIXTURES,
    SESSION_ID,
    TASK_ID,
    assert_replay_fixture_expectations,
    materialize_replay_fixture,
)


@pytest.mark.parametrize(
    "fixture",
    REPLAY_FIXTURES,
    ids=[fixture.fixture_id for fixture in REPLAY_FIXTURES],
)
def test_replay_fixture_corpus_regression(tmp_path, fixture):
    project_dir, checkpoint_dir = materialize_replay_fixture(tmp_path, fixture)

    report = reconstruct_execution_state(
        project_dir=project_dir,
        session_id=SESSION_ID,
        task_id=TASK_ID,
        boundary=fixture.boundary,
        checkpoint_dir=checkpoint_dir,
        compare_workspace=fixture.compare_workspace,
    )

    assert_replay_fixture_expectations(report, fixture)


def test_replay_fixtures_pin_current_reducer_and_compatibility_versions():
    for fixture in REPLAY_FIXTURES:
        assert fixture.expected_reducer_version == REDUCER_VERSION
        assert fixture.expected_compatibility_version == COMPATIBILITY_VERSION


def test_replay_fixture_corpus_has_required_trace_families():
    fixture_ids = {fixture.fixture_id for fixture in REPLAY_FIXTURES}

    assert "successful_execution_trace" in fixture_ids
    assert "validation_rejection_trace" in fixture_ids
    assert "repair_chain_trace" in fixture_ids
    assert "timeout_failure_trace" in fixture_ids
    assert "intervention_flow_trace" in fixture_ids
    assert "checkpoint_redirect_trace" in fixture_ids
    assert "workspace_drift_evidence_trace" in fixture_ids
    assert "malformed_jsonl_trace" in fixture_ids
    assert "unknown_event_type_trace" in fixture_ids
