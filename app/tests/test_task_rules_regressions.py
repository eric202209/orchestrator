from app.services.orchestration.task_rules import (
    get_workflow_profile,
    should_force_review_execution_profile,
)


def test_force_review_profile_for_true_inspection_task():
    assert (
        should_force_review_execution_profile(
            "full_lifecycle",
            "Inspect current project architecture and inventory extension points.",
            "Inspect current project architecture",
            "Review the real files before implementation.",
        )
        is True
    )


def test_do_not_force_review_profile_for_build_task_with_clean_architecture():
    assert (
        should_force_review_execution_profile(
            "full_lifecycle",
            "Set up frontend (React or Vite) and backend (Node.js or FastAPI) with clean architecture.",
            "SkillSync AI Hiring Platform",
            "Set up frontend (React or Vite) and backend (Node.js or FastAPI) with clean architecture.",
        )
        is False
    )


def test_fullstack_scaffold_task_resolves_workflow_profile():
    assert (
        get_workflow_profile(
            "full_lifecycle",
            "SkillSync AI Hiring Platform",
            "Set up frontend (React or Vite) and backend (FastAPI) with clean architecture.",
        )
        == "fullstack_scaffold"
    )


def test_backend_api_task_with_negated_frontend_resolves_backend_only():
    assert (
        get_workflow_profile(
            "full_lifecycle",
            "Tiny FastAPI notes API",
            "Build a FastAPI notes API. Do not create a frontend or package manager setup.",
        )
        == "backend_only"
    )


def test_static_frontend_task_with_negated_backend_resolves_frontend_only():
    assert (
        get_workflow_profile(
            "full_lifecycle",
            "Static productivity timer landing page",
            "Build a static frontend landing page. Do not create a backend.",
        )
        == "frontend_only"
    )
