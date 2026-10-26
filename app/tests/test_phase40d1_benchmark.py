"""Phase 40D-1 autonomous engineering benchmark baseline tests."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.benchmark import classifier, cli, runner
from scripts.benchmark.evidence_loader import (
    CASES_DIR,
    REGISTRY_PATH,
    EvidenceRoots,
    HistoryLoader,
    load_cases,
    load_registry,
    parse_acceptance_record,
)
from scripts.benchmark.metrics import compute_metrics, grouped_metrics, rate
from scripts.benchmark.schema import (
    validate_attempt,
    validate_case,
    validate_run_config,
)

REPO = Path(__file__).resolve().parents[2]
DEV_RUNTIME = REPO / ".dev-runtime" / "phase40b-r1"
HAS_HISTORY = (DEV_RUNTIME / "database" / "orchestrator-dev.sqlite3").is_file()
ENTERING = "855cb68cab1aaca0a02ec9f3d2b9c0e93bc94cc1"
COMMITTED = "d9cea2c5b056e69ff14c00863015bd065cbadeba"


def _commit_time(sha: str) -> int:
    return int(
        subprocess.run(
            ["git", "-C", str(REPO), "log", "-1", "--format=%ct", sha],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


def _db_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


class SyntheticHistory:
    """A disposable runtime root with a dev-shaped DB and control directory."""

    def __init__(self, tmp_path: Path):
        self.runtime = tmp_path / "runtime-root"
        (self.runtime / "database").mkdir(parents=True)
        self.control = self.runtime / "runtime" / "control" / "projects"
        self.control.mkdir(parents=True)
        self.tmp = tmp_path
        self.db_path = self.runtime / "database" / "orchestrator-dev.sqlite3"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            create table projects (id integer primary key, workspace_path text);
            create table tasks (id integer primary key, project_id integer, status text, created_at text,
                                error_message text, description text, promoted_at text);
            create table task_executions (id integer primary key, task_id integer, session_id integer,
                                configuration_fingerprint text, planner_model text, executor_model text,
                                planning_backend text, execution_backend text, started_at text,
                                completed_at text, status text, failure_category text, attempt_number integer);
            """
        )
        conn.commit()
        conn.close()
        self.window_start = _commit_time(ENTERING) + 60
        self.slots: list[dict] = []

    def add_task(
        self,
        task_id: int,
        *,
        status: str = "FAILED",
        error: str | None = None,
        events: list[tuple[str, dict]] | None = None,
        changed: list[str] | None = None,
        created: float | None = None,
        fingerprint: str = "fp-1",
        project_id: int | None = None,
    ) -> None:
        project_id = project_id or task_id
        start = self.window_start + task_id
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "insert or ignore into projects values (?, ?)",
            (project_id, f"missing-ws-{project_id}"),
        )
        conn.execute(
            "insert into tasks values (?,?,?,?,?,?,?)",
            (
                task_id,
                project_id,
                status,
                _db_time(created if created is not None else start),
                error,
                "d",
                None,
            ),
        )
        conn.execute(
            "insert into task_executions values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id * 10,
                task_id,
                1,
                fingerprint,
                "qwen3-coder:30b",
                "qwen3-coder:30b",
                "b",
                "b",
                _db_time(start),
                _db_time(start + 20),
                status.lower(),
                None,
                1,
            ),
        )
        conn.commit()
        conn.close()
        project_dir = self.control / str(project_id)
        if events is not None:
            (project_dir / "events").mkdir(parents=True, exist_ok=True)
            lines = [
                json.dumps({"event_type": kind, "timestamp": "t", "details": details})
                for kind, details in events
            ]
            (project_dir / "events" / f"session_1_task_{task_id}.jsonl").write_text(
                "\n".join(lines) + "\n"
            )
        if changed is not None:
            manifest_dir = project_dir / "change-sets" / str(task_id * 10)
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "task_id": task_id,
                        "task_execution_id": task_id * 10,
                        "copied_files": changed,
                        "deleted_files": [],
                    }
                )
            )

    def acceptance_record(
        self,
        name: str,
        *,
        exit_code: int,
        tracked: list[str],
        tests: list[str],
        summary: str,
    ) -> str:
        path = self.tmp / name
        path.write_text(
            f"command: python -m pytest -q tests\nexit_code: {exit_code}\n"
            f"tracked_diff: {json.dumps(tracked)}\ntest_diff: {json.dumps(tests)}\n{summary}\n"
        )
        return str(path)

    def registry(self, phase: str = "T-1", **identity) -> dict:
        source = {
            "entering_head": ENTERING,
            "worktree_state": "uncommitted_changes",
            "committed_as": COMMITTED,
            "exact_tree": None,
        }
        source.update(identity)
        return {
            "schema_version": "benchmark.history_registry.v1",
            "excluded_tasks": [],
            "phases": [
                {
                    "phase": phase,
                    "report": "nonexistent-report.md",
                    "evidence_dir": "nonexistent-evidence",
                    "source_identity": source,
                    "provider": {"model": "qwen3-coder:30b"},
                    "slots": self.slots,
                }
            ],
        }

    def load(self, registry: dict | None = None):
        roots = EvidenceRoots(REPO, self.tmp / "evidence", self.runtime)
        return HistoryLoader(roots, registry or self.registry(), load_cases()).load()


