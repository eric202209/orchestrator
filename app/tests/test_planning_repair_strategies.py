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


def test_direct_ollama_structured_op_plan_is_normalized_before_repair():
    raw_plan = [
        {
            "step": 1,
            "op": "write_file",
            "path": "README.md",
            "content": "# Project Title\n\n## Status\nIn progress.\n",
        },
        {
            "step": 2,
            "commands": [],
            "verification": (
                "python -c \"import pathlib,sys; sys.exit(0 if 'Status' "
                "in pathlib.Path('README.md').read_text() else 1)\""
            ),
        },
        {
            "step": 3,
            "commands": [],
            "verification": (
                'python -c "import pathlib,sys; '
                "sys.exit(0 if pathlib.Path('README.md').exists() else 1)\""
            ),
        },
        {"step": 4, "cmd": "echo Verification complete"},
    ]

    normalized = PlannerService.sanitize_common_plan_issues(raw_plan)

    assert [step["step_number"] for step in normalized] == [1, 2, 3, 4]
    assert normalized[0]["ops"] == [
        {
            "op": "write_file",
            "path": "README.md",
            "content": "# Project Title\n\n## Status\nIn progress.\n",
        }
    ]
    assert normalized[0]["expected_files"] == ["README.md"]
    assert normalized[0]["verification"].startswith("python -c ")
    assert normalized[1]["commands"] == [normalized[1]["verification"]]
    assert normalized[2]["commands"] == [normalized[2]["verification"]]
    assert normalized[3]["commands"] == ["echo Verification complete"]


def test_direct_ollama_type_file_ops_plan_is_normalized_before_repair():
    raw_plan = [
        {
            "step": 1,
            "action": "Create README.md with project description and Status section",
            "ops": [
                {
                    "type": "write",
                    "file": "README.md",
                    "content": "# Project Description\n\n## Status\nIn progress.",
                }
            ],
        },
        {
            "step": 2,
            "action": "Verify README.md exists and contains Status",
            "ops": [{"type": "check", "file": "README.md", "content": "Status"}],
        },
    ]

    normalized = PlannerService.sanitize_common_plan_issues(raw_plan)

    assert normalized[0]["ops"] == [
        {
            "op": "write_file",
            "path": "README.md",
            "content": "# Project Description\n\n## Status\nIn progress.",
        }
    ]
    assert normalized[0]["expected_files"] == ["README.md"]
    assert normalized[0]["verification"].startswith("python -c ")
    assert normalized[1]["commands"] == [normalized[1]["verification"]]
    assert "Status" in normalized[1]["verification"]


def test_generated_file_verifications_reject_absolute_and_traversal_paths():
    raw_plan = [
        {
            "step": 1,
            "op": "write_file",
            "path": "../../outside.txt",
            "content": "bad",
        },
        {
            "step": 2,
            "op": "verify_file",
            "path": "/etc/passwd",
        },
        {
            "step": 3,
            "op": "check",
            "path": "../secret.txt",
            "content": "token",
        },
    ]

    normalized = PlannerService.sanitize_common_plan_issues(raw_plan)

    assert normalized[0]["verification"] == 'python -c "import sys; sys.exit(1)"'
    assert normalized[1]["verification"] == 'python -c "import sys; sys.exit(1)"'
    assert normalized[2]["verification"] == 'python -c "import sys; sys.exit(1)"'


def test_expected_files_generate_verification_without_structured_ops():
    raw_plan = [
        {
            "step": 1,
            "commands": ["cp source.txt output.txt"],
            "expected_files": ["output.txt"],
        }
    ]

    normalized = PlannerService.sanitize_common_plan_issues(raw_plan)

    assert normalized[0]["verification"].startswith("python -c ")
    assert "output.txt" in normalized[0]["verification"]


def test_mkdir_operation_drops_write_only_fields():
    raw_plan = [
        {
            "step": 1,
            "ops": [
                {
                    "op": "mkdir",
                    "path": "src",
                    "content": "",
                    "old": "x",
                    "new": "y",
                }
            ],
        }
    ]

    normalized = PlannerService.sanitize_common_plan_issues(raw_plan)

    assert normalized[0]["ops"] == [{"op": "mkdir", "path": "src"}]


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
