from __future__ import annotations

import json
from pathlib import Path

from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.repair_strategies import (
    build_specialized_repair_prompt,
)


def _static_workspace(tmp_path: Path) -> None:
    (tmp_path / "css").mkdir()
    (tmp_path / "images").mkdir()
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="css/style.css">'
        '<img src="images/flower-bg.svg" alt="">',
        encoding="utf-8",
    )
    (tmp_path / "css" / "style.css").write_text(
        "body { background: white; }", encoding="utf-8"
    )
    (tmp_path / "images" / "flower-bg.svg").write_text("<svg></svg>", encoding="utf-8")


def test_specialized_verification_repair_prompt_uses_inventory_without_source_dump(
    tmp_path,
):
    _static_workspace(tmp_path)
    malformed = json.dumps(
        [
            {
                "step_number": 1,
                "description": "Create or update index.html",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "index.html",
                        "content": "x" * 2000,
                    }
                ],
                "commands": [],
                "verification": "node -e \"require('fs').readFileSync('styles.css')\"",
                "rollback": None,
                "expected_files": ["index.html", "styles.css"],
            }
        ]
    )

    prompt = build_specialized_repair_prompt(
        task_description="Upgrade landing page verification commands",
        malformed_output=malformed,
        project_dir=tmp_path,
        rejection_reasons=[
            "Verification/review plan references source files that do not exist "
            "in the current workspace (files: ['styles.css'])"
        ],
        knowledge_block="## REPAIR KNOWLEDGE REFERENCES\nUse existing paths.",
    )

    assert prompt is not None
    assert "Verification-only repair mode" in prompt
    assert "- index.html" in prompt
    assert "- css/style.css" in prompt
    assert "- images/flower-bg.svg" in prompt
    assert "x" * 200 not in prompt
    assert len(prompt) < 3500


def test_planner_delegates_verification_workspace_failures_to_specialized_prompt(
    tmp_path,
):
    _static_workspace(tmp_path)

    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Upgrade landing page verification commands",
        malformed_output=json.dumps(
            [
                {
                    "step_number": 1,
                    "description": "Verify wrong file",
                    "commands": ["cat styles.css"],
                    "verification": "node -e \"require('fs').readFileSync('styles.css')\"",
                    "rollback": None,
                    "expected_files": [],
                }
            ]
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "Verification/review plan references source files that do not exist "
            "in the current workspace (files: ['styles.css'])"
        ],
    )

    assert "Verification-only repair mode" in prompt
    assert "Use only paths from the existing workspace source files list" in prompt
    assert len(prompt) < 3500
