from app.models import Plan, Project


def test_legacy_planner_parse_requires_auth(api_client):
    response = api_client.post(
        "/api/v1/planner/parse",
        json={"markdown": "## Task List\n- [ ] TASK_START: A | B"},
    )

    assert response.status_code == 401


def test_legacy_planner_parse_rejects_oversized_markdown(authenticated_client):
    response = authenticated_client.post(
        "/api/v1/planner/parse",
        json={"markdown": "x" * 100_001},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Markdown too large"


def test_legacy_planner_plan_update_requires_project_access(
    authenticated_client, db_session
):
    project = Project(id=77, user_id=999, name="Other user project")
    db_session.add(project)
    db_session.flush()
    plan = Plan(
        project_id=project.id,
        title="Plan",
        source_brain="local",
        requirement="Need work",
        markdown="## Task List",
    )
    db_session.add(plan)
    db_session.commit()

    response = authenticated_client.put(
        f"/api/v1/projects/{project.id}/plans/{plan.id}",
        json={"title": "stolen"},
    )

    assert response.status_code == 404
    db_session.refresh(plan)
    assert plan.title == "Plan"