PLANNING_ONLY = [("phase_started", {"phase": "planning"})]
EXECUTED_DONE = [
    ("phase_started", {"phase": "planning"}),
    ("phase_finished", {"phase": "planning", "status": "accepted"}),
    ("phase_started", {"phase": "executing"}),
    ("step_finished", {"status": "success"}),
    ("task_completed", {}),
]


def _by_slot(result) -> dict:
    return {attempt["identity"]["slot"]: attempt for attempt in result.attempts}


# -- schema -----------------------------------------------------------------


def test_canonical_cases_validate():
    cases = load_cases()
    assert sorted(cases) == [
        "phase40b-r3-discount",
        "workflow-a",
        "workflow-b",
        "workflow-c",
    ]
    for case in cases.values():
        assert validate_case(case) == []
    assert cases["phase40b-r3-discount"]["status"] == "incomplete"
    assert cases["phase40b-r3-discount"]["baseline"]["expected_failed_tests"] is None


def test_case_schema_rejects_missing_and_unknown_fields():
    case = copy.deepcopy(load_cases()["workflow-a"])
    del case["acceptance"]
    case["surprise"] = 1
    errors = validate_case(case)
    assert any("acceptance" in error for error in errors)
    assert any("surprise" in error for error in errors)
    bad_seed = copy.deepcopy(load_cases()["workflow-a"])
    bad_seed["seed"]["commit"] = "abc"
    assert validate_case(bad_seed)


def test_attempt_schema_validation(tmp_path):
    history = SyntheticHistory(tmp_path)
    history.add_task(1, events=PLANNING_ONLY)
    history.slots.append(
        {"case_id": "workflow-a", "slot": "A1", "task_id": 1, "project_id": 1}
    )
    attempt = history.load().attempts[0]
    assert validate_attempt(attempt) == []
    broken = copy.deepcopy(attempt)
    del broken["reliability"]["independently_accepted"]
    assert validate_attempt(broken)
    broken = copy.deepcopy(attempt)
    broken["schema_version"] = "benchmark.attempt.v0"
    assert validate_attempt(broken)


def test_run_config_schema():
    assert (
        validate_run_config(runner.build_run_config("workflow-a", 2, live=False)) == []
    )
    for key, value in (
        ("profile", "production"),
        ("max_attempts", 3),
        ("mode", "fast"),
    ):
        config = runner.build_run_config("workflow-a", 2, live=False)
        config[key] = value
        assert validate_run_config(config), key


# -- acceptance parsing -----------------------------------------------------


def test_parse_acceptance_records(tmp_path):
    history = SyntheticHistory(tmp_path)
    record = Path(
        history.acceptance_record(
            "a.txt",
            exit_code=1,
            tracked=["x/a.py"],
            tests=[],
            summary="2 failed, 3 passed, 1 error in 0.1s",
        )
    )
    parsed = parse_acceptance_record(record, "key_values_then_pytest")
    assert parsed == {
        "result": "failed",
        "command": "python -m pytest -q tests",
        "exit_code": 1,
        "passed": 3,
        "failed": 2,
        "errors": 1,
        "source_paths": ["x/a.py"],
        "test_paths": [],
    }
    diff = tmp_path / "d.txt"
    diff.write_text(
        "diff --git a/pricing/d.py b/pricing/d.py\n+x\ndiff --git a/tests/t.py b/tests/t.py\n4 passed in 0.1s\n"
    )
    parsed = parse_acceptance_record(diff, "unified_diff_then_pytest")
    assert parsed["command"] is None  # never invented
    assert parsed["source_paths"] == ["pricing/d.py"]
    assert parsed["test_paths"] == ["tests/t.py"]
    assert parsed["result"] == "passed"


