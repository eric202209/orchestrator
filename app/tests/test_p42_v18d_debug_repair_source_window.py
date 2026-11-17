"""P42-V18-D: bounded Debug Repair shows the failing region of a long file.

A changed file over the 1200-character per-file budget used to be shown as
its leading prefix only, so a failing function further down was never in the
prompt and no exact ``replace_in_file`` anchor could be copied for it.  When
verification output names a line of an authorized source file, the excerpt is
now a line-aligned window of the file's exact current bytes around that line,
within the same per-file and total budgets.  The file stays [TRUNCATED], so a
whole-file ``write_file`` is still refused and stale anchors still fail.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

from app.services.orchestration.diagnostics.debug_feedback import (
    DebugFeedbackEnvelope,
    bounded_debug_repair_ops_authority_rejection,
    build_bounded_debug_repair_changed_file_context,
    build_bounded_debug_repair_prompt_with_metadata,
)
from app.services.orchestration.phases.execution_debug_repair import (
    _bounded_debug_repair_stale_replace_issues,
)
from app.services.orchestration.prompt_optimization import optimize_prompt
from app.tests.test_p42_v17n_bounded_debug_repair_safety import _run
from app.tests.test_p42_v18b_debug_repair_prompt_fidelity import (
    _CopyingDebugRepairRuntime,
    _rejection_reasons,
)
from app.tests.test_phase7f_debug_feedback import (
    _FakeRuntime,
    _install_role_owned_debug_runtime,
    _make_run_context,
)

PATH = "src/demo.py"
HEAD = '"""Demo module."""\n\n\n' + "".join(
    f"def helper_{index}(value: str) -> str:\n"
    f"    return value.replace('{index}', '-')\n\n\n"
    for index in range(24)
)
TARGET = (
    "def value(text: str) -> str:\n"
    "    chars = []\n"
    "    for char in text:\n"
    "        if char == '#':\n"
    "            raise ValueError('comment')\n"
    "        chars.append(char)\n"
    "    return ''.join(chars)  # end-of-value\n"
)
TAIL = "\n\ndef trailer() -> int:\n    return 1\n"
SOURCE = HEAD + TARGET + TAIL
RAISE_LINE = SOURCE[: SOURCE.index("raise ValueError")].count("\n") + 1
CHECK = "        if char == '#':\n            raise ValueError('comment')\n"
FIXED = "        if char == '#':\n            break\n"
PREFIX_LABEL = "[TRUNCATED: 1197 of"
WINDOW_KEY = "bounded_execution_debug_repair_changed_file_context_windows"


def _write(project_dir, relative_path: str, content: str) -> None:
    path = project_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _envelope(project_dir, **overrides) -> DebugFeedbackEnvelope:
    values = {
        "task_execution_id": 418,
        "task_id": 418,
        "step_index": 2,
        "failure_phase": "execution",
        "failed_command": "python3 -m pytest -q",
        "workspace_path": str(project_dir),
        "failure_class": "pytest_failure",
        "pytest_excerpt": f"{PATH}:{RAISE_LINE}: ValueError",
    }
    values.update(overrides)
    return DebugFeedbackEnvelope(**values)


def _section(rendered: str, path: str) -> tuple[str, str]:
    """Return (label, visible source) for ``path``; boundary lines removed."""

    block = rendered.split(f"--- {path}\n", 1)[1].split("\n--- ", 1)[0]
    label, rest = block.split("\n", 1)
    visible = "\n".join(
        line for line in rest.split("\n") if not line.startswith("[TRUNCATION")
    )
    return label, visible


def test_fixture_is_long_and_its_target_is_beyond_the_prefix():
    assert len(SOURCE) > 1200
    assert SOURCE.index("def value(") > 1200


def test_failing_function_is_shown_as_exact_current_bytes(tmp_path):
    _write(tmp_path, PATH, SOURCE)

    rendered, metadata = build_bounded_debug_repair_changed_file_context(
        _envelope(tmp_path), prior_source_paths=[PATH]
    )

    label, visible = _section(rendered, PATH)
    assert label.startswith("[TRUNCATED: ") and "lines " in label
    assert TARGET.rstrip("\n") in visible  # whole function, indentation intact
    assert visible in SOURCE  # one contiguous slice of the current file
    assert len(visible) <= 1197
    assert len(rendered) <= 3000
    assert rendered.count("[TRUNCATION BOUNDARY: ") == 1  # hidden lines above
    assert metadata[WINDOW_KEY][PATH]["selection"] == "traceback_local"
    assert metadata[
        "bounded_execution_debug_repair_changed_file_context_truncated_paths"
    ] == [PATH]
    assert (
        metadata["bounded_execution_debug_repair_changed_file_context_complete_paths"]
        == []
    )
    # An anchor copied from the window is exact; the file is still not
    # complete source truth, so a whole-file rewrite stays refused.
    assert CHECK in visible
    op = {"op": "replace_in_file", "path": PATH, "old": CHECK, "new": FIXED}
    assert _bounded_debug_repair_stale_replace_issues([op], tmp_path) == []
    assert (
        bounded_debug_repair_ops_authority_rejection(
            [{"op": "write_file", "path": PATH, "content": "x"}],
            authority=None,
            project_dir=tmp_path,
            complete_source_paths=metadata[
                "bounded_execution_debug_repair_changed_file_context_complete_paths"
            ],
        )
        == "missing_source_provenance"
    )


def test_cpython_traceback_with_absolute_workspace_path_selects_window(tmp_path):
    _write(tmp_path, PATH, SOURCE)
    envelope = _envelope(
        tmp_path,
        pytest_excerpt="",
        stderr_excerpt=(
            "Traceback (most recent call last):\n"
            f'  File "{tmp_path / PATH}", line {RAISE_LINE}, in value\n'
            "ValueError: comment"
        ),
    )

    rendered, metadata = build_bounded_debug_repair_changed_file_context(
        envelope, prior_source_paths=[PATH]
    )

    assert TARGET.rstrip("\n") in _section(rendered, PATH)[1]
    assert PATH in metadata[WINDOW_KEY]


def test_form_feed_does_not_shift_traceback_line_numbers(tmp_path):
    source = HEAD.replace("\n\n\ndef helper_3(", "\n\n\x0c\ndef helper_3(") + TARGET
    assert "\x0c" in source
    raise_line = source[: source.index("raise ValueError")].count("\n") + 1
    _write(tmp_path, PATH, source)
    envelope = _envelope(tmp_path, pytest_excerpt=f"{PATH}:{raise_line}: ValueError")

    rendered, _metadata = build_bounded_debug_repair_changed_file_context(
        envelope, prior_source_paths=[PATH]
    )

    assert TARGET.rstrip("\n") in _section(rendered, PATH)[1]


def test_without_a_usable_line_reference_the_prefix_is_unchanged(tmp_path):
    _write(tmp_path, PATH, SOURCE)
    _write(tmp_path, "tests/test_demo.py", "def test_value():\n    assert False\n")
    references = [
        "",
        f"FAILED tests/test_demo.py::test_value - {PATH}",  # path, no line
        "tests/test_demo.py:2: AssertionError",  # tests are never source
        f"../outside.py:{RAISE_LINE}: ValueError",  # escapes the workspace
        f"/etc/demo.py:{RAISE_LINE}: ValueError",  # absolute, outside
        f"src/missing.py:{RAISE_LINE}: ValueError",  # nonexistent
        f"{PATH}:0: ValueError",  # not a line
        f"{PATH}:9999: ValueError",  # beyond the current file
    ]
    for reference in references:
        rendered, metadata = build_bounded_debug_repair_changed_file_context(
            _envelope(tmp_path, pytest_excerpt=reference), prior_source_paths=[PATH]
        )

        label, visible = _section(rendered, PATH)
        assert label.startswith(PREFIX_LABEL), reference
        assert visible == SOURCE.strip()[:1197].rstrip(), reference
        assert metadata.get(WINDOW_KEY, {}) == {}, reference
        assert "src/missing.py" not in rendered


def test_reference_to_an_unauthorized_source_file_shows_nothing_of_it(tmp_path):
    _write(tmp_path, PATH, SOURCE)
    _write(tmp_path, "src/other.py", SOURCE)
    envelope = _envelope(
        tmp_path, pytest_excerpt=f"src/other.py:{RAISE_LINE}: ValueError"
    )

    rendered, metadata = build_bounded_debug_repair_changed_file_context(
        envelope, prior_source_paths=[PATH], allowed_source_paths=[PATH]
    )

    assert "src/other.py" not in rendered
    assert _section(rendered, PATH)[0].startswith(PREFIX_LABEL)
    assert metadata.get(WINDOW_KEY, {}) == {}


def test_small_file_stays_complete_with_a_line_reference(tmp_path):
    _write(tmp_path, PATH, TARGET)
    envelope = _envelope(tmp_path, pytest_excerpt=f"{PATH}:5: ValueError")

    rendered, metadata = build_bounded_debug_repair_changed_file_context(
        envelope, prior_source_paths=[PATH]
    )

    assert _section(rendered, PATH) == (
        f"[COMPLETE: all {len(TARGET)} characters shown]",
        TARGET.strip(),
    )
    assert metadata.get(WINDOW_KEY, {}) == {}


def test_function_larger_than_the_budget_is_centred_on_the_line(tmp_path):
    body = "".join(f"    step_{index} = text + '{index}'\n" for index in range(90))
    source = "def huge(text: str) -> str:\n" + body + "    raise ValueError('x')\n"
    line = source.count("\n")
    _write(tmp_path, PATH, source)
    envelope = _envelope(tmp_path, pytest_excerpt=f"{PATH}:{line}: ValueError")

    rendered, metadata = build_bounded_debug_repair_changed_file_context(
        envelope, prior_source_paths=[PATH]
    )

    visible = _section(rendered, PATH)[1]
    assert "    raise ValueError('x')" in visible
    assert visible in source
    assert len(visible) <= 1197
    assert metadata[WINDOW_KEY][PATH]["end_line"] == line


def test_stale_and_ambiguous_anchors_are_still_rejected(tmp_path):
    _write(tmp_path, PATH, SOURCE)
    # The window is current bytes; text from an earlier version is not there.
    stale = CHECK.replace("'#'", "'!'")
    shifted = CHECK.replace("\n            raise", "\n           raise")
    for old in (stale, shifted):
        issues = _bounded_debug_repair_stale_replace_issues(
            [{"op": "replace_in_file", "path": PATH, "old": old, "new": FIXED}],
            tmp_path,
        )
        assert [issue["reason"] for issue in issues] == ["old_text_not_found"]
    # Ambiguity is still the executor's exact-occurrence rule, unchanged.
    assert SOURCE.count("    return value.replace(") > 1


def test_window_bytes_survive_prompt_assembly_exactly(tmp_path):
    _write(tmp_path, PATH, SOURCE)

    result = build_bounded_debug_repair_prompt_with_metadata(
        _envelope(tmp_path), prior_source_paths=[PATH]
    )
    sent = optimize_prompt(result.prompt, max_tokens=25000, preserve_whitespace=True)

    assert TARGET.rstrip("\n") in sent
    assert "shows only an excerpt" in sent


STEP_FAILURE = {
    "status": "failed",
    "output": (
        "F                                                    [100%]\n"
        "    def test_value():\n"
        ">       assert value('a #b') == 'a '\n"
        f"{PATH}:{RAISE_LINE}: in value\n"
        "    raise ValueError('comment')\n"
        "E   ValueError: comment\n"
        "FAILED tests/test_demo.py::test_value"
    ),
    "error": "ValueError: comment",
    "returncode": 1,
}


def _copy_check_from_primary(prompt: str) -> dict:
    """Copy ``old`` exactly from the changed-file context the model received."""

    context = prompt[prompt.index("## CURRENT CONTENT OF IMPLICATED SOURCE FILES") :]
    start = context.index("        if char == '#':\n")
    old = context[start : context.index("('comment')\n", start) + len("('comment')\n")]
    return {
        "output": json.dumps(
            [
                {
                    "repair_type": "ops_fix",
                    "ops": [
                        {
                            "op": "replace_in_file",
                            "path": PATH,
                            "old": old,
                            "new": FIXED,
                        }
                    ],
                    "verification_command": f"python3 -m py_compile {PATH}",
                }
            ]
        )
    }


def test_production_loop_primary_anchor_copied_from_the_window_applies(
    db_session, tmp_path, monkeypatch
):
    runtime = _FakeRuntime([dict(STEP_FAILURE)])
    debug_runtime = _CopyingDebugRepairRuntime([_copy_check_from_primary])
    _install_role_owned_debug_runtime(monkeypatch, runtime, debug_runtime)
    ctx, _execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        expected_files=[PATH],
        step_overrides={
            "description": "Verify the value tests pass",
            "commands": ["custom-test-command"],
            "verification": "",
            "ops": [],
        },
    )
    source_path = ctx.orchestration_state.project_dir / PATH
    source_path.parent.mkdir(parents=True)
    source_path.write_text(SOURCE, encoding="utf-8")
    ctx = dataclasses.replace(
        ctx,
        restore_workspace_snapshot_if_needed=lambda reason: source_path.write_text(
            SOURCE, encoding="utf-8"
        ),
    )
    sc = SimpleNamespace(ctx=ctx)

    result = _run(sc.ctx)

    primary = debug_runtime.prompts[0]
    assert "lines " in primary and "[COMPLETE: all" not in primary
    assert TARGET.rstrip("\n") in primary
    assert result["status"] == "completed", result
    assert len(debug_runtime.prompts) == 1  # no stale-anchor correction needed
    assert source_path.read_text(encoding="utf-8") == SOURCE.replace(CHECK, FIXED)
    assert _rejection_reasons(sc.ctx) == []
