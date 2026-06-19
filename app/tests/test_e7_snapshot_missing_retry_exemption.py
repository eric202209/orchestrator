"""Tests for Phase 13B-E7/E9: snapshot_missing retry-gate exemption with admin-event filter.

E7 added the exemption. E9 fixed the condition: filter out admin events written by
handle_task_failure itself (TASK_FAILED, CHECKPOINT_SAVED, HEALTH_SCORE_UPDATED) so
the exemption is reachable in real task-failure ordering.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.phases.failure_flow import (
    _HANDLE_TASK_FAILURE_ADMIN_EVENTS,
    _has_no_orchestration_events_for_retry,
    _prepare_retry_workspace,
)

_PERSISTENCE_PATH = (
    "app.services.orchestration.phases.failure_flow.read_orchestration_events"
)

# Canonical admin event values (what handle_task_failure writes before exemption check).
_ADMIN_EVENTS = [
    {"event_type": EventType.TASK_FAILED},
    {"event_type": EventType.HEALTH_SCORE_UPDATED},
    {"event_type": EventType.CHECKPOINT_SAVED},
    {"event_type": EventType.HEALTH_SCORE_UPDATED},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestration_state(project_dir: str = "/tmp/test-project") -> MagicMock:
    state = MagicMock()
    state.project_dir = project_dir
    return state


def _make_ctx(
    orchestration_state: object = None,
    session_id: int = 1,
    task_id: int = 42,
) -> MagicMock:
    ctx = MagicMock()
    ctx.db = MagicMock()
    ctx.session = MagicMock()
    ctx.session.instance_id = "test-instance"
    ctx.session_id = session_id
    ctx.task_id = task_id
    ctx.orchestration_state = orchestration_state or _make_orchestration_state()
    return ctx


def _restore_fn(result: dict) -> object:
    """Return a callable that returns *result* regardless of args."""

    def _fn(reason: str, force_restore: bool = False) -> dict:
        return result

    return _fn


# ---------------------------------------------------------------------------
# _HANDLE_TASK_FAILURE_ADMIN_EVENTS constant
# ---------------------------------------------------------------------------


def test_admin_events_constant_contains_expected_types():
    assert EventType.TASK_FAILED in _HANDLE_TASK_FAILURE_ADMIN_EVENTS
    assert EventType.CHECKPOINT_SAVED in _HANDLE_TASK_FAILURE_ADMIN_EVENTS
    assert EventType.HEALTH_SCORE_UPDATED in _HANDLE_TASK_FAILURE_ADMIN_EVENTS
    # Planning/execution events must NOT be admin events.
    assert EventType.PHASE_STARTED not in _HANDLE_TASK_FAILURE_ADMIN_EVENTS
    assert EventType.STEP_STARTED not in _HANDLE_TASK_FAILURE_ADMIN_EVENTS
    assert (
        EventType.PLANNING_REPAIR_ARBITRATION not in _HANDLE_TASK_FAILURE_ADMIN_EVENTS
    )


# ---------------------------------------------------------------------------
# _has_no_orchestration_events_for_retry
# ---------------------------------------------------------------------------


def test_helper_returns_true_when_events_empty():
    with patch(_PERSISTENCE_PATH, return_value=[]):
        assert (
            _has_no_orchestration_events_for_retry(
                orchestration_state=_make_orchestration_state(),
                session_id=1,
                task_id=42,
            )
            is True
        )


def test_helper_returns_true_when_only_admin_events():
    """Admin-only events = same as empty for exemption purposes (E9 fix)."""
    with patch(_PERSISTENCE_PATH, return_value=list(_ADMIN_EVENTS)):
        assert (
            _has_no_orchestration_events_for_retry(
                orchestration_state=_make_orchestration_state(),
                session_id=1,
                task_id=42,
            )
            is True
        )


def test_helper_returns_false_when_planning_event_present():
    """A planning/execution event blocks the exemption."""
    events = list(_ADMIN_EVENTS) + [{"event_type": EventType.PHASE_STARTED}]
    with patch(_PERSISTENCE_PATH, return_value=events):
        assert (
            _has_no_orchestration_events_for_retry(
                orchestration_state=_make_orchestration_state(),
                session_id=1,
                task_id=42,
            )
            is False
        )


def test_helper_returns_false_when_step_started_event_present():
    events = list(_ADMIN_EVENTS) + [{"event_type": EventType.STEP_STARTED}]
    with patch(_PERSISTENCE_PATH, return_value=events):
        assert (
            _has_no_orchestration_events_for_retry(
                orchestration_state=_make_orchestration_state(),
                session_id=1,
                task_id=42,
            )
            is False
        )


def test_helper_returns_false_when_planning_repair_arbitration_present():
    events = list(_ADMIN_EVENTS) + [
        {"event_type": EventType.PLANNING_REPAIR_ARBITRATION}
    ]
    with patch(_PERSISTENCE_PATH, return_value=events):
        assert (
            _has_no_orchestration_events_for_retry(
                orchestration_state=_make_orchestration_state(),
                session_id=1,
                task_id=42,
            )
            is False
        )


def test_helper_returns_false_on_read_exception():
    with patch(_PERSISTENCE_PATH, side_effect=OSError("disk error")):
        assert (
            _has_no_orchestration_events_for_retry(
                orchestration_state=_make_orchestration_state(),
                session_id=1,
                task_id=42,
            )
            is False
        )


def test_helper_returns_false_when_orchestration_state_none():
    assert (
        _has_no_orchestration_events_for_retry(
            orchestration_state=None,
            session_id=1,
            task_id=42,
        )
        is False
    )


def test_helper_returns_false_when_session_id_none():
    assert (
        _has_no_orchestration_events_for_retry(
            orchestration_state=_make_orchestration_state(),
            session_id=None,
            task_id=42,
        )
        is False
    )


# ---------------------------------------------------------------------------
# _prepare_retry_workspace: exemption — reachable scenarios
# ---------------------------------------------------------------------------


def test_snapshot_missing_empty_events_allows_direct_retry():
    """snapshot_missing + no events → retry NOT blocked, no checkpoint-resume."""
    ctx = _make_ctx()
    restore = _restore_fn({"restored": False, "reason": "snapshot_missing"})

    with patch(_PERSISTENCE_PATH, return_value=[]):
        workspace_restored, retry_kwargs, restore_blocked = _prepare_retry_workspace(
            ctx=ctx,
            exc=RuntimeError("setup failed before snapshot"),
            restore_workspace_snapshot_if_needed=restore,
            record_live_log_fn=MagicMock(),
            logger=MagicMock(),
            self_task=MagicMock(),
        )

    assert restore_blocked is False
    assert retry_kwargs is None or "resume_checkpoint_name" not in (retry_kwargs or {})


def test_snapshot_missing_only_admin_events_allows_direct_retry():
    """snapshot_missing + only admin events → retry allowed (real production scenario after E9).

    handle_task_failure writes TASK_FAILED, HEALTH_SCORE_UPDATED, CHECKPOINT_SAVED,
    HEALTH_SCORE_UPDATED before _prepare_retry_workspace is called. After E9 these
    admin events are filtered out, making the exemption reachable.
    """
    ctx = _make_ctx()
    restore = _restore_fn({"restored": False, "reason": "snapshot_missing"})

    with patch(_PERSISTENCE_PATH, return_value=list(_ADMIN_EVENTS)):
        workspace_restored, retry_kwargs, restore_blocked = _prepare_retry_workspace(
            ctx=ctx,
            exc=RuntimeError("pre-snapshot setup failure"),
            restore_workspace_snapshot_if_needed=restore,
            record_live_log_fn=MagicMock(),
            logger=MagicMock(),
            self_task=MagicMock(),
        )

    assert restore_blocked is False
    assert retry_kwargs is None or "resume_checkpoint_name" not in (retry_kwargs or {})


# ---------------------------------------------------------------------------
# _prepare_retry_workspace: exemption — blocked scenarios
# ---------------------------------------------------------------------------


def test_snapshot_missing_phase_started_blocks_retry():
    """snapshot_missing + PHASE_STARTED → planning ran, block preserved."""
    ctx = _make_ctx()
    restore = _restore_fn({"restored": False, "reason": "snapshot_missing"})
    events = list(_ADMIN_EVENTS) + [{"event_type": EventType.PHASE_STARTED}]

    with patch(_PERSISTENCE_PATH, return_value=events):
        _, _, restore_blocked = _prepare_retry_workspace(
            ctx=ctx,
            exc=RuntimeError("planning failed"),
            restore_workspace_snapshot_if_needed=restore,
            record_live_log_fn=MagicMock(),
            logger=MagicMock(),
            self_task=MagicMock(),
        )

    assert restore_blocked is True


def test_snapshot_missing_step_started_blocks_retry():
    """snapshot_missing + STEP_STARTED → execution ran, block preserved."""
    ctx = _make_ctx()
    restore = _restore_fn({"restored": False, "reason": "snapshot_missing"})
    events = list(_ADMIN_EVENTS) + [{"event_type": EventType.STEP_STARTED}]

    with patch(_PERSISTENCE_PATH, return_value=events):
        _, _, restore_blocked = _prepare_retry_workspace(
            ctx=ctx,
            exc=RuntimeError("step failed"),
            restore_workspace_snapshot_if_needed=restore,
            record_live_log_fn=MagicMock(),
            logger=MagicMock(),
            self_task=MagicMock(),
        )

    assert restore_blocked is True


def test_snapshot_missing_planning_repair_arbitration_blocks_retry():
    """snapshot_missing + PLANNING_REPAIR_ARBITRATION → repair ran, block preserved."""
    ctx = _make_ctx()
    restore = _restore_fn({"restored": False, "reason": "snapshot_missing"})
    events = list(_ADMIN_EVENTS) + [
        {"event_type": EventType.PLANNING_REPAIR_ARBITRATION}
    ]

    with patch(_PERSISTENCE_PATH, return_value=events):
        _, _, restore_blocked = _prepare_retry_workspace(
            ctx=ctx,
            exc=RuntimeError("repair failed"),
            restore_workspace_snapshot_if_needed=restore,
            record_live_log_fn=MagicMock(),
            logger=MagicMock(),
            self_task=MagicMock(),
        )

    assert restore_blocked is True


def test_restore_failed_only_admin_events_still_blocks():
    """restore_failed + only admin events → block remains (reason, not event count, matters)."""
    ctx = _make_ctx()
    restore = _restore_fn({"restored": False, "reason": "restore_failed:some error"})

    with patch(_PERSISTENCE_PATH, return_value=list(_ADMIN_EVENTS)):
        _, _, restore_blocked = _prepare_retry_workspace(
            ctx=ctx,
            exc=RuntimeError("restore errored"),
            restore_workspace_snapshot_if_needed=restore,
            record_live_log_fn=MagicMock(),
            logger=MagicMock(),
            self_task=MagicMock(),
        )

    assert restore_blocked is True


def test_empty_snapshot_preserved_reason_is_not_exempted():
    """empty_snapshot_preserved_existing_workspace → block regardless of events."""
    ctx = _make_ctx()
    restore = _restore_fn(
        {"restored": False, "reason": "empty_snapshot_preserved_existing_workspace"}
    )

    with patch(_PERSISTENCE_PATH, return_value=list(_ADMIN_EVENTS)):
        _, _, restore_blocked = _prepare_retry_workspace(
            ctx=ctx,
            exc=RuntimeError("empty snapshot"),
            restore_workspace_snapshot_if_needed=restore,
            record_live_log_fn=MagicMock(),
            logger=MagicMock(),
            self_task=MagicMock(),
        )

    assert restore_blocked is True


def test_event_read_exception_keeps_block():
    """Event read throws → fail-safe: block preserved, no exemption applied."""
    ctx = _make_ctx()
    restore = _restore_fn({"restored": False, "reason": "snapshot_missing"})

    with patch(_PERSISTENCE_PATH, side_effect=OSError("disk error")):
        _, _, restore_blocked = _prepare_retry_workspace(
            ctx=ctx,
            exc=RuntimeError("setup failed"),
            restore_workspace_snapshot_if_needed=restore,
            record_live_log_fn=MagicMock(),
            logger=MagicMock(),
            self_task=MagicMock(),
        )

    assert restore_blocked is True


def test_successful_restore_path_unchanged():
    """restored=True path still returns (True, None, False) unaffected by E7/E9."""
    ctx = _make_ctx()
    restore = _restore_fn({"restored": True, "files_restored": 5})

    workspace_restored, retry_kwargs, restore_blocked = _prepare_retry_workspace(
        ctx=ctx,
        exc=RuntimeError("some execution error"),
        restore_workspace_snapshot_if_needed=restore,
        record_live_log_fn=MagicMock(),
        logger=MagicMock(),
        self_task=MagicMock(),
    )

    assert workspace_restored is True
    assert retry_kwargs is None
    assert restore_blocked is False


def test_no_restore_fn_path_unchanged():
    """No restore fn → restore_result is None → blocked=False (existing behavior)."""
    ctx = _make_ctx()

    _, _, restore_blocked = _prepare_retry_workspace(
        ctx=ctx,
        exc=RuntimeError("some error"),
        restore_workspace_snapshot_if_needed=None,
        record_live_log_fn=MagicMock(),
        logger=MagicMock(),
        self_task=MagicMock(),
    )

    assert restore_blocked is False