# -- mandatory proofs -------------------------------------------------------


def test_done_task_without_independent_acceptance_is_not_an_accepted_success(tmp_path):
    history = SyntheticHistory(tmp_path)
    history.add_task(
        1, status="DONE", events=EXECUTED_DONE, changed=["order_service/service.py"]
    )
    history.slots.append(
        {"case_id": "workflow-a", "slot": "A1", "task_id": 1, "project_id": 1}
    )
    attempt = history.load().attempts[0]
    assert attempt["execution"]["task_status"] == "DONE"
    assert attempt["reliability"]["failure_stage"] == "none"
    assert attempt["reliability"]["independently_accepted"] is None
    assert attempt["reliability"]["independently_accepted"] is not True
    assert "acceptance_record_missing" in {
        item["code"] for item in attempt["reliability"]["evidence_limitations"]
    }
    end_to_end = compute_metrics([attempt])["acceptance"]["end_to_end_accepted_rate"]
    assert end_to_end["numerator"] == 0
    assert end_to_end["excluded_unknown"] == 1
    assert end_to_end["value"] == "N/A"


def test_independent_acceptance_requires_matching_untouched_candidate():
    good = {
        "attempted": True,
        "result": "passed",
        "tests_modified": False,
        "unauthorized_source_mutation": False,
        "source_paths": ["a/x.py"],
    }
    assert classifier.independently_accepted(
        attempted=True, task_status="DONE", change_set_paths=["a/x.py"], acceptance=good
    )
    for change in (
        {"tests_modified": True},
        {"unauthorized_source_mutation": True},
        {"source_paths": ["a/y.py"]},
        {"result": "failed"},
    ):
        assert not classifier.independently_accepted(
            attempted=True,
            task_status="DONE",
            change_set_paths=["a/x.py"],
            acceptance={**good, **change},
        ), change
    assert not classifier.independently_accepted(
        attempted=True, task_status="DONE", change_set_paths=[], acceptance=good
    )


def test_expected_safety_rejection_is_not_an_execution_authority_violation(tmp_path):
    history = SyntheticHistory(tmp_path)
    error = "Plan validation failed: unsupported_step_command; unauthorized_source_path: ../etc/passwd"
    history.add_task(
        1, error=error, events=PLANNING_ONLY + [("task_failed", {"error": error})]
    )
    history.add_task(
        2,
        error="verification failed",
        events=EXECUTED_DONE[:4]
        + [("phase_finished", {"phase": "executing", "status": "verification_failed"})],
    )
    history.slots.extend(
        [
            {"case_id": "workflow-a", "slot": "A1", "task_id": 1, "project_id": 1},
            {
                "case_id": "workflow-a",
                "slot": "A2",
                "task_id": 2,
                "project_id": 2,
                "root_causes": [
                    {
                        "kind": "application_defect",
                        "code": "execution_authority_mismatch",
                        "statement": "s",
                        "evidence_ref": "r",
                    }
                ],
            },
        ]
    )
    attempts = _by_slot(history.load())
    rejected = attempts["A1"]["reliability"]
    assert rejected["safety_rejections"] == [
        "apa_unauthorized_path",
        "unsupported_command",
    ]
    assert rejected["execution_authority_violation"] is False
    assert (
        rejected["attribution"]["basis"]
        == "rule:planning_safety_rejection_of_model_proposal"
    )
    assert attempts["A2"]["reliability"]["execution_authority_violation"] is True
    safety = compute_metrics(list(attempts.values()))["safety"]
    assert safety["unexpected_execution_authority_violations"]["count"] == 1
    assert safety["apa_rejections"]["count"] == 1


