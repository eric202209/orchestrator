"""Shared test helpers for the planner timeout regression test family.

Phase 20D split ``test_planner_timeout_regressions.py`` into several
scenario-specific test modules. These helpers were used across multiple
scenario families in the original monolithic file, so they live here once
instead of being duplicated in each split file.
"""

from app.services.agents.openclaw_service import OpenClawSessionService


def _valid_three_step_plan():
    return [
        {
            "step_number": 1,
            "description": "Inspect current planning modules",
            "commands": ['rg -n "PlannerService" app/services/orchestration/planning'],
            "verification": "python3 -c \"print('inspect ok')\"",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Update planner timeout handling",
            "commands": ["printf 'ok\\n' > planner_timeout_marker.txt"],
            "verification": "python3 - <<'PY'\nfrom pathlib import Path\nassert Path('planner_timeout_marker.txt').read_text() == 'ok\\n'\nPY",
            "rollback": "rm -f planner_timeout_marker.txt",
            "expected_files": ["planner_timeout_marker.txt"],
        },
        {
            "step_number": 3,
            "description": "Verify planner tests",
            "commands": [
                "python3 -m pytest app/tests/test_planner_timeout_regressions.py -q"
            ],
            "verification": "python3 -m pytest app/tests/test_planner_timeout_regressions.py -q",
            "rollback": None,
            "expected_files": [],
        },
    ]


def _patch_planning_flow_external_writes(monkeypatch):
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control.append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.write_orchestration_state_snapshot",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.emit_phase_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_repair_arbitration_control.emit_phase_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.assemble_planning_prompt",
        lambda *args, **kwargs: "mock planning prompt",
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._retrieve_knowledge",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.record_validation_verdict",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.maybe_emit_divergence_detected",
        lambda *args, **kwargs: None,
    )


def _openclaw_parse_service():
    service = object.__new__(OpenClawSessionService)
    service.logged_entries = []

    def log_entry(level, message, metadata=None, **kwargs):
        service.logged_entries.append(
            {
                "level": level,
                "message": message,
                "metadata": metadata,
                **kwargs,
            }
        )

    service._log_entry = log_entry
    return service
