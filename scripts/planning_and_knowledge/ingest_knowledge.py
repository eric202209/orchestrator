#!/usr/bin/env python3
"""Ingest knowledge documents into SQLite + Qdrant.

Scans docs/, CONTEXT.md, and knowledge/ for markdown/JSON files with
frontmatter metadata. Idempotent: skips files whose content hasn't changed.

Usage:
    python scripts/ingest_knowledge.py
    python scripts/ingest_knowledge.py --source-dir /path/to/repo
    python scripts/ingest_knowledge.py --qdrant-url :memory:
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import warnings
from pathlib import Path
from typing import Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import settings
from app.database import SessionLocal
from app.models import KnowledgeItem
from app.services.knowledge.knowledge_service import KnowledgeService


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def _parse_markdown(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) from a markdown string.

    Returns ({}, text) when no frontmatter block is present.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_block = text[3:end].strip()
    body = text[end + 4 :].strip()
    try:
        fm = yaml.safe_load(fm_block) or {}
    except yaml.YAMLError as exc:
        warnings.warn(f"YAML parse error in frontmatter: {exc}")
        fm = {}
    return fm, body


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def _collect_files(source_dir: Path) -> list[Path]:
    files: list[Path] = []

    # CONTEXT.md at repo root
    ctx = source_dir / "CONTEXT.md"
    if ctx.exists():
        files.append(ctx)

    # All markdown files under docs/
    docs_dir = source_dir / "docs"
    if docs_dir.exists():
        files.extend(sorted(docs_dir.rglob("*.md")))

    # knowledge/ directory — markdown + JSON
    knowledge_dir = source_dir / "knowledge"
    knowledge_dir.mkdir(exist_ok=True)
    for suffix in ("*.md", "*.json"):
        files.extend(sorted(knowledge_dir.rglob(suffix)))

    return files


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------


def _process_markdown(path: Path, source_dir: Path) -> Optional[dict]:
    text = path.read_text(encoding="utf-8")
    fm, body = _parse_markdown(text)

    knowledge_type = fm.get("type")
    if not knowledge_type:
        warnings.warn(
            f"SKIP {path.relative_to(source_dir)}: missing 'type' in frontmatter"
        )
        return None

    applies_to = fm.get("applies_to")
    title = fm.get("title")
    if not applies_to or not title:
        warnings.warn(
            f"SKIP {path.relative_to(source_dir)}: missing required frontmatter field(s) "
            f"({'applies_to' if not applies_to else 'title'})"
        )
        return None

    if isinstance(applies_to, str):
        applies_to = [applies_to]

    return {
        "title": title,
        "content": body or text,
        "source_path": str(path.relative_to(source_dir)),
        "knowledge_type": knowledge_type,
        "tags": fm.get("tags") or [],
        "project_scope": fm.get("project_scope"),
        "applies_to": applies_to,
        "failure_signature": fm.get("failure_signature"),
        "tool_name": fm.get("tool_name"),
        "priority": int(fm.get("priority", 0)),
    }


def _process_json(path: Path, source_dir: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        warnings.warn(f"SKIP {path.relative_to(source_dir)}: JSON parse error: {exc}")
        return None

    required = ("title", "content", "knowledge_type", "applies_to")
    missing = [f for f in required if not data.get(f)]
    if missing:
        warnings.warn(
            f"SKIP {path.relative_to(source_dir)}: missing required field(s): {missing}"
        )
        return None

    data.setdefault("source_path", str(path.relative_to(source_dir)))
    data.setdefault("tags", [])
    data.setdefault("priority", 0)
    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(source_dir: Path, db_url: str, qdrant_url: str) -> None:
    svc = KnowledgeService(
        qdrant_url=qdrant_url,
        collection_name=settings.QDRANT_COLLECTION_NAME,
    )
    db = SessionLocal()

    files = _collect_files(source_dir)
    ingested = skipped = errors = 0

    try:
        for path in files:
            rel = path.relative_to(source_dir)
            try:
                if path.suffix == ".json":
                    data = _process_json(path, source_dir)
                else:
                    data = _process_markdown(path, source_dir)

                if data is None:
                    skipped += 1
                    continue

                content = data["content"]
                checksum = _sha256(content)
                source_path = data["source_path"]

                existing = (
                    db.query(KnowledgeItem)
                    .filter(
                        KnowledgeItem.source_path == source_path,
                        KnowledgeItem.checksum == checksum,
                    )
                    .first()
                )
                if existing:
                    print(f"  unchanged  {rel}")
                    skipped += 1
                    continue

                # New or changed — upsert SQLite record
                stale = (
                    db.query(KnowledgeItem)
                    .filter(KnowledgeItem.source_path == source_path)
                    .first()
                )
                if stale:
                    # Update in place
                    for k, v in data.items():
                        setattr(stale, k, v)
                    stale.checksum = checksum
                    stale.version = (stale.version or 1) + 1
                    item = stale
                else:
                    item = KnowledgeItem(**data, checksum=checksum)
                    db.add(item)

                db.commit()
                try:
                    svc.ingest(item)
                except Exception as embed_exc:
                    warnings.warn(f"WARN {rel}: vector embedding skipped: {embed_exc}")
                print(f"  ingested   {rel}")
                ingested += 1

            except Exception as exc:
                db.rollback()
                warnings.warn(f"ERROR {rel}: {exc}")
                errors += 1
    finally:
        db.close()

    print(f"\nDone. ingested={ingested} skipped={skipped} errors={errors}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest knowledge documents")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=REPO_ROOT,
        help="Repo root to scan (default: repo root)",
    )
    parser.add_argument(
        "--db-url",
        default=settings.DATABASE_URL,
        help="SQLAlchemy DB URL (default: from config)",
    )
    parser.add_argument(
        "--qdrant-url",
        default=settings.QDRANT_URL,
        help="Qdrant URL or ':memory:' (default: from config)",
    )
    args = parser.parse_args()
    run(source_dir=args.source_dir, db_url=args.db_url, qdrant_url=args.qdrant_url)


if __name__ == "__main__":
    main()
