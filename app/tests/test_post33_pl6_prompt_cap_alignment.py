from pathlib import Path

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryObservation,
)


def test_minimal_retries_preserve_admitted_discovery_observation(tmp_path: Path):
    observation = DiscoveryObservation(
        action="read_file",
        status="completed",
        paths=("app/tasks/maintenance.py",),
        content="def scheduled_task_execution(value):\n    return value\n",
    )

    minimal = PlannerService.build_minimal_planning_prompt(
        "Change the maintenance task",
        project_dir=tmp_path,
        read_only_observation=observation,
    )
    ultra = PlannerService.build_ultra_minimal_planning_prompt(
        "Change the maintenance task",
        project_dir=tmp_path,
        read_only_observation=observation,
    )

    for prompt in (minimal, ultra):
        assert "## READ-ONLY OBSERVATION" in prompt
        assert "app/tasks/maintenance.py" in prompt
        assert "scheduled_task_execution" in prompt
