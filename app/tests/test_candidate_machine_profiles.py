"""Phase 17G: Candidate Recovery flag/profile trigger tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.phases.planning_candidate_recovery import (
    candidate_recovery_precheck,
)


def _verdict(status: str):
    return SimpleNamespace(
        status=status,
        accepted=status in {"accepted", "warning"},
    )


def test_candidate_recovery_precheck_requires_flag(monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", False)
    monkeypatch.setattr("app.config.settings.RUNTIME_PROFILE", "standard")

    assert candidate_recovery_precheck(SimpleNamespace(), _verdict("rejected")) is False


def test_candidate_recovery_precheck_requires_machine_a(monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.RUNTIME_PROFILE", "medium")

    assert candidate_recovery_precheck(SimpleNamespace(), _verdict("rejected")) is False


def test_candidate_recovery_precheck_allows_standard_repair_required(monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.RUNTIME_PROFILE", "standard")

    assert (
        candidate_recovery_precheck(SimpleNamespace(), _verdict("repair_required"))
        is True
    )


def test_candidate_recovery_precheck_rejects_accepted_verdict(monkeypatch):
    monkeypatch.setattr("app.config.settings.CANDIDATE_RECOVERY_ENABLED", True)
    monkeypatch.setattr("app.config.settings.RUNTIME_PROFILE", "standard")

    assert candidate_recovery_precheck(SimpleNamespace(), _verdict("accepted")) is False
