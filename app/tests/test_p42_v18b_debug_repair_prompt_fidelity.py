"""P42-V18-B: bounded Debug Repair prompts keep source bytes exact (provider-free).

TE 400's stale ``replace_in_file`` anchors were faithful copies of what the
model was sent. The role-owned DEBUG_REPAIR invocation passed every prompt
through ``optimize_prompt``, whose ``" ".join(prompt.split())`` flattened
changed-file source excerpts onto one line and collapsed every JSON-escaped
indentation run in the stale-replace correction's ``current_excerpt`` to one
space. A model copying ``old`` exactly from the prompt could therefore never
match the file.

The fake DEBUG_REPAIR runtime below answers by copying ``old`` verbatim from
the prompt it actually received.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.prompt_optimization import optimize_prompt
from app.services.orchestration.state.persistence import read_orchestration_events
from app.tests.test_p42_v17n_bounded_debug_repair_safety import _run
from app.tests.test_phase7f_debug_feedback import (
    _FakeDebugRepairRuntime,
    _FakeRuntime,
    _install_role_owned_debug_runtime,
    _make_run_context,
)

SOURCE = (
    "def value(text: str) -> str:\n"
    "    chars = []\n"
    "    for char in text:\n"
    "        if char == '#':\n"
    "            raise ValueError('comment')\n"
    "        chars.append(char)\n"
    "    return ''.join(chars)  # end-of-value\n"
)
FUNCTION_START = "def value("
FUNCTION_END = "# end-of-value"
FIXED_FUNCTION = (
    "def value(text: str) -> str:\n" "    return text.split(' #', 1)[0]  # end-of-value"
)
STEP_FAILURE = {
    "status": "failed",
    "output": (
        "F                                                    [100%]\n"
        "    def test_value():\n"
        ">       assert value('a #b') == 'a'\n"
        "src/demo.py:5: in value\n"
        "    raise ValueError('comment')\n"
        "E   ValueError: comment\n"
        "FAILED tests/test_demo.py::test_value"
    ),
    "error": "ValueError: comment",
    "returncode": 1,
}
# TE 400 shape: every indentation run collapsed to a single space.
COLLAPSED_OLD = re.sub(r"\n +", "\n ", SOURCE[: SOURCE.index(FUNCTION_END)])
CORRECTION_MARKER = "Failed replace_in_file targets with exact current file excerpts:"


def _ops_fix(old: str) -> dict:
    return {
        "output": json.dumps(
            [
                {
                    "repair_type": "ops_fix",
                    "ops": [
                        {
                            "op": "replace_in_file",
                            "path": "src/demo.py",
                            "old": old,
                            "new": FIXED_FUNCTION,
                        }
                    ],
                    "verification_command": "python3 -m py_compile src/demo.py",
                }
            ]
        )
    }


def _function_slice(text: str) -> str:
    start = text.index(FUNCTION_START)
    return text[start : text.index(FUNCTION_END, start) + len(FUNCTION_END)]


def _copy_from_primary(prompt: str) -> dict:
    """Copy ``old`` exactly from the changed-file context the model received."""
    context = prompt[prompt.index("## CURRENT CONTENT OF IMPLICATED SOURCE FILES") :]
    return _ops_fix(_function_slice(context))


def _copy_from_correction(prompt: str) -> dict:
    """Copy ``old`` exactly from the correction's ``current_excerpt``."""
    issues = json.loads(prompt.split(CORRECTION_MARKER, 1)[1])
    return _ops_fix(_function_slice(issues[0]["current_excerpt"]))


class _CopyingDebugRepairRuntime(_FakeDebugRepairRuntime):
    async def invoke_prompt(self, prompt, **kwargs):
        self.prompts.append(str(prompt))
        self.options.append(kwargs.get("invocation_options"))
        if not self.responses:
            raise AssertionError("Fake debug runtime received unexpected prompt")
        response = self.responses.pop(0)
        return dict(response(str(prompt)) if callable(response) else response)


