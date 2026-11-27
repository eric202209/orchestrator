"""P43-PRODUCT-24B-R1: the targeted syntax second repair must show its excerpt.

U2 evidence: the Planner replaced the semantic target region ``"parse_amount"``
(the bare identifier) with a whole ``def parse_amount(...)`` function, which
yields ``def def parse_amount(`` at line 4.  The targeted second repair built a
line-numbered candidate excerpt that shows exactly that line, but the repair
prompt cut every rejection reason to 180 characters, so the provider saw only
"... Candidate source excerpt" and no excerpt.

Only the provider transport, the DB and external writes are stubbed here; the
semantic target inventory, Validator, Bootstrap Contract, repair arbitration
and repair prompt builder are the production components.
"""

import json
import logging
import re
from unittest.mock import MagicMock

from app.config import settings
from app.services.orchestration.phases.planning_flow import (
    _get_targeted_second_repair_reason,
    execute_planning_phase,
)
from app.services.orchestration.phases.planning_support import _PlanningRetryState
from app.services.orchestration.planning.repair_prompts import (
    build_planning_repair_prompt_with_metadata,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.parsing import extract_structured_text
from app.tests.planner_timeout_test_helpers import _patch_planning_flow_external_writes

PARSING = (
    '"""Amount parsing. Amounts are integer cents."""\n'
    "\n"
    "\n"
    "def parse_amount(text: str) -> int:\n"
    '    """Parse ``"1,234.56"`` or ``"7"`` into integer cents."""\n'
    '    cleaned = text.strip().replace(",", "").strip("()")\n'
    '    negative = cleaned.startswith("-")\n'
    "    if negative:\n"
    "        cleaned = cleaned[1:]\n"
    '    whole, _, frac = cleaned.partition(".")\n'
    "    if not whole.isdigit() or (frac and (not frac.isdigit() or len(frac) > 2)):\n"
    '        raise ValueError(f"invalid amount: {text!r}")\n'
    '    cents = int(whole) * 100 + int(frac.ljust(2, "0") or "0")\n'
    "    return -cents if negative else cents\n"
)
EXISTING_TEST = (
    "from ledgerlite import parse_amount\n"
    "\n"
    "\n"
    "def test_plain_amount() -> None:\n"
    '    assert parse_amount("40.00") == 4000\n'
)
TASK = (
    "Fix a reporting defect in the ledgerlite library: `parse_amount` treats "
    "`(12.50)` as income instead of -12.50. Find and fix the root cause and add "
    "focused regression tests. Preserve existing behavior.\n"
    "Independent acceptance contract (required):\n"
    "- `python -m pytest tests/test_ledger.py -q`"
)
# The whole function, emitted for a target region that is only the identifier.
WHOLE_FUNCTION = (
    "def parse_amount(text: str) -> int:\n"
    "    raw = text.strip()\n"
    '    negative = raw.startswith("(") and raw.endswith(")")\n'
    '    cleaned = (raw[1:-1] if negative else raw).replace(",", "")\n'
    '    if cleaned.startswith("-"):\n'
    "        negative = True\n"
    "        cleaned = cleaned[1:]\n"
    '    whole, _, frac = cleaned.partition(".")\n'
    "    if not whole.isdigit():\n"
    '        raise ValueError(f"invalid amount: {text!r}")\n'
    '    cents = int(whole) * 100 + int(frac.ljust(2, "0") or "0")\n'
    "    return -cents if negative else cents"
)
TEST_STEP = {
    "description": "Add focused regression tests",
    "ops": [
        {
            "op": "write_file",
            "path": "tests/test_parentheses.py",
            "content": (
                "from ledgerlite import parse_amount\n\n\n"
                "def test_parentheses_are_negative() -> None:\n"
                '    assert parse_amount("(12.50)") == -1250\n'
            ),
        }
    ],
    "commands": [],
    "verification": "python3 -m pytest tests/test_parentheses.py -q",
    "expected_files": ["tests/test_parentheses.py"],
}
ACCEPT_STEP = {
    "description": "Run acceptance",
    "commands": [],
    "verification": "python3 -m pytest tests/test_ledger.py -q",
    "expected_files": [],
}


def _seed(tmp_path):
    (tmp_path / "ledgerlite").mkdir()
    (tmp_path / "ledgerlite" / "__init__.py").write_text(
        "from .parsing import parse_amount\n\n__all__ = ['parse_amount']\n"
    )
    (tmp_path / "ledgerlite" / "parsing.py").write_text(PARSING)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ledger.py").write_text(EXISTING_TEST)


def _misused_target_plan(target_id: str) -> str:
    fix = {
        "description": "Fix parse_amount",
        "ops": [
            {
                "op": "replace_in_file",
                "path": "ledgerlite/parsing.py",
                "target_id": target_id,
                "new": WHOLE_FUNCTION,
            }
        ],
        "commands": [],
        "verification": "python3 -m py_compile ledgerlite/parsing.py",
        "expected_files": ["ledgerlite/parsing.py"],
    }
    return json.dumps([fix, TEST_STEP, ACCEPT_STEP])


def _run_planning(tmp_path, monkeypatch, responses):
    """Run production Planning; ``responses`` maps call index -> output builder."""

    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", 32768)
    _seed(tmp_path)
    prompts: list[str] = []
    issued: dict[str, str] = {}

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, prompt, *args, **kwargs):
            prompts.append(prompt)
            found = re.findall(r"target_id: (tgt_[0-9a-f]{24})", prompt)
            if found:
                issued.setdefault("id", found[0])
            index = len(prompts) - 1
            if index >= len(responses):
                return {"status": "completed", "output": "NO_RECOVERY_POSSIBLE"}
            return {"status": "completed", "output": responses[index](issued)}

    state = MagicMock()
    state.project_dir = tmp_path
    state.project_id = 1
    state.project_context = ""
    state.plan = []
    state.current_step_index = 0
    state.reasoning_artifact = None
    task = MagicMock()
    task.title = "ledgerlite task"
    task.description = TASK
    task.plan_position = 1
    project = MagicMock()
    project.name = "ledgerlite"
    project.description = "ledgerlite"
    project.workspace_path = "ledgerlite"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    ctx = OrchestrationRunContext(
        db=db,
        session=session,
        project=project,
        task=task,
        session_task_link=MagicMock(),
        session_id=1,
        task_id=1,
        prompt=TASK,
        timeout_seconds=1200,
        execution_profile="implementation",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.p43_24b_r1"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )
    _patch_planning_flow_external_writes(monkeypatch)
    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": True},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )
    return result, prompts, issued


def _validation_block(prompt: str) -> str:
    start = prompt.index("Validation error:")
    end = prompt.index("## PYTHON TEST SOURCE CONTEXT", start)
    return prompt[start:end]


def test_second_syntax_repair_prompt_shows_the_def_def_line(tmp_path, monkeypatch):
    misused = lambda issued: _misused_target_plan(issued["id"])  # noqa: E731
    result, prompts, issued = _run_planning(
        tmp_path,
        monkeypatch,
        [
            lambda issued: '{"action":"read_file","path":"ledgerlite/parsing.py"}',
            misused,
            misused,
            misused,
        ],
    )

    assert issued.get("id"), "production inventory issued no target handle"
    # Discovery, first-pass Planning, repair 1, targeted syntax second repair.
    assert len(prompts) == 4
    block = _validation_block(prompts[3])
    assert "Candidate source excerpt with real newlines preserved:" in block
    assert "   4: def def parse_amount(text: str) -> int:" in block
    # The Validator is unchanged: the same misuse is still rejected.
    assert result["status"] == "failed"


def test_valid_plan_is_accepted_without_repair(tmp_path, monkeypatch):
    def valid(issued):
        fix = {
            "description": "Fix parse_amount",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "ledgerlite/parsing.py",
                    "old": PARSING[PARSING.index("def parse_amount") :].rstrip("\n"),
                    "new": WHOLE_FUNCTION,
                }
            ],
            "commands": [],
            "verification": "python3 -m py_compile ledgerlite/parsing.py",
            "expected_files": ["ledgerlite/parsing.py"],
        }
        return json.dumps([fix, TEST_STEP, ACCEPT_STEP])

    result, prompts, _ = _run_planning(
        tmp_path,
        monkeypatch,
        [lambda issued: '{"action":"stop"}', valid],
    )

    assert result == {"status": "completed"}
    assert len(prompts) == 2


