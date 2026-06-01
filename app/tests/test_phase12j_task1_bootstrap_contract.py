import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, LogEntry, Project, Task, TaskStatus
from app.services.observability.metrics_collector import MetricsCollector
from app.services.orchestration.phases.planning_task1_bootstrap import (
    normalize_task1_bootstrap_plan_for_json_stability,
    task1_bootstrap_contract_passed,
    task1_plan_failed_only_brittle_command_shape,
)
from app.services.orchestration.planning.repair_arbitration import (
    classify_planning_repair_candidate,
)
from app.services.orchestration.validation.validator import ValidatorService


@pytest.fixture()
def mem_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine)
    db = session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _step(
    *,
    ops=None,
    commands=None,
    verification="python -m pytest -q",
    expected_files=None,
):
    return {
        "step_number": 1,
        "description": "Bootstrap the first implementation slice",
        "commands": commands if commands is not None else [],
        "verification": verification,
        "rollback": None,
        "expected_files": expected_files if expected_files is not None else [],
        "ops": ops if ops is not None else [],
    }


def test_task1_bootstrap_rejects_inspect_only_plan(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                commands=["python -c \"import os; print(os.listdir('.'))\""],
                verification=None,
            )
        ],
        output_text="[]",
        task_prompt="Build the first implementation slice for a small app",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert not verdict.accepted
    assert contract["passed"] is False
    assert (
        "task1_bootstrap_missing_expected_source_files" in contract["violation_codes"]
    )
    assert (
        "task1_bootstrap_missing_required_verification" in contract["violation_codes"]
    )
    assert (
        "task1_bootstrap_minimum_implementation_evidence_missing"
        in contract["violation_codes"]
    )
    assert (
        "task1_bootstrap_missing_expected_source_files"
        in verdict.details["semantic_violation_codes"]
    )


def test_task1_bootstrap_rejects_requested_tests_without_test_files(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "src/app.py",
                        "content": "def answer():\n    return 42\n",
                    }
                ],
                expected_files=["src/app.py"],
                verification="python -m pytest -q",
            )
        ],
        output_text="[]",
        task_prompt="Build the first app slice with tests",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert not verdict.accepted
    assert "task1_bootstrap_missing_expected_test_files" in contract["violation_codes"]


def test_task1_repair_source_preservation_is_not_enough_when_tests_disappear(tmp_path):
    previous_plan = [
        _step(
            ops=[
                {
                    "op": "write_file",
                    "path": "src/notes_app/greetings.py",
                    "content": "def greeting(name):\n    return f'Hello, {name}!'\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_greetings.py",
                    "content": (
                        "from notes_app.greetings import greeting\n\n"
                        "def test_greeting():\n"
                        "    assert greeting('Ada') == 'Hello, Ada!'\n"
                    ),
                },
            ],
            expected_files=[
                "src/notes_app/greetings.py",
                "tests/test_greetings.py",
            ],
        )
    ]
    repaired_plan = [
        _step(
            ops=[
                {
                    "op": "write_file",
                    "path": "src/notes_app/greetings.py",
                    "content": "def greeting(name):\n    return f'Hello, {name}!'\n",
                }
            ],
            expected_files=["src/notes_app/greetings.py"],
            verification="python -m pytest -q",
        )
    ]

    arbitration = classify_planning_repair_candidate(
        previous_plan=previous_plan,
        repaired_plan=repaired_plan,
        project_dir=tmp_path,
    )
    verdict = ValidatorService.validate_plan(
        repaired_plan,
        output_text=json.dumps(repaired_plan),
        task_prompt="Extend the existing notes app with greeting support and tests",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    assert arbitration["source_materialization"]["status"] == "preserved"
    assert "removed_materialization" not in arbitration["regression_labels"]
    contract = verdict.details["task1_bootstrap_contract"]
    assert not verdict.accepted
    assert "task1_bootstrap_missing_expected_test_files" in contract["violation_codes"]


def test_task1_bootstrap_accepts_source_test_and_verification(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "src/app.py",
                        "content": "def answer():\n    return 42\n",
                    },
                    {
                        "op": "write_file",
                        "path": "tests/test_app.py",
                        "content": (
                            "from src.app import answer\n\n"
                            "def test_answer():\n"
                            "    assert answer() == 42\n"
                        ),
                    },
                ],
                expected_files=["src/app.py", "tests/test_app.py"],
                verification="python -m pytest -q",
            )
        ],
        output_text="[]",
        task_prompt="Build the first app slice with tests",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert verdict.accepted
    assert contract["passed"] is True
    assert contract["expected_source_files"] == ["src/app.py"]
    assert contract["expected_test_files"] == ["tests/test_app.py"]
    assert contract["minimum_implementation_evidence"] is True


