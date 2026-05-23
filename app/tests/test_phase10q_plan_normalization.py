from types import SimpleNamespace

from app.services.orchestration.planning.normalization import (
    complete_repaired_plan_contract,
    normalize_existing_static_site_plan,
)
from app.services.orchestration.phases.planning_flow import (
    _looks_like_verification_only_task,
    _read_only_stage_fallback_plan,
)
from app.services.orchestration.validation.validator import ValidatorService


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


def test_static_site_contract_completion_does_not_overwrite_existing_declared_svg(
    tmp_path,
):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "flower-bg.svg").write_text("<svg></svg>", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Inspect existing page asset",
            "commands": ["test -f images/flower-bg.svg"],
            "verification": "test -f images/flower-bg.svg",
            "expected_files": ["images/flower-bg.svg"],
            "ops": [],
        }
    ]

    completed, details = complete_repaired_plan_contract(
        plan,
        task_prompt="Reference existing images/flower-bg.svg",
    )

    assert completed == plan
    assert details["changed"] is False


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


def test_existing_static_site_plan_normalization_rewrites_framework_drift(tmp_path):
    (tmp_path / "css").mkdir()
    (tmp_path / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    (tmp_path / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Add section to src/index.js",
            "commands": [
                "python -c \"import pathlib; pathlib.Path('src/index.js').read_text()\""
            ],
            "verification": "python -c \"import pathlib; pathlib.Path('src/index.js').read_text()\"",
            "rollback": "sed -i '/section/d' src/index.js",
            "expected_files": ["src/index.js"],
            "ops": [
                {
                    "op": "append_file",
                    "path": "src/index.js",
                    "content": "<section>Seasonal Flower Facts</section>",
                }
            ],
        },
        {
            "step_number": 2,
            "description": "Build frontend",
            "commands": ["npm run build"],
            "verification": "npm run build",
            "rollback": None,
            "expected_files": [],
            "ops": [],
        },
    ]

    normalized, details = normalize_existing_static_site_plan(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert details["rewritten_paths"] == {"src/index.js": "index.html"}
    assert normalized[0]["expected_files"] == ["index.html"]
    assert normalized[0]["ops"][0]["path"] == "index.html"
    assert normalized[0]["commands"] == []
    assert normalized[1]["commands"][0].startswith("python -c ")
    verdict = ValidatorService.validate_plan(
        normalized,
        output_text="[]",
        task_prompt="Add seasonal facts section to existing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )
    assert "Plan mixes inconsistent implementation stacks for one task" not in (
        verdict.reasons
    )


def test_existing_static_site_plan_normalization_simplifies_asset_reference_check(
    tmp_path,
):
    (tmp_path / "css").mkdir()
    (tmp_path / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    (tmp_path / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Add gallery image",
            "commands": [],
            "verification": (
                'python -c "import pathlib,sys; content = '
                "pathlib.Path('index.html').read_text(); sys.exit(0 if "
                '\'<img src=\\"images/tulip-card.svg\\"\' in content else 1)"'
            ),
            "rollback": "true",
            "expected_files": ["index.html"],
            "ops": [
                {
                    "op": "append_file",
                    "path": "index.html",
                    "content": "<img src='images/tulip-card.svg' alt='Tulip'>",
                }
            ],
        }
    ]

    normalized, details = normalize_existing_static_site_plan(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert "tulip-card.svg" in normalized[0]["verification"]
    assert "<img src" not in normalized[0]["verification"]


def test_existing_static_site_plan_normalization_simplifies_css_svg_url_check(
    tmp_path,
):
    (tmp_path / "css").mkdir()
    (tmp_path / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    (tmp_path / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Write stylesheet with background asset",
            "commands": [],
            "verification": (
                "python -c 'import pathlib,sys; sys.exit(0 if "
                "\"background-image: url('../images/flower-bg.svg');\" in "
                'pathlib.Path("css/style.css").read_text() else 1)\''
            ),
            "rollback": "true",
            "expected_files": ["css/style.css"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "css/style.css",
                    "content": (
                        ".hero { background-image: " "url('../images/flower-bg.svg'); }"
                    ),
                }
            ],
        }
    ]

    normalized, details = normalize_existing_static_site_plan(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert "flower-bg.svg" in normalized[0]["verification"]
    assert "background-image" not in normalized[0]["verification"]


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
    assert verdict.details["completion_contract"]["validation_profile"] == (
        "verification"
    )


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
