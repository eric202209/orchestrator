from types import SimpleNamespace

from app.services.orchestration.planning.normalization import (
    complete_repaired_plan_contract,
    normalize_existing_static_site_plan,
)
from app.services.orchestration.phases.planning_flow import (
    _looks_like_verification_only_task,
    _prune_unmaterialized_expected_files,
    _read_only_stage_fallback_plan,
    _static_site_validation_fallback_plan,
    _split_repaired_single_step_full_lifecycle_plan,
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


def test_contract_completion_does_not_generate_content_from_expected_files_only():
    plan = [
        {
            "step_number": 1,
            "description": "Declare static files without materializing content",
            "commands": ["test -f index.html || true"],
            "verification": None,
            "expected_files": ["index.html", "css/style.css", "images/site.svg"],
            "ops": [],
        }
    ]

    completed, details = complete_repaired_plan_contract(
        plan,
        task_prompt="Create a plain static site with index.html, CSS, and SVG",
        repaired=True,
    )

    assert completed == plan
    assert details["changed"] is False
    assert details["added_expected_files"] == []
    assert not any(
        op.get("op") == "write_file"
        for step in completed
        for op in (step.get("ops") or [])
    )


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


def test_existing_nested_static_site_verification_uses_existing_html_svg_link(
    tmp_path,
):
    site_root = tmp_path / "public" / "status-site"
    (site_root / "css").mkdir(parents=True)
    (site_root / "images").mkdir()
    (site_root / "index.html").write_text(
        "<html><head><link rel='stylesheet' href='css/style.css'></head>"
        "<body><img src='images/status-badge.svg' alt='Status Badge'></body></html>",
        encoding="utf-8",
    )
    (site_root / "css" / "style.css").write_text("body {}", encoding="utf-8")
    (site_root / "images" / "status-badge.svg").write_text(
        "<svg></svg>",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Add incident cards and preserve SVG reference",
            "commands": [],
            "verification": (
                "grep 'background-image: url(../status-badge.svg)' "
                "public/status-site/css/style.css"
            ),
            "rollback": "true",
            "expected_files": [
                "public/status-site/index.html",
                "public/status-site/css/style.css",
            ],
            "ops": [
                {
                    "op": "append_file",
                    "path": "public/status-site/index.html",
                    "content": "<section><h2>Incident Summary</h2></section>",
                }
            ],
        }
    ]

    normalized, details = normalize_existing_static_site_plan(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert "public/status-site/index.html" in normalized[0]["verification"]
    assert "status-badge.svg" in normalized[0]["verification"]
    assert "background-image" not in normalized[0]["verification"]


def test_existing_nested_static_site_normalization_rewrites_root_file_drift(
    tmp_path,
):
    site_root = tmp_path / "public" / "status-site"
    (site_root / "css").mkdir(parents=True)
    (site_root / "index.html").write_text(
        "<main id='main-content'></main>", encoding="utf-8"
    )
    (site_root / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Improve existing status site accessibility",
            "commands": [],
            "verification": (
                'python -c "from pathlib import Path; '
                "assert 'main-content' in Path('index.html').read_text(); "
                "assert 'body' in Path('style.css').read_text()\""
            ),
            "rollback": "true",
            "expected_files": ["index.html", "style.css"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "index.html",
                    "old": "<main id='main-content'></main>",
                    "new": "<a href='#main-content'>Skip to main content</a>"
                    "<main id='main-content'></main>",
                },
                {
                    "op": "append_file",
                    "path": "style.css",
                    "content": "@media (max-width: 600px) { body { margin: 0; } }",
                },
            ],
        }
    ]

    normalized, details = normalize_existing_static_site_plan(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert normalized[0]["expected_files"] == [
        "public/status-site/index.html",
        "public/status-site/css/style.css",
    ]
    assert [op["path"] for op in normalized[0]["ops"]] == [
        "public/status-site/index.html",
        "public/status-site/css/style.css",
    ]
    assert "Path('public/status-site/index.html')" in normalized[0]["verification"]
    assert "Path('public/status-site/css/style.css')" in normalized[0]["verification"]


def test_existing_static_site_normalization_appends_html_fragments_instead_of_overwriting(
    tmp_path,
):
    site_root = tmp_path / "public" / "status-site"
    (site_root / "css").mkdir(parents=True)
    (site_root / "index.html").write_text(
        "<!doctype html><html><head><link rel='stylesheet' href='css/style.css'>"
        "</head><body><img src='images/status-badge.svg' alt='Status'></body></html>",
        encoding="utf-8",
    )
    (site_root / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Add incident summary cards",
            "commands": [],
            "verification": "python -c \"import pathlib; print(pathlib.Path('public/status-site/index.html').exists())\"",
            "rollback": "true",
            "expected_files": ["public/status-site/index.html"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "public/status-site/index.html",
                    "content": "<div class='incident-summary'>API Queue Knowledge</div>",
                }
            ],
        }
    ]

    normalized, details = normalize_existing_static_site_plan(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert normalized[0]["ops"][0]["op"] == "append_file"
    assert normalized[0]["ops"][0]["path"] == "public/status-site/index.html"


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


def test_static_site_contract_completion_rewrites_quoted_html_link_verification():
    brittle_verification = (
        'python -c "import pathlib,sys; content = '
        "pathlib.Path('public/status-site/index.html').read_text(); "
        'sys.exit(0 if \'<link rel="stylesheet" href="css/style.css">\' '
        'in content and \'<img src="images/status-badge.svg" '
        'alt="Status Badge">\' in content else 1)"'
    )
    plan = [
        {
            "step_number": 1,
            "description": "Create base static site files",
            "commands": [],
            "verification": brittle_verification,
            "rollback": "true",
            "expected_files": [
                "public/status-site/index.html",
                "public/status-site/css/style.css",
                "public/status-site/images/status-badge.svg",
            ],
            "ops": [
                {
                    "op": "write_file",
                    "path": "public/status-site/index.html",
                    "content": (
                        '<link rel="stylesheet" href="css/style.css">'
                        '<img src="images/status-badge.svg" alt="Status Badge">'
                    ),
                },
                {
                    "op": "write_file",
                    "path": "public/status-site/css/style.css",
                    "content": "body {}",
                },
                {
                    "op": "write_file",
                    "path": "public/status-site/images/status-badge.svg",
                    "content": "<svg></svg>",
                },
            ],
        }
    ]

    completed, details = complete_repaired_plan_contract(
        plan,
        task_prompt="Create a plain static site with HTML, CSS, and SVG",
    )

    assert details["changed"] is True
    assert completed[0]["verification"].startswith("python -c ")
    assert '\\"stylesheet\\"' not in completed[0]["verification"]
    assert "css/style.css" in completed[0]["verification"]
    assert "images/status-badge.svg" in completed[0]["verification"]


def test_static_site_contract_completion_rewrites_final_link_verification_step():
    brittle_verification = (
        'python -c "import pathlib,sys; content = '
        "pathlib.Path('public/status-site/index.html').read_text(); "
        'sys.exit(0 if \'<link rel="stylesheet" href="css/style.css">\' '
        'in content and \'<img src="images/status-badge.svg" '
        'alt="Status Badge">\' in content else 1)"'
    )
    plan = [
        {
            "step_number": 1,
            "description": "Create base static site files",
            "commands": [],
            "verification": None,
            "rollback": "true",
            "expected_files": [
                "public/status-site/index.html",
                "public/status-site/css/style.css",
                "public/status-site/images/status-badge.svg",
            ],
            "ops": [
                {
                    "op": "write_file",
                    "path": "public/status-site/index.html",
                    "content": (
                        '<link rel="stylesheet" href="css/style.css">'
                        '<img src="images/status-badge.svg" alt="Status Badge">'
                    ),
                },
                {
                    "op": "write_file",
                    "path": "public/status-site/css/style.css",
                    "content": "body {}",
                },
                {
                    "op": "write_file",
                    "path": "public/status-site/images/status-badge.svg",
                    "content": "<svg></svg>",
                },
            ],
        },
        {
            "step_number": 2,
            "description": "Verify links",
            "commands": [brittle_verification],
            "verification": brittle_verification,
            "rollback": "true",
            "expected_files": [],
            "ops": [],
        },
    ]

    completed, details = complete_repaired_plan_contract(
        plan,
        task_prompt="Create a plain static site with HTML, CSS, and SVG",
    )

    assert details["changed"] is True
    assert completed[1]["verification"].startswith("python -c ")
    assert completed[1]["commands"] == [completed[1]["verification"]]
    assert '\\"stylesheet\\"' not in completed[1]["verification"]
    assert "css/style.css" in completed[1]["verification"]
    assert "images/status-badge.svg" in completed[1]["verification"]


def test_static_site_contract_completion_rewrites_html_selector_content_check():
    brittle_verification = (
        "python -c 'import pathlib,sys; content = "
        'pathlib.Path("public/status-site/index.html").read_text(); '
        'sys.exit(0 if ".incident-summary" in content and ".status-card" '
        'in content and "API" in content and "Queue" in content and '
        '"Knowledge" in content else 1)\''
    )
    plan = [
        {
            "step_number": 1,
            "description": "Add incident summary cards",
            "commands": [brittle_verification],
            "verification": brittle_verification,
            "rollback": "true",
            "expected_files": ["public/status-site/index.html"],
            "ops": [
                {
                    "op": "append_file",
                    "path": "public/status-site/index.html",
                    "content": (
                        '<section class="incident-summary">'
                        '<article class="status-card">API</article>'
                        '<article class="status-card">Queue</article>'
                        '<article class="status-card">Knowledge</article>'
                        "</section>"
                    ),
                }
            ],
        }
    ]

    completed, details = complete_repaired_plan_contract(
        plan,
        task_prompt="Add incident summary cards to the static status site",
    )

    assert details["changed"] is True
    assert completed[0]["verification"].startswith("python -c ")
    assert ".incident-summary" not in completed[0]["verification"]
    assert ".status-card" not in completed[0]["verification"]
    assert "incident-summary" in completed[0]["verification"]
    assert "status-card" in completed[0]["verification"]
    assert completed[0]["commands"] == [completed[0]["verification"]]


def test_static_site_contract_completion_does_not_attach_whole_site_check_to_partial_step():
    plan = [
        {
            "step_number": 1,
            "description": "Create the SVG asset first",
            "commands": [],
            "verification": None,
            "rollback": "rm -rf public/status-site",
            "expected_files": ["public/status-site/images/status-badge.svg"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "public/status-site/images/status-badge.svg",
                    "content": "<svg></svg>",
                }
            ],
        },
        {
            "step_number": 2,
            "description": "Verify the completed site after later steps",
            "commands": [],
            "verification": None,
            "rollback": "true",
            "expected_files": [
                "public/status-site/index.html",
                "public/status-site/css/style.css",
                "public/status-site/images/status-badge.svg",
            ],
            "ops": [],
        },
    ]

    completed, details = complete_repaired_plan_contract(
        plan,
        task_prompt="Create a plain static site with HTML, CSS, and SVG",
    )

    assert details["changed"] is True
    assert "public/status-site/images/status-badge.svg" in completed[0]["verification"]
    assert "public/status-site/index.html" not in completed[0]["verification"]
    assert "public/status-site/css/style.css" not in completed[0]["verification"]
    assert "public/status-site/index.html" in completed[1]["verification"]
    assert "public/status-site/css/style.css" in completed[1]["verification"]


