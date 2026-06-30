from __future__ import annotations

import hashlib

from app.models import KnowledgeItem


def _make_item(
    db_session,
    *,
    title: str,
    content: str = "Knowledge content",
    knowledge_type: str = "format_guide",
    is_active: bool = True,
) -> KnowledgeItem:
    item = KnowledgeItem(
        title=title,
        content=content,
        knowledge_type=knowledge_type,
        is_active=is_active,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def test_knowledge_library_list_includes_retired_items_by_default(
    authenticated_client,
    db_session,
):
    active = _make_item(db_session, title="Active item", is_active=True)
    retired = _make_item(db_session, title="Retired item", is_active=False)

    response = authenticated_client.get("/api/v1/knowledge/items")

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert active.id in ids
    assert retired.id in ids


def test_knowledge_library_list_can_filter_out_retired_items(
    authenticated_client,
    db_session,
):
    active = _make_item(db_session, title="Active item", is_active=True)
    retired = _make_item(db_session, title="Retired item", is_active=False)

    response = authenticated_client.get(
        "/api/v1/knowledge/items", params={"include_retired": False}
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert active.id in ids
    assert retired.id not in ids


def test_knowledge_library_list_supports_title_search(
    authenticated_client,
    db_session,
):
    match = _make_item(db_session, title="Planner repair guide")
    miss = _make_item(db_session, title="Unrelated workflow")

    response = authenticated_client.get(
        "/api/v1/knowledge/items", params={"search": "repair"}
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert match.id in ids
    assert miss.id not in ids


def test_knowledge_library_list_supports_type_filter(
    authenticated_client,
    db_session,
):
    match = _make_item(db_session, title="Debug case", knowledge_type="debug_case")
    miss = _make_item(db_session, title="Format guide", knowledge_type="format_guide")

    response = authenticated_client.get(
        "/api/v1/knowledge/items", params={"knowledge_type": "debug_case"}
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert match.id in ids
    assert miss.id not in ids