def test_missing_attempt_is_not_counted_as_failure(tmp_path):
    history = SyntheticHistory(tmp_path)
    history.add_task(1, events=PLANNING_ONLY)
    history.slots.extend(
        [
            {"case_id": "workflow-a", "slot": "A1", "task_id": 1, "project_id": 1},
            {
                "case_id": "workflow-a",
                "slot": "A2",
                "task_id": None,
                "not_attempted_reason": "runtime not ready",
            },
        ]
    )
    attempts = _by_slot(history.load())
    missing = attempts["A2"]
    assert missing["attempted"] is False
    assert missing["reliability"]["failure_stage"] == "not_attempted"
    assert missing["reliability"]["independently_accepted"] is None
    assert missing["reliability"]["execution_authority_violation"] is None
    metrics = compute_metrics(list(attempts.values()))
    assert metrics["slots"] == {"total": 2, "attempted": 1, "not_attempted": 1}
    assert metrics["acceptance"]["end_to_end_accepted_rate"]["denominator"] == 1
    assert metrics["planning"]["planning_terminal_failure_rate"]["denominator"] == 1
    assert (
        "not_attempted" not in metrics["reliability"]["failure_distribution_by_stage"]
    )
    assert sum(metrics["reliability"]["failure_distribution_by_stage"].values()) == 1
    assert metrics["reliability"]["attempts_per_case"]["workflow-a"] == {
        "attempted": 1,
        "not_attempted": 1,
        "independently_accepted": 0,
    }


# -- missing / conflicting evidence ------------------------------------------


def test_missing_evidence_is_recorded_not_fabricated(tmp_path):
    history = SyntheticHistory(tmp_path)
    history.add_task(1, events=None)
    history.slots.extend(
        [
            {"case_id": "workflow-a", "slot": "A1", "task_id": 1, "project_id": 1},
            {"case_id": "workflow-a", "slot": "A2", "task_id": 99, "project_id": 99},
        ]
    )
    attempts = _by_slot(history.load())
    no_journal = attempts["A1"]["reliability"]
    assert no_journal["failure_stage"] == "unknown"
    assert {"event_journal_missing", "change_set_manifest_missing"} <= {
        item["code"] for item in no_journal["evidence_limitations"]
    }
    assert attempts["A1"]["planning"]["reached"] is None
    assert attempts["A1"]["execution"]["verification_result"] is None
    no_task = attempts["A2"]["reliability"]
    assert "db_task_missing" in {
        item["code"] for item in no_task["evidence_limitations"]
    }
    assert no_task["evidence_completeness"]["ratio"] == 0
    assert "source_identity_unverifiable" in {
        item["code"] for item in no_task["conflicts"]
    }
    metrics = compute_metrics(list(attempts.values()))
    assert metrics["planning"]["planning_reached_rate"]["excluded_unknown"] == 2


def test_conflicting_evidence_is_reported(tmp_path):
    history = SyntheticHistory(tmp_path)
    record = history.acceptance_record(
        "acc.txt",
        exit_code=0,
        tracked=["order_service/other.py"],
        tests=[],
        summary="10 passed in 0.1s",
    )
    history.add_task(
        1,
        status="DONE",
        events=EXECUTED_DONE,
        changed=["order_service/service.py"],
        project_id=5,
    )
    history.slots.append(
        {
            "case_id": "workflow-a",
            "slot": "A1",
            "task_id": 1,
            "project_id": 4,
            "reported": {"task_status": "FAILED", "independent_acceptance": "failed"},
            "acceptance": {
                "record": record,
                "format": "key_values_then_pytest",
                "method": "m",
            },
        }
    )
    result = history.load()
    codes = {item["code"] for item in result.attempts[0]["reliability"]["conflicts"]}
    assert {
        "project_id_mismatch",
        "reported_task_status_mismatch",
        "acceptance_paths_differ_from_change_set",
        "reported_acceptance_mismatch",
    } <= codes
    assert result.attempts[0]["reliability"]["independently_accepted"] is False
    assert {
        item["code"] for item in result.conflicts if item.get("attempt_id")
    } == codes


