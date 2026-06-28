"""Shared pagination abstractions.

Every paginated endpoint uses Page[T] for the response envelope
and paginate() to execute the bounded SQL query.
"""

from __future__ import annotations

import math
from typing import Any, Generic, List, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

MAX_PER_PAGE = 200


class QueryOptions(BaseModel):
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=25, ge=1, le=MAX_PER_PAGE)
    order_by: str = "created_at"
    order_dir: str = "desc"


class Page(BaseModel, Generic[T]):
    items: List[T]
    page: int
    per_page: int
    total: int
    total_pages: int
    has_next: bool
    has_previous: bool


def paginate(query: Any, page: int, per_page: int) -> dict:
    """Execute a paginated SQLAlchemy query and return a dict matching Page[T].

    The caller is responsible for ordering the query before passing it here.
    """
    total: int = query.count()
    offset = (page - 1) * per_page
    items = query.offset(offset).limit(per_page).all()
    total_pages = max(1, math.ceil(total / per_page)) if total > 0 else 1
    return {
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1,
    }
