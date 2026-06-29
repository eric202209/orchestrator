"""DecisionAnalyticsService — Phase 15F-1 / 15F-2.

Read-only operational intelligence over existing analytics sources.

Sources:
  - sessions, projects, task_executions, intervention_requests
  - knowledge_usage_logs, knowledge_items
  - orchestration event journal for recovery/coordinator evidence

Does not write to any table. Does not emit events. No runtime behavior changes.
All recommendations are advisory only.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, case, func as sa_func
from sqlalchemy.orm import Session as DbSession

from app.models import (
    InterventionRequest,
    KnowledgeItem,
    KnowledgeUsageLog,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.events.event_types import EventType

_WINDOW_DAYS: Dict[str, Optional[int]] = {
    "7d": 7,
    "30d": 30,
    "all_time": None,
}

_FAILURE_LIKE_EVENTS = {
    EventType.TOOL_FAILED,
    EventType.TASK_FAILED,
    EventType.REPAIR_REJECTED,
    EventType.COMPLETION_EVIDENCE_FAILED,
    EventType.WORKSPACE_CONTRACT_FAILED,
    EventType.EXECUTION_RECOVERY_FAILED,
}

_RECOVERY_ATTEMPT = EventType.EXECUTION_RECOVERY_ATTEMPTED
_RECOVERY_SUCCESS = EventType.EXECUTION_RECOVERY_SUCCEEDED
_PHASE_STARTED = EventType.PHASE_STARTED
_PHASE_FINISHED = EventType.PHASE_FINISHED

_REPEATED_FAILURE_THRESHOLD = 2
_LOW_KNOWLEDGE_EFFECTIVENESS = 0.25
_HIGH_COORDINATOR_FAILURE_RATE = 0.35
_HIGH_PROJECT_REPAIR_CHURN = 2
_UNSTABLE_FAILURE_OCCURRENCES = 5

_PHASE_TO_COORDINATOR = {
    "planning": "PlanningCoordinator",
    "plan": "PlanningCoordinator",
    "execution": "ExecutionCoordinator",
    "execute": "ExecutionCoordinator",
    "failure": "FailureCoordinator",
    "debug": "FailureCoordinator",
    "debug_repair": "FailureCoordinator",
    "completion": "CompletionCoordinator",
    "completion_repair": "CompletionCoordinator",
    "validation": "CompletionCoordinator",
    "review": "ReviewCoordinator",
}


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator > 0 else None


def _sorted_ints(values: Any) -> List[int]:
    return sorted(int(v) for v in values if v is not None)


def _evidence(
    *,
    sample_size: int,
    project_ids: Any,
    session_ids: Any,
    supporting_metrics: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "sample_size": int(sample_size or 0),
        "affected_projects": _sorted_ints(project_ids),
        "affected_sessions": _sorted_ints(session_ids),
        "supporting_metrics": supporting_metrics,
    }


def _confidence(sample_size: int, completeness: float = 1.0) -> float:
    if sample_size <= 0:
        return 0.0
    sample_factor = min(1.0, sample_size / 10.0)
    return round(max(0.0, min(1.0, sample_factor * completeness)), 4)


def _event_details(event: Dict[str, Any]) -> Dict[str, Any]:
    details = event.get("details")
    return details if isinstance(details, dict) else {}


def _strategy_key(event: Dict[str, Any]) -> str:
    details = _event_details(event)
    for key in ("repair_type", "strategy", "recovery_strategy", "repair_strategy"):
        value = details.get(key) or event.get(key)
        if value:
            return str(value)
    phase = event.get("phase") or details.get("phase")
    if phase:
        return str(phase)
    return "execution_recovery"


def _coordinator_for_phase(phase: Any) -> str:
    if not phase:
        return "UnknownCoordinator"
    raw = str(phase)
    normalized = raw.strip().lower()
    if normalized in _PHASE_TO_COORDINATOR:
        return _PHASE_TO_COORDINATOR[normalized]
    if normalized.endswith("_repair") and "completion" in normalized:
        return "CompletionCoordinator"
    if "planning" in normalized or normalized == "planner":
        return "PlanningCoordinator"
    if "execution" in normalized:
        return "ExecutionCoordinator"
    if "completion" in normalized or "validation" in normalized:
        return "CompletionCoordinator"
    if "failure" in normalized or "debug" in normalized or "repair" in normalized:
        return "FailureCoordinator"
    return f"{raw[:1].upper()}{raw[1:]}Coordinator"


def _failure_signature(row: Any) -> str:
    category = getattr(row, "failure_category", None)
    if category:
        return str(category)
    error_message = getattr(row, "error_message", None)
    if error_message:
        return str(error_message).strip()[:120]
    return "uncategorized_failure"


class DecisionAnalyticsService:
    """Computes recommendation-oriented analytics from existing evidence."""

    def __init__(self, db: DbSession) -> None:
        self._db = db

    def compute(self) -> Dict[str, Any]:
        now = datetime.now(UTC)
        events = self._collect_event_records()
        windows: Dict[str, Any] = {}
        for label, days in _WINDOW_DAYS.items():
            since = (now - timedelta(days=days)) if days is not None else None
            window_events = [
                e
                for e in events
                if since is None
                or (e.get("timestamp") is not None and e["timestamp"] >= since)
            ]
            windows[label] = self._compute_window(since, window_events)
        return {
            "windows": windows,
            "generated_at": now.isoformat(),
            "metrics_version": 1,
        }

    def drilldown(
        self,
        *,
        kind: str,
        target: str,
        window: str = "all_time",
    ) -> Dict[str, Any]:
        """Return deterministic detail for one decision analytics item.

        This reuses compute() output and does not duplicate data. Missing items
        return found=False with an empty evidence object.
        """
        result = self.compute()
        selected_window = window if window in result["windows"] else "all_time"
        win = result["windows"][selected_window]
        normalized_kind = (kind or "").strip().lower()
        normalized_target = (target or "").strip()

        collections = {
            "recovery_strategy": (
                win["successful_recovery_strategies"],
                lambda item: item.get("repair_type"),
            ),
            "repeated_failure": (
                win["repeated_failures"],
                lambda item: item.get("failure_signature"),
            ),
            "failure_signature": (
                win["repeated_failures"],
                lambda item: item.get("failure_signature"),
            ),
            "knowledge": (
                win["knowledge_effectiveness"],
                lambda item: item.get("knowledge_item_id") or item.get("title"),
            ),
            "coordinator": (
                win["coordinator_reliability"],
                lambda item: item.get("coordinator"),
            ),
            "project": (
                win["project_reliability"],
                lambda item: (
                    str(item.get("project_id"))
                    if normalized_target.isdigit()
                    else item.get("project_name")
                ),
            ),
        }

        items, key_fn = collections.get(normalized_kind, ([], lambda item: None))
        match = next(
            (
                item
                for item in items
                if str(key_fn(item) or "").strip() == normalized_target
            ),
            None,
        )
        if match is None:
            return {
                "kind": normalized_kind,
                "target": normalized_target,
                "window": selected_window,
                "found": False,
                "item": None,
                "evidence": _evidence(
                    sample_size=0,
                    project_ids=[],
                    session_ids=[],
                    supporting_metrics={},
                ),
            }

        return {
            "kind": normalized_kind,
            "target": normalized_target,
            "window": selected_window,
            "found": True,
            "item": match,
            "evidence": match.get(
                "evidence",
                _evidence(
                    sample_size=match.get("attempts")
                    or match.get("occurrences")
                    or match.get("retrievals")
                    or match.get("invocations")
                    or 0,
                    project_ids=match.get("affected_project_ids", []),
                    session_ids=match.get("affected_session_ids", []),
                    supporting_metrics=match,
                ),
            ),
        }

    def _compute_window(
        self,
        since: Optional[datetime],
        events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        repeated_failures = self._repeated_failures(since)
        knowledge = self._knowledge_effectiveness(since)
        coordinators = self._coordinator_reliability(events)
        projects = self._project_reliability(since)

        return {
            "successful_recovery_strategies": self._recovery_strategies(events),
            "repeated_failures": repeated_failures,
            "knowledge_effectiveness": knowledge,
            "coordinator_reliability": coordinators,
            "project_reliability": projects,
            "improvement_opportunities": self._improvement_opportunities(
                repeated_failures,
                knowledge,
                coordinators,
                projects,
            ),
        }

    def _recovery_strategies(
        self, events: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        attempts: Dict[str, int] = defaultdict(int)
        successes: Dict[str, int] = defaultdict(int)
        projects: Dict[str, set] = defaultdict(set)
        sessions: Dict[str, set] = defaultdict(set)

        for record in events:
            et = record["event"].get("event_type")
            key = _strategy_key(record["event"])
            if et == _RECOVERY_ATTEMPT:
                attempts[key] += 1
            elif et == _RECOVERY_SUCCESS:
                successes[key] += 1
            else:
                continue
            projects[key].add(record.get("project_id"))
            sessions[key].add(record.get("session_id"))

        keys = set(attempts) | set(successes)
        result = [
            {
                "repair_type": key,
                "attempts": attempts.get(key, 0),
                "successes": successes.get(key, 0),
                "success_rate": _rate(successes.get(key, 0), attempts.get(key, 0)),
                "affected_project_ids": _sorted_ints(projects.get(key, [])),
                "affected_session_ids": _sorted_ints(sessions.get(key, [])),
            }
            for key in keys
        ]
        result.sort(
            key=lambda x: (
                (x["success_rate"] is None),
                -(x["success_rate"] or 0),
                -x["attempts"],
                x["repair_type"],
            )
        )
        return result

    def _repeated_failures(self, since: Optional[datetime]) -> List[Dict[str, Any]]:
        q = (
            self._db.query(
                TaskExecution.failure_category,
                Task.error_message,
                TaskExecution.session_id,
                SessionModel.project_id,
            )
            .join(Task, TaskExecution.task_id == Task.id)
            .join(SessionModel, TaskExecution.session_id == SessionModel.id)
            .filter(TaskExecution.status == TaskStatus.FAILED)
            .filter(SessionModel.deleted_at.is_(None))
        )
        if since is not None:
            q = q.filter(TaskExecution.created_at >= since)

        buckets: Dict[str, Dict[str, Any]] = {}
        for row in q.all():
            sig = _failure_signature(row)
            bucket = buckets.setdefault(
                sig,
                {
                    "failure_signature": sig,
                    "occurrences": 0,
                    "projects": set(),
                    "sessions": set(),
                },
            )
            bucket["occurrences"] += 1
            bucket["projects"].add(row.project_id)
            bucket["sessions"].add(row.session_id)

        result = []
        for bucket in buckets.values():
            if bucket["occurrences"] < _REPEATED_FAILURE_THRESHOLD:
                continue
            result.append(
                {
                    "failure_signature": bucket["failure_signature"],
                    "occurrences": bucket["occurrences"],
                    "projects": len(bucket["projects"]),
                    "sessions": len(bucket["sessions"]),
                    "affected_project_ids": _sorted_ints(bucket["projects"]),
                    "affected_session_ids": _sorted_ints(bucket["sessions"]),
                }
            )
        result.sort(
            key=lambda x: (-x["occurrences"], -x["projects"], x["failure_signature"])
        )
        return result

    def _knowledge_effectiveness(
        self, since: Optional[datetime]
    ) -> List[Dict[str, Any]]:
        used_sum = sa_func.sum(
            case((KnowledgeUsageLog.used_in_prompt.is_(True), 1), else_=0)
        )
        effective_sum = sa_func.sum(
            case(
                (
                    and_(
                        KnowledgeUsageLog.used_in_prompt.is_(True),
                        KnowledgeUsageLog.was_effective.is_(True),
                    ),
                    1,
                ),
                else_=0,
            )
        )
        q = (
            self._db.query(
                KnowledgeUsageLog.knowledge_item_id,
                KnowledgeItem.title,
                SessionModel.project_id,
                KnowledgeUsageLog.session_id,
                sa_func.count(KnowledgeUsageLog.id).label("retrievals"),
                used_sum.label("used"),
                effective_sum.label("effective"),
                sa_func.avg(KnowledgeUsageLog.confidence).label("confidence"),
            )
            .join(SessionModel, KnowledgeUsageLog.session_id == SessionModel.id)
            .outerjoin(
                KnowledgeItem, KnowledgeUsageLog.knowledge_item_id == KnowledgeItem.id
            )
            .filter(SessionModel.deleted_at.is_(None))
            .group_by(
                KnowledgeUsageLog.knowledge_item_id,
                KnowledgeItem.title,
                SessionModel.project_id,
                KnowledgeUsageLog.session_id,
            )
        )
        if since is not None:
            q = q.filter(KnowledgeUsageLog.created_at >= since)

        buckets: Dict[str, Dict[str, Any]] = {}
        for row in q.all():
            key = row.knowledge_item_id
            bucket = buckets.setdefault(
                key,
                {
                    "knowledge_item_id": row.knowledge_item_id,
                    "title": row.title,
                    "retrievals": 0,
                    "used": 0,
                    "effective": 0,
                    "confidence_total": 0.0,
                    "confidence_weight": 0,
                    "project_ids": set(),
                    "session_ids": set(),
                },
            )
            retrievals = row.retrievals or 0
            bucket["retrievals"] += retrievals
            bucket["used"] += int(row.used or 0)
            bucket["effective"] += int(row.effective or 0)
            if row.confidence is not None:
                bucket["confidence_total"] += float(row.confidence) * retrievals
                bucket["confidence_weight"] += retrievals
            bucket["project_ids"].add(row.project_id)
            bucket["session_ids"].add(row.session_id)

        result = []
        for bucket in buckets.values():
            retrievals = bucket["retrievals"]
            used = int(bucket["used"] or 0)
            effective = int(bucket["effective"] or 0)
            confidence = (
                round(bucket["confidence_total"] / bucket["confidence_weight"], 4)
                if bucket["confidence_weight"] > 0
                else None
            )
            effectiveness = _rate(effective, used)
            confidence_factor = confidence if confidence is not None else 0.0
            contribution = round((effectiveness or 0.0) * confidence_factor, 4)
            result.append(
                {
                    "knowledge_item_id": bucket["knowledge_item_id"],
                    "title": bucket["title"],
                    "retrievals": retrievals,
                    "success_contribution": effective,
                    "confidence": confidence,
                    "effectiveness": effectiveness,
                    "score": contribution,
                    "affected_project_ids": _sorted_ints(bucket["project_ids"]),
                    "affected_session_ids": _sorted_ints(bucket["session_ids"]),
                }
            )
        result.sort(
            key=lambda x: (-x["score"], -x["success_contribution"], -x["retrievals"])
        )
        return result

    def _coordinator_reliability(
        self, events: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        stats: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "invocations": 0,
                "failures": 0,
                "recovery_attempts": 0,
                "recovery_successes": 0,
                "durations": [],
                "project_ids": set(),
                "session_ids": set(),
            }
        )
        active: Dict[str, List[datetime]] = defaultdict(list)
        current_by_scope: Dict[str, str] = {}

        for record in sorted(
            events, key=lambda r: r.get("timestamp") or datetime.min.replace(tzinfo=UTC)
        ):
            event = record["event"]
            et = event.get("event_type")
            phase = event.get("phase") or _event_details(event).get("phase")
            scope = f"{record.get('session_id')}:{record.get('task_id')}"

            if et == _PHASE_STARTED and phase:
                coordinator = _coordinator_for_phase(phase)
                stats[coordinator]["invocations"] += 1
                stats[coordinator]["project_ids"].add(record.get("project_id"))
                stats[coordinator]["session_ids"].add(record.get("session_id"))
                current_by_scope[scope] = coordinator
                if record.get("timestamp") is not None:
                    active[f"{scope}:{phase}"].append(record["timestamp"])
                continue

            coordinator = (
                _coordinator_for_phase(phase)
                if phase
                else current_by_scope.get(scope, "UnknownCoordinator")
            )

            if et == _PHASE_FINISHED and phase:
                stack = active.get(f"{scope}:{phase}", [])
                if stack and record.get("timestamp") is not None:
                    started_at = stack.pop(0)
                    duration = (record["timestamp"] - started_at).total_seconds()
                    if duration >= 0:
                        stats[coordinator]["durations"].append(duration)
                continue

            if et in _FAILURE_LIKE_EVENTS:
                stats[coordinator]["failures"] += 1
                stats[coordinator]["project_ids"].add(record.get("project_id"))
                stats[coordinator]["session_ids"].add(record.get("session_id"))
            elif et == _RECOVERY_ATTEMPT:
                stats[coordinator]["recovery_attempts"] += 1
                stats[coordinator]["project_ids"].add(record.get("project_id"))
                stats[coordinator]["session_ids"].add(record.get("session_id"))
            elif et == _RECOVERY_SUCCESS:
                stats[coordinator]["recovery_successes"] += 1
                stats[coordinator]["project_ids"].add(record.get("project_id"))
                stats[coordinator]["session_ids"].add(record.get("session_id"))

        result = []
        for coordinator, data in stats.items():
            durations = data["durations"]
            avg_duration = (
                round(sum(durations) / len(durations), 4) if durations else None
            )
            result.append(
                {
                    "coordinator": coordinator,
                    "invocations": data["invocations"],
                    "failures": data["failures"],
                    "recovery_rate": _rate(
                        data["recovery_successes"], data["recovery_attempts"]
                    ),
                    "average_duration_seconds": avg_duration,
                    "affected_project_ids": _sorted_ints(data["project_ids"]),
                    "affected_session_ids": _sorted_ints(data["session_ids"]),
                }
            )
        result.sort(key=lambda x: (-x["failures"], x["coordinator"]))
        return result

    def _project_reliability(self, since: Optional[datetime]) -> List[Dict[str, Any]]:
        projects = (
            self._db.query(Project)
            .filter(Project.deleted_at.is_(None))
            .order_by(Project.name.asc())
            .all()
        )
        result = []
        for project in projects:
            sessions_q = self._db.query(SessionModel).filter(
                SessionModel.project_id == project.id,
                SessionModel.deleted_at.is_(None),
            )
            if since is not None:
                sessions_q = sessions_q.filter(SessionModel.created_at >= since)
            sessions = sessions_q.all()
            session_ids = [s.id for s in sessions]
            completed = sum(1 for s in sessions if s.status == "completed")
            failed = sum(1 for s in sessions if s.status == "stopped")
            terminal = completed + failed
            churn = sum(1 for s in sessions if s.repair_churn_stopped is True)

            interventions = 0
            if session_ids:
                ivt_q = self._db.query(
                    sa_func.count(sa_func.distinct(InterventionRequest.session_id))
                ).filter(InterventionRequest.session_id.in_(session_ids))
                if since is not None:
                    ivt_q = ivt_q.filter(InterventionRequest.created_at >= since)
                interventions = ivt_q.scalar() or 0

            attempts = successes = 0
            if session_ids:
                recovery_q = self._db.query(TaskExecution).filter(
                    TaskExecution.session_id.in_(session_ids),
                    TaskExecution.attempt_number > 1,
                    TaskExecution.status.in_(
                        (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)
                    ),
                )
                if since is not None:
                    recovery_q = recovery_q.filter(TaskExecution.created_at >= since)
                recovery_rows = recovery_q.all()
                attempts = len(recovery_rows)
                successes = sum(
                    1 for ex in recovery_rows if ex.status == TaskStatus.DONE
                )

            intervention_rate = _rate(interventions, terminal)
            result.append(
                {
                    "project_id": project.id,
                    "project_name": project.name,
                    "session_success_rate": _rate(completed, terminal),
                    "intervention_rate": intervention_rate,
                    "autonomy_rate": (
                        round(1 - intervention_rate, 4)
                        if intervention_rate is not None
                        else None
                    ),
                    "recovery_rate": _rate(successes, attempts),
                    "repair_churn": churn,
                    "terminal_sessions": terminal,
                    "affected_project_ids": [project.id],
                    "affected_session_ids": _sorted_ints(session_ids),
                }
            )
        result.sort(
            key=lambda x: (
                -x["repair_churn"],
                x["session_success_rate"] is None,
                x["session_success_rate"] or 0,
                x["project_name"],
            )
        )
        return result

    def _improvement_opportunities(
        self,
        failures: List[Dict[str, Any]],
        knowledge: List[Dict[str, Any]],
        coordinators: List[Dict[str, Any]],
        projects: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        opportunities: List[Dict[str, Any]] = []

        for item in knowledge:
            if item["retrievals"] < 3 or item["effectiveness"] is None:
                continue
            if item["effectiveness"] < _LOW_KNOWLEDGE_EFFECTIVENESS:
                opportunities.append(
                    {
                        "kind": "knowledge",
                        "target": item["title"] or item["knowledge_item_id"],
                        "metric_label": "Effectiveness",
                        "metric_value": item["effectiveness"],
                        "confidence": _confidence(
                            item["retrievals"],
                            (
                                item["confidence"]
                                if item["confidence"] is not None
                                else 0.5
                            ),
                        ),
                        "recommendation": "Candidate for rewrite.",
                        "rationale": "Knowledge is retrieved often enough to evaluate but rarely contributes to successful work.",
                        "severity": "medium",
                        "evidence": _evidence(
                            sample_size=item["retrievals"],
                            project_ids=item.get("affected_project_ids", []),
                            session_ids=item.get("affected_session_ids", []),
                            supporting_metrics={
                                "retrievals": item["retrievals"],
                                "success_contribution": item["success_contribution"],
                                "effectiveness": item["effectiveness"],
                                "average_confidence": item["confidence"],
                                "score": item["score"],
                            },
                        ),
                    }
                )

        for coordinator in coordinators:
            invocations = coordinator["invocations"]
            failures_count = coordinator["failures"]
            failure_rate = _rate(failures_count, invocations)
            if (
                failure_rate is not None
                and failure_rate >= _HIGH_COORDINATOR_FAILURE_RATE
            ):
                opportunities.append(
                    {
                        "kind": "coordinator",
                        "target": coordinator["coordinator"],
                        "metric_label": "Failure rate",
                        "metric_value": failure_rate,
                        "confidence": _confidence(invocations),
                        "recommendation": f"Review {coordinator['coordinator']} prompt and recovery policy.",
                        "rationale": "Coordinator failures are high relative to observed invocations.",
                        "severity": "high",
                        "evidence": _evidence(
                            sample_size=invocations,
                            project_ids=coordinator.get("affected_project_ids", []),
                            session_ids=coordinator.get("affected_session_ids", []),
                            supporting_metrics={
                                "invocations": invocations,
                                "failures": failures_count,
                                "failure_rate": failure_rate,
                                "recovery_rate": coordinator["recovery_rate"],
                                "average_duration_seconds": coordinator[
                                    "average_duration_seconds"
                                ],
                            },
                        ),
                    }
                )

        for project in projects:
            if project["repair_churn"] >= _HIGH_PROJECT_REPAIR_CHURN:
                opportunities.append(
                    {
                        "kind": "project",
                        "target": project["project_name"],
                        "metric_label": "Repair churn",
                        "metric_value": project["repair_churn"],
                        "confidence": _confidence(project["terminal_sessions"]),
                        "recommendation": "Investigate repeated repairs before adding more automation.",
                        "rationale": "The project has hit the repair churn guard repeatedly.",
                        "severity": "high",
                        "evidence": _evidence(
                            sample_size=project["terminal_sessions"],
                            project_ids=project.get("affected_project_ids", []),
                            session_ids=project.get("affected_session_ids", []),
                            supporting_metrics={
                                "repair_churn": project["repair_churn"],
                                "session_success_rate": project["session_success_rate"],
                                "intervention_rate": project["intervention_rate"],
                                "autonomy_rate": project["autonomy_rate"],
                                "recovery_rate": project["recovery_rate"],
                            },
                        ),
                    }
                )

        for failure in failures:
            if failure["occurrences"] >= _UNSTABLE_FAILURE_OCCURRENCES:
                opportunities.append(
                    {
                        "kind": "failure_signature",
                        "target": failure["failure_signature"],
                        "metric_label": "Repeated",
                        "metric_value": failure["occurrences"],
                        "confidence": _confidence(failure["occurrences"]),
                        "recommendation": "High priority operational issue.",
                        "rationale": "The same failure signature is recurring across sessions or projects.",
                        "severity": "high",
                        "evidence": _evidence(
                            sample_size=failure["occurrences"],
                            project_ids=failure.get("affected_project_ids", []),
                            session_ids=failure.get("affected_session_ids", []),
                            supporting_metrics={
                                "occurrences": failure["occurrences"],
                                "project_count": failure["projects"],
                                "session_count": failure["sessions"],
                            },
                        ),
                    }
                )

        severity_rank = {"high": 0, "medium": 1, "low": 2}
        opportunities.sort(
            key=lambda x: (
                severity_rank.get(x["severity"], 9),
                -float(x["metric_value"] or 0),
                x["target"],
            )
        )
        return opportunities

    def _collect_event_records(self) -> List[Dict[str, Any]]:
        from app.services.orchestration.state.persistence import (
            read_orchestration_events,
        )
        from app.services.session.session_runtime_service import (
            resolve_event_log_project_dir,
        )

        records: List[Dict[str, Any]] = []
        try:
            sessions = (
                self._db.query(SessionModel)
                .filter(SessionModel.deleted_at.is_(None))
                .all()
            )
        except Exception:
            return records

        for sess in sessions:
            try:
                tasks = (
                    self._db.query(Task)
                    .filter(Task.project_id == sess.project_id)
                    .all()
                )
            except Exception:
                continue

            for task in tasks:
                try:
                    project_dir = resolve_event_log_project_dir(self._db, sess, task.id)
                    if not project_dir:
                        continue
                    events = read_orchestration_events(project_dir, sess.id, task.id)
                except Exception:
                    continue

                for event in events:
                    if not isinstance(event, dict):
                        continue
                    records.append(
                        {
                            "project_id": sess.project_id,
                            "session_id": sess.id,
                            "task_id": task.id,
                            "timestamp": _parse_ts(event.get("timestamp")),
                            "event": event,
                        }
                    )
        return records
