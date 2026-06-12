import json
from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.phases.planning_flow import (
    _looks_like_verification_only_task,
    _prune_unmaterialized_expected_files,
    _read_only_stage_fallback_plan,
    _repair_removed_source_materialization,
    _split_repaired_single_step_full_lifecycle_plan,
)
from app.services.orchestration.validation.validator import ValidatorService


def test_repaired_single_step_split_uses_actual_edit_paths_not_speculative_expected_files(
    tmp_path,
):
    site_root = tmp_path / "public" / "status-site"
    (site_root / "css").mkdir(parents=True)
    (site_root / "index.html").write_text("<main></main>", encoding="utf-8")
    (site_root / "css" / "style.css").write_text("body {}", encoding="utf-8")
    repaired_single_step = [
        {
            "step_number": 1,
            "description": "Add incident summary to existing status site",
            "commands": [],
            "verification": "python -c \"from pathlib import Path; assert Path('public/status-site/index.html').exists()\"",
            "rollback": "true",
            "expected_files": [
                "public/status-site/index.html",
                "public/status-site/css/style.css",
                "README.md",
            ],
            "ops": [
                {
                    "op": "append_file",
                    "path": "public/status-site/index.html",
                    "content": "<section>API Queue Knowledge</section>",
                },
                {
                    "op": "replace_in_file",
                    "path": "public/status-site/css/style.css",
                    "old": "body {}",
                    "new": "body { color: #111; }",
                },
            ],
        }
    ]

    split_plan = _split_repaired_single_step_full_lifecycle_plan(repaired_single_step)

    assert split_plan is not None
    assert split_plan[1]["expected_files"] == [
        "public/status-site/index.html",
        "public/status-site/css/style.css",
    ]
    verdict = ValidatorService.validate_plan(
        split_plan,
        output_text="[]",
        task_prompt="Add incident summary section to existing status site",
        execution_profile="full_lifecycle",
        workflow_stage="implement",
        project_dir=tmp_path,
    )
    assert verdict.accepted
    assert "unmaterialized_expected_files" not in verdict.details


def test_repair_removed_source_materialization_detects_no_op_salvage():
    rejected_plan = [
        {
            "step_number": 2,
            "description": "Create missing operations module",
            "commands": [],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/math_tools/operations.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/math_tools/operations.py",
                    "content": "# Placeholder\n\ndef add(a, b):\n    return a + b\n",
                }
            ],
        }
    ]
    salvaged_plan = [
        {
            "step_number": 1,
            "description": "Inspect current workspace",
            "commands": ["rg --files . | sort"],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": [],
        }
    ]

    assert _repair_removed_source_materialization(rejected_plan, salvaged_plan) == [
        "src/math_tools/operations.py"
    ]


def test_repair_removed_source_materialization_allows_preserved_source_write():
    rejected_plan = [
        {
            "step_number": 2,
            "description": "Create missing operations module",
            "commands": [],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/math_tools/operations.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/math_tools/operations.py",
                    "content": "# Placeholder\n\ndef add(a, b):\n    return a + b\n",
                }
            ],
        }
    ]
    repaired_plan = [
        {
            "step_number": 2,
            "description": "Create missing operations module",
            "commands": [],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/math_tools/operations.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/math_tools/operations.py",
                    "content": "def add(a, b):\n    return a + b\n",
                }
            ],
        }
    ]

    assert _repair_removed_source_materialization(rejected_plan, repaired_plan) == []


def test_prune_unmaterialized_expected_files_keeps_concrete_edit_scope(tmp_path):
    site_root = tmp_path / "public" / "status-site"
    (site_root / "css").mkdir(parents=True)
    (site_root / "index.html").write_text("<main></main>", encoding="utf-8")
    (site_root / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Edit existing status site",
            "commands": [],
            "verification": "python -c \"from pathlib import Path; assert Path('public/status-site/index.html').exists()\"",
            "rollback": "true",
            "expected_files": [
                "public/status-site/index.html",
                "public/status-site/css/style.css",
                "README.md",
            ],
            "ops": [
                {
                    "op": "append_file",
                    "path": "public/status-site/index.html",
                    "content": "<section>Knowledge</section>",
                }
            ],
        }
    ]

    pruned, details = _prune_unmaterialized_expected_files(plan, ["README.md"])

    assert details["changed"] is True
    assert details["removed_expected_files"] == ["README.md"]
    assert pruned[0]["expected_files"] == [
        "public/status-site/index.html",
        "public/status-site/css/style.css",
    ]


