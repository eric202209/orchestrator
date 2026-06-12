"""
Unit tests for wm_off_runner_v3 pure-logic functions.
No DB, Redis, or HTTP required.
"""
import sys
import os

from scripts.maintenance._runner_common import chdir_repo_root, ensure_repo_on_syspath

ensure_repo_on_syspath()
chdir_repo_root()

import pytest

# Import the functions under test directly from the module
from scripts.maintenance.wm_off_runner_v3 import (
    is_already_running_error,
    is_env_capacity_failure,
    is_pythonpath_repair,
    count_debug_repairs,
    count_planning_repairs,
)


# ── is_already_running_error ──────────────────────────────────────────────────

class TestIsAlreadyRunningError:
    def test_exact_api_message(self):
        assert is_already_running_error(
            "Task is already running; active execution is in progress."
        )

    def test_case_insensitive(self):
        assert is_already_running_error("Task Is Already Running right now")

    def test_empty(self):
        assert not is_already_running_error("")

    def test_unrelated_error(self):
        assert not is_already_running_error("Earlier ordered tasks must finish first.")

    def test_blocked_by_prior(self):
        assert not is_already_running_error(
            "Blocked by: #1 Bootstrap package (failed)"
        )

    def test_capacity_limit(self):
        assert not is_already_running_error("backend_capacity_limit reached")


# ── is_env_capacity_failure ───────────────────────────────────────────────────

def _claimed_events(n: int) -> list:
    return [{"event_type": "task_claimed"} for _ in range(n)]


class TestIsEnvCapacityFailure:
    def test_five_claims_no_exec(self):
        # 5 claimed events, no step_started → capacity limit pattern
        events = _claimed_events(5)
        assert is_env_capacity_failure(events, "failed")

    def test_three_claims_below_threshold(self):
        # Only 3 claimed — not a capacity pattern
        events = _claimed_events(3)
        assert not is_env_capacity_failure(events, "failed")

    def test_execution_reached_not_capacity(self):
        # Even if 5 claims, execution_reached=True means it wasn't purely capacity
        events = _claimed_events(5) + [{"event_type": "step_started"}]
        assert not is_env_capacity_failure(events, "failed")

    def test_status_contains_capacity(self):
        assert is_env_capacity_failure([], "backend_capacity_limit")

    def test_done_status_not_capacity(self):
        events = _claimed_events(5)
        assert not is_env_capacity_failure(events, "done")

    def test_empty_events_done(self):
        assert not is_env_capacity_failure([], "done")


# ── is_pythonpath_repair ──────────────────────────────────────────────────────

class TestIsPythonpathRepair:
    def test_import_error_class(self):
        assert is_pythonpath_repair(["import_error"], [])

    def test_importerror_substring(self):
        assert is_pythonpath_repair(["ImportError"], [])

    def test_modulenotfound(self):
        assert is_pythonpath_repair(["ModuleNotFoundError"], [])

    def test_venv_in_class(self):
        assert is_pythonpath_repair(["venv_activation_failed"], [])

    def test_pythonpath_keyword(self):
        assert is_pythonpath_repair(["PYTHONPATH_missing"], [])

    def test_plan_reason_import(self):
        assert is_pythonpath_repair([], [["use PYTHONPATH=. to run pytest"]])

    def test_unrelated_class(self):
        assert not is_pythonpath_repair(["completion_validation_failed"], [])

    def test_empty(self):
        assert not is_pythonpath_repair([], [])

    def test_pytest_failure_not_pythonpath(self):
        assert not is_pythonpath_repair(["pytest_failure"], [])


# ── count_debug_repairs ───────────────────────────────────────────────────────

