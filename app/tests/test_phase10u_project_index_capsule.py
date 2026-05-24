"""Phase 10U: ProjectIndex prompt capsule tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.phases.planning_flow import (
    _read_only_stage_fallback_plan,
    _static_site_validation_fallback_plan,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.repair_prompts import (
    PLANNING_REPAIR_PROMPT_MAX_CHARS,
)
from app.services.prompt_templates import PromptTemplates
from app.services.project.index_service import (
    PROJECT_STRUCTURE_CAPSULE_MAX_CHARS,
    build_project_index,
    render_project_structure_capsule,
)


def test_project_structure_capsule_has_deterministic_read_only_format(tmp_path):
    (tmp_path / "src" / "ledger_app").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "ledger_app" / "__init__.py").write_text("")
    (tmp_path / "src" / "ledger_app" / "calculator.py").write_text(
        "SECRET_IMPL = True\n"
    )
    (tmp_path / "tests" / "test_calculator.py").write_text("SECRET_TEST = True\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='ledger'\n")

    capsule = render_project_structure_capsule(build_project_index(tmp_path))

    assert capsule.startswith("PROJECT STRUCTURE CAPSULE")
    assert "Use these paths as workspace facts." in capsule
    assert "Do not create files outside this structure" in capsule
    assert "Source files:\n" in capsule
    assert "- src/ledger_app/calculator.py" in capsule
    assert "Tests:\n- tests/test_calculator.py" in capsule
    assert "Entry points:\n- pyproject.toml" in capsule
    assert "Package roots:\n- src/ledger_app" in capsule
    assert "Ignored:\n" in capsule
    assert "SECRET_IMPL" not in capsule
    assert "SECRET_TEST" not in capsule
    assert len(capsule) <= PROJECT_STRUCTURE_CAPSULE_MAX_CHARS


def test_project_structure_capsule_truncates_with_counts(tmp_path):
    (tmp_path / "src").mkdir()
    for idx in range(8):
        (tmp_path / "src" / f"module_{idx}.py").write_text("")

    capsule = render_project_structure_capsule(
        build_project_index(tmp_path),
        max_source_files=3,
        max_test_files=2,
        max_entry_points=2,
        max_package_roots=2,
        max_ignored_dirs=2,
        max_chars=700,
    )

    assert "- src/module_0.py" in capsule
    assert "- src/module_2.py" in capsule
    assert "- src/module_3.py" not in capsule
    assert "- ... 5 more source files omitted" in capsule
    assert len(capsule) <= 700


def test_planning_prompt_includes_capsule_as_context_not_static_site_rewrite(tmp_path):
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "package.json").write_text('{"scripts":{"build":"vite"}}')
    (tmp_path / "frontend" / "src" / "App.tsx").write_text("export default null;\n")
    capsule = render_project_structure_capsule(build_project_index(tmp_path))

    prompt = PromptTemplates.build_planning_prompt(
        task_description="Update the existing React component. Do not create a static site.",
        project_context="Existing Vite frontend.",
        project_dir=str(tmp_path),
        project_structure_capsule=capsule,
    )

    assert "PROJECT STRUCTURE CAPSULE" in prompt
    assert "- frontend/src/App.tsx" in prompt
    assert "Use these paths as workspace facts." in prompt
    assert "`replace_in_file` is only for exact old text" in prompt
    assert "src/index.js -> index.html" not in prompt
    assert "frontend/src/frontend/src" not in prompt


def test_minimal_planning_prompt_uses_capped_capsule(tmp_path):
    (tmp_path / "src").mkdir()
    for idx in range(100):
        (tmp_path / "src" / f"module_{idx}.py").write_text("")

    prompt = PlannerService.build_minimal_planning_prompt(
        "Fix one existing Python module.",
        tmp_path,
    )

    assert "PROJECT STRUCTURE CAPSULE" in prompt
    assert "... " in prompt
    assert "`replace_in_file` is only for exact old text" in prompt
    assert len(prompt) < 12000


def test_repair_prompt_includes_capsule_without_exceeding_budget(tmp_path):
    (tmp_path / "src" / "ledger_app").mkdir(parents=True)
    (tmp_path / "src" / "ledger_app" / "__init__.py").write_text("")
    (tmp_path / "src" / "ledger_app" / "summary.py").write_text(
        "def summarize_entries(entries):\n    return {}\n"
    )
    for idx in range(50):
        (tmp_path / "src" / "ledger_app" / f"module_{idx}.py").write_text("")

    prompt = PlannerService.build_planning_repair_prompt(
        task_description="Fix the existing ledger summary function.",
        malformed_output='[{"ops":[{"op":"replace_in_file","path":"summary.py"}]}]',
        project_dir=tmp_path,
        rejection_reasons=[
            "stale_replace_ops_steps: old text not found; use current file evidence"
        ],
        knowledge_context=SimpleNamespace(retrieved_items=[]),
    )

    assert "PROJECT STRUCTURE CAPSULE" in prompt
    assert "- src/ledger_app/summary.py" in prompt
    assert "Use these paths as workspace facts." in prompt
    assert "`replace_in_file` is only for exact old text" in prompt
    assert "Do not invent helper variables" in prompt
    assert len(prompt) <= PLANNING_REPAIR_PROMPT_MAX_CHARS


def _make_medium_ledger_workspace(tmp_path):
    (tmp_path / "src" / "ledger_app").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "src" / "ledger_app" / "__init__.py").write_text("")
    (tmp_path / "src" / "ledger_app" / "calculator.py").write_text(
        "def calculate_totals(entries):\n    return {}\n"
    )
    (tmp_path / "tests" / "test_calculator.py").write_text(
        "def test_totals():\n    pass\n"
    )
    (tmp_path / "frontend" / "src" / "App.tsx").write_text("export default null;\n")
    (tmp_path / "frontend" / "package.json").write_text('{"scripts":{"build":"vite"}}')
    (tmp_path / "pyproject.toml").write_text("[project]\nname='ledger'\n")


def test_review_lane_fallback_plan_fires_for_medium_project(tmp_path):
    """Review stage always produces a safe read-only inspection plan."""
    _make_medium_ledger_workspace(tmp_path)

    state = SimpleNamespace(project_dir=str(tmp_path))
    ctx = SimpleNamespace(
        workflow_stage="review",
        prompt="Review the ledger backend for correctness.",
        orchestration_state=state,
    )

    fallback = _read_only_stage_fallback_plan(ctx)

    assert fallback is not None
    assert len(fallback) == 1
    step = fallback[0]
    assert step["expected_files"] == []
    assert not step.get("ops")
    assert "pathlib" in step["commands"][0]
    assert "rglob" in step["commands"][0]
    assert "inspect workspace" in step["description"].lower()


def test_validation_lane_no_static_site_fallback_for_python_ledger_project(tmp_path):
    """Validation lane on a Python/ledger project must not trigger the static-site fallback."""
    _make_medium_ledger_workspace(tmp_path)

    state = SimpleNamespace(project_dir=str(tmp_path))
    ctx = SimpleNamespace(
        workflow_stage="validate",
        prompt="Validate that the ledger calculator produces correct totals.",
        orchestration_state=state,
    )

    # No public/ dir → static-site fallback must not fire
    fallback = _static_site_validation_fallback_plan(ctx)

    assert fallback is None


def test_medium_project_capsule_covers_backend_and_frontend_paths(tmp_path):
    """Capsule for a medium fullstack ledger project includes both backend and frontend paths."""
    _make_medium_ledger_workspace(tmp_path)

    capsule = render_project_structure_capsule(build_project_index(tmp_path))

    # Backend paths present
    assert "src/ledger_app/calculator.py" in capsule
    assert "tests/test_calculator.py" in capsule
    assert "pyproject.toml" in capsule
    assert "src/ledger_app" in capsule
    # Frontend paths present
    assert "frontend/src/App.tsx" in capsule
    assert "frontend/package.json" in capsule
    # Read-only wording retained
    assert "Use these paths as workspace facts." in capsule
    assert "Do not create files outside this structure" in capsule
    # No static-site rewrite hint
    assert "src/index.js" not in capsule
    assert "index.html" not in capsule
