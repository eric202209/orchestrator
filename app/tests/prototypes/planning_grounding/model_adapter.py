"""Real-model grounding adapter for the PGP2 trial.

The model is given the operator task, deterministic orientation, the bounded
observations made so far, and the remaining budget. It is given no expected
path, symbol, route, byte range, observation id, or fixture metadata, and no
historical conclusion from any earlier phase.

It chooses the navigation. This module only renders the prompt and parses the
reply; it contains no relevance scoring of any kind.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import protocol as P
from .harness import GroundingAdapter
from .provider_client import planning_chat

SYSTEM_PROMPT = (
    "You are the grounding stage of a software planning system.\n"
    "You are NOT writing a plan. You are NOT editing code. You are NOT "
    "proposing changes.\n"
    "Your only job is to gather enough read-only evidence to identify which "
    "existing implementation region of this repository implements the "
    "behavior the task is about.\n"
    "You may request bounded read-only inspection. The harness executes it; "
    "you never read files yourself.\n"
    "After each observation you must decide whether the evidence is "
    "sufficient.\n"
    "Answer with exactly one JSON object and no other text."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

ACTION_MENU = """AVAILABLE READ-ONLY ACTIONS (choose exactly one):

{"action": "search_text", "query": "<exact literal text>", "scope_paths": ["<path>", ...], "max_results": <1-4>}
    Reports where that exact literal occurs, in file order then byte order,
    together with the symbol or route that owns each occurrence.

{"action": "inspect_symbol", "symbol_name": "<function or class name>", "scope_paths": ["<path>", ...]}
    Returns the definition region of that named symbol.

{"action": "inspect_route", "route_method": "GET|POST|PUT|DELETE|PATCH", "route_path": "<route path>", "scope_paths": ["<path>", ...]}
    Returns the handler region of that route, including its decorator.

DECISIONS (choose one of these instead of an action when you are done):

{"decision": "SUFFICIENT", "cited_observation_ids": ["<observation id>", ...], "structural_locators": ["<symbol or route you believe implements the behavior>"], "why": "<one sentence>"}

{"decision": "NEED_MORE_EVIDENCE", "next_action": {"action": "<one available action>", ...}, "why": "<one sentence>"}

{"decision": "INSUFFICIENT", "why": "<one sentence>"}

Rules:
- scope_paths must be chosen from the orientation list above.
- Cite only observation ids that already exist in OBSERVATIONS.
- Cite only the evidence you actually believe implements the requested
  behavior. Do not cite an observation you consider irrelevant.
- After an observation exists, explicitly assess whether the observed existing
  code is sufficient to identify the implementation area relevant to the requested change. The requested future behavior does not need to already exist.
- If it is not enough and a request remains, choose NEED_MORE_EVIDENCE and put
  exactly one next read-only action in next_action.
