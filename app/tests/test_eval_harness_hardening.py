"""Regression tests for eval-harness hardening changes."""

from __future__ import annotations

import base64
import importlib.util
import json
import time
from pathlib import Path
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest


# ---------------------------------------------------------------------------
# Module loaders
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "evals" / "run_orchestrator_eval_slice.py").is_file():
            return parent
    pytest.skip("repo scripts not present", allow_module_level=True)


def _load_runner():
    path = _repo_root() / "scripts" / "evals" / "run_orchestrator_eval_slice.py"
    spec = importlib.util.spec_from_file_location("run_orchestrator_eval_slice", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_scorer():
    path = _repo_root() / "scripts" / "maintenance" / "score_orchestrator_eval_case.py"
    spec = importlib.util.spec_from_file_location("score_orchestrator_eval_case", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner = _load_runner()
scorer = _load_scorer()


# ---------------------------------------------------------------------------
# Task 3: scorer — paused/failed session alignment
# ---------------------------------------------------------------------------


def test_is_session_aborted_true_for_paused_with_failure_category():
    assert scorer._is_session_aborted("paused", "backend_timeout") is True


def test_is_session_aborted_true_for_failed_with_failure_category():
    assert scorer._is_session_aborted("failed", "worker_oom") is True


def test_is_session_aborted_false_for_paused_without_failure_category():
    assert scorer._is_session_aborted("paused", None) is False
    assert scorer._is_session_aborted("paused", "") is False


def test_is_session_aborted_false_for_completed():
    assert scorer._is_session_aborted("completed", "backend_timeout") is False


def test_derive_clean_success_aborted_session_returns_single_blocker():
    success, blockers = scorer._derive_clean_success(
        case={},
        verifier={"passed": False},
        files={
            "missing_required_files": ["pyproject.toml"],
            "present_forbidden_existing_files": [],
        },
        scope={"forbidden_touched_files": []},
        event_summary={
            "task_completed": False,
            "task_failed": False,
            "divergence_detected": False,
            "intent_outcome_mismatch_count": 0,
            "workspace_contract_failed_count": 0,
            "retry_count": 0,
            "repair_events": {
                "debug_feedback_captured": 0,
                "debug_repair_attempted": 0,
                "repair_generated": 0,
                "repair_applied": 0,
                "repair_rejected": 0,
            },
            "checkpoint_events": {
                "checkpoint_saved": 0,
                "checkpoint_loaded": 0,
                "checkpoint_cursor_reconciled": 0,
                "checkpoint_redirected": 0,
                "resume_workspace_drift": 0,
                "workspace_retry_dirty": 0,
            },
            "health_score_min": None,
            "health_score_final": None,
            "event_count": 0,
            "event_type_counts": {},
        },
        snapshot_summary={"state_snapshot_present": False},
        session_status="paused",
        failure_category="backend_timeout",
    )

    assert success is False
    assert blockers == ["session_aborted:backend_timeout"]
    assert "task_completed_event_missing" not in blockers
    assert "verifier_failed" not in blockers
    assert "required_files_missing" not in blockers


def test_score_case_aborted_session_sets_primary_failure_phase(tmp_path):
    events_dir = tmp_path / ".agent" / "events"
    events_dir.mkdir(parents=True)

    manifest = {
        "benchmark_id": "test",
        "baseline_label": "test",
        "schema_version": 1,
        "cases": [
            {
                "case_id": "python_cli_small_feature",
                "category": "baseline_success",
                "required_events": ["task_started"],
                "verifier": {"command": "true", "timeout_seconds": 5},
                "required_files": [],
                "forbidden_existing_files": [],
                "allowed_touched_prefixes": [],
                "forbidden_touched_prefixes": [],
            }
        ],
    }
    case = manifest["cases"][0]

    report = scorer._score_case(
        manifest_path=tmp_path / "manifest.json",
        manifest=manifest,
        case=case,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        python_executable=None,
        session_status="paused",
        failure_category="backend_timeout",
    )

    assert report["result"]["clean_success"] is False
    assert report["result"]["session_aborted"] is True
    assert report["path_observability"]["primary_failure_phase"] == "session_aborted"
    assert report["result"]["blockers"] == ["session_aborted:backend_timeout"]


def test_score_case_normal_session_does_not_set_session_aborted(tmp_path):
    events_dir = tmp_path / ".agent" / "events"
    events_dir.mkdir(parents=True)

    manifest = {
        "benchmark_id": "test",
        "baseline_label": "test",
        "schema_version": 1,
        "cases": [
            {
                "case_id": "python_cli_small_feature",
                "category": "baseline_success",
                "required_events": ["task_started"],
                "verifier": {"command": "true", "timeout_seconds": 5},
                "required_files": [],
                "forbidden_existing_files": [],
                "allowed_touched_prefixes": [],
                "forbidden_touched_prefixes": [],
            }
        ],
    }
    case = manifest["cases"][0]

    report = scorer._score_case(
        manifest_path=tmp_path / "manifest.json",
        manifest=manifest,
        case=case,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
        python_executable=None,
        session_status="completed",
        failure_category=None,
    )

    assert report["result"]["session_aborted"] is False
    assert (
        report["path_observability"].get("primary_failure_phase") != "session_aborted"
    )


# ---------------------------------------------------------------------------
# Task 5: scorer env_summary present in every report
# ---------------------------------------------------------------------------


def test_score_case_report_includes_env_summary(tmp_path):
    manifest = {
        "benchmark_id": "test",
        "baseline_label": "test",
        "schema_version": 1,
        "cases": [
            {
                "case_id": "python_cli_small_feature",
                "category": "baseline_success",
                "required_events": ["task_started"],
                "verifier": {"command": "true", "timeout_seconds": 5},
                "required_files": [],
                "forbidden_existing_files": [],
                "allowed_touched_prefixes": [],
                "forbidden_touched_prefixes": [],
            }
        ],
    }
    case = manifest["cases"][0]

    report = scorer._score_case(
        manifest_path=tmp_path / "manifest.json",
        manifest=manifest,
        case=case,
        project_dir=tmp_path,
        session_id=1,
        task_id=1,
    )

    assert "env_summary" in report
    env = report["env_summary"]
    assert "git_sha" in env
    assert "backend" in env
    assert "model" in env
    assert "worker_pid" in env
    assert "workspace_root" in env
    assert "runtime_profile" in env
    assert "timeout_env" in env
    assert isinstance(env["timeout_env"], dict)
    assert env["workspace_root"] == str(tmp_path.parent)


# ---------------------------------------------------------------------------
# Task 1: runner — workspace root validation from API
# ---------------------------------------------------------------------------


def test_resolve_workspace_root_skips_nonexistent_api_path(
    monkeypatch, tmp_path, capsys
):
    def fake_request_json(method, base_url, path, token, payload=None):
        return {"system": {"workspace_root": "/nonexistent/fake/path/from/api"}}

    monkeypatch.setattr(runner, "_request_json", fake_request_json)

    with pytest.raises(SystemExit):
        runner._resolve_workspace_root(None, "http://localhost/api/v1", "token")

    captured = capsys.readouterr()
    assert "/nonexistent/fake/path/from/api" in captured.err
    assert "does not exist" in captured.err


def test_resolve_workspace_root_uses_existing_api_path(monkeypatch, tmp_path):
    def fake_request_json(method, base_url, path, token, payload=None):
        return {"system": {"workspace_root": str(tmp_path)}}

    monkeypatch.setattr(runner, "_request_json", fake_request_json)

    result = runner._resolve_workspace_root(None, "http://localhost/api/v1", "token")
    assert result == tmp_path


def test_resolve_workspace_root_prefers_explicit_arg(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner,
        "_request_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call API")),
    )

    result = runner._resolve_workspace_root(tmp_path, "http://localhost/api/v1", None)
    assert result == tmp_path


# ---------------------------------------------------------------------------
# Task 2: runner — token lifetime warning
# ---------------------------------------------------------------------------


def _make_jwt(exp: int) -> str:
    header = (
        base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    )
    payload_bytes = json.dumps({"sub": "user@test", "exp": exp}).encode()
    payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def test_preflight_warn_token_lifetime_warns_when_short(capsys):
    exp = int(time.time()) + 60
    token = _make_jwt(exp)
    runner._preflight_warn_token_lifetime(token, timeout_seconds=3600)
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "token expires" in captured.err


def test_preflight_warn_token_lifetime_silent_when_sufficient(capsys):
    exp = int(time.time()) + 7200
    token = _make_jwt(exp)
    runner._preflight_warn_token_lifetime(token, timeout_seconds=3600)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_preflight_warn_token_lifetime_silent_on_malformed_token(capsys):
    runner._preflight_warn_token_lifetime("not-a-jwt", timeout_seconds=3600)
    captured = capsys.readouterr()
    assert captured.err == ""