def test_prune_unmaterialized_expected_files_does_not_hide_missing_outputs():
    plan = [
        {
            "step_number": 1,
            "description": "Declare files without edits",
            "commands": [],
            "verification": None,
            "rollback": "true",
            "expected_files": ["index.html"],
            "ops": [],
        }
    ]

    pruned, details = _prune_unmaterialized_expected_files(plan, ["index.html"])

    assert pruned == plan
    assert details["changed"] is False
    assert details["reason"] == "no_concrete_file_ops"


def test_plan_workflow_stage_rejects_mutating_file_ops():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Write a recovery file",
                "commands": [],
                "verification": None,
                "expected_files": ["index.html"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "index.html",
                        "content": "<html></html>",
                    }
                ],
            }
        ],
        output_text="[]",
        task_prompt="Plan the recovery approach without changing files",
        execution_profile="review_only",
        workflow_stage="plan",
    )

    assert not verdict.accepted
    assert "read_only_stage_mutation_steps" in verdict.details


def test_validate_workflow_stage_does_not_require_expected_file_materialization():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Run bounded quality checks",
                "commands": ["test -f README.md || true"],
                "verification": "test -d .",
                "rollback": "true",
                "expected_files": ["README.md"],
            }
        ],
        output_text="[]",
        task_prompt="Add a final quality check without background server",
        execution_profile="test_only",
        workflow_stage="validate",
    )

    assert "unmaterialized_expected_files" not in verdict.details
    assert not any("declares expected files" in reason for reason in verdict.reasons)


def test_validate_workflow_stage_uses_verification_workspace_checks(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Verify stylesheet link",
                "commands": ["grep -q styles.css index.html || true"],
                "verification": "test -f styles.css",
                "rollback": "true",
                "expected_files": ["styles.css"],
            }
        ],
        output_text="[]",
        task_prompt="Add a final quality check without background server",
        execution_profile="test_only",
        workflow_stage="validate",
        project_dir=tmp_path,
    )

    assert not verdict.accepted
    assert verdict.details["missing_workspace_expected_files"] == ["styles.css"]
    assert "unmaterialized_expected_files" not in verdict.details


def test_existing_expected_files_do_not_require_rematerialization(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "flower-bg.svg").write_text("<svg></svg>", encoding="utf-8")

    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Inspect existing SVG",
                "commands": ["ls images"],
                "verification": "test -f images/flower-bg.svg",
                "rollback": "true",
                "expected_files": ["images/flower-bg.svg"],
            },
            {
                "step_number": 2,
                "description": "Create second SVG",
                "commands": [],
                "verification": "test -f images/tulip-card.svg",
                "rollback": "rm -f images/tulip-card.svg",
                "expected_files": ["images/tulip-card.svg"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "images/tulip-card.svg",
                        "content": "<svg></svg>",
                    }
                ],
            },
        ],
        output_text="[]",
        task_prompt="Create images/tulip-card.svg and reference existing flower SVG",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert "unmaterialized_expected_files" not in verdict.details
    assert not any("declares expected files" in reason for reason in verdict.reasons)


def test_validate_workflow_stage_rejects_file_mutation():
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Write validation report",
                "commands": ["echo ok > quality-report.txt"],
                "verification": "test -f quality-report.txt",
                "rollback": "rm -f quality-report.txt",
                "expected_files": ["quality-report.txt"],
            }
        ],
        output_text="[]",
        task_prompt="Validate the project without changing files",
        execution_profile="test_only",
        workflow_stage="validate",
    )

    assert not verdict.accepted
    assert verdict.details["read_only_stage_mutation_steps"] == [1]