def _scenario(db_session, tmp_path, monkeypatch, responses):
    runtime = _FakeRuntime([dict(STEP_FAILURE)])
    debug_runtime = _CopyingDebugRepairRuntime(responses)
    _install_role_owned_debug_runtime(monkeypatch, runtime, debug_runtime)
    ctx, _execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        expected_files=["src/demo.py"],
        step_overrides={
            "description": "Verify the value tests pass",
            "commands": ["custom-test-command"],
            "verification": "",
            "ops": [],
        },
    )
    source_path = ctx.orchestration_state.project_dir / "src" / "demo.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(SOURCE, encoding="utf-8")
    restores: list[str] = []

    def _restore(reason):
        restores.append(reason)
        source_path.write_text(SOURCE, encoding="utf-8")

    ctx = dataclasses.replace(ctx, restore_workspace_snapshot_if_needed=_restore)
    return SimpleNamespace(
        ctx=ctx, debug=debug_runtime, source_path=source_path, restores=restores
    )


def _rejection_reasons(ctx) -> list:
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    return [
        e["details"].get("debug_repair_rejection_reason")
        for e in events
        if e.get("event_type") == EventType.REPAIR_REJECTED
    ]


def test_primary_prompt_keeps_exact_source_so_a_faithful_copy_applies(
    db_session, tmp_path, monkeypatch
):
    sc = _scenario(db_session, tmp_path, monkeypatch, [_copy_from_primary])

    result = _run(sc.ctx)

    primary = sc.debug.prompts[0]
    assert "[COMPLETE: all" in primary
    assert SOURCE.strip() in primary  # newlines and indentation intact
    assert result["status"] == "completed", result
    assert len(sc.debug.prompts) == 1  # no correction was needed
    assert FIXED_FUNCTION in sc.source_path.read_text(encoding="utf-8")
    assert _rejection_reasons(sc.ctx) == []


def test_correction_prompt_keeps_exact_current_source(
    db_session, tmp_path, monkeypatch
):
    sc = _scenario(
        db_session,
        tmp_path,
        monkeypatch,
        [_ops_fix(COLLAPSED_OLD), _copy_from_correction],
    )

    result = _run(sc.ctx)

    correction = sc.debug.prompts[1]
    assert json.dumps(SOURCE)[1:-1] in correction  # escaped, byte-exact
    assert result["status"] == "completed", result
    assert len(sc.debug.prompts) == 2  # one bounded correction, no more
    assert FIXED_FUNCTION in sc.source_path.read_text(encoding="utf-8")


def test_collapsed_indentation_anchor_is_still_rejected(
    db_session, tmp_path, monkeypatch
):
    assert COLLAPSED_OLD not in SOURCE
    sc = _scenario(
        db_session,
        tmp_path,
        monkeypatch,
        [_ops_fix(COLLAPSED_OLD), _ops_fix(COLLAPSED_OLD)],
    )

    result = _run(sc.ctx)

    assert result["status"] == "failed", result
    assert _rejection_reasons(sc.ctx)[-1] == "stale_replace_after_correction"
    assert len(sc.debug.prompts) == 2
    assert sc.restores == ["debug parse error"]
    assert sc.source_path.read_text(encoding="utf-8") == SOURCE


def test_optimize_prompt_preserve_whitespace_contract():
    text = "a\n    b\n\n\tc  d"
    # Default behaviour, used by other callers, is unchanged.
    assert optimize_prompt(text) == "a b c d"
    assert optimize_prompt(text, preserve_whitespace=True) == text
    # The existing length bound still applies when whitespace is preserved.
    long_text = "line\n    indented\n" * 200
    bounded = optimize_prompt(long_text, max_tokens=1000, preserve_whitespace=True)
    assert len(long_text) > 3000
    assert len(bounded) <= 3000 + len("\n\n[Content truncated for performance]\n\n")
    assert "[Content truncated for performance]" in bounded
    assert bounded.startswith("line\n    indented\n")
