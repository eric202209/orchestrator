"""Phase 10S: Runtime boundary enforcement tests.

Verifies that:
- Every public normalizer function and every _static_site_* function in
  planning_flow.py has a registry entry.
- All deprecated_artifact entries have allowed_to_expand=False.
- The two deprecated artifacts are not imported outside planning_flow.py.
- The repair_churn_stopped column exists in the Session model.
- The repair governor fires correctly for each trigger.
- The repair governor does not fire below threshold.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]  # orchestrator/
APP_ROOT = REPO_ROOT / "app"


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


def _static_site_function_names_in_planning_flow() -> list[str]:
    source = (
        APP_ROOT / "services" / "orchestration" / "phases" / "planning_flow.py"
    ).read_text()
    tree = ast.parse(source)
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_static_site_")
    ]


def test_rule_registry_has_no_unlabeled_normalizer():
    from app.services.orchestration.rule_registry import RULE_REGISTRY

    public_normalizers = _public_normalizer_function_names()
    static_site_fns = _static_site_function_names_in_planning_flow()

    # Map source_location prefixes to rule_ids for lookup
    registered_locations = {r.source_location for r in RULE_REGISTRY.values()}

    for fn_name in public_normalizers:
        matching = [r for r in RULE_REGISTRY.values() if fn_name in r.source_location]
        assert matching, (
            f"Public normalizer '{fn_name}' has no entry in RULE_REGISTRY. "
            "Add an entry before modifying this function."
        )

    for fn_name in static_site_fns:
        matching = [r for r in RULE_REGISTRY.values() if fn_name in r.source_location]
        assert matching, (
            f"_static_site_* function '{fn_name}' in planning_flow.py has no "
            "entry in RULE_REGISTRY. Add an entry before modifying this function."
        )


def test_deprecated_artifacts_have_allowed_to_expand_false():
    from app.services.orchestration.rule_registry import RULE_REGISTRY

    deprecated = [
        r for r in RULE_REGISTRY.values() if r.owner_layer == "deprecated_artifact"
    ]
    assert deprecated, "Expected at least one deprecated_artifact in the registry."

    for rule in deprecated:
        assert rule.allowed_to_expand is False, (
            f"Rule '{rule.rule_id}' is a deprecated_artifact but has "
            f"allowed_to_expand={rule.allowed_to_expand}. "
            "deprecated_artifact rules must have allowed_to_expand=False."
        )


def test_deprecated_artifacts_only_called_from_planning_flow():
    """Verify that deprecated normalizers are not imported outside planning_flow.py."""
    deprecated_function_names = {
        "normalize_existing_static_site_plan",
        "_static_site_validation_fallback_plan",
    }

    py_files = list(APP_ROOT.rglob("*.py"))
    violations: list[str] = []

    for py_file in py_files:
        relative = py_file.relative_to(APP_ROOT)
        # Skip definition sites, the registry (names them in strings), and tests
        if "planning_flow" in py_file.name:
            continue
        if "normalization" in py_file.name:
            continue
        if "rule_registry" in py_file.name:
            continue
        if "test_" in py_file.name:
            continue
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for fn_name in deprecated_function_names:
            if fn_name in source:
                violations.append(f"{relative}: references '{fn_name}'")

    assert (
        not violations
    ), "Deprecated artifact(s) referenced outside their expected files:\n" + "\n".join(
        violations
    )


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