def test_validate_plan_does_not_mutate_input_when_reordering_existence_check():
    plan = [
        {
            "step_number": 1,
            "description": "Check file before creation",
            "commands": ["test -f app.py"],
            "verification": "test -f app.py",
            "rollback": "true",
            "expected_files": ["app.py"],
        },
        {
            "step_number": 2,
            "description": "Create app file",
            "commands": [],
            "verification": "python -m py_compile app.py",
            "rollback": "rm -f app.py",
            "expected_files": ["app.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app.py",
                    "content": "VALUE = 1\n",
                }
            ],
        },
    ]
    original = json.loads(json.dumps(plan))

    ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Create app.py",
        execution_profile="full_lifecycle",
        project_dir=Path("."),
    )

    assert plan == original


def test_read_only_stage_fallback_plan_is_non_mutating():
    ctx = SimpleNamespace(
        prompt="Add a final quality check without background server",
        execution_profile="test_only",
        workflow_stage="validate",
    )

    plan = _read_only_stage_fallback_plan(ctx)
    assert plan is not None
    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt=ctx.prompt,
        execution_profile=ctx.execution_profile,
        workflow_stage=ctx.workflow_stage,
    )

    assert verdict.accepted
    assert plan[0]["expected_files"] == []
    assert ">" not in plan[0]["commands"][0]


def test_validate_workflow_stage_completion_allows_no_source_outputs(tmp_path):
    verdict = ValidatorService.validate_task_completion(
        project_dir=tmp_path,
        plan=[
            {
                "step_number": 1,
                "description": "Inspect workspace for validate stage",
                "commands": ["python -c 'print(1)'"],
                "verification": "python -c 'print(1)'",
                "rollback": "true",
                "expected_files": [],
            }
        ],
        task_prompt="Add a final quality check without background server",
        execution_profile="test_only",
        workflow_stage="validate",
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": [],
        },
    )

    assert verdict.accepted
    details = verdict.details["completion_contract"]
    assert details
    assert details["validation_profile"] == "verification"


def test_validate_workflow_stage_completion_resolves_static_site_relative_mentions(
    tmp_path,
):
    site_root = tmp_path / "public" / "status-site"
    (site_root / "css").mkdir(parents=True)
    (site_root / "images").mkdir()
    (site_root / "index.html").write_text(
        "<link rel='stylesheet' href='css/style.css'>"
        "<img src='images/status-badge.svg' alt='Status Badge'>"
        "<section>API Queue Knowledge</section>",
        encoding="utf-8",
    )
    (site_root / "css" / "style.css").write_text("body {}", encoding="utf-8")
    (site_root / "images" / "status-badge.svg").write_text(
        "<svg></svg>",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=tmp_path,
        plan=[
            {
                "step_number": 1,
                "description": "Inspect workspace for validate stage",
                "commands": ["python -c 'print(1)'"],
                "verification": "python -c 'print(1)'",
                "rollback": "true",
                "expected_files": [],
            }
        ],
        task_prompt=(
            "Validate the final public/status-site without changing files. "
            "Check that index.html, css/style.css, and images/status-badge.svg exist."
        ),
        execution_profile="test_only",
        workflow_stage="validate",
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": [],
        },
    )

    assert verdict.accepted
    assert verdict.details["expected_core_files"] == [
        "public/status-site/css/style.css",
        "public/status-site/images/status-badge.svg",
    ]
    assert "missing_core_files" not in verdict.details


def test_existing_static_site_plan_rejects_static_writes_outside_detected_root(
    tmp_path,
):
    site_root = tmp_path / "public" / "status-site"
    (site_root / "css").mkdir(parents=True)
    (site_root / "index.html").write_text("<main></main>", encoding="utf-8")
    (site_root / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Improve existing status site styling",
            "commands": [],
            "verification": "python -c \"import pathlib; print(pathlib.Path('styles/additional.css').exists())\"",
            "rollback": "true",
            "expected_files": ["styles/additional.css"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "styles/additional.css",
                    "content": "@media (max-width: 600px) { body { margin: 0; } }",
                }
            ],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Update the existing status site responsive styling",
        execution_profile="full_lifecycle",
        workflow_stage="implement",
        project_dir=tmp_path,
    )

    assert not verdict.accepted
    assert verdict.details["static_site_off_root_mutations"] == [
        "styles/additional.css"
    ]


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
