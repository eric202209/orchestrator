from __future__ import annotations

from app.services.orchestration.validation.workspace_checks import (
    assess_plan_workspace_compatibility,
)


def test_resume_compatibility_ignores_future_expected_files_before_first_step(
    tmp_path,
):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    plan = [
        {
            "step_number": 1,
            "description": "Inspect workspace",
            "commands": ["ls -la"],
            "verification": "test -f index.html",
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Create final validation script",
            "ops": [
                {
                    "op": "write_file",
                    "path": "validate_project.py",
                    "content": "print('ok')\n",
                }
            ],
            "verification": "python validate_project.py",
            "expected_files": ["validate_project.py"],
        },
    ]

    compatibility = assess_plan_workspace_compatibility(
        project_dir=project_dir,
        plan=plan,
        completed_step_count=0,
    )

    assert compatibility["compatible"] is True
    assert compatibility["expected_core_files"] == []


def test_resume_compatibility_checks_expected_files_after_completed_steps(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    plan = [
        {
            "step_number": 1,
            "description": "Create validation script",
            "ops": [
                {
                    "op": "write_file",
                    "path": "validate_project.py",
                    "content": "print('ok')\n",
                }
            ],
            "verification": "python validate_project.py",
            "expected_files": ["validate_project.py"],
        },
    ]

    compatibility = assess_plan_workspace_compatibility(
        project_dir=project_dir,
        plan=plan,
        completed_step_count=1,
    )

    assert compatibility["compatible"] is False
    assert compatibility["expected_core_files"] == ["validate_project.py"]