def test_static_site_contract_completion_neutralizes_malformed_typed_op_rollback():
    plan = [
        {
            "step_number": 1,
            "description": "Append incident summary section",
            "commands": [],
            "verification": "python -c \"import pathlib,sys; sys.exit(0 if 'API' in pathlib.Path('public/status-site/index.html').read_text() else 1)\"",
            "rollback": "sed -i '/<section id=\\'incident-summary\\'>/,/<\\/section>/d' public/status-site/index.html",
            "expected_files": ["public/status-site/index.html"],
            "ops": [
                {
                    "op": "append_file",
                    "path": "public/status-site/index.html",
                    "content": "<section id='incident-summary'>API Queue Knowledge</section>",
                }
            ],
        }
    ]

    completed, details = complete_repaired_plan_contract(
        plan,
        task_prompt="Update the existing public/status-site static site",
    )

    assert details["changed"] is True
    assert completed[0]["rollback"] == "true"
    assert completed[0]["ops"][0]["op"] == "append_file"


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


def test_validate_stage_static_site_fallback_checks_requested_content(tmp_path):
    site_root = tmp_path / "public" / "status-site"
    (site_root / "css").mkdir(parents=True)
    (site_root / "images").mkdir()
    (site_root / "index.html").write_text(
        "<link rel='stylesheet' href='css/style.css'>"
        "<img src='images/status-badge.svg' alt='Status Badge'>"
        "<a class='skip-link' href='#main'>Skip</a>"
        "API Queue Knowledge",
        encoding="utf-8",
    )
    (site_root / "css" / "style.css").write_text("body {}", encoding="utf-8")
    (site_root / "images" / "status-badge.svg").write_text(
        "<svg></svg>", encoding="utf-8"
    )
    ctx = SimpleNamespace(
        workflow_stage="validate",
        prompt=(
            "Validate the final public/status-site. Check that index.html, "
            "css/style.css, and images/status-badge.svg exist, that index.html "
            "links css/style.css, references images/status-badge.svg, and contains "
            "API, Queue, Knowledge, skip link, and alt text."
        ),
        orchestration_state=SimpleNamespace(project_dir=tmp_path),
    )

    plan = _static_site_validation_fallback_plan(ctx)

    assert plan is not None
    command = plan[0]["verification"]
    assert "public/status-site/index.html" in command
    assert "API" in command
    assert "Queue" in command
    assert "Knowledge" in command
    assert "skip" in command
    assert "alt=" in command
    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt=ctx.prompt,
        execution_profile="test_only",
        workflow_stage="validate",
        project_dir=tmp_path,
    )
    assert verdict.accepted


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