def _syntax_verdict(candidate: str, line: int):
    return type(
        "Verdict",
        (),
        {
            "reasons": [
                "Plan writes Python source with invalid syntax "
                f"(python_source_syntax_invalid; pkg/mod.py line {line}, offset 5: "
                "invalid syntax)"
            ],
            "details": {
                "python_source_syntax_invalid": [
                    {
                        "path": "pkg/mod.py",
                        "line": line,
                        "offset": 5,
                        "message": "invalid syntax",
                        "candidate_content": candidate,
                    }
                ],
                "semantic_violation_codes": ["python_source_syntax_invalid"],
            },
        },
    )()


def _repair_prompt(tmp_path, monkeypatch, rejection_reasons):
    monkeypatch.setattr(settings, "PLANNING_REPAIR_CONTEXT_TOKENS", 32768)
    return build_planning_repair_prompt_with_metadata(
        task_description="Repair pkg/mod.py",
        malformed_output="[]",
        project_dir=tmp_path,
        rejection_reasons=rejection_reasons,
    ).prompt


def test_error_line_survives_for_a_mid_size_file(tmp_path, monkeypatch):
    # ~2 KB: below the old 2400-char whole-file threshold, so before the
    # correction the excerpt began at line 1 and the error line fell past
    # the per-reason cap.
    lines = [
        f"value_{index:02d} = {index}  # padding padding" for index in range(1, 61)
    ]
    lines[44] = "def def broken(x):"
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=_syntax_verdict("\n".join(lines) + "\n", 45),
    )

    prompt = _repair_prompt(tmp_path, monkeypatch, [reason.rejection_text])

    assert "  45: def def broken(x):" in prompt


def test_ordinary_reasons_keep_the_180_char_cap(tmp_path, monkeypatch):
    long_reason = "plan_validation_failed: " + "x" * 400
    prompt = _repair_prompt(tmp_path, monkeypatch, [long_reason])

    block = _validation_block(prompt) if "## PYTHON TEST" in prompt else prompt
    assert ("- " + long_reason[:180]) in block
    assert long_reason[:181] not in block