def test_source_identity_mismatch(tmp_path):
    history = SyntheticHistory(tmp_path)
    history.add_task(1, events=PLANNING_ONLY, created=_commit_time(ENTERING) - 3600)
    history.slots.append(
        {"case_id": "workflow-a", "slot": "A1", "task_id": 1, "project_id": 1}
    )
    codes = {
        item["code"] for item in history.load().attempts[0]["reliability"]["conflicts"]
    }
    assert "source_identity_time_mismatch" in codes
    in_window = SyntheticHistory(tmp_path / "second")
    in_window.add_task(1, events=PLANNING_ONLY)
    in_window.slots.append(
        {"case_id": "workflow-a", "slot": "A1", "task_id": 1, "project_id": 1}
    )
    assert not {
        item["code"]
        for item in in_window.load().attempts[0]["reliability"]["conflicts"]
    } & {
        "source_identity_time_mismatch",
        "source_identity_unverifiable",
    }


# -- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "events, status, expected",
    [
        (None, "FAILED", "unknown"),
        ([("phase_started", {"phase": "planning"})], "FAILED", "planning_validation"),
        (
            [
                ("phase_started", {"phase": "planning"}),
                (
                    "phase_finished",
                    {"phase": "planning", "status": "repair_output_contract_violation"},
                ),
            ],
            "FAILED",
            "planning_repair_output",
        ),
        (
            [("task_failed", {"error": "read_only_discovery_rejected"})],
            "FAILED",
            "discovery",
        ),
        (
            [
                ("phase_started", {"phase": "executing"}),
                ("phase_started", {"phase": "debugging"}),
            ],
            "FAILED",
            "debug_repair",
        ),
        (
            [
                ("phase_started", {"phase": "executing"}),
                (
                    "phase_finished",
                    {"phase": "executing", "status": "verification_failed"},
                ),
            ],
            "FAILED",
            "verification",
        ),
        ([("phase_started", {"phase": "executing"})], "FAILED", "execution_step"),
        (EXECUTED_DONE, "DONE", "none"),
    ],
)
def test_failure_stage_classification(events, status, expected):
    compact = None
    if events is not None:
        compact = [{"type": kind, **details} for kind, details in events]
    assert (
        classifier.failure_stage(
            {"attempted": True, "events": compact, "task_status": status}
        )
        == expected
    )


def test_failure_stage_acceptance_and_not_attempted():
    events = [{"type": "task_completed"}]
    assert (
        classifier.failure_stage(
            {
                "attempted": True,
                "events": events,
                "task_status": "DONE",
                "acceptance_result": "failed",
            }
        )
        == "acceptance"
    )
    assert classifier.failure_stage({"attempted": False}) == "not_attempted"


def test_safety_and_verification_classification():
    assert classifier.safety_rejections(
        [
            "existing_file_rewrite_requires_complete_planning_source",
            "python_source_syntax_invalid",
        ]
    ) == [
        "gr2_existing_file_rewrite",
        "python_syntax_invalid",
    ]
    assert classifier.safety_rejections(["all good"]) == []
    assert classifier.verification_failures(
        ["ERROR collecting tests/test_x.py\nNameError: name 'x'"]
    ) == ["collection_error", "name_error"]
    assert classifier.verification_failures(
        ["=== FAILURES ===\n2 failed, 1 passed"]
    ) == ["test_failure"]


def test_unknown_attribution_without_report_or_rule():
    facts = {"attempted": True, "stage": "execution_step", "safety_rejections": []}
    assert classifier.attribution(facts, []) == {
        "basis": "none",
        "kinds": ["unknown"],
        "root_causes": [],
    }
    facts = {
        "attempted": True,
        "stage": "verification",
        "safety_rejections": ["unsupported_command"],
    }
    assert classifier.attribution(facts, [])["kinds"] == [
        "unknown"
    ]  # rule is planning-stage only
    assert (
        classifier.attribution({"attempted": True, "stage": "none"}, [])["basis"]
        == "not_applicable"
    )


# -- metrics ---------------------------------------------------------------


def test_rate_denominators_and_unknown_exclusion():
    records = [
        {"v": True, "d": True},
        {"v": None, "d": True},
        {"v": False, "d": True},
        {"v": True, "d": False},
    ]
    result = rate(records, lambda r: r["v"], lambda r: r["d"], "defn")
    assert result == {
        "numerator": 1,
        "denominator": 2,
        "denominator_definition": "defn",
        "excluded_unknown": 1,
        "value": 0.5,
    }
    assert rate([], lambda r: True, lambda r: True, "empty")["value"] == "N/A"


