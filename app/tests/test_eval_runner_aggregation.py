from __future__ import annotations

from io import BytesIO
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest


def _repo_script_path() -> Path:
    relative = Path("scripts/evals/run_orchestrator_eval_slice.py")
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    pytest.skip(
        f"Optional eval runner script not present: {relative}", allow_module_level=True
    )


def _load_runner_module():
    path = _repo_script_path()
    spec = importlib.util.spec_from_file_location("run_orchestrator_eval_slice", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner_module()


def _report(
    *,
    clean_success: bool,
    primary_failure_phase: str | None,
    path_observed: bool,
    intended_path_observed: bool,
    execution_reached: bool,
    debug_repair_reached: bool,
    phase7f_used: bool,
    phase7g_used: bool,
    blockers: list[str],
) -> dict:
    return {
        "result": {
            "clean_success": clean_success,
            "path_observed": path_observed,
            "blockers": blockers,
        },
        "path_observability": {
            "primary_failure_phase": primary_failure_phase,
            "intended_path_observed": intended_path_observed,
            "execution_reached": execution_reached,
            "debug_repair_reached": debug_repair_reached,
            "phase7f_used": phase7f_used,
            "phase7g_used": phase7g_used,
        },
    }


def test_aggregate_case_reports_counts_path_observability_and_metadata():
    reports = [
        _report(
            clean_success=False,
            primary_failure_phase="debug_repair",
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=True,
            phase7f_used=True,
            phase7g_used=False,
            blockers=["verifier_failed"],
        ),
        _report(
            clean_success=False,
            primary_failure_phase="debug_repair",
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=True,
            phase7f_used=True,
            phase7g_used=True,
            blockers=["verifier_failed"],
        ),
        _report(
            clean_success=True,
            primary_failure_phase=None,
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=False,
            phase7f_used=False,
            phase7g_used=False,
            blockers=[],
        ),
    ]

    aggregate = runner._aggregate_case_reports(
        case_id="python_cli_small_feature",
        reports=reports,
        report_paths=[Path("run1.json"), Path("run2.json"), Path("run3.json")],
        run_context={
            "git_sha": "abc123",
            "model": "qwen-local",
            "backend": "local_openclaw",
            "runtime_profile": "standard",
            "repeat_seed": "seed-1",
        },
        score_readiness=[
            {
                "event_journal_path": "workspace-r01/.openclaw/events/a.jsonl",
                "required_terminal_event": "task_completed",
                "observed_terminal_event": "task_completed",
                "stabilized": True,
            },
            {
                "event_journal_path": "workspace-r02/.openclaw/events/b.jsonl",
                "required_terminal_event": "task_completed",
                "observed_terminal_event": None,
                "stabilized": True,
            },
            {
                "event_journal_path": "workspace-r03/.openclaw/events/c.jsonl",
                "required_terminal_event": None,
                "observed_terminal_event": None,
                "stabilized": True,
            },
        ],
    )

    assert aggregate["repeat_count"] == 3
    assert aggregate["git_sha"] == "abc123"
    assert aggregate["model"] == "qwen-local"
    assert aggregate["backend"] == "local_openclaw"
    assert aggregate["runtime_profile"] == "standard"
    assert aggregate["repeat_seed"] == "seed-1"
    assert aggregate["clean_success_count"] == 1
    assert aggregate["clean_success_rate"] == 1 / 3
    assert aggregate["primary_failure_phase_distribution"] == {
        "clean_success": 1,
        "debug_repair": 2,
    }
    assert aggregate["stable_primary_failure_phase"] is False
    assert aggregate["path_observed_count"] == 3
    assert aggregate["intended_path_observed_count"] == 3
    assert aggregate["execution_reached_count"] == 3
    assert aggregate["debug_repair_reached_count"] == 2
    assert aggregate["phase7f_used_count"] == 2
    assert aggregate["bounded_execution_debug_repair_used_count"] == 2
    assert aggregate["phase7g_used_count"] == 1
    assert aggregate["diff_scoped_debug_repair_used_count"] == 1
    assert aggregate["phase7f_exercised_rate"] == 2 / 3
    assert aggregate["bounded_execution_debug_repair_exercised_rate"] == 2 / 3
    assert aggregate["phase7g_exercised_rate"] == 1 / 3
    assert aggregate["diff_scoped_debug_repair_exercised_rate"] == 1 / 3
    assert aggregate["most_common_blocker"] == "verifier_failed"
    assert aggregate["score_readiness_summary"] == {
        "all_runs_scoreable": False,
        "readiness_recorded_count": 3,
        "stabilized_count": 3,
        "stabilization_missing_count": 0,
        "required_terminal_event_count": 2,
        "required_terminal_event_observed_count": 1,
        "terminal_event_observed_count": 1,
        "terminal_event_missing_count": 1,
        "observed_terminal_event_distribution": {"task_completed": 1},
        "journal_paths": [
            "workspace-r01/.openclaw/events/a.jsonl",
            "workspace-r02/.openclaw/events/b.jsonl",
            "workspace-r03/.openclaw/events/c.jsonl",
        ],
        "journal_path_count": 3,
    }
    assert aggregate["run_report_paths"] == ["run1.json", "run2.json", "run3.json"]


def test_aggregate_case_reports_marks_stable_phase_at_eighty_percent_threshold():
    reports = [
        _report(
            clean_success=False,
            primary_failure_phase="planning_validation",
            path_observed=False,
            intended_path_observed=False,
            execution_reached=False,
            debug_repair_reached=False,
            phase7f_used=False,
            phase7g_used=False,
            blockers=["task_completed_event_missing"],
        )
        for _ in range(4)
    ]
    reports.append(
        _report(
            clean_success=False,
            primary_failure_phase="execution",
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=False,
            phase7f_used=False,
            phase7g_used=False,
            blockers=["verifier_failed"],
        )
    )

    aggregate = runner._aggregate_case_reports(
        case_id="python_cli_small_feature",
        reports=reports,
        report_paths=[Path(f"run{index}.json") for index in range(5)],
        run_context={
            "git_sha": None,
            "model": None,
            "backend": None,
            "runtime_profile": None,
            "repeat_seed": None,
        },
    )

    assert aggregate["primary_failure_phase_distribution"] == {
        "execution": 1,
        "planning_validation": 4,
    }
    assert aggregate["stable_primary_failure_phase"] is True


def test_aggregate_case_reports_reads_architecture_named_debug_repair_aliases():
    reports = [
        _report(
            clean_success=False,
            primary_failure_phase="debug_repair",
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=True,
            phase7f_used=False,
            phase7g_used=False,
            blockers=["verifier_failed"],
        ),
        _report(
            clean_success=False,
            primary_failure_phase="debug_repair",
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=True,
            phase7f_used=False,
            phase7g_used=False,
            blockers=["verifier_failed"],
        ),
    ]
    reports[0]["path_observability"]["bounded_execution_debug_repair_used"] = True
    reports[1]["path_observability"]["diff_scoped_debug_repair_used"] = True

    aggregate = runner._aggregate_case_reports(
        case_id="python_cli_small_feature",
        reports=reports,
        report_paths=[Path("run1.json"), Path("run2.json")],
        run_context={
            "git_sha": None,
            "model": None,
            "backend": None,
            "runtime_profile": None,
            "repeat_seed": None,
        },
    )

    assert aggregate["phase7f_used_count"] == 1
    assert aggregate["bounded_execution_debug_repair_used_count"] == 1
    assert aggregate["phase7g_used_count"] == 1
    assert aggregate["diff_scoped_debug_repair_used_count"] == 1
    assert aggregate["phase7f_exercised_rate"] == 1 / 2
    assert aggregate["bounded_execution_debug_repair_exercised_rate"] == 1 / 2
    assert aggregate["phase7g_exercised_rate"] == 1 / 2
    assert aggregate["diff_scoped_debug_repair_exercised_rate"] == 1 / 2


def test_aggregate_case_reports_marks_all_runs_scoreable_when_ready():
    reports = [
        _report(
            clean_success=True,
            primary_failure_phase=None,
            path_observed=True,
            intended_path_observed=True,
            execution_reached=True,
            debug_repair_reached=False,
            phase7f_used=False,
            phase7g_used=False,
            blockers=[],
        )
        for _ in range(3)
    ]

    aggregate = runner._aggregate_case_reports(
        case_id="python_cli_small_feature",
        reports=reports,
        report_paths=[Path(f"run{index}.json") for index in range(3)],
        run_context={
            "git_sha": "abc123",
            "model": "qwen-local",
            "backend": "local_openclaw",
            "runtime_profile": "standard",
            "repeat_seed": "seed-1",
        },
        score_readiness=[
            {
                "event_journal_path": f"workspace-r0{index}/.openclaw/events/log.jsonl",
                "required_terminal_event": "task_completed",
                "observed_terminal_event": "task_completed",
                "stabilized": True,
            }
            for index in range(1, 4)
        ],
    )

    assert aggregate["score_readiness_summary"] == {
        "all_runs_scoreable": True,
        "readiness_recorded_count": 3,
        "stabilized_count": 3,
        "stabilization_missing_count": 0,
        "required_terminal_event_count": 3,
        "required_terminal_event_observed_count": 3,
        "terminal_event_observed_count": 3,
        "terminal_event_missing_count": 0,
        "observed_terminal_event_distribution": {"task_completed": 3},
        "journal_paths": [
            "workspace-r01/.openclaw/events/log.jsonl",
            "workspace-r02/.openclaw/events/log.jsonl",
            "workspace-r03/.openclaw/events/log.jsonl",
        ],
        "journal_path_count": 3,
    }


def test_request_json_raises_auth_expired_on_401(monkeypatch):
    def raise_unauthorized(_request, timeout):
        raise HTTPError(
            url="http://example.test/api/v1/sessions/1",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=BytesIO(b'{"detail":"Could not validate credentials"}'),
        )

    monkeypatch.setattr(runner.request, "urlopen", raise_unauthorized)

    with pytest.raises(runner.AuthExpiredError, match="auth_expired"):
        runner._request_json(
            "GET",
            "http://example.test/api/v1",
            "sessions/1",
            "expired-token",
        )


def test_wait_for_scoreable_event_journal_accepts_existing_task_completed(tmp_path):
    event_dir = tmp_path / ".openclaw/events"
    event_dir.mkdir(parents=True)
    event_path = event_dir / "session_10_task_20.jsonl"
    event_path.write_text(
        '{"event_type":"task_completed","details":{"steps_completed":1}}\n',
        encoding="utf-8",
    )

    result = runner._wait_for_scoreable_event_journal(
        workspace=tmp_path,
        session_id=10,
        task_id=20,
        session_status="completed",
        timeout_seconds=0,
        stable_seconds=999,
        poll_seconds=0,
    )

    assert result["observed_terminal_event"] == "task_completed"


def test_run_case_refuses_to_score_completed_session_without_terminal_event(
    monkeypatch, tmp_path
):
    scored = False

    def fake_request_json(method, _base_url, path, _token, payload=None):
        if method == "POST" and path == "projects":
            return {"id": 1}
        if method == "POST" and path == "tasks":
            return {"id": 2}
        if method == "POST" and path == "sessions":
            return {"id": 3}
        if method == "POST" and path == "sessions/3/tasks/2/run":
            return {}
        raise AssertionError(f"unexpected request: {method} {path} {payload}")

    def fake_run_scorer(**_kwargs):
        nonlocal scored
        scored = True
        raise AssertionError("scorer should not run before terminal event")

    monkeypatch.setattr(runner, "_select_case", lambda _manifest, _case_id: {})
    monkeypatch.setattr(
        runner, "_task_prompt_for_case", lambda _case, _fixture_dir: ("prompt", "test")
    )
    monkeypatch.setattr(
        runner,
        "_fresh_workspace",
        lambda _root, _case_id, _fixture_dir, _timestamp: tmp_path,
    )
    monkeypatch.setattr(runner, "_request_json", fake_request_json)
    monkeypatch.setattr(
        runner,
        "_wait_for_terminal_session",
        lambda **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(
        runner,
        "_wait_for_scoreable_event_journal",
        lambda **_kwargs: (_ for _ in ()).throw(SystemExit("terminal_event_missing")),
    )
    monkeypatch.setattr(runner, "_run_scorer", fake_run_scorer)

    args = SimpleNamespace(
        api_base_url="http://example.test/api/v1",
        fixtures_dir=tmp_path,
        workspace_root=tmp_path,
        reports_dir=tmp_path,
        manifest=tmp_path / "manifest.json",
        python="python",
        timeout_seconds=60,
        poll_seconds=0,
        event_stabilization_timeout_seconds=0,
        event_stable_seconds=0,
    )

    with pytest.raises(SystemExit, match="terminal_event_missing"):
        runner._run_case(
            args=args,
            repo_root=tmp_path,
            manifest={},
            case_id="python_cli_small_feature",
            token="token",
            run_index=1,
            repeat_count=1,
        )

    assert scored is False


def test_run_case_auth_expiry_during_polling_does_not_score(monkeypatch, tmp_path):
    scored = False

    def fake_request_json(method, _base_url, path, _token, payload=None):
        if method == "POST" and path == "projects":
            return {"id": 1}
        if method == "POST" and path == "tasks":
            return {"id": 2}
        if method == "POST" and path == "sessions":
            return {"id": 3}
        if method == "POST" and path == "sessions/3/tasks/2/run":
            return {}
        raise AssertionError(f"unexpected request: {method} {path} {payload}")

    def fake_run_scorer(**_kwargs):
        nonlocal scored
        scored = True
        raise AssertionError("scorer should not run after auth expiry")

    monkeypatch.setattr(runner, "_select_case", lambda _manifest, _case_id: {})
    monkeypatch.setattr(
        runner, "_task_prompt_for_case", lambda _case, _fixture_dir: ("prompt", "test")
    )
    monkeypatch.setattr(
        runner,
        "_fresh_workspace",
        lambda _root, _case_id, _fixture_dir, _timestamp: tmp_path,
    )
    monkeypatch.setattr(runner, "_request_json", fake_request_json)
    monkeypatch.setattr(
        runner,
        "_wait_for_terminal_session",
        lambda **_kwargs: (_ for _ in ()).throw(
            runner.AuthExpiredError("auth_expired: polling token expired")
        ),
    )
    monkeypatch.setattr(runner, "_run_scorer", fake_run_scorer)

    args = SimpleNamespace(
        api_base_url="http://example.test/api/v1",
        fixtures_dir=tmp_path,
        workspace_root=tmp_path,
        reports_dir=tmp_path,
        manifest=tmp_path / "manifest.json",
        python="python",
        timeout_seconds=60,
        poll_seconds=0,
        event_stabilization_timeout_seconds=0,
        event_stable_seconds=0,
    )

    with pytest.raises(runner.AuthExpiredError, match="auth_expired"):
        runner._run_case(
            args=args,
            repo_root=tmp_path,
            manifest={},
            case_id="python_cli_small_feature",
            token="token",
            run_index=1,
            repeat_count=1,
        )

    assert scored is False
