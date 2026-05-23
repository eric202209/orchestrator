"""Knowledge runtime readiness diagnostics."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict

from openai import OpenAI
from qdrant_client import QdrantClient
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import BASE_DIR, settings
from app.models import KnowledgeItem


def _count_candidate_knowledge_files(root: Path) -> int:
    knowledge_dir = root / "knowledge"
    if not knowledge_dir.exists():
        return 0
    return sum(
        1
        for path in knowledge_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".json"}
    )


def _qdrant_status() -> Dict[str, Any]:
    try:
        client = QdrantClient(url=settings.QDRANT_URL, check_compatibility=False)
        collections = [item.name for item in client.get_collections().collections]
        collection_exists = settings.QDRANT_COLLECTION_NAME in collections
        point_count = None
        if collection_exists:
            point_count = int(
                client.count(collection_name=settings.QDRANT_COLLECTION_NAME).count or 0
            )
        return {
            "status": "ready" if collection_exists else "missing_collection",
            "url": settings.QDRANT_URL,
            "collection": settings.QDRANT_COLLECTION_NAME,
            "collection_exists": collection_exists,
            "point_count": point_count,
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "url": settings.QDRANT_URL,
            "collection": settings.QDRANT_COLLECTION_NAME,
            "collection_exists": False,
            "point_count": None,
            "error": str(exc),
        }


def _embedding_status() -> Dict[str, Any]:
    provider = settings.EMBEDDING_PROVIDER.strip().lower()
    if provider == "auto":
        provider = "openai" if settings.OPENAI_API_KEY.strip() else "ollama"
    model = (
        settings.OLLAMA_EMBEDDING_MODEL
        if provider == "ollama"
        else settings.OPENAI_EMBEDDING_MODEL
    )
    base_url = (
        settings.OLLAMA_BASE_URL.rstrip("/") + "/v1" if provider == "ollama" else None
    )

    try:
        client = (
            OpenAI(api_key="ollama", base_url=base_url, max_retries=0, timeout=3.0)
            if provider == "ollama"
            else OpenAI(
                api_key=settings.OPENAI_API_KEY or "no-key",
                max_retries=0,
                timeout=3.0,
            )
        )
        response = client.embeddings.create(
            model=model,
            input="knowledge readiness probe",
        )
        dimension = len(response.data[0].embedding)
        return {
            "status": "ready",
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "dimension": dimension,
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "dimension": None,
            "error": str(exc),
        }


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("ORCHESTRATOR_IN_DOCKER") in {
        "1",
        "true",
        "yes",
    }


def _recommended_ingest_command() -> str:
    if _running_in_container():
        return (
            "docker compose -f docker-compose.windows.yml exec -T orchestrator "
            "python scripts/ingest_knowledge.py --source-dir /app "
            "--qdrant-url http://qdrant:6333"
        )
    return (
        "venv/bin/python scripts/ingest_knowledge.py --source-dir . "
        f"--qdrant-url {settings.QDRANT_URL}"
    )


def knowledge_readiness_snapshot(
    db: Session, *, probe_embedding: bool = True
) -> Dict[str, Any]:
    """Return operator-facing readiness for the active knowledge runtime."""

    candidate_files = _count_candidate_knowledge_files(BASE_DIR)
    sqlite_count = int(
        db.query(func.count(KnowledgeItem.id))
        .filter(KnowledgeItem.is_active.is_(True))
        .scalar()
        or 0
    )
    last_updated = db.query(
        func.max(func.coalesce(KnowledgeItem.updated_at, KnowledgeItem.created_at))
    ).scalar()
    qdrant = _qdrant_status()
    embedding = _embedding_status() if probe_embedding else {"status": "not_probed"}

    warnings: list[str] = []
    qdrant_points = qdrant.get("point_count")
    if candidate_files > 0 and sqlite_count == 0:
        warnings.append("knowledge_files_exist_but_sqlite_empty")
    if candidate_files > 0 and (qdrant_points in {0, None}):
        warnings.append("knowledge_files_exist_but_qdrant_empty")
    if sqlite_count > 0 and qdrant_points == 0:
        warnings.append("sqlite_has_items_but_qdrant_empty")
    if embedding.get("status") not in {"ready", "not_probed"}:
        warnings.append("embedding_probe_unavailable")
    if qdrant.get("status") != "ready":
        warnings.append("qdrant_not_ready")

    status = "ready"
    if warnings:
        status = "warning"
    if "qdrant_not_ready" in warnings and sqlite_count == 0:
        status = "unavailable"

    return {
        "status": status,
        "checked_at": datetime.now(UTC).isoformat(),
        "knowledge_dir": str((BASE_DIR / "knowledge").resolve()),
        "candidate_file_count": candidate_files,
        "sqlite_item_count": sqlite_count,
        "qdrant": qdrant,
        "embedding": embedding,
        "last_ingest_at": last_updated.isoformat() if last_updated else None,
        "warnings": warnings,
        "recommended_ingest_command": _recommended_ingest_command(),
    }
