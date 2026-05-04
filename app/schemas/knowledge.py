"""Knowledge layer Pydantic schemas"""

import enum
from typing import Literal

from pydantic import BaseModel


class KnowledgeType(str, enum.Enum):
    format_guide = "format_guide"
    tool_contract = "tool_contract"
    debug_case = "debug_case"
    best_practice = "best_practice"
    failure_memory = "failure_memory"
    system_doc = "system_doc"
    task_example = "task_example"


class RecommendedAction(str, enum.Enum):
    adjust_format = "adjust_format"
    stop_retry = "stop_retry"
    use_tool_contract = "use_tool_contract"
    review_failure = "review_failure"
    none = "none"


class KnowledgeItemRef(BaseModel):
    id: str
    title: str
    knowledge_type: str
    content: str
    priority: int
    confidence: float


class KnowledgeContext(BaseModel):
    retrieved_items: list[KnowledgeItemRef]
    query: str | None
    trigger_phase: Literal["planning", "validation", "failure"]
    retrieval_reason: str
    confidence: float
    matched_failure_memory: bool
    recommended_action: RecommendedAction
