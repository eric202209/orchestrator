"""Offline, read-only loader for retained Phase 40B-40C benchmark evidence.

Sources, all opened read-only:
- the development SQLite database (``mode=ro&immutable=1``);
- per-project control directories (event journals, ChangeSet manifests,
  planning provider-response evidence, discovery contracts);
- git-ignored phase evidence under ``docs/roadmap/reports/evidence``;
- git metadata (``rev-parse``/``log``) for seed and source identity checks.

No network, provider, or repository mutation occurs. Missing artifacts are
reported, never reconstructed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import classifier
from .schema import ATTEMPT_SCHEMA_VERSION, validate_attempt, validate_case

BENCHMARK_DIR = Path(__file__).resolve().parent
CASES_DIR = BENCHMARK_DIR / "cases"
REGISTRY_PATH = BENCHMARK_DIR / "history_registry.json"
DATABASE_RELATIVE = Path("database/orchestrator-dev.sqlite3")
CONTROL_RELATIVE = Path("runtime/control/projects")
WORKSPACE_RELATIVE = Path("workspace")
RETENTION_POLICY = "hold_all"


@dataclass(frozen=True)
class EvidenceRoots:
    repo_root: Path
    evidence_root: Path
    runtime_root: Path

    @property
    def database(self) -> Path:
        return self.runtime_root / DATABASE_RELATIVE

    @property
    def control(self) -> Path:
        return self.runtime_root / CONTROL_RELATIVE

    @property
    def workspaces(self) -> Path:
        return self.runtime_root / WORKSPACE_RELATIVE


@dataclass
class HistoryResult:
    attempts: list[dict[str, Any]]
    inventory: dict[str, Any]
    conflicts: list[dict[str, Any]]
    case_errors: dict[str, list[str]] = field(default_factory=dict)


def load_cases(cases_dir: Path = CASES_DIR) -> dict[str, dict[str, Any]]:
    cases = {}
    for path in sorted(cases_dir.glob("*.json")):
        cases[json.loads(path.read_text(encoding="utf-8"))["case_id"]] = json.loads(
            path.read_text(encoding="utf-8")
        )
    return cases


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _git(repo: Path, *args: str) -> str | None:
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _parse_db_time(value: Any) -> float | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _parse_iso(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class _Database:
    def __init__(self, path: Path):
        self.available = path.is_file()
        self._conn = (
            sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            if self.available
            else None
        )
        if self._conn is not None:
            self._conn.row_factory = sqlite3.Row

    def row(self, sql: str, *params: Any) -> dict[str, Any] | None:
        if self._conn is None:
            return None
        found = self._conn.execute(sql, params).fetchone()
        return dict(found) if found is not None else None

    def rows(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        return [dict(found) for found in self._conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()


def _read_events(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        details = raw.get("details") or {}
        event = {"type": raw.get("event_type"), "timestamp": raw.get("timestamp")}
        if event["type"] in {"phase_started", "phase_finished"}:
            event.update(phase=details.get("phase"), status=details.get("status"))
        elif event["type"] == "validation_result":
            event.update(
                stage=details.get("stage"),
                status=details.get("status"),
                reasons=[str(reason) for reason in details.get("reasons") or []],
            )
        elif event["type"] == "planning_repair_arbitration":
            event.update(outcome=details.get("outcome"))
        elif event["type"] == "debug_feedback_captured":
            envelope = details.get("debug_feedback_envelope") or {}
            event.update(
                failure_class=details.get("debug_failure_class"),
                eligible=envelope.get("eligible_for_debug_repair"),
            )
        elif event["type"] == "repair_rejected":
            event.update(
                reason=details.get("debug_repair_rejection_reason")
                or details.get("reason")
            )
        elif event["type"] == "step_finished":
            event.update(
                status=details.get("status"), error=str(details.get("error") or "")
            )
        elif event["type"] == "task_failed":
            event.update(error=details.get("error"))
        elif event["type"] == "recovery_decision_routed":
            event.update(
                strategy=details.get("strategy"),
                failure_class=details.get("failure_class"),
            )
        elif event["type"] == "planning_context_provenance":
            materialization = details.get("planner_source_materialization") or {}
            event.update(
                materialized_file_count=materialization.get("materialized_file_count"),
                expected_file_count=materialization.get("expected_file_count"),
                unavailable_reasons=materialization.get("unavailable_reasons"),
                truncated_paths=sorted(
                    item.get("relative_path")
                    for item in materialization.get("files") or []
                    if item.get("truncated")
                ),
            )
        events.append(event)
    return events


def _provider_responses(
    project_dir: Path, start: float | None, end: float | None
) -> list[dict[str, Any]]:
    """Planning provider responses whose own request time falls in the run window."""

    found = []
    for path in sorted(project_dir.glob("planning-evidence/*/provider-response.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        started = _parse_iso((data.get("timing") or {}).get("request_started_utc"))
        if (
            start is None
            or end is None
            or started is None
            or not (start - 1 <= started <= end + 1)
        ):
            continue
        validation = data.get("plan_validation") or {}
        found.append(
            {
                "path": path,
                "started": started,
                "role": (data.get("provider") or {}).get("role"),
                "provider_call_id": (data.get("correlation") or {}).get(
                    "provider_call_id"
                ),
                "parse_status": (data.get("planning_parse") or {}).get("status"),
                "schema_status": (validation.get("schema_validation") or {}).get(
                    "status"
                ),
                "final_status": (validation.get("final_plan_validation") or {}).get(
                    "status"
                ),
            }
        )
    return sorted(found, key=lambda item: item["started"])


def parse_acceptance_record(path: Path, record_format: str) -> dict[str, Any]:
    """Parse a retained independent-acceptance record without executing anything."""

    text = path.read_text(encoding="utf-8")
    summary = next(
        (
            line
            for line in reversed(text.splitlines())
            if re.search(r"\d+ (passed|failed|errors?)", line)
        ),
        "",
    )
    counts = {
        key: int(match.group(1))
        for key in ("passed", "failed", "error")
        if (match := re.search(rf"\b(\d+) {key}s?\b", summary))
    }
    if record_format == "key_values_then_pytest":
        values = dict(
            (match.group(1), match.group(2).strip())
            for match in re.finditer(
                r"^(command|exit_code|tracked_diff|test_diff):[ \t]*(.*)$",
                text,
                re.MULTILINE,
            )
        )

        def paths(raw: str | None) -> list[str]:
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
                return (
                    sorted(str(item) for item in parsed)
                    if isinstance(parsed, list)
                    else [str(parsed)]
                )
            except json.JSONDecodeError:
                return sorted(part for part in re.split(r"[\s,]+", raw) if part)

        exit_code = (
            int(values["exit_code"])
            if values.get("exit_code", "").lstrip("-").isdigit()
            else None
        )
        source_paths = paths(values.get("tracked_diff"))
        test_paths = paths(values.get("test_diff"))
        command = values.get("command")
    elif record_format == "unified_diff_then_pytest":
        diff_paths = sorted(
            set(re.findall(r"^diff --git a/(\S+) b/", text, re.MULTILINE))
        )
        test_paths = [item for item in diff_paths if item.startswith("tests/")]
        source_paths = [item for item in diff_paths if not item.startswith("tests/")]
        exit_code = None
        command = None
    else:
        raise ValueError(f"unknown acceptance record format: {record_format}")

    if exit_code is not None:
        passed = exit_code == 0
    else:
        passed = (
            bool(counts.get("passed"))
            and not counts.get("failed")
            and not counts.get("error")
        )
    return {
        "result": "passed" if passed else "failed",
        "command": command,
        "exit_code": exit_code,
        "passed": counts.get("passed"),
        "failed": counts.get("failed"),
        "errors": counts.get("error"),
        "source_paths": source_paths,
        "test_paths": test_paths,
    }


def _under(path: str, prefixes: list[str]) -> bool:
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix) for prefix in prefixes
    )


def _empty_attempt(
    case_id: str, phase: dict[str, Any], slot: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "attempted": False,
        "not_attempted_reason": slot.get("not_attempted_reason"),
        "identity": {
            "case_id": case_id,
            "attempt_id": f"{phase['phase']}:{case_id}:{slot['slot']}",
            "phase": phase["phase"],
            "slot": slot["slot"],
            "project_id": None,
            "task_id": None,
            "task_execution_id": None,
            "session_id": None,
            "source_identity": dict(phase["source_identity"]),
            "runtime_identity": {},
            "provider_identity": dict(phase.get("provider") or {}),
        },
        "planning": {
            "reached": None,
            "discovery_status": None,
            "grounding_status": None,
            "provider_response_status": None,
            "json_parse_result": None,
            "schema_validation_result": None,
            "plan_validation_result": None,
            "repair_count": None,
            "outcome": None,
        },
        "execution": {
            "reached": None,
            "steps_executed": None,
            "source_mutation_captured": None,
            "debug_feedback_count": None,
            "debug_repair_count": None,
            "debug_repair_rejections": None,
            "verification_result": None,
            "task_status": None,
            "task_execution_status": None,
        },
        "change_set": {
            "exists": None,
            "identity": None,
            "changed_paths": None,
            "retention_policy": None,
            "promoted": None,
        },
        "acceptance": {
            "attempted": None,
            "result": None,
            "command": None,
            "passed": None,
            "failed": None,
            "errors": None,
            "tests_modified": None,
            "unauthorized_source_mutation": None,
            "evidence_ref": None,
            "method": None,
            "source_paths": None,
        },
        "reliability": {
            "failure_stage": "not_attempted",
            "failure_category_raw": None,
            "error_message_raw": None,
            "observed_errors": [],
            "safety_rejections": [],
            "terminal_safety_rejections": [],
            "verification_failure": [],
            "attribution": {"basis": "not_applicable", "kinds": [], "root_causes": []},
            "execution_authority_violation": None,
            "retry_count": None,
            "recovery_count": None,
            "duration_seconds": None,
            "independently_accepted": None,
            "evidence_completeness": None,
            "evidence_limitations": [],
            "conflicts": [],
            "evidence_refs": [],
        },
    }


class HistoryLoader:
    def __init__(
        self,
        roots: EvidenceRoots,
        registry: dict[str, Any],
        cases: dict[str, dict[str, Any]],
    ):
        self.roots = roots
        self.registry = registry
        self.cases = cases
        self.db = _Database(roots.database)
        self._commit_times: dict[str, int | None] = {}

    # -- identity helpers -------------------------------------------------
    def _commit_time(self, sha: str) -> int | None:
        if sha not in self._commit_times:
            value = _git(self.roots.repo_root, "log", "-1", "--format=%ct", sha)
            self._commit_times[sha] = int(value) if value and value.isdigit() else None
        return self._commit_times[sha]

    def _source_identity_conflicts(
        self, phase: dict[str, Any], created_at: float | None
    ) -> list[dict[str, Any]]:
        identity = phase["source_identity"]
        lower = self._commit_time(identity["entering_head"])
        upper = (
            self._commit_time(identity["committed_as"])
            if identity.get("committed_as")
            else None
        )
        if created_at is None or lower is None:
            return [
                {
                    "code": "source_identity_unverifiable",
                    "detail": "task creation time or entering HEAD commit time unavailable",
                }
            ]
        if created_at < lower or (upper is not None and created_at > upper):
            return [
                {
                    "code": "source_identity_time_mismatch",
                    "detail": (
                        f"task created at {created_at:.0f} outside [{lower}, {upper}] for "
                        f"entering_head {identity['entering_head'][:12]}"
                    ),
                }
            ]
        return []

    # -- per attempt ------------------------------------------------------
    def _attempt(self, phase: dict[str, Any], slot: dict[str, Any]) -> dict[str, Any]:
        case_id = slot["case_id"]
        case = self.cases.get(case_id) or {}
        record = _empty_attempt(case_id, phase, slot)
        if slot.get("task_id") is None:
            return record

        reliability = record["reliability"]
        conflicts: list[dict[str, Any]] = []
        limitations: list[dict[str, Any]] = [
            dict(item) for item in slot.get("known_evidence_limitations") or []
        ]
        refs: list[str] = []
        present: dict[str, bool] = {}
        task_id = int(slot["task_id"])
        record["attempted"] = True
        record["not_attempted_reason"] = None
        record["identity"]["task_id"] = task_id

        task = self.db.row("select * from tasks where id=?", task_id)
        executions = self.db.rows(
            "select * from task_executions where task_id=? order by id", task_id
        )
        present["db_task"] = task is not None
        present["db_task_execution"] = bool(executions)
        if task is None:
            limitations.append(
                {
                    "code": "db_task_missing",
                    "statement": f"Task {task_id} not in development database",
                }
            )
        execution = executions[-1] if executions else None
        if len(executions) > 1:
            conflicts.append(
                {
                    "code": "multiple_task_executions",
                    "detail": [row["id"] for row in executions],
                }
            )

        project_id = task["project_id"] if task else slot.get("project_id")
        if (
            task
            and slot.get("project_id") is not None
            and task["project_id"] != slot["project_id"]
        ):
            conflicts.append(
                {
                    "code": "project_id_mismatch",
                    "detail": {
                        "registry": slot["project_id"],
                        "database": task["project_id"],
                    },
                }
            )
        record["identity"].update(
            project_id=project_id,
            task_execution_id=execution["id"] if execution else None,
            session_id=execution["session_id"] if execution else None,
            runtime_identity={
                "configuration_fingerprint": (
                    execution["configuration_fingerprint"] if execution else None
                ),
                "runtime": "isolated development runtime (.dev-runtime/phase40b-r1)",
                "review_policy": RETENTION_POLICY,
            },
        )
        record["identity"]["provider_identity"].update(
            planner_model=execution["planner_model"] if execution else None,
            executor_model=execution["executor_model"] if execution else None,
            planning_backend=execution["planning_backend"] if execution else None,
            execution_backend=execution["execution_backend"] if execution else None,
        )
        if execution and phase.get("provider", {}).get("model") not in (
            None,
            execution["planner_model"],
        ):
            conflicts.append(
                {
                    "code": "provider_model_mismatch",
                    "detail": {
                        "registry": phase["provider"]["model"],
                        "database": execution["planner_model"],
                    },
                }
            )

        created = _parse_db_time(task["created_at"]) if task else None
        conflicts.extend(self._source_identity_conflicts(phase, created))
        reported = slot.get("reported") or {}
        if (
            task
            and reported.get("task_status")
            and reported["task_status"] != task["status"]
        ):
            conflicts.append(
                {
                    "code": "reported_task_status_mismatch",
                    "detail": {
                        "report": reported["task_status"],
                        "database": task["status"],
                    },
                }
            )

        # Workspace seed identity.
        if task and case:
            project = self.db.row(
                "select workspace_path from projects where id=?", task["project_id"]
            )
            workspace = self.roots.workspaces / str(
                (project or {}).get("workspace_path") or ""
            )
            head = _git(workspace, "rev-parse", "HEAD") if project else None
            record["identity"]["source_identity"]["workspace_seed_commit"] = head
            if head is None:
                limitations.append(
                    {
                        "code": "workspace_seed_unverifiable",
                        "statement": f"workspace {workspace.name} unavailable",
                    }
                )
            elif head != case["seed"]["commit"]:
                conflicts.append(
                    {
                        "code": "workspace_seed_mismatch",
                        "detail": {
                            "case_seed": case["seed"]["commit"],
                            "workspace_head": head,
                        },
                    }
                )

        # Event journal.
        project_dir = self.roots.control / str(project_id)
        journal = None
        if execution:
            journal = (
                project_dir
                / "events"
                / f"session_{execution['session_id']}_task_{task_id}.jsonl"
            )
        events = _read_events(journal) if journal else None
        present["event_journal"] = events is not None
        if journal and events is not None:
            refs.append(_rel(journal, self.roots.repo_root))
        if events is None:
            limitations.append(
                {
                    "code": "event_journal_missing",
                    "statement": "orchestration event journal not found",
                }
            )

        # ChangeSet manifest.
        manifest_path = (
            project_dir / "change-sets" / str(execution["id"]) / "manifest.json"
            if execution
            else None
        )
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path is not None and manifest_path.is_file()
            else None
        )
        present["change_set_manifest"] = manifest is not None
        change_paths: list[str] | None = None
        if manifest is not None:
            if (
                manifest.get("task_execution_id") != execution["id"]
                or manifest.get("task_id") != task_id
            ):
                conflicts.append(
                    {
                        "code": "change_set_identity_mismatch",
                        "detail": _rel(manifest_path, self.roots.repo_root),
                    }
                )
            change_paths = sorted(
                set(manifest.get("copied_files") or [])
                | set(manifest.get("deleted_files") or [])
            )
            refs.append(_rel(manifest_path, self.roots.repo_root))
            record["change_set"] = {
                "exists": True,
                "identity": {
                    "manifest": _rel(manifest_path, self.roots.repo_root),
                    "manifest_sha256": _sha256(manifest_path),
                    "task_execution_id": manifest.get("task_execution_id"),
                },
                "changed_paths": change_paths,
                "retention_policy": RETENTION_POLICY,
                "promoted": bool(task and task.get("promoted_at")),
            }
        else:
            limitations.append(
                {
                    "code": "change_set_manifest_missing",
                    "statement": "ChangeSet manifest not found",
                }
            )
            record["change_set"].update(exists=False if execution else None)

        # Provider responses, correlated by request time within the execution window.
        start = _parse_db_time(execution["started_at"]) if execution else None
        end = _parse_db_time(execution["completed_at"]) if execution else None
        responses = _provider_responses(project_dir, start, end)
        planning_reached = (
            None
            if events is None
            else any(
                event["type"] == "phase_started" and event.get("phase") == "planning"
                for event in events
            )
        )
        present["raw_provider_response"] = bool(responses)
        if planning_reached and not responses:
            if not any(
                item["code"] == "provider_response_missing" for item in limitations
            ):
                limitations.append(
                    {
                        "code": "provider_response_missing",
                        "statement": "no retained raw Planning provider response for this execution",
                    }
                )
        refs.extend(_rel(item["path"], self.roots.repo_root) for item in responses)

        # Planning section.
        plan_validations = [
            event
            for event in events or []
            if event["type"] == "validation_result" and event.get("stage") == "plan"
        ]
        provenance = next(
            (
                event
                for event in events or []
                if event["type"] == "planning_context_provenance"
            ),
            None,
        )
        discovery = sorted(
            project_dir.glob(
                f"discovery-contract-capture/session_*_task_{task_id}_execution_*.json"
            )
        )
        discovery_status = None
        if discovery:
            data = json.loads(discovery[-1].read_text(encoding="utf-8"))
            action = data.get("action") or {}
            discovery_status = f"{action.get('action')}:{'admitted' if action.get('validation_pass') else 'rejected'}"
            refs.append(_rel(discovery[-1], self.roots.repo_root))
        planning_finished = [
            event
            for event in events or []
            if event["type"] == "phase_finished" and event.get("phase") == "planning"
        ]
        planning_outcome = None
        if events is not None:
            if any(event.get("status") == "accepted" for event in planning_finished):
                planning_outcome = "accepted"
            elif planning_finished:
                planning_outcome = planning_finished[-1].get("status")
            elif planning_reached:
                planning_outcome = "terminated_before_planning_finished"
            else:
                planning_outcome = "not_reached"
        record["planning"] = {
            "reached": planning_reached,
            "discovery_status": discovery_status,
            "grounding_status": (
                None
                if provenance is None
                or provenance.get("materialized_file_count") is None
                else {
                    "materialized_file_count": provenance.get(
                        "materialized_file_count"
                    ),
                    "expected_file_count": provenance.get("expected_file_count"),
                    "truncated_paths": provenance.get("truncated_paths"),
                    "unavailable_reasons": provenance.get("unavailable_reasons"),
                }
            ),
            "provider_response_status": (
                "retained" if responses else ("missing" if planning_reached else None)
            ),
            "json_parse_result": (
                [item["parse_status"] for item in responses] if responses else None
            ),
            "schema_validation_result": (
                [item["schema_status"] for item in responses] if responses else None
            ),
            "plan_validation_result": (
                plan_validations[-1].get("status") if plan_validations else None
            ),
            "repair_count": (
                None
                if events is None
                else sum(
                    1
                    for event in events
                    if event["type"] == "planning_repair_arbitration"
                )
            ),
            "outcome": planning_outcome,
        }

        # Execution section.
        steps = [event for event in events or [] if event["type"] == "step_finished"]
        executing = (
            None
            if events is None
            else any(
                event["type"] == "phase_started" and event.get("phase") == "executing"
                for event in events
            )
        )
        completed = any(event["type"] == "task_completed" for event in events or [])
        record["execution"] = {
            "reached": executing,
            "steps_executed": None if events is None else len(steps),
            "source_mutation_captured": (
                None if change_paths is None else bool(change_paths)
            ),
            "debug_feedback_count": (
                None
                if events is None
                else sum(
                    1 for event in events if event["type"] == "debug_feedback_captured"
                )
            ),
            "debug_repair_count": (
                None
                if events is None
                else sum(
                    1 for event in events if event["type"] == "debug_repair_attempted"
                )
            ),
            "debug_repair_rejections": (
                None
                if events is None
                else [
                    event.get("reason")
                    for event in events
                    if event["type"] == "repair_rejected"
                ]
            ),
            "verification_result": (
                None
                if not executing
                else (
                    "passed"
                    if completed
                    and not any(step.get("status") == "failed" for step in steps)
                    else "failed"
                )
            ),
            "task_status": task["status"] if task else None,
            "task_execution_status": execution["status"] if execution else None,
        }

        # Acceptance.
        acceptance_spec = slot.get("acceptance")
        acceptance = record["acceptance"]
        acceptance.update(attempted=False)
        if acceptance_spec:
            path = self.roots.repo_root / acceptance_spec["record"]
            present["acceptance_record"] = path.is_file()
            if path.is_file():
                parsed = parse_acceptance_record(path, acceptance_spec["format"])
                authorized = case.get("authorized_source_paths") or []
                acceptance.update(
                    attempted=True,
                    result=parsed["result"],
                    command=parsed["command"],
                    passed=parsed["passed"],
                    failed=parsed["failed"],
                    errors=parsed["errors"],
                    tests_modified=bool(parsed["test_paths"]),
                    unauthorized_source_mutation=any(
                        not _under(item, authorized) for item in parsed["source_paths"]
                    ),
                    evidence_ref=acceptance_spec["record"],
                    method=acceptance_spec.get("method"),
                    source_paths=parsed["source_paths"],
                )
                refs.append(acceptance_spec["record"])
                if (
                    change_paths is not None
                    and sorted(parsed["source_paths"]) != change_paths
                ):
                    conflicts.append(
                        {
                            "code": "acceptance_paths_differ_from_change_set",
                            "detail": {
                                "acceptance": parsed["source_paths"],
                                "change_set": change_paths,
                            },
                        }
                    )
                if parsed["command"] is None:
                    limitations.append(
                        {
                            "code": "acceptance_command_not_retained",
                            "statement": "acceptance record does not state its command",
                        }
                    )
                if parsed["passed"] is None and parsed["failed"] is None:
                    limitations.append(
                        {
                            "code": "acceptance_counts_unparsed",
                            "statement": "no pytest summary counts in record",
                        }
                    )
            else:
                acceptance.update(attempted=None)
                limitations.append(
                    {
                        "code": "acceptance_record_missing",
                        "statement": acceptance_spec["record"],
                    }
                )
        elif change_paths:
            present["acceptance_record"] = False
            acceptance.update(attempted=None)
            limitations.append(
                {
                    "code": "acceptance_record_missing",
                    "statement": "candidate ChangeSet exists but no acceptance record is registered",
                }
            )
        if reported.get("independent_acceptance") in {
            "passed",
            "failed",
        } and acceptance.get("result") not in {
            reported["independent_acceptance"],
            None,
        }:
            conflicts.append(
                {
                    "code": "reported_acceptance_mismatch",
                    "detail": {
                        "report": reported["independent_acceptance"],
                        "record": acceptance.get("result"),
                    },
                }
            )

        # Reliability and classification.
        texts = [str(task.get("error_message") or "") if task else ""]
        texts.extend(
            reason
            for event in plan_validations
            for reason in event.get("reasons") or []
        )
        texts.extend(
            str(event.get("reason") or "")
            for event in events or []
            if event["type"] == "repair_rejected"
        )
        texts.extend(
            str(event.get("error") or "")
            for event in events or []
            if event["type"] in {"task_failed"}
        )
        step_texts = [
            step.get("error") or "" for step in steps if step.get("status") == "failed"
        ]
        rejections = classifier.safety_rejections(texts)
        terminal_texts = [texts[0]] + [
            str(event.get("reason") or "")
            for event in events or []
            if event["type"] == "repair_rejected"
        ]
        terminal_rejections = (
            []
            if task and task["status"] == "DONE"
            else classifier.safety_rejections(terminal_texts)
        )
        verification = (
            classifier.verification_failures(step_texts + [texts[0]])
            if executing
            else []
        )
        facts = {
            "attempted": True,
            "events": events,
            "task_status": task["status"] if task else None,
            "acceptance_result": acceptance.get("result"),
            "safety_rejections": rejections,
        }
        stage = classifier.failure_stage(facts)
        facts["stage"] = stage
        root_causes = [dict(cause) for cause in slot.get("root_causes") or []]
        observed = sorted(
            {
                str(event.get("error"))
                for event in events or []
                if event["type"] == "task_failed" and event.get("error")
            }
            | {
                f"debug_repair_rejected:{event.get('reason')}"
                for event in events or []
                if event["type"] == "repair_rejected"
            }
            | {
                f"debug_failure_class:{event.get('failure_class')}"
                for event in events or []
                if event["type"] == "debug_feedback_captured"
            }
        )
        recovery_events = [
            event
            for event in events or []
            if event["type"] == "recovery_decision_routed"
        ]
        expected = [
            "db_task",
            "db_task_execution",
            "event_journal",
            "change_set_manifest",
            "raw_provider_response",
        ]
        if acceptance_spec or change_paths:
            expected.append("acceptance_record")
        reliability.update(
            failure_stage=stage,
            failure_category_raw=execution["failure_category"] if execution else None,
            error_message_raw=(task.get("error_message") if task else None),
            observed_errors=observed,
            safety_rejections=rejections,
            terminal_safety_rejections=terminal_rejections,
            verification_failure=verification,
            attribution=classifier.attribution(facts, root_causes),
            execution_authority_violation=classifier.execution_authority_violation(
                root_causes, True
            ),
            retry_count=(
                (
                    sum(int(row["attempt_number"] or 1) - 1 for row in executions)
                    + max(len(executions) - 1, 0)
                )
                if executions
                else None
            ),
            recovery_count=(
                None
                if events is None
                else sum(
                    1
                    for event in recovery_events
                    if event.get("strategy") != "terminal"
                )
            ),
            duration_seconds=(
                round(end - start, 3) if start is not None and end is not None else None
            ),
            independently_accepted=classifier.independently_accepted(
                attempted=True,
                task_status=task["status"] if task else None,
                change_set_paths=change_paths,
                acceptance=acceptance,
            ),
            evidence_completeness={
                "expected": sorted(expected),
                "present": sorted(name for name in expected if present.get(name)),
                "missing": sorted(name for name in expected if not present.get(name)),
                "ratio": round(
                    sum(1 for name in expected if present.get(name)) / len(expected), 4
                ),
            },
            evidence_limitations=sorted(limitations, key=lambda item: item["code"]),
            conflicts=sorted(conflicts, key=lambda item: item["code"]),
            evidence_refs=sorted(set(refs)),
        )
        return record

    # -- whole history ----------------------------------------------------
    def load(self) -> HistoryResult:
        attempts = []
        for phase in self.registry["phases"]:
            for slot in phase["slots"]:
                attempt = self._attempt(phase, slot)
                errors = validate_attempt(attempt)
                if errors:
                    raise ValueError(
                        f"attempt {attempt['identity']['attempt_id']} failed schema validation: {errors}"
                    )
                attempts.append(attempt)
        case_errors = {
            case_id: validate_case(case) for case_id, case in sorted(self.cases.items())
        }
        conflicts = [
            {"attempt_id": attempt["identity"]["attempt_id"], **conflict}
            for attempt in attempts
            for conflict in attempt["reliability"]["conflicts"]
        ]
        conflicts.extend(self._case_conflicts())
        inventory = self._inventory(attempts)
        self.db.close()
        return HistoryResult(
            attempts=attempts,
            inventory=inventory,
            conflicts=conflicts,
            case_errors=case_errors,
        )

    def _case_conflicts(self) -> list[dict[str, Any]]:
        conflicts = []
        for case_id, case in sorted(self.cases.items()):
            origin = self.db.row(
                "select description from tasks where id=?",
                case["original_task"]["origin_task_id"],
            )
            if (
                origin is not None
                and origin["description"] != case["original_task"]["description"]
            ):
                conflicts.append(
                    {
                        "case_id": case_id,
                        "code": "case_description_differs_from_origin_task",
                    }
                )
            repo = self.roots.workspaces / case["seed"]["repository"]
            head = _git(repo, "rev-parse", f"{case['seed']['commit']}^{{commit}}")
            tree = _git(repo, "rev-parse", f"{case['seed']['commit']}^{{tree}}")
            if head is None:
                conflicts.append(
                    {"case_id": case_id, "code": "canonical_seed_unavailable"}
                )
            elif tree != case["seed"]["tree"]:
                conflicts.append(
                    {
                        "case_id": case_id,
                        "code": "canonical_seed_tree_mismatch",
                        "detail": tree,
                    }
                )
        return conflicts

    def _inventory(self, attempts: list[dict[str, Any]]) -> dict[str, Any]:
        phases = []
        for phase in self.registry["phases"]:
            evidence_dir = self.roots.repo_root / phase["evidence_dir"]
            report = self.roots.repo_root / phase["report"]
            phase_attempts = [
                item for item in attempts if item["identity"]["phase"] == phase["phase"]
            ]
            missing = sorted(
                {
                    f"{item['identity']['attempt_id']}:{name}"
                    for item in phase_attempts
                    if item["attempted"]
                    for name in item["reliability"]["evidence_completeness"]["missing"]
                }
            )
            phases.append(
                {
                    "phase": phase["phase"],
                    "report": {"path": phase["report"], "present": report.is_file()},
                    "evidence_dir": {
                        "path": phase["evidence_dir"],
                        "present": evidence_dir.is_dir(),
                        "files": (
                            sorted(
                                _rel(path, evidence_dir)
                                for path in evidence_dir.rglob("*")
                                if path.is_file()
                            )
                            if evidence_dir.is_dir()
                            else []
                        ),
                    },
                    "source_identity": phase["source_identity"],
                    "provider": phase.get("provider"),
                    "slots_total": len(phase_attempts),
                    "slots_attempted": sum(
                        1 for item in phase_attempts if item["attempted"]
                    ),
                    "task_ids": sorted(
                        item["identity"]["task_id"]
                        for item in phase_attempts
                        if item["attempted"]
                    ),
                    "configuration_fingerprints": sorted(
                        {
                            item["identity"]["runtime_identity"].get(
                                "configuration_fingerprint"
                            )
                            for item in phase_attempts
                            if item["attempted"]
                            and item["identity"]["runtime_identity"].get(
                                "configuration_fingerprint"
                            )
                        }
                    ),
                    "missing_artifacts": missing,
                    "phase_limitations": phase.get("phase_limitations", []),
                }
            )
        return {
            "database": {
                "path": _rel(self.roots.database, self.roots.repo_root),
                "present": self.roots.database.is_file(),
            },
            "control_root": {
                "path": _rel(self.roots.control, self.roots.repo_root),
                "present": self.roots.control.is_dir(),
            },
            "excluded_tasks": self.registry.get("excluded_tasks", []),
            "phases": phases,
        }