- If you cannot establish the implementation within the budget, answer
  INSUFFICIENT."""


def _render_orientation(orientation: P.Orientation) -> str:
    if not orientation.available or not orientation.paths:
        return "REPOSITORY ORIENTATION: unavailable."
    lines = [
        "REPOSITORY ORIENTATION (facts only):",
        "Git-tracked paths whose path text contains a word from the task. These",
        "are advisory candidates, not an answer: a listed path is not known to be",
        "relevant, and no implementation region is identified here.",
        "",
    ]
    lines.extend(f"- {path}" for path in orientation.paths)
    if orientation.truncated:
        lines.append(
            f"({orientation.entries_total} candidates matched; "
            f"{len(orientation.paths)} shown)"
        )
    return "\n".join(lines)


def _render_observations(observations) -> str:
    if not observations:
        return "OBSERVATIONS: none yet."
    blocks = ["OBSERVATIONS:"]
    for item in observations:
        if item.outcome == P.OBSERVATION_NOT_FOUND:
            action = item.action
            if action.kind == P.ACTION_INSPECT_ROUTE:
                requested = (
                    f"{(action.route_method or '').upper()} {action.route_path or ''}"
                )
                evidence = "no matching route found"
                declaration_count = item.notes.get("route_declaration_count")
                if declaration_count:
                    evidence += (
                        f"; {declaration_count} route declarations exist in the "
                        "scoped file(s)"
                    )
            elif action.kind == P.ACTION_INSPECT_SYMBOL:
                requested = f"symbol {action.symbol_name or ''}"
                evidence = "no matching symbol found"
            else:
                requested = f"literal {action.query or ''}"
                evidence = "no matching text found"
            blocks.append(
                f"\n--- {item.observation_id} ---\n"
                f"outcome: {P.OBSERVATION_NOT_FOUND}\n"
                f"request: {requested}\n"
                f"scope: {', '.join(action.scope_paths)}\n"
                f"evidence: {evidence}"
            )
            continue
        identity = item.structural_identity
        if identity is None:
            descriptor = "no structural owner"
        elif identity.route_path:
            descriptor = (
                f"{identity.kind} {identity.name} "
                f"[{identity.http_method} {identity.route_path}] "
                f"lines {identity.start_line}-{identity.end_line}"
            )
        else:
            descriptor = (
                f"{identity.kind} {identity.name} "
                f"lines {identity.start_line}-{identity.end_line}"
            )
        blocks.append(
            f"\n--- {item.observation_id} ---\n"
            f"outcome: {item.outcome}\n"
            f"path: {item.source_path}\n"
            f"structural identity: {descriptor}\n"
            f"bytes returned: {item.byte_count}"
            f"{' (truncated)' if item.truncated else ''}\n"
            f"content:\n"
            f"{item.bounded_content.decode('utf-8', 'replace')}"
        )
    return "\n".join(blocks)


def _render_budget(budget: P.Budget) -> str:
    return (
        "GROUNDING BUDGET (enforced by the harness):\n"
        f"- inspection requests remaining: {budget.requests_remaining}\n"
        f"- source evidence bytes remaining: {budget.evidence_bytes_remaining}\n"
        f"- distinct files remaining: {budget.files_remaining}\n"
        f"- regions remaining: {budget.regions_remaining}"
    )


def build_user_prompt(request: P.GroundingRequest, observations) -> str:
    return "\n\n".join(
        [
            "TASK (operator wording, verbatim):\n" + request.task_text,
            _render_orientation(request.orientation),
            _render_budget(request.remaining_budget),
            _render_observations(observations),
            ACTION_MENU,
        ]
    )


def _parse_action_payload(payload: dict, orientation: P.Orientation):
    kind = str(payload.get("action") or "").strip()
    raw_scope = payload.get("scope_paths") or []
    scope = tuple(str(item) for item in raw_scope if isinstance(item, str))
    # A missing scope defaults to the advisory orientation list. This is a
    # protocol convenience, not semantic help: it narrows nothing the model
    # was not already shown.
    if not scope:
        scope = orientation.paths
    try:
        max_results = max(1, min(4, int(payload.get("max_results") or 1)))
    except (TypeError, ValueError):
        return None, "max_results is not an integer"
    if kind == P.ACTION_SEARCH_TEXT:
        return (
            P.GroundingAction(
                kind=P.ACTION_SEARCH_TEXT,
                mode=P.SEARCH_MODE_LITERAL,
                query=str(payload.get("query") or ""),
                scope_paths=scope,
                max_results=max_results,
            ),
            None,
        )
    if kind == P.ACTION_INSPECT_SYMBOL:
        return (
            P.GroundingAction(
                kind=P.ACTION_INSPECT_SYMBOL,
                symbol_name=str(payload.get("symbol_name") or ""),
                scope_paths=scope,
                max_results=max_results,
            ),
            None,
        )
    if kind == P.ACTION_INSPECT_ROUTE:
        return (
            P.GroundingAction(
                kind=P.ACTION_INSPECT_ROUTE,
                route_method=str(payload.get("route_method") or "") or None,
                route_path=str(payload.get("route_path") or ""),
                scope_paths=scope,
                max_results=max_results,
            ),
            None,
        )
    return None, f"unrecognised action/decision: {payload!r}"[:300]


def parse_model_reply(reply: str, orientation: P.Orientation):
    """Parse one JSON object into an action or an explicit assessment."""

    match = _JSON_RE.search(reply or "")
    if match is None:
        return None, "no JSON object in model reply"
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return None, f"unparsable JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "model reply is not a JSON object"

    decision = str(payload.get("decision") or "").strip().upper()
    if decision == P.DECISION_SUFFICIENT:
        cited = payload.get("cited_observation_ids") or []
        locators = payload.get("structural_locators") or []
        return (
            P.GroundingDecision(
                decision=P.DECISION_SUFFICIENT,
                sufficiency=P.SufficiencyClaim(
                    cited_observation_ids=tuple(str(item) for item in cited),
                    relevant_source_paths=(),
                    structural_locators=tuple(str(item) for item in locators),
                ),
                rationale=str(payload.get("why") or "")[:400],
            ),
            None,
        )
    if decision == P.DECISION_NEED_MORE_EVIDENCE:
        next_payload = payload.get("next_action")
        if not isinstance(next_payload, dict):
            return None, "NEED_MORE_EVIDENCE requires next_action"
        next_action, error = _parse_action_payload(next_payload, orientation)
        if error is not None:
            return None, f"invalid next_action: {error}"
        return (
            P.GroundingDecision(
                decision=P.DECISION_NEED_MORE_EVIDENCE,
                next_action=next_action,
                rationale=str(payload.get("why") or "")[:400],
            ),
            None,
        )
    if decision in {P.DECISION_INSUFFICIENT, "INSUFFICIENT_GROUNDING"}:
        return (
            P.GroundingDecision(
                decision=P.DECISION_INSUFFICIENT,
                stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
                rationale=str(payload.get("why") or "")[:400],
            ),
            None,
        )

    return _parse_action_payload(payload, orientation)


@dataclass
class ModelGroundingAdapter(GroundingAdapter):
    """Delegates every semantic choice to the configured Planning model."""

    transcript: list[dict] = field(default_factory=list)

    def propose(self, request, observations):
        user = build_user_prompt(request, observations)
        reply = planning_chat(SYSTEM_PROMPT, user)
        parsed, error = parse_model_reply(reply, request.orientation)
        self.transcript.append(
            {
                "turn": request.turn,
                "prompt_bytes": len(user.encode("utf-8")),
                "reply": reply,
                "parse_error": error,
            }
        )
        if parsed is None:
            return P.GroundingDecision(
                decision=P.DECISION_INSUFFICIENT,
                stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
                rationale=f"model_reply_unusable: {error}",
            )
        return parsed


# PHASE35-PGP3 only. The single mechanical difference from the PGP2 contract:
# `scope_paths` is bounded by Git-tracked repository scope rather than by the
# advisory orientation list, because orientation is truncated and does not
# necessarily contain the implementation. It adds no semantic hint -- no
# mention of services, wrappers, aliases, indirection, refinement, repetition,
# or when to declare sufficiency.
ACTION_MENU_OPEN_SCOPE = ACTION_MENU.replace(
    "- scope_paths must be chosen from the orientation list above.",
    "- scope_paths must be Git-tracked repository paths. The orientation list\n"
    "  above is partial and advisory: it is neither complete nor known to be\n"
    "  relevant, and you may name any other repository path instead.",
)


def build_open_scope_user_prompt(request, observations):
    """PGP3 prompt: identical to PGP2 except for the scope rule above."""

    return "\n\n".join(
        [
            "TASK (operator wording, verbatim):\n" + request.task_text,
            _render_orientation(request.orientation),
            _render_budget(request.remaining_budget),
            _render_observations(observations),
            ACTION_MENU_OPEN_SCOPE,
        ]
    )


@dataclass
class OpenScopeModelGroundingAdapter(ModelGroundingAdapter):
    """PGP3 adapter. Same model, same contract, open Git-tracked path scope."""

    def propose(self, request, observations):
        user = build_open_scope_user_prompt(request, observations)
        reply = planning_chat(SYSTEM_PROMPT, user)
        parsed, error = parse_model_reply(reply, request.orientation)
        self.transcript.append(
            {
                "turn": request.turn,
                "prompt_bytes": len(user.encode("utf-8")),
                "reply": reply,
                "parse_error": error,
            }
        )
        if parsed is None:
            return P.GroundingDecision(
                decision=P.DECISION_INSUFFICIENT,
                stop_reason=P.STOP_INSUFFICIENT_GROUNDING,
                rationale=f"model_reply_unusable: {error}",
            )
        return parsed