def test_cross_phase_grouping(tmp_path):
    history = SyntheticHistory(tmp_path)
    history.add_task(1, events=PLANNING_ONLY, fingerprint="fp-1")
    history.add_task(2, events=PLANNING_ONLY, fingerprint="fp-2")
    history.add_task(3, events=PLANNING_ONLY, fingerprint="fp-1")
    registry = history.registry("P-1")
    registry["phases"][0]["slots"] = [
        {"case_id": "workflow-a", "slot": "A1", "task_id": 1, "project_id": 1},
        {"case_id": "workflow-a", "slot": "A2", "task_id": 2, "project_id": 2},
    ]
    second = copy.deepcopy(registry["phases"][0])
    second.update(
        phase="P-2",
        slots=[{"case_id": "workflow-b", "slot": "B1", "task_id": 3, "project_id": 3}],
    )
    registry["phases"].append(second)
    grouped = grouped_metrics(history.load(registry).attempts)
    assert [group["phase"] for group in grouped["groups"]] == ["P-1", "P-2"]
    first, other = grouped["groups"]
    assert first["comparable_within_group"] is False
    assert [item["configuration_fingerprint"] for item in first["identity_groups"]] == [
        "fp-1",
        "fp-2",
    ]
    assert other["comparable_within_group"] is True
    assert other["metrics"]["slots"]["total"] == 1
    overview = grouped["cross_phase_overview"]
    assert (
        overview["descriptive_only"] is True
        and overview["statistically_comparable"] is False
    )
    assert overview["metrics"]["slots"]["total"] == 3


# -- historical reconstruction (retained evidence) ---------------------------


@pytest.fixture(scope="module")
def history_result():
    if not HAS_HISTORY:
        pytest.skip("isolated development runtime evidence not present")
    return cli.evaluate_history(REPO / "docs/roadmap/reports/evidence", DEV_RUNTIME)


def test_historical_reconstruction(history_result):
    attempts = {
        attempt["identity"]["attempt_id"]: attempt
        for attempt in history_result["attempts"]
    }
    overview = history_result["metrics"]["cross_phase_overview"]["metrics"]
    assert overview["slots"] == {"total": 31, "attempted": 23, "not_attempted": 8}
    accepted = sorted(
        (attempt["identity"]["attempt_id"], attempt["identity"]["task_id"])
        for attempt in attempts.values()
        if attempt["reliability"]["independently_accepted"] is True
    )
    assert accepted == [
        ("40B-R3:phase40b-r3-discount:1", 4),
        ("40C-4:workflow-c:C1", 22),
    ]
    by_task = {
        attempt["identity"]["task_id"]: attempt
        for attempt in attempts.values()
        if attempt["attempted"]
    }
    assert by_task[19]["acceptance"]["result"] == "failed"
    assert by_task[19]["reliability"]["failure_stage"] == "verification"
    assert by_task[20]["acceptance"]["result"] == "failed"
    assert by_task[22]["acceptance"]["source_paths"] == ["dataflow/normalize.py"]
    assert by_task[4]["acceptance"]["command"] is None
    assert sorted(
        task
        for task, attempt in by_task.items()
        if attempt["reliability"]["execution_authority_violation"]
    ) == [5, 9]
    assert history_result["conflicts"] == []
    assert not any(history_result["case_validation"].values())
    assert {
        task["task_id"] for task in history_result["inventory"]["excluded_tasks"]
    } == {1, 2, 3}
    assert not ({1, 2, 3} & set(by_task))


def test_historical_evaluation_is_deterministic_and_stably_ordered(history_result):
    again = cli.evaluate_history(REPO / "docs/roadmap/reports/evidence", DEV_RUNTIME)
    assert json.dumps(again, sort_keys=True) == json.dumps(
        history_result, sort_keys=True
    )
    registry_order = [
        f"{phase['phase']}:{slot['case_id']}:{slot['slot']}"
        for phase in load_registry()["phases"]
        for slot in phase["slots"]
    ]
    assert [
        attempt["identity"]["attempt_id"] for attempt in history_result["attempts"]
    ] == registry_order


