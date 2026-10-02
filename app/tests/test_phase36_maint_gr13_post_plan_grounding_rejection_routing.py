"""PHASE36-MAINT-GR13 — post-Plan grounding rejection routing & retry authority.

REENTRY-6: every Plan whole-file rewrote the unmaterialized, >2,000-byte
``permissions.py``.  Post-Plan grounding correctly failed closed with
``POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE``, but the rejection was Planning
terminal (no Plan repair), and the bare code was then retried as a transient
task failure: four identical full re-plans.

GR13 keeps the fail-closed boundary.  The Plan-selected ungroundable target
class (and only it) now owns one pass of the existing bounded repair funnel;
a second rejection terminates with a deterministic, retry-exempt reason.

These cases drive the real Planning flow, discovery reader, materializer,
post-Plan grounding, validator and ``PlannerService.repair_output``.  Only the
provider seams are replaced; no provider is called.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.error_handler import EnhancedErrorHandler
from app.services.orchestration.phases import planning_support
from app.services.orchestration.phases.planning_flow import execute_planning_phase
from app.services.orchestration.phases.post_plan_source_grounding import (
    FAILURE_CLASS_PLAN_TARGET_UNGROUNDABLE,
    POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
    POST_PLAN_GROUNDING_SYMLINK,
    POST_PLAN_GROUNDING_VERSION_STALE,
    ground_post_plan_source_materialization,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.source_materialization import (
    MAX_SOURCE_CONTENT_PER_FILE_CHARS,
    materialize_planner_source_context,
)
from app.services.orchestration.recovery.failure_classifier import FailureClassifier
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.parsing import extract_structured_text
from app.services.orchestration.validation.validator import ValidatorService
from app.services.session.execution_policy import (
    classify_failure,
    is_retry_exempt_category,
)
from app.tests.planner_timeout_test_helpers import (
    _patch_planning_flow_external_writes,
)
from app.tests.test_phase36_maint_gr4_long_file_grounding_continuation import (
    NARROW_OLD,
    ROUTER,
    TARGET,
    TASK,
    VERIFY,
)

PERM = "app/api/v1/endpoints/permissions.py"
OTHER = "app/api/v1/endpoints/users.py"
PERM_SRC = "".join(
    f'@router.get("/permissions/r{i}")\nasync def r{i}():\n    return {{"i": {i}}}\n\n\n'
    for i in range(160)
)
PLACEHOLDER = (
    "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
    '@router.get("/pending")\nasync def pending():\n    return {"pending": []}\n'
)
REJECTED_AFTER_REPAIR = planning_support.POST_PLAN_GROUNDING_REJECTED_AFTER_REPAIR
OBSERVATION_HEADER = "## READ-ONLY OBSERVATION"
SOURCE_HEADER = "## CURRENT SOURCE MATERIALIZATION"
DEFECT = 'prefix="/permissions"'


def _destructive_plan(path: str = PERM) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Inspect the permissions endpoint",
            "commands": [f"cat {path}"],
            "verification": None,
            "rollback": None,
            "expected_files": [],
            "ops": [],
        },
        {
            "step_number": 2,
            "description": "Rewrite the permissions endpoint",
            "commands": [],
            "verification": f"python -m py_compile {path}",
            "rollback": None,
            "expected_files": [path],
            "ops": [{"op": "write_file", "path": path, "content": PLACEHOLDER}],
        },
    ]


def _narrow_router_plan() -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Drop the doubled permissions prefix",
            "commands": [],
            "verification": VERIFY,
            "rollback": None,
            "expected_files": [TARGET],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": TARGET,
                    "old": NARROW_OLD,
                    "new": NARROW_OLD.replace(DEFECT, 'prefix=""'),
                }
            ],
        }
    ]


def _workspace(root: Path) -> Path:
    root = root.resolve()
    (root / "app/api/v1/endpoints").mkdir(parents=True)
    (root / TARGET).write_text(ROUTER, encoding="utf-8")
    (root / PERM).write_text(PERM_SRC, encoding="utf-8")
    (root / OTHER).write_text(PERM_SRC.replace("/permissions", "/users"), "utf-8")
    return root


class _Calls:
    def __init__(self) -> None:
        self.discovery = 0
        self.planning = 0
        self.repair_prompts: list[str] = []
        self.repair_kwargs: list[dict] = []


@pytest.fixture(autouse=True)
def _isolate_flow(monkeypatch):
    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Repair the permission route",
            "workspace_facts": [],
            "planned_actions": [],
            "verification_plan": ["Parse the router"],
        },
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        staticmethod(
            lambda *args, **kwargs: type(
                "Verdict", (), {"accepted": True, "status": "accepted", "reasons": []}
            )()
        ),
    )


def _install_providers(monkeypatch, calls: _Calls, initial, repaired) -> None:
    """Discovery reads router.py (the REENTRY-6 shape); Planning is canned."""

    async def planning_lock(cls, runtime_service, prompt, **kwargs):
        if kwargs.get("diagnostic_label") == "PLANNING_DISCOVERY":
            calls.discovery += 1
            return {
                "status": "completed",
                "output": json.dumps({"action": "read_file", "path": TARGET}),
            }
        calls.planning += 1
        return {"status": "completed", "output": json.dumps(initial)}

    async def fake_repair(runtime_service, repair_prompt, repair_timeout, **kwargs):
        calls.repair_prompts.append(repair_prompt)
        return {"status": "completed", "output": json.dumps(repaired)}

    original_repair_output = PlannerService.repair_output.__func__

    def recording_repair_output(cls, *args, **kwargs):
        calls.repair_kwargs.append(kwargs)
        return original_repair_output(cls, *args, **kwargs)

    monkeypatch.setattr(
        PlannerService,
        "_execute_task_with_planning_lock",
        classmethod(planning_lock),
    )
    monkeypatch.setattr(
        PlannerService, "_invoke_repair_prompt", staticmethod(fake_repair)
    )
    monkeypatch.setattr(
        PlannerService, "repair_output", classmethod(recording_repair_output)
    )


def _context(root: Path, initial) -> OrchestrationRunContext:
    state = MagicMock()
    state.project_dir = root
    state.project_context = ""
    state.plan = []
    state.current_step_index = 0
    state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(initial)}

    task = MagicMock()
    task.title = "Repair the permission pending route"
    task.description = TASK
    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=MagicMock(),
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=1306,
        task_id=1306,
        prompt=TASK,
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.gr13"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )
    return ctx


def _run(tmp_path: Path, monkeypatch, initial, repaired):
    root = _workspace(tmp_path)
    calls = _Calls()
    _install_providers(monkeypatch, calls, initial, repaired)
    terminal = []
    original_finalize = planning_support._finalize_planning_terminal_failure

    def recording_finalize(**kwargs):
        terminal.append(kwargs)
        return original_finalize(**kwargs)

    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_support."
        "_finalize_planning_terminal_failure",
        recording_finalize,
    )
    ctx = _context(root, initial)
    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": True},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )
    return root, ctx, calls, terminal, result


def _section(prompt: str, header: str) -> str:
    start = prompt.index(header)
    ends = [
        index
        for index in (
            prompt.find("\n## ", start + len(header)),
            prompt.find("\nBad:\n", start),
        )
        if index != -1
    ]
    return prompt[start : min(ends)] if ends else prompt[start:]


def _grounding(root: Path, plan):
    initial = materialize_planner_source_context(
        root, task_description=TASK, supporting_paths=()
    )
    return ground_post_plan_source_materialization(
        plan, project_dir=root, source_materialization=initial
    )


# --- R1/R2/R3: the fail-closed rejection and its typed class ---------------


def test_r1_r3_reentry6_plan_is_rejected_as_plan_target_ungroundable(tmp_path):
    root = _workspace(tmp_path)

    grounding = _grounding(root, _destructive_plan())

    assert grounding.failure_code == POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE
    assert grounding.failure_path == PERM
    assert grounding.failure_class == FAILURE_CLASS_PLAN_TARGET_UNGROUNDABLE
    assert (
        grounding.to_dict()["failure_class"] == FAILURE_CLASS_PLAN_TARGET_UNGROUNDABLE
    )
    assert PERM not in grounding.materialization.file_map()


def test_r2_destructive_whole_file_plan_never_validates(tmp_path):
    root = _workspace(tmp_path)
    grounding = _grounding(root, _destructive_plan())
    plan = _destructive_plan()

    verdict = ValidatorService().validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=root,
        source_materialization=grounding.materialization,
    )

    assert not verdict.accepted


# --- R4–R7, R8, R11, R13, R17: bounded repair in the real flow ------------


def test_r4_to_r8_rejection_enters_one_bounded_repair_then_terminates(
    tmp_path, monkeypatch
):
    plan = _destructive_plan()
    root, ctx, calls, terminal, result = _run(tmp_path, monkeypatch, plan, plan)

    # R4/R8/R17: one repair, the re-grounded repair is rejected identically,
    # and the attempt ends deterministically with the retry-exempt reason.
    assert result == {"status": "failed", "reason": REJECTED_AFTER_REPAIR}
    assert len(calls.repair_prompts) == 1
    assert [call["failure_type"] for call in terminal] == [REJECTED_AFTER_REPAIR]
    assert POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE in terminal[0]["failure_reason"]
    # R13: one logical attempt — one discovery, one Plan, one repair.
    assert (calls.discovery, calls.planning) == (1, 1)

    # R5: the repair receives the typed rejection reason.
    kwargs = calls.repair_kwargs[0]
    assert kwargs["reason"].startswith(POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE)
    reasons = kwargs["rejection_reasons"]
    assert (
        reasons[0] == f"{POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE}: Plan mutates {PERM}"
    )
    assert "existing file has no complete authoritative source" in reasons[1]
    assert "write_file cannot be admitted" in reasons[1]
    # The rejection is exposed, not solved: no target or edit is suggested.
    assert not any("router.py" in reason for reason in reasons)

    # R6: GR12 continuity — the repair sees the advisory observation and its
    # permission include/prefix region, plus the rejected Plan.
    prompt = calls.repair_prompts[0]
    assert DEFECT in _section(prompt, OBSERVATION_HEADER)
    assert reasons[0][:100] in prompt
    assert "/pending" in prompt

    # R7: canonical/advisory authority is unchanged.  The canonical record is
    # the same truncated router head; permissions.py never became evidence.
    assert kwargs["read_only_observation"] is ctx.read_only_observation
    canonical = kwargs["source_materialization"]
    assert sorted(canonical.file_map()) == [TARGET]
    assert canonical.file_map()[TARGET].truncated
    assert DEFECT not in _section(prompt, SOURCE_HEADER)
    assert PERM not in ctx.planner_source_materialization.file_map()

    # R11: nothing executed or mutated.
    assert (root / PERM).read_text(encoding="utf-8") == PERM_SRC
    assert (root / TARGET).read_text(encoding="utf-8") == ROUTER


def test_r9_repair_to_another_ungrounded_long_file_remains_rejected(
    tmp_path, monkeypatch
):
    root, _, calls, terminal, result = _run(
        tmp_path, monkeypatch, _destructive_plan(), _destructive_plan(OTHER)
    )

    assert result == {"status": "failed", "reason": REJECTED_AFTER_REPAIR}
    assert len(calls.repair_prompts) == 1
    assert OTHER in terminal[0]["failure_reason"]
    assert (root / OTHER).read_text(encoding="utf-8") != PLACEHOLDER


def test_r10_groundable_repair_continues_to_ordinary_validation(tmp_path, monkeypatch):
    root, ctx, calls, terminal, result = _run(
        tmp_path, monkeypatch, _destructive_plan(), _narrow_router_plan()
    )

    assert len(calls.repair_prompts) == 1
    assert result == {"status": "completed"}
    assert terminal == []
    admitted_ops = [
        operation
        for step in ctx.orchestration_state.plan
        for operation in step.get("ops") or []
    ]
    assert [(op["op"], op["path"]) for op in admitted_ops] == [
        ("replace_in_file", TARGET)
    ]
    # Admission only; Planning executes nothing.
    assert (root / TARGET).read_text(encoding="utf-8") == ROUTER


# --- R15/R16: other classes keep their existing routing --------------------


def test_r15_safety_denial_stays_terminal_without_repair(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    link = "app/api/v1/endpoints/linked.py"
    (root / link).symlink_to(root / PERM)
    assert _grounding(root, _destructive_plan(link)).failure_class is None

    flow_root = tmp_path / "flow"
    _workspace(flow_root)
    (flow_root / link).symlink_to(flow_root.resolve() / PERM)
    calls = _Calls()
    _install_providers(
        monkeypatch, calls, _destructive_plan(link), _narrow_router_plan()
    )
    ctx = _context(flow_root.resolve(), _destructive_plan(link))
    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": True},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "failed", "reason": POST_PLAN_GROUNDING_SYMLINK}
    assert calls.repair_prompts == []


def test_r16_stale_source_and_internal_invariants_are_not_plan_target_class(
    tmp_path,
):
    root = _workspace(tmp_path)
    initial = materialize_planner_source_context(
        root, task_description=TASK, expected_paths=[TARGET], supporting_paths=()
    )
    (root / TARGET).write_text(ROUTER.replace("# Area 0", "# Area zero"), "utf-8")
    stat = (root / TARGET).stat()
    os.utime(root / TARGET, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    stale = ground_post_plan_source_materialization(
        _narrow_router_plan(), project_dir=root, source_materialization=initial
    )
    no_materialization = ground_post_plan_source_materialization(
        _destructive_plan(), project_dir=root, source_materialization=None
    )
    foreign = ground_post_plan_source_materialization(
        _destructive_plan(),
        project_dir=root,
        source_materialization=initial,
        workspace_identity=str(tmp_path / "elsewhere"),
    )

    assert stale.failure_code == POST_PLAN_GROUNDING_VERSION_STALE
    for result in (stale, no_materialization, foreign):
        assert not result.ok
        assert result.failure_class is None


# --- R12–R14, R18: retry and reflection authority --------------------------


def test_r12_r13_terminal_rejection_is_not_task_retry_authority():
    exc = RuntimeError(REJECTED_AFTER_REPAIR)

    assert EnhancedErrorHandler().should_retry(exc, "task_execution") is False
    category = classify_failure(str(exc), "", {"failure_phase": "execution"})
    assert category == "planning_contract_violation"
    assert is_retry_exempt_category(category)


def test_r14_transient_failures_remain_retryable():
    handler = EnhancedErrorHandler()

    # Celery retry authority (ErrorHandler) is unchanged for transients.
    for message in (
        "503 Service Unavailable from ai-gateway",
        "Remote end closed connection without response",
        "worker lost while dispatching",
    ):
        assert handler.should_retry(RuntimeError(message), "task_execution") is True
    # Automatic-recovery authority is unchanged for infrastructure loss.
    category = classify_failure(
        "worker lost while dispatching", "", {"failure_phase": "execution"}
    )
    assert category == "infrastructure_failure"
    assert not is_retry_exempt_category(category)


def test_r14_other_grounding_codes_keep_existing_retry_policy():
    # GR13 does not reclassify non-Plan-target grounding failures (stale
    # source, infrastructure, internal invariants): their routing is unchanged.
    for code in (
        POST_PLAN_GROUNDING_INCOMPLETE_EVIDENCE,
        POST_PLAN_GROUNDING_VERSION_STALE,
    ):
        assert EnhancedErrorHandler().should_retry(RuntimeError(code), "x") is True


def test_r18_reflection_cannot_reopen_the_deterministic_failure():
    exc = RuntimeError(REJECTED_AFTER_REPAIR)
    event = FailureClassifier.classify(exc, None, session_id=1306, task_id=1306)

    decision = RecoveryStrategyRegistry.route(
        event,
        session_id=1306,
        task_id=1306,
        llm_callable=lambda prompt: "Retry the full Planning attempt.",
    )

    assert decision.strategy == "terminal"


# --- R21/R25: bound and bfd670e compact-repair compatibility ---------------


def test_r21_grounding_byte_bound_is_unchanged(tmp_path):
    root = _workspace(tmp_path)

    assert MAX_SOURCE_CONTENT_PER_FILE_CHARS == 2000
    grounding = _grounding(root, _destructive_plan())
    assert grounding.materialization.maximum_bytes_per_file == 2000


def test_r25_compact_observation_prompt_keeps_typed_reason(tmp_path):
    root = _workspace(tmp_path)
    grounding = _grounding(root, _destructive_plan())
    reasons = planning_support._post_plan_grounding_repair_reasons(
        _destructive_plan(), grounding
    )
    guidance = f"{OBSERVATION_HEADER}\nadvisory read_file {TARGET}\n{DEFECT}\n"

    prompt = PlannerService.build_compact_planning_repair_prompt(
        json.dumps(_destructive_plan()),
        rejection_reasons=reasons,
        guidance_block=guidance,
    )

    assert OBSERVATION_HEADER in prompt and DEFECT in prompt
    # bfd670e cuts each reason at 100 chars; the typed facts survive intact.
    assert all(len(reason) <= 100 for reason in reasons)
    assert all(reason in prompt for reason in reasons)
