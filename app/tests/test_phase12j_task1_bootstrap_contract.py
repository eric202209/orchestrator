import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, LogEntry, Project, Task, TaskStatus
from app.services.observability.metrics_collector import MetricsCollector
from app.services.orchestration.phases.planning_support import (
    _PlanningRetryState,
    _build_repair_rejection_reasons,
    _get_targeted_second_repair_reason,
)
from app.services.orchestration.phases.planning_task1_bootstrap import (
    normalize_task1_bootstrap_plan_for_json_stability,
    task1_bootstrap_contract_passed,
    task1_plan_failed_only_brittle_command_shape,
)
from app.services.orchestration.planning.task_bootstrap_contract import (
    validate_task1_bootstrap_contract,
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
    assert contract["expected_test_reason"] == "explicit_code_test_intent"


def test_task1_artifact_checklist_test_lifecycle_word_does_not_require_test_files(
    tmp_path,
):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "CHECKLIST.md",
                        "content": (
                            "# Release Checklist\n\n"
                            "- Build the artifact.\n"
                            "- Test the release candidate manually.\n"
                            "- Deploy after approval.\n"
                        ),
                    }
                ],
                expected_files=["CHECKLIST.md"],
                verification=(
                    'python -c "from pathlib import Path; '
                    "text=Path('CHECKLIST.md').read_text(); "
                    "assert 'Deploy after approval' in text\""
                ),
            )
        ],
        output_text="[]",
        task_prompt=(
            "Create an operational checklist artifact covering build, test, "
            "deploy, and rollback as checklist items. Do not create source code."
        ),
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert verdict.accepted
    assert contract["bootstrap_task_type"] == "ARTIFACT_ONLY"
    assert contract["expected_test_reason"] == "artifact_only_no_code_test_intent"
    assert (
        "task1_bootstrap_missing_expected_test_files" not in contract["violation_codes"]
    )


def test_task1_existing_project_tests_source_fix_does_not_require_test_materialization(
    tmp_path,
):
    # E29: existing tests are verification assets for source-fix tasks; the plan
    # must NOT be rejected for lacking test file ops when it has source + verification.
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("def test_existing():\n    assert True\n")

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
        task_prompt="Extend the existing app implementation",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert verdict.accepted
    assert contract["expected_test_reason"] == "existing_project_tests_present"
    assert (
        "task1_bootstrap_missing_expected_test_files" not in contract["violation_codes"]
    )


def test_task1_unknown_type_with_explicit_tests_remains_conservative(tmp_path):
    verdict = validate_task1_bootstrap_contract(
        plan=[
            _step(
                commands=["python -c \"print('inspect')\""],
                verification="python -m pytest -q",
            )
        ],
        task_prompt="Create the first project slice with tests",
    )

    contract = verdict.to_dict()
    assert not verdict.passed
    assert contract["bootstrap_task_type"] == "UNKNOWN"
    assert contract["expected_test_reason"] == "unknown_conservative"
    assert "task1_bootstrap_missing_expected_test_files" in contract["violation_codes"]


def test_task1_mixed_code_component_requires_test_diagnostic(tmp_path):
    verdict = validate_task1_bootstrap_contract(
        plan=[
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "src/app.py",
                        "content": "def answer():\n    return 42\n",
                    },
                    {
                        "op": "write_file",
                        "path": "README.md",
                        "content": "# App\n\nUsage documentation for the app.\n",
                    },
                ],
                expected_files=["src/app.py", "README.md"],
                verification="python -m pytest -q",
            )
        ],
        task_prompt="Create a mixed project with a small Python utility and docs",
    )

    contract = verdict.to_dict()
    assert not verdict.passed
    assert contract["bootstrap_task_type"] == "MIXED"
    assert contract["expected_test_reason"] == "mixed_task_code_component"
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


def test_task1_bootstrap_contract_reports_required_artifacts_for_repair(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "src/notes_app/parser.py",
                        "content": "def parse_note(raw):\n    return {'title': raw}\n",
                    },
                    {
                        "op": "write_file",
                        "path": "tests/test_parser.py",
                        "content": (
                            "from notes_app.parser import parse_note\n\n"
                            "def test_parse_note():\n"
                            "    assert parse_note('Title')['title'] == 'Title'\n"
                        ),
                    },
                ],
                expected_files=["src/notes_app/parser.py", "tests/test_parser.py"],
            )
        ],
        output_text="[]",
        task_prompt="Extend the existing notes app with parser tests",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert "src/notes_app/__init__.py" in contract["required_artifacts"]
    assert "src/notes_app/parser.py" in contract["required_source_files"]
    assert "src/notes_app/__init__.py" in contract["required_source_files"]
    assert contract["required_test_files"] == ["tests/test_parser.py"]


def test_task1_repair_rejection_reasons_include_same_contract_payload(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "src/notes_app/parser.py",
                        "content": "def parse_note(raw):\n    return {'title': raw}\n",
                    },
                    {
                        "op": "write_file",
                        "path": "tests/test_parser.py",
                        "content": (
                            "from notes_app.parser import parse_note\n\n"
                            "def test_parse_note():\n"
                            "    assert parse_note('Title')['title'] == 'Title'\n"
                        ),
                    },
                ],
                expected_files=["src/notes_app/parser.py", "tests/test_parser.py"],
            )
        ],
        output_text="[]",
        task_prompt="Extend the existing notes app with parser tests",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    reasons = _build_repair_rejection_reasons(verdict.reasons, verdict.details)
    rendered = "\n".join(reasons)

    assert "task1_bootstrap_contract" in rendered
    assert "required_artifacts" in rendered
    assert "src/notes_app/__init__.py" in rendered
    assert "tests/test_parser.py" in rendered
    assert "import the package namespace, not `src.*`" in rendered