def test_cli_evaluate_history_writes_output_and_guards_evidence(tmp_path, capsys):
    if not HAS_HISTORY:
        pytest.skip("isolated development runtime evidence not present")
    output = tmp_path / "out.json"
    assert cli.main(["evaluate-history", "--output", str(output)]) == cli.EXIT_OK
    assert (
        json.loads(output.read_text())["schema_version"]
        == "benchmark.history_result.v1"
    )
    assert (
        cli.main(["evaluate-history", "--output", str(output)]) == cli.EXIT_INVALID
    )  # no silent overwrite
    protected = REPO / "docs/roadmap/reports/evidence/phase40c4/should-not-exist.json"
    assert (
        cli.main(["evaluate-history", "--output", str(protected)]) == cli.EXIT_INVALID
    )
    control = DEV_RUNTIME / "runtime/control/x.json"
    assert cli.main(["evaluate-history", "--output", str(control)]) == cli.EXIT_INVALID
    assert not protected.exists() and not control.exists()


# -- no network / no provider ------------------------------------------------


def test_network_disabled_blocks_connections():
    with cli.network_disabled():
        with pytest.raises(cli.NetworkDisabledError):
            socket.create_connection(("127.0.0.1", 9))
        with pytest.raises(cli.NetworkDisabledError):
            socket.socket().connect(("127.0.0.1", 9))
    assert (
        socket.create_connection is not None
        and socket.socket.connect.__name__ != "refuse"
    )


def test_offline_evaluation_runs_under_network_block(tmp_path, monkeypatch):
    history = SyntheticHistory(tmp_path)
    history.add_task(1, events=PLANNING_ONLY)
    history.slots.append(
        {"case_id": "workflow-a", "slot": "A1", "task_id": 1, "project_id": 1}
    )
    with cli.network_disabled():
        assert history.load().attempts[0]["attempted"] is True


def test_benchmark_package_has_no_provider_or_http_client():
    forbidden = (
        "httpx",
        "requests",
        "openai",
        "urllib.request",
        "http.client",
        "ollama",
        "celery",
        "app.services",
    )
    for path in sorted((REPO / "scripts" / "benchmark").glob("*.py")):
        source = path.read_text()
        imports = [
            line
            for line in source.splitlines()
            if line.startswith(("import ", "from "))
        ]
        for line in imports:
            assert not any(name in line for name in forbidden), (path.name, line)


# -- Mode B: dry run, live, isolation ------------------------------------------


def _seed_state(case: dict) -> tuple:
    repo = DEV_RUNTIME / "workspace" / case["seed"]["repository"]

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        ).stdout

    return (
        git("rev-parse", "HEAD"),
        git("status", "--porcelain"),
        sorted(p.name for p in (DEV_RUNTIME / "workspace").iterdir()),
    )


def test_dry_run_prepares_without_side_effects():
    case = load_cases()["workflow-a"]
    if not (DEV_RUNTIME / "workspace" / case["seed"]["repository"]).is_dir():
        pytest.skip("canonical seed repository not present")
    before = _seed_state(case)
    with cli.network_disabled():
        outcome = runner.dry_run(
            runner.build_run_config("workflow-a", 2, live=False), repo_root=REPO
        )
    assert _seed_state(case) == before
    assert outcome["status"] == runner.STATUS_DRY_RUN_PREPARED, outcome["blockers"]
    assert (
        outcome["is_benchmark_attempt"] is False and outcome["would_execute"] is False
    )
    assert outcome["runtime"]["probed"] is False
    assert outcome["seed"]["commit_present"] and outcome["seed"]["tree_matches"]
    porcelain = before[1].splitlines()
    assert outcome["seed"]["worktree_changes"] == sorted(line[3:] for line in porcelain)
    names = [item["disposable_workspace"] for item in outcome["planned_attempts"]]
    assert len(names) == 2 and all(
        name.startswith("phase40d-workflow-a-") for name in names
    )
    assert not any((DEV_RUNTIME / "workspace" / name).exists() for name in names)


def test_dry_run_blocks_incomplete_case_and_rejects_invalid_config():
    cases = load_cases()
    outcome = runner.dry_run(
        runner.build_run_config("phase40b-r3-discount", 1, live=False),
        repo_root=REPO,
        cases=cases,
    )
    assert outcome["status"] == runner.STATUS_DRY_RUN_BLOCKED
    assert any(blocker.startswith("case incomplete") for blocker in outcome["blockers"])
    bad = runner.build_run_config("workflow-a", 5, live=False)
    assert runner.dry_run(bad, repo_root=REPO)["status"] == runner.STATUS_INVALID_CONFIG
    unknown = runner.dry_run(
        runner.build_run_config("nope", 1, live=False), repo_root=REPO, cases=cases
    )
    assert unknown["status"] == runner.STATUS_INVALID_CONFIG


