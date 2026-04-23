"""Neutral prompt/task schema for backend-specific rendering."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PromptEnvelope:
    """Provider-neutral prompt payload before backend rendering."""

    objective: str
    execution_mode: str = "implementation"
    instructions: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    expected_output: Optional[str] = None
    prompt_body: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
