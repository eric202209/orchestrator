"""Phase 22B-1X1 §7: deployment admission requires usable execution capacity.

R5 passed every readiness probe and still could not dispatch: the provider was
reachable while its only slot was held by a dead owner. Provider readiness
alone is therefore not sufficient for DEPLOYMENT_ADMISSION_PASSED.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SPEC_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "maintenance"
    / "dogfood_admission.py"
)


@pytest.fixture(scope="module")
def admission():
    spec = importlib.util.spec_from_file_location(
        "dogfood_admission_under_test", SPEC_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHA = "a" * 40
BUILD_TIME = "2026-08-02T00:00:00Z"
CONFIG_SHA = "b" * 64


def _identity():
    role = {"provider_ready": True, "backend": "local_openclaw"}
    return {
        "build_git_sha": SHA,
        "repo_git_sha": SHA,
        "build_time": BUILD_TIME,
        "configuration_sha256": CONFIG_SHA,
        "migration_status": "ok",
        "stale_container_check": "ok",
        "provider_role_matrix": {
            "planning": role,
            "execution": role,
            "repair": role,
            "debug_repair": role,
        },
        "active_backend_lanes": {"execution": "local_openclaw"},
    }


def _install_healthy_stack(monkeypatch, admission, capacity):
    responses = {
        "/api/v1/auth/me": {"email": "eval@local.dev"},
        "/health": {
            "status": "healthy",
            "checks": {"database": "ok", "redis": "ok", "backend": "ok"},
        },
        "/api/v1/ops/health": {
            "components": {
                "database": {"status": "ok"},
                "redis": {"status": "ok"},
                "qdrant": {"status": "ok"},
                "celery": {"status": "ok"},
                "maintenance": {"beat": {"configuration_status": "CONFIGURED"}},
            }
        },
        "/api/v1/ops/build-identity": _identity(),
        "/api/v1/ops/backends/health": {
            "runtime_lane": {"verdict": "ok"},
            "backends": [
                {
                    "name": "local_openclaw",
                    "available": True,
                    "ready": True,
                    "status": "ok",
                }
            ],
        },
        "/api/v1/ops/backends/capacity": capacity,
    }

    def fake_request_json(url, token=None):
        # Longest suffix first: "/api/v1/ops/health" also ends with "/health".
        for suffix in sorted(responses, key=len, reverse=True):
            if url.endswith(suffix):
                return responses[suffix]
        raise AssertionError(f"unexpected admission probe: {url}")

    monkeypatch.setattr(admission, "_request_json", fake_request_json)
    monkeypatch.setattr(
        admission,
        "_runtime_processes",
        lambda: {
            "backend": [(1, "uvicorn")],
            "worker": [(2, "celery")],
            "beat": [(3, "beat")],
        },
    )
    monkeypatch.setattr(
        admission,
        "_process_environment",
        lambda pid: {
            "ORCHESTRATOR_GIT_SHA": SHA,
            "ORCHESTRATOR_BUILD_TIME": BUILD_TIME,
            "ORCHESTRATOR_CONFIG_SHA256": CONFIG_SHA,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dogfood_admission.py",
            "--expected-sha",
            SHA,
            "--expected-build-time",
            BUILD_TIME,
            "--expected-config-sha256",
            CONFIG_SHA,
        ],
    )


def _capacity(**overrides):
    execution = {
        "backend_id": "local_openclaw",
        "capacity_available": True,
        "status_code": "capacity_available",
        "max_slots": 1,
        "active_valid_count": 0,
        "stale_reconciled_count": 0,
        "ambiguous_count": 0,
        "available_count": 1,
    }
    execution.update(overrides)
    return {
        "redis_available": True,
        "capacity_available": execution["capacity_available"],
        "status_code": execution["status_code"],
        "execution_backend": "local_openclaw",
        "roles": {"execution": execution},
    }


def test_available_capacity_passes_admission(admission, monkeypatch, capsys):
    _install_healthy_stack(monkeypatch, admission, _capacity())
    assert admission.main() == 0
    assert "DEPLOYMENT_ADMISSION_PASSED" in capsys.readouterr().out


def test_reachable_provider_without_capacity_fails_admission(
    admission, monkeypatch, capsys
):
    _install_healthy_stack(
        monkeypatch,
        admission,
        _capacity(
            capacity_available=False,
            status_code="backend_capacity_unavailable",
            active_valid_count=1,
            available_count=0,
        ),
    )
    assert admission.main() == 1
    captured = capsys.readouterr()
    assert "DEPLOYMENT_ADMISSION_PASSED" not in captured.out
    assert "backend_capacity_unavailable" in captured.err


def test_ambiguous_slot_ownership_fails_admission(admission, monkeypatch, capsys):
    _install_healthy_stack(
        monkeypatch,
        admission,
        _capacity(
            capacity_available=False,
            status_code="backend_slot_ownership_ambiguous",
            ambiguous_count=1,
            available_count=1,
        ),
    )
    assert admission.main() == 1
    assert "backend_slot_ownership_ambiguous" in capsys.readouterr().err


def test_reconciliation_failure_fails_admission(admission, monkeypatch, capsys):
    _install_healthy_stack(
        monkeypatch,
        admission,
        {
            "redis_available": False,
            "capacity_available": False,
            "status_code": "backend_slot_reconciliation_failed",
            "error": "redis down",
            "roles": {},
        },
    )
    assert admission.main() == 1
    err = capsys.readouterr().err
    assert "slot reconciliation reachable" in err
    assert "DEPLOYMENT_ADMISSION_FAILED" in err


def test_stale_lease_reconciled_then_admission_passes(admission, monkeypatch, capsys):
    _install_healthy_stack(
        monkeypatch, admission, _capacity(stale_reconciled_count=1, available_count=1)
    )
    assert admission.main() == 0
    assert "DEPLOYMENT_ADMISSION_PASSED" in capsys.readouterr().out