def test_live_run_fails_closed_without_campaign(capsys):
    # Phase 40D-2 implements live runs; without a pinned campaign they still
    # fail closed before touching any runtime, provider, or repository.
    outcome = runner.run_live(
        runner.build_run_config("workflow-a", 1, live=True),
        repo_root=REPO,
        campaign_id=None,
        output_root=REPO / "docs/roadmap/reports/evidence/phase40d2",
    )
    assert outcome["status"] == runner.STATUS_INVALID_CONFIG
    assert outcome["is_benchmark_attempt"] is False
    assert (
        cli.main(["run", "--case", "workflow-a", "--max-attempts", "1", "--live"])
        == cli.EXIT_INVALID
    )
    assert json.loads(capsys.readouterr().out)["status"] == runner.STATUS_INVALID_CONFIG
    with pytest.raises(SystemExit):
        cli.main(["run", "--case", "workflow-a"])  # mode must be explicit


def test_isolation_checks_reject_production_shapes(tmp_path):
    from scripts import dev_runtime

    config = dev_runtime.RuntimeConfig.for_repo(REPO)
    assert all(check["passed"] for check in runner.isolation_checks(config))

    class Fake:
        api_url = "http://10.0.0.5:8080"
        queue_name = "celery"
        root = tmp_path
        repo_root = REPO

    failed = {
        check["check"]
        for check in runner.isolation_checks(Fake())
        if not check["passed"]
    }
    assert failed == {
        "api_loopback",
        "dedicated_queue",
        "runtime_root_inside_dev_runtime",
    }


# -- canonical seed and production isolation ----------------------------------


def test_canonical_seeds_and_definitions_unchanged_by_benchmark(tmp_path):
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [REGISTRY_PATH, *sorted(CASES_DIR.glob("*.json"))]
    }
    db = DEV_RUNTIME / "database" / "orchestrator-dev.sqlite3"
    db_state = (
        (db.stat().st_mtime_ns, hashlib.sha256(db.read_bytes()).hexdigest())
        if db.is_file()
        else None
    )
    history = SyntheticHistory(tmp_path)
    history.add_task(1, events=PLANNING_ONLY)
    history.slots.append(
        {"case_id": "workflow-a", "slot": "A1", "task_id": 1, "project_id": 1}
    )
    history.load()
    runner.dry_run(runner.build_run_config("workflow-b", 1, live=False), repo_root=REPO)
    if HAS_HISTORY:
        cli.evaluate_history(REPO / "docs/roadmap/reports/evidence", DEV_RUNTIME)
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [REGISTRY_PATH, *sorted(CASES_DIR.glob("*.json"))]
    }
    assert after == hashes
    if db_state is not None:
        assert (
            db.stat().st_mtime_ns,
            hashlib.sha256(db.read_bytes()).hexdigest(),
        ) == db_state
    for case in load_cases().values():
        repo = DEV_RUNTIME / "workspace" / case["seed"]["repository"]
        if not repo.is_dir():
            continue
        tree = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", f"{case['seed']['commit']}^{{tree}}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert tree == case["seed"]["tree"], case["case_id"]


def test_history_database_is_opened_read_only(tmp_path):
    history = SyntheticHistory(tmp_path)
    history.add_task(1, events=PLANNING_ONLY)
    loader = HistoryLoader(
        EvidenceRoots(REPO, tmp_path, history.runtime), history.registry(), load_cases()
    )
    with pytest.raises(sqlite3.OperationalError):
        loader.db._conn.execute("insert into projects values (500, 'x')")
    loader.db.close()


def test_historical_task_ids_are_not_reused_as_execution_identities():
    outcome = runner.dry_run(
        runner.build_run_config("workflow-c", 2, live=False), repo_root=REPO
    )
    text = json.dumps(outcome)
    assert "task_id" not in text and "task_execution_id" not in text
    assert all(
        "fresh" in " ".join(item["steps"])
        for item in outcome.get("planned_attempts", [])
    )
