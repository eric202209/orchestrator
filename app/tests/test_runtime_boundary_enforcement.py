"""Phase 10S: Runtime boundary enforcement tests."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# D1/D2 — Registry completeness and labeling                                  #
# --------------------------------------------------------------------------- #


def _public_normalizer_function_names() -> list[str]:
    from app.services.orchestration.planning import normalization

    return [
        name
        for name, obj in inspect.getmembers(normalization, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == normalization.__name__
    ]


def test_rule_registry_has_no_unlabeled_normalizer():
    from app.services.orchestration.rule_registry import RULE_REGISTRY

    public_normalizers = _public_normalizer_function_names()

    for fn_name in public_normalizers:
        matching = [r for r in RULE_REGISTRY.values() if fn_name in r.source_location]
        assert matching, (
            f"Public normalizer '{fn_name}' has no entry in RULE_REGISTRY. "
            "Add an entry before modifying this function."
        )


def test_runtime_registry_has_no_deprecated_planning_artifacts():
    from app.services.orchestration.rule_registry import RULE_REGISTRY

    deprecated = [
        r for r in RULE_REGISTRY.values() if r.owner_layer == "deprecated_artifact"
    ]
    assert deprecated == []


# --------------------------------------------------------------------------- #
# D3 — DB schema and repair governor                                           #
# --------------------------------------------------------------------------- #


def test_repair_churn_stopped_column_exists():
    from app.models import Session as SessionModel

    columns = {c.key for c in SessionModel.__table__.columns}
    assert "repair_churn_stopped" in columns, (
        "Session model is missing 'repair_churn_stopped' column. "
        "Run migration 016_session_repair_churn."
    )
    assert (
        "repair_churn_trigger" in columns
    ), "Session model is missing 'repair_churn_trigger' column."


def _make_db_stub(failed_count: int):
    """Return a minimal DB stub that returns failed_count for TaskExecution queries."""
    from app.models import TaskExecution, TaskStatus

    query_result = MagicMock()
    query_result.count.return_value = failed_count

    db = MagicMock()

    def _query(model):
        m = MagicMock()
        m.filter.return_value = query_result
        return m

    db.query.side_effect = _query
    return db


def test_repair_governor_fires_on_same_signature_repeat():
    from app.services.orchestration.execution.repair_governor import check_repair_churn

    db = _make_db_stub(failed_count=3)
    should_stop, trigger = check_repair_churn(
        db, session_id=1, task_id=1, completion_repair_attempts=0
    )
    assert should_stop is True
    assert trigger == "same_signature_repeat"


def test_repair_governor_fires_on_strategy_pivot_without_progress():
    from app.services.orchestration.execution.repair_governor import check_repair_churn

    db = _make_db_stub(failed_count=1)
    should_stop, trigger = check_repair_churn(
        db, session_id=1, task_id=1, completion_repair_attempts=2
    )
    assert should_stop is True
    assert trigger == "strategy_pivot_without_progress"


def test_repair_governor_fires_on_constrained_lane_streak():
    from app.services.orchestration.execution.repair_governor import check_repair_churn

    db = _make_db_stub(failed_count=2)
    should_stop, trigger = check_repair_churn(
        db,
        session_id=1,
        task_id=1,
        completion_repair_attempts=0,
        model_lane_label="local_constrained",
    )
    assert should_stop is True
    assert trigger == "constrained_lane_repair_failure_streak"


def test_repair_governor_does_not_fire_below_threshold():
    from app.services.orchestration.execution.repair_governor import check_repair_churn

    db = _make_db_stub(failed_count=1)
    should_stop, trigger = check_repair_churn(
        db,
        session_id=1,
        task_id=1,
        completion_repair_attempts=1,
        model_lane_label="hosted_openai",
    )
    assert should_stop is False
    assert trigger is None


def test_repair_governor_does_not_fire_constrained_lane_below_threshold():
    from app.services.orchestration.execution.repair_governor import check_repair_churn

    db = _make_db_stub(failed_count=1)
    should_stop, trigger = check_repair_churn(
        db,
        session_id=1,
        task_id=1,
        completion_repair_attempts=0,
        model_lane_label="local_constrained",
    )
    assert should_stop is False
    assert trigger is None


def test_planning_flow_line_count_does_not_exceed_gate():
    planning_flow = (
        REPO_ROOT / "app" / "services" / "orchestration" / "phases" / "planning_flow.py"
    )
    line_count = sum(1 for _ in planning_flow.open(encoding="utf-8"))
    assert line_count <= 2600, (
        f"planning_flow.py is {line_count} lines. "
        "D1/D2/D3 extractions should have reduced it below 2600. "
        "Do not add code to planning_flow.py without first extracting existing content."
    )
