from __future__ import annotations

REAL_REPLAY_REPORTS = {
    "session_43_task_5_failed.semantic.json": {
        "reducer_version": "phase4a-v1",
        "compatibility_version": "phase4a-compat-v1",
        "integrity": {"confidence": "high", "event_count_applied": 57},
        "state": {
            "session_id": 43,
            "task_id": 5,
            "phase": "planning",
            "status": "repair_timeout",
            "validation_verdict_status_history": ["accepted", "rejected"],
        },
    },
    "session_43_task_8_completed_then_cancelled.semantic.json": {
        "reducer_version": "phase4a-v1",
        "compatibility_version": "phase4a-compat-v1",
        "integrity": {"confidence": "high", "event_count_applied": 58},
        "state": {
            "session_id": 43,
            "task_id": 8,
            "phase": "executing",
            "status": "executing",
            "validation_verdict_status_history": ["accepted"] * 10,
        },
    },
    "session_43_task_9_planning_timeout.semantic.json": {
        "reducer_version": "phase4a-v1",
        "compatibility_version": "phase4a-compat-v1",
        "integrity": {"confidence": "high", "event_count_applied": 7},
        "state": {
            "session_id": 43,
            "task_id": 9,
            "phase": "planning",
            "status": "started",
            "latest_failure_event_id": "phase6b-real-planning-timeout",
        },
    },
}


def _load(name: str) -> dict:
    return REAL_REPLAY_REPORTS[name]


def test_real_failed_session_replay_fixture_pins_planning_repair_timeout():
    report = _load("session_43_task_5_failed.semantic.json")

    assert report["reducer_version"] == "phase4a-v1"
    assert report["compatibility_version"] == "phase4a-compat-v1"
    assert report["integrity"]["confidence"] == "high"
    assert report["integrity"]["event_count_applied"] == 57
    assert report["state"]["session_id"] == 43
    assert report["state"]["task_id"] == 5
    assert report["state"]["phase"] == "planning"
    assert report["state"]["status"] == "repair_timeout"
    assert "rejected" in report["state"]["validation_verdict_status_history"]


def test_real_completed_attempt_replay_fixture_keeps_full_journal_semantics():
    report = _load("session_43_task_8_completed_then_cancelled.semantic.json")

    assert report["reducer_version"] == "phase4a-v1"
    assert report["compatibility_version"] == "phase4a-compat-v1"
    assert report["integrity"]["confidence"] == "high"
    assert report["integrity"]["event_count_applied"] == 58
    assert report["state"]["session_id"] == 43
    assert report["state"]["task_id"] == 8
    assert report["state"]["phase"] == "executing"
    assert report["state"]["status"] == "executing"
    assert report["state"]["validation_verdict_status_history"].count("accepted") == 10


def test_real_planning_timeout_fixture_pins_sparse_journal_boundary():
    report = _load("session_43_task_9_planning_timeout.semantic.json")

    assert report["reducer_version"] == "phase4a-v1"
    assert report["compatibility_version"] == "phase4a-compat-v1"
    assert report["integrity"]["confidence"] == "high"
    assert report["integrity"]["event_count_applied"] == 7
    assert report["state"]["session_id"] == 43
    assert report["state"]["task_id"] == 9
    assert report["state"]["phase"] == "planning"
    assert report["state"]["status"] == "started"
    assert report["state"]["latest_failure_event_id"] is not None
