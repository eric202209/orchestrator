from app.services.orchestration.planning.normalization import (
    complete_repaired_plan_contract,
)
from app.services.orchestration.phases.planning_flow import (
    _looks_like_verification_only_task,
)


def test_static_site_contract_completion_adds_dirs_expected_files_and_verification():
    plan = [
        {
            "step_number": 1,
            "description": "Write static site files",
            "commands": [],
            "verification": None,
            "expected_files": ["index.html"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "index.html",
                    "content": "<html></html>",
                },
                {
                    "op": "write_file",
                    "path": "css/style.css",
                    "content": "body { color: #111; }",
                },
                {
                    "op": "write_file",
                    "path": "images/flower-bg.svg",
                    "content": "<svg></svg>",
                },
            ],
        }
    ]

    completed, details = complete_repaired_plan_contract(
        plan,
        task_prompt="Create index.html, css/style.css, and images/flower-bg.svg",
        repaired=True,
    )

    assert details["changed"] is True
    assert details["added_parent_dirs"] == ["css", "images"]
    assert completed[0]["ops"][0] == {"op": "mkdir", "path": "css"}
    assert completed[0]["ops"][1] == {"op": "mkdir", "path": "images"}
    assert completed[0]["expected_files"] == [
        "index.html",
        "css/style.css",
        "images/flower-bg.svg",
    ]
    assert completed[0]["verification"].startswith("python -c ")


def test_non_static_plan_contract_completion_does_not_change_plan():
    plan = [
        {
            "step_number": 1,
            "description": "Write Python file",
            "commands": [],
            "verification": None,
            "expected_files": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "print('ok')\n",
                }
            ],
        }
    ]

    completed, details = complete_repaired_plan_contract(
        plan,
        task_prompt="Create a small Python module",
        repaired=True,
    )

    assert completed == plan
    assert details["changed"] is False


def test_verification_only_task_detection_excludes_static_site_mutation_tasks():
    assert _looks_like_verification_only_task(
        "Upgrade landing page verification commands",
        (
            "Do not change page design much. Improve task verification so checks "
            "prove content and linkage, not only file existence."
        ),
    )
    assert _looks_like_verification_only_task(
        "Audit garden site for accessibility and link integrity",
        "No major implementation. Inspect current files.",
    )
    assert not _looks_like_verification_only_task(
        "Add seasonal facts section to existing page",
        "Add new `section` with three seasonal flower facts. Update CSS only as needed.",
    )
    assert not _looks_like_verification_only_task(
        "Refine rollback commands for static file edits",
        "Adjust one content block and one CSS rule.",
    )
