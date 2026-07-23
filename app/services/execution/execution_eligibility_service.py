"""Deterministic Execution Plan eligibility projection and reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json

from sqlalchemy.orm import Session

from app.models import (
    ExecutionDependencyEdge,
    ExecutionGroup,
    ExecutionGroupMember,
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskTransition,
)
from app.services.execution.execution_plan_commit_service import (
    DEPENDENCY_RUNTIME_CLASS_MAP,
    ExecutionPlanCommitError,
    ExecutionPlanCommitService,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskLifecycleFence,
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionResult,
    ExecutionTaskTransitionService,
)
from app.services.planning.operator_review import canonical_json_hash


EXECUTION_ELIGIBILITY_SCHEMA_VERSION = "execution-eligibility/1.0"
SUPPORTED_DEPENDENCY_TYPES = frozenset(DEPENDENCY_RUNTIME_CLASS_MAP)
SUPPORTED_BLOCKING_STATES = frozenset({"blocking", "non_blocking", "review_required"})
SUPPORTED_GROUP_KINDS = frozenset(
    {"sequential", "parallel", "optional", "review_gate", "verification"}
)
SUPPORTED_GROUP_SKIP_POLICIES = frozenset({"not_skippable", "skippable"})
RECONCILABLE_STATES = frozenset({"pending", "blocked", "ready"})
REGRESSION_BLOCKERS = frozenset(
    {
        "waiting_on_dependencies",
        "dependency_failed",
        "dependency_cancelled",
        "dependency_skipped",
        "review_gate_pending",
        "manual_gate_pending",
        "resource_gate_pending",
        "group_gate_pending",
    }
)
NON_MUTATING_REASONS = frozenset(
    {
        "graph_integrity_failure",
        "lifecycle_integrity_failure",
        "unknown_dependency_type",
        "unknown_gate_type",
        "execution_plan_inactive",
        "task_state_not_reconcilable",
    }
)


class ExecutionEligibilityError(Exception):
    """Bounded error for an eligibility command or missing authority row."""

    def __init__(self, code: str, message: str, decision=None):
        self.code = code
        self.message = message
        self.decision = decision
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ExecutionDependencyResult:
    execution_dependency_edge_id: int
    plan_dependency_id: str
    dependency_type: str
    runtime_class: str
    prerequisite_execution_task_id: int
    prerequisite_plan_task_id: str | None
    predecessor_state: str | None
    predecessor_state_version: int | None
    predecessor_lifecycle_head_hash: str | None
    result: str
    reason_code: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_dependency_edge_id": self.execution_dependency_edge_id,
            "plan_dependency_id": self.plan_dependency_id,
            "dependency_type": self.dependency_type,
            "runtime_class": self.runtime_class,
            "prerequisite_execution_task_id": self.prerequisite_execution_task_id,
            "prerequisite_plan_task_id": self.prerequisite_plan_task_id,
            "predecessor_state": self.predecessor_state,
            "predecessor_state_version": self.predecessor_state_version,
            "predecessor_lifecycle_head_hash": self.predecessor_lifecycle_head_hash,
            "result": self.result,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ExecutionGateResult:
    gate_type: str
    gate_id: str
    result: str
    reason_code: str | None
    detail: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "gate_type": self.gate_type,
            "gate_id": self.gate_id,
            "result": self.result,
            "reason_code": self.reason_code,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class ExecutionEligibilityDecision:
    execution_plan_id: int
    execution_task_id: int
    plan_task_id: str
    plan_status: str
    evaluated_state: str
    evaluated_state_version: int
    eligible: bool
    recommended_state: str
    reason_code: str
    blockers: tuple[str, ...]
    dependency_results: tuple[ExecutionDependencyResult, ...]
    gate_results: tuple[ExecutionGateResult, ...]
    graph_hash: str
    lifecycle_head_hashes: tuple[tuple[int, str | None], ...]
    decision_hash: str

    def payload(self, *, include_hash: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": EXECUTION_ELIGIBILITY_SCHEMA_VERSION,
            "execution_plan_id": self.execution_plan_id,
            "execution_task_id": self.execution_task_id,
            "plan_task_id": self.plan_task_id,
            "plan_status": self.plan_status,
            "evaluated_state": self.evaluated_state,
            "evaluated_state_version": self.evaluated_state_version,
            "eligible": self.eligible,
            "recommended_state": self.recommended_state,
            "reason_code": self.reason_code,
            "blockers": list(self.blockers),
            "dependency_results": [item.to_dict() for item in self.dependency_results],
            "gate_results": [item.to_dict() for item in self.gate_results],
            "graph_hash": self.graph_hash,
            "lifecycle_head_hashes": [
                {"execution_task_id": task_id, "event_hash": event_hash}
                for task_id, event_hash in self.lifecycle_head_hashes
            ],
        }
        if include_hash:
            payload["decision_hash"] = self.decision_hash
        return payload


@dataclass(frozen=True)
class EligibilityReconciliationResult:
    decision: ExecutionEligibilityDecision
    transition: ExecutionTaskTransitionResult | None
    no_op: bool
    replayed: bool = False


@dataclass(frozen=True)
class _GraphSnapshot:
    plan: ExecutionPlan
    tasks: tuple[ExecutionTask, ...]
    edges: tuple[ExecutionDependencyEdge, ...]
    groups: tuple[ExecutionGroup, ...]
    members: tuple[ExecutionGroupMember, ...]
    task_by_id: Mapping[int, ExecutionTask]
    graph_hash: str
    graph_issues: tuple[str, ...]


class ExecutionEligibilityService:
    """Evaluate and explicitly reconcile one immutable execution task."""

    def __init__(self, db: Session):
        self.db = db
        self._transition = ExecutionTaskTransitionService(db)

    def evaluate_task(self, execution_task_id: int) -> ExecutionEligibilityDecision:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            raise ExecutionEligibilityError(
                "execution_task_not_found", "Execution Task was not found"
            )
        plan = self.db.get(ExecutionPlan, task.execution_plan_id)
        if plan is None:
            raise ExecutionEligibilityError(
                "graph_integrity_failure", "Execution Task parent plan is missing"
            )
        graph = self._load_graph(plan)
        incoming = tuple(
            edge for edge in graph.edges if edge.dependent_execution_task_id == task.id
        )
        relevant_tasks = {task.id: task}
        for edge in incoming:
            predecessor = graph.task_by_id.get(edge.prerequisite_execution_task_id)
            if predecessor is not None:
                relevant_tasks[predecessor.id] = predecessor

        lifecycle_issues: list[str] = []
        lifecycle_heads: dict[int, str | None] = {}
        for relevant in sorted(relevant_tasks.values(), key=lambda row: row.id):
            lifecycle_heads[relevant.id] = self._lifecycle_head(relevant.id)
            try:
                self._transition.verify_task_lifecycle_integrity(relevant.id)
            except ExecutionTaskTransitionError:
                lifecycle_issues.append("lifecycle_integrity_failure")

        dependency_results = tuple(
            self._dependency_result(edge, graph.task_by_id, lifecycle_heads)
            for edge in incoming
        )
        gate_results = self._gate_results(task, graph)
        blockers = self._ordered_blockers(
            graph.graph_issues, lifecycle_issues, dependency_results, gate_results
        )

        if graph.graph_issues or lifecycle_issues:
            reason_code = self._integrity_reason(graph.graph_issues, lifecycle_issues)
            eligible = False
            recommended_state = task.status
        elif plan.status != "active":
            reason_code = "execution_plan_inactive"
            blockers = self._append_once(blockers, reason_code)
            eligible = False
            recommended_state = task.status
        elif task.status not in RECONCILABLE_STATES:
            reason_code = "task_state_not_reconcilable"
            blockers = self._append_once(blockers, reason_code)
            eligible = False
            recommended_state = task.status
        elif blockers:
            reason_code = self._primary_blocker(blockers)
            eligible = False
            recommended_state = "blocked" if task.status == "pending" else task.status
        else:
            eligible = True
            recommended_state = "ready"
            reason_code = (
                "eligible_root_task"
                if not dependency_results
                else "eligible_dependencies_satisfied"
            )

        decision = ExecutionEligibilityDecision(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            plan_task_id=task.plan_task_id,
            plan_status=plan.status,
            evaluated_state=task.status,
            evaluated_state_version=int(task.state_version),
            eligible=eligible,
            recommended_state=recommended_state,
            reason_code=reason_code,
            blockers=tuple(blockers),
            dependency_results=dependency_results,
            gate_results=gate_results,
            graph_hash=graph.graph_hash,
            lifecycle_head_hashes=tuple(sorted(lifecycle_heads.items())),
            decision_hash="",
        )
        return ExecutionEligibilityDecision(
            **{
                **decision.__dict__,
                "decision_hash": canonical_json_hash(decision.payload()),
            }
        )

    def evaluate_plan(
        self, execution_plan_id: int
    ) -> tuple[ExecutionEligibilityDecision, ...]:
        plan = self.db.get(ExecutionPlan, int(execution_plan_id))
        if plan is None:
            raise ExecutionEligibilityError(
                "execution_plan_not_found", "Execution Plan was not found"
            )
        tasks = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == plan.id)
            .order_by(ExecutionTask.plan_task_id.asc(), ExecutionTask.id.asc())
            .all()
        )
        return tuple(self.evaluate_task(task.id) for task in tasks)

    def reconcile_task(
        self,
        execution_task_id: int,
        expected_state: str,
        expected_state_version: int,
        actor_id: str,
        idempotency_key: str,
    ) -> EligibilityReconciliationResult:
        task_id = int(execution_task_id)
        expected_version = int(expected_state_version)
        actor = str(actor_id)
        key = str(idempotency_key)
        prior = self._find_reconciliation_event(task_id, actor, key)
        if prior is not None:
            return self._replay_existing(
                prior, task_id, expected_state, expected_version
            )

        decision = self.evaluate_task(task_id)
        self._assert_expected(decision, expected_state, expected_version)
        if self._action(decision) is None:
            return EligibilityReconciliationResult(
                decision=decision, transition=None, no_op=True
            )

        # The savepoint keeps the fresh read/fence/transition sequence atomic
        # for callers that leave the surrounding transaction open.
        with self.db.begin_nested():
            try:
                fresh = self.evaluate_task(task_id)
                self._assert_expected(fresh, expected_state, expected_version)
            except ExecutionEligibilityError:
                concurrent = self._find_reconciliation_event(task_id, actor, key)
                if concurrent is not None:
                    return self._replay_existing(
                        concurrent, task_id, expected_state, expected_version
                    )
                raise
            action = self._action(fresh)
            if action is None:
                return EligibilityReconciliationResult(
                    decision=fresh, transition=None, no_op=True
                )
            self._assert_predecessor_fence(fresh)
            to_state, transition_reason = action
            command = ExecutionTaskTransitionCommand(
                execution_task_id=task_id,
                execution_plan_id=fresh.execution_plan_id,
                expected_from_state=fresh.evaluated_state,
                expected_state_version=fresh.evaluated_state_version,
                to_state=to_state,
                reason_code=transition_reason,
                reason_detail=self._reason_detail(fresh),
                actor_type="system",
                actor_id=actor,
                idempotency_key=self._transition_command_id(
                    key, task_id, expected_state, expected_version, fresh
                ),
                guarded_task_fences=self._fences(fresh),
            )
            try:
                transition = self._transition.transition(command)
            except ExecutionTaskTransitionError as exc:
                concurrent = self._find_reconciliation_event(task_id, actor, key)
                if concurrent is not None:
                    return self._replay_existing(
                        concurrent, task_id, expected_state, expected_version
                    )
                if exc.code == "transition_dependency_stale":
                    raise ExecutionEligibilityError(
                        "eligibility_predecessor_stale", exc.message, fresh
                    ) from exc
                raise ExecutionEligibilityError(exc.code, exc.message, fresh) from exc
            return EligibilityReconciliationResult(
                decision=fresh,
                transition=transition,
                no_op=False,
                replayed=transition.replayed,
            )

    def _load_graph(self, plan: ExecutionPlan) -> _GraphSnapshot:
        tasks = tuple(
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == plan.id)
            .order_by(ExecutionTask.plan_task_id.asc(), ExecutionTask.id.asc())
            .all()
        )
        edges = tuple(
            self.db.query(ExecutionDependencyEdge)
            .filter(ExecutionDependencyEdge.execution_plan_id == plan.id)
            .order_by(
                ExecutionDependencyEdge.plan_dependency_id.asc(),
                ExecutionDependencyEdge.id.asc(),
            )
            .all()
        )
        groups = tuple(
            self.db.query(ExecutionGroup)
            .filter(ExecutionGroup.execution_plan_id == plan.id)
            .order_by(ExecutionGroup.plan_group_id.asc(), ExecutionGroup.id.asc())
            .all()
        )
        members = tuple(
            self.db.query(ExecutionGroupMember)
            .join(ExecutionGroup)
            .filter(ExecutionGroup.execution_plan_id == plan.id)
            .order_by(
                ExecutionGroup.plan_group_id.asc(),
                ExecutionGroupMember.member_order.asc(),
                ExecutionGroupMember.id.asc(),
            )
            .all()
        )
        task_by_id = {row.id: row for row in tasks}
        issues: list[str] = []
        if len(task_by_id) != len(tasks) or len(
            {row.plan_task_id for row in tasks}
        ) != len(tasks):
            issues.append("graph_integrity_failure")
        if any(task.blocking_state not in SUPPORTED_BLOCKING_STATES for task in tasks):
            issues.append("unknown_gate_type")
        for edge in edges:
            if edge.source_dependency_type not in SUPPORTED_DEPENDENCY_TYPES:
                issues.append("unknown_dependency_type")
            elif (
                edge.runtime_class
                != DEPENDENCY_RUNTIME_CLASS_MAP[edge.source_dependency_type]
            ):
                issues.append("graph_integrity_failure")
            prerequisite = task_by_id.get(edge.prerequisite_execution_task_id)
            dependent = task_by_id.get(edge.dependent_execution_task_id)
            if (
                prerequisite is None
                or dependent is None
                or prerequisite.execution_plan_id != plan.id
                or dependent.execution_plan_id != plan.id
            ):
                issues.append("graph_integrity_failure")

        members_by_group: dict[int, list[ExecutionGroupMember]] = {}
        member_tasks: set[int] = set()
        groups_by_id = {group.id: group for group in groups}
        for member in members:
            members_by_group.setdefault(member.execution_group_id, []).append(member)
            group = groups_by_id.get(member.execution_group_id)
            if group is None or member.execution_task_id not in task_by_id:
                issues.append("graph_integrity_failure")
            elif task_by_id[member.execution_task_id].execution_plan_id != plan.id:
                issues.append("graph_integrity_failure")
            if member.execution_task_id in member_tasks:
                issues.append("graph_integrity_failure")
            member_tasks.add(member.execution_task_id)
        for group in groups:
            if (
                group.kind not in SUPPORTED_GROUP_KINDS
                or group.skip_policy not in SUPPORTED_GROUP_SKIP_POLICIES
            ):
                issues.append("unknown_gate_type")
            group_members = members_by_group.get(group.id, [])
            if not group_members or len(
                {item.member_order for item in group_members}
            ) != len(group_members):
                issues.append("graph_integrity_failure")

        graph_hash = canonical_json_hash(
            self._graph_payload(plan, tasks, edges, groups, members)
        )
        if not any(
            issue in {"unknown_dependency_type", "unknown_gate_type"}
            for issue in issues
        ):
            try:
                ExecutionPlanCommitService(self.db).verify_integrity(plan.id)
            except (ExecutionPlanCommitError, KeyError, TypeError, ValueError):
                issues.append("graph_integrity_failure")
        return _GraphSnapshot(
            plan=plan,
            tasks=tasks,
            edges=edges,
            groups=groups,
            members=members,
            task_by_id=task_by_id,
            graph_hash=graph_hash,
            graph_issues=tuple(sorted(set(issues))),
        )

    @staticmethod
    def _graph_payload(plan, tasks, edges, groups, members) -> dict[str, object]:
        return {
            "schema_version": EXECUTION_ELIGIBILITY_SCHEMA_VERSION,
            "execution_plan": {
                "id": plan.id,
                "project_id": plan.project_id,
                "planning_session_id": plan.planning_session_id,
                "planning_commit_manifest_id": plan.planning_commit_manifest_id,
                "generation": plan.generation,
                "protocol_version": plan.protocol_version,
                "source_commit_identity": plan.source_commit_identity,
                "source_plan_checkpoint_id": plan.source_plan_checkpoint_id,
                "source_plan_hash": plan.source_plan_hash,
            },
            "tasks": [
                {
                    "id": task.id,
                    "plan_task_id": task.plan_task_id,
                    "title": task.title,
                    "blocking_state": task.blocking_state,
                    "task_spec": task.task_spec,
                    "done_when": task.done_when,
                }
                for task in tasks
            ],
            "dependencies": [
                {
                    "id": edge.id,
                    "plan_dependency_id": edge.plan_dependency_id,
                    "prerequisite_execution_task_id": edge.prerequisite_execution_task_id,
                    "dependent_execution_task_id": edge.dependent_execution_task_id,
                    "source_dependency_type": edge.source_dependency_type,
                    "runtime_class": edge.runtime_class,
                    "rationale": edge.rationale,
                }
                for edge in edges
            ],
            "groups": [
                {
                    "id": group.id,
                    "plan_group_id": group.plan_group_id,
                    "kind": group.kind,
                    "order_index": group.order_index,
                    "parallel_limit": group.parallel_limit,
                    "skip_policy": group.skip_policy,
                }
                for group in groups
            ],
            "group_members": [
                {
                    "id": member.id,
                    "execution_group_id": member.execution_group_id,
                    "execution_task_id": member.execution_task_id,
                    "member_order": member.member_order,
                }
                for member in members
            ],
        }

    def _dependency_result(self, edge, task_by_id, lifecycle_heads):
        predecessor = task_by_id.get(edge.prerequisite_execution_task_id)
        if predecessor is None:
            return ExecutionDependencyResult(
                edge.id,
                edge.plan_dependency_id,
                edge.source_dependency_type,
                edge.runtime_class,
                edge.prerequisite_execution_task_id,
                None,
                None,
                None,
                None,
                "invalid",
                "graph_integrity_failure",
            )
        state = predecessor.status
        if state == "succeeded":
            if edge.source_dependency_type == "review_gate":
                result, reason = "review_pending", "review_gate_pending"
            else:
                result, reason = "satisfied", None
        elif state in {"pending", "ready", "running", "blocked", "paused"}:
            result, reason = "waiting", "waiting_on_dependencies"
        elif state == "awaiting_validation":
            result, reason = "waiting", "predecessor_awaiting_validation"
        elif state == "awaiting_recovery":
            result, reason = "waiting", "predecessor_awaiting_recovery"
        elif state == "failed":
            result, reason = "failed", "dependency_failed"
        elif state == "cancelled":
            result, reason = "failed", "dependency_cancelled"
        elif state == "skipped":
            result, reason = "not_satisfied", "dependency_skipped"
        else:
            result, reason = "invalid", "lifecycle_integrity_failure"
        return ExecutionDependencyResult(
            edge.id,
            edge.plan_dependency_id,
            edge.source_dependency_type,
            edge.runtime_class,
            predecessor.id,
            predecessor.plan_task_id,
            state,
            int(predecessor.state_version),
            lifecycle_heads.get(predecessor.id),
            result,
            reason,
        )

    @staticmethod
    def _gate_results(task, graph):
        if task.blocking_state == "review_required":
            gate_results = [
                ExecutionGateResult(
                    "review_gate",
                    f"task:{task.plan_task_id}",
                    "pending",
                    "review_gate_pending",
                    {"blocking_state": task.blocking_state},
                )
            ]
        elif task.blocking_state in {"blocking", "non_blocking"}:
            gate_results = []
        else:
            gate_results = [
                ExecutionGateResult(
                    "unknown",
                    f"task:{task.plan_task_id}",
                    "blocked",
                    "unknown_gate_type",
                    {"blocking_state": task.blocking_state},
                )
            ]
        for group in sorted(
            graph.groups, key=lambda item: (item.plan_group_id, item.id)
        ):
            matching = [
                member
                for member in graph.members
                if member.execution_group_id == group.id
                and member.execution_task_id == task.id
            ]
            for member in sorted(
                matching, key=lambda item: (item.member_order, item.id)
            ):
                gate_results.append(
                    ExecutionGateResult(
                        "execution_group",
                        group.plan_group_id,
                        "metadata_only",
                        None,
                        {
                            "kind": group.kind,
                            "member_order": member.member_order,
                            "group_member_count": sum(
                                item.execution_group_id == group.id
                                for item in graph.members
                            ),
                        },
                    )
                )
        return tuple(gate_results)

    @staticmethod
    def _ordered_blockers(
        graph_issues, lifecycle_issues, dependency_results, gate_results
    ):
        blockers: list[str] = []

        def add_once(reason):
            if reason not in blockers:
                blockers.append(reason)

        for reason in sorted(set(graph_issues)):
            add_once(reason)
        if lifecycle_issues:
            add_once("lifecycle_integrity_failure")
        for result in dependency_results:
            if result.reason_code is not None and result.result != "satisfied":
                add_once(result.reason_code)
        for result in gate_results:
            if result.reason_code is not None:
                add_once(result.reason_code)
        return tuple(blockers)

    @staticmethod
    def _primary_blocker(blockers):
        precedence = {
            "graph_integrity_failure": 0,
            "lifecycle_integrity_failure": 0,
            "execution_plan_inactive": 1,
            "task_state_not_reconcilable": 2,
            "unknown_dependency_type": 3,
            "unknown_gate_type": 3,
            "dependency_failed": 4,
            "dependency_cancelled": 4,
            "dependency_skipped": 4,
            "predecessor_awaiting_validation": 6,
            "predecessor_awaiting_recovery": 6,
            "review_gate_pending": 5,
            "manual_gate_pending": 5,
            "resource_gate_pending": 5,
            "group_gate_pending": 5,
            "waiting_on_dependencies": 6,
        }
        return min(
            blockers,
            key=lambda reason: (
                precedence.get(reason, 99),
                blockers.index(reason),
                reason,
            ),
        )

    @staticmethod
    def _integrity_reason(graph_issues, lifecycle_issues):
        if "graph_integrity_failure" in graph_issues:
            return "graph_integrity_failure"
        if lifecycle_issues:
            return "lifecycle_integrity_failure"
        return sorted(graph_issues)[0]

    @staticmethod
    def _append_once(values, value):
        return tuple(values) if value in values else tuple(values) + (value,)

    def _lifecycle_head(self, task_id: int) -> str | None:
        event = (
            self.db.query(ExecutionTaskTransition)
            .filter(ExecutionTaskTransition.execution_task_id == task_id)
            .order_by(ExecutionTaskTransition.sequence.desc())
            .first()
        )
        return event.event_hash if event is not None else None

    @staticmethod
    def _assert_expected(decision, expected_state, expected_version):
        if decision.evaluated_state != expected_state:
            raise ExecutionEligibilityError(
                "eligibility_target_state_stale",
                "expected target state does not match the evaluated projection",
                decision,
            )
        if decision.evaluated_state_version != int(expected_version):
            raise ExecutionEligibilityError(
                "eligibility_target_version_stale",
                "expected target state version does not match the evaluated version",
                decision,
            )

    @staticmethod
    def _action(decision):
        if decision.reason_code in NON_MUTATING_REASONS:
            return None
        if decision.evaluated_state in {"pending", "blocked"} and decision.eligible:
            return "ready", "dependencies_satisfied"
        if (
            decision.evaluated_state in {"pending", "ready"}
            and not decision.eligible
            and decision.reason_code in REGRESSION_BLOCKERS
        ):
            reason = {
                "waiting_on_dependencies": "dependency_blocked",
                "predecessor_awaiting_validation": "dependency_blocked",
                "predecessor_awaiting_recovery": "dependency_blocked",
                "dependency_failed": "dependency_failed",
                "dependency_cancelled": "dependency_cancelled",
                "dependency_skipped": "dependency_skipped",
                "review_gate_pending": "review_gate_pending",
                "manual_gate_pending": "manual_gate_pending",
                "resource_gate_pending": "resource_gate_pending",
                "group_gate_pending": "group_gate_pending",
            }[decision.reason_code]
            return "blocked", reason
        return None

    @staticmethod
    def _fences(decision):
        by_task: dict[int, ExecutionDependencyResult] = {}
        for result in decision.dependency_results:
            if result.predecessor_state is not None:
                by_task[result.prerequisite_execution_task_id] = result
        return tuple(
            ExecutionTaskLifecycleFence(
                execution_task_id=task_id,
                expected_state=result.predecessor_state,
                expected_state_version=int(result.predecessor_state_version),
                lifecycle_head_hash=result.predecessor_lifecycle_head_hash,
            )
            for task_id, result in sorted(by_task.items())
        )

    def _assert_predecessor_fence(self, decision):
        for fence in self._fences(decision):
            predecessor = self.db.get(ExecutionTask, fence.execution_task_id)
            if (
                predecessor is None
                or predecessor.execution_plan_id != decision.execution_plan_id
                or predecessor.status != fence.expected_state
                or int(predecessor.state_version) != fence.expected_state_version
                or self._lifecycle_head(fence.execution_task_id)
                != fence.lifecycle_head_hash
            ):
                raise ExecutionEligibilityError(
                    "eligibility_predecessor_stale",
                    "a predecessor changed during eligibility reconciliation",
                    decision,
                )

    @staticmethod
    def _reason_detail(decision):
        fences = [
            {
                "execution_task_id": fence.execution_task_id,
                "expected_state": fence.expected_state,
                "expected_state_version": fence.expected_state_version,
                "lifecycle_head_hash": fence.lifecycle_head_hash,
            }
            for fence in ExecutionEligibilityService._fences(decision)
        ]
        payload = {
            "schema_version": EXECUTION_ELIGIBILITY_SCHEMA_VERSION,
            "decision_hash": decision.decision_hash,
            "graph_hash": decision.graph_hash,
            "reason_code": decision.reason_code,
            "blockers": list(decision.blockers),
            "predecessor_fences": fences,
        }
        detail = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(detail) <= 1024:
            return detail
        return json.dumps(
            {
                "schema_version": EXECUTION_ELIGIBILITY_SCHEMA_VERSION,
                "decision_hash": decision.decision_hash,
                "graph_hash": decision.graph_hash,
                "reason_code": decision.reason_code,
                "evidence_hash": canonical_json_hash(payload),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _transition_command_id(
        key, task_id, expected_state, expected_version, decision
    ):
        key_hash = canonical_json_hash({"idempotency_key": key})
        request_hash = canonical_json_hash(
            {
                "execution_task_id": task_id,
                "expected_state": expected_state,
                "expected_state_version": expected_version,
            }
        )
        action = ExecutionEligibilityService._action(decision)
        to_state = action[0] if action else "none"
        return (
            f"eligibility:{key_hash[:40]}:{request_hash[:16]}:"
            f"{decision.decision_hash[:16]}:{to_state}"
        )

    def _find_reconciliation_event(self, task_id, actor_id, key):
        key_hash = canonical_json_hash({"idempotency_key": key})[:40]
        prefix = f"eligibility:{key_hash}:"
        events = (
            self.db.query(ExecutionTaskTransition)
            .filter(
                ExecutionTaskTransition.actor_type == "system",
                ExecutionTaskTransition.actor_id == actor_id,
                ExecutionTaskTransition.command_id.like(f"{prefix}%"),
            )
            .order_by(ExecutionTaskTransition.id.asc())
            .all()
        )
        return events[0] if events else None

    def _replay_existing(self, event, task_id, expected_state, expected_version):
        if (
            event.execution_task_id != task_id
            or event.from_state != expected_state
            or event.expected_version != expected_version
        ):
            raise ExecutionEligibilityError(
                "eligibility_idempotency_conflict",
                "idempotency key is bound to a different reconciliation request",
            )
        self._transition.verify_task_lifecycle_integrity(task_id)
        decision = self.evaluate_task(task_id)
        return EligibilityReconciliationResult(
            decision=decision,
            transition=self._transition_result(event, replayed=True),
            no_op=False,
            replayed=True,
        )

    @staticmethod
    def _transition_result(event, *, replayed):
        return ExecutionTaskTransitionResult(
            execution_task_id=event.execution_task_id,
            execution_plan_id=event.execution_plan_id,
            plan_task_id=event.plan_task_id,
            event_id=event.id,
            sequence=event.sequence,
            from_state=event.from_state,
            to_state=event.to_state,
            expected_version=event.expected_version,
            resulting_version=event.resulting_version,
            event_hash=event.event_hash,
            replayed=replayed,
        )