class TestCountDebugRepairs:
    def test_no_events(self):
        count, classes = count_debug_repairs([])
        assert count == 0
        assert classes == []

    def test_one_repair(self):
        events = [
            {"event_type": "debug_repair_attempted",
             "details": {"debug_failure_class": "import_error"}},
        ]
        count, classes = count_debug_repairs(events)
        assert count == 1
        assert classes == ["import_error"]

    def test_two_repairs(self):
        events = [
            {"event_type": "debug_repair_attempted",
             "details": {"debug_failure_class": "completion_validation_failed"}},
            {"event_type": "step_started", "details": {}},
            {"event_type": "debug_repair_attempted",
             "details": {"debug_failure_class": "pytest_failure"}},
        ]
        count, classes = count_debug_repairs(events)
        assert count == 2
        assert "completion_validation_failed" in classes
        assert "pytest_failure" in classes

    def test_missing_class_defaults_unknown(self):
        events = [{"event_type": "debug_repair_attempted", "details": {}}]
        count, classes = count_debug_repairs(events)
        assert count == 1
        assert classes == ["unknown"]

    def test_ignores_non_repair_events(self):
        events = [
            {"event_type": "step_started", "details": {}},
            {"event_type": "validation_result", "details": {"stage": "plan"}},
        ]
        count, classes = count_debug_repairs(events)
        assert count == 0


# ── count_planning_repairs ────────────────────────────────────────────────────

class TestCountPlanningRepairs:
    def test_no_events(self):
        count, reasons = count_planning_repairs([])
        assert count == 0
        assert reasons == []

    def test_one_plan_repair(self):
        events = [
            {"event_type": "validation_result", "details": {
                "stage": "plan",
                "status": "repair_required",
                "reasons": ["weak verification command"],
            }},
        ]
        count, reasons = count_planning_repairs(events)
        assert count == 1
        assert reasons[0] == ["weak verification command"]

    def test_ignores_accepted_plan(self):
        events = [
            {"event_type": "validation_result", "details": {
                "stage": "plan", "status": "accepted", "reasons": [],
            }},
        ]
        count, _ = count_planning_repairs(events)
        assert count == 0

    def test_ignores_step_completion_repair(self):
        events = [
            {"event_type": "validation_result", "details": {
                "stage": "step_completion",
                "status": "repair_required",
                "reasons": ["verification failed"],
            }},
        ]
        count, _ = count_planning_repairs(events)
        assert count == 0

    def test_multiple_plan_repairs(self):
        events = [
            {"event_type": "validation_result", "details": {
                "stage": "plan", "status": "repair_required",
                "reasons": ["reason A"],
            }},
            {"event_type": "validation_result", "details": {
                "stage": "plan", "status": "repair_required",
                "reasons": ["reason B"],
            }},
        ]
        count, reasons = count_planning_repairs(events)
        assert count == 2


# ── Eligibility logic (inline, not a separate function in runner) ─────────────

class TestEligibilityLogic:
    """Test the eligibility filtering logic that the runner's summary uses."""

    def _make_result(self, pos, status, exec_reached, env_cap):
        return {
            "plan_position": pos,
            "status": status,
            "execution_reached": exec_reached,
            "env_capacity_failure": env_cap,
            "debug_repair_count": 0,
            "pythonpath_constraint_repair": False,
        }

    def _eligible(self, results):
        return [
            r for r in results
            if r["plan_position"] > 1
            and r["status"] in ("done", "failed")
            and r["execution_reached"]
            and not r["env_capacity_failure"]
        ]

    def test_t1_excluded(self):
        r = self._make_result(1, "done", True, False)
        assert self._eligible([r]) == []

    def test_t2_done_eligible(self):
        r = self._make_result(2, "done", True, False)
        assert len(self._eligible([r])) == 1

    def test_t2_failed_exec_reached_eligible(self):
        r = self._make_result(2, "failed", True, False)
        assert len(self._eligible([r])) == 1

    def test_t2_failed_no_exec_not_eligible(self):
        r = self._make_result(2, "failed", False, False)
        assert self._eligible([r]) == []

    def test_env_capacity_excluded(self):
        r = self._make_result(2, "failed", False, True)
        assert self._eligible([r]) == []

    def test_blocked_not_eligible(self):
        r = self._make_result(2, "blocked_prior_task_failed", False, False)
        assert self._eligible([r]) == []

    def test_timeout_not_eligible(self):
        r = self._make_result(2, "runner_timeout__running", False, False)
        assert self._eligible([r]) == []