def test_post_repair_task1_bootstrap_failure_gets_targeted_second_pass(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "src/notes_app/__init__.py",
                        "content": "",
                    },
                    {
                        "op": "write_file",
                        "path": "src/notes_app/parser.py",
                        "content": "def parse_note(raw):\n    return {'title': raw}\n",
                    },
                ],
                expected_files=[
                    "src/notes_app/__init__.py",
                    "src/notes_app/parser.py",
                ],
            )
        ],
        output_text="[]",
        task_prompt="Extend the existing notes app with parser tests",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
        project_dir=tmp_path,
    )

    assert reason is not None
    assert reason.issue_key == "task1_bootstrap_contract"
    assert reason.event_reason == "post_repair_task1_bootstrap_contract_second_pass"
    assert reason.cap_used is False


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
    assert contract["bootstrap_task_type"] == "SOURCE_CODE"


def test_task1_artifact_only_bootstrap_does_not_require_source_materialization(
    tmp_path,
):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "reports/status.md",
                        "content": (
                            "# Status Report\n\n"
                            "## Findings\n"
                            "- Bootstrap evidence is summarized.\n\n"
                            "## Recommendations\n"
                            "- Continue with Task 2.\n"
                        ),
                    }
                ],
                expected_files=["reports/status.md"],
                verification=(
                    'python -c "from pathlib import Path; '
                    "text=Path('reports/status.md').read_text(); "
                    "assert 'Recommendations' in text and len(text) > 50\""
                ),
            )
        ],
        output_text="[]",
        task_prompt="Create a status report artifact for the project",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert verdict.accepted
    assert contract["bootstrap_task_type"] == "ARTIFACT_ONLY"
    assert contract["classification_evidence"]["artifact_paths"] == [
        "reports/status.md"
    ]
    assert contract["required_artifacts"] == ["reports/status.md"]
    assert contract["required_source_files"] == []
    assert contract["expected_source_files"] == []
    assert contract["minimum_artifact_evidence"] is True
    assert (
        "task1_bootstrap_missing_expected_source_files"
        not in contract["violation_codes"]
    )
    assert (
        "task1_bootstrap_minimum_implementation_evidence_missing"
        not in contract["violation_codes"]
    )


def test_task1_artifact_only_ignores_negated_source_code_instruction(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "reports/status.md",
                        "content": (
                            "# Phase 12T Status\n\n"
                            "## Findings\n"
                            "- Artifact-only evidence is present.\n\n"
                            "## Recommendations\n"
                            "- Ready for continuation.\n"
                        ),
                    }
                ],
                expected_files=["reports/status.md"],
                verification=(
                    'python -c "from pathlib import Path; '
                    "text=Path('reports/status.md').read_text(); "
                    "assert 'Ready for continuation' in text\""
                ),
            )
        ],
        output_text="[]",
        task_prompt=(
            "Create a status report artifact. This is an artifact-only "
            "deliverable; do not create source code."
        ),
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert verdict.accepted
    assert contract["bootstrap_task_type"] == "ARTIFACT_ONLY"
    assert contract["classification_evidence"]["has_source_intent"] is False
    assert contract["classification_evidence"]["negated_source_intent_removed"] is True


def test_task1_artifact_only_bootstrap_still_requires_verification(tmp_path):
    verdict = validate_task1_bootstrap_contract(
        plan=[
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "docs/summary.md",
                        "content": "# Summary\n\nSubstantive project summary.\n",
                    }
                ],
                expected_files=["docs/summary.md"],
                verification=None,
            )
        ],
        task_prompt="Create a docs summary artifact",
    )

    contract = verdict.to_dict()
    assert not verdict.passed
    assert contract["bootstrap_task_type"] == "ARTIFACT_ONLY"
    assert (
        "task1_bootstrap_missing_required_verification" in contract["violation_codes"]
    )
    assert (
        "task1_bootstrap_missing_expected_source_files"
        not in contract["violation_codes"]
    )


def test_task1_artifact_only_bootstrap_rejects_placeholder_artifact(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "CHECKLIST.md",
                        "content": "TODO placeholder\n",
                    }
                ],
                expected_files=["CHECKLIST.md"],
                verification="test -s CHECKLIST.md",
            )
        ],
        output_text="[]",
        task_prompt="Create a checklist artifact",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert not verdict.accepted
    assert contract["bootstrap_task_type"] == "ARTIFACT_ONLY"
    assert contract["minimum_artifact_evidence"] is False
    assert (
        "task1_bootstrap_minimum_artifact_evidence_missing"
        in contract["violation_codes"]
    )


def test_task1_mixed_bootstrap_keeps_source_materialization_required(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            _step(
                ops=[
                    {
                        "op": "write_file",
                        "path": "reports/status.md",
                        "content": "# Status Report\n\nImplementation notes.\n",
                    }
                ],
                expected_files=["reports/status.md"],
                verification="test -s reports/status.md",
            )
        ],
        output_text="[]",
        task_prompt="Implement a CLI and create a status report",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )

    contract = verdict.details["task1_bootstrap_contract"]
    assert not verdict.accepted
    assert contract["bootstrap_task_type"] == "MIXED"
    assert (
        "task1_bootstrap_missing_expected_source_files" in contract["violation_codes"]
    )
    assert (
        "task1_bootstrap_minimum_implementation_evidence_missing"
        in contract["violation_codes"]
    )


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
