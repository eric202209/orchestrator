"""Phase 39 P0A: planner evidence rows persist plan metadata, never plan content.

Live rows 42902/42904 (TE 392) retained ``compiled_plan`` -- the full plan,
including model-generated replacement source -- through ``emit_live`` in
``app/tasks/worker.py``.  These tests use synthetic canaries only.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from app.api.v1.endpoints import project_logs
from app.models import LogEntry, Project, Session as SessionModel
from app.services.observability.log_stream import LogStreamService
from app.services.orchestration.invocation_provenance import (
    CORRELATED,
    InvocationProvenanceReference,
    bind_invocation_reference,
    reset_invocation_reference,
)
from app.services.orchestration.planning.planner_contract_registry import (
    compiled_plan_log_summary,
)
from app.services.orchestration.state.persistence import record_live_log

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = REPO_ROOT / "app" / "tasks" / "worker.py"
PLANNING_FLOW_PATH = (
    REPO_ROOT / "app" / "services" / "orchestration" / "phases" / "planning_flow.py"
)

SOURCE_CANARY = "def synthetic_source_canary_p0a_7f1c(): return 'SRC-CANARY'"
STEP_CANARY = "synthetic-step-description-canary-p0a-2b9e"
# Canaries avoid double quotes so JSON escaping cannot hide a leak.
COMMAND_CANARY = "python3 synthetic_command_canary_p0a_91d0.py"
VERIFY_CANARY = "synthetic-verification-canary-p0a-44aa"
PROVIDER_CANARY = "synthetic-provider-response-canary-p0a-c3e5"
REPAIR_CANARY = "synthetic-repair-output-canary-p0a-5d71"
PATH_CANARY = "synthetic_path_canary_p0a/core.py"
CANARIES = (
    SOURCE_CANARY,
    "SRC-CANARY",
    STEP_CANARY,
    COMMAND_CANARY,
    VERIFY_CANARY,
    PROVIDER_CANARY,
    REPAIR_CANARY,
    PATH_CANARY,
)


def _synthetic_plan() -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": STEP_CANARY,
            "commands": [COMMAND_CANARY, f"echo {PROVIDER_CANARY}"],
            "verification": VERIFY_CANARY,
            "rollback": None,
            "expected_files": [PATH_CANARY],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": PATH_CANARY,
                    "old": REPAIR_CANARY,
                    "new": SOURCE_CANARY,
                },
                {"op": "write_file", "path": PATH_CANARY, "content": SOURCE_CANARY},
            ],
        },
        {
            "step_number": 2,
            "description": f"{STEP_CANARY} verify",
            "commands": ["python3 -m pytest -q"],
            "verification": VERIFY_CANARY,
            "rollback": None,
            "expected_files": [],
            "ops": [],
        },
    ]


def _assert_no_canary(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text, canary


def _reference() -> InvocationProvenanceReference:
    invocation_id = uuid.uuid4().hex
    return InvocationProvenanceReference(
        invocation_attempt_id=invocation_id,
        projection_journal_reference=invocation_id,
        worker_boot_manifest_id=None,
        worker_boot_manifest_digest=None,
        attempt_number=1,
        classification=CORRELATED,
    )


class _CapturingDb:
    def __init__(self):
        self.entries = []

    def add(self, entry):
        self.entries.append(entry)

    def commit(self):
        return None


def test_summary_is_content_free_bounded_and_deterministic():
    plan = _synthetic_plan()
    before = copy.deepcopy(plan)

    summary = compiled_plan_log_summary(plan)

    _assert_no_canary(json.dumps(summary))
    assert plan == before
    canonical = json.dumps(
        plan, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    assert summary == {
        "content_omitted": True,
        "step_count": 2,
        "step_numbers": [1, 2],
        "command_count": 3,
        "ops_count": 2,
        "expected_file_count": 1,
        "serialized_bytes": len(canonical),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }
    assert compiled_plan_log_summary(copy.deepcopy(plan)) == summary


def test_summary_handles_empty_and_malformed_plans():
    assert compiled_plan_log_summary(None)["step_count"] == 0
    assert compiled_plan_log_summary([])["step_numbers"] == []
    malformed = compiled_plan_log_summary(
        [STEP_CANARY, {"step_number": True, "commands": SOURCE_CANARY}]
    )
    _assert_no_canary(json.dumps(malformed))
    assert malformed["step_count"] == 2
    assert malformed["step_numbers"] == [None]
    assert malformed["command_count"] == 0
    many = compiled_plan_log_summary([{"step_number": i} for i in range(200)])
    assert many["step_count"] == 200
    assert len(many["step_numbers"]) == 64


def _worker_planner_evidence_dicts() -> dict[str, ast.Dict]:
    tree = ast.parse(WORKER_PATH.read_text())
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "event_type"
                and isinstance(value, ast.Constant)
                and value.value
                in {"planner_terminal_evidence", "planner_execution_start"}
            ):
                found[value.value] = node
    return found


def test_worker_planner_evidence_writers_use_summary_not_plan():
    found = _worker_planner_evidence_dicts()
    assert set(found) == {"planner_terminal_evidence", "planner_execution_start"}
    for event_type, node in found.items():
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        assert "compiled_plan" not in keys, event_type
        summary = node.values[
            [k.value if isinstance(k, ast.Constant) else None for k in node.keys].index(
                "compiled_plan_summary"
            )
        ]
        assert isinstance(summary, ast.Call)
        assert ast.unparse(summary.func) == "compiled_plan_log_summary"
        assert ast.unparse(summary.args[0]) == "orchestration_state.plan"


_LOG_SINKS = {"emit_live", "emit_phase_event", "_record_live_log", "record_live_log"}
_SAFE_WRAPPERS = {"len", "bool", "compiled_plan_log_summary"}
# Known failure-path writers that intentionally retain bounded stale-replace
# evidence (``_extract_stale_replace_evidence_from_plan``).  Out of scope for
# P0A row suppression; tracked as a residual risk in the phase report.
_KNOWN_RESIDUAL_DETAIL_BUILDERS = {
    "_terminal_validation_failure_details",
    "_planning_invalid_commands_after_repair_details",
}


def _raw_plan_references(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []

    def walk(node, safe):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func).rsplit(".", 1)[-1]
            safe = safe or name in _SAFE_WRAPPERS
            if name in _KNOWN_RESIDUAL_DETAIL_BUILDERS:
                return
        if not safe and isinstance(node, ast.Attribute) and node.attr == "plan":
            hits.append((node.lineno, ast.unparse(node)))
        for child in ast.iter_child_nodes(node):
            walk(child, safe)

    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        if ast.unparse(node.func).rsplit(".", 1)[-1] not in _LOG_SINKS:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            walk(arg, False)
    return hits


def test_planner_log_writers_on_the_shared_path_do_not_pass_the_raw_plan():
    assert _raw_plan_references(WORKER_PATH) == []
    assert _raw_plan_references(PLANNING_FLOW_PATH) == []


def _persist_planner_evidence(metadata: dict) -> LogEntry:
    db = _CapturingDb()
    record_live_log(
        db,
        41,
        42,
        "INFO",
        "[ORCHESTRATION] Planner terminal evidence retained",
        session_instance_id="f2be8165-00a1-44ee-a956-da5fec04743a",
        metadata=metadata,
        task_execution_id=43,
    )
    assert len(db.entries) == 1
    return db.entries[0]


def _terminal_metadata(plan: list[dict]) -> dict:
    # Mirrors the worker's planner_terminal_evidence payload shape.
    return {
        "event_type": "planner_terminal_evidence",
        "phase": "planning",
        "planner_status": "completed",
        "planner_terminal_reason": None,
        "compiled_plan_summary": compiled_plan_log_summary(plan),
        "execution_start_reached": True,
        "task_execution_id": 43,
    }


def test_persisted_row_and_api_omit_plan_content_and_keep_metadata(monkeypatch):
    plan = _synthetic_plan()
    canonical_before = copy.deepcopy(plan)
    token = bind_invocation_reference(_reference())
    try:
        entry = _persist_planner_evidence(_terminal_metadata(plan))
    finally:
        reset_invocation_reference(token)

    _assert_no_canary(entry.message)
    _assert_no_canary(entry.log_metadata)
    assert plan == canonical_before

    stored = json.loads(entry.log_metadata)
    assert entry.session_id == 41
    assert entry.task_id == 42
    assert entry.task_execution_id == 43
    assert stored["event_type"] == "planner_terminal_evidence"
    assert stored["planner_status"] == "completed"
    assert stored["compiled_plan_summary"]["step_count"] == 2
    assert stored["compiled_plan_summary"]["content_omitted"] is True
    assert stored["invocation_provenance"]["classification"] == CORRELATED
    assert "compiled_plan" not in stored

    row = SimpleNamespace(
        id=7,
        session_id=entry.session_id,
        task_id=entry.task_id,
        task_execution_id=entry.task_execution_id,
        level=entry.level,
        message=entry.message,
        log_metadata=entry.log_metadata,
        created_at=None,
        session_instance_id=entry.session_instance_id,
    )

    class _Query:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *_a, **_k):
            return self

        join = order_by = limit = filter

        def all(self):
            return list(self.rows)

        def first(self):
            return self.rows[0] if self.rows else None

    class Db:
        def query(self, *entities):
            entity = entities[0]
            if entity is Project:
                return _Query([SimpleNamespace(id=99, name="Synthetic project")])
            if entity is SessionModel.id:
                return _Query([(41,)])
            if entity is LogEntry.level:
                return _Query([("INFO", 1)])
            if entity is LogEntry:
                return _Query([row])
            raise AssertionError(f"Unexpected query entity: {entity!r}")

    streamed = next(LogStreamService(Db()).stream_logs(session_id=41))
    _assert_no_canary(json.dumps(streamed))
    assert streamed["metadata"]["compiled_plan_summary"]["step_count"] == 2

    monkeypatch.setattr(
        LogStreamService,
        "get_project_logs_summary",
        lambda _self, _project_id: {"total_logs": 1, "by_level": {}, "recent_logs": []},
    )
    response = project_logs.get_project_logs(project_id=99, db=Db(), limit=10)
    _assert_no_canary(json.dumps(response, default=str))
    assert response["logs"][0]["metadata"]["compiled_plan_summary"] == (
        stored["compiled_plan_summary"]
    )


def test_legacy_compiled_plan_payload_would_have_leaked():
    """Negative control: the pre-P0A payload shape carries every canary."""

    entry = _persist_planner_evidence({"compiled_plan": _synthetic_plan()})
    for canary in CANARIES:
        assert canary in entry.log_metadata, canary
