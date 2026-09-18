"""Definitive provider-capable lane inventory for one governed research run.

Derived from the current code at the RR1 baseline, not from the RER-01O
Planning-repair capture assumption.  Two audited facts close the inventory:

1. Every provider invocation leaves orchestration through one of the adapter
   methods in :data:`PROVIDER_ENTRY_METHODS` on one of the adapter classes in
   :data:`PROVIDER_CAPABLE_RUNTIMES`.  Instrumenting there -- rather than at
   the call sites -- means a lane nobody predicted is still captured.
2. Each call site supplies a ``session_prefix`` that reaches the adapter.  The
   prefixes below are the complete set produced by non-test call sites at this
   baseline, verified by enumerating every ``session_prefix`` assignment.

``execute_task`` on ``OpenAIResponsesRuntime`` delegates to ``invoke_prompt``.
Capture therefore records only the outermost boundary entry per invocation so
one provider request counts once; the nested entry is retained as evidence.

A lane that is not listed still produces a capture record attributed to
:data:`LANE_UNATTRIBUTED`.  That is an evidence-integrity signal, never a
silent zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LANE_UNATTRIBUTED = "unattributed"


@dataclass(frozen=True)
class ProviderLane:
    """One provider-capable lane that may execute during a research run."""

    lane_id: str
    session_prefix: str
    role: str | None
    entry_point: str
    description: str
    provider_capable: bool = True

    def as_evidence(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "session_prefix": self.session_prefix,
            "role": self.role,
            "entry_point": self.entry_point,
            "description": self.description,
            "provider_capable": self.provider_capable,
        }


PROVIDER_LANES: tuple[ProviderLane, ...] = (
    ProviderLane(
        lane_id="planning_initial",
        session_prefix="planning",
        role="planning",
        entry_point="app.services.planning.planning_session_service:1488",
        description="Initial Planning generation for a task attempt.",
    ),
    ProviderLane(
        lane_id="planning_brief",
        session_prefix="planning-brief",
        role="planning",
        entry_point="app.services.planning.providers.openclaw:104",
        description="OpenClaw Planning provider, planning-brief artifact.",
    ),
    ProviderLane(
        lane_id="structured_task_plan",
        session_prefix="structured-task-plan",
        role="planning",
        entry_point="app.services.planning.providers.openclaw:104",
        description="OpenClaw Planning provider, structured-task-plan artifact.",
    ),
    ProviderLane(
        lane_id="grounding",
        session_prefix="grounding",
        role="planning",
        entry_point="app.services.planning.providers.openclaw:104",
        description=(
            "Grounding artifact provider request; also the read-only discovery "
            "route via planner._execute_task_with_planning_lock."
        ),
    ),
    ProviderLane(
        lane_id="planning_repair",
        session_prefix="planning-repair",
        role="repair",
        entry_point="app.services.orchestration.planning.planner:1858,1912,1935",
        description="Planning output-contract repair; the RER-01O capture lane.",
    ),
    ProviderLane(
        lane_id="read_only_discovery",
        session_prefix="planning",
        role="planning",
        entry_point="app.services.orchestration.planning.read_only_discovery:418",
        description=(
            "Bounded read-only discovery; reaches the adapter through "
            "planner._execute_task_with_planning_lock -> execute_task."
        ),
    ),
    ProviderLane(
        lane_id="execution_step",
        session_prefix="direct",
        role="execution",
        entry_point="app.tasks.worker:1576 -> execute_task",
        description="Execution-loop step invocation via the execution role.",
    ),
    ProviderLane(
        lane_id="completion_repair",
        session_prefix="completion-summary",
        role="repair",
        entry_point="app.services.orchestration.phases.completion_summary:94",
        description=(
            "Completion-repair / task-summary lane identified by RER-02A as "
            "insufficiently frozen."
        ),
    ),
    ProviderLane(
        lane_id="debug_repair",
        session_prefix="debug-repair",
        role="debug_repair",
        entry_point="app.services.orchestration.phases.execution_loop:2379",
        description="Debug/recovery repair lane.",
    ),
    ProviderLane(
        lane_id="failure_reflection",
        session_prefix="reflection",
        role="execution",
        entry_point="app.services.orchestration.coordinators.failure_coordinator:81",
        description="Failure-coordinator reflection/recovery lane.",
    ),
    ProviderLane(
        lane_id="replan_failure_summary",
        session_prefix="failure_summary",
        role="execution",
        entry_point="app.services.session.replan_service:212",
        description="Replan service failure summarization.",
    ),
    ProviderLane(
        lane_id="session_digest",
        session_prefix="session_digest",
        role="execution",
        entry_point="app.services.session.session_inspection_service:2671",
        description="Session digest / inspection lane.",
    ),
    ProviderLane(
        lane_id="human_intervention",
        session_prefix="human_intervention",
        role="execution",
        entry_point="app.tasks.worker:3217",
        description="Human-intervention guidance lane.",
    ),
)

LANE_BY_ID = {lane.lane_id: lane for lane in PROVIDER_LANES}
LANE_IDS = tuple(lane.lane_id for lane in PROVIDER_LANES)
SESSION_PREFIXES = tuple(sorted({lane.session_prefix for lane in PROVIDER_LANES}))

#: Adapter classes that can reach a real provider boundary.
PROVIDER_CAPABLE_RUNTIMES: tuple[tuple[str, str], ...] = (
    ("app.services.agents.providers.ollama_adapter", "OllamaRuntime"),
    (
        "app.services.agents.providers.openai_chat_adapter",
        "OpenAIChatCompletionsRuntime",
    ),
    ("app.services.agents.providers.openai_adapter", "OpenAIResponsesRuntime"),
    ("app.services.agents.openclaw_service", "OpenClawSessionService"),
)

#: Adapter methods that can start a provider request.  ``invoke_prompt`` alone
#: is insufficient: the execution and discovery lanes use ``execute_task``.
PROVIDER_ENTRY_METHODS: tuple[str, ...] = (
    "invoke_prompt",
    "execute_task",
    "execute_task_with_streaming",
)

#: Deliberately excluded: never reaches a provider.  Listed so the exclusion
#: is explicit evidence rather than an omission.
NON_PROVIDER_RUNTIMES: tuple[tuple[str, str], ...] = (
    ("app.services.agents.stub_runtime", "StubRuntime"),
)


def lane_for_session_prefix(session_prefix: str | None) -> str:
    """Map an observed session prefix to a lane id, or the unattributed lane.

    Ambiguous prefixes (``planning`` is shared by initial Planning and
    read-only discovery) resolve to the first declared lane; the capture
    record always retains the raw prefix and entry method so the ambiguity is
    visible rather than erased.
    """

    prefix = str(session_prefix or "").strip()
    if not prefix:
        return LANE_UNATTRIBUTED
    for lane in PROVIDER_LANES:
        if lane.session_prefix == prefix:
            return lane.lane_id
    return LANE_UNATTRIBUTED


def lane_inventory_evidence() -> dict[str, Any]:
    return {
        "lane_count": len(PROVIDER_LANES),
        "lanes": [lane.as_evidence() for lane in PROVIDER_LANES],
        "provider_capable_runtimes": [
            f"{module}.{name}" for module, name in PROVIDER_CAPABLE_RUNTIMES
        ],
        "provider_entry_methods": list(PROVIDER_ENTRY_METHODS),
        "non_provider_runtimes": [
            f"{module}.{name}" for module, name in NON_PROVIDER_RUNTIMES
        ],
        "unattributed_lane": LANE_UNATTRIBUTED,
        "session_prefixes": list(SESSION_PREFIXES),
    }
