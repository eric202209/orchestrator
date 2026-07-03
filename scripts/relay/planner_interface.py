"""planner_interface.py — backend-agnostic Planner I/O contract.

Even though the current implementation pastes markdown to ChatGPT, the relay
internally models the exchange as structured objects so a future backend
(OpenAI API, Claude API, Orchestrator's own planner) can plug in without
changing the relay's file contract.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class PlannerRequest:
    handoff: str  # full content of HANDOFF_DRAFT.md
    phase: str  # e.g. "17C"
    subtask: Optional[str]  # e.g. "17C-2"
    project: str  # e.g. "orchestrator"


@dataclass
class PlannerResponse:
    prompt: str  # full content to write to NEXT_PROMPT.md
    priority: str  # "normal" | "high" | "blocked"
    estimated_duration: Optional[str]  # e.g. "30m"
    raw: str  # raw text from planner (always preserved)
