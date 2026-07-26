"""Narrow regressions for the Phase 31C-R1 corrective seams."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.orchestration.execution.step_support import (
    repair_step_commands_with_self_correction,
)
from scripts.maintenance.phase31_certification_facts import (
    assemble_repair_telemetry_from_live_run,
    assemble_timing_facts_from_live_run,
)


class _EmptyPrimaryRuntime:
    runtime_configuration = object()

    async def execute_task(self, prompt, timeout_seconds=120):
        return {"output": "", "error": "primary lane returned no output"}


class _RoleRepairRuntime:
    async def execute_task(self, prompt, timeout_seconds=120):
        return {
            "output": json.dumps(
                {
                    "description": "repair the malformed step",
                    "commands": ["python -m pytest -q"],
                    "verification": "python -m pytest -q",
                }
            )
        }


def test_step_repair_uses_role_owned_lane_after_empty_primary_output(
    monkeypatch, tmp_path
):
    fallback = _RoleRepairRuntime()
    monkeypatch.setattr(
        "app.services.agents.agent_runtime.create_agent_runtime",
        lambda *args, **kwargs: fallback,
    )
    logs = []
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    repaired = repair_step_commands_with_self_correction(
        runtime_service=_EmptyPrimaryRuntime(),
        db=db,
        session_id=1,
        task_id=2,
        session_instance_id="instance",
        task_prompt="repair the step",
        step={"description": "repair", "commands": []},
        step_index=0,
        project_dir=tmp_path,
        prior_results_summary="",
        project_context="",
        logger_obj=logging.getLogger("phase31c-r1-test"),
        extract_structured_text=lambda value: str(value or ""),
        normalize_step=lambda data, *_args: data,
        record_live_log=lambda *args, **kwargs: logs.append(kwargs.get("metadata")),
    )

    assert repaired["commands"] == ["python -m pytest -q"]
    assert logs[-1]["repair_runtime_fallback"] is True


def test_step_repair_single_model_keeps_strict_json_path(tmp_path):
    logs = []
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    repaired = repair_step_commands_with_self_correction(
        runtime_service=_RoleRepairRuntime(),
        db=db,
        session_id=1,
        task_id=2,
        session_instance_id="instance",
        task_prompt="repair the step",
        step={"description": "repair", "commands": []},
        step_index=0,
        project_dir=tmp_path,
        prior_results_summary="",
        project_context="",
        logger_obj=logging.getLogger("phase31c-r1-single-model-test"),
        extract_structured_text=lambda value: str(value or ""),
        normalize_step=lambda data, *_args: data,
        record_live_log=lambda *args, **kwargs: logs.append(kwargs.get("metadata")),
    )

    assert repaired["commands"] == ["python -m pytest -q"]
    assert logs[-1]["repair_runtime_fallback"] is False


def test_certification_helpers_expose_structured_repair_and_plan_timing(monkeypatch):
    base = datetime(2026, 7, 26, 13, 0, tzinfo=timezone.utc)
    entries = [
        SimpleNamespace(
            id=1,
            created_at=base,
            message="unrelated prose must not be parsed",
            log_metadata=json.dumps({"phase": "planning"}),
        ),
        SimpleNamespace(
            id=2,
            created_at=base.replace(second=12),
            message="planning completed",
            log_metadata=json.dumps({"phase": "planning", "steps": 2}),
        ),
        SimpleNamespace(
            id=3,
            created_at=base.replace(second=15),
            message="repair completed",
            log_metadata=json.dumps(
                {
                    "phase": "planning",
                    "attempt": "repair",
                    "duration_seconds": 2.5,
                    "repair_attempts": 1,
                }
            ),
        ),
        SimpleNamespace(
            id=4,
            created_at=base.replace(second=16),
            message="final repair outcome",
            log_metadata=json.dumps(
                {
                    "phase": "planning",
                    "repair_attempt_count": 1,
                    "target_outcomes": {
                        "weak_verification": {
                            "target_final_status": "RESOLVED",
                            "repair_outcome_consistent": True,
                        }
                    },
                }
            ),
        ),
    ]
    monkeypatch.setattr(
        "scripts.maintenance.phase31_certification_facts._completion_log_entries",
        lambda db, session_id, task_id: entries,
    )

    timings = assemble_timing_facts_from_live_run(object(), session_id=1, task_id=2)
    telemetry = assemble_repair_telemetry_from_live_run(
        object(), session_id=1, task_id=2
    )

    assert timings["time_to_valid_plan_seconds"] == 12.0
    assert timings["planning_repair_durations_seconds"] == [2.5]
    assert [item["event"] for item in telemetry] == [
        "planning_repair_completed",
        "planning_repair_outcome_final",
    ]
    assert (
        telemetry[1]["metadata"]["target_outcomes"]["weak_verification"][
            "repair_outcome_consistent"
        ]
        is True
    )
