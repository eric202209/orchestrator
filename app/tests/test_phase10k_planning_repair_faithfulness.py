"""Tests for Phase 10K-a — Planning Repair Faithfulness Guard.

Covers:
- Symbol extraction from task descriptions
- File path extraction
- check_plan_faithfulness (pass and fail cases)
- build_faithfulness_prompt_block content
- T3 P5d regression fixtures (unfaithful plan rejected, faithful plan accepted)
- Integration: run_guidance_plan_enforcement emits LogEntry on faithfulness failure
- repair prompt faithfulness block propagation via guidance_block
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.planning.repair_faithfulness import (
    build_faithfulness_prompt_block,
    check_plan_faithfulness,
    extract_required_file_paths,
    extract_required_symbols,
)

# ---------------------------------------------------------------------------
# T3 task description fixture
# ---------------------------------------------------------------------------

_T3_TASK = (
    "Add add_category(category: str, categories: list[str] = []) -> list[str]"
    " to the looptools package."
)

_T3_BAD_PLAN = [
    {
        "step_number": 1,
        "description": "Implement looptools module with safe default arguments",
        "commands": [
            "python -c \"import looptools; print(looptools.repeat_string('a', 3))\""
        ],
        "verification": (
            "python -c \"import looptools; print(looptools.repeat_string('a', 3))\""
        ),
        "rollback": None,
        "expected_files": ["looptools/__init__.py"],
        "ops": [
            {
                "op": "write_file",
                "path": "looptools/__init__.py",
                "content": (
                    "def repeat_string(s, times=None):\n"
                    "    if times is None: times = 1\n"
                    "    return s * times\n\n"
                    "def filter_list(items, predicate=None):\n"
                    "    if predicate is None: return list(items)\n"
                    "    return [x for x in items if predicate(x)]\n\n"
                    "def merge_dicts(base, override=None):\n"
                    "    if override is None: override = {}\n"
                    "    return {**base, **override}\n"
                ),
            }
        ],
    }
]

_T3_GOOD_PLAN = [
    {
        "step_number": 1,
        "description": "Implement add_category function in looptools package",
        "commands": [
            "python -c \"import looptools; print(looptools.add_category('a', []))\""
        ],
        "verification": (
            "python -c \"import looptools; print(looptools.add_category('a', []))\""
        ),
        "rollback": None,
        "expected_files": ["looptools/__init__.py"],
        "ops": [
            {
                "op": "write_file",
                "path": "looptools/__init__.py",
                "content": (
                    "def add_category(\n"
                    "    category: str,\n"
                    "    categories: list[str] | None = None,\n"
                    ") -> list[str]:\n"
                    "    if categories is None:\n"
                    "        categories = []\n"
                    "    return categories + [category]\n"
                ),
            }
        ],
    }
]

# ---------------------------------------------------------------------------
# extract_required_symbols
# ---------------------------------------------------------------------------


class TestExtractRequiredSymbols:
    def test_typed_function_in_task_description(self):
        symbols = extract_required_symbols(_T3_TASK)
        assert "add_category" in symbols

    def test_explicit_def_statement(self):
        symbols = extract_required_symbols(
            "Create def process_items(items: list[str]) -> list[str]"
        )
        assert "process_items" in symbols

    def test_explicit_class_statement(self):
        symbols = extract_required_symbols("Add class CategoryManager to the module.")
        assert "CategoryManager" in symbols

    def test_python_keywords_excluded(self):
        symbols = extract_required_symbols(
            "Use if(condition: bool) else return(value: int)"
        )
        assert "if" not in symbols
        assert "return" not in symbols
        assert "else" not in symbols

    def test_common_words_excluded(self):
        symbols = extract_required_symbols(
            "Call get(key: str) and set(key: str, value: str)"
        )
        assert "get" not in symbols
        assert "set" not in symbols

    def test_no_typed_params_no_symbols(self):
        symbols = extract_required_symbols("Write a Python file that does stuff.")
        assert symbols == []

    def test_empty_string_returns_empty(self):
        assert extract_required_symbols("") == []

    def test_no_duplicates(self):
        text = (
            "Add add_category(category: str) and then call"
            " def add_category to add_category(x: int)"
        )
        symbols = extract_required_symbols(text)
        assert symbols.count("add_category") == 1


# ---------------------------------------------------------------------------
# extract_required_file_paths
# ---------------------------------------------------------------------------


class TestExtractRequiredFilePaths:
    def test_module_init_path(self):
        paths = extract_required_file_paths(
            "Write the function to looptools/__init__.py"
        )
        assert "looptools/__init__.py" in paths

    def test_nested_path(self):
        paths = extract_required_file_paths(
            "Edit app/services/auth.py to add the login route"
        )
        assert "app/services/auth.py" in paths

    def test_no_path_no_match(self):
        paths = extract_required_file_paths("Add a function to the package.")
        assert paths == []

    def test_no_duplicates(self):
        text = "Write looptools/__init__.py then read looptools/__init__.py"
        paths = extract_required_file_paths(text)
        assert paths.count("looptools/__init__.py") == 1


# ---------------------------------------------------------------------------
# check_plan_faithfulness
# ---------------------------------------------------------------------------


class TestCheckPlanFaithfulness:
    # T3 P5d regression — unfaithful plan
    def test_t3_unfaithful_plan_fails(self):
        ok, missing = check_plan_faithfulness(_T3_TASK, _T3_BAD_PLAN)
        assert not ok
        assert "add_category" in missing

    # T3 P5d regression — faithful plan
    def test_t3_faithful_plan_passes(self):
        ok, missing = check_plan_faithfulness(_T3_TASK, _T3_GOOD_PLAN)
        assert ok
        assert missing == []

    def test_class_name_missing_in_repaired_plan(self):
        task = "Add class CategoryManager to the module."
        bad_plan = [
            {
                "step_number": 1,
                "description": "Add GenericHelper class",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "module.py",
                        "content": "class GenericHelper:\n    pass\n",
                    }
                ],
            }
        ]
        ok, missing = check_plan_faithfulness(task, bad_plan)
        assert not ok
        assert "CategoryManager" in missing

    def test_class_name_present_in_repaired_plan(self):
        task = "Add class CategoryManager to the module."
        good_plan = [
            {
                "step_number": 1,
                "description": "Add CategoryManager class",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "module.py",
                        "content": "class CategoryManager:\n    pass\n",
                    }
                ],
            }
        ]
        ok, missing = check_plan_faithfulness(task, good_plan)
        assert ok
        assert missing == []

    def test_empty_task_description_trivially_passes(self):
        ok, missing = check_plan_faithfulness("", _T3_BAD_PLAN)
        assert ok
        assert missing == []

    def test_no_symbols_in_task_trivially_passes(self):
        task = "Write a simple Python script that prints hello world."
        ok, missing = check_plan_faithfulness(task, _T3_BAD_PLAN)
        assert ok
        assert missing == []

    def test_original_plan_gates_required_symbols(self):
        task = "Add add_category(category: str) to looptools."
        # The original plan does NOT contain add_category, so the check should
        # not require it (original_plan provided as gate).
        original = [{"step_number": 1, "description": "Do something else"}]
        bad_repaired = [{"step_number": 1, "description": "Implement repeat_string"}]
        ok, missing = check_plan_faithfulness(
            task, bad_repaired, original_plan=original
        )
        assert ok  # gated out because add_category wasn't in original plan

    def test_file_path_missing_from_repaired_plan(self):
        task = "Write the function to looptools/__init__.py"
        bad_plan = [
            {
                "step_number": 1,
                "description": "Write function",
                "ops": [{"op": "write_file", "path": "other/module.py", "content": ""}],
            }
        ]
        ok, missing = check_plan_faithfulness(task, bad_plan)
        assert not ok
        assert "looptools/__init__.py" in missing

    def test_file_path_present_in_repaired_plan(self):
        task = "Write the function to looptools/__init__.py"
        good_plan = [
            {
                "step_number": 1,
                "description": "Write to looptools/__init__.py",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "looptools/__init__.py",
                        "content": "def fn(): pass",
                    }
                ],
            }
        ]
        ok, missing = check_plan_faithfulness(task, good_plan)
        assert ok
        assert missing == []


# ---------------------------------------------------------------------------
# build_faithfulness_prompt_block
# ---------------------------------------------------------------------------


class TestBuildFaithfulnessPromptBlock:
    def test_contains_preserve_instruction(self):
        block = build_faithfulness_prompt_block(_T3_TASK)
        assert "Preserve" in block or "preserve" in block

    def test_contains_required_artifact_names(self):
        block = build_faithfulness_prompt_block(_T3_TASK)
        assert "add_category" in block

    def test_contains_mutable_default_instruction(self):
        block = build_faithfulness_prompt_block(_T3_TASK)
        assert (
            "mutable default" in block.lower() or "= []" in block or "= None" in block
        )

    def test_empty_when_no_typed_symbols(self):
        block = build_faithfulness_prompt_block("Write a file that prints hello.")
        assert block == ""

    def test_empty_on_empty_input(self):
        assert build_faithfulness_prompt_block("") == ""

    def test_explicit_symbols_override(self):
        block = build_faithfulness_prompt_block(
            "some description", required_symbols=["my_func"]
        )
        assert "my_func" in block

    def test_explicit_empty_symbols_returns_empty(self):
        block = build_faithfulness_prompt_block("some description", required_symbols=[])
        assert block == ""


# ---------------------------------------------------------------------------
# repair prompt faithfulness block propagation
# ---------------------------------------------------------------------------


class TestRepairPromptFaithfulnessBlockPropagation:
    """The faithfulness block reaches both repair prompt builders via guidance_block."""

    def test_full_repair_prompt_includes_faithfulness_via_guidance_block(
        self, tmp_path
    ):
        from app.services.orchestration.planning.repair_prompts import (
            build_planning_repair_prompt_with_metadata,
        )

        guidance_block = build_faithfulness_prompt_block(_T3_TASK)
        assert guidance_block  # confirm block is non-empty for T3

        malformed = json.dumps(
            [
                {
                    "step_number": 1,
                    "description": "Impl",
                    "commands": [],
                    "verification": None,
                    "rollback": None,
                    "expected_files": [],
                    "ops": [{"op": "write_file", "path": "f.py", "content": "x=[]"}],
                }
            ]
        )
        result = build_planning_repair_prompt_with_metadata(
            task_description=_T3_TASK,
            malformed_output=malformed,
            project_dir=tmp_path,
            rejection_reasons=["mutable_default: = []"],
            guidance_block=guidance_block,
        )
        prompt = result.prompt
        assert "add_category" in prompt
        assert "preserve" in prompt.lower() or "Preserve" in prompt

    def test_compact_repair_prompt_includes_faithfulness_via_guidance_block(self):
        from app.services.orchestration.planning.repair_prompts import (
            build_compact_planning_repair_prompt,
        )

        guidance_block = build_faithfulness_prompt_block(_T3_TASK)
        malformed = json.dumps(
            [
                {
                    "step_number": 1,
                    "description": "Impl",
                    "commands": [],
                    "verification": None,
                    "rollback": None,
                    "expected_files": [],
                    "ops": [],
                }
            ]
        )
        prompt = build_compact_planning_repair_prompt(
            malformed_output=malformed,
            rejection_reasons=["mutable_default"],
            guidance_block=guidance_block,
        )
        assert "add_category" in prompt


# ---------------------------------------------------------------------------
# collect_repair_guidance_block: faithfulness block prepended when applicable
# ---------------------------------------------------------------------------


class TestCollectRepairGuidanceBlockFaithfulness:
    def _make_ctx(self, prompt: str = ""):
        ctx = MagicMock()
        ctx.project.id = 1
        ctx.project.user_id = 1
        ctx.session_id = 10
        ctx.task_id = 100
        ctx.db = MagicMock()
        ctx.prompt = prompt
        return ctx

    def test_faithfulness_block_absent_when_no_typed_symbols(self):
        from app.services.orchestration.phases.planning_guidance_enforcement import (
            collect_repair_guidance_block,
        )

        ctx = self._make_ctx(prompt="Write a simple file.")
        with patch(
            "app.services.human_guidance.plan_validator.render_active_guidance_for_repair",
            return_value="## OPERATOR GUIDANCE\n- Use print.",
        ):
            result = collect_repair_guidance_block(ctx)

        assert "## OPERATOR GUIDANCE" in result
        assert "Preserve" not in result

    def test_faithfulness_block_prepended_when_typed_symbols_present(self):
        from app.services.orchestration.phases.planning_guidance_enforcement import (
            collect_repair_guidance_block,
        )

        ctx = self._make_ctx(prompt=_T3_TASK)
        with patch(
            "app.services.human_guidance.plan_validator.render_active_guidance_for_repair",
            return_value="## OPERATOR GUIDANCE\n- Use None.",
        ):
            result = collect_repair_guidance_block(ctx)

        assert "add_category" in result
        assert "## OPERATOR GUIDANCE" in result
        # faithfulness block must come before operator guidance
        assert result.index("add_category") < result.index("## OPERATOR GUIDANCE")

    def test_faithfulness_block_only_when_no_operator_guidance(self):
        from app.services.orchestration.phases.planning_guidance_enforcement import (
            collect_repair_guidance_block,
        )

        ctx = self._make_ctx(prompt=_T3_TASK)
        with patch(
            "app.services.human_guidance.plan_validator.render_active_guidance_for_repair",
            return_value="",
        ):
            result = collect_repair_guidance_block(ctx)

        assert "add_category" in result
        assert "Preserve" in result or "preserve" in result


# ---------------------------------------------------------------------------
# run_guidance_plan_enforcement: faithfulness check integration
# ---------------------------------------------------------------------------


def _make_enforcement_ctx(db, plan, prompt: str, task_id: int = 99):
    """Build minimal ctx for run_guidance_plan_enforcement integration tests."""
    state = SimpleNamespace(
        plan=plan,
        session_id=999,
        project_dir="/tmp",
    )
    return SimpleNamespace(
        db=db,
        project=SimpleNamespace(id=1, user_id=1),
        session_id=999,
        task_id=task_id,
        prompt=prompt,
        orchestration_state=state,
        logger=MagicMock(),
        emit_live=MagicMock(),
        guidance_backend="local_openclaw",
        guidance_model_family="qwen",
    )


@pytest.mark.usefixtures("db_session")
def test_faithfulness_rejection_returns_sentinel(db_session, monkeypatch):
    """Second-pass enforcement returns faithfulness failure sentinel for T3 bad plan."""
    from app.services.orchestration.phases.planning_guidance_enforcement import (
        run_guidance_plan_enforcement,
    )

    retry_state = SimpleNamespace(
        hg_repair_prompt_used=True,
        repair_prompt_used=False,
        last_repair_reason="guidance_violation",
    )
    ctx = _make_enforcement_ctx(db_session, _T3_BAD_PLAN, _T3_TASK)

    with patch(
        "app.services.orchestration.phases.planning_guidance_enforcement._check_plan_violations",
        return_value=[],
    ), patch(
        "app.services.orchestration.phases.planning_guidance_enforcement.emit_phase_event",
    ):
        result = run_guidance_plan_enforcement(
            ctx,
            retry_state=retry_state,
            output_text=json.dumps(_T3_BAD_PLAN),
            planning_timeout_seconds=240,
            prompt_profile="default",
            repair_fn=MagicMock(),
            emit_diagnostics_fn=MagicMock(),
        )

    assert result is not None
    assert result.get("__faithfulness_failure__") is True
    assert result.get("reason") == "planning_repair_unfaithful_to_task_objective"


@pytest.mark.usefixtures("db_session")
def test_faithfulness_rejection_emits_log_entry(db_session):
    """Second-pass enforcement writes LogEntry on faithfulness failure."""
    from app.models import LogEntry
    from app.services.orchestration.phases.planning_guidance_enforcement import (
        run_guidance_plan_enforcement,
    )

    retry_state = SimpleNamespace(
        hg_repair_prompt_used=True,
        repair_prompt_used=False,
        last_repair_reason="guidance_violation",
    )
    ctx = _make_enforcement_ctx(db_session, _T3_BAD_PLAN, _T3_TASK, task_id=991)

    with patch(
        "app.services.orchestration.phases.planning_guidance_enforcement._check_plan_violations",
        return_value=[],
    ), patch(
        "app.services.orchestration.phases.planning_guidance_enforcement.emit_phase_event",
    ):
        run_guidance_plan_enforcement(
            ctx,
            retry_state=retry_state,
            output_text=json.dumps(_T3_BAD_PLAN),
            planning_timeout_seconds=240,
            prompt_profile="default",
            repair_fn=MagicMock(),
            emit_diagnostics_fn=MagicMock(),
        )

    entry = (
        db_session.query(LogEntry)
        .filter(
            LogEntry.task_id == 991,
            LogEntry.message.like("%[PLANNING_REPAIR_FAITHFULNESS_REJECTION]%"),
        )
        .first()
    )
    assert entry is not None
    assert "add_category" in entry.message
    meta = json.loads(entry.log_metadata)
    assert "add_category" in meta["missing_symbols"]
    assert meta["reason"] == "planning_repair_unfaithful_to_task_objective"


@pytest.mark.usefixtures("db_session")
def test_faithful_plan_passes_second_pass(db_session):
    """Second-pass enforcement returns None (no action) for faithful repaired plan."""
    from app.services.orchestration.phases.planning_guidance_enforcement import (
        run_guidance_plan_enforcement,
    )

    retry_state = SimpleNamespace(
        hg_repair_prompt_used=True,
        repair_prompt_used=False,
        last_repair_reason="guidance_violation",
    )
    ctx = _make_enforcement_ctx(db_session, _T3_GOOD_PLAN, _T3_TASK)

    with patch(
        "app.services.orchestration.phases.planning_guidance_enforcement._check_plan_violations",
        return_value=[],
    ), patch(
        "app.services.orchestration.phases.planning_guidance_enforcement.emit_phase_event",
    ):
        result = run_guidance_plan_enforcement(
            ctx,
            retry_state=retry_state,
            output_text=json.dumps(_T3_GOOD_PLAN),
            planning_timeout_seconds=240,
            prompt_profile="default",
            repair_fn=MagicMock(),
            emit_diagnostics_fn=MagicMock(),
        )

    assert result is None


@pytest.mark.usefixtures("db_session")
def test_no_typed_symbols_in_task_passes_second_pass(db_session):
    """No typed symbols → faithfulness check trivially passes."""
    from app.services.orchestration.phases.planning_guidance_enforcement import (
        run_guidance_plan_enforcement,
    )

    retry_state = SimpleNamespace(
        hg_repair_prompt_used=True,
        repair_prompt_used=False,
        last_repair_reason="guidance_violation",
    )
    ctx = _make_enforcement_ctx(
        db_session,
        _T3_BAD_PLAN,
        "Write a Python script that does stuff.",
    )

    with patch(
        "app.services.orchestration.phases.planning_guidance_enforcement._check_plan_violations",
        return_value=[],
    ), patch(
        "app.services.orchestration.phases.planning_guidance_enforcement.emit_phase_event",
    ):
        result = run_guidance_plan_enforcement(
            ctx,
            retry_state=retry_state,
            output_text=json.dumps(_T3_BAD_PLAN),
            planning_timeout_seconds=240,
            prompt_profile="default",
            repair_fn=MagicMock(),
            emit_diagnostics_fn=MagicMock(),
        )

    assert result is None


@pytest.mark.usefixtures("db_session")
def test_first_pass_unaffected_by_faithfulness_guard(db_session):
    """First-pass (hg_repair_prompt_used=False) triggers repair, not faithfulness check."""
    from app.services.orchestration.phases.planning_guidance_enforcement import (
        run_guidance_plan_enforcement,
    )

    retry_state = SimpleNamespace(
        hg_repair_prompt_used=False,
        repair_prompt_used=False,
        last_repair_reason=None,
        consecutive_failures=0,
    )
    ctx = _make_enforcement_ctx(db_session, _T3_BAD_PLAN, _T3_TASK)

    mock_repair_fn = MagicMock(return_value={"output": json.dumps(_T3_GOOD_PLAN)})

    with patch(
        "app.services.orchestration.phases.planning_guidance_enforcement._check_plan_violations",
        return_value=["mutable_default: categories = []"],
    ), patch(
        "app.services.orchestration.phases.planning_guidance_enforcement.emit_phase_event",
    ):
        result = run_guidance_plan_enforcement(
            ctx,
            retry_state=retry_state,
            output_text=json.dumps(_T3_BAD_PLAN),
            planning_timeout_seconds=240,
            prompt_profile="default",
            repair_fn=mock_repair_fn,
            emit_diagnostics_fn=MagicMock(),
        )

    # First pass returns the repair result, not a faithfulness failure
    assert result is not None
    assert not result.get("__faithfulness_failure__")
    mock_repair_fn.assert_called_once()
    assert retry_state.hg_repair_prompt_used is True