def test_task1_bootstrap_normalizes_stale_heredoc_output_before_repair(tmp_path):
    plan = [
        _step(
            commands=[
                "cat > src/app.py <<'PY'\n" "def answer():\n" "    return 42\n" "PY"
            ],
            ops=[
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "def answer():\n    return 42\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_app.py",
                    "content": (
                        "from src.app import answer\n\n"
                        "def test_answer():\n"
                        "    assert answer() == 42\n"
                    ),
                },
            ],
            expected_files=["src/app.py", "tests/test_app.py"],
        )
    ]
    stale_output_text = (
        "```json\n"
        '[{"commands":["cat > src/app.py <<EOF","cat > tests/test_app.py <<EOF"]}]'
        "\n```"
    )
    initial_verdict = ValidatorService.validate_plan(
        plan,
        output_text=stale_output_text,
        task_prompt="Build the first app slice with tests",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    assert not initial_verdict.accepted
    assert task1_bootstrap_contract_passed(initial_verdict)
    assert task1_plan_failed_only_brittle_command_shape(initial_verdict)

    normalized_plan = normalize_task1_bootstrap_plan_for_json_stability(plan)
    final_verdict = ValidatorService.validate_plan(
        normalized_plan,
        output_text=json.dumps(normalized_plan),
        task_prompt="Build the first app slice with tests",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    assert normalized_plan[0]["commands"] == []
    assert final_verdict.accepted


def test_task1_bootstrap_contract_is_not_applied_to_later_tasks(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                commands=["python -c \"import os; print(os.listdir('.'))\""],
                verification=None,
            )
        ],
        output_text="[]",
        task_prompt="Build a later implementation slice",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=False,
    )

    assert "task1_bootstrap_contract" not in verdict.details


def test_task1_product_health_counts_events_and_blocked_projects(mem_db):
    now = datetime.now(UTC)
    project = Project(name="Task1 Metrics", workspace_path="/tmp/task1-metrics")
    mem_db.add(project)
    mem_db.flush()
    first_task = Task(
        project_id=project.id,
        title="Bootstrap",
        plan_position=1,
        status=TaskStatus.FAILED,
        created_at=now,
    )
    second_task = Task(
        project_id=project.id,
        title="Follow-up",
        plan_position=2,
        status=TaskStatus.PENDING,
        created_at=now,
    )
    mem_db.add_all([first_task, second_task])
    mem_db.flush()
    for event_type in (
        "task1_bootstrap_contract_failed",
        "task1_execution_failed",
        "project_blocked_after_task1",
    ):
        mem_db.add(
            LogEntry(
                task_id=first_task.id,
                level="WARN",
                message=event_type,
                log_metadata=json.dumps({"event_type": event_type}),
                created_at=now,
            )
        )
    mem_db.commit()

    metrics = MetricsCollector(mem_db).task1_product_health(days=1)

    assert metrics["event_counters"]["task1_bootstrap_contract_failed"] == 1
    assert metrics["event_counters"]["task1_execution_failed"] == 1
    assert metrics["event_counters"]["project_blocked_after_task1"] == 1
    assert metrics["blocked_after_task1_count"] == 1
    assert metrics["ordered_project_first_task_success_rate"] == 0.0
    assert metrics["blocked_after_task1_rate"] == 1.0
